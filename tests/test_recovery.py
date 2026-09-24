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
