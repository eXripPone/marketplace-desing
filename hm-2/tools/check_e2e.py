"""Black-box API checks against Docker Compose. Uses only the Python standard library."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from uuid import uuid4

BASE = os.environ.get("API_URL", "http://localhost:8001")
ROOT = Path(__file__).resolve().parents[1]
SUFFIX = uuid4().hex[:10]
checks = 0


def request(method, path, body=None, token=None, expected=200, code=None):
    global checks
    headers={"Content-Type":"application/json"}
    if token: headers["Authorization"]="Bearer "+token
    req=Request(BASE+path,data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
    try:
        response=urlopen(req,timeout=20)
    except HTTPError as error:
        response=error
    raw=response.read()
    result=json.loads(raw) if raw else None
    assert response.status==expected,(method,path,response.status,expected,result)
    assert response.headers.get("X-Request-Id"),(path,"Missing X-Request-Id")
    if code: assert result["error_code"]==code,result
    checks+=1
    return result


def login(email):
    return request("POST","/auth/login",{"email":email,"password":"DemoPass123!"})


def buyer(label):
    email=f"{label}-{SUFFIX}@example.com"
    user=request("POST","/auth/register",{"email":email,"password":"DemoPass123!"},expected=201)
    return user,login(email)["access_token"]


def sql(query):
    process=subprocess.run(["docker","compose","exec","-T","db","psql","-U","marketplace","-d","marketplace","-At","-c",query],
                           cwd=ROOT,check=True,capture_output=True,text=True)
    return process.stdout.strip()


def product(token,**changes):
    data={"name":"Test "+SUFFIX,"description":None,"price":100,"stock":50,"category":SUFFIX,"status":"ACTIVE"}
    data.update(changes)
    return request("POST","/products",data,token,201)


def promo(token,label,**changes):
    now=datetime.now(timezone.utc)
    data={"code":(label+SUFFIX).upper(),"discount_type":"PERCENTAGE","discount_value":20,
          "min_order_amount":500,"max_uses":5,"valid_from":(now-timedelta(days=1)).isoformat(),
          "valid_until":(now+timedelta(days=1)).isoformat(),"active":True}
    data.update(changes)
    return request("POST","/promo-codes",data,token,201)


def expired_token(user_id):
    def b64(data): return base64.urlsafe_b64encode(json.dumps(data,separators=(",",":")).encode()).rstrip(b"=")
    now=int(time.time())
    payload={"sub":str(user_id),"role":"USER","type":"access","jti":str(uuid4()),"iat":now-120,"exp":now-60}
    message=b64({"alg":"HS256","typ":"JWT"})+b"."+b64(payload)
    secret=os.environ.get("JWT_SECRET","local-study-secret-change-before-deployment-12345")
    signature=base64.urlsafe_b64encode(hmac.new(secret.encode(),message,hashlib.sha256).digest()).rstrip(b"=")
    return (message+b"."+signature).decode()


def main():
    request("GET","/health")
    request("GET","/products",expected=401,code="TOKEN_INVALID")
    request("POST","/auth/login",{"email":"bad@example.com","password":"wrong"},expected=401,code="TOKEN_INVALID")
    admin=login("admin@example.com")["access_token"]
    seller=login("seller@example.com")["access_token"]
    user,token=buyer("base")
    request("GET","/products",token=expired_token(user["id"]),expected=401,code="TOKEN_EXPIRED")
    pair=login(f"base-{SUFFIX}@example.com")
    request("POST","/auth/refresh",{"refresh_token":pair["refresh_token"]})
    request("POST","/auth/refresh",{"refresh_token":pair["access_token"]},expected=401,code="REFRESH_TOKEN_INVALID")
    request("GET","/products",token=pair["refresh_token"],expected=401,code="TOKEN_INVALID")
    print("PASS: authentication, token types and expiration")

    data={"name":"Test","price":100,"stock":5,"category":SUFFIX,"status":"ACTIVE"}
    request("POST","/products",data,token,403,"ACCESS_DENIED")
    for field,value in [("name",""),("name","x"*256),("description","x"*4001),("price",0),("price",-1),
                        ("price","10.00"),("price",0.001),("stock",-1),("category",""),("category","x"*101),("status","WRONG")]:
        error=request("POST","/products",{**data,field:value},seller,400,"VALIDATION_ERROR")
        assert error.get("details"),error
    p=product(seller)
    own=product(admin)
    request("DELETE",f"/products/{own['id']}",token=seller,expected=403,code="ACCESS_DENIED")
    got=request("GET",f"/products/{p['id']}",token=token)
    assert got["price"]==100 and got["created_at"] and got["seller_id"]
    page=request("GET",f"/products?page=0&size=1&status=ACTIVE&category={SUFFIX}",token=token)
    assert len(page["items"])==1 and page["totalElements"]==2
    empty=request("GET",f"/products?page=999&size=20&category={SUFFIX}",token=token)
    assert empty["items"]==[] and empty["totalElements"]==2
    request("GET","/products?status=WRONG",token=token,expected=400,code="VALIDATION_ERROR")
    request("GET","/products?page=-1",token=token,expected=400,code="VALIDATION_ERROR")
    request("GET","/products/999999999",token=token,expected=404,code="PRODUCT_NOT_FOUND")
    request("DELETE",f"/products/{own['id']}",token=admin,expected=204)
    archived=request("GET",f"/products/{own['id']}",token=token)
    assert archived["status"]=="ARCHIVED"
    assert sql(f"SELECT status FROM products WHERE id={own['id']}")=="ARCHIVED"
    print("PASS: CRUD, validation, filtering, pagination, ownership and SQL soft delete")

    for items in [[],[{"product_id":p["id"],"quantity":0}],[{"product_id":p["id"],"quantity":1000}],
                  [{"product_id":p["id"],"quantity":1}]*51]:
        request("POST","/orders",{"items":items},token,400,"VALIDATION_ERROR")
    request("POST","/orders",{"items":[{"product_id":p["id"],"quantity":1}],"promo_code":"bad"},token,400,"VALIDATION_ERROR")
    request("POST","/orders",{"items":[{"product_id":999999999,"quantity":1}]},token,404,"PRODUCT_NOT_FOUND")
    request("POST","/orders",{"items":[{"product_id":own["id"],"quantity":1}]},token,409,"PRODUCT_INACTIVE")
    request("POST","/orders",{"items":[{"product_id":p["id"],"quantity":51}]},token,409,"INSUFFICIENT_STOCK")
    request("POST","/orders",{"items":[{"product_id":p["id"],"quantity":30},{"product_id":p["id"],"quantity":30}]},token,409,"INSUFFICIENT_STOCK")
    request("POST","/orders",{"items":[{"product_id":p["id"],"quantity":1}],"promo_code":"MISSING_CODE"},token,422,"PROMO_CODE_INVALID")
    assert request("GET",f"/products/{p['id']}",token=token)["stock"]==50
    assert sql(f"SELECT count(*) FROM orders WHERE user_id={user['id']}")=="0"
    promo1=promo(seller,"SALE")
    request("POST","/orders",{"items":[{"product_id":p["id"],"quantity":1}],"promo_code":promo1["code"]},token,422,"PROMO_CODE_MIN_AMOUNT")
    order=request("POST","/orders",{"items":[{"product_id":p["id"],"quantity":10}],"promo_code":promo1["code"]},token,201)
    assert order["total_amount"]==800 and order["discount_amount"]==200
    assert request("GET",f"/products/{p['id']}",token=token)["stock"]==40
    request("POST","/orders",{"items":[{"product_id":p["id"],"quantity":1}]},token,429,"ORDER_LIMIT_EXCEEDED")
    sql(f"UPDATE user_operations SET created_at=now()-interval '2 minutes' WHERE user_id={user['id']}")
    request("POST","/orders",{"items":[{"product_id":p["id"],"quantity":1}]},token,409,"ORDER_HAS_ACTIVE")
    _,other=buyer("other")
    request("GET",f"/orders/{order['id']}",token=other,expected=403,code="ORDER_OWNERSHIP_VIOLATION")
    request("GET",f"/orders/{order['id']}",token=seller,expected=403,code="ACCESS_DENIED")
    request("GET","/orders/999999999",token=token,expected=404,code="ORDER_NOT_FOUND")
    request("PUT",f"/orders/{order['id']}",{"items":[{"product_id":p["id"],"quantity":100}]},token,409,"INSUFFICIENT_STOCK")
    assert request("GET",f"/products/{p['id']}",token=token)["stock"]==40
    updated=request("PUT",f"/orders/{order['id']}",{"items":[{"product_id":p["id"],"quantity":1}]},token)
    assert updated["total_amount"]==100 and updated["promo_code_id"] is None
    assert sql(f"SELECT current_uses FROM promo_codes WHERE id={promo1['id']}")=="0"
    request("PUT",f"/orders/{order['id']}",{"items":[{"product_id":p["id"],"quantity":2}]},token,429,"ORDER_LIMIT_EXCEEDED")
    request("POST",f"/orders/{order['id']}/cancel",token=token)
    request("POST",f"/orders/{order['id']}/cancel",token=token,expected=409,code="INVALID_STATE_TRANSITION")
    assert request("GET",f"/products/{p['id']}",token=token)["stock"]==50
    print("PASS: order checks, rollback, promo removal, rate limit and cancellation")

    _,snapshot_token=buyer("snapshot")
    snapshot=request("POST","/orders",{"items":[{"product_id":p["id"],"quantity":1}]},snapshot_token,201)
    changed={"name":"Changed","price":150,"stock":49,"category":SUFFIX,"status":"ACTIVE"}
    request("PUT",f"/products/{p['id']}",changed,seller)
    assert request("GET",f"/orders/{snapshot['id']}",token=snapshot_token)["items"][0]["price_at_order"]==100
    request("PUT",f"/orders/{snapshot['id']}/status",{"status":"PAID"},admin,409,"INVALID_STATE_TRANSITION")
    for status in ["PAYMENT_PENDING","PAID","SHIPPED","COMPLETED"]:
        request("PUT",f"/orders/{snapshot['id']}/status",{"status":status},admin)
    request("POST",f"/orders/{snapshot['id']}/cancel",token=snapshot_token,expected=409,code="INVALID_STATE_TRANSITION")
    request("PUT",f"/orders/{snapshot['id']}",{"items":[{"product_id":p["id"],"quantity":1}]},snapshot_token,409,"INVALID_STATE_TRANSITION")
    print("PASS: price snapshot and state machine")

    scarce=product(seller,stock=1)
    _,a=buyer("racea")
    _,b=buyer("raceb")
    def buy(t):
        req=Request(BASE+"/orders",data=json.dumps({"items":[{"product_id":scarce["id"],"quantity":1}]}).encode(),
                    headers={"Authorization":"Bearer "+t,"Content-Type":"application/json"},method="POST")
        try: return urlopen(req,timeout=20).status
        except HTTPError as e: return e.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses=sorted(pool.map(buy,[a,b]))
    assert statuses==[201,409],statuses
    assert request("GET",f"/products/{scarce['id']}",token=token)["stock"]==0
    print("PASS: concurrent purchase does not oversell stock")

    # Cancellation releases both stock and the promo usage exactly once.
    _,cancel_token=buyer("cancelpromo")
    cancellable=request("POST","/orders",{"items":[{"product_id":p["id"],"quantity":5}],"promo_code":promo1["code"]},cancel_token,201)
    assert sql(f"SELECT current_uses FROM promo_codes WHERE id={promo1['id']}")=="1"
    request("POST",f"/orders/{cancellable['id']}/cancel",token=cancel_token)
    assert sql(f"SELECT current_uses FROM promo_codes WHERE id={promo1['id']}")=="0"

    # Fixed discount cannot exceed the order amount; expired promo rolls stock back.
    fixed=promo(seller,"FIXED",discount_type="FIXED_AMOUNT",discount_value=9999,min_order_amount=0)
    _,fixed_token=buyer("fixed")
    fixed_order=request("POST","/orders",{"items":[{"product_id":p["id"],"quantity":1}],"promo_code":fixed["code"]},fixed_token,201)
    assert fixed_order["total_amount"]==0 and fixed_order["discount_amount"]==150
    expired=promo(seller,"EXPIRED",valid_from="2020-01-01T00:00:00Z",valid_until="2020-02-01T00:00:00Z",min_order_amount=0)
    _,expired_buyer=buyer("expiredpromo")
    before=request("GET",f"/products/{p['id']}",token=token)["stock"]
    request("POST","/orders",{"items":[{"product_id":p["id"],"quantity":1}],"promo_code":expired["code"]},expired_buyer,422,"PROMO_CODE_INVALID")
    assert request("GET",f"/products/{p['id']}",token=token)["stock"]==before
    over70=promo(seller,"OVER",discount_value=80,min_order_amount=0)
    _,over_token=buyer("over70")
    full=request("POST","/orders",{"items":[{"product_id":p["id"],"quantity":1}],"promo_code":over70["code"]},over_token,201)
    assert full["discount_amount"]==0 and full["total_amount"]==150

    # Two users compete for the final promo use; the loser must not reserve stock.
    limited=promo(seller,"LIMIT",max_uses=1,min_order_amount=0)
    item=product(seller,stock=3)
    _,ta=buyer("promoa")
    _,tb=buyer("promob")
    def use_promo(t):
        body={"items":[{"product_id":item["id"],"quantity":1}],"promo_code":limited["code"]}
        req=Request(BASE+"/orders",data=json.dumps(body).encode(),
                    headers={"Authorization":"Bearer "+t,"Content-Type":"application/json"},method="POST")
        try: return urlopen(req,timeout=20).status
        except HTTPError as e: return e.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses=sorted(pool.map(use_promo,[ta,tb]))
    assert statuses==[201,422],statuses
    assert sql(f"SELECT current_uses FROM promo_codes WHERE id={limited['id']}")=="1"
    assert request("GET",f"/products/{item['id']}",token=token)["stock"]==2
    print("PASS: promo cancellation, fixed discount, expiry, >70% interpretation and concurrent last use")
    print(f"SUCCESS: {checks} HTTP checks plus SQL and concurrency assertions")


if __name__=="__main__":
    main()
