"""Simulated fix applier — applies agent fixes to workspace files without git commits.

Mirrors fix_applier.py's apply_fix() API exactly but skips all git operations.
The SimulatedPipelineRunner reads real files, so mutations here are immediately
visible on the next runner.run() call.
"""

from __future__ import annotations

import ast
import json
import os
import re
import textwrap
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class FixResult:
    success: bool
    files_modified: List[str]
    commit_sha: str = ""
    strategy_used: str = ""
    error: str = ""
    description: str = ""


_PATH_SEARCH_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}
_VERSION_PIN_RE = re.compile(r'^([\w\-]+)([>=<!~][^\s,]+)', re.MULTILINE)


def _resolve_workspace_path(workspace: str, rel: str, create_ok: bool = False) -> Optional[str]:
    rel_norm = rel.replace("\\", "/").lstrip("./")
    direct = os.path.join(workspace, rel_norm)
    if os.path.exists(direct):
        return rel_norm
    basename = os.path.basename(rel_norm)
    matches: List[str] = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _PATH_SEARCH_SKIP_DIRS]
        if basename in files:
            matches.append(os.path.relpath(os.path.join(root, basename), workspace).replace("\\", "/"))
            if len(matches) > 1:
                break
    if len(matches) == 1:
        return matches[0]
    if create_ok:
        return rel_norm
    return None


def _resolve_conflict_markers(content: str, keep: str = "head") -> str:
    result, in_conflict, in_theirs = [], False, False
    for line in content.split("\n"):
        if line.startswith("<<<<<<< "):
            in_conflict, in_theirs = True, False
        elif line.startswith("======="):
            in_theirs = True
        elif line.startswith(">>>>>>> "):
            in_conflict, in_theirs = False, False
        elif in_conflict:
            if (keep == "head" and not in_theirs) or (keep == "theirs" and in_theirs):
                result.append(line)
        else:
            result.append(line)
    return "\n".join(result)


def _looks_like_version_conflict(req_content: str) -> bool:
    packages: dict = {}
    for line in req_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r'^([\w\-]+)[>=<!~]', line)
        if m:
            name = m.group(1).lower()
            packages[name] = packages.get(name, 0) + 1
    if any(count > 1 for count in packages.values()):
        return True
    seen_names = set(packages.keys())
    pins = _VERSION_PIN_RE.findall(req_content)
    pin_names = {p[0].lower() for p in pins}
    return bool(pin_names & seen_names and len(pin_names) < len(seen_names))


def _repair_python_syntax(content: str) -> str:
    lines = content.splitlines()
    cleaned = [l for l in lines if not (
        l.startswith("<<<<<<<") or l.startswith("=======") or l.startswith(">>>>>>>")
    )]
    result: List[str] = []
    seen_defs: set = set()
    skip_block = False
    indent_level = 0
    for line in cleaned:
        stripped = line.strip()
        if stripped.startswith(("def ", "class ", "async def ")):
            name = re.split(r'[\s(:]', stripped, maxsplit=2)[1]
            if name in seen_defs:
                skip_block = True
                indent_level = len(line) - len(line.lstrip())
                continue
            seen_defs.add(name)
            skip_block = False
        elif skip_block:
            current_indent = len(line) - len(line.lstrip()) if line.strip() else 999
            if line.strip() and current_indent <= indent_level:
                skip_block = False
            else:
                continue
        result.append(line)
    return "\n".join(result)


def _repair_dockerfile_order(content: str) -> str:
    lines = content.splitlines()
    copy_req_idx = next(
        (i for i, l in enumerate(lines) if "COPY" in l and "requirements" in l), None
    )
    run_install_idx = next(
        (i for i, l in enumerate(lines) if re.search(r"RUN\s+(pip|uv)\s+", l)), None
    )
    if (
        copy_req_idx is not None
        and run_install_idx is not None
        and run_install_idx < copy_req_idx
    ):
        run_line = lines.pop(run_install_idx)
        copy_req_idx = next(
            (i for i, l in enumerate(lines) if "COPY" in l and "requirements" in l), copy_req_idx - 1
        )
        lines.insert(copy_req_idx + 1, run_line)
        return "\n".join(lines)
    return content


# ── Public API ──────────────────────────────────────────────────────────────

