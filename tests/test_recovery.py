import uuid

import pytest
from psycopg.types.json import Jsonb

from importdock.db import connect
from importdock.worker import process


def job(format="csv"):
    with connect() as conn:
        return conn.execute(
            "INSERT INTO jobs (id,object_key,format,mapping) VALUES (%s,%s,%s,%s) RETURNING *",
            (uuid.uuid4(), "test", format, Jsonb({"external_id": "id", "amount": "amount"})),
        ).fetchone()


def test_restart_keeps_committed_chunks(client, tmp_path):
    path = tmp_path / "input.csv"
    path.write_text("id,amount\n" + "".join(f"{i},1.00\n" for i in range(2500)) + "0,2\nbad,no\n")
    task = job()

    def stop(checkpoint):
        raise InterruptedError("Остановка после зафиксированной порции")

    with connect() as conn:
        conn.autocommit = True
        with pytest.raises(InterruptedError):
            process(conn, task, path, stop)
    with connect() as conn:
        task = conn.execute("SELECT * FROM jobs WHERE id=%s", (task["id"],)).fetchone()
        assert task["checkpoint"] == task["accepted"] == 1000
        assert client.get(f"/imports/{task['id']}/records").status_code == 409
    with connect() as conn:
        conn.autocommit = True
        process(conn, task, path)
    result = client.get(f"/imports/{task['id']}").json()
    assert result["status"] == "completed"
    assert result["checkpoint"] == 2502
    assert result["accepted"] == 2500
    assert result["rejected"] == 2
    assert len(client.get(f"/imports/{task['id']}/errors").json()) == 2
    with connect() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM records WHERE job_id=%s", (task["id"],)
            ).fetchone()["n"]
            == 2500
        )


def test_cancel_and_rollback_cannot_be_resurrected(client, tmp_path):
    path = tmp_path / "input.csv"
    path.write_text("id,amount\n1,1\n")
    task = job()
    assert client.post(f"/imports/{task['id']}/rollback").status_code == 409
    client.post(f"/imports/{task['id']}/cancel")
    client.post(f"/imports/{task['id']}/rollback")
    with connect() as conn:
        conn.autocommit = True
        process(conn, task, path)
    assert client.get(f"/imports/{task['id']}").json()["status"] == "rolled_back"
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM records").fetchone()["n"] == 0


def test_preview_and_validation(client):
    response = client.post(
        "/imports",
        files={"file": ("input.csv", b"id,amount\na,1\nb,no\n")},
        data={"mapping": '{"external_id":"id","amount":"amount"}', "preview": "true"},
    )
    assert response.status_code == 200
    assert response.json()["checked"] == 2
    assert "error" in response.json()["preview"][1]
    assert (
        client.post(
            "/imports", files={"file": ("input.txt", b"x")}, data={"mapping": "{}"}
        ).status_code
        == 422
    )
    assert client.get("/health", headers={"X-API-Key": "bad"}).status_code == 401


@pytest.mark.parametrize("preview", ["true", "false"])
def test_malformed_csv_is_rejected_before_job_creation(client, preview):
    response = client.post(
        "/imports",
        files={"file": ("input.csv", b'id,amount\n"unclosed,1\n')},
        data={"mapping": '{"external_id":"id","amount":"amount"}', "preview": preview},
    )
    assert response.status_code == 422
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0


def test_csv_resume_uses_byte_boundary_with_multiline_unicode(client, tmp_path, monkeypatch):
    import csv

    from importdock.parser import SeekableCSV

    path = tmp_path / "multi.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["id", "amount"])
        for i in range(2005):
            writer.writerow([f"Запись {i}\nвторая строка", "1.25"])
    task = job()

    def stop(_):
        raise InterruptedError()

    with connect() as conn:
        conn.autocommit = True
        with pytest.raises(InterruptedError):
            process(conn, task, path, stop)
        task = conn.execute("SELECT * FROM jobs WHERE id=%s", (task["id"],)).fetchone()
    assert task["byte_offset"] > 0
    count = 0
    original = SeekableCSV.__next__

    def counted(self):
        nonlocal count
        count += 1
        return original(self)

    monkeypatch.setattr(SeekableCSV, "__next__", counted)
    with connect() as conn:
        conn.autocommit = True
        process(conn, task, path)
    # Заголовок + остаток + проверки EOF; первые 1000 записей повторно не разбираются.
    assert count <= 1010
    result = client.get(f"/imports/{task['id']}").json()
    assert result["accepted"] == 2005
    assert result["rejected"] == 0


