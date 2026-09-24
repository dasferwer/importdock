import os

import psycopg
from psycopg.rows import dict_row


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init():
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(300029)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id uuid PRIMARY KEY, object_key text NOT NULL, format text NOT NULL,
                mapping jsonb NOT NULL, status text NOT NULL DEFAULT 'queued',
                checkpoint bigint NOT NULL DEFAULT 0, accepted bigint NOT NULL DEFAULT 0,
                rejected bigint NOT NULL DEFAULT 0, error text,
                created_at timestamptz NOT NULL DEFAULT now());
            CREATE TABLE IF NOT EXISTS records (
                job_id uuid NOT NULL REFERENCES jobs(id), external_id text NOT NULL,
                amount numeric(18,2) NOT NULL, PRIMARY KEY(job_id,external_id));
            CREATE TABLE IF NOT EXISTS errors (
                job_id uuid NOT NULL REFERENCES jobs(id), row_number bigint NOT NULL,
                message text NOT NULL, PRIMARY KEY(job_id,row_number));
            ALTER TABLE jobs ADD COLUMN IF NOT EXISTS byte_offset bigint NOT NULL DEFAULT 0;
            ALTER TABLE jobs ADD COLUMN IF NOT EXISTS source_sha text;
        """)
