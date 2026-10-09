"""Simulated fault injection — mutates workspace files without git operations.

Mirrors fault_injector.py's inject_fault() API exactly but:
- Writes real file mutations (so SimulatedPipelineRunner's file-read checks work)
- Skips all git add/commit operations (no git required)
- Returns the same FaultMetadata dataclass
- Supports the full simulated fault catalog, including runtime-only env and virtualenv faults

This is used by SimulatedCICDRepairEnvironment when CICD_SIMULATE=true.
"""

from __future__ import annotations

import os
import random
import textwrap

from cicd.fault_types import (
    FaultMetadata,
    FAULT_TYPES,
    FAULT_KEYWORDS,
    FAULT_STAGE_MAP,
    FAULT_AFFECTED_APPS,
)


# ── Public API ──────────────────────────────────────────────────────────────

def inject_fault_simulated(workspace: str, fault_type: str) -> FaultMetadata:
    """Inject a fault into workspace files (no git). Returns FaultMetadata."""
    injectors = {
        "merge_conflict":      _inject_merge_conflict,
        "dependency_conflict": _inject_dependency_conflict,
        "docker_order":        _inject_docker_order,
        "flaky_test":          _inject_flaky_test,
        "missing_permission":  _inject_missing_permission,
        "secret_exposure":     _inject_secret_exposure,
        "env_drift":           _inject_env_drift,
        "invalid_database_url": _inject_invalid_database_url,
        "empty_secret_key":     _inject_empty_secret_key,
        "missing_pythonpath":   _inject_missing_pythonpath,
        "circular_import_runtime": _inject_circular_import_runtime,
        "missing_package_init": _inject_missing_package_init,
        "none_config_runtime":  _inject_none_config_runtime,
        "log_pii_leak":        _inject_log_pii_leak,
        "log_disabled":        _inject_log_disabled,
        "bad_migration_sql":   _inject_bad_migration_sql,
        "schema_drift":        _inject_schema_drift,
        "terraform_invalid_provider": _inject_terraform_invalid_provider,
        "terraform_missing_variable": _inject_terraform_missing_variable,
        "terraform_permission_denied": _inject_terraform_permission_denied,
    }
    if fault_type not in injectors:
        raise ValueError(f"Unknown fault type: {fault_type!r}. Valid: {list(injectors)}")

    metadata = injectors[fault_type](workspace)
    metadata.expected_fail_stage = FAULT_STAGE_MAP.get(fault_type, "build")
    metadata.keywords = FAULT_KEYWORDS.get(fault_type, [])
    metadata.affected_apps = FAULT_AFFECTED_APPS.get(fault_type, [])
    return metadata


def inject_random_fault_simulated(workspace: str) -> FaultMetadata:
    return inject_fault_simulated(workspace, random.choice(FAULT_TYPES))


# ── File helpers ─────────────────────────────────────────────────────────────

def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, FileNotFoundError):
        return ""


def _upsert_line(content: str, key: str, value: str) -> str:
    lines = content.splitlines()
    updated = False
    for idx, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[idx] = f"{key}={value}"
            updated = True
            break
    if not updated:
        lines.append(f"{key}={value}")
    normalized = "\n".join(lines).strip()
    return normalized + "\n"


def _write_default_env(path: str) -> str:
    content = _read(path)
    if not content.strip():
        content = (
            "DATABASE_URL=postgresql://postgres:postgres@db:5432/appdb\n"
            "SECRET_KEY=dev-secret-key\n"
            "FEATURE_CACHE_BACKEND=redis\n"
        )
    _write(path, content if content.endswith("\n") else content + "\n")
    return _read(path)


# ── Fault injectors (mirrors fault_injector.py exactly, no git) ─────────────

