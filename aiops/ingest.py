from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
import urllib.request

from aiops import db
from aiops.config import Settings


def parse_tags(tags: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if not tags:
        return result
    for tag in tags:
        if not isinstance(tag, str):
            continue
        key, separator, value = tag.partition(":")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if key and key not in result:
            result[key] = value
    return result


def fetch(settings: Settings, conn: sqlite3.Connection) -> int:
    if settings.demo:
        alerts = _fetch_demo(settings)
    else:
        alerts = _fetch_opsgenie(settings)

    count = 0
    for alert in alerts:
        if not alert.get("id"):
            continue
        db.upsert_alert(conn, _map_alert(alert))
        count += 1
    return count


def _map_alert(alert: dict) -> dict:
    raw_tags = alert.get("tags")
    if not isinstance(raw_tags, list):
        raw_tags = []
    parsed = parse_tags(raw_tags)

    if alert.get("status") == "closed":
        status = "closed"
    elif alert.get("acknowledged"):
        status = "acked"
    else:
        status = "open"

    try:
        count = int(alert.get("count", 0) or 0)
    except (TypeError, ValueError):
        count = 0

    return {
        "id": alert.get("id"),
        "alias": alert.get("alias"),
        "message": alert.get("message"),
        "priority": alert.get("priority"),
        "status": status,
        "service": parsed.get("service"),
        "env": parsed.get("env"),
        "team": parsed.get("team"),
        "tags": raw_tags,
        "count": count,
        "created_at": alert.get("createdAt"),
        "updated_at": alert.get("updatedAt"),
        "raw": alert,
    }


def _fetch_demo(settings: Settings) -> list[dict]:
    path = os.path.join(settings.fixtures_dir, "opsgenie_alerts.json")
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    if isinstance(data, dict):
        items = data.get("data", [])
    elif isinstance(data, list):
        items = data
    else:
        items = []

    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _fetch_opsgenie(settings: Settings) -> list[dict]:
    if not settings.opsgenie_api_key:
        raise RuntimeError("OPSGENIE_API_KEY is required to fetch alerts from Opsgenie")

    base_url = settings.opsgenie_url.rstrip("/")
    alerts: list[dict] = []
    offset = 0

    while len(alerts) < 1000:
        params = urllib.parse.urlencode(
            {
                "query": settings.opsgenie_query,
                "limit": 100,
                "offset": offset,
                "sort": "createdAt",
                "order": "desc",
            }
        )
        url = f"{base_url}/v2/alerts?{params}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"GenieKey {settings.opsgenie_api_key}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))

        page = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(page, list):
            page = []

        alerts.extend(page)
        if len(page) < 100:
            break

        offset += 100
        if len(alerts) >= 1000:
            alerts = alerts[:1000]
            break

    return alerts
