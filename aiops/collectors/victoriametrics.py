from __future__ import annotations

import base64
import json
import math
import urllib.parse
import urllib.request

from aiops.config import Settings
from aiops.db import parse_ts

DEFAULT_QUERIES = {
    "error_rate": 'sum(rate(http_requests_total{selector,status=~"5.."}[5m]))',
    "latency_p95": 'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{selector}[5m])) by (le))',
    "cpu": 'sum(rate(container_cpu_usage_seconds_total{selector}[5m]))',
}


def _basic_auth_header(user: str, password: str) -> str | None:
    if not user:
        return None
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _get_json(url: str, settings: Settings, user: str, password: str) -> dict:
    headers: dict[str, str] = {}
    auth = _basic_auth_header(user, password)
    if auth is not None:
        headers["Authorization"] = auth

    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=settings.collector_timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _format_3sig(value: float) -> str:
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    if value == 0:
        return "0.00"

    text = format(value, ".3g")
    if "e" in text or "E" in text:
        return text

    if "." not in text:
        if abs(value) >= 100:
            return text
        if abs(value) >= 10:
            return f"{text}.0"
        return f"{text}.00"

    return text


def collect(settings: Settings, alert: dict, entry: dict, window: dict) -> list[dict]:
    vm = entry.get("victoriametrics") if isinstance(entry, dict) else None
    if not isinstance(vm, dict):
        vm = {}

    labels = vm.get("labels")
    if not settings.vm_url or not isinstance(labels, dict) or not labels:
        raise RuntimeError("not configured")

    selector = ",".join(f'{key}="{value}"' for key, value in labels.items())

    queries = dict(DEFAULT_QUERIES)
    entry_queries = vm.get("queries")
    if isinstance(entry_queries, dict):
        queries.update(entry_queries)

    t = parse_ts(window.get("t", ""))
    start_epoch = int(t.timestamp() - 3600)
    end_epoch = int(parse_ts(window.get("end", "")).timestamp())
    window_start = parse_ts(window.get("start", ""))
    window_end = parse_ts(window.get("end", ""))

    items: list[dict] = []
    for name, query_template in queries.items():
        if not query_template:
            continue

        name = str(name)
        query = str(query_template).replace("{selector}", selector)
        url = (
            f"{settings.vm_url.rstrip('/')}/api/v1/query_range"
            f"?query={urllib.parse.quote(query)}"
            f"&start={start_epoch}"
            f"&end={end_epoch}"
            f"&step=60"
        )

        data = _get_json(url, settings, settings.vm_user, settings.vm_password)
        result_wrapper = data.get("data")
        if not isinstance(result_wrapper, dict):
            result_wrapper = {}
        result = result_wrapper.get("result")
        if not isinstance(result, list):
            continue

        baseline_values: list[float] = []
        window_values: list[float] = []

        for series in result:
            if not isinstance(series, dict):
                continue

            values = series.get("values")
            if not isinstance(values, list):
                continue

            for value in values:
                if not isinstance(value, list) or len(value) < 2:
                    continue

                try:
                    ts = float(value[0])
                    number = float(value[1])
                except (TypeError, ValueError):
                    continue

                if not math.isfinite(number):
                    continue

                if ts < window_start.timestamp():
                    baseline_values.append(number)
                elif ts <= window_end.timestamp():
                    window_values.append(number)

        if not window_values:
            continue

        peak = max(window_values)
        baseline = sum(baseline_values) / len(baseline_values) if baseline_values else 0.0

        if baseline > 0:
            ratio = peak / baseline
        else:
            ratio = 1.0 if peak == 0 else float("inf")

        items.append(
            {
                "prefix": "met",
                "source": "victoriametrics",
                "ts": f"{window.get('start')}/{window.get('end')}",
                "kind": name,
                "text": (
                    f"{name} peak {_format_3sig(peak)} "
                    f"vs baseline {_format_3sig(baseline)} "
                    f"(x{_format_3sig(ratio)})"
                ),
                "ref": query,
            }
        )

    return items
