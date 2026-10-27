from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from aiops import db
from aiops.config import Settings

StepCallback = Callable[[str, str, str], None]
JobFn = Callable[[Any, StepCallback], Any]


def start(settings: Settings, kind: str, target: str, fn: JobFn) -> str:
    conn = db.connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT id FROM jobs WHERE kind = ? AND target = ? AND status = 'running' LIMIT 1",
            (kind, target),
        ).fetchone()
        if row is not None:
            raise RuntimeError(f"Un job {kind} pour {target} est déjà en cours")

        job_id = db.create_job(conn, kind, target)
    finally:
        conn.close()

    def runner() -> None:
        job_conn = db.connect(settings.db_path)
        steps: list[dict[str, str]] = []

        def on_step(name: str, status: str, detail: str) -> None:
            for step in steps:
                if step.get("name") == name:
                    step["status"] = status
                    step["detail"] = detail
                    break
            else:
                steps.append({"name": name, "status": status, "detail": detail})

            db.update_job(job_conn, job_id, steps=steps)

        try:
            result = fn(job_conn, on_step)
            db.update_job(job_conn, job_id, status="done", result=str(result))
        except Exception as exc:
            db.update_job(job_conn, job_id, status="error", error=str(exc))
        finally:
            job_conn.close()

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    return job_id