def test_cancel_resume_preserves_checkpoint_and_rollback_forbids_resume(client, tmp_path):
    path = tmp_path / "input.csv"
    path.write_text("id,amount\n" + "".join(f"{i},1\n" for i in range(1500)))
    task = job()

    def stop(_):
        client.post(f"/imports/{task['id']}/cancel")

    with connect() as conn:
        conn.autocommit = True
        process(conn, task, path, stop)
    resumed = client.post(f"/imports/{task['id']}/resume").json()
    assert resumed["checkpoint"] == 1000
    with connect() as conn:
        conn.autocommit = True
        current = conn.execute("SELECT * FROM jobs WHERE id=%s", (task["id"],)).fetchone()
        process(conn, current, path)
    assert client.get(f"/imports/{task['id']}").json()["accepted"] == 1500
    assert client.post(f"/imports/{task['id']}/rollback").status_code == 200
    assert client.post(f"/imports/{task['id']}/resume").status_code == 409


def test_upload_intent_exists_before_storage_and_retry_recovers(client, monkeypatch):
    import pika

    from importdock import api

    def fail(path, key):
        with connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=%s", (key,)).fetchone()
            assert row["status"] == "uploading"
            assert row["source_sha"]
        raise OSError("Хранилище недоступно")

    monkeypatch.setattr(api, "upload", fail)
    payload = {
        "files": {"file": ("test.csv", b"id,amount\na,1\n")},
        "data": {"mapping": '{"external_id":"id","amount":"amount"}'},
    }
    assert client.post("/imports", **payload).status_code == 503
    with connect() as conn:
        assert conn.execute("SELECT status FROM jobs").fetchone()["status"] == "upload_failed"
    monkeypatch.setattr(api, "upload", lambda path, key: None)
    monkeypatch.setenv("AMQP_URL", "amqp://demo:demo@localhost:1/%2F")

    def unavailable(*args, **kwargs):
        raise pika.exceptions.AMQPConnectionError()

    monkeypatch.setattr(api.pika, "BlockingConnection", unavailable)
    result = client.post("/imports", **payload)
    assert result.status_code == 200
    assert result.json()["status"] == "queued"
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 1


def xlsx_bytes(count):
    from io import BytesIO

    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["id", "amount"])
    for i in range(count):
        book.active.append([f"item-{i:04d}", "1.25"])
    target = BytesIO()
    book.save(target)
    book.close()
    return target.getvalue()


def test_xlsx_preview_and_resume_extensionless_input(client, tmp_path):
    payload = xlsx_bytes(2005)
    response = client.post(
        "/imports",
        files={"file": ("real.xlsx", payload)},
        data={"mapping": '{"external_id":"id","amount":"amount"}', "preview": "true"},
    )
    assert response.status_code == 200
    assert response.json()["checked"] == 20
    assert all(row["amount"] == "1.25" for row in response.json()["preview"])
    path = tmp_path / "input"
    path.write_bytes(payload)
    task = job("xlsx")

    def stop(_):
        raise InterruptedError("Остановка после committed chunk")

    with connect() as conn:
        conn.autocommit = True
        with pytest.raises(InterruptedError):
            process(conn, task, path, stop)
        task = conn.execute("SELECT * FROM jobs WHERE id=%s", (task["id"],)).fetchone()
        assert task["checkpoint"] == task["accepted"] == 1000
        assert task["byte_offset"] == 0
        process(conn, task, path)
    result = client.get(f"/imports/{task['id']}").json()
    assert (result["status"], result["checkpoint"], result["accepted"], result["rejected"]) == (
        "completed",
        2005,
        2005,
        0,
    )
    with connect() as conn:
        saved = conn.execute(
            "SELECT count(*) AS n, min(amount) AS lo, max(amount) AS hi FROM records WHERE job_id=%s",
            (task["id"],),
        ).fetchone()
    assert saved["n"] == 2005 and saved["lo"] == saved["hi"] == 1.25


