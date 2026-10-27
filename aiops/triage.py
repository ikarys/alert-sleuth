from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from aiops import db, llm
from aiops import catalog as catalog_module
from aiops.config import Settings
from aiops.db import parse_ts

logger = logging.getLogger(__name__)

_PRIORITY_SCORES = {
    "P1": 40,
    "P2": 30,
    "P3": 15,
    "P4": 5,
    "P5": 0,
}

_CRITICALITY_SCORES = {
    "critical": 25,
    "high": 15,
    "medium": 5,
    "low": 0,
}

_CRITICALITY_LABELS = {
    "critical": "service critique",
    "high": "service de criticité haute",
    "medium": "service de criticité moyenne",
    "low": "service de criticité faible",
}


def decide(score: int) -> str:
    if score >= 70:
        return "investigate"
    if score >= 40:
        return "watch"
    return "ignore"


def build_groups(alerts: list[dict], catalog: dict[str, dict]) -> list[dict]:
    eligible = [alert for alert in alerts if alert.get("status") in ("open", "acked")]
    by_key: dict[tuple[str, str], list[dict]] = {}

    for alert in eligible:
        service = alert.get("service") or "unknown"
        env = alert.get("env") or "unknown"
        by_key.setdefault((service, env), []).append(alert)

    groups: list[dict] = []
    for (service, env), key_alerts in by_key.items():
        key_alerts.sort(key=lambda alert: _ts(alert.get("created_at")))
        current: list[dict] = []

        for alert in key_alerts:
            if not current:
                current.append(alert)
                continue

            gap = _ts(alert.get("created_at")) - _ts(current[-1].get("created_at"))
            if gap > timedelta(minutes=15):
                groups.append(_make_group(service, env, current, catalog, alerts))
                current = [alert]
            else:
                current.append(alert)

        if current:
            groups.append(_make_group(service, env, current, catalog, alerts))

    return groups


def common_causes(groups: list[dict]) -> list[list[dict]]:
    eligible = [group for group in groups if group.get("decision") != "ignore"]
    eligible.sort(key=lambda group: _ts(group.get("first_seen")))

    clusters: list[list[dict]] = []
    current: list[dict] = []
    services: set[str] = set()

    for group in eligible:
        service = group.get("service") or "unknown"
        if not current:
            current.append(group)
            services.add(service)
            continue

        first_ts = _ts(current[0].get("first_seen"))
        group_ts = _ts(group.get("first_seen"))
        if group_ts - first_ts <= timedelta(minutes=5) and service not in services:
            current.append(group)
            services.add(service)
        else:
            if len(current) >= 2:
                clusters.append(current)
            current = [group]
            services = {service}

    if len(current) >= 2:
        clusters.append(current)

    return clusters


def refine_with_llm(settings: Settings, groups: list[dict], catalog: dict[str, dict]) -> list[dict]:
    if not settings.llm_url or not groups:
        return groups

    compact_groups = []
    for group in groups:
        entry = catalog_module.for_service(catalog, group.get("service"))
        criticality = entry.get("criticality") if entry and entry.get("criticality") else "unknown"
        compact_groups.append(
            {
                "group_id": group.get("id"),
                "service": group.get("service"),
                "env": group.get("env"),
                "priority": group.get("priority"),
                "titles": [group.get("title")] if group.get("title") else [],
                "count": group.get("count"),
                "first_seen": group.get("first_seen"),
                "last_seen": group.get("last_seen"),
                "flapping": group.get("flapping"),
                "score": group.get("score"),
                "factors": group.get("factors", {}),
                "criticality": criticality,
                "recent_changes": [],
            }
        )

    try:
        data = llm.chat_json(settings, "alert-triage", {"groups": compact_groups})
    except Exception as exc:
        logger.warning("Triage LLM refinement failed: %s", exc)
        return groups

    items = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return groups

    by_id = {group.get("id"): group for group in groups}
    result = [dict(group) for group in groups]

    for item in items:
        if not isinstance(item, dict):
            continue
        group_id = item.get("group_id")
        if group_id not in by_id:
            continue

        original = by_id[group_id]
        try:
            adjustment = int(item.get("adjustment", 0))
        except (TypeError, ValueError):
            adjustment = 0
        adjustment = _clamp(adjustment, -20, 20)

        try:
            score = int(original.get("score", 0) or 0)
        except (TypeError, ValueError):
            score = 0
        final_score = _clamp(score + adjustment, 0, 100)

        reason = item.get("reason")
        if reason is None:
            reason = original.get("reason")
        reason = str(reason)[:120]

        for group in result:
            if group.get("id") == group_id:
                group["adjustment"] = adjustment
                group["final_score"] = final_score
                group["decision"] = decide(final_score)
                group["reason"] = reason
                break

    return result


