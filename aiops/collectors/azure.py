from __future__ import annotations

import json
import subprocess

from aiops.config import Settings


def collect(settings: Settings, alert: dict, entry: dict, window: dict) -> list[dict]:
    azure = entry.get("azure") if isinstance(entry, dict) else None
    if not isinstance(azure, dict):
        azure = {}

    resource_ids = azure.get("resource_ids")
    if not isinstance(resource_ids, list) or not resource_ids:
        raise RuntimeError("not configured")

    items: list[dict] = []

    for resource_id in resource_ids:
        cmd = [
            settings.az_bin,
            "monitor",
            "activity-log",
            "list",
            "--resource-id",
            str(resource_id),
            "--start-time",
            str(window.get("changes_start", "")),
            "--end-time",
            str(window.get("end", "")),
            "-o",
            "json",
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=settings.collector_timeout,
            check=True,
        )

        try:
            events = json.loads(result.stdout)
        except json.JSONDecodeError:
            events = []

        if not isinstance(events, list):
            continue

        for event in events:
            if not isinstance(event, dict):
                continue

            operation = event.get("operationName")
            if not isinstance(operation, dict):
                operation = {}

            operation_value = str(operation.get("value") or "")
            if operation_value.endswith("/read"):
                continue

            localized = str(
                operation.get("localizedValue") or operation_value or "unknown operation"
            )

            status = event.get("status")
            if not isinstance(status, dict):
                status = {}
            status_value = str(status.get("value") or "Unknown")

            caller = str(event.get("caller") or "unknown")
            ts = str(event.get("eventTimestamp") or window.get("t", ""))

            items.append(
                {
                    "prefix": "chg",
                    "source": "azure",
                    "ts": ts,
                    "kind": "azure_activity",
                    "text": f"{localized} {status_value} by {caller}",
                    "ref": " ".join(cmd),
                }
            )

    return items
