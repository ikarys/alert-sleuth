from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from aiops import analysis, collectors, db, ingest, mcp, triage
from aiops.collectors import elasticsearch as es_collector
from aiops.collectors import victoriametrics as vm_collector
from aiops.config import Settings, load
from aiops.web.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]

UNKNOWN_REPORT = {
    "summary": "Résumé de test.",
    "probable_cause": {
        "text": "Cause de test.",
        "confidence": 0.9,
        "evidence": ["alt-1", "unknown-999"],
    },
    "timeline": [],
    "hypotheses": [
        {"text": "Hypothèse de test 1", "would_confirm": "Vérifier le premier signal"},
        {"text": "Hypothèse de test 2", "would_confirm": "Vérifier le second signal"},
    ],
    "immediate_actions": [
        {"action": "Vérifier le service", "risk": "low", "evidence": ["alt-1"]}
    ],
    "improvements": [
        {"text": "Amélioration de test", "category": "process"}
    ],
}


def make_settings(tmp_path: Path, llm_url: str = "http://llm.test/v1") -> Settings:
    return Settings(
        db_path=str(tmp_path / "t.db"),
        demo=True,
        catalog_path=str(REPO_ROOT / "catalog" / "services.yaml"),
        skills_dir=str(REPO_ROOT / "skills"),
        fixtures_dir=str(REPO_ROOT / "tests" / "fixtures" / "demo"),
        llm_url=llm_url,
        llm_model="test-model",
    )


def make_fake_chat_json(
    incident_payload: dict | None = None,
    counter: list[int] | None = None,
) -> Callable[[Settings, str, dict], dict]:
    def fake_chat_json(settings: Settings, skill: str, payload: dict) -> dict:
        if skill == "alert-triage":
            groups = payload.get("groups", [])
            adjustments: list[dict] = []
            if isinstance(groups, list):
                for group in groups:
                    if isinstance(group, dict) and group.get("service") == "checkout":
                        adjustments.append(
                            {
                                "group_id": group.get("group_id"),
                                "adjustment": 5,
                                "reason": "ajustement de test",
                            }
                        )
            return {"groups": adjustments}

        if skill == "incident-analysis":
            if counter is not None:
                counter[0] += 1
            if incident_payload is not None:
                return incident_payload

            evidence = payload.get("evidence", [])
            evidence_ids: list[str] = []
            if isinstance(evidence, list):
                for item in evidence:
                    if isinstance(item, dict):
                        evidence_id = item.get("id")
                        if evidence_id and evidence_id != "alt-1":
                            evidence_ids.append(str(evidence_id))
            cite = evidence_ids[0] if evidence_ids else "alt-1"
            return {
                "summary": "Résumé de test.",
                "probable_cause": {
                    "text": "Cause probable de test.",
                    "confidence": 0.9,
                    "evidence": ["alt-1", cite],
                },
                "timeline": [
                    {
                        "ts": "2026-02-14T09:40:00Z",
                        "description": "Alerte reçue",
                        "evidence": ["alt-1"],
                    }
                ],
                "hypotheses": [
                    {"text": "Hypothèse de test 1", "would_confirm": "Vérifier le premier signal"},
                    {"text": "Hypothèse de test 2", "would_confirm": "Vérifier le second signal"},
                ],
                "immediate_actions": [
                    {
                        "action": "Vérifier le service",
                        "risk": "low",
                        "evidence": [cite],
                    }
                ],
                "improvements": [
                    {"text": "Amélioration de test", "category": "process"}
                ],
            }

        if skill == "evidence-summarizer":
            return {"items": []}

        return {}

    return fake_chat_json


def setup_demo(
    settings: Settings,
    monkeypatch,
    fake: Callable[[Settings, str, dict], dict] | None = None,
) -> tuple[sqlite3.Connection, int, list[dict]]:
    if fake is None:
        fake = make_fake_chat_json()
    monkeypatch.setattr("aiops.llm.chat_json", fake)
    conn = db.connect(settings.db_path)
    fetched = ingest.fetch(settings, conn)
    groups = triage.run(settings, conn, use_llm=True)
    return conn, fetched, groups


