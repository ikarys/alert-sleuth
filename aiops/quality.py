from __future__ import annotations

import sqlite3

from aiops import db


def scorecard(conn: sqlite3.Connection, catalog: dict[str, dict]) -> list[dict]:
    if catalog is None:
        catalog = {}

    alerts = db.list_alerts(conn, statuses=None)
    total_alerts = len(alerts)

    tagged_alerts = sum(
        1
        for alert in alerts
        if alert.get("service") and alert.get("env") and alert.get("team")
    )

    catalog_alerts = sum(
        1
        for alert in alerts
        if alert.get("service") and alert.get("service") in catalog
    )

    p1_p2_alerts = [
        alert
        for alert in alerts
        if str(alert.get("priority") or "").upper() in ("P1", "P2")
    ]
    p1_p2_runbook_alerts = sum(
        1
        for alert in p1_p2_alerts
        if _has_runbook(catalog, alert.get("service"))
    )

    reports = db.list_reports(conn)
    total_reports = len(reports)
    reports_without_missing = sum(1 for report in reports if not report.get("missing"))

    reports_with_feedback = [
        report
        for report in reports
        if report.get("feedback_verdict") in ("useful", "wrong")
    ]
    useful_reports = sum(
        1
        for report in reports_with_feedback
        if report.get("feedback_verdict") == "useful"
    )

    indicators = [
        {
            "indicator": "Alertes avec tags service+env+team",
            "value": _percent(tagged_alerts, total_alerts),
            "target": 95.0,
        },
        {
            "indicator": "Alertes avec service présent au catalogue",
            "value": _percent(catalog_alerts, total_alerts),
            "target": 100.0,
        },
        {
            "indicator": "Alertes P1/P2 avec runbook au catalogue",
            "value": _percent(p1_p2_runbook_alerts, len(p1_p2_alerts)),
            "target": 100.0,
        },
        {
            "indicator": "Analyses sans preuves manquantes",
            "value": _percent(reports_without_missing, total_reports),
            "target": 90.0,
        },
        {
            "indicator": "Rapports jugés utiles parmi ceux avec feedback",
            "value": _percent(useful_reports, len(reports_with_feedback)),
            "target": 80.0,
        },
    ]

    for indicator in indicators:
        indicator["status"] = _status(indicator.get("value"), indicator.get("target", 0.0))

    return indicators


def _has_runbook(catalog: dict[str, dict], service: str | None) -> bool:
    if not service:
        return False

    entry = catalog.get(service)
    if not isinstance(entry, dict):
        return False

    ops = entry.get("ops")
    if not isinstance(ops, dict):
        return False

    return bool(ops.get("runbook"))


def _percent(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator * 100.0 / denominator, 2)


def _status(value: float | None, target: float) -> str:
    if value is None:
        return "n/a"
    if value >= target:
        return "ok"
    if value >= target - 15:
        return "warn"
    return "ko"
