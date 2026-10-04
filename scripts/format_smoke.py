"""Проверяет CSV/XLSX через настоящий API и фоновый воркер работающего стенда."""

import json
import os
import time
import uuid
from decimal import Decimal
from io import BytesIO

import httpx
from openpyxl import Workbook

from importdock.db import connect


def wait_job(client, identity, seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        response = client.get(f"/imports/{identity}")
        response.raise_for_status()
        state = response.json()
        if state["status"] == "completed":
            return state
        if state["status"] in {"failed", "cancelled", "rolled_back", "upload_failed"}:
            raise RuntimeError(state)
        time.sleep(0.1)
    raise TimeoutError("Импорт не завершился за отведённое время")


def main():
    prefix = uuid.uuid4().hex
    workbook = Workbook()
    workbook.active.append(["id", "amount"])
    for index in range(2005):
        workbook.active.append([f"{prefix}-xlsx-{index}", "1.25"])
    source = BytesIO()
    workbook.save(source)
    workbook.close()
    mapping = {"mapping": '{"external_id":"id","amount":"amount"}'}
    receipts = []
    with httpx.Client(
        base_url=os.environ.get("API_URL", "http://localhost:8090"),
        headers={"X-API-Key": os.environ.get("API_KEY", "local-demo-key")},
        timeout=120,
        trust_env=False,
    ) as client:
        preview = client.post(
            "/imports",
            files={"file": ("actual.xlsx", source.getvalue())},
            data={**mapping, "preview": "true"},
        )
        preview.raise_for_status()
        assert preview.json()["checked"] == 20
        assert all(row["amount"] == "1.25" for row in preview.json()["preview"])
        poison = "id,amount\n" + "".join(f"{prefix}-good-{i},1.25\n" for i in range(20))
        for index, value in enumerate(["1e1000000", "-1e1000000", "1e-1000000", "sNaN"]):
            poison += f"{prefix}-bad-{index},{value}\n"
        poison += (
            f"{prefix}-zero-up,0e1000000\n{prefix}-zero-down,0e-1000000\n{prefix}-after,2.50\n"
        )
        for filename, payload, accepted, rejected, total in [
            ("actual.xlsx", source.getvalue(), 2005, 0, Decimal("2506.25")),
            ("poison.csv", poison.encode(), 23, 4, Decimal("27.50")),
            ("next.csv", f"id,amount\n{prefix}-healthy,3.50\n".encode(), 1, 0, Decimal("3.50")),
        ]:
            response = client.post("/imports", files={"file": (filename, payload)}, data=mapping)
            response.raise_for_status()
            identity = response.json()["id"]
            final = wait_job(client, identity)
            assert final["checkpoint"] == accepted + rejected
            assert final["accepted"] == accepted and final["rejected"] == rejected
            with connect() as conn:
                totals = conn.execute(
                    "SELECT count(*) AS n,sum(amount) AS total FROM records WHERE job_id=%s",
                    (identity,),
                ).fetchone()
            assert totals["n"] == accepted and totals["total"] == total
            errors = client.get(f"/imports/{identity}/errors")
            errors.raise_for_status()
            assert [row["row_number"] for row in errors.json()] == (
                [21, 22, 23, 24] if rejected else []
            )
            receipts.append(
                {
                    "format": filename,
                    "id": identity,
                    "status": final["status"],
                    "checkpoint": final["checkpoint"],
                    "accepted": accepted,
                    "rejected": rejected,
                    "sum": str(totals["total"]),
                }
            )
    print(json.dumps({"xlsx_preview": 20, "imports": receipts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
