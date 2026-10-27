from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

from aiops import catalog, collectors, db, llm
from aiops.config import Settings
from aiops.db import parse_ts

StepCallback = Callable[[str, str, str], None]


def analyze(
    settings: Settings,
    conn: sqlite3.Connection,
    group_id: str,
    on_step: StepCallback | None = None,
) -> int:
    group = db.get_group(conn, group_id)
    if group is None:
        raise ValueError(f"Group {group_id} not found")

    alert = _highest_priority_alert(conn, group)
    service = group.get("service") or "unknown"

    entries = catalog.load(settings.catalog_path)
    entry = catalog.for_service(entries, service)
    missing: list[str] = []

    if entry is None:
        entry = {}
        missing.append("catalog (service absent)")
        _step(on_step, "Corrélation catalogue", "error", f"service {service} absent du catalogue")
    else:
        _step(on_step, "Corrélation catalogue", "ok", f"service {service} trouvé dans le catalogue")

    window = _window(group.get("first_seen"), settings)
    evidence, collector_missing = collectors.run_all(settings, alert, entry, window, on_step=on_step)
    missing.extend(collector_missing)

    alert_item = {
        "id": "alt-1",
        "source": "opsgenie",
        "ts": group.get("first_seen") or window["t"],
        "kind": "alert",
        "text": (
            f"{group.get('priority') or alert.get('priority')} "
            f"{group.get('title') or alert.get('message')} "
            f"({group.get('count', 0)} occurrences)"
        ),
        "ref": f"opsgenie alert {alert.get('id')}",
    }
    evidence = [alert_item, *evidence]

    alert_payload = _alert_payload(alert, group)
    evidence = _condense_evidence(settings, alert_payload, evidence, on_step)

    service_payload = _service_payload(entry, service)
    group_payload = _group_payload(group)
    payload = {
        "alert": alert_payload,
        "group": group_payload,
        "service": service_payload,
        "evidence": evidence,
        "missing_evidence": missing,
    }

    try:
        report = llm.chat_json(settings, "incident-analysis", payload)
    except Exception as exc:
        _step(on_step, "Analyse LLM", "error", str(exc))
        raise

    if not isinstance(report, dict):
        _step(on_step, "Analyse LLM", "error", "rapport LLM invalide")
        raise llm.LLMError("incident-analysis returned a non-object report")

    evidence_ids = {item.get("id") for item in evidence if item.get("id")}
    unknown = cited_ids(report) - evidence_ids

    if unknown:
        payload["error"] = f"Unknown evidence ids: {sorted(unknown)}. Cite only ids from evidence."
        try:
            report = llm.chat_json(settings, "incident-analysis", payload)
        except Exception as exc:
            _step(on_step, "Analyse LLM", "error", str(exc))
            raise

        if not isinstance(report, dict):
            _step(on_step, "Analyse LLM", "error", "rapport LLM invalide après retry")
            raise llm.LLMError("incident-analysis retry returned a non-object report")

        unknown = cited_ids(report) - evidence_ids
        if unknown:
            report["unverified_ids"] = sorted(unknown)

    _cap_confidence(report, missing)
    report["service"] = service_payload
    report["missing_evidence"] = missing

    _step(on_step, "Analyse LLM", "ok", "rapport généré")

    markdown = render_markdown(report, evidence)
    report_id = db.save_report(
        conn,
        group.get("id") or group_id,
        alert.get("id") or "",
        service,
        report,
        markdown,
        evidence,
        missing,
        settings.llm_model,
    )
    return report_id


def cited_ids(report: dict) -> set[str]:
    ids: set[str] = set()
    if not isinstance(report, dict):
        return ids

    timeline = report.get("timeline")
    if isinstance(timeline, list):
        for event in timeline:
            if isinstance(event, dict):
                _add_evidence_ids(event.get("evidence"), ids)

    probable_cause = report.get("probable_cause")
    if isinstance(probable_cause, dict):
        _add_evidence_ids(probable_cause.get("evidence"), ids)

    actions = report.get("immediate_actions")
    if isinstance(actions, list):
        for action in actions:
            if isinstance(action, dict):
                _add_evidence_ids(action.get("evidence"), ids)

    return ids


