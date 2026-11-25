CREATE DATABASE IF NOT EXISTS cinema;

CREATE TABLE IF NOT EXISTS cinema.events_raw (
    event_id UUID, user_id String, movie_id String,
    event_type Enum8('VIEW_STARTED'=1,'VIEW_FINISHED'=2,'VIEW_PAUSED'=3,'VIEW_RESUMED'=4,'LIKED'=5,'SEARCHED'=6),
    timestamp DateTime64(3, 'UTC'),
    device_type Enum8('MOBILE'=1,'DESKTOP'=2,'TV'=3,'TABLET'=4),
    session_id String, progress_seconds Int32
) ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(timestamp)
ORDER BY (toDate(timestamp), user_id, event_id);

CREATE TABLE IF NOT EXISTS cinema.events_kafka (
    event_id UUID, user_id String, movie_id String,
    event_type Enum8('VIEW_STARTED'=1,'VIEW_FINISHED'=2,'VIEW_PAUSED'=3,'VIEW_RESUMED'=4,'LIKED'=5,'SEARCHED'=6),
    timestamp DateTime64(3, 'UTC'),
    device_type Enum8('MOBILE'=1,'DESKTOP'=2,'TV'=3,'TABLET'=4),
    session_id String, progress_seconds Int32
) ENGINE = Kafka
SETTINGS kafka_broker_list='kafka:9092', kafka_topic_list='movie-events',
    kafka_group_name='clickhouse-cinema-v1', kafka_format='AvroConfluent',
    kafka_num_consumers=3, kafka_thread_per_consumer=1,
    kafka_flush_interval_ms=1000,
    format_avro_schema_registry_url='http://schema-registry:8081';

CREATE MATERIALIZED VIEW IF NOT EXISTS cinema.events_ingest
TO cinema.events_raw AS SELECT * FROM cinema.events_kafka;

CREATE VIEW IF NOT EXISTS cinema.events AS SELECT * FROM cinema.events_raw FINAL;

CREATE TABLE IF NOT EXISTS cinema.metrics (
    day Date, metric String, dimension String, value Nullable(Float64),
    computed_at DateTime64(6, 'UTC')
) ENGINE = ReplacingMergeTree(computed_at)
PARTITION BY toYYYYMM(day)
ORDER BY (day, metric, dimension);

CREATE VIEW IF NOT EXISTS cinema.metrics_current AS SELECT * FROM cinema.metrics FINAL;
