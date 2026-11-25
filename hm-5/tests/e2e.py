"""Run with docker compose run --build --rm tests. Dates in Jan 2000 are reserved for tests."""
import json
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import requests
from confluent_kafka.admin import AdminClient

from app.common import ch, rows, pg, s3

PRODUCER = 'http://producer:8000'
AGG = 'http://aggregation:8000'
DAY = '2000-01-01'
checks = 0


def check(condition, message):
    global checks
    assert condition, message
    checks += 1
    print(f'PASS {checks}: {message}', flush=True)


def post(url, body=None):
    response = requests.post(url, json=body, timeout=120)
    response.raise_for_status()
    return response.json()


def wait_events(expected):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        data = rows("SELECT * FROM cinema.events WHERE user_id LIKE 'e2e-%' AND toYear(timestamp)=2000")
        if len(data) == expected:
            return data
        time.sleep(1)
    raise AssertionError(f'Expected {expected} events; got {len(data)}')


def metric(name, day=DAY):
    with pg() as connection:
        return connection.execute('SELECT value FROM daily_metrics WHERE day=%s AND metric=%s AND dimension=%s',
                                  (day, name, '')).fetchone()[0]


def run():
    # Only our reserved fixtures are deleted. Regular demo events are untouched.
    ch("ALTER TABLE cinema.events_raw DELETE WHERE user_id LIKE 'e2e-%' AND toYear(timestamp)=2000 SETTINGS mutations_sync=2")
    ch("ALTER TABLE cinema.metrics DELETE WHERE day BETWEEN '2000-01-01' AND '2000-01-09' SETTINGS mutations_sync=2")
    with pg() as connection:
        connection.execute("DELETE FROM daily_metrics WHERE day BETWEEN '2000-01-01' AND '2000-01-09'")
    metadata = AdminClient({'bootstrap.servers': 'kafka:9092'}).list_topics(timeout=10)
    check(len(metadata.topics['movie-events'].partitions) >= 3, 'topic has three partitions')
    registry = requests.get('http://schema-registry:8081/subjects/movie-events-value/versions/latest', timeout=10).json()
    check(registry['version'] >= 1 and registry['id'] > 0, 'Avro schema version registered')
    schema = json.loads(registry['schema'])
    bad_schema = dict(schema, fields=[dict(f, type='int') if f['name']=='user_id' else f for f in schema['fields']])
    compatibility = post('http://schema-registry:8081/compatibility/subjects/movie-events-value/versions/latest',
                         {'schema': json.dumps(bad_schema)})
    check(not compatibility['is_compatible'], 'registry rejects incompatible schema change')
    events = []
    def emit(user, age, kind, progress, movie='test-movie'):
        event = {'event_id':str(uuid4()), 'user_id':f'e2e-{user}', 'movie_id':movie,
                 'event_type':kind, 'timestamp':(datetime(2000,1,1,tzinfo=timezone.utc)+timedelta(days=age,seconds=len(events))).isoformat(),
                 'device_type':'DESKTOP','session_id':f'e2e-{user}-{age}', 'progress_seconds':progress}
        result = post(PRODUCER+'/events',event)
        check(result['event_id']==event['event_id'], 'API returns published event_id')
        events.append(event)
        return event
    first = emit('a',0,'VIEW_STARTED',0)
    emit('a',0,'VIEW_FINISHED',120)
    emit('b',0,'VIEW_STARTED',0)
    emit('b',0,'VIEW_FINISHED',240)
    emit('c',0,'VIEW_STARTED',0)
    emit('a',1,'VIEW_STARTED',0)
    emit('a',1,'VIEW_FINISHED',60)
    emit('a',7,'VIEW_STARTED',0)
    emit('b',7,'VIEW_STARTED',0)
    for field, value in [('progress_seconds',-1),('event_type','UNKNOWN'),('event_id','bad'),
                         ('device_type','WATCH'),('user_id',''),('timestamp','2000-01-01T00:00:00')]:
        response = requests.post(PRODUCER+'/events', json=dict(first,**{field:value}), timeout=15)
        check(response.status_code==422, f'invalid {field} returns 422')
    data = wait_events(len(events))
    stored = next(r for r in data if r['event_id']==first['event_id'])
    check(all(stored[k]==first[k] for k in first if k!='timestamp'), 'all event fields preserved in ClickHouse')
    check(stored['timestamp']=='2000-01-01 00:00:00.000', 'UTC timestamp preserved')
    post(PRODUCER+'/events', first)
    time.sleep(3)
    check(len(wait_events(len(events)))==len(events), 'duplicate immutable event is deduplicated')
    post(AGG+'/recalculate/'+DAY)
    check(metric('dau')==3, 'DAU = 3')
    check(metric('avg_watch_seconds')==180, 'average finished watch = 180 seconds')
    check(abs(metric('conversion')-2/3)<1e-9, 'conversion = 2/3')
    check(abs(metric('retention_d1')-1/3)<1e-9, 'D1 = 1/3')
    check(abs(metric('retention_d7')-2/3)<1e-9, 'D7 = 2/3')
    check(metric('retention_d0')==1, 'cohort Day 0 = 100%')
    check(metric('retention_d2')==0, 'no returns on Day 2 = 0%')
    with pg() as connection:
        views=connection.execute("SELECT value FROM daily_metrics WHERE day=%s AND metric='movie_views' AND dimension='test-movie'",(DAY,)).fetchone()[0]
        count=connection.execute('SELECT count(*) FROM daily_metrics WHERE day=%s',(DAY,)).fetchone()[0]
    check(views==2, 'top movies counts completed views')
    post(AGG+'/recalculate/'+DAY)
    with pg() as connection:
        check(connection.execute('SELECT count(*) FROM daily_metrics WHERE day=%s',(DAY,)).fetchone()[0]==count, 'PostgreSQL UPSERT creates no duplicates')
    stored_metrics=rows("SELECT * FROM cinema.metrics_current WHERE day='2000-01-01'")
    check(len(stored_metrics)==count, 'ClickHouse materializes one current row per metric')
    post(AGG+'/recalculate/2000-01-09')
    check(metric('dau','2000-01-09')==0 and metric('conversion','2000-01-09')==0, 'empty day and zero denominator handled')
    key = post(AGG+'/export/'+DAY)['key']
    exported=json.loads(s3().get_object(Bucket='movie-analytics',Key=key)['Body'].read())
    check(len(exported)==count, 'S3 JSON contains every daily metric')
    check(all(set(r)=={'day','metric','dimension','value','computed_at'} for r in exported), 'export includes date and computation time')
    post(AGG+'/export/'+DAY)
    objects=s3().list_objects_v2(Bucket='movie-analytics',Prefix=f'daily/{DAY}/')['Contents']
    check(len(objects)==1 and key==f'daily/{DAY}/aggregates.json', 're-export overwrites same key')
    grafana=requests.get('http://grafana:3000/api/dashboards/uid/cinema',auth=('admin','admin'),timeout=15)
    grafana.raise_for_status()
    panels=grafana.json()['dashboard']['panels']
    check(len(panels)>=3 and 'Heatmap' in panels[0]['title'], 'Grafana dashboard automatically provisioned')
    health=requests.get('http://grafana:3000/api/datasources/uid/clickhouse/health',auth=('admin','admin'),timeout=30)
    check(health.status_code==200, 'Grafana ClickHouse datasource connected')
    for panel in panels:
        check(bool(rows(panel['targets'][0]['rawSql'])), 'dashboard SQL returns data: '+panel['title'])
    print(f'ALL {checks} CHECKS PASSED')


if __name__=='__main__':
    run()