def apply_fix_simulated(workspace: str, fix_text: str, target: str = "", fault_type: str = "") -> FixResult:
    """Apply a fix to workspace files (no git). Same strategy order as fix_applier.apply_fix()."""

    # Strategy A: structured JSON patch
    result = _try_structured_fix(workspace, fix_text)
    if result is not None:
        return result

    # Strategy B: fault-type direct dispatch
    if fault_type:
        result = _apply_fault_type_fix(workspace, fault_type)
        if result.success:
            return result

    # Strategy C: heuristic keyword fallback
    result = _apply_heuristic_fix(workspace, fix_text, target)
    if result.success:
        return result

    # Strategy D: generic auto-repair
    return _auto_repair_workspace(workspace, error_hint=fix_text)


# ── Strategy A: Structured JSON ─────────────────────────────────────────────

def _try_structured_fix(workspace: str, fix_text: str) -> Optional[FixResult]:
    json_str = fix_text.strip()
    list_match = re.search(r'\[[^[\]]*\{[^{}]+\}[^[\]]*\]', json_str, re.DOTALL)
    obj_match = re.search(r'\{[^{}]+\}', json_str, re.DOTALL)

    if list_match:
        json_str = list_match.group(0)
    elif obj_match:
        json_str = obj_match.group(0)
    else:
        return None

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        return None

    fixes = [parsed] if isinstance(parsed, dict) else (parsed if isinstance(parsed, list) else None)
    if fixes is None:
        return None

    modified: List[str] = []
    last_obj = {}
    for fix_obj in fixes:
        if not isinstance(fix_obj, dict) or "file" not in fix_obj:
            continue
        last_obj = fix_obj
        action = fix_obj.get("action", "replace")
        resolved_rel = _resolve_workspace_path(workspace, fix_obj["file"], create_ok=(action == "write"))
        if resolved_rel is None:
            continue
        fix_obj["file"] = resolved_rel
        filepath = os.path.join(workspace, resolved_rel)
        try:
            if action == "replace":
                old, new = fix_obj.get("old", ""), fix_obj.get("new", "")
                if not old:
                    continue
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                if old not in content:
                    continue
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content.replace(old, new))
                modified.append(fix_obj["file"])

            elif action == "delete_lines":
                pattern = fix_obj.get("pattern", fix_obj.get("old", ""))
                if not pattern:
                    continue
                with open(filepath, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                with open(filepath, "w", encoding="utf-8") as f:
                    f.writelines(l for l in lines if pattern not in l)
                modified.append(fix_obj["file"])

            elif action == "write":
                content = fix_obj.get("new", fix_obj.get("content", ""))
                parent = os.path.dirname(filepath)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)
                modified.append(fix_obj["file"])

        except OSError as e:
            return FixResult(success=False, files_modified=[], strategy_used="structured_json", error=str(e))

    if not modified:
        return None

    return FixResult(
        success=True,
        files_modified=modified,
        commit_sha="sim-no-git",
        strategy_used="structured_json",
        description=f"Applied structured fix to {', '.join(modified)}",
    )


# ── Strategy B: Fault-type direct dispatch ──────────────────────────────────

_FAULT_FIX_MAP = {
    "merge_conflict":      lambda ws: _fix_merge_conflict(ws),
    "dependency_conflict": lambda ws: _fix_dependency_conflict(ws),
    "docker_order":        lambda ws: _fix_docker_order(ws),
    "flaky_test":          lambda ws: _fix_flaky_test(ws),
    "missing_permission":  lambda ws: _fix_missing_permission(ws),
    "secret_exposure":     lambda ws: _fix_secret_exposure(ws),
    "env_drift":           lambda ws: _fix_env_drift(ws),
    "invalid_database_url": lambda ws: _fix_invalid_database_url(ws),
    "empty_secret_key":     lambda ws: _fix_empty_secret_key(ws),
    "missing_pythonpath":   lambda ws: _fix_missing_pythonpath(ws),
    "circular_import_runtime": lambda ws: _fix_circular_import_runtime(ws),
    "missing_package_init": lambda ws: _fix_missing_package_init(ws),
    "none_config_runtime":  lambda ws: _fix_none_config_runtime(ws),
    "log_pii_leak":        lambda ws: _fix_log_pii_leak(ws),
    "log_disabled":        lambda ws: _fix_log_disabled(ws),
    "bad_migration_sql":   lambda ws: _fix_bad_migration_sql(ws),
    "schema_drift":        lambda ws: _fix_schema_drift(ws),
    "terraform_invalid_provider": lambda ws: _fix_terraform_invalid_provider(ws),
    "terraform_missing_variable": lambda ws: _fix_terraform_missing_variable(ws),
    "terraform_permission_denied": lambda ws: _fix_terraform_permission_denied(ws),
}

