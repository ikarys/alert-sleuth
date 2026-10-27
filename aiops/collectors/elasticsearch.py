from __future__ import annotations

import base64
import json
import re
import urllib.request

from aiops import mcp
from aiops.config import Settings


def _basic_auth_header(user: str, password: str) -> str | None:
    if not user:
        return None
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _post_json(url: str, payload: dict, settings: Settings, user: str, password: str) -> dict:
    headers = {"Content-Type": "application/json"}
    auth = _basic_auth_header(user, password)
    if auth is not None:
        headers["Authorization"] = auth

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=settings.collector_timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def template(msg: str) -> str:
    # ponytail: regex templating, switch to Drain3 when templates fragment
    text = re.sub(r'"[^"]*"|\'[^\']*\'', "<*>", msg)
    text = re.sub(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        "<*>",
        text,
    )
    text = re.sub(r"\b[0-9a-fA-F]{8,}\b", "<*>", text)
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "<*>", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "<*>", text)
    return text


def _message(source: object) -> str:
    if isinstance(source, dict):
        message = source.get("message")
        if message is not None:
            return str(message)

        log = source.get("log")
        if isinstance(log, dict):
            message = log.get("message")
            if message is not None:
                return str(message)

    return json.dumps(source, ensure_ascii=False)


def _sources_from_payload(payload: object) -> list[dict] | None:
    if isinstance(payload, dict):
        hits_wrapper = payload.get("hits")
        if isinstance(hits_wrapper, dict):
            hits = hits_wrapper.get("hits")
            if isinstance(hits, list):
                sources: list[dict] = []
                for hit in hits:
                    if not isinstance(hit, dict):
                        continue
                    source = hit.get("_source", {})
                    if isinstance(source, dict):
                        sources.append(source)
                return sources
        return None

    if isinstance(payload, list):
        sources = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            if "_source" in item:
                source = item["_source"]
                if isinstance(source, dict):
                    sources.append(source)
            else:
                sources.append(item)
        return sources if sources else None

    return None


def _extract_hits(payloads: list[str]) -> list[dict]:
    for payload in mcp.parse_json_texts(payloads):
        sources = _sources_from_payload(payload)
        if sources is not None:
            return sources
    return []


def collect(settings: Settings, alert: dict, entry: dict, window: dict) -> list[dict]:
    es = entry.get("elasticsearch") if isinstance(entry, dict) else None
    if not isinstance(es, dict):
        es = {}

    index_pattern = es.get("index_pattern")
    query = es.get("query")
    if not index_pattern or not query:
        raise RuntimeError("not configured")

    if settings.es_mode == "http":
        if not settings.es_url:
            raise RuntimeError("not configured")
    else:
        if not settings.mcp_url:
            raise RuntimeError("not configured")

    query_string = (
        f"({query}) AND (level:(error OR warn OR warning OR fatal) "
        "OR log.level:(error OR warn OR warning OR fatal))"
    )
    payload = {
        "size": 500,
        "sort": [{"@timestamp": {"order": "asc"}}],
        "query": {
            "bool": {
                "filter": [
                    {
                        "range": {
                            "@timestamp": {
                                "gte": window.get("start"),
                                "lte": window.get("end"),
                            }
                        }
                    },
                    {"query_string": {"query": query_string}},
                ]
            }
        },
    }

    if settings.es_mode == "mcp":
        payloads = mcp.call_tool(
            settings,
            settings.es_mcp_tool,
            {
                "index": index_pattern,
                "query_body": payload,
            },
        )
        sources = _extract_hits(payloads)
    else:
        url = f"{settings.es_url.rstrip('/')}/{index_pattern}/_search"
        data = _post_json(url, payload, settings, settings.es_user, settings.es_password)
        hits_wrapper = data.get("hits")
        if not isinstance(hits_wrapper, dict):
            hits_wrapper = {}
        hits = hits_wrapper.get("hits")
        if not isinstance(hits, list):
            hits = []

        sources = []
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            source = hit.get("_source", {})
            if isinstance(source, dict):
                sources.append(source)

    groups: dict[str, dict[str, object]] = {}
    for source in sources:
        ts = source.get("@timestamp") if isinstance(source, dict) else None
        if not ts:
            ts = window.get("t", "")
        ts = str(ts)

        tpl = template(_message(source))
        group = groups.get(tpl)
        if group is None:
            groups[tpl] = {"count": 1, "first": ts, "last": ts}
        else:
            group["count"] = int(group.get("count", 0)) + 1
            if ts < str(group.get("first", ts)):
                group["first"] = ts
            if ts > str(group.get("last", ts)):
                group["last"] = ts

    ranked = sorted(
        groups.items(),
        key=lambda item: (-int(item[1]["count"]), str(item[1]["first"])),
    )[:15]

    items: list[dict] = []
    for tpl, group in ranked:
        text = f"{int(group['count'])}x {tpl}"
        if len(text) > 200:
            text = text[:200]

        items.append(
            {
                "prefix": "log",
                "source": "elasticsearch",
                "ts": f"{group['first']}/{group['last']}",
                "kind": "log_template",
                "text": text,
                "ref": query_string,
            }
        )

    return items
