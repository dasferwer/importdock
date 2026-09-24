import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx

from importdock.db import connect

count = int(os.environ.get("ROWS", "1000000"))
base = os.environ.get("API_URL", "http://localhost:8090")
key = os.environ.get("API_KEY", "local-demo-key")
prefix = uuid.uuid4().hex[:8]


def docker(*arguments):
    subprocess.run(["docker", "compose", *arguments], check=True, capture_output=True)


def wait_job(client, identity, predicate, seconds=300):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = client.get(f"/imports/{identity}").json()
        if predicate(state):
            return state
        if state["status"] in {"failed", "cancelled"}:
            raise RuntimeError(state)
        time.sleep(0.1)
    raise TimeoutError("Импорт не достиг ожидаемого состояния")


with (
    tempfile.TemporaryDirectory() as folder,
    httpx.Client(base_url=base, headers={"X-API-Key": key}, timeout=120) as client,
):
    path = Path(folder) / "million.csv"
    with path.open("w") as stream:
        stream.write("id,amount\n")
        for i in range(count):
            stream.write(f"{prefix}-{i},1.25\n")
    docker("stop", "worker")
    try:
        with path.open("rb") as stream:
            response = client.post(
                "/imports",
                files={"file": ("million.csv", stream, "text/csv")},
                data={"mapping": '{"external_id":"id","amount":"amount"}'},
            )
        response.raise_for_status()
        identity = response.json()["id"]
        start = time.monotonic()
        docker("start", "worker")
        before = wait_job(client, identity, lambda state: state["checkpoint"] >= 1000)
        assert before["checkpoint"] < count, "Импорт завершился до аварийной остановки"
        docker("kill", "-s", "SIGKILL", "worker")
        stopped = client.get(f"/imports/{identity}").json()
        assert 0 < stopped["checkpoint"] < count
        docker("start", "worker")
        final = wait_job(client, identity, lambda state: state["status"] == "completed")
        assert final["accepted"] == final["checkpoint"] == count
        assert final["rejected"] == 0
        with connect() as conn:
            totals = conn.execute(
                "SELECT count(*) AS n, sum(amount) AS amount FROM records WHERE job_id=%s",
                (identity,),
            ).fetchone()
        assert totals["n"] == count
        assert totals["amount"] == count * 1.25
        print(
            json.dumps(
                {
                    "rows": count,
                    "checkpoint_before_kill": stopped["checkpoint"],
                    "accepted": final["accepted"],
                    "rejected": final["rejected"],
                    "seconds": round(time.monotonic() - start, 2),
                    "sum": str(totals["amount"]),
                },
                indent=2,
            )
        )
    finally:
        docker("start", "worker")