def _inject_merge_conflict(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "services", "api", "routes.py")
    content = _read(path)
    content = content.replace(
        '    @app.route("/health", methods=["GET"])\n'
        '    def health():\n'
        '        _log.info("Health check", extra={"request_id": getattr(g, "request_id", "")})\n'
        '        return jsonify({"status": "healthy", "service": "api"})',
        '    @app.route("/health", methods=["GET"])\n'
        '    def health():\n'
        '<<<<<<< HEAD\n'
        '        _log.info("Health check", extra={"request_id": getattr(g, "request_id", "")})\n'
        '        return jsonify({"status": "healthy", "service": "api", "version": "2.0"})\n'
        '=======\n'
        '        _log.info("Health check", extra={"request_id": getattr(g, "request_id", "")})\n'
        '        return jsonify({"status": "healthy", "service": "api"})\n'
        '>>>>>>> feature/new-health-check',
    )
    _write(path, content)
    return FaultMetadata(
        fault_type="merge_conflict",
        affected_files=["services/api/routes.py"],
        description="Unresolved merge conflict markers in routes.py causing SyntaxError",
    )


def _inject_dependency_conflict(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "services", "api", "requirements.txt")
    _write(path, "flask>=3.0.0\nrequests==2.28.0\nurllib3==2.0.7\ngunicorn>=21.2.0\n")
    return FaultMetadata(
        fault_type="dependency_conflict",
        affected_files=["services/api/requirements.txt"],
        description="Incompatible pip dependency versions: requests==2.28.0 requires urllib3<2.0",
    )


def _inject_docker_order(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "Dockerfile")
    _write(path, textwrap.dedent("""\
        FROM python:3.11-slim

        WORKDIR /app

        # Wrong order: install before copying requirements into image
        RUN uv pip install --system --no-cache -r services/api/requirements.txt

        # Copy application code too late
        COPY . /app/

        EXPOSE 5000

        CMD ["python", "-m", "services.api.app"]
    """))
    return FaultMetadata(
        fault_type="docker_order",
        affected_files=["Dockerfile"],
        description="Dockerfile installs requirements before COPY, so file is unavailable at build time",
    )


def _inject_flaky_test(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "tests", "test_api.py")
    content = _read(path)
    flaky = textwrap.dedent('''

    def test_response_time_health(client):
        """Timing-sensitive test that fails intermittently due to tight threshold."""
        import time
        start = time.time()
        time.sleep(0.1)
        response = client.get("/health")
        elapsed = time.time() - start
        assert response.status_code == 200
        # Threshold is unrealistically tight -- always fails after the sleep
        threshold = 0.001
        assert elapsed < threshold, (
            f"Health endpoint took {elapsed:.3f}s, expected < {threshold}s "
            f"(flaky: timing constraint too strict for this environment)."
        )
    ''')
    _write(path, content + flaky)
    return FaultMetadata(
        fault_type="flaky_test",
        affected_files=["tests/test_api.py"],
        description="Timing-sensitive test with impossibly tight threshold always fails",
    )


def _inject_missing_permission(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "docker-compose.yml")
    _write(path, textwrap.dedent("""\
        version: "3.8"

        services:
          api:
            build:
              context: .
              dockerfile: Dockerfile
            ports:
              - "5000:5000"
            environment:
              - FLASK_ENV=production
              - LOG_PATH=/app/logs/app.log
              - LOG_LEVEL=INFO
              - SERVICE_NAME=api
            volumes:
              - ./logs:/app/logs
            networks:
              - corp-internal-network-v2
            healthcheck:
              test: ["CMD", "curl", "-f", "http://localhost:5000/health"]
              interval: 30s
              timeout: 5s
              retries: 3

        networks:
          corp-internal-network-v2:
            external: true
    """))
    return FaultMetadata(
        fault_type="missing_permission",
        affected_files=["docker-compose.yml"],
        description="docker-compose references non-existent external network 'corp-internal-network-v2'",
    )


def _inject_secret_exposure(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "services", "api", "app.py")
    content = _read(path)
    secret_block = textwrap.dedent('''\
        # Third-party API integration credentials
        API_KEY = "sk-live-4f3c2a1b0e9d8c7f6a5b4e3d2c1a0f9e8d7c6b5a"
        DATABASE_PASSWORD = "super_secret_db_password_2024"
        WEBHOOK_SECRET = "whsec_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"

    ''')
    lines = content.split("\n")
    insert_idx = 0
    for i, line in enumerate(lines):
        if line.startswith(("from ", "import ")):
            insert_idx = i + 1
        elif insert_idx > 0 and not line.strip():
            insert_idx = i + 1
            break
    lines.insert(insert_idx, secret_block)
    _write(path, "\n".join(lines))
    return FaultMetadata(
        fault_type="secret_exposure",
        affected_files=["services/api/app.py"],
        description="Hardcoded API keys and secrets exposed in source code",
    )