_FAULT_FIX_DESCRIPTIONS = {
    "merge_conflict":      "Resolved merge conflict markers",
    "dependency_conflict": "Fixed dependency version pins",
    "docker_order":        "Fixed Dockerfile instruction order",
    "flaky_test":          "Removed flaky timing-sensitive test",
    "missing_permission":  "Fixed docker-compose network configuration",
    "secret_exposure":     "Removed hardcoded secrets",
    "env_drift":           "Fixed docker-compose env/port configuration",
    "invalid_database_url": "Fixed DATABASE_URL in .env",
    "empty_secret_key":     "Restored SECRET_KEY in .env",
    "missing_pythonpath":   "Restored PYTHONPATH bootstrap in virtualenv",
    "circular_import_runtime": "Removed runtime circular import helper",
    "missing_package_init": "Restored missing __init__.py in runtime support package",
    "none_config_runtime":  "Replaced None-valued runtime config with a concrete backend",
    "log_pii_leak":        "Removed PII-leaking log call from routes.py",
    "log_disabled":        "Restored LOG_LEVEL to INFO from CRITICAL",
    "bad_migration_sql":   "Fixed SQL syntax error in migration file",
    "schema_drift":        "Aligned CANONICAL_COLUMNS with database schema",
    "terraform_invalid_provider": "Replaced invalid Terraform provider with supported provider",
    "terraform_missing_variable": "Supplied required Terraform variable values",
    "terraform_permission_denied": "Removed Terraform permission-denied simulation flag",
}


def _apply_fault_type_fix(workspace: str, fault_type: str) -> FixResult:
    fn = _FAULT_FIX_MAP.get(fault_type)
    if fn is None:
        return FixResult(
            success=False, files_modified=[], strategy_used="fault_type_route",
            error=f"No direct fix for fault_type={fault_type!r}",
        )
    modified = fn(workspace)
    if not modified:
        return FixResult(
            success=False, files_modified=[], strategy_used="fault_type_route",
            error=f"Fault-type fix for {fault_type!r} found nothing to change (already fixed?)",
        )
    description = _FAULT_FIX_DESCRIPTIONS.get(fault_type, f"Fixed {fault_type}")
    return FixResult(
        success=True, files_modified=modified, commit_sha="sim-no-git",
        strategy_used="fault_type_route", description=description,
    )


# ── Strategy C: Heuristic keyword dispatch ───────────────────────────────────

