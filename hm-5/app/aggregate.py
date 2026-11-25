import asyncio
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException

from app.common import ch, rows, pg, s3, retry, log

lock = threading.Lock()


def calculate(day: date):
    with lock:
        started = time.monotonic()
        log.info('Aggregation started day=%s', day)
        params = {'param_day': str(day)}
        daily = rows('''SELECT count() AS processed, uniqExact(user_id) AS dau,
            countIf(event_type='VIEW_STARTED') AS starts,
            countIf(event_type='VIEW_FINISHED') AS finishes,
            if(countIf(event_type='VIEW_FINISHED')=0, 0,
               avgIf(progress_seconds,event_type='VIEW_FINISHED')) AS avg_watch
            FROM cinema.events WHERE toDate(timestamp)={day:Date}''', params)[0]
        values = [('dau', '', float(daily['dau'])),
                  ('avg_watch_seconds', '', float(daily['avg_watch'])),
                  ('conversion', '', int(daily['finishes']) / int(daily['starts']) if int(daily['starts']) else 0)]
        # Include movies with zero views to overwrite their previous result on recalculation.
        movies = rows('''SELECT movie_id,
            countIf(toDate(timestamp)={day:Date} AND event_type='VIEW_FINISHED') AS views
            FROM cinema.events GROUP BY movie_id ORDER BY views DESC, movie_id''', params)
        values.extend(('movie_views', r['movie_id'], float(r['views'])) for r in movies)
        cohort = rows('''WITH first_watch AS (
                SELECT user_id, min(toDate(timestamp)) AS cohort
                FROM cinema.events WHERE event_type='VIEW_STARTED' GROUP BY user_id
            ), members AS (SELECT user_id FROM first_watch WHERE cohort={day:Date})
            SELECT dateDiff('day', {day:Date}, toDate(timestamp)) AS age,
                   uniqExact(user_id) AS returned
            FROM cinema.events WHERE user_id IN (SELECT user_id FROM members)
                AND toDate(timestamp) BETWEEN {day:Date} AND addDays({day:Date},7)
            GROUP BY age''', params)
        returned = {int(r['age']): int(r['returned']) for r in cohort}
        size = returned.get(0, 0)
        today = datetime.now(timezone.utc).date()
        for age in range(8):
            value = returned.get(age, 0) / size if size else 0
            if day + timedelta(days=age) > today:
                value = None
            values.append((f'retention_d{age}', '', value))
        values.append(('cohort_size', '', float(size)))
        computed = datetime.now(timezone.utc)
        documents = [{'day': str(day), 'metric': metric, 'dimension': dimension, 'value': value,
                      'computed_at': computed.strftime('%Y-%m-%d %H:%M:%S.%f')}
                     for metric, dimension, value in values]
        retry(lambda: ch('INSERT INTO cinema.metrics FORMAT JSONEachRow\n' +
                         '\n'.join(json.dumps(r) for r in documents)))

        def save_pg():
            with pg() as connection:
                with connection.cursor() as cursor:
                    cursor.executemany('''INSERT INTO daily_metrics(day,metric,dimension,value,computed_at)
                        VALUES (%s,%s,%s,%s,%s) ON CONFLICT(day,metric,dimension)
                        DO UPDATE SET value=excluded.value,computed_at=excluded.computed_at''',
                        [(day, metric, dimension, value, computed) for metric, dimension, value in values])
        retry(save_pg)
        log.info('Aggregation completed day=%s processed=%s metrics=%s duration_seconds=%.3f',
                 day, daily['processed'], len(values), time.monotonic() - started)
        return {'day': str(day), 'processed': int(daily['processed']), 'metrics': len(values)}


def export_day(day: date):
    def upload():
        with pg() as connection:
            result = connection.execute('''SELECT day,metric,dimension,value,computed_at
                FROM daily_metrics WHERE day=%s ORDER BY metric,dimension''', (day,)).fetchall()
        if not result:
            raise ValueError('No aggregates for this day; run /recalculate first')
        data = [dict(zip(('day','metric','dimension','value','computed_at'), row)) for row in result]
        key = f'daily/{day}/aggregates.json'
        s3().put_object(Bucket='movie-analytics', Key=key,
                        Body=json.dumps(data, default=str, ensure_ascii=False).encode(),
                        ContentType='application/json')
        log.info('Export completed key=%s rows=%s', key, len(data))
        return {'key': key, 'rows': len(data)}
    return retry(upload)


async def scheduler():
    last_export = None
    # Give the startup generator and Kafka ingestion time before the first snapshot.
    await asyncio.sleep(float(os.getenv('AGG_INTERVAL_SECONDS', '60')))
    while True:
        try:
            # Recalculate old cohorts too: late events may change their retention.
            days = await asyncio.to_thread(rows, 'SELECT DISTINCT toDate(timestamp) AS day FROM cinema.events ORDER BY day')
            yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
            dates = {date.fromisoformat(r['day']) for r in days} | {yesterday}
            for day in sorted(dates):
                await asyncio.to_thread(calculate, day)
            if last_export != yesterday:
                await asyncio.to_thread(export_day, yesterday)
                last_export = yesterday
        except Exception:
            log.exception('Scheduled cycle failed; will retry next interval')
        await asyncio.sleep(float(os.getenv('AGG_INTERVAL_SECONDS', '60')))


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(scheduler())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title='Aggregation Service', lifespan=lifespan)


@app.get('/health')
def health():
    ch('SELECT 1')
    with pg() as connection:
        connection.execute('SELECT 1')
    return {'status': 'ok'}


@app.post('/recalculate/{day}')
def recalculate(day: date):
    return calculate(day)


@app.post('/export/{day}')
def export(day: date):
    try:
        return export_day(day)
    except ValueError as error:
        raise HTTPException(404, str(error)) from error
