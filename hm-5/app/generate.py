import os
import random
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4, uuid5, NAMESPACE_URL

import requests
from app.common import log, retry


def session(user, movie, start, key, finish=True):
    sequence = [('VIEW_STARTED', 0), ('VIEW_PAUSED', 120), ('VIEW_RESUMED', 120)]
    if finish:
        sequence += [('VIEW_FINISHED', 3600), ('LIKED', 3600)]
    offsets = [0, 120, 180, 3660, 3661]
    for index, (kind, progress) in enumerate(sequence):
        event = {'event_id': str(uuid5(NAMESPACE_URL, f'{key}-{index}')),
                 'user_id': user, 'movie_id': movie, 'event_type': kind,
                 'timestamp': (start + timedelta(seconds=offsets[index])).isoformat(),
                 'device_type': 'TV', 'session_id': key, 'progress_seconds': progress}
        def publish():
            response = requests.post('http://producer:8000/events', json=event, timeout=90)
            response.raise_for_status()
        retry(publish)


def main():
    # Fixed keys per date make demo seeding safe after container restart.
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for cohort_age in range(1, 10):
        first = today - timedelta(days=cohort_age)
        for user in range(6):
            for age in range(min(cohort_age, 7) + 1):
                if age and user >= max(1, 6 - age):
                    continue
                key = f'demo-{first.date()}-{user}-{age}'
                session(f'demo-{first.date()}-{user}', f'movie-{user % 3 + 1}',
                        first + timedelta(days=age), key, finish=user % 3 != 0)
    log.info('Demo history generated')
    while True:
        key = str(uuid4())
        session(f'live-{random.randrange(20)}', f'movie-{random.randrange(1,4)}',
                datetime.now(timezone.utc) - timedelta(seconds=3661), key,
                finish=random.random() < .8)
        # Search is a separate session without viewing progress.
        response = requests.post('http://producer:8000/events', json={
            'event_id': str(uuid4()), 'user_id': 'live-search', 'movie_id': 'movie-1',
            'event_type': 'SEARCHED', 'timestamp': datetime.now(timezone.utc).isoformat(),
            'device_type': 'MOBILE', 'session_id': str(uuid4()), 'progress_seconds': 0}, timeout=90)
        response.raise_for_status()
        time.sleep(float(os.getenv('GENERATOR_INTERVAL_SECONDS', '15')))


if __name__ == '__main__':
    main()
