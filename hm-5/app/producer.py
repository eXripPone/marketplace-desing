import io
import json
import os
import struct
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import UUID

import fastavro
import requests
from confluent_kafka import Producer
from confluent_kafka.admin import AdminClient, NewTopic
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.common import log, retry

TOPIC = 'movie-events'
REGISTRY = os.getenv('REGISTRY_URL', 'http://schema-registry:8081')
BROKER = os.getenv('KAFKA_BROKERS', 'kafka:9092')
schema = json.loads(Path('schemas/movie-event-v1.avsc').read_text())
parsed_schema = fastavro.parse_schema(schema)
producer = None
schema_id = None


class EventType(str, Enum):
    VIEW_STARTED = 'VIEW_STARTED'
    VIEW_FINISHED = 'VIEW_FINISHED'
    VIEW_PAUSED = 'VIEW_PAUSED'
    VIEW_RESUMED = 'VIEW_RESUMED'
    LIKED = 'LIKED'
    SEARCHED = 'SEARCHED'


class DeviceType(str, Enum):
    MOBILE = 'MOBILE'
    DESKTOP = 'DESKTOP'
    TV = 'TV'
    TABLET = 'TABLET'


class Event(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event_id: UUID
    user_id: str = Field(min_length=1, max_length=100)
    movie_id: str = Field(min_length=1, max_length=100)
    event_type: EventType
    timestamp: datetime
    device_type: DeviceType
    session_id: str = Field(min_length=1, max_length=100)
    progress_seconds: int = Field(strict=True, ge=0, le=2147483647)

    @field_validator('timestamp')
    @classmethod
    def utc(cls, value):
        if value.tzinfo is None:
            raise ValueError('timestamp must include timezone')
        return value.astimezone(timezone.utc)


def bootstrap():
    global producer, schema_id
    admin = AdminClient({'bootstrap.servers': BROKER})
    if TOPIC not in admin.list_topics(timeout=10).topics:
        admin.create_topics([NewTopic(TOPIC, 3, 1)])[TOPIC].result(timeout=20)
    response = requests.put(f'{REGISTRY}/config/{TOPIC}-value',
                            json={'compatibility': 'BACKWARD'}, timeout=10)
    response.raise_for_status()
    response = requests.post(f'{REGISTRY}/subjects/{TOPIC}-value/versions',
                             json={'schemaType': 'AVRO', 'schema': json.dumps(schema)}, timeout=10)
    response.raise_for_status()
    schema_id = response.json()['id']
    version = requests.get(f'{REGISTRY}/subjects/{TOPIC}-value/versions/latest', timeout=10)
    version.raise_for_status()
    log.info('Registered movie-event-v1: version=%s schema_id=%s', version.json()['version'], schema_id)
    producer = Producer({'bootstrap.servers': BROKER, 'acks': 'all',
                         'enable.idempotence': True, 'delivery.timeout.ms': 15000,
                         'request.timeout.ms': 5000, 'retry.backoff.ms': 500})


@asynccontextmanager
async def lifespan(app):
    retry(bootstrap)
    yield
    producer.flush(10)


app = FastAPI(title='Movie Service', lifespan=lifespan)


@app.get('/health')
def health():
    return {'status': 'ok', 'schema_id': schema_id}


@app.post('/events', status_code=201)
def publish(event: Event):
    record = event.model_dump(mode='json')
    record['timestamp'] = int(event.timestamp.timestamp() * 1000)
    fastavro.validation.validate(record, parsed_schema)
    buffer = io.BytesIO()
    # Confluent wire format: magic byte + schema ID + Avro record.
    buffer.write(struct.pack('>bI', 0, schema_id))
    fastavro.schemaless_writer(buffer, parsed_schema, record)

    def send():
        result = []
        producer.produce(TOPIC, key=event.user_id, value=buffer.getvalue(),
                         on_delivery=lambda error, message: result.append(error))
        producer.flush(16)
        if not result or result[0] is not None:
            raise RuntimeError(str(result[0]) if result else 'Delivery timeout')
    try:
        retry(send, attempts=3)
    except Exception as error:
        raise HTTPException(503, 'Kafka publication failed; retry with the same event_id') from error
    log.info('published event_id=%s event_type=%s timestamp=%s',
             event.event_id, event.event_type.value, event.timestamp.isoformat())
    return {'event_id': str(event.event_id)}
