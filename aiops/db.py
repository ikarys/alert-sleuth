from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

_write_lock = threading.Lock()


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: object, default: object) -> object:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _decode(row: sqlite3.Row, json_fields: tuple[str, ...]) -> dict:
    data = dict(row)
    for field in json_fields:
        if field in data:
            data[field] = _loads(data[field], [] if field in ("tags", "alert_ids", "evidence", "missing", "steps") else {})
    return data


def connect(path: str) -> sqlite3.Connection:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init(conn)
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY,
            alias TEXT,
            message TEXT,
            priority TEXT,
            status TEXT,
            service TEXT,
            env TEXT,
            team TEXT,
            tags TEXT,
            count INTEGER,
            created_at TEXT,
            updated_at TEXT,
            raw TEXT
        );

        CREATE TABLE IF NOT EXISTS groups (
            id TEXT PRIMARY KEY,
            alert_ids TEXT,
            service TEXT,
            env TEXT,
            priority TEXT,
            title TEXT,
            count INTEGER,
            first_seen TEXT,
            last_seen TEXT,
            flapping INTEGER,
            score INTEGER,
            factors TEXT,
            adjustment INTEGER,
            final_score INTEGER,
            decision TEXT,
            reason TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT,
            alert_id TEXT,
            service TEXT,
            created_at TEXT,
            report TEXT,
            markdown TEXT,
            evidence TEXT,
            missing TEXT,
            model TEXT
        );

        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id INTEGER,
            verdict TEXT,
            root_cause TEXT,
            comment TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            kind TEXT,
            target TEXT,
            status TEXT,
            steps TEXT,
            result TEXT,
            error TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        """
    )
    conn.commit()


def upsert_alert(conn: sqlite3.Connection, a: dict) -> None:
    with _write_lock:
        conn.execute(
            """
            INSERT OR REPLACE INTO alerts (
                id, alias, message, priority, status, service, env, team,
                tags, count, created_at, updated_at, raw
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                a.get("id"),
                a.get("alias"),
                a.get("message"),
                a.get("priority"),
                a.get("status"),
                a.get("service"),
                a.get("env"),
                a.get("team"),
                _dumps(a.get("tags", [])),
                int(a.get("count", 0) or 0),
                a.get("created_at"),
                a.get("updated_at"),
                _dumps(a.get("raw", {})),
            ),
        )
        conn.commit()


def list_alerts(conn: sqlite3.Connection, statuses: tuple[str, ...] = ("open", "acked")) -> list[dict]:
    if statuses is None:
        rows = conn.execute("SELECT * FROM alerts ORDER BY created_at ASC").fetchall()
    else:
        placeholders = ",".join("?" for _ in statuses)
        rows = conn.execute(
            f"SELECT * FROM alerts WHERE status IN ({placeholders}) ORDER BY created_at ASC",
            tuple(statuses),
        ).fetchall()
    return [_decode(row, ("tags", "raw")) for row in rows]