def _apply_heuristic_fix(workspace: str, fix_text: str, target: str = "") -> FixResult:
    fix_lower = fix_text.lower()
    modified: List[str] = []
    description = ""

    if any(kw in fix_lower for kw in ["merge conflict", "conflict marker", "resolve conflict"]):
        modified = _fix_merge_conflict(workspace)
        description = "Resolved merge conflict markers"
    elif any(kw in fix_lower for kw in ["pin", "dependency", "requests", "urllib3", "compatible", "version"]):
        modified = _fix_dependency_conflict(workspace)
        description = "Fixed dependency version pins"
    elif any(kw in fix_lower for kw in ["flaky", "retry", "timing", "intermittent"]):
        modified = _fix_flaky_test(workspace)
        description = "Removed flaky timing-sensitive test"
    elif any(kw in fix_lower for kw in ["pii", "log call", "token_in_log", "sk-live", "credential"]):
        modified = _fix_log_pii_leak(workspace)
        description = "Removed PII-leaking log call"
    elif any(kw in fix_lower for kw in ["critical", "log_level", "log_disabled", "silenced"]):
        modified = _fix_log_disabled(workspace)
        description = "Restored LOG_LEVEL to INFO"
    elif any(kw in fix_lower for kw in ["creat table", "migration", "sql syntax"]):
        modified = _fix_bad_migration_sql(workspace)
        description = "Fixed SQL syntax error"
    elif any(kw in fix_lower for kw in ["artifact_url", "schema_drift", "canonical_columns"]):
        modified = _fix_schema_drift(workspace)
        description = "Aligned CANONICAL_COLUMNS"
    elif any(kw in fix_lower for kw in ["permission", "network", "external", "compose"]):
        modified = _fix_missing_permission(workspace)
        description = "Fixed docker-compose network configuration"
    elif any(kw in fix_lower for kw in ["dockerfile", "reorder", "install order", "layer"]):
        modified = _fix_docker_order(workspace)
        description = "Fixed Dockerfile instruction order"
    elif any(kw in fix_lower for kw in ["secret", "credential", "api_key", "hardcoded"]):
        modified = _fix_secret_exposure(workspace)
        description = "Removed hardcoded secrets"
    elif any(kw in fix_lower for kw in ["database_url", "wrong port", "db url", ".env"]):
        modified = _fix_invalid_database_url(workspace)
        description = "Fixed DATABASE_URL in .env"
    elif any(kw in fix_lower for kw in ["secret_key", "empty secret", "blank secret"]):
        modified = _fix_empty_secret_key(workspace)
        description = "Restored SECRET_KEY"
    elif any(kw in fix_lower for kw in ["pythonpath", "runtime.pth", "venv path"]):
        modified = _fix_missing_pythonpath(workspace)
        description = "Restored virtualenv PYTHONPATH"
    elif any(kw in fix_lower for kw in ["circular import", "runtime probe", "lazy import"]):
        modified = _fix_circular_import_runtime(workspace)
        description = "Removed circular import from runtime probe"
    elif any(kw in fix_lower for kw in ["__init__", "package init", "missing package"]):
        modified = _fix_missing_package_init(workspace)
        description = "Restored package __init__.py"
    elif any(kw in fix_lower for kw in ["none config", "feature_cache_backend", "none runtime"]):
        modified = _fix_none_config_runtime(workspace)
        description = "Restored concrete runtime config"
    elif any(kw in fix_lower for kw in ["terraform provider", "invalid provider", "registry.terraform"]):
        modified = _fix_terraform_invalid_provider(workspace)
        description = "Replaced invalid Terraform provider"
    elif any(kw in fix_lower for kw in ["terraform variable", "tfvars", "required variable"]):
        modified = _fix_terraform_missing_variable(workspace)
        description = "Added required Terraform variable values"
    elif any(kw in fix_lower for kw in ["accessdenied", "permission denied", "iam", "terraform apply"]):
        modified = _fix_terraform_permission_denied(workspace)
        description = "Removed Terraform apply permission blocker"

    if not modified:
        return FixResult(
            success=False, files_modified=[], strategy_used="heuristic",
            error="No heuristic keyword matched; will try auto-repair",
        )
    return FixResult(
        success=True, files_modified=modified, commit_sha="sim-no-git",
        strategy_used="heuristic", description=description,
    )


# ── Strategy D: Generic auto-repair ─────────────────────────────────────────

_SOURCE_FILES = [
    ".env",
    "services/api/routes.py",
    "services/api/app.py",
    "services/api/logging_config.py",
    "services/api/runtime_probe.py",
    "services/api/requirements.txt",
    "services/runtime_support/__init__.py",
    "Dockerfile",
    "docker-compose.yml",
    "tests/test_api.py",
]

_HARDCODED_SECRETS_RE = re.compile(
    r'(?:API_KEY|SECRET_KEY|DATABASE_PASSWORD|WEBHOOK_SECRET|ACCESS_TOKEN|PRIVATE_KEY)\s*=\s*["\'][^"\']{4,}["\']',
    re.IGNORECASE,
)
_CONFLICT_START = re.compile(r'^<{7} ', re.MULTILINE)


