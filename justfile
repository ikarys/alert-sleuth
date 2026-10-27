set dotenv-load

# Show available recipes.
default:
    just --list

# Install dependencies with uv.
install:
    uv sync

# Run a demo dev instance with auto-reload.
dev port="8080":
    AIOPS_DEMO=1 AIOPS_DB=dev.db uv run aiops fetch
    AIOPS_DEMO=1 AIOPS_DB=dev.db uv run aiops triage
    AIOPS_DEMO=1 AIOPS_DB=dev.db uv run uvicorn aiops.web.app:factory --factory --reload --host 127.0.0.1 --port {{port}}

# Run a real web instance from environment configuration.
web port="8080":
    uv run uvicorn aiops.web.app:factory --factory --host 127.0.0.1 --port {{port}}

# Fetch alerts from Opsgenie or demo fixtures.
fetch:
    uv run aiops fetch

# Run triage grouping.
triage:
    uv run aiops triage

# Analyze a group.
analyze group:
    uv run aiops analyze {{group}}

# Show a report.
report id:
    uv run aiops report {{id}}

# Show data quality scorecard.
quality:
    uv run aiops quality

# Run tests.
test *args:
    uv run pytest -q {{args}}

# Remove the demo database.
reset-dev:
    rm -f dev.db