def run(settings: Settings, conn: sqlite3.Connection, use_llm: bool = True) -> list[dict]:
    alerts = db.list_alerts(conn, statuses=("open", "acked", "closed"))
    services = catalog_module.load(settings.catalog_path)
    groups = build_groups(alerts, services)
    if use_llm:
        groups = refine_with_llm(settings, groups, services)
    db.replace_groups(conn, groups)
    return groups


def _make_group(
    service: str,
    env: str,
    alerts: list[dict],
    catalog: dict[str, dict],
    all_alerts: list[dict],
) -> dict:
    first_id = str(alerts[0].get("id") or "")
    key = f"{service}|{env}"
    group_id = "G-" + hashlib.sha1((key + first_id).encode("utf-8")).hexdigest()[:8]

    best_alert = None
    best_rank = 99
    for alert in alerts:
        rank = _priority_rank(alert.get("priority"))
        if rank < best_rank:
            best_rank = rank
            best_alert = alert

    priority = best_alert.get("priority") if best_alert else None
    title = best_alert.get("message") if best_alert else None
    count = sum(_alert_count(alert) for alert in alerts)
    first_seen = alerts[0].get("created_at")
    last_seen = alerts[-1].get("created_at")
    # ponytail: alias recurrence over 24 h, use Opsgenie alert logs (close/reopen events) if it misfires
    flapping = _is_flapping(alerts, all_alerts, last_seen)

    entry = catalog.get(service) if service != "unknown" else None
    criticality = entry.get("criticality") if entry else None

    score = 0
    factors: dict[str, int] = {}

    priority_score = _PRIORITY_SCORES.get(str(priority).upper() if priority else "", 0)
    if priority_score:
        factors["priority"] = priority_score
        score += priority_score

    criticality_score = _CRITICALITY_SCORES.get(criticality, 0) if criticality else 0
    if criticality_score:
        factors["criticality"] = criticality_score
        score += criticality_score

    volume = min(15, count)
    if volume:
        factors["volume"] = volume
        score += volume

    # ponytail: count fallback for burst volume when few distinct alerts are present.
    distinct = len(alerts) >= 3 or count >= 3
    if distinct:
        factors["distinct_alerts"] = 10
        score += 10

    if flapping:
        factors["flapping"] = -15
        score -= 15

    score = _clamp(score, 0, 100)

    return {
        "id": group_id,
        "alert_ids": [alert.get("id") for alert in alerts],
        "service": service,
        "env": env,
        "priority": priority,
        "title": title,
        "count": count,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "flapping": flapping,
        "score": score,
        "factors": factors,
        "adjustment": 0,
        "final_score": score,
        "decision": decide(score),
        "reason": _reason(priority, criticality, count, flapping),
    }


def _is_flapping(group_alerts: list[dict], all_alerts: list[dict], last_seen: str | None) -> int:
    if not last_seen:
        return 0

    last_dt = _ts(last_seen)
    if last_dt == datetime.min.replace(tzinfo=timezone.utc):
        return 0

    cutoff = last_dt - timedelta(hours=24)

    group_aliases: set[str] = set()
    for alert in group_alerts:
        alias = alert.get("alias")
        if alias:
            group_aliases.add(str(alias))

    if not group_aliases:
        return 0

    counts: dict[str, set[object]] = {}
    for alert in all_alerts:
        alias = alert.get("alias")
        if alias is None:
            continue
        alias = str(alias)
        if alias not in group_aliases:
            continue

        created = _ts(alert.get("created_at"))
        if cutoff <= created <= last_dt:
            record_id = alert.get("id")
            if record_id is None:
                record_id = id(alert)
            counts.setdefault(alias, set()).add(record_id)

    for alias in group_aliases:
        if len(counts.get(alias, set())) >= 3:
            return 1

    return 0


def _reason(priority: str | None, criticality: str | None, count: int, flapping: int) -> str:
    parts: list[str] = []
    if priority:
        parts.append(f"{priority} sur {_criticality_label(criticality)}")
    if count == 1:
        parts.append("1 occurrence")
    elif count > 1:
        parts.append(f"{count} occurrences")
    if flapping:
        parts.append("flapping")
    if not parts:
        return "Aucun facteur significatif"
    return ", ".join(parts)


def _criticality_label(criticality: str | None) -> str:
    if criticality is None:
        return "service hors catalogue"
    return _CRITICALITY_LABELS.get(criticality, "service hors catalogue")


def _ts(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return parse_ts(value)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _priority_rank(priority: str | None) -> int:
    if not priority:
        return 99
    value = str(priority).strip().upper()
    if value.startswith("P") and value[1:].isdigit():
        return int(value[1:])
    return 99


def _alert_count(alert: dict) -> int:
    try:
        return int(alert.get("count", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))