def _auto_repair_workspace(workspace: str, error_hint: str = "") -> FixResult:
    modified: List[str] = []

    for rel_path in _SOURCE_FILES:
        full_path = os.path.join(workspace, rel_path)
        if not os.path.exists(full_path):
            continue
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                original = f.read()
        except OSError:
            continue

        content = original

        if _CONFLICT_START.search(content):
            content = _resolve_conflict_markers(content)

        if rel_path.endswith(".py") and content == original:
            try:
                ast.parse(content)
            except SyntaxError:
                content = _repair_python_syntax(content)

        if rel_path.endswith(".py") and content == original:
            cleaned = _HARDCODED_SECRETS_RE.sub(
                lambda m: m.group(0).split("=")[0] + '= os.environ.get("' +
                          m.group(0).split("=")[0].strip() + '", "")',
                content,
            )
            if cleaned != content:
                if "import os" not in cleaned:
                    cleaned = "import os\n" + cleaned
                content = cleaned

        if content != original:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            modified.append(rel_path)

    if not modified:
        modified.extend(_fix_log_pii_leak(workspace))
    if not modified:
        modified.extend(_fix_log_disabled(workspace))

    req_path = os.path.join(workspace, "services/api/requirements.txt")
    if not modified and os.path.exists(req_path):
        with open(req_path, "r", encoding="utf-8") as f:
            req_content = f.read()
        if _looks_like_version_conflict(req_content):
            with open(req_path, "w", encoding="utf-8") as f:
                f.write("flask>=3.0.0\nrequests>=2.31.0\nurllib3>=2.0.0\ngunicorn>=21.2.0\npytest>=8.0.0\n")
            modified.append("services/api/requirements.txt")

    df_path = os.path.join(workspace, "Dockerfile")
    if not modified and os.path.exists(df_path):
        with open(df_path, "r", encoding="utf-8") as f:
            df_content = f.read()
        fixed_df = _repair_dockerfile_order(df_content)
        if fixed_df != df_content:
            with open(df_path, "w", encoding="utf-8") as f:
                f.write(fixed_df)
            modified.append("Dockerfile")

    dc_path = os.path.join(workspace, "docker-compose.yml")
    if not modified and os.path.exists(dc_path):
        with open(dc_path, "r", encoding="utf-8") as f:
            dc_content = f.read()
        fixed_dc = _repair_docker_compose(dc_content)
        if fixed_dc != dc_content:
            with open(dc_path, "w", encoding="utf-8") as f:
                f.write(fixed_dc)
            modified.append("docker-compose.yml")

    if not modified:
        return FixResult(
            success=False, files_modified=[], strategy_used="auto_repair",
            error="Auto-repair found nothing to fix; structured JSON fix required",
        )
    return FixResult(
        success=True, files_modified=modified, commit_sha="sim-no-git",
        strategy_used="auto_repair",
        description=f"Auto-repaired {', '.join(modified)}",
    )


def _repair_docker_compose(content: str) -> str:
    """Remove external network references and malformed port specs."""
    fixed = re.sub(r'\n\s*networks:\s*\n(?:\s+\S[^\n]*\n)+', '\n', content)
    fixed = re.sub(r'- "not-a-number:\d+"', '- "5000:5000"', fixed)
    fixed = re.sub(r'external: true\n?', '', fixed)
    return fixed


def _upsert_env_value(content: str, key: str, value: str) -> str:
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


# ── Per-fault fix implementations (no git) ───────────────────────────────────

def _fix_merge_conflict(workspace: str) -> List[str]:
    modified = []
    for root, _dirs, files in os.walk(workspace):
        if ".git" in root:
            continue
        for fname in files:
            if not fname.endswith((".py", ".yml", ".yaml", ".json", ".txt")):
                continue
            filepath = os.path.join(root, fname)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                if "<<<<<<< " not in content:
                    continue
                cleaned = _resolve_conflict_markers(content)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(cleaned)
                modified.append(os.path.relpath(filepath, workspace).replace("\\", "/"))
            except OSError:
                continue
    return modified


def _fix_dependency_conflict(workspace: str) -> List[str]:
    path = os.path.join(workspace, "services", "api", "requirements.txt")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    # The injected fault pins requests==2.28.0 + urllib3==2.0.7 which are incompatible.
    # Fix: upgrade requests to >=2.31.0 (which requires urllib3>=2) and use urllib3>=2.0.0.
    # The simulated partial-fix check looks for urllib3 pinned as <2 for requests==2.28 style;
    # but since we upgrade requests too, urllib3>=2.0.0 is correct and the check passes
    # via _no_version_drift() returning True.
    with open(path, "w", encoding="utf-8") as f:
        f.write("flask>=3.0.0\nrequests>=2.31.0\nurllib3>=2.0.0\ngunicorn>=21.2.0\npytest>=8.0.0\n")
    return ["services/api/requirements.txt"]


