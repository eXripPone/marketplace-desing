from pathlib import Path
from app.common import ch, pg, retry, log, s3


def migrate():
    for sql in Path('migrations/clickhouse.sql').read_text().split(';'):
        if sql.strip():
            ch(sql)
    with pg() as connection:
        connection.execute(Path('migrations/postgres.sql').read_text())
    client = s3()
    if not any(b['Name'] == 'movie-analytics' for b in client.list_buckets()['Buckets']):
        client.create_bucket(Bucket='movie-analytics')
    log.info('Migrations applied, bucket ready')


if __name__ == '__main__':
    retry(migrate, attempts=8)