def render_markdown(report: dict, evidence: list[dict]) -> str:
    if not isinstance(report, dict):
        report = {}

    service = report.get("service") if isinstance(report.get("service"), dict) else {}
    service_name = service.get("name") or report.get("service_name") or "service inconnu"

    lines: list[str] = [f"# Rapport — {service_name}", ""]

    lines.append("## Résumé")
    summary = report.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.append(summary.strip())
    else:
        lines.append("Aucun résumé.")
    lines.append("")

    lines.append("## Cause probable")
    probable_cause = report.get("probable_cause")
    if not isinstance(probable_cause, dict):
        probable_cause = {}

    cause_text = (
        probable_cause.get("text")
        or probable_cause.get("cause")
        or probable_cause.get("description")
    )
    if isinstance(cause_text, str) and cause_text.strip():
        lines.append(cause_text.strip())
    else:
        lines.append("Cause inconnue.")

    lines.append(f"Confiance : {_format_confidence(probable_cause.get('confidence'))}")

    cause_ids = _evidence_ids_display(probable_cause.get("evidence"))
    if cause_ids:
        lines.append(f"Preuves : {', '.join(cause_ids)}")
    lines.append("")

    lines.append("## Timeline")
    timeline = report.get("timeline")
    if isinstance(timeline, list) and timeline:
        for event in timeline:
            if not isinstance(event, dict):
                continue
            ts = event.get("ts") or event.get("timestamp") or event.get("time") or ""
            description = event.get("description") or event.get("text") or event.get("event") or ""
            ids = _evidence_ids_display(event.get("evidence"))
            suffix = f" (preuves : {', '.join(ids)})" if ids else ""
            lines.append(f"- **{ts}** {description}{suffix}".rstrip())
    else:
        lines.append("- Aucune timeline.")
    lines.append("")

    lines.append("## Hypothèses")
    hypotheses = report.get("hypotheses")
    if isinstance(hypotheses, list) and hypotheses:
        for hypothesis in hypotheses:
            if not isinstance(hypothesis, dict):
                continue
            text = hypothesis.get("text") or hypothesis.get("hypothesis") or hypothesis.get("description")
            would_confirm = hypothesis.get("would_confirm") or hypothesis.get("confirmation")
            if not text and not would_confirm:
                continue
            lines.append(f"- {text} — pour confirmer : {would_confirm}")
    else:
        lines.append("- Aucune hypothèse.")
    lines.append("")

    lines.append("## Actions immédiates")
    actions = report.get("immediate_actions")
    if isinstance(actions, list) and actions:
        for action in actions:
            if not isinstance(action, dict):
                continue
            text = action.get("action") or action.get("text") or action.get("description")
            risk = action.get("risk") or action.get("risque") or "risque inconnu"
            runbook = action.get("runbook") or service.get("runbook")
            lines.append(f"- {text}")
            lines.append(f"  - Risque : {risk}")
            if runbook:
                lines.append(f"  - Runbook : {runbook}")
        lines.append("Suggestions à exécuter par un humain")
    else:
        lines.append("- Aucune action immédiate.")
    lines.append("")

    lines.append("## Améliorations")
    improvements = report.get("improvements")
    if isinstance(improvements, list) and improvements:
        for improvement in improvements:
            if not isinstance(improvement, dict):
                continue
            text = improvement.get("text") or improvement.get("improvement") or improvement.get("description")
            category = improvement.get("category") or improvement.get("categorie")
            if category:
                lines.append(f"- [{category}] {text}")
            else:
                lines.append(f"- {text}")
    else:
        lines.append("- Aucune amélioration.")
    lines.append("")

    lines.append("## Preuves manquantes")
    missing = report.get("missing_evidence")
    if isinstance(missing, list) and missing:
        for item in missing:
            lines.append(f"- {item}")
    else:
        lines.append("- Aucune preuve manquante.")
    lines.append("")

    lines.append("## Preuves")
    if evidence:
        for item in evidence:
            if not isinstance(item, dict):
                continue
            evidence_id = item.get("id") or "sans-id"
            source = item.get("source") or "inconnu"
            ts = item.get("ts") or ""
            text = item.get("text") or ""
            ref = item.get("ref")
            lines.append(f"- **{evidence_id}** [{source}] {ts} — {text}")
            if ref:
                lines.append(f"  - ref: `{ref}`")
    else:
        lines.append("- Aucune preuve.")

    return "\n".join(lines)


def _step(on_step: StepCallback | None, name: str, status: str, detail: str) -> None:
    if on_step is not None:
        on_step(name, status, detail)


def _window(first_seen: str | None, settings: Settings) -> dict[str, str]:
    try:
        t = parse_ts(first_seen) if first_seen else datetime.now(timezone.utc)
    except ValueError:
        t = datetime.now(timezone.utc)

    start = t - timedelta(minutes=settings.window_before_min)
    end = t + timedelta(minutes=settings.window_after_min)
    changes_start = t - timedelta(hours=settings.changes_lookback_h)

    return {
        "t": _iso(t),
        "start": _iso(start),
        "end": _iso(end),
        "changes_start": _iso(changes_start),
    }


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _highest_priority_alert(conn: sqlite3.Connection, group: dict) -> dict:
    alert_ids = group.get("alert_ids") or []
    best: dict | None = None
    best_rank = 99

    for alert_id in alert_ids:
        alert = db.get_alert(conn, str(alert_id))
        if alert is None:
            continue
        rank = _priority_rank(alert.get("priority"))
        if rank < best_rank:
            best_rank = rank
            best = alert

    if best is None:
        raise ValueError(f"Aucune alerte trouvée pour le groupe {group.get('id')}")
    return best


def _priority_rank(priority: object) -> int:
    if not priority:
        return 99
    text = str(priority).strip().upper()
    if text.startswith("P") and text[1:].isdigit():
        return int(text[1:])
    return 99