def _fix_docker_order(workspace: str) -> List[str]:
    path = os.path.join(workspace, "Dockerfile")
    if not os.path.exists(path):
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent("""\
            FROM python:3.11-slim

            WORKDIR /app

            COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

            COPY services/api/requirements.txt /app/requirements.txt
            RUN uv pip install --system --no-cache -r requirements.txt

            COPY . /app/

            EXPOSE 5000

            CMD ["python", "-m", "services.api.app"]
        """))
    return ["Dockerfile"]


def _fix_flaky_test(workspace: str) -> List[str]:
    path = os.path.join(workspace, "tests", "test_api.py")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    cleaned = re.sub(r'\ndef test_response_time_health\(.*?\n(?=\ndef |\Z)', '\n', content, flags=re.DOTALL)
    if cleaned == content:
        lines, out, skip = content.split("\n"), [], False
        for line in lines:
            if "def test_response_time" in line:
                skip = True
                continue
            if skip and line.strip() and not line[0].isspace():
                skip = False
            if not skip:
                out.append(line)
        cleaned = "\n".join(out)
    with open(path, "w", encoding="utf-8") as f:
        f.write(cleaned)
    return ["tests/test_api.py"]


def _fix_missing_permission(workspace: str) -> List[str]:
    path = os.path.join(workspace, "docker-compose.yml")
    if not os.path.exists(path):
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent("""\
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
                volumes:
                  - ./logs:/app/logs
                healthcheck:
                  test: ["CMD", "curl", "-f", "http://localhost:5000/health"]
                  interval: 30s
                  timeout: 5s
                  retries: 3
        """))
    return ["docker-compose.yml"]


def _fix_env_drift(workspace: str) -> List[str]:
    path = os.path.join(workspace, "docker-compose.yml")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    fixed = re.sub(r'- "not-a-number:\d+"', '- "5000:5000"', content)
    fixed = re.sub(r'- PORT=not-a-number\n?', '- PORT=5000\n', fixed)
    if fixed == content:
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(fixed)
    return ["docker-compose.yml"]


def _fix_invalid_database_url(workspace: str) -> List[str]:
    path = os.path.join(workspace, ".env")
    content = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    fixed = _upsert_env_value(content, "DATABASE_URL", "postgresql://postgres:postgres@db:5432/appdb")
    if fixed == content:
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(fixed)
    return [".env"]


def _fix_empty_secret_key(workspace: str) -> List[str]:
    path = os.path.join(workspace, ".env")
    content = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    fixed = _upsert_env_value(content, "SECRET_KEY", "dev-secret-key")
    if fixed == content:
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(fixed)
    return [".env"]


def _fix_missing_pythonpath(workspace: str) -> List[str]:
    path = os.path.join(workspace, ".venv", "runtime.pth")
    desired = "/app\n/app/services\n"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    current = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            current = f.read()
    if current == desired:
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(desired)
    return [".venv/runtime.pth"]


def _fix_circular_import_runtime(workspace: str) -> List[str]:
    path = os.path.join(workspace, "services", "api", "runtime_probe.py")
    if not os.path.exists(path):
        return []
    fixed = '"""Runtime probe helpers."""\n\n\ndef load_runtime_probe():\n    return "runtime-ok"\n'
    with open(path, "r", encoding="utf-8") as f:
        current = f.read()
    if current == fixed:
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(fixed)
    return ["services/api/runtime_probe.py"]


def _fix_missing_package_init(workspace: str) -> List[str]:
    path = os.path.join(workspace, "services", "runtime_support", "__init__.py")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = "from .request_context import runtime_context\n"
    current = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            current = f.read()
    if current == content:
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return ["services/runtime_support/__init__.py"]


def _fix_none_config_runtime(workspace: str) -> List[str]:
    path = os.path.join(workspace, ".env")
    content = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    fixed = _upsert_env_value(content, "FEATURE_CACHE_BACKEND", "redis")
    if fixed == content:
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(fixed)
    return [".env"]


