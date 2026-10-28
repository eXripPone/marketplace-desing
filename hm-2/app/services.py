"""Business rules. Each write operation uses one PostgreSQL transaction."""
import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from psycopg.errors import UniqueViolation
from app.db import connect
from app.errors import fail
from app.security import current_user, require_role, hash_password, verify_password, tokens, decode_token

CENT = Decimal("0.01")
INTERVAL = float(os.environ.get("ORDER_INTERVAL_MINUTES", "1"))


def register(data):
    try:
        with connect() as db:
            return db.execute("INSERT INTO users(email,password_hash) VALUES(%s,%s) RETURNING id,email,role",
                              (data.email.lower(), hash_password(data.password))).fetchone()
    except UniqueViolation:
        fail("EMAIL_EXISTS", "Email уже зарегистрирован")


def login(data):
    with connect() as db:
        user = db.execute("SELECT * FROM users WHERE email=%s", (data.email.lower(),)).fetchone()
    if not user or not verify_password(data.password, user["password_hash"]):
        fail("TOKEN_INVALID", "Неверный email или пароль", 401)
    return tokens(user)


def refresh(data):
    return tokens(decode_token(data.refresh_token, "refresh"))


def find_product(db, product_id, lock=False):
    product = db.execute("SELECT * FROM products WHERE id=%s" + (" FOR UPDATE" if lock else ""), (product_id,)).fetchone()
    if not product:
        fail("PRODUCT_NOT_FOUND", "Товар не найден", 404)
    return product


def check_seller(user, product):
    if user["role"] != "ADMIN" and product["seller_id"] != user["id"]:
        fail("ACCESS_DENIED", "Товар принадлежит другому продавцу", 403)


def get_product(product_id):
    with connect() as db:
        return find_product(db, product_id)


def list_products(page=0, size=20, status=None, category=None):
    conditions, params = [], []
    if status is not None:
        conditions.append("status=%s")
        params.append(status)
    if category is not None:
        conditions.append("category=%s")
        params.append(category)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with connect() as db:
        # Repeatable snapshot keeps count and page consistent within this request.
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        total = db.execute("SELECT count(*) AS total FROM products" + where, params).fetchone()["total"]
        items = db.execute("SELECT * FROM products" + where + " ORDER BY id LIMIT %s OFFSET %s",
                           params + [size, page * size]).fetchall()
    return {"items": items, "totalElements": total, "page": page, "size": size}


