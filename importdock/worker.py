import hashlib
import logging
import os
import tempfile
import time
from itertools import islice
from pathlib import Path

import pika

from importdock.db import connect, init
from importdock.parser import SeekableCSV, columns, rows, validate
from importdock.storage import download

logger = logging.getLogger(__name__)
CHUNK = 1000


def process(conn, job, path, after_chunk=None):
    stream = SeekableCSV(path) if job["format"] == "csv" else rows(path, job["format"])
    try:
        _process(conn, job, stream, after_chunk)
    finally:
        stream.close()


def _process(conn, job, stream, after_chunk):
    header = next(stream, [])
    indices = columns(header, job["mapping"])
    checkpoint = job["checkpoint"]
    if job["format"] == "csv" and job.get("byte_offset"):
        stream.seek(job["byte_offset"])
    else:
        # Старые контрольные точки и XLSX остаются совместимы с последовательным чтением.
        for _ in islice(stream, checkpoint):
            pass
    while batch := list(islice(stream, CHUNK)):
        with conn.transaction():
            state = conn.execute(
                "SELECT status FROM jobs WHERE id=%s FOR UPDATE", (job["id"],)
            ).fetchone()
            if state["status"] not in {"queued", "running"}:
                return
            candidates = {}
            failures = []
            for offset, row in enumerate(batch, start=checkpoint + 1):
                try:
                    identity, amount = validate(row, indices)
                    if identity in candidates:
                        raise ValueError("Идентификатор уже встречался в этом импорте")
                    candidates[identity] = (amount, offset)
                except ValueError as exc:
                    failures.append((job["id"], offset, str(exc)))
            inserted = conn.execute(
                """INSERT INTO records (job_id,external_id,amount)
                SELECT %s, x.id, x.amount FROM unnest(%s::text[],%s::numeric[]) AS x(id,amount)
                ON CONFLICT DO NOTHING RETURNING external_id""",
                (job["id"], list(candidates), [value[0] for value in candidates.values()]),
            ).fetchall()
            saved = {row["external_id"] for row in inserted}
            for identity, (_, offset) in candidates.items():
                if identity not in saved:
                    failures.append(
                        (job["id"], offset, "Идентификатор уже встречался в этом импорте")
                    )
            if failures:
                with conn.cursor() as cursor:
                    cursor.executemany("INSERT INTO errors VALUES (%s,%s,%s)", failures)
            accepted, rejected = len(saved), len(failures)
            checkpoint += len(batch)
            conn.execute(
                """UPDATE jobs SET checkpoint=%s,accepted=accepted+%s,
                rejected=rejected+%s,byte_offset=%s,status='running',error=NULL WHERE id=%s""",
                (
                    checkpoint,
                    accepted,
                    rejected,
                    stream.offset if job["format"] == "csv" else 0,
                    job["id"],
                ),
            )
        if after_chunk:
            after_chunk(checkpoint)
    with conn.transaction():
        conn.execute(
            "UPDATE jobs SET status='completed',error=NULL WHERE id=%s AND status IN ('queued','running')",
            (job["id"],),
        )


def tick():
    with connect() as conn:
        conn.autocommit = True
        jobs = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY created_at LIMIT 100"
        ).fetchall()
        for job in jobs:
            lock = job["id"].int % (2**63 - 1)
            if not conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (lock,)).fetchone()["ok"]:
                continue
            try:
                job = conn.execute("SELECT * FROM jobs WHERE id=%s", (job["id"],)).fetchone()
                if job["status"] not in {"queued", "running"}:
                    continue
                with tempfile.TemporaryDirectory() as folder:
                    path = Path(folder) / "input"
                    download(job["object_key"], path)
                    if job.get("source_sha"):
                        with path.open("rb") as source:
                            digest = hashlib.file_digest(source, "sha256").hexdigest()
                        if digest != job["source_sha"]:
                            raise ValueError(
                                "Содержимое объекта изменилось: контрольная сумма не совпала"
                            )
                    process(conn, job, path)
                return True
            except ValueError as exc:
                conn.execute(
                    "UPDATE jobs SET status='failed',error=%s WHERE id=%s AND status IN ('queued','running')",
                    (str(exc), job["id"]),
                )
            except Exception:
                logger.exception(
                    "Не удалось обработать импорт; повтор после восстановления зависимости"
                )
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (lock,))
    return False


def main():
    logging.basicConfig(level=logging.INFO)
    init()
    broker = None
    while True:
        try:
            if broker is None or broker.is_closed:
                broker = pika.BlockingConnection(pika.URLParameters(os.environ["AMQP_URL"]))
                channel = broker.channel()
                channel.queue_declare(queue="imports", durable=True)
            method, _, _ = channel.basic_get(queue="imports", auto_ack=False)
            if method:
                channel.basic_ack(method.delivery_tag)
            broker.process_data_events(time_limit=0)
        except (pika.exceptions.AMQPError, OSError):
            broker = None
        try:
            # RabbitMQ ускоряет пробуждение, а БД восстанавливает пропущенные уведомления.
            worked = tick()
        except Exception:
            logger.exception("Ошибка опроса заданий")
            worked = False
        if not worked:
            time.sleep(1)


if __name__ == "__main__":
    main()
