from __future__ import annotations

import os
import sys
from dataclasses import dataclass

VALID_SOURCES = ("elasticsearch", "victoriametrics", "argocd", "azure")


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
    sources: tuple[str, ...] = ()  # AIOPS_SOURCES
    mcp_url: str = ""  # LITELLM_MCP_URL
    mcp_api_key: str = ""  # LITELLM_API_KEY
    vm_mode: str = "mcp"  # VM_MODE
    es_mode: str = "mcp"  # ES_MODE
    vm_mcp_tool: str = "query_range"  # VM_MCP_TOOL
    es_mcp_tool: str = "search"  # ES_MCP_TOOL


def load_dotenv(path: str) -> None:
    if not os.path.isfile(path):
        return

    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if line.startswith("export "):
                line = line[len("export "):].strip()

            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue

            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]

            os.environ.setdefault(key, value)


def _env_str(name: str, default: str) -> str:
    # Empty means "use the default", so a .env with blank values is safe.
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


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


def _env_sources(name: str) -> tuple[str, ...]:
    value = os.environ.get(name, "")
    items = [part.strip().lower() for part in value.split(",") if part.strip()]
    for item in items:
        if item not in VALID_SOURCES:
            print(f"warning: unknown source {item!r} in {name}, valid: {', '.join(VALID_SOURCES)}", file=sys.stderr)
    return tuple(item for item in items if item in VALID_SOURCES)


def _llm_url() -> str:
    value = _env_str("AIOPS_LLM_URL", "http://127.0.0.1:8000/v1")
    return "" if value.lower() in ("off", "none") else value


def load() -> Settings:
    load_dotenv(os.environ.get("AIOPS_ENV_FILE", ".env"))
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
        llm_url=_llm_url(),
        llm_model=_env_str("AIOPS_LLM_MODEL", "qwen3.8-27b"),
        llm_api_key=_env_str("AIOPS_LLM_API_KEY", ""),
        llm_max_tokens=_env_int("AIOPS_LLM_MAX_TOKENS", 16000),
        collector_timeout=_env_float("AIOPS_COLLECTOR_TIMEOUT", 10.0),
        window_before_min=_env_int("AIOPS_WINDOW_BEFORE_MIN", 30),
        window_after_min=_env_int("AIOPS_WINDOW_AFTER_MIN", 10),
        changes_lookback_h=_env_int("AIOPS_CHANGES_LOOKBACK_H", 2),
        sources=_env_sources("AIOPS_SOURCES"),
        mcp_url=_env_str("LITELLM_MCP_URL", ""),
        mcp_api_key=_env_str("LITELLM_API_KEY", ""),
        vm_mode=_env_str("VM_MODE", "mcp"),
        es_mode=_env_str("ES_MODE", "mcp"),
        vm_mcp_tool=_env_str("VM_MCP_TOOL", "query_range"),
        es_mcp_tool=_env_str("ES_MCP_TOOL", "search"),
    )
