from __future__ import annotations

import json
import subprocess

from aiops.config import Settings
from aiops.db import parse_ts


def collect(settings: Settings, alert: dict, entry: dict, window: dict) -> list[dict]:
    argocd = entry.get("argocd") if isinstance(entry, dict) else None
    if not isinstance(argocd, dict):
        argocd = {}

    app = argocd.get("app")
    if not app:
        raise RuntimeError("not configured")

    app = str(app)
    cmd = [settings.argocd_bin, "app", "get", app, "-o", "json"]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=settings.collector_timeout,
        check=True,
    )

    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        data = {}

    health = data.get("health")
    if not isinstance(health, dict):
        health = {}
    sync = data.get("sync")
    if not isinstance(sync, dict):
        sync = {}

    health_status = str(health.get("status", "Unknown"))
    sync_status = str(sync.get("status", "Unknown"))

    items: list[dict] = [
        {
            "prefix": "hlt",
            "source": "argocd",
            "ts": window.get("t", ""),
            "kind": "argocd_status",
            "text": f"health={health_status} sync={sync_status}",
            "ref": f"argocd app get {app}",
        }
    ]

    status = data.get("status")
    if not isinstance(status, dict):
        status = {}
    history = status.get("history")
    if not isinstance(history, list):
        history = []

    changes_start = parse_ts(window.get("changes_start", ""))
    end = parse_ts(window.get("end", ""))

    for record in history:
        if not isinstance(record, dict):
            continue

        deployed_at = record.get("deployedAt")
        if not deployed_at:
            continue

        try:
            deployed = parse_ts(str(deployed_at))
        except ValueError:
            continue

        if changes_start <= deployed <= end:
            revision = str(record.get("revision", ""))[:12]
            deploy_id = record.get("id", "")
            items.append(
                {
                    "prefix": "dep",
                    "source": "argocd",
                    "ts": str(deployed_at),
                    "kind": "deploy",
                    "text": f"deploy id={deploy_id} revision={revision}",
                    "ref": f"argocd app history {app}",
                }
            )

    return items
