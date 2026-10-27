from __future__ import annotations

import os

import yaml


def load(path: str) -> dict[str, dict]:
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        return {}
    services = data.get("services")
    if not isinstance(services, list):
        return {}
    catalog: dict[str, dict] = {}
    for entry in services:
        if isinstance(entry, dict) and entry.get("name"):
            catalog[str(entry["name"])] = entry
    return catalog


def for_service(catalog: dict[str, dict], service: str | None) -> dict | None:
    if service is None:
        return None
    return catalog.get(service)
