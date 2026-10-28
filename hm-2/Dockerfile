FROM openapitools/openapi-generator-cli:v7.26.0 AS generator
WORKDIR /local
COPY openapi/ ./openapi/
RUN docker-entrypoint.sh validate -i /local/openapi/marketplace.json
RUN docker-entrypoint.sh generate -i /local/openapi/marketplace.json -g python-fastapi -o /local/generated --additional-properties=fastapiImplementationPackage=app.api,useExternalImplementationPackage=true
RUN docker-entrypoint.sh generate -i /local/openapi/marketplace.json -g python -o /local/generated/src --global-property models --additional-properties=packageName=openapi_server,mapNumberTo=Decimal

FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=generator /local/generated/src/openapi_server ./generated/src/openapi_server
COPY tools/fix_decimal.py ./tools/fix_decimal.py
RUN python tools/fix_decimal.py
COPY app/ ./app/
COPY openapi/ ./openapi/
ENV PYTHONPATH=/app:/app/generated/src PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