def test_extreme_amount_after_preview_is_terminal_row_error_and_queue_continues(
    client, monkeypatch
):
    import pika

    from importdock import api, worker

    objects = {}
    monkeypatch.setattr(api, "upload", lambda path, key: objects.update({key: path.read_bytes()}))
    monkeypatch.setattr(worker, "download", lambda key, path: path.write_bytes(objects[key]))
    monkeypatch.setenv("AMQP_URL", "amqp://demo:demo@localhost:1/%2F")

    def unavailable(*args, **kwargs):
        raise pika.exceptions.AMQPConnectionError()

    monkeypatch.setattr(api.pika, "BlockingConnection", unavailable)
    payload = b"id,amount\n" + b"".join(f"item-{i},1.25\n".encode() for i in range(20))
    payload += b"poison,1e1000000\nzero,0e1000000\nnext,2.50\n"
    task = client.post(
        "/imports",
        files={"file": ("poison.csv", payload)},
        data={"mapping": '{"external_id":"id","amount":"amount"}'},
    )
    assert task.status_code == 200
    identity = task.json()["id"]
    assert worker.tick()
    result = client.get(f"/imports/{identity}").json()
    assert (result["status"], result["checkpoint"], result["accepted"], result["rejected"]) == (
        "completed",
        23,
        22,
        1,
    )
    assert result["byte_offset"] == len(payload)
    assert client.get(f"/imports/{identity}/errors").json()[0]["row_number"] == 21
    assert not worker.tick()
    next_task = client.post(
        "/imports",
        files={"file": ("next.csv", b"id,amount\nhealthy,3.50\n")},
        data={"mapping": '{"external_id":"id","amount":"amount"}'},
    ).json()
    assert worker.tick()
    assert client.get(f"/imports/{next_task['id']}").json()["status"] == "completed"


def test_database_error_still_retries_job(client, tmp_path, monkeypatch):
    import psycopg

    from importdock import worker

    task = job()
    monkeypatch.setattr(worker, "download", lambda key, path: path.write_bytes(b"id,amount\na,1\n"))
    original = worker.process

    def outage(*args, **kwargs):
        raise psycopg.OperationalError("Временный сбой БД")

    monkeypatch.setattr(worker, "process", outage)
    assert not worker.tick()
    assert client.get(f"/imports/{task['id']}").json()["status"] == "queued"
    monkeypatch.setattr(worker, "process", original)
    assert worker.tick()
    assert client.get(f"/imports/{task['id']}").json()["status"] == "completed"


def test_corrupt_xlsx_is_rejected_before_job_creation(client):
    response = client.post(
        "/imports",
        files={"file": ("broken.xlsx", b"not a zip")},
        data={"mapping": '{"external_id":"id","amount":"amount"}'},
    )
    assert response.status_code == 422
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0


def test_xlsx_formula_is_preview_row_error(client):
    from io import BytesIO

    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["id", "amount"])
    book.active.append(["formula", "=1+1"])
    source = BytesIO()
    book.save(source)
    book.close()
    response = client.post(
        "/imports",
        files={"file": ("formula.xlsx", source.getvalue())},
        data={"mapping": '{"external_id":"id","amount":"amount"}', "preview": "true"},
    )
    assert response.status_code == 200
    assert "error" in response.json()["preview"][0]


def test_xlsx_expanded_size_guard_rejects_archive_before_job(client):
    import struct

    payload = bytearray(xlsx_bytes(1))
    central_header = payload.index(b"PK\x01\x02")
    # Настоящий ZIP с метаданными большого элемента, без выделения 513 МиБ в тесте.
    struct.pack_into("<I", payload, central_header + 24, 513 * 1024 * 1024)
    response = client.post(
        "/imports",
        files={"file": ("oversized.xlsx", bytes(payload))},
        data={"mapping": '{"external_id":"id","amount":"amount"}'},
    )
    assert response.status_code == 422
    assert "512 МиБ" in response.json()["detail"]
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0
