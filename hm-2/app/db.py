import os
import psycopg
from psycopg.rows import dict_row


def connect():
    # Context manager commits on success and rolls back on any exception.
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