def get_alert(conn: sqlite3.Connection, alert_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    if row is None:
        return None
    return _decode(row, ("tags", "raw"))


def replace_groups(conn: sqlite3.Connection, groups: list[dict]) -> None:
    with _write_lock:
        conn.execute("DELETE FROM groups")
        for group in groups:
            conn.execute(
                """
                INSERT INTO groups (
                    id, alert_ids, service, env, priority, title, count,
                    first_seen, last_seen, flapping, score, factors,
                    adjustment, final_score, decision, reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group.get("id"),
                    _dumps(group.get("alert_ids", [])),
                    group.get("service"),
                    group.get("env"),
                    group.get("priority"),
                    group.get("title"),
                    int(group.get("count", 0) or 0),
                    group.get("first_seen"),
                    group.get("last_seen"),
                    int(group.get("flapping", 0) or 0),
                    int(group.get("score", 0) or 0),
                    _dumps(group.get("factors", {})),
                    int(group.get("adjustment", 0) or 0),
                    int(group.get("final_score", 0) or 0),
                    group.get("decision"),
                    group.get("reason"),
                    group.get("updated_at") or utcnow(),
                ),
            )
        conn.commit()


def list_groups(conn: sqlite3.Connection, service: str | None = None, decision: str | None = None) -> list[dict]:
    query = "SELECT * FROM groups"
    clauses: list[str] = []
    params: list[object] = []
    if service is not None:
        clauses.append("service = ?")
        params.append(service)
    if decision is not None:
        clauses.append("decision = ?")
        params.append(decision)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY final_score DESC, id ASC"
    rows = conn.execute(query, tuple(params)).fetchall()
    return [_decode(row, ("alert_ids", "factors")) for row in rows]


def get_group(conn: sqlite3.Connection, group_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    if row is None:
        return None
    return _decode(row, ("alert_ids", "factors"))


def save_report(
    conn: sqlite3.Connection,
    group_id: str,
    alert_id: str,
    service: str,
    report: dict,
    markdown: str,
    evidence: list[dict],
    missing: list[str],
    model: str,
) -> int:
    with _write_lock:
        cur = conn.execute(
            """
            INSERT INTO reports (
                group_id, alert_id, service, created_at, report,
                markdown, evidence, missing, model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                alert_id,
                service,
                utcnow(),
                _dumps(report),
                markdown,
                _dumps(evidence),
                _dumps(missing),
                model,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_report(conn: sqlite3.Connection, report_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if row is None:
        return None
    data = _decode(row, ("report", "evidence", "missing"))
    feedback_rows = conn.execute(
        "SELECT * FROM feedback WHERE report_id = ? ORDER BY id ASC",
        (report_id,),
    ).fetchall()
    data["feedback"] = [dict(row) for row in feedback_rows]
    return data


def list_reports(conn: sqlite3.Connection, q: str | None = None) -> list[dict]:
    query = "SELECT * FROM reports"
    params: list[object] = []
    if q is not None and q.strip():
        like = f"%{q.strip()}%"
        query += " WHERE service LIKE ? OR alert_id LIKE ?"
        params = [like, like]
    query += " ORDER BY id DESC"
    rows = conn.execute(query, tuple(params)).fetchall()
    reports: list[dict] = []
    for row in rows:
        data = _decode(row, ("report", "evidence", "missing"))
        feedback = conn.execute(
            "SELECT verdict FROM feedback WHERE report_id = ? ORDER BY id DESC LIMIT 1",
            (data["id"],),
        ).fetchone()
        data["feedback_verdict"] = feedback["verdict"] if feedback else None
        reports.append(data)
    return reports


def add_feedback(
    conn: sqlite3.Connection,
    report_id: int,
    verdict: str,
    root_cause: str,
    comment: str,
) -> None:
    with _write_lock:
        conn.execute(
            """
            INSERT INTO feedback (report_id, verdict, root_cause, comment, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (report_id, verdict, root_cause, comment, utcnow()),
        )
        conn.commit()


def create_job(conn: sqlite3.Connection, kind: str, target: str) -> str:
    job_id = uuid.uuid4().hex
    with _write_lock:
        conn.execute(
            """
            INSERT INTO jobs (
                id, kind, target, status, steps, result, error,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, kind, target, "running", "[]", None, None, utcnow(), utcnow()),
        )
        conn.commit()
    return job_id


def update_job(conn: sqlite3.Connection, job_id: str, **fields: object) -> None:
    allowed = {"status", "steps", "result", "error", "updated_at"}
    assignments: list[str] = []
    params: list[object] = []

    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in ("steps", "result") and isinstance(value, (list, dict)):
            value = _dumps(value)
        elif key == "steps" and value is None:
            value = "[]"
        assignments.append(f"{key} = ?")
        params.append(value)

    if "updated_at" not in fields:
        assignments.append("updated_at = ?")
        params.append(utcnow())

    if not assignments:
        return

    params.append(job_id)
    with _write_lock:
        conn.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", params)
        conn.commit()


def get_job(conn: sqlite3.Connection, job_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    return _decode(row, ("steps",))
