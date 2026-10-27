from __future__ import annotations

import concurrent.futures
import json
import os
import time
from collections.abc import Callable

from aiops.config import Settings

from aiops.collectors import argocd, elasticsearch, victoriametrics, azure

SOURCES: dict[str, Callable[[Settings, dict, dict, dict], list[dict]]] = {
    "elasticsearch": elasticsearch.collect,
    "victoriametrics": victoriametrics.collect,
    "argocd": argocd.collect,
    "azure": azure.collect,
}


def _short_error(exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        message = exc.__class__.__name__
    return message[:120]


def _load_demo_evidence(fixtures_dir: str, source: str, service: str) -> list[dict]:
    path = os.path.join(fixtures_dir, "evidence", f"{source}.json")
    if not os.path.isfile(path):
        return []

    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, dict):
        return []

    payload = data.get(service)
    if payload is None:
        return []

    if isinstance(payload, dict):
        if payload.get("error") == "timeout":
            raise TimeoutError(f"{source} timeout")
        return []

    if isinstance(payload, list):
        return payload

    return []


def _assign_ids(items: list[dict]) -> None:
    counts: dict[str, int] = {}
    for item in items:
        prefix = item.pop("prefix", None)
        if prefix is None:
            continue
        prefix = str(prefix)
        counts[prefix] = counts.get(prefix, 0) + 1
        item["id"] = f"{prefix}-{counts[prefix]}"


def run_all(
    settings: Settings,
    alert: dict,
    entry: dict,
    window: dict,
    on_step: Callable[[str, str, str], None] | None = None,
) -> tuple[list[dict], list[str]]:
    evidence: list[dict] = []
    missing: list[str] = []
    service = str(alert.get("service") or "unknown")

    if settings.demo:
        for source in SOURCES:
            started = time.monotonic()
            try:
                items = _load_demo_evidence(settings.fixtures_dir, source, service)
            except TimeoutError:
                missing.append(f"{source} (timeout)")
                if on_step is not None:
                    on_step(source, "timeout", "délai dépassé")
                continue
            except Exception as exc:
                short = _short_error(exc)
                missing.append(f"{source} (error: {short})")
                if on_step is not None:
                    on_step(source, "error", short)
                continue

            elapsed = time.monotonic() - started
            evidence.extend(items)
            if on_step is not None:
                on_step(source, "ok", f"{len(items)} éléments, {elapsed:.1f} s")

        _assign_ids(evidence)
        return evidence, missing

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(SOURCES))
    futures = {
        source: executor.submit(SOURCES[source], settings, alert, entry, window)
        for source in SOURCES
    }

    try:
        for source, future in futures.items():
            started = time.monotonic()
            try:
                items = future.result(timeout=settings.collector_timeout + 1)
            except concurrent.futures.TimeoutError:
                missing.append(f"{source} (timeout)")
                if on_step is not None:
                    on_step(source, "timeout", "délai dépassé")
                continue
            except Exception as exc:
                short = _short_error(exc)
                missing.append(f"{source} (error: {short})")
                if on_step is not None:
                    on_step(source, "error", short)
                continue

            elapsed = time.monotonic() - started
            evidence.extend(items)
            if on_step is not None:
                on_step(source, "ok", f"{len(items)} éléments, {elapsed:.1f} s")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    _assign_ids(evidence)
    return evidence, missing
