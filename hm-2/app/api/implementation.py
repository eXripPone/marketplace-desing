"""Implementation of generated API; DTOs and routes come from OpenAPI."""
from datetime import date, datetime

import simplejson
from fastapi import Response
from starlette.concurrency import run_in_threadpool
from openapi_server.apis.default_api_base import BaseDefaultApi
from openapi_server.models.product_response import ProductResponse
from openapi_server.models.product_page import ProductPage
from openapi_server.models.user_response import UserResponse
from openapi_server.models.token_response import TokenResponse
from openapi_server.models.order_response import OrderResponse
from openapi_server.models.promo_code_response import PromoCodeResponse
from app import services


def encode(value):
    if isinstance(value, (datetime,date)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def response(model, data, status=200):
    validated = model.model_validate(data)
    # Decimal is written as a JSON number without conversion to float.
    return Response(simplejson.dumps(validated.model_dump(mode="python",by_alias=True),
                                    use_decimal=True,default=encode), status_code=status,media_type="application/json")


class MarketplaceApi(BaseDefaultApi):
    async def get_product_by_id(self,id):
        return response(ProductResponse,await run_in_threadpool(services.get_product,id))

    async def get_products(self,page=0,size=20,status=None,category=None):
        return response(ProductPage,await run_in_threadpool(services.list_products,page,size,status,category))

    async def create_product(self,product_create):
        return response(ProductResponse,await run_in_threadpool(services.create_product,product_create),201)

    async def update_product(self,id,product_update):
        return response(ProductResponse,await run_in_threadpool(services.update_product,id,product_update))

    async def archive_product(self,id):
        await run_in_threadpool(services.archive_product,id)
        return Response(status_code=204)

    async def register_user(self,register_request):
        return response(UserResponse,await run_in_threadpool(services.register,register_request),201)

    async def login_user(self,login_request):
        return response(TokenResponse,await run_in_threadpool(services.login,login_request))

    async def refresh_token(self,refresh_request):
        return response(TokenResponse,await run_in_threadpool(services.refresh,refresh_request))

    async def create_order(self,order_create):
        return response(OrderResponse,await run_in_threadpool(services.create_order,order_create),201)

    async def get_order(self,id):
        return response(OrderResponse,await run_in_threadpool(services.get_order,id))

    async def update_order(self,id,order_update):
        return response(OrderResponse,await run_in_threadpool(services.update_order,id,order_update))

    async def cancel_order(self,id):
        return response(OrderResponse,await run_in_threadpool(services.cancel_order,id))

    async def change_order_status(self,id,order_state_update):
        return response(OrderResponse,await run_in_threadpool(services.change_status,id,order_state_update))

    async def create_promo_code(self,promo_code_create):
        return response(PromoCodeResponse,await run_in_threadpool(services.create_promo,promo_code_create),201)