def _alert_payload(alert: dict, group: dict) -> dict:
    return {
        "id": alert.get("id"),
        "alias": alert.get("alias"),
        "message": group.get("title") or alert.get("message"),
        "priority": group.get("priority") or alert.get("priority"),
        "service": group.get("service"),
        "env": group.get("env"),
        "count": group.get("count", 0),
        "created_at": group.get("first_seen") or alert.get("created_at"),
        "updated_at": alert.get("updated_at"),
    }


def _group_payload(group: dict) -> dict:
    return {
        "id": group.get("id"),
        "service": group.get("service"),
        "env": group.get("env"),
        "priority": group.get("priority"),
        "title": group.get("title"),
        "count": group.get("count"),
        "first_seen": group.get("first_seen"),
        "last_seen": group.get("last_seen"),
        "flapping": group.get("flapping"),
        "score": group.get("score"),
        "final_score": group.get("final_score"),
        "decision": group.get("decision"),
        "reason": group.get("reason"),
        "factors": group.get("factors", {}),
    }


def _service_payload(entry: dict, service: str) -> dict:
    ops = entry.get("ops") if isinstance(entry.get("ops"), dict) else {}
    return {
        "name": entry.get("name") or service,
        "criticality": entry.get("criticality"),
        "owner": entry.get("owner"),
        "runbook": ops.get("runbook"),
    }


def _condense_evidence(
    settings: Settings,
    alert_payload: dict,
    evidence: list[dict],
    on_step: StepCallback | None,
) -> list[dict]:
    sources: list[str] = []
    by_source: dict[str, list[dict]] = {}

    for item in evidence:
        source = str(item.get("source") or "unknown")
        if source not in by_source:
            by_source[source] = []
            sources.append(source)
        by_source[source].append(item)

    condensed_sources = 0
    for source in sources:
        items = by_source[source]
        if len(items) > 15:
            by_source[source] = _condense_source(settings, alert_payload, source, items)
            condensed_sources += 1

    if condensed_sources:
        _step(on_step, "Résumé des preuves", "ok", f"{condensed_sources} sources condensées")
    else:
        _step(on_step, "Résumé des preuves", "ok", "aucune source à condenser")

    result: list[dict] = []
    for source in sources:
        result.extend(by_source[source])
    return result


def _condense_source(settings: Settings, alert_payload: dict, source: str, items: list[dict]) -> list[dict]:
    existing_ids = {item.get("id") for item in items if item.get("id")}
    by_id = {item.get("id"): item for item in items if item.get("id")}

    payload = {
        "alert": alert_payload,
        "source": source,
        "max_items": 15,
        "items": items,
    }

    try:
        data = llm.chat_json(settings, "evidence-summarizer", payload)
    except Exception:
        return items[:15]

    returned = data.get("items") if isinstance(data, dict) else None
    if not isinstance(returned, list):
        return items[:15]

    result: list[dict] = []
    seen: set[str] = set()

    for item in returned:
        if not isinstance(item, dict):
            continue

        ids = item.get("ids")
        if isinstance(ids, str):
            ids = [ids]
        if not isinstance(ids, list):
            continue

        valid_ids = [str(value) for value in ids if str(value) in existing_ids]
        if not valid_ids:
            continue

        chosen_id = valid_ids[0]
        if chosen_id in seen:
            continue
        seen.add(chosen_id)

        base = dict(item)
        base.pop("ids", None)
        base["id"] = chosen_id

        original = by_id.get(chosen_id, {})
        for field in ("source", "ts", "kind", "text", "ref"):
            if base.get(field) in (None, ""):
                base[field] = original.get(field, "")

        base.setdefault("source", source)
        result.append(base)

    if not result:
        return items[:15]
    return result[:15]


def _cap_confidence(report: dict, missing: list[str]) -> None:
    if not missing:
        return

    probable_cause = report.get("probable_cause")
    if not isinstance(probable_cause, dict):
        return

    try:
        confidence = float(probable_cause.get("confidence"))
    except (TypeError, ValueError):
        return

    if confidence > 0.6:
        probable_cause["confidence"] = 0.6


def _add_evidence_ids(value: object, ids: set[str]) -> None:
    if isinstance(value, str):
        if value:
            ids.add(value)
        return

    if not isinstance(value, list):
        return

    for item in value:
        if isinstance(item, str):
            if item:
                ids.add(item)
        elif isinstance(item, dict):
            evidence_id = item.get("id")
            if isinstance(evidence_id, str) and evidence_id:
                ids.add(evidence_id)


def _evidence_ids_display(value: object) -> list[str]:
    ids: list[str] = []
    if isinstance(value, str):
        if value:
            ids.append(value)
        return ids

    if not isinstance(value, list):
        return ids

    for item in value:
        if isinstance(item, str):
            if item:
                ids.append(item)
        elif isinstance(item, dict):
            evidence_id = item.get("id")
            if isinstance(evidence_id, str) and evidence_id:
                ids.append(evidence_id)

    return ids


def _format_confidence(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "inconnue"
    return f"{number:.1f}".replace(".", ",")
