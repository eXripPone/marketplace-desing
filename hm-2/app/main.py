import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.concurrency import run_in_threadpool

from app.db import connect
from app.errors import ApiError, fail
from app.security import current_user, decode_token, seed_accounts
from openapi_server.apis.default_api import router
from openapi_server.models.extra_models import TokenModel
from openapi_server.security_api import get_token_bearerAuth

logger = logging.getLogger("marketplace.api")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(handler)
logger.propagate = False


@asynccontextmanager
async def lifespan(app):
    await run_in_threadpool(seed_accounts)
    yield


app = FastAPI(title="Marketplace API", lifespan=lifespan)
app.include_router(router)
# Swagger shows the hand-written contract, which is the source of truth.
app.openapi = lambda: json.loads((Path(__file__).resolve().parents[1]/"openapi/marketplace.json").read_text(encoding="utf-8"))


async def authenticated_token():
    return TokenModel(sub=str(current_user.get()["id"]))


# Authentication is checked once in middleware, including uniform error responses.
app.dependency_overrides[get_token_bearerAuth] = authenticated_token


def error_response(error):
    body = {"error_code":error.code,"message":error.message}
    if error.details is not None:
        body["details"] = error.details
    return JSONResponse(body,status_code=error.status)


@app.exception_handler(ApiError)
async def business_error(request,error):
    return error_response(error)


@app.exception_handler(RequestValidationError)
async def validation_error(request,error):
    details = {"fields":[{"field":".".join(map(str,e["loc"])),"message":e["msg"]} for e in error.errors()]}
    return error_response(ApiError("VALIDATION_ERROR","Ошибка входных данных",400,details))


@app.exception_handler(HTTPException)
async def http_error(request,error):
    return error_response(ApiError("VALIDATION_ERROR",str(error.detail),error.status_code))


def mask(value):
    if isinstance(value,dict):
        return {k: "***" if any(secret in k.lower() for secret in ("password","token","secret")) else mask(v) for k,v in value.items()}
    if isinstance(value,list):
        return [mask(v) for v in value]
    return value


def check_json_numbers(value):
    # JSON money fields must be numbers, not strings or booleans.
    if isinstance(value,dict):
        for key,child in value.items():
            if key in ("price","discount_value","min_order_amount") and (isinstance(child,bool) or not isinstance(child,(int,float))):
                fail("VALIDATION_ERROR","Денежное поле должно быть JSON-числом",400,{"fields":[{"field":key,"message":"Ожидается number"}]})
            check_json_numbers(child)
    elif isinstance(value,list):
        for child in value: check_json_numbers(child)


@app.middleware("http")
async def request_middleware(request:Request,call_next):
    started=time.perf_counter()
    request_id=str(uuid4())
    timestamp=datetime.now(timezone.utc).isoformat()
    context=current_user.set(None)
    logged_body=None
    try:
        if request.method in ("POST","PUT","DELETE"):
            raw=await request.body()
            if raw:
                try:
                    body=json.loads(raw)
                    logged_body=mask(body)
                    check_json_numbers(body)
                except (ValueError,UnicodeDecodeError):
                    logged_body="<invalid JSON>"
                    fail("VALIDATION_ERROR","Некорректный JSON",400)
        protected=request.url.path.startswith(("/products","/orders","/promo-codes"))
        if protected:
            header=request.headers.get("Authorization","")
            if not header.startswith("Bearer "):
                fail("TOKEN_INVALID","Нужен access token в Authorization: Bearer",401)
            user=await run_in_threadpool(decode_token,header[7:])
            current_user.set(user)
        if request.url.path=="/products" and request.method=="GET":
            status=request.query_params.get("status")
            if status is not None and status not in ("ACTIVE","INACTIVE","ARCHIVED"):
                fail("VALIDATION_ERROR","Неверный статус товара",400,{"fields":[{"field":"query.status","message":"ACTIVE, INACTIVE или ARCHIVED"}]})
        result=await call_next(request)
    except ApiError as error:
        result=error_response(error)
    except Exception:
        logging.getLogger("uvicorn.error").exception("Unexpected request failure; request_id=%s",request_id)
        result=error_response(ApiError("INTERNAL_ERROR","Внутренняя ошибка сервера",500))
    result.headers["X-Request-Id"]=request_id
    user=current_user.get()
    entry={"request_id":request_id,"method":request.method,"endpoint":request.url.path,
           "status_code":result.status_code,"duration_ms":round((time.perf_counter()-started)*1000,2),
           "user_id":user["id"] if user else None,"timestamp":timestamp}
    if request.method in ("POST","PUT","DELETE"):
        entry["request_body"]=logged_body
    logger.info(json.dumps(entry,ensure_ascii=False))
    current_user.reset(context)
    return result


@app.get("/health")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status":"ok"}
