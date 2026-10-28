import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from contextvars import ContextVar
from uuid import uuid4

import jwt
from app.db import connect
from app.errors import fail

current_user = ContextVar("current_user", default=None)
SECRET = os.environ.get("JWT_SECRET", "local-study-secret-change-before-deployment-12345")


def hash_password(password):
    salt = os.urandom(16).hex()
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200000).hex()
    return salt + ":" + hashed


def verify_password(password, saved):
    salt, hashed = saved.split(":")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200000).hex()
    return hmac.compare_digest(actual, hashed)


def tokens(user):
    now = datetime.now(timezone.utc)
    def issue(kind, lifetime):
        return jwt.encode({"sub": str(user["id"]), "role": user["role"], "type": kind,
                           "iat": now, "exp": now + lifetime, "jti": str(uuid4())}, SECRET, algorithm="HS256")
    return {"access_token": issue("access", timedelta(minutes=20)),
            "refresh_token": issue("refresh", timedelta(days=14)), "token_type": "bearer"}


def decode_token(token, kind="access"):
    try:
        data = jwt.decode(token, SECRET, algorithms=["HS256"],
                          options={"require": ["sub", "role", "type", "exp", "iat", "jti"]})
        if data["type"] != kind:
            raise jwt.InvalidTokenError("Wrong token type")
        user_id = int(data["sub"])
        with connect() as db:
            user = db.execute("SELECT id,email,role FROM users WHERE id=%s", (user_id,)).fetchone()
        if not user or user["role"] != data["role"]:
            raise jwt.InvalidTokenError("Unknown user or changed role")
        return user
    except jwt.ExpiredSignatureError:
        fail("REFRESH_TOKEN_INVALID" if kind == "refresh" else "TOKEN_EXPIRED", "Токен истёк", 401)
    except (jwt.InvalidTokenError, ValueError, TypeError):
        fail("REFRESH_TOKEN_INVALID" if kind == "refresh" else "TOKEN_INVALID", "Невалидный токен", 401)


def require_role(*roles):
    user = current_user.get()
    if not user or user["role"] not in roles:
        fail("ACCESS_DENIED", "Недостаточно прав", 403)
    return user


def seed_accounts():
    with connect() as db:
        for email, role in [("admin@example.com", "ADMIN"), ("seller@example.com", "SELLER"), ("user@example.com", "USER")]:
            db.execute("INSERT INTO users(email,password_hash,role) VALUES(%s,%s,%s) ON CONFLICT(email) DO NOTHING",
                       (email, hash_password("DemoPass123!"), role))