def create_product(data):
    user = require_role("SELLER", "ADMIN")
    seller_id = data.seller_id or user["id"]
    if user["role"] == "SELLER" and seller_id != user["id"]:
        fail("ACCESS_DENIED", "Продавец создаёт только свои товары", 403)
    with connect() as db:
        owner = db.execute("SELECT id,role FROM users WHERE id=%s", (seller_id,)).fetchone()
        if not owner or owner["role"] not in ("SELLER", "ADMIN"):
            fail("VALIDATION_ERROR", "Укажите существующего продавца", 400, {"seller_id": "SELLER или ADMIN"})
        return db.execute("""INSERT INTO products(name,description,price,stock,category,status,seller_id)
            VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (data.name, data.description, data.price, data.stock, data.category, data.status, seller_id)).fetchone()


def update_product(product_id, data):
    user = require_role("SELLER", "ADMIN")
    with connect() as db:
        product = find_product(db, product_id, True)
        check_seller(user, product)
        seller_id = data.seller_id or product["seller_id"]
        if user["role"] == "SELLER" and seller_id != user["id"]:
            fail("ACCESS_DENIED", "Нельзя передать товар другому продавцу", 403)
        owner = db.execute("SELECT role FROM users WHERE id=%s", (seller_id,)).fetchone()
        if not owner or owner["role"] not in ("SELLER", "ADMIN"):
            fail("VALIDATION_ERROR", "Продавец не найден", 400, {"seller_id": "SELLER или ADMIN"})
        return db.execute("""UPDATE products SET name=%s,description=%s,price=%s,stock=%s,category=%s,status=%s,seller_id=%s
            WHERE id=%s RETURNING *""", (data.name,data.description,data.price,data.stock,data.category,data.status,seller_id,product_id)).fetchone()


def archive_product(product_id):
    user = require_role("SELLER", "ADMIN")
    with connect() as db:
        product = find_product(db, product_id, True)
        check_seller(user, product)
        db.execute("UPDATE products SET status='ARCHIVED' WHERE id=%s", (product_id,))


def check_rate(db, user_id, operation):
    row = db.execute("SELECT created_at FROM user_operations WHERE user_id=%s AND operation_type=%s ORDER BY created_at DESC LIMIT 1",
                     (user_id, operation)).fetchone()
    if row and (datetime.now(timezone.utc) - row["created_at"]).total_seconds() < INTERVAL * 60:
        fail("ORDER_LIMIT_EXCEEDED", "Операция выполняется слишком часто", 429, {"interval_minutes": INTERVAL})


def record_operation(db, user_id, operation):
    db.execute("INSERT INTO user_operations(user_id,operation_type) VALUES(%s,%s)", (user_id, operation))


def find_order(db, order_id, lock=False):
    order = db.execute("SELECT * FROM orders WHERE id=%s" + (" FOR UPDATE" if lock else ""), (order_id,)).fetchone()
    if not order:
        fail("ORDER_NOT_FOUND", "Заказ не найден", 404)
    return order


def order_owner(user, order):
    if user["role"] != "ADMIN" and user["id"] != order["user_id"]:
        fail("ORDER_OWNERSHIP_VIOLATION", "Заказ принадлежит другому пользователю", 403)


def order_result(db, order_id):
    order = find_order(db, order_id)
    order["items"] = db.execute("SELECT id,product_id,quantity,price_at_order FROM order_items WHERE order_id=%s ORDER BY id", (order_id,)).fetchall()
    return order


def quantities(items):
    # Duplicate product IDs are combined before checking/reserving stock.
    result = {}
    for item in items:
        result[item.product_id] = result.get(item.product_id, 0) + item.quantity
    return result


def lock_products(db, ids):
    rows = db.execute("SELECT * FROM products WHERE id=ANY(%s) ORDER BY id FOR UPDATE", (sorted(ids),)).fetchall()
    return {row["id"]: row for row in rows}


def check_items(products, requested):
    for product_id in requested:
        if product_id not in products:
            fail("PRODUCT_NOT_FOUND", "Товар не найден", 404, {"product_id": product_id})
        if products[product_id]["status"] != "ACTIVE":
            fail("PRODUCT_INACTIVE", "Товар неактивен", 409, {"product_id": product_id})
    shortages = [{"product_id": p, "requested": q, "available": products[p]["stock"]}
                 for p,q in requested.items() if products[p]["stock"] < q]
    if shortages:
        fail("INSUFFICIENT_STOCK", "Недостаточно товара на складе", 409, {"products": shortages})


def reserve_items(db, order_id, products, requested):
    for product_id, count in requested.items():
        db.execute("UPDATE products SET stock=stock-%s WHERE id=%s", (count, product_id))
        db.execute("INSERT INTO order_items(order_id,product_id,quantity,price_at_order) VALUES(%s,%s,%s,%s)",
                   (order_id, product_id, count, products[product_id]["price"]))


def promo_discount(promo, subtotal, existing=False):
    now = datetime.now(timezone.utc)
    used = promo["current_uses"] - (1 if existing else 0)
    if not promo["active"] or used >= promo["max_uses"] or not (promo["valid_from"] <= now <= promo["valid_until"]):
        fail("PROMO_CODE_INVALID", "Промокод истёк, отключён или исчерпан", 422)
    if subtotal < promo["min_order_amount"]:
        fail("PROMO_CODE_MIN_AMOUNT", "Сумма ниже минимальной для промокода", 422)
    if promo["discount_type"] == "PERCENTAGE":
        discount = subtotal * promo["discount_value"] / Decimal(100)
        # Interpretation of the assignment: >70% means full price (no discount).
        if discount > subtotal * Decimal("0.70"):
            discount = Decimal(0)
    else:
        discount = min(promo["discount_value"], subtotal)
    return discount.quantize(CENT, rounding=ROUND_HALF_UP)


def get_order(order_id):
    user = require_role("USER", "ADMIN")
    with connect() as db:
        order_owner(user, find_order(db, order_id))
        return order_result(db, order_id)


def create_order(data):
    user = require_role("USER", "ADMIN")
    with connect() as db:
        # Serializes create/update requests by the same actor, including the first operation.
        db.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (user["id"],))
        check_rate(db, user["id"], "CREATE_ORDER")
        if db.execute("SELECT id FROM orders WHERE user_id=%s AND status IN ('CREATED','PAYMENT_PENDING')", (user["id"],)).fetchone():
            fail("ORDER_HAS_ACTIVE", "У пользователя уже есть активный заказ")
        requested = quantities(data.items)
        products = lock_products(db, requested)
        check_items(products, requested)
        subtotal = sum((products[p]["price"] * q for p,q in requested.items()), Decimal(0))
        if subtotal > Decimal("9999999999.99"):
            fail("VALIDATION_ERROR", "Сумма заказа превышает диапазон DECIMAL(12,2)", 400,
                 {"fields": [{"field": "items", "message": "Уменьшите количество или стоимость"}]})
        # Stock reservation is rolled back together with the order if promo checks fail.
        order = db.execute("INSERT INTO orders(user_id,total_amount) VALUES(%s,%s) RETURNING id", (user["id"],subtotal)).fetchone()
        reserve_items(db, order["id"], products, requested)
        discount, promo_id = Decimal(0), None
        if data.promo_code:
            promo = db.execute("SELECT * FROM promo_codes WHERE code=%s FOR UPDATE", (data.promo_code,)).fetchone()
            if not promo:
                fail("PROMO_CODE_INVALID", "Промокод не найден", 422)
            discount = promo_discount(promo, subtotal)
            promo_id = promo["id"]
            db.execute("UPDATE promo_codes SET current_uses=current_uses+1 WHERE id=%s", (promo_id,))
        db.execute("UPDATE orders SET total_amount=%s,discount_amount=%s,promo_code_id=%s WHERE id=%s",
                   (subtotal-discount,discount,promo_id,order["id"]))
        record_operation(db, user["id"], "CREATE_ORDER")
        return order_result(db, order["id"])


def update_order(order_id, data):
    user = require_role("USER", "ADMIN")
    with connect() as db:
        db.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (user["id"],))
        order = find_order(db, order_id, True)
        order_owner(user, order)
        if order["status"] != "CREATED":
            fail("INVALID_STATE_TRANSITION", "Изменение позиций разрешено только в CREATED")
        check_rate(db, user["id"], "UPDATE_ORDER")
        old = db.execute("SELECT * FROM order_items WHERE order_id=%s", (order_id,)).fetchall()
        requested = quantities(data.items)
        products = lock_products(db, set(requested) | {i["product_id"] for i in old})
        for item in old:
            db.execute("UPDATE products SET stock=stock+%s WHERE id=%s", (item["quantity"],item["product_id"]))
            products[item["product_id"]]["stock"] += item["quantity"]
        check_items(products, requested)
        db.execute("DELETE FROM order_items WHERE order_id=%s", (order_id,))
        reserve_items(db, order_id, products, requested)
        subtotal = sum((products[p]["price"]*q for p,q in requested.items()), Decimal(0))
        if subtotal > Decimal("9999999999.99"):
            fail("VALIDATION_ERROR", "Сумма заказа превышает диапазон DECIMAL(12,2)", 400,
                 {"fields": [{"field": "items", "message": "Уменьшите количество или стоимость"}]})
        discount, promo_id = Decimal(0), order["promo_code_id"]
        if promo_id:
            promo = db.execute("SELECT * FROM promo_codes WHERE id=%s FOR UPDATE", (promo_id,)).fetchone()
            if subtotal < promo["min_order_amount"]:
                db.execute("UPDATE promo_codes SET current_uses=current_uses-1 WHERE id=%s", (promo_id,))
                promo_id = None
            else:
                discount = promo_discount(promo, subtotal, existing=True)
        db.execute("UPDATE orders SET total_amount=%s,discount_amount=%s,promo_code_id=%s WHERE id=%s",
                   (subtotal-discount,discount,promo_id,order_id))
        record_operation(db,user["id"],"UPDATE_ORDER")
        return order_result(db,order_id)


def cancel_order(order_id):
    user = require_role("USER", "ADMIN")
    with connect() as db:
        order = find_order(db,order_id,True)
        order_owner(user,order)
        if order["status"] not in ("CREATED","PAYMENT_PENDING"):
            fail("INVALID_STATE_TRANSITION", "Заказ нельзя отменить в этом состоянии")
        items = db.execute("SELECT * FROM order_items WHERE order_id=%s", (order_id,)).fetchall()
        lock_products(db,{item["product_id"] for item in items})
        for item in items:
            db.execute("UPDATE products SET stock=stock+%s WHERE id=%s", (item["quantity"],item["product_id"]))
        if order["promo_code_id"]:
            db.execute("UPDATE promo_codes SET current_uses=current_uses-1 WHERE id=%s", (order["promo_code_id"],))
        db.execute("UPDATE orders SET status='CANCELED' WHERE id=%s", (order_id,))
        return order_result(db,order_id)


def change_status(order_id,data):
    require_role("ADMIN")
    transitions={"CREATED":"PAYMENT_PENDING","PAYMENT_PENDING":"PAID","PAID":"SHIPPED","SHIPPED":"COMPLETED"}
    with connect() as db:
        order = find_order(db,order_id,True)
        if transitions.get(order["status"]) != data.status:
            fail("INVALID_STATE_TRANSITION", "Недопустимый переход состояния")
        return db.execute("UPDATE orders SET status=%s WHERE id=%s RETURNING *", (data.status,order_id)).fetchone() | {
            "items": db.execute("SELECT id,product_id,quantity,price_at_order FROM order_items WHERE order_id=%s ORDER BY id",(order_id,)).fetchall()}


def create_promo(data):
    require_role("SELLER","ADMIN")
    start = data.valid_from.replace(tzinfo=timezone.utc) if data.valid_from.tzinfo is None else data.valid_from
    end = data.valid_until.replace(tzinfo=timezone.utc) if data.valid_until.tzinfo is None else data.valid_until
    if end < start:
        fail("VALIDATION_ERROR", "Неверный интервал действия",400,{"valid_until":"Должно быть не раньше valid_from"})
    try:
        with connect() as db:
            return db.execute("""INSERT INTO promo_codes(code,discount_type,discount_value,min_order_amount,max_uses,valid_from,valid_until,active)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (data.code,data.discount_type,data.discount_value,data.min_order_amount or Decimal(0),data.max_uses,start,end,True if data.active is None else data.active)).fetchone()
    except UniqueViolation:
        fail("PROMO_CODE_EXISTS","Промокод уже существует")
