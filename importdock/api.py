import hashlib
import json
import logging
import os
import secrets
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from itertools import islice
from pathlib import Path
from typing import Annotated

import pika
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from psycopg.types.json import Jsonb

from importdock.db import connect, init
from importdock.parser import columns, rows, validate
from importdock.storage import upload

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app):
    init()
    yield


def authorize(x_api_key: str = Header(default="")):
    key = os.environ.get("API_KEY", "")
    if not key or not secrets.compare_digest(x_api_key, key):
        raise HTTPException(401, "Неверный API-ключ")


app = FastAPI(title="ImportDock", lifespan=lifespan, dependencies=[Depends(authorize)])


@app.get("/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/imports")
def create(
    file: Annotated[UploadFile, File()],
    mapping: Annotated[str, Form()],
    preview: bool = Form(False),
):
    format = Path(file.filename or "").suffix.lower().lstrip(".")
    if format not in {"csv", "xlsx"}:
        raise HTTPException(422, "Поддерживаются CSV и XLSX")
    try:
        fields = json.loads(mapping)
        if not isinstance(fields, dict) or not all(isinstance(v, str) for v in fields.values()):
            raise ValueError("Сопоставление должно быть объектом со строковыми значениями")
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "Некорректное сопоставление колонок") from exc
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "input"
        digest = hashlib.sha256()
        size = 0
        with path.open("wb") as out:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > 512 * 1024 * 1024:
                    raise HTTPException(413, "Лимит файла: 512 МиБ")
                out.write(chunk)
                digest.update(chunk)
        try:
            if format == "xlsx":
                with zipfile.ZipFile(path) as archive:
                    if sum(item.file_size for item in archive.infolist()) > 512 * 1024 * 1024:
                        raise ValueError("Распакованный XLSX превышает 512 МиБ")
            stream = rows(path, format)
            try:
                indices = columns(next(stream, []), fields)
                sample = []
                for index, row in enumerate(islice(stream, 20), start=1):
                    try:
                        identity, amount = validate(row, indices)
                        sample.append(
                            {"row": index, "external_id": identity, "amount": str(amount)}
                        )
                    except ValueError as exc:
                        sample.append({"row": index, "error": str(exc)})
            finally:
                stream.close()
        except Exception as exc:
            raise HTTPException(422, "Файл не удалось прочитать: " + str(exc)[:200]) from exc
        if preview:
            return {"preview": sample, "checked": len(sample)}
        fingerprint = digest.hexdigest() + format + json.dumps(fields, sort_keys=True)
        identity = uuid.uuid5(uuid.NAMESPACE_URL, fingerprint)
        with connect() as conn:
            existing = conn.execute("SELECT * FROM jobs WHERE id=%s", (identity,)).fetchone()
        if existing:
            return existing
        try:
            upload(path, str(identity))
        except Exception as exc:
            raise HTTPException(503, "Хранилище временно недоступно") from exc
        with connect() as conn:
            conn.execute(
                """INSERT INTO jobs (id,object_key,format,mapping) VALUES (%s,%s,%s,%s)
                         ON CONFLICT DO NOTHING""",
                (identity, str(identity), format, Jsonb(fields)),
            )
        try:
            broker = pika.BlockingConnection(pika.URLParameters(os.environ["AMQP_URL"]))
            try:
                channel = broker.channel()
                channel.queue_declare(queue="imports", durable=True)
                channel.basic_publish(
                    exchange="",
                    routing_key="imports",
                    body=str(identity),
                    properties=pika.BasicProperties(delivery_mode=2),
                )
            finally:
                broker.close()
        except (pika.exceptions.AMQPError, OSError):
            logger.warning("Уведомление не отправлено; воркер найдёт задание в БД")
        return get_job(identity)


@app.get("/imports/{identity}")
def get_job(identity: uuid.UUID):
    with connect() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=%s", (identity,)).fetchone()
    if job is None:
        raise HTTPException(404, "Импорт не найден")
    return job


@app.get("/imports/{identity}/errors")
def errors(
    identity: uuid.UUID, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)
):
    get_job(identity)
    with connect() as conn:
        return conn.execute(
            "SELECT row_number,message FROM errors WHERE job_id=%s AND row_number>%s ORDER BY row_number LIMIT %s",
            (identity, after, limit),
        ).fetchall()


@app.get("/imports/{identity}/records")
def records(identity: uuid.UUID, after: str = "", limit: int = Query(100, ge=1, le=1000)):
    if get_job(identity)["status"] != "completed":
        raise HTTPException(409, "Данные доступны только после завершения импорта")
    with connect() as conn:
        return conn.execute(
            "SELECT external_id,amount FROM records WHERE job_id=%s AND external_id>%s ORDER BY external_id LIMIT %s",
            (identity, after, limit),
        ).fetchall()


@app.post("/imports/{identity}/cancel")
def cancel(identity: uuid.UUID):
    get_job(identity)
    with connect() as conn:
        conn.execute(
            "UPDATE jobs SET status='cancelled' WHERE id=%s AND status IN ('queued','running')",
            (identity,),
        )
    return get_job(identity)


@app.post("/imports/{identity}/rollback")
def rollback(identity: uuid.UUID):
    with connect() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=%s FOR UPDATE", (identity,)).fetchone()
        if not job:
            raise HTTPException(404, "Импорт не найден")
        if job["status"] in {"queued", "running"}:
            raise HTTPException(409, "Сначала отмените импорт")
        conn.execute("DELETE FROM records WHERE job_id=%s", (identity,))
        conn.execute("UPDATE jobs SET status='rolled_back' WHERE id=%s", (identity,))
    return get_job(identity)