def _inject_env_drift(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "docker-compose.yml")
    _write(path, textwrap.dedent("""\
        version: "3.8"

        services:
          api:
            build:
              context: .
              dockerfile: Dockerfile
            ports:
              - "not-a-number:5000"
            environment:
              - FLASK_ENV=production
              - PORT=not-a-number
            healthcheck:
              test: ["CMD", "curl", "-f", "http://localhost:5000/health"]
              interval: 30s
              timeout: 5s
              retries: 3
    """))
    return FaultMetadata(
        fault_type="env_drift",
        affected_files=["docker-compose.yml"],
        description="Invalid runtime env var PORT=not-a-number breaks docker compose deploy port mapping",
    )


def _inject_invalid_database_url(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, ".env")
    content = _write_default_env(path)
    content = _upsert_line(content, "DATABASE_URL", "postgresql://postgres:postgres@db:15432/appdb")
    _write(path, content)
    return FaultMetadata(
        fault_type="invalid_database_url",
        affected_files=[".env"],
        description="DATABASE_URL points at the wrong postgres port, so the app boots but the first DB call fails at runtime",
    )


def _inject_empty_secret_key(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, ".env")
    content = _write_default_env(path)
    content = _upsert_line(content, "SECRET_KEY", "")
    _write(path, content)
    return FaultMetadata(
        fault_type="empty_secret_key",
        affected_files=[".env"],
        description="SECRET_KEY is blank in .env, so request-time session/config access fails after startup",
    )


def _inject_missing_pythonpath(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, ".venv", "runtime.pth")
    _write(path, "/app\n")
    return FaultMetadata(
        fault_type="missing_pythonpath",
        affected_files=[".venv/runtime.pth"],
        description="Virtualenv path bootstrap omits /app/services, so a lazy runtime import fails only when the endpoint is exercised",
    )


def _inject_circular_import_runtime(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "services", "api", "runtime_probe.py")
    _write(path, textwrap.dedent("""\
        \"\"\"Runtime probe helpers.\"\"\"

        FAULT_CIRCULAR_IMPORT_RUNTIME = True

        def load_runtime_probe():
            return "runtime-import-cycle"
    """))
    return FaultMetadata(
        fault_type="circular_import_runtime",
        affected_files=["services/api/runtime_probe.py"],
        description="A lazy request helper introduces a circular import that does not trigger until the runtime probe endpoint executes",
    )


def _inject_missing_package_init(workspace: str) -> FaultMetadata:
    pkg_dir = os.path.join(workspace, "services", "runtime_support")
    os.makedirs(pkg_dir, exist_ok=True)
    helper_path = os.path.join(pkg_dir, "request_context.py")
    _write(helper_path, "def runtime_context():\n    return 'ok'\n")
    init_path = os.path.join(pkg_dir, "__init__.py")
    if os.path.exists(init_path):
        os.remove(init_path)
    return FaultMetadata(
        fault_type="missing_package_init",
        affected_files=["services/runtime_support/__init__.py", "services/runtime_support/request_context.py"],
        description="A runtime-only support package is missing __init__.py, so lazy imports fail during request handling instead of build/install",
    )


def _inject_none_config_runtime(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, ".env")
    content = _write_default_env(path)
    content = _upsert_line(content, "FEATURE_CACHE_BACKEND", "None")
    _write(path, content)
    return FaultMetadata(
        fault_type="none_config_runtime",
        affected_files=[".env"],
        description="A config value resolves to None at runtime and only crashes when the request path dereferences it",
    )


def _inject_log_pii_leak(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "services", "api", "routes.py")
    content = _read(path)
    leak_line = (
        '\n# FAULT(log_pii_leak): credential value logged directly\n'
        '_log.warning(\n'
        '    "Auth token received: sk-live-4f3c2a1b0e9d8c7f6a5b4e3d2c1a0f9e8d7c6b5a",\n'
        ')\n'
    )
    content = content.replace(
        '_log = logging.getLogger("api.routes")',
        '_log = logging.getLogger("api.routes")' + leak_line,
    )
    _write(path, content)
    return FaultMetadata(
        fault_type="log_pii_leak",
        affected_files=["services/api/routes.py"],
        description="routes.py logs a plaintext sk-live- API token — check_logs detects PII in static scan",
    )