def _fix_secret_exposure(workspace: str) -> List[str]:
    patterns = [
        re.compile(r'^.*API_KEY\s*=\s*"[^"]*".*$', re.MULTILINE),
        re.compile(r'^.*DATABASE_PASSWORD\s*=\s*"[^"]*".*$', re.MULTILINE),
        re.compile(r'^.*WEBHOOK_SECRET\s*=\s*"[^"]*".*$', re.MULTILINE),
        re.compile(r'^.*SECRET_KEY\s*=\s*"[^"]*".*$', re.MULTILINE),
        re.compile(r'^# Third-party API integration credentials\s*$', re.MULTILINE),
    ]
    modified = []
    for root, _dirs, files in os.walk(workspace):
        if ".git" in root:
            continue
        for fname in files:
            if not fname.endswith(".py"):
                continue
            filepath = os.path.join(root, fname)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    original = f.read()
                content = original
                for pat in patterns:
                    content = pat.sub("", content)
                content = re.sub(r'\n{3,}', '\n\n', content)
                if content != original:
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(content)
                    modified.append(os.path.relpath(filepath, workspace).replace("\\", "/"))
            except OSError:
                continue
    return modified


def _fix_log_pii_leak(workspace: str) -> List[str]:
    path = os.path.join(workspace, "services", "api", "routes.py")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    cleaned = re.sub(
        r'\n# FAULT\(log_pii_leak\)[^\n]*\n_log\.\w+\s*\(\n[^)]*\),?\n\)\n',
        '\n', content,
    )
    cleaned = re.sub(
        r'_log\.\w+\s*\(\s*\n?\s*["\'][^"\']*(?:sk-live|sk-test|AKIA)[^"\']*["\']\s*,?\s*\n?\s*\)',
        '', cleaned,
    )
    if cleaned == content:
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(cleaned)
    return ["services/api/routes.py"]


def _fix_log_disabled(workspace: str) -> List[str]:
    path = os.path.join(workspace, "services", "api", "logging_config.py")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    cleaned = re.sub(
        r'LOG_LEVEL\s*:\s*str\s*=\s*["\']CRITICAL["\'].*#.*FAULT\(log_disabled\)[^\n]*',
        'LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()',
        content,
    )
    if cleaned == content:
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(cleaned)
    return ["services/api/logging_config.py"]


def _fix_bad_migration_sql(workspace: str) -> List[str]:
    path = os.path.join(workspace, "db", "migrations", "001_init.sql")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    cleaned = content.replace("CREAT TABLE IF NOT EXISTS builds", "CREATE TABLE IF NOT EXISTS builds", 1)
    if cleaned == content:
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(cleaned)
    return ["db/migrations/001_init.sql"]


def _fix_schema_drift(workspace: str) -> List[str]:
    path = os.path.join(workspace, "db", "database.py")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    cleaned = content.replace(
        '"id", "task_key", "status", "started_at", "finished_at", "exit_code", "log_tail", "artifact_url"',
        '"id", "task_key", "status", "started_at", "finished_at", "exit_code", "log_tail"',
        1,
    )
    if cleaned == content:
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(cleaned)
    return ["db/database.py"]


def _fix_terraform_invalid_provider(workspace: str) -> List[str]:
    path = os.path.join(workspace, "infra", "main.tf")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    fixed = content.replace('provider "invalidcorp" {}', 'provider "aws" {\n  region = "us-east-1"\n}')
    fixed = fixed.replace('resource "invalid_resource" "demo" {}', 'resource "aws_s3_bucket" "demo" {\n  bucket = "sim-demo-artifacts"\n}')
    if fixed == content:
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(fixed)
    return ["infra/main.tf"]


def _fix_terraform_missing_variable(workspace: str) -> List[str]:
    path = os.path.join(workspace, "infra", "terraform.tfvars")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = 'region = "us-east-1"\nproject_name = "sample-app"\n'
    current = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            current = f.read()
    if current == content:
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return ["infra/terraform.tfvars"]


def _fix_terraform_permission_denied(workspace: str) -> List[str]:
    path = os.path.join(workspace, "infra", "main.tf")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    fixed = re.sub(r"simulate_permission_denied\s*=\s*true", "simulate_permission_denied = false", content, flags=re.IGNORECASE)
    if fixed == content:
        return []
    with open(path, "w", encoding="utf-8") as f:
        f.write(fixed)
    return ["infra/main.tf"]