def test_fetch_and_triage(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    conn, fetched, groups = setup_demo(settings, monkeypatch)
    try:
        assert fetched == 8
        assert len(groups) == 6
        checkout = next(group for group in groups if group["service"] == "checkout")
        assert checkout["decision"] == "investigate"
        for group in groups:
            assert 0 <= group["final_score"] <= 100
    finally:
        conn.close()


def test_build_groups_splits_after_15_minutes() -> None:
    alerts = [
        {
            "id": "a1",
            "service": "svc",
            "env": "prod",
            "status": "open",
            "priority": "P3",
            "count": 1,
            "created_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": "a2",
            "service": "svc",
            "env": "prod",
            "status": "open",
            "priority": "P3",
            "count": 1,
            "created_at": "2026-01-01T00:10:00Z",
        },
        {
            "id": "a3",
            "service": "svc",
            "env": "prod",
            "status": "open",
            "priority": "P3",
            "count": 1,
            "created_at": "2026-01-01T00:30:00Z",
        },
    ]
    groups = triage.build_groups(alerts, {})
    assert len(groups) == 2
    assert groups[0]["alert_ids"] == ["a1", "a2"]
    assert groups[1]["alert_ids"] == ["a3"]


def test_flapping_alias_recurrence() -> None:
    alerts = [
        {
            "id": "closed-1",
            "alias": "alias-flap",
            "service": "svc-flap",
            "env": "prod",
            "status": "closed",
            "priority": "P3",
            "message": "closed one",
            "count": 1,
            "created_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": "closed-2",
            "alias": "alias-flap",
            "service": "svc-flap",
            "env": "prod",
            "status": "closed",
            "priority": "P3",
            "message": "closed two",
            "count": 1,
            "created_at": "2026-01-01T00:10:00Z",
        },
        {
            "id": "open-flap",
            "alias": "alias-flap",
            "service": "svc-flap",
            "env": "prod",
            "status": "open",
            "priority": "P3",
            "message": "open flap",
            "count": 1,
            "created_at": "2026-01-01T00:20:00Z",
        },
        {
            "id": "open-noflap",
            "alias": "alias-noflap",
            "service": "svc-noflap",
            "env": "prod",
            "status": "open",
            "priority": "P3",
            "message": "open no flap",
            "count": 1,
            "created_at": "2026-01-01T00:25:00Z",
        },
    ]
    groups = triage.build_groups(alerts, {})
    flap = next(group for group in groups if group["service"] == "svc-flap")
    no_flap = next(group for group in groups if group["service"] == "svc-noflap")
    assert flap["flapping"] == 1
    assert no_flap["flapping"] == 0


def test_analyze_checkout_group(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    conn, _, groups = setup_demo(settings, monkeypatch)
    try:
        checkout = next(group for group in groups if group["service"] == "checkout")
        report_id = analysis.analyze(settings, conn, checkout["id"])
        data = db.get_report(conn, report_id)
        assert data is not None
        assert any("azure" in item and "timeout" in item for item in data["missing"])
        assert data["report"]["probable_cause"]["confidence"] == 0.6
        assert "## Timeline" in data["markdown"]
    finally:
        conn.close()


def test_unknown_cited_id_retries_once(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    counter = [0]
    fake = make_fake_chat_json(incident_payload=UNKNOWN_REPORT, counter=counter)
    conn, _, groups = setup_demo(settings, monkeypatch, fake=fake)
    try:
        checkout = next(group for group in groups if group["service"] == "checkout")
        report_id = analysis.analyze(settings, conn, checkout["id"])
        assert counter[0] == 2
        data = db.get_report(conn, report_id)
        assert data is not None
        assert data["report"]["unverified_ids"] == ["unknown-999"]
    finally:
        conn.close()


def test_web_pages_and_feedback(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    conn, _, groups = setup_demo(settings, monkeypatch)
    checkout = next(group for group in groups if group["service"] == "checkout")
    report_id = analysis.analyze(settings, conn, checkout["id"])
    conn.close()

    monkeypatch.setattr("aiops.jobs.start", lambda settings, kind, target, fn: "job-test")
    app = create_app(settings)
    client = TestClient(app)

    assert client.get("/alerts").status_code == 200
    assert client.get("/reports").status_code == 200
    assert client.get(f"/reports/{report_id}").status_code == 200
    assert client.get("/quality").status_code == 200

    response = client.post(
        f"/reports/{report_id}/feedback",
        data={"verdict": "useful", "root_cause": "deploy", "comment": "ok"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    page = client.get(f"/reports/{report_id}")
    assert page.status_code == 200
    assert "useful" in page.text
    assert "deploy" in page.text
    assert "pour confirmer" in page.text

    foreign = client.post(
        "/fetch",
        headers={"Origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert foreign.status_code == 403


def test_load_dotenv(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "\n"
        "export AIOPS_DOTENV_A=1\n"
        'AIOPS_DOTENV_B="quoted"\n'
        "AIOPS_DOTENV_C=\n"
        "AIOPS_DOTENV_EXISTING=from_file\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("AIOPS_ENV_FILE", str(env_file))
    monkeypatch.setenv("AIOPS_DOTENV_EXISTING", "real")
    monkeypatch.delenv("AIOPS_DOTENV_A", raising=False)
    monkeypatch.delenv("AIOPS_DOTENV_B", raising=False)
    monkeypatch.delenv("AIOPS_DOTENV_C", raising=False)

    load()

    assert os.environ["AIOPS_DOTENV_A"] == "1"
    assert os.environ["AIOPS_DOTENV_B"] == "quoted"
    assert os.environ["AIOPS_DOTENV_C"] == ""
    assert os.environ["AIOPS_DOTENV_EXISTING"] == "real"


def test_run_all_respects_sources(tmp_path: Path) -> None:
    settings = Settings(
        db_path=str(tmp_path / "t.db"),
        demo=True,
        catalog_path=str(REPO_ROOT / "catalog" / "services.yaml"),
        skills_dir=str(REPO_ROOT / "skills"),
        fixtures_dir=str(REPO_ROOT / "tests" / "fixtures" / "demo"),
        llm_url="http://llm.test/v1",
        llm_model="test-model",
        sources=("victoriametrics",),
    )

    steps: list[tuple[str, str, str]] = []

    def on_step(name: str, status: str, detail: str) -> None:
        steps.append((name, status, detail))

    evidence, missing = collectors.run_all(
        settings,
        {"service": "checkout"},
        {},
        {},
        on_step=on_step,
    )

    assert missing == []
    assert evidence
    assert all(item["id"].startswith("met-") for item in evidence)
    assert any(name == "argocd" and status == "skipped" for name, status, _ in steps)


def test_vm_mcp_mode(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        db_path=str(tmp_path / "t.db"),
        demo=False,
        vm_mode="mcp",
        mcp_url="http://x/mcp/",
    )

    window = {
        "t": "2026-02-14T09:40:00Z",
        "start": "2026-02-14T09:10:00Z",
        "end": "2026-02-14T09:50:00Z",
        "changes_start": "2026-02-14T07:40:00Z",
    }

    start_epoch = datetime(2026, 2, 14, 9, 10, tzinfo=timezone.utc).timestamp()
    payload = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {},
                    "values": [
                        [start_epoch - 60, "0.1"],
                        [start_epoch, "0.1"],
                        [start_epoch + 60, "5.0"],
                    ],
                }
            ],
        },
    }

    monkeypatch.setattr(
        "aiops.mcp.call_tool",
        lambda settings, name, arguments: [json.dumps(payload)],
    )

    items = vm_collector.collect(
        settings,
        {"service": "checkout"},
        {"victoriametrics": {"labels": {"service": "checkout"}}},
        window,
    )

    assert items
    assert any("peak" in item["text"] for item in items)


def test_es_mcp_mode(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        db_path=str(tmp_path / "t.db"),
        demo=False,
        es_mode="mcp",
        mcp_url="http://x/mcp/",
    )

    entry = {
        "elasticsearch": {
            "index_pattern": "logs-*",
            "query": "service:checkout",
        }
    }
    window = {
        "t": "2026-02-14T09:40:00Z",
        "start": "2026-02-14T09:10:00Z",
        "end": "2026-02-14T09:50:00Z",
        "changes_start": "2026-02-14T07:40:00Z",
    }

    hits = [
        {
            "_source": {
                "@timestamp": "2026-02-14T09:40:00Z",
                "message": "Timeout after 1234 ms",
            }
        }
        for _ in range(3)
    ]
    payload = {"hits": {"hits": hits}}

    monkeypatch.setattr(
        "aiops.mcp.call_tool",
        lambda settings, name, arguments: ["Total results: 3", json.dumps(payload)],
    )

    items = es_collector.collect(settings, {"service": "checkout"}, entry, window)

    assert len(items) == 1
    assert items[0]["text"] == "3x Timeout after <*> ms"