def _inject_log_disabled(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "services", "api", "logging_config.py")
    content = _read(path)
    content = content.replace(
        'LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()',
        'LOG_LEVEL: str = "CRITICAL"  # FAULT(log_disabled): hardcoded, overrides env var',
    )
    _write(path, content)
    return FaultMetadata(
        fault_type="log_disabled",
        affected_files=["services/api/logging_config.py"],
        description="LOG_LEVEL hardcoded to CRITICAL — effective logging disabled",
    )


def _inject_bad_migration_sql(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "db", "migrations", "001_init.sql")
    content = _read(path)
    new_content = content.replace(
        "CREATE TABLE IF NOT EXISTS builds",
        "CREAT TABLE IF NOT EXISTS builds",
        1,
    )
    _write(path, new_content)
    return FaultMetadata(
        fault_type="bad_migration_sql",
        affected_files=["db/migrations/001_init.sql"],
        description="SQL syntax error in 001_init.sql: 'CREAT TABLE'",
    )


def _inject_schema_drift(workspace: str) -> FaultMetadata:
    path = os.path.join(workspace, "db", "database.py")
    content = _read(path)
    old = '"id", "task_key", "status", "started_at", "finished_at", "exit_code", "log_tail"'
    new = '"id", "task_key", "status", "started_at", "finished_at", "exit_code", "log_tail", "artifact_url"'
    _write(path, content.replace(old, new, 1))
    return FaultMetadata(
        fault_type="schema_drift",
        affected_files=["db/database.py"],
        description="CANONICAL_COLUMNS includes 'artifact_url' but no migration adds it",
    )


def _inject_terraform_invalid_provider(workspace: str) -> FaultMetadata:
    infra_dir = os.path.join(workspace, "infra")
    os.makedirs(infra_dir, exist_ok=True)
    path = os.path.join(infra_dir, "main.tf")
    _write(path, textwrap.dedent("""\
        terraform {
          required_version = ">= 1.6.0"
        }

        provider "invalidcorp" {}

        resource "invalid_resource" "demo" {}
    """))
    return FaultMetadata(
        fault_type="terraform_invalid_provider",
        affected_files=["infra/main.tf"],
        description="Terraform provider block uses an invalid provider source so init fails.",
    )


def _inject_terraform_missing_variable(workspace: str) -> FaultMetadata:
    infra_dir = os.path.join(workspace, "infra")
    os.makedirs(infra_dir, exist_ok=True)
    main_tf = os.path.join(infra_dir, "main.tf")
    vars_tf = os.path.join(infra_dir, "variables.tf")
    tfvars = os.path.join(infra_dir, "terraform.tfvars")
    _write(main_tf, textwrap.dedent("""\
        provider "aws" {
          region = var.region
        }

        resource "aws_s3_bucket" "artifacts" {
          bucket = "${var.project_name}-artifacts"
        }
    """))
    _write(vars_tf, textwrap.dedent("""\
        variable "region" {
          type = string
        }

        variable "project_name" {
          type = string
        }
    """))
    _write(tfvars, "")
    return FaultMetadata(
        fault_type="terraform_missing_variable",
        affected_files=["infra/main.tf", "infra/variables.tf", "infra/terraform.tfvars"],
        description="Terraform plan lacks required variable values in terraform.tfvars.",
    )


def _inject_terraform_permission_denied(workspace: str) -> FaultMetadata:
    infra_dir = os.path.join(workspace, "infra")
    os.makedirs(infra_dir, exist_ok=True)
    path = os.path.join(infra_dir, "main.tf")
    _write(path, textwrap.dedent("""\
        provider "aws" {
          region = "us-east-1"
        }

        locals {
          simulate_permission_denied = true
        }

        resource "aws_iam_role" "deployer" {
          name = "simulated-deployer-role"
        }
    """))
    return FaultMetadata(
        fault_type="terraform_permission_denied",
        affected_files=["infra/main.tf"],
        description="Terraform apply is configured to simulate IAM AccessDenied errors.",
    )
