from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = "aiops.db"  # AIOPS_DB
    catalog_path: str = "catalog/services.yaml"  # AIOPS_CATALOG
    skills_dir: str = "skills"  # AIOPS_SKILLS
    demo: bool = False  # AIOPS_DEMO in ("1","true","yes")
    fixtures_dir: str = "tests/fixtures/demo"  # AIOPS_FIXTURES
    opsgenie_url: str = "https://api.opsgenie.com"  # OPSGENIE_URL
    opsgenie_api_key: str = ""  # OPSGENIE_API_KEY
    opsgenie_query: str = "status:open"  # OPSGENIE_QUERY
    es_url: str = ""  # ES_URL
    es_user: str = ""  # ES_USER
    es_password: str = ""  # ES_PASSWORD
    vm_url: str = ""  # VM_URL
    vm_user: str = ""  # VM_USER
    vm_password: str = ""  # VM_PASSWORD
    argocd_bin: str = "argocd"  # ARGOCD_BIN (auth via ARGOCD_SERVER / ARGOCD_AUTH_TOKEN env)
    az_bin: str = "az"  # AZ_BIN
    llm_url: str = "http://127.0.0.1:8000/v1"  # AIOPS_LLM_URL ("" disables the LLM)
    llm_model: str = "qwen3.8-27b"  # AIOPS_LLM_MODEL
    llm_api_key: str = ""  # AIOPS_LLM_API_KEY
    llm_max_tokens: int = 16000  # AIOPS_LLM_MAX_TOKENS (reasoning models need room)
    collector_timeout: float = 10.0  # AIOPS_COLLECTOR_TIMEOUT
    window_before_min: int = 30
    window_after_min: int = 10
    changes_lookback_h: int = 2


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes")


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return int(value.strip())


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return float(value.strip())


def load() -> Settings:
    return Settings(
        db_path=_env_str("AIOPS_DB", "aiops.db"),
        catalog_path=_env_str("AIOPS_CATALOG", "catalog/services.yaml"),
        skills_dir=_env_str("AIOPS_SKILLS", "skills"),
        demo=_env_bool("AIOPS_DEMO", False),
        fixtures_dir=_env_str("AIOPS_FIXTURES", "tests/fixtures/demo"),
        opsgenie_url=_env_str("OPSGENIE_URL", "https://api.opsgenie.com"),
        opsgenie_api_key=_env_str("OPSGENIE_API_KEY", ""),
        opsgenie_query=_env_str("OPSGENIE_QUERY", "status:open"),
        es_url=_env_str("ES_URL", ""),
        es_user=_env_str("ES_USER", ""),
        es_password=_env_str("ES_PASSWORD", ""),
        vm_url=_env_str("VM_URL", ""),
        vm_user=_env_str("VM_USER", ""),
        vm_password=_env_str("VM_PASSWORD", ""),
        argocd_bin=_env_str("ARGOCD_BIN", "argocd"),
        az_bin=_env_str("AZ_BIN", "az"),
        llm_url=_env_str("AIOPS_LLM_URL", "http://127.0.0.1:8000/v1"),
        llm_model=_env_str("AIOPS_LLM_MODEL", "qwen3.8-27b"),
        llm_api_key=_env_str("AIOPS_LLM_API_KEY", ""),
        llm_max_tokens=_env_int("AIOPS_LLM_MAX_TOKENS", 16000),
        collector_timeout=_env_float("AIOPS_COLLECTOR_TIMEOUT", 10.0),
        window_before_min=_env_int("AIOPS_WINDOW_BEFORE_MIN", 30),
        window_after_min=_env_int("AIOPS_WINDOW_AFTER_MIN", 10),
        changes_lookback_h=_env_int("AIOPS_CHANGES_LOOKBACK_H", 2),
    )
