import logging
import os
import time

import boto3
import psycopg
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('cinema')


def retry(action, attempts=5):
    for attempt in range(attempts):
        try:
            return action()
        except Exception:
            log.exception('Attempt %s/%s failed', attempt + 1, attempts)
            if attempt == attempts - 1:
                raise
            time.sleep(min(2 ** attempt, 8))


def ch(sql, params=None):
    response = requests.post(
        os.getenv('CLICKHOUSE_URL', 'http://clickhouse:8123'),
        params=params, data=sql.encode(), auth=('cinema', 'cinema'), timeout=60,
    )
    response.raise_for_status()
    return response.text


def rows(sql, params=None):
    import json
    return [json.loads(line) for line in ch(sql + ' FORMAT JSONEachRow', params).splitlines()]


def pg():
    return psycopg.connect(os.getenv('DATABASE_URL', 'postgresql://cinema:cinema@postgres:5432/cinema'))


def s3():
    return boto3.client('s3', endpoint_url=os.getenv('S3_URL', 'http://s3:8333'),
                       aws_access_key_id='cinema', aws_secret_access_key='cinema-secret',
                       region_name='us-east-1')
