"""Simulated CI/CD pipeline runner for Hugging Face Spaces deployment.

Replaces Docker/subprocess-based execution with high-fidelity simulation that:
- Produces realistic logs matching real tool output (git, docker, uv pip, pytest, compose)
- Honors all 20 fault types with correct stage failures and error messages
- Detects fixes by inspecting actual workspace files (with partial fix detection)
- Validates Python syntax via real AST parsing (not pattern matching)
- Validates SQL syntax via real token-level parsing
- Replicates the real secret scan logic (walks files, same patterns as pipeline_runner.py)
- Replicates check_logs.py static validation (LOG_LEVEL, RotatingFileHandler, PII scan)
- Simulates health-check probing for deploy-stage verification
- Simulates multi-fault cascading: all active faults surface at their own stages
- Partial-fix detection: scores fix completeness and emits targeted warnings when incomplete
- Clone log reflects actual workspace git state (real SHA + commit message)
- Stage durations are failure-mode-aware (fast for early errors, full range for success)
- stage.status is always a _StatusWrapper so .value never raises AttributeError
- Runs entirely in pure Python (no Docker, no subprocess, no privileged operations)
- Maintains exact API compatibility with RealPipelineRunner

Target: Hugging Face Spaces CPU environment (no Docker-in-Docker support)
"""

from __future__ import annotations

import ast
import hashlib
import os
import random
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple, TYPE_CHECKING
from cicd.terraform_simulator import has_terraform_config, simulate_terraform_pipeline

if TYPE_CHECKING:
    from models import AdversarialCICDScenario


# ── Stage and Pipeline Status Enums (must match pipeline_runner.py) ────────

class StageStatus:
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PipelineStatus:
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


STAGE_ORDER = ["clone", "build", "test", "deploy"]


# ── Stage Weights for Pipeline Health ──────────────────────────────────────

STAGE_WEIGHTS = {
    "clone": 0.10,
    "build": 0.30,
    "test": 0.30,
    "deploy": 0.30,
}


# ── Fault → Stage Mapping ──────────────────────────────────────────────────

FAULT_STAGE_MAP: Dict[str, str] = {
    "merge_conflict":      "build",
    "dependency_conflict": "build",
    "docker_order":        "build",
    "flaky_test":          "test",
    "missing_permission":  "deploy",
    "secret_exposure":     "build",
    "env_drift":           "deploy",
    "invalid_database_url": "deploy",
    "empty_secret_key":     "deploy",
    "missing_pythonpath":   "deploy",
    "circular_import_runtime": "deploy",
    "missing_package_init": "deploy",
    "none_config_runtime":  "deploy",
    "log_pii_leak":        "build",
    "log_disabled":        "build",
    "bad_migration_sql":   "build",
    "schema_drift":        "deploy",
    "terraform_invalid_provider": "deploy",
    "terraform_missing_variable": "deploy",
    "terraform_permission_denied": "deploy",
}


# ── _StatusWrapper ─────────────────────────────────────────────────────────

class _StatusWrapper:
    """Wraps a status string to provide a .value attribute (matches str-Enum API)."""

    def __init__(self, status: str):
        self._status = status
        self.value = status

    def __str__(self):
        return self._status

    def __repr__(self):
        return f"_StatusWrapper({self._status!r})"

    def __eq__(self, other):
        if isinstance(other, _StatusWrapper):
            return self._status == other._status
        return self._status == other

    def __hash__(self):
        return hash(self._status)


def _sw(status: str) -> _StatusWrapper:
    return _StatusWrapper(status)


# ── File helpers ───────────────────────────────────────────────────────────

def _read_file_safe(workspace: str, rel_path: str) -> str:
    try:
        with open(os.path.join(workspace, rel_path), "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except (OSError, FileNotFoundError):
        return ""


def _parse_env_file(workspace: str, rel_path: str = ".env") -> Dict[str, str]:
    content = _read_file_safe(workspace, rel_path)
    values: Dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _find_python_files(workspace: str, subdir: str = "") -> List[str]:
    """Walk a subdirectory and return relative paths of all .py files."""
    root = os.path.join(workspace, subdir) if subdir else workspace
    results: List[str] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", ".venv")]
            for fname in filenames:
                if fname.endswith(".py"):
                    full = os.path.join(dirpath, fname)
                    results.append(os.path.relpath(full, workspace).replace("\\", "/"))
    except OSError:
        pass
    return results


# ── Real Syntax / Semantic Validators ─────────────────────────────────────

def _validate_python_syntax(workspace: str, rel_path: str) -> Tuple[bool, str]:
    """Parse a file with the real CPython AST. Returns (ok, error_message)."""
    content = _read_file_safe(workspace, rel_path)
    if not content:
        return True, ""
    try:
        ast.parse(content, filename=rel_path)
        return True, ""
    except SyntaxError as exc:
        return False, (
            f'  File "/app/{rel_path}", line {exc.lineno}\n'
            f"    {exc.text or ''}\n"
            f"    {'':>{max((exc.offset or 1) - 1, 0)}}^\n"
            f"SyntaxError: {exc.msg}"
        )


def _validate_sql_tokens(workspace: str, rel_path: str) -> Tuple[bool, str]:
    """Token-level SQL keyword check for common typos."""
    content = _read_file_safe(workspace, rel_path)
    if not content:
        return True, ""
    bad_keywords = {"CREAT", "INSER", "SELEC", "UPDAT", "DELET", "DROPT", "ALTERR"}
    for lineno, raw_line in enumerate(content.splitlines(), 1):
        for word in re.split(r"\s+", raw_line.strip()):
            token = word.upper().rstrip("(")
            if token in bad_keywords:
                col = raw_line.index(word) if word in raw_line else 0
                return False, (
                    f'psycopg2.errors.SyntaxError: syntax error at or near "{word}"\n'
                    f"LINE {lineno}: {raw_line.strip()}\n"
                    f"        {'':>{col}}^\n"
                    f"DETAIL:  Expected {token[:-1] if token.endswith('T') else token}, found {word}\n"
                    f"CONTEXT:  SQL statement in migration file: {rel_path}:{lineno}\n"
                    f"ERROR: Database migration failed during build"
                )
    return True, ""


def _simulate_health_check(host: str = "localhost", port: int = 5000, path: str = "/health") -> Tuple[bool, str]:
    """Probe TCP reachability as a stand-in for HTTP health-check."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        connected = sock.connect_ex((host, port)) == 0
        if connected:
            return True, f"Health check: GET http://{host}:{port}{path} -> 200 OK"
        return True, f"Health check: simulated GET http://{host}:{port}{path} -> 200 OK (no real server)"
    except OSError:
        return True, f"Health check: simulated GET http://{host}:{port}{path} -> 200 OK (no real server)"
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ── Real Secret Scan (mirrors pipeline_runner.py _secret_scan exactly) ────

_TOKEN_PATTERNS = [
    "sk-live-", "sk-test-", "sk_live_", "sk_test_",
    "AKIA", "ghp_", "gho_", "github_pat_",
]
_ASSIGN_PATTERNS = [
    re.compile(r"API_KEY\s*=\s*['\"]"),
    re.compile(r"SECRET_KEY\s*=\s*['\"]"),
    re.compile(r"PASSWORD\s*=\s*['\"]"),
]
_SCAN_EXTENSIONS = {".py", ".yml", ".yaml", ".json", ".env", ".cfg"}


def _run_secret_scan(workspace: str) -> Tuple[int, str, str]:
    """Walk workspace files and detect hardcoded secrets (same logic as real runner)."""
    secrets_found: List[str] = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in (".git", ".venv", "__pycache__")]
        rel_root = os.path.relpath(root, workspace).replace("\\", "/")
        # Skip scripts/ (contains scanner patterns as literals) and .github/ (CI yml grep commands)
        if rel_root.startswith("scripts") or rel_root.startswith(".github"):
            continue
        for fname in files:
            if os.path.splitext(fname)[1] not in _SCAN_EXTENSIONS:
                continue
            filepath = os.path.join(root, fname)
            rel = os.path.relpath(filepath, workspace).replace("\\", "/")
            try:
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                for i, line in enumerate(content.splitlines(), 1):
                    if any(p in line for p in _TOKEN_PATTERNS):
                        secrets_found.append(f"{rel}:{i}: {line.strip()}")
                if fname.endswith(".py"):
                    for pat in _ASSIGN_PATTERNS:
                        for m in pat.finditer(content):
                            line_num = content[: m.start()].count("\n") + 1
                            line = content.splitlines()[line_num - 1].strip()
                            entry = f"{rel}:{line_num}: {line}"
                            if entry not in secrets_found:
                                secrets_found.append(entry)
            except OSError:
                continue

    if secrets_found:
        stderr = "[SECURITY GATE] Secret scan FAILED\nHardcoded secrets detected:\n"
        stderr += "".join(f"  ERROR: {f}\n" for f in secrets_found)
        stderr += "\nPolicy check failed: plaintext credential found in source code.\nBuild blocked: secret exposure policy violation"
        return 1, "", stderr
    return 0, "Secret scan passed: no hardcoded secrets detected.\n", ""


# ── Real Log Config Check (mirrors check_logs.py validate_config) ─────────

_SOURCE_PII_PATTERNS = [
    ("token_in_log_call",
     re.compile(r'_log\.\w+\s*\([^)]*(?:sk-live|sk-test|AKIA|Bearer\s+sk)[^)]*\)', re.DOTALL)),
    ("password_in_log_call",
     re.compile(r'_log\.\w+\s*\([^)]*(?i:password|passwd|secret)[^)]*[=:]\s*["\'][^"\']{6,}["\'][^)]*\)', re.DOTALL)),
]
_SILENCING_LEVELS = {"CRITICAL"}
_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "WARN", "ERROR", "CRITICAL"}


def _run_log_config_check(workspace: str) -> Tuple[int, str, str]:
    """Replicate check_logs.py --config-only inside the simulated build."""
    config_rel = "services/api/logging_config.py"
    routes_rel = "services/api/routes.py"

    config_src = _read_file_safe(workspace, config_rel)
    if not config_src:
        return 0, "Log config check skipped: scripts/check_logs.py not in workspace.\n", ""

    failures: List[str] = []

    # Parseable Python
    try:
        tree = ast.parse(config_src)
    except SyntaxError as exc:
        failures.append(f"SyntaxError in logging_config.py: {exc}")
        return _log_check_result(failures, config_rel)

    # Required variables
    assigned: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    assigned.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigned.add(node.target.id)
    for var in ("LOG_PATH", "LOG_LEVEL", "MAX_BYTES", "BACKUP_COUNT"):
        if var not in assigned:
            failures.append(f"logging_config.py is missing required variable: {var}")

    # RotatingFileHandler
    if "RotatingFileHandler(" not in config_src:
        failures.append(
            "logging_config.py does not use RotatingFileHandler — "
            "logs may grow unboundedly without rotation"
        )

    # LOG_LEVEL not silencing
    level_m = re.search(r'LOG_LEVEL\s*(?::\s*\w+)?\s*=\s*["\']([A-Z]+)["\']', config_src)
    if level_m:
        level = level_m.group(1)
        if level in _SILENCING_LEVELS:
            failures.append(
                f"LOG_LEVEL is hardcoded to {level!r} — "
                "this silences all log output below CRITICAL"
            )
        elif level not in _VALID_LEVELS:
            failures.append(f"LOG_LEVEL {level!r} is not a recognised logging level")

    # LOG_PATH not restricted
    path_m = re.search(r'LOG_PATH\s*(?::\s*\w+)?\s*=\s*["\']([^"\']+)["\']', config_src)
    if path_m:
        declared = path_m.group(1)
        if declared.startswith(("/var/log", "/root", "/sys", "/proc")):
            failures.append(
                f"LOG_PATH default {declared!r} points to a restricted system directory "
                "— the application process cannot write there"
            )

    # JSON formatter
    if "json.dumps" not in config_src:
        failures.append(
            "logging_config.py formatter does not call json.dumps() — "
            "log records will not be valid JSON"
        )

    # Required JSON fields
    for f_name in ("timestamp", "level", "message", "service"):
        if f'"{f_name}"' not in config_src and f"'{f_name}'" not in config_src:
            failures.append(
                f"JSON formatter appears to be missing required field: {f_name!r} — "
                "structured log records will be incomplete"
            )

    # Static PII scan of config
    for label, pattern in _SOURCE_PII_PATTERNS:
        if pattern.search(config_src):
            failures.append(
                f"logging_config.py contains a log call that may emit credential values "
                f"[pattern: {label}]"
            )

    # Static PII scan of routes.py
    routes_src = _read_file_safe(workspace, routes_rel)
    if routes_src:
        for label, pattern in _SOURCE_PII_PATTERNS:
            if pattern.search(routes_src):
                failures.append(
                    f"routes.py contains a log call that may emit credential values "
                    f"[pattern: {label}] — PII leak risk"
                )

    return _log_check_result(failures, config_rel)


def _log_check_result(failures: List[str], config_rel: str) -> Tuple[int, str, str]:
    if failures:
        stderr = "LOG CONFIG CHECK FAILED\n"
        for f in failures:
            stderr += f"  FAIL: {f}\n"
        stderr += "\nBuild blocked: logging configuration violates observability requirements"
        return 1, "", stderr
    return 0, "Log config check passed: logging configuration is valid.\n", ""


# ── Partial Fix Detection ──────────────────────────────────────────────────

PARTIAL_FIX_CHECKS: Dict[str, List[Tuple[str, Callable[[str], bool]]]] = {
    "merge_conflict": [
        ("conflict markers removed from routes.py",
         lambda ws: "<<<<<<" not in _read_file_safe(ws, "services/api/routes.py")),
        ("routes.py has valid Python syntax",
         lambda ws: _validate_python_syntax(ws, "services/api/routes.py")[0]),
    ],
    "dependency_conflict": [
        ("urllib3==2.0 exact pin removed from requirements.txt",
         lambda ws: "urllib3==2.0" not in _read_file_safe(ws, "services/api/requirements.txt")),
        ("requests and urllib3 versions are mutually compatible",
         lambda ws: _no_version_drift(ws)),
    ],
    "docker_order": [
        ("COPY requirements.txt precedes RUN uv pip install",
         lambda ws: _dockerfile_copy_before_run(ws)),
    ],
    "flaky_test": [
        ("test_response_time test removed or threshold relaxed to >= 0.1s",
         lambda ws: _flaky_test_fixed(ws)),
    ],
    "missing_permission": [
        ("external: true removed from docker-compose.yml",
         lambda ws: "external: true" not in _read_file_safe(ws, "docker-compose.yml")),
    ],
    "secret_exposure": [
        ("secret scan passes (no hardcoded secrets found)",
         lambda ws: _run_secret_scan(ws)[0] == 0),
    ],
    "env_drift": [
        ("not-a-number port removed from docker-compose.yml",
         lambda ws: "not-a-number" not in _read_file_safe(ws, "docker-compose.yml")),
    ],
    "invalid_database_url": [
        ("DATABASE_URL points at the healthy db port",
         lambda ws: ":15432/" not in _parse_env_file(ws).get("DATABASE_URL", "")),
    ],
    "empty_secret_key": [
        ("SECRET_KEY is non-empty in .env",
         lambda ws: bool(_parse_env_file(ws).get("SECRET_KEY", "").strip())),
    ],
    "missing_pythonpath": [
        ("virtualenv runtime.pth includes /app/services",
         lambda ws: "/app/services" in _read_file_safe(ws, ".venv/runtime.pth")),
    ],
    "circular_import_runtime": [
        ("runtime probe helper no longer contains the circular import marker",
         lambda ws: "FAULT_CIRCULAR_IMPORT_RUNTIME" not in _read_file_safe(ws, "services/api/runtime_probe.py")),
    ],
    "missing_package_init": [
        ("runtime support package has __init__.py restored",
         lambda ws: bool(_read_file_safe(ws, "services/runtime_support/__init__.py").strip())),
    ],
    "none_config_runtime": [
        ("FEATURE_CACHE_BACKEND no longer resolves to None",
         lambda ws: _parse_env_file(ws).get("FEATURE_CACHE_BACKEND", "").strip().lower() not in {"", "none"}),
    ],
    "log_pii_leak": [
        ("log config check passes (no PII in log calls)",
         lambda ws: _run_log_config_check(ws)[0] == 0),
    ],
    "log_disabled": [
        ("log config check passes (LOG_LEVEL not CRITICAL)",
         lambda ws: _run_log_config_check(ws)[0] == 0),
    ],
    "bad_migration_sql": [
        ("CREAT TABLE typo corrected in 001_init.sql",
         lambda ws: "CREAT TABLE" not in _read_file_safe(ws, "db/migrations/001_init.sql")),
        ("SQL tokens valid",
         lambda ws: _validate_sql_tokens(ws, "db/migrations/001_init.sql")[0]),
    ],
    "schema_drift": [
        ("artifact_url removed from database.py CANONICAL_COLUMNS",
         lambda ws: "artifact_url" not in _read_file_safe(ws, "db/database.py")),
    ],
    "terraform_invalid_provider": [
        ("provider block no longer references invalidcorp",
         lambda ws: 'provider "invalidcorp"' not in _read_file_safe(ws, "infra/main.tf")),
    ],
    "terraform_missing_variable": [
        ("required tf variable values supplied in infra/terraform.tfvars",
         lambda ws: bool(_read_file_safe(ws, "infra/terraform.tfvars").strip())),
    ],
    "terraform_permission_denied": [
        ("permission denial simulation flag removed",
         lambda ws: "simulate_permission_denied" not in _read_file_safe(ws, "infra/main.tf").lower()),
    ],
}


def _dockerfile_copy_before_run(workspace: str) -> bool:
    content = _read_file_safe(workspace, "Dockerfile")
    if not content:
        return False
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    copy_idx, run_idx = -1, -1
    for i, line in enumerate(lines):
        if "COPY" in line and "requirements.txt" in line:
            copy_idx = i
        if "RUN" in line and ("pip install" in line or "uv pip install" in line):
            run_idx = i
    return copy_idx != -1 and run_idx != -1 and copy_idx < run_idx


def _flaky_test_fixed(workspace: str) -> bool:
    """True if the flaky timing test has been removed OR its threshold raised to >= 0.1s."""
    content = _read_file_safe(workspace, "tests/test_api.py")
    if "test_response_time" not in content:
        return True
    m = re.search(r"threshold\s*=\s*([0-9]*\.?[0-9]+)", content)
    return bool(m) and float(m.group(1)) >= 0.1


def _no_duplicate_ports(workspace: str) -> bool:
    content = _read_file_safe(workspace, "docker-compose.yml")
    ports = re.findall(r'"\s*(\d+):\d+"', content)
    return len(ports) == len(set(ports))


def _no_version_drift(workspace: str) -> bool:
    content = _read_file_safe(workspace, "services/api/requirements.txt")
    # Passes if no pinned version conflicts exist (simplistic: no ==x.y with known bad combos)
    has_bad_urllib3 = bool(re.search(r"urllib3==1\.", content))
    has_new_requests = bool(re.search(r"requests==2\.(3[0-9]|[4-9]\d)\.", content))
    return not (has_bad_urllib3 and has_new_requests)


FIX_DETECTION: Dict[str, Callable[[str], bool]] = {
    fault: (lambda ws, f=fault: _score_fix(ws, f)[0] == 1.0)
    for fault in PARTIAL_FIX_CHECKS
}


def _score_fix(workspace: str, fault_type: str) -> Tuple[float, List[str]]:
    """Return (fraction_complete, list_of_failing_check_descriptions)."""
    checks = PARTIAL_FIX_CHECKS.get(fault_type, [])
    if not checks:
        return 1.0, []
    passed = 0
    failing: List[str] = []
    for desc, fn in checks:
        try:
            ok = fn(workspace)
        except Exception:
            ok = False
        if ok:
            passed += 1
        else:
            failing.append(desc)
    return passed / len(checks), failing


# ── Stage Result and Pipeline Result ──────────────────────────────────────

@dataclass
class SimulatedStageResult:
    name: str
    status: _StatusWrapper = field(default_factory=lambda: _sw(StageStatus.PENDING))
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    command: str = ""

    def __post_init__(self):
        if isinstance(self.status, str):
            self.status = _sw(self.status)


@dataclass
class SimulatedPipelineResult:
    pipeline_id: str = ""
    status: _StatusWrapper = field(default_factory=lambda: _sw(PipelineStatus.PENDING))
    stages: Dict[str, SimulatedStageResult] = field(default_factory=dict)
    failed_stage: str = ""
    total_duration_seconds: float = 0.0
    workspace_dir: str = ""
    image_tag: str = ""
    cache_tag: str = ""

    def __post_init__(self):
        if not self.stages:
            self.stages = {name: SimulatedStageResult(name=name) for name in STAGE_ORDER}
        if isinstance(self.status, str):
            self.status = _sw(self.status)

    def get_stage_logs(self, stage_name: str) -> str:
        stage = self.stages.get(stage_name)
        if not stage:
            return f"No logs available for stage '{stage_name}'"
        parts = []
        if stage.command:
            parts.append(f"$ {stage.command}")
        if stage.stdout:
            parts.append(stage.stdout)
        if stage.stderr:
            parts.append(stage.stderr)
        return "\n".join(parts) if parts else f"No output captured for stage '{stage_name}'"

    def get_stage_statuses(self) -> Dict[str, str]:
        return {name: str(stage.status) for name, stage in self.stages.items()}

    def get_stage_durations(self) -> Dict[str, float]:
        return {name: round(stage.duration_seconds, 2) for name, stage in self.stages.items()}


# ── Module-level partial-fix warning helper (importable by other runners) ─

def _partial_fix_warnings(
    active_faults: List[str],
    stage: str,
    fault_status: Dict[str, Tuple[bool, float, List[str]]],
) -> str:
    lines: List[str] = []
    for fault in active_faults:
        fully_fixed, score, failing = fault_status[fault]
        if not fully_fixed and score > 0 and FAULT_STAGE_MAP.get(fault) == stage:
            for check in failing:
                lines.append(f"Error: {check}")
            lines.append("Pipeline still failing — fix is incomplete.")
    return "\n".join(lines)


# ── Simulated Pipeline Runner ──────────────────────────────────────────────

class SimulatedPipelineRunner:
    """Simulated CI/CD pipeline runner that produces realistic logs without Docker.

    Maintains exact API compatibility with PipelineRunner from pipeline_runner.py.
    """

    def __init__(
        self,
        workspace_path: str,
        fault_type: Optional[str] = None,
        scenario: Optional["AdversarialCICDScenario"] = None,
        episode_id: Optional[str] = None,
    ):
        self.workspace_path = os.path.abspath(workspace_path)
        self.fault_type = fault_type
        self.scenario = scenario
        self.episode_id = episode_id or "sim-episode"

        # Collect all active faults (single fault_type OR scenario fault list)
        # Always start with the explicitly injected fault_type so the runner
        # checks for it even when the adversarial scenario lists a different
        # root cause (e.g. LLM returned a mismatched fault type).
        self._active_faults: List[str] = []
        if fault_type:
            self._active_faults = [fault_type]
        if scenario and hasattr(scenario, 'steps'):
            # Merge scenario faults, preserving fault_type as the first entry
            # and deduplicating so the same fault isn't checked twice.
            seen = set(self._active_faults)
            for step in scenario.steps:
                if step.fault_type not in seen:
                    self._active_faults.append(step.fault_type)
                    seen.add(step.fault_type)

        # Deterministic RNG seeded on episode + fault so reruns are identical
        seed_str = f"{self.episode_id}:{self.fault_type or 'none'}"
        seed_bytes = hashlib.sha256(seed_str.encode()).digest()
        self._seed = int.from_bytes(seed_bytes[:4], byteorder="big")
        self._rng = random.Random(self._seed)

    # ── Public run ─────────────────────────────────────────────────────────

    def run_stage(self, stage_name: str, workspace_dir: Optional[str] = None) -> SimulatedStageResult:
        """Execute a single pipeline stage and return its result.

        Useful for streaming stage-by-stage execution over WebSocket.
        Caller is responsible for tracking fault_status across calls.
        """
        ws_dir = workspace_dir or self.workspace_path

        fault_status: Dict[str, Tuple[bool, float, List[str]]] = {}
        for fault in self._active_faults:
            score, failing = _score_fix(ws_dir, fault)
            fault_status[fault] = (score == 1.0, score, failing)

        faults_by_stage: Dict[str, List[str]] = {s: [] for s in STAGE_ORDER}
        for fault in self._active_faults:
            fully_fixed, _, _ = fault_status[fault]
            if not fully_fixed:
                stage = FAULT_STAGE_MAP.get(fault, "build")
                faults_by_stage[stage].append(fault)

        syntax_errors_build: List[str] = []
        if stage_name == "build":
            for py_file in _find_python_files(ws_dir, "services") + _find_python_files(ws_dir, "tests"):
                ok, msg = _validate_python_syntax(ws_dir, py_file)
                if not ok:
                    syntax_errors_build.append(msg)
            sql_ok, sql_msg = _validate_sql_tokens(ws_dir, "db/migrations/001_init.sql")
            if not sql_ok:
                syntax_errors_build.append(sql_msg)

        active_stage_faults = faults_by_stage.get(stage_name, [])
        stage = SimulatedStageResult(name=stage_name, status=_sw(StageStatus.RUNNING))

        if stage_name == "clone":
            exit_code, stdout, stderr = self._run_clone_stage(ws_dir)
        elif stage_name == "build":
            exit_code, stdout, stderr = self._run_build_stage(
                ws_dir, active_stage_faults, fault_status, syntax_errors_build
            )
        elif stage_name == "test":
            exit_code, stdout, stderr = self._run_test_stage(ws_dir, active_stage_faults, fault_status)
        elif stage_name == "deploy":
            exit_code, stdout, stderr = self._run_deploy_stage(ws_dir, active_stage_faults, fault_status)
        else:
            exit_code, stdout, stderr = 1, "", f"Unknown stage: {stage_name}"

        stage.duration_seconds = self._stage_duration(stage_name, exit_code, active_stage_faults)
        stage.exit_code = exit_code
        stage.stdout = stdout
        stage.stderr = stderr
        stage.command = self._stage_command(stage_name)
        stage.status = _sw(StageStatus.PASSED if exit_code == 0 else StageStatus.FAILED)
        return stage

    def run(self, workspace_dir: Optional[str] = None) -> SimulatedPipelineResult:
        ws_dir = workspace_dir or self.workspace_path

        result = SimulatedPipelineResult(
            pipeline_id=f"sim-{self.episode_id[:8]}",
            status=_sw(PipelineStatus.RUNNING),
            workspace_dir=ws_dir,
            image_tag=f"sample-app-sim-{self.episode_id[:8]}",
            cache_tag="sample-app-sim-cache",
        )

        start_time = time.time()

        # Score every active fault against current workspace state
        fault_status: Dict[str, Tuple[bool, float, List[str]]] = {}
        for fault in self._active_faults:
            score, failing = _score_fix(ws_dir, fault)
            fault_status[fault] = (score == 1.0, score, failing)

        # Group still-failing faults by their pipeline stage
        faults_by_stage: Dict[str, List[str]] = {s: [] for s in STAGE_ORDER}
        for fault in self._active_faults:
            fully_fixed, _, _ = fault_status[fault]
            if not fully_fixed:
                stage = FAULT_STAGE_MAP.get(fault, "build")
                faults_by_stage[stage].append(fault)

        # Always run real Python syntax check on all service + test files
        syntax_errors_build: List[str] = []
        for py_file in _find_python_files(ws_dir, "services") + _find_python_files(ws_dir, "tests"):
            ok, msg = _validate_python_syntax(ws_dir, py_file)
            if not ok:
                syntax_errors_build.append(msg)

        # Always run real SQL syntax check
        sql_ok, sql_msg = _validate_sql_tokens(ws_dir, "db/migrations/001_init.sql")
        if not sql_ok:
            syntax_errors_build.append(sql_msg)

        for stage_name in STAGE_ORDER:
            stage = result.stages[stage_name]
            stage.status = _sw(StageStatus.RUNNING)

            active_stage_faults = faults_by_stage.get(stage_name, [])

            if stage_name == "clone":
                exit_code, stdout, stderr = self._run_clone_stage(ws_dir)
            elif stage_name == "build":
                exit_code, stdout, stderr = self._run_build_stage(
                    ws_dir, active_stage_faults, fault_status, syntax_errors_build
                )
            elif stage_name == "test":
                exit_code, stdout, stderr = self._run_test_stage(
                    ws_dir, active_stage_faults, fault_status
                )
            elif stage_name == "deploy":
                exit_code, stdout, stderr = self._run_deploy_stage(
                    ws_dir, active_stage_faults, fault_status
                )
            else:
                exit_code, stdout, stderr = 1, "", f"Unknown stage: {stage_name}"

            stage.duration_seconds = self._stage_duration(stage_name, exit_code, active_stage_faults)
            stage.exit_code = exit_code
            stage.stdout = stdout
            stage.stderr = stderr
            stage.command = self._stage_command(stage_name)

            if exit_code == 0:
                stage.status = _sw(StageStatus.PASSED)
            else:
                stage.status = _sw(StageStatus.FAILED)
                result.status = _sw(PipelineStatus.FAILED)
                result.failed_stage = stage_name

                for remaining in STAGE_ORDER[STAGE_ORDER.index(stage_name) + 1:]:
                    secondary = faults_by_stage.get(remaining, [])
                    note = f"Stage skipped due to upstream failure in {stage_name}."
                    if secondary:
                        note += (
                            f" NOTE: the following fault(s) would also fail here "
                            f"if reached: {', '.join(secondary)}"
                        )
                    result.stages[remaining].status = _sw(StageStatus.SKIPPED)
                    result.stages[remaining].stdout = note
                break
        else:
            result.status = _sw(PipelineStatus.PASSED)

        result.total_duration_seconds = time.time() - start_time
        return result

    # ── Duration helpers ───────────────────────────────────────────────────

    # (success ranges, fast-failure range) per stage
    _DURATION_SUCCESS = {
        "clone":  (0.8, 2.2),
        "build":  (8.0, 14.0),
        "test":   (2.5, 5.0),
        "deploy": (3.0, 6.0),
    }
    # Fault types that fail very early (< 1 s into the stage)
    _FAST_FAIL_FAULTS = {
        "dependency_conflict", "docker_order", "secret_exposure",
        "log_pii_leak", "log_disabled", "bad_migration_sql",
        "merge_conflict",  # SyntaxError caught at pytest import, fast
    }

    def _stage_duration(self, stage_name: str, exit_code: int, active_faults: List[str]) -> float:
        if exit_code != 0:
            if any(f in self._FAST_FAIL_FAULTS for f in active_faults):
                return round(self._rng.uniform(0.1, 0.8), 2)
            return round(self._rng.uniform(0.5, 2.0), 2)
        lo, hi = self._DURATION_SUCCESS.get(stage_name, (1.0, 3.0))
        return round(self._rng.uniform(lo, hi), 2)

    def _stage_command(self, stage_name: str) -> str:
        return {
            "clone":  "git clone /workspace/repo .",
            "build":  "docker build -t sample-app:latest .",
            "test":   "docker run --rm sample-app:latest python -m pytest tests/ -v",
            "deploy": "docker compose -f docker-compose.yml up -d",
        }.get(stage_name, "")

    # ── Stage simulators ───────────────────────────────────────────────────

    def _run_clone_stage(self, workspace: str) -> Tuple[int, str, str]:
        """Simulate git clone — reflect real workspace SHA if available."""
        sha, msg = self._real_git_head(workspace)
        stdout = (
            f"Cloning into 'sample-app'...\n"
            f"remote: Enumerating objects: 142, done.\n"
            f"remote: Counting objects: 100% (142/142), done.\n"
            f"remote: Compressing objects: 100% (89/89), done.\n"
            f"Receiving objects: 100% (142/142), 48.31 KiB | 2.1 MiB/s, done.\n"
            f"Resolving deltas: 100% (34/34), done.\n"
            f"HEAD is now at {sha} {msg}"
        )
        return 0, stdout, ""

    def _real_git_head(self, workspace: str) -> Tuple[str, str]:
        """Return (short_sha, subject) from the real workspace git log, or placeholders."""
        try:
            r = subprocess.run(
                ["git", "-C", workspace, "log", "-1", "--pretty=%h %s"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                parts = r.stdout.strip().split(" ", 1)
                return parts[0], parts[1] if len(parts) > 1 else ""
        except Exception:
            pass
        return "a3f812c", "feat: initial sample-app scaffold"

    def _run_build_stage(
        self,
        workspace: str,
        active_faults: List[str],
        fault_status: Dict[str, Tuple[bool, float, List[str]]],
        syntax_errors: List[str],
    ) -> Tuple[int, str, str]:
        """Simulate docker build + real secret scan + real log config check."""
        extra_warnings = self._partial_fix_warnings("build", fault_status)

        # 1. Active fault log
        if active_faults:
            _, stdout, stderr = self._fault_log(active_faults[0])
            if extra_warnings:
                stderr = extra_warnings + "\n" + stderr
            return 1, stdout, stderr

        # 2. Real Python / SQL syntax gate
        if syntax_errors:
            stderr = "\n\n".join(syntax_errors)
            if extra_warnings:
                stderr = extra_warnings + "\n" + stderr
            return 1, "", stderr

        # 3. Real secret scan
        scan_exit, scan_out, scan_err = _run_secret_scan(workspace)
        if scan_exit != 0:
            return 1, scan_out, scan_err

        # 4. Real log config check
        log_exit, log_out, log_err = _run_log_config_check(workspace)
        if log_exit != 0:
            return 1, log_out, log_err

        stdout = (
            "Step 1/8 : FROM python:3.11-slim\n"
            " ---> 4b8a3f2e1c9d\n"
            "Step 2/8 : WORKDIR /app\n"
            " ---> 9f2c4a7b3d61\n"
            "Step 3/8 : COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/\n"
            " ---> 1a2b3c4d5e6f\n"
            "Step 4/8 : COPY services/api/requirements.txt /app/requirements.txt\n"
            " ---> 2b3c4d5e6f7a\n"
            "Step 5/8 : RUN uv pip install --system --no-cache -r requirements.txt\n"
            "Using Python 3.11 environment\n"
            "Resolved 5 packages in 140ms\n"
            "Downloading Flask-3.0.3-py3-none-any.whl (101 kB)\n"
            "Downloading requests-2.31.0-py3-none-any.whl (62 kB)\n"
            "Downloading urllib3-2.0.7-py3-none-any.whl (123 kB)\n"
            "Downloading gunicorn-21.2.0-py3-none-any.whl (79 kB)\n"
            "Downloading pytest-8.1.0-py3-none-any.whl (341 kB)\n"
            "Installed 5 packages in 380ms\n"
            " + flask==3.0.3\n"
            " + gunicorn==21.2.0\n"
            " + pytest==8.1.0\n"
            " + requests==2.31.0\n"
            " + urllib3==2.0.7\n"
            "Step 6/8 : COPY . /app/\n"
            " ---> 3c4d5e6f7a8b\n"
            "Step 7/8 : EXPOSE 5000\n"
            " ---> 4d5e6f7a8b9c\n"
            "Step 8/8 : CMD [\"python\", \"-m\", \"services.api.app\"]\n"
            " ---> 5e6f7a8b9c0d\n"
            "Successfully built 5e6f7a8b9c0d\n"
            "Successfully tagged sample-app:latest\n"
            + scan_out
            + log_out
        )
        return 0, stdout, ""

    def _run_test_stage(
        self,
        workspace: str,
        active_faults: List[str],
        fault_status: Dict[str, Tuple[bool, float, List[str]]],
    ) -> Tuple[int, str, str]:
        extra_warnings = self._partial_fix_warnings("test", fault_status)

        if active_faults:
            _, stdout, stderr = self._fault_log(active_faults[0])
            if extra_warnings:
                stderr = extra_warnings + "\n" + stderr
            return 1, stdout, stderr

        # Count real test functions in the workspace for a realistic test count
        n_tests = self._count_test_functions(workspace)
        stdout = self._pytest_success_log(n_tests)
        return 0, stdout, ""

    def _run_deploy_stage(
        self,
        workspace: str,
        active_faults: List[str],
        fault_status: Dict[str, Tuple[bool, float, List[str]]],
    ) -> Tuple[int, str, str]:
        extra_warnings = self._partial_fix_warnings("deploy", fault_status)
        if has_terraform_config(workspace):
            tf_code, tf_stdout, tf_stderr = simulate_terraform_pipeline(workspace)
            if tf_code != 0:
                if extra_warnings:
                    tf_stderr = (extra_warnings + "\n" + tf_stderr).strip()
                return 1, tf_stdout, tf_stderr
        else:
            tf_stdout = ""

        if active_faults:
            _, stdout, stderr = self._fault_log(active_faults[0])
            if extra_warnings:
                stderr = extra_warnings + "\n" + stderr
            return 1, stdout, stderr

        _, health_msg = _simulate_health_check()
        stdout = (
            (tf_stdout + "\n" if tf_stdout else "")
            +
            'Creating network "sample-app_default" with the default driver\n'
            "Pulling db (postgres:15-alpine)...\n"
            "Pulling api (sample-app:latest)...\n"
            "Creating sample-app_db_1  ... done\n"
            "Creating sample-app_api_1 ... done\n"
            "api-service  | INFO:     Started server process [1]\n"
            "api-service  | INFO:     Waiting for application startup.\n"
            "api-service  | INFO:     Application startup complete.\n"
            "api-service  | INFO:     Uvicorn running on http://0.0.0.0:5000 (Press CTRL+C to quit)\n"
            f"{health_msg}\n"
            'api-service  | INFO:     GET /items runtime probe -> 200 OK\n'
            "All services healthy. Deploy complete."
        )
        return 0, stdout, ""

    # ── Partial-fix warning block ──────────────────────────────────────────

    def _partial_fix_warnings(
        self,
        stage: str,
        fault_status: Dict[str, Tuple[bool, float, List[str]]],
    ) -> str:
        return _partial_fix_warnings(self._active_faults, stage, fault_status)

    # ── Test helpers ───────────────────────────────────────────────────────

    def _count_test_functions(self, workspace: str) -> int:
        """Count def test_* functions across the workspace tests/ directory."""
        count = 0
        for py_file in _find_python_files(workspace, "tests"):
            content = _read_file_safe(workspace, py_file)
            count += len(re.findall(r"^def test_", content, re.MULTILINE))
        return max(count, 4)  # at least 4 so the log looks plausible

    def _pytest_success_log(self, n: int) -> str:
        files = ["tests/test_api.py", "tests/test_db.py", "tests/test_integration.py"]
        lines = [
            "============================= test session starts ==============================",
            "platform linux -- Python 3.11.7, pytest-8.1.0, pluggy-1.3.0",
            "rootdir: /workspace/sample-app",
            f"collected {n} items",
            "",
        ]
        per_file = max(1, n // len(files))
        collected = 0
        test_names = [
            "test_health_endpoint", "test_list_items", "test_get_item_exists",
            "test_get_item_not_found", "test_auth_flow", "test_response_schema",
            "test_migration_applies", "test_query_returns_rows", "test_schema_match",
            "test_end_to_end_flow", "test_concurrent_requests", "test_error_propagation",
        ]
        idx = 0
        for fpath in files:
            for _ in range(per_file):
                if idx >= len(test_names) or collected >= n:
                    break
                pct = int(((collected + 1) / n) * 100)
                name = test_names[idx]
                pad = max(0, 40 - len(fpath) - len(name) - 9)
                lines.append(f"{fpath}::{name} PASSED{' ' * pad}[{pct:3d}%]")
                idx += 1
                collected += 1
        dur = round(self._rng.uniform(2.1, 5.8), 2)
        lines.append("")
        lines.append(f"============================== {n} passed in {dur}s ==============================")
        return "\n".join(lines)

    # ── Fault log templates ────────────────────────────────────────────────

    def _fault_log(self, fault: str) -> Tuple[int, str, str]:
        """Return (exit_code, stdout, stderr) for a given fault."""

        # ── build-stage faults ─────────────────────────────────────────────
        if fault == "dependency_conflict":
            return 1, "", (
                "  × No solution found when resolving dependencies:\n"
                "  ╰─▶ Because requests==2.28.0 requires urllib3>=1.21.1,<1.27\n"
                "      and the requested urllib3==2.0.7 is incompatible,\n"
                "      we can conclude that requests==2.28.0 cannot be installed.\n"
                "      And because your project requires requests==2.28.0, we can\n"
                "      conclude that your project's requirements are unsatisfiable.\n\n"
                "hint: Pre-releases are available for urllib3 in the requested range\n"
                "      (e.g. 2.0.0a1), and pre-releases were not requested.\n"
                "      Add `--prerelease=allow` to allow them.\n\n"
                "ERROR: ResolutionImpossible\n"
                "The command '/bin/sh -c uv pip install --system --no-cache -r requirements.txt'"
                " returned a non-zero code: 1"
            )

        if fault == "docker_order":
            return 1, "", (
                "Step 3/8 : RUN uv pip install --system --no-cache -r services/api/requirements.txt\n"
                " ---> Running in 8f3a2b1e9c45\n"
                "error: Failed to open file `services/api/requirements.txt`\n"
                "  Caused by: No such file or directory (os error 2)\n"
                "The command '/bin/sh -c uv pip install --system --no-cache"
                " -r services/api/requirements.txt' returned a non-zero code: 2\n"
                "HINT: This is a docker build failure. The file is not missing — it has not been"
                " COPY'd into the image yet. Fix the COPY order in Dockerfile so COPY precedes RUN."
            )

        if fault == "secret_exposure":
            # Use real scan result so logs match actual file state
            _, _, stderr = _run_secret_scan(self.workspace_path)
            if not stderr:
                stderr = (
                    "[SECURITY GATE] Hardcoded secret detected in services/api/app.py:23\n"
                    '  ERROR: services/api/app.py:23: API_KEY = "sk-live-4f3c2a1b0e9d8c7f6a5b4e3d2c1a0f9e8d7c6b5a"\n'
                    '  ERROR: services/api/app.py:24: DATABASE_PASSWORD = "super_secret_db_password_2024"\n'
                    '  ERROR: services/api/app.py:25: WEBHOOK_SECRET = "whsec_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"\n\n'
                    "Policy check failed: plaintext credential found in source code.\n"
                    "Build blocked: secret exposure policy violation"
                )
            return 1, "", stderr

        if fault in ("log_pii_leak", "log_disabled"):
            # Use real log config check so logs match actual file state
            _, _, stderr = _run_log_config_check(self.workspace_path)
            if not stderr:
                _fallbacks = {
                    "log_pii_leak": (
                        "LOG CONFIG CHECK FAILED\n"
                        "[SECURITY SCAN] PII/credential leak detected: routes.py:89\n"
                        '  ERROR: services/api/routes.py:89: _log.warning("Auth token received:'
                        ' sk-live-4f3c2a1b0e9d8c7f6a5b4e3d2c1a0f9e8d7c6b5a")\n\n'
                        "Build blocked: PII policy violation"
                    ),
                    "log_disabled": (
                        "LOG CONFIG CHECK FAILED\n"
                        "WARNING: LOG_LEVEL=CRITICAL — all application logging suppressed.\n"
                        "  ERROR: services/api/logging_config.py:12: LOG_LEVEL hardcoded to CRITICAL\n\n"
                        "Build blocked: logging configuration violates observability requirements"
                    ),
                }
                stderr = _fallbacks.get(fault, "LOG CONFIG CHECK FAILED\nBuild blocked.")
            return 1, "", stderr

        if fault == "bad_migration_sql":
            # Use real SQL validator for accurate line/column info
            sql_ok, sql_msg = _validate_sql_tokens(self.workspace_path, "db/migrations/001_init.sql")
            if not sql_ok:
                return 1, "", sql_msg
            return 1, "", (
                'psycopg2.errors.SyntaxError: syntax error at or near "TABLE"\n'
                "LINE 1: CREAT TABLE IF NOT EXISTS builds (\n"
                "        ^\n"
                "DETAIL:  Expected CREATE, found CREAT\n"
                "CONTEXT:  SQL statement in migration file: db/migrations/001_init.sql:1\n"
                "ERROR: Database migration failed during build\n"
                "HINT: Read db/migrations/001_init.sql and fix 'CREAT TABLE' -> 'CREATE TABLE'."
            )

        # ── test-stage faults ──────────────────────────────────────────────
        if fault == "merge_conflict":
            ok, msg = _validate_python_syntax(self.workspace_path, "services/api/routes.py")
            if not ok:
                return 1, "", (
                    f"{msg}\n\n"
                    "During handling of the above exception, another exception occurred:\n\n"
                    "Traceback (most recent call last):\n"
                    '  File "/usr/local/lib/python3.11/site-packages/pytest/__main__.py", line 5, in <module>\n'
                    "    from pytest import console_main\n"
                    '  File "/app/tests/test_api.py", line 3, in <module>\n'
                    "    from services.api.routes import register_routes\n"
                    + msg + "\n"
                    "ERROR: InvocationError for command pytest tests/ -v (exited with code 1)"
                )
            return 1, "", (
                '  File "/app/services/api/routes.py", line 47\n'
                "    <<<<<<< HEAD\n"
                "    ^\n"
                "SyntaxError: invalid syntax\n\n"
                "ERROR: InvocationError for command pytest tests/ -v (exited with code 1)\n"
                "HINT: Read services/api/routes.py and remove ALL three git conflict marker lines"
                " (<<<<<<< HEAD, =======, >>>>>>> branch), keeping the correct code."
            )

        if fault == "flaky_test":
            # If the agent already relaxed the threshold or deleted the test, don't fire.
            if _flaky_test_fixed(self.workspace_path):
                return 0, "", ""
            # Read the actual threshold from the workspace file so the log reflects reality.
            content = _read_file_safe(self.workspace_path, "tests/test_api.py")
            m = re.search(r"threshold\s*=\s*([0-9]*\.?[0-9]+)", content)
            live_threshold = float(m.group(1)) if m else 0.001
            elapsed = round(self._rng.uniform(0.12, 0.19), 3)
            return 1, "", (
                "tests/test_api.py::test_response_time_health FAILED                   [ 50%]\n\n"
                "================================== FAILURES ===================================\n"
                "_________________________ test_response_time_health __________________________\n\n"
                "client = <FlaskClient <Flask 'api'>>\n\n"
                "    def test_response_time_health(client):\n"
                "        import time\n"
                "        start = time.time()\n"
                "        time.sleep(0.1)\n"
                "        response = client.get(\"/health\")\n"
                "        elapsed = time.time() - start\n"
                "        assert response.status_code == 200\n"
                f"        threshold = {live_threshold}\n"
                ">       assert elapsed < threshold, (\n"
                f'            f"Health endpoint took {elapsed:.3f}s, expected < {{threshold}}s "\n'
                '            f"(flaky: timing constraint too strict for this environment)."\n'
                "        )\n"
                f"E       AssertionError: Health endpoint took {elapsed:.3f}s, expected < {live_threshold}s"
                " (flaky: timing constraint too strict for this environment).\n\n"
                "tests/test_api.py:89: AssertionError\n"
                "=========================== short test summary info ============================\n"
                f"FAILED tests/test_api.py::test_response_time_health - AssertionError: response time {elapsed:.3f}s exceeds threshold {live_threshold}s\n"
                "========================= 1 failed, 11 passed in 3.21s =========================\n"
                "HINT: Read tests/test_api.py and change 'threshold = 0.001' to 'threshold = 0.5'."
            )

        # ── deploy-stage faults ────────────────────────────────────────────
        if fault == "missing_permission":
            return 1, "", (
                "ERROR: for api  Cannot create container for service api: "
                "network corp-internal-network-v2 declared as external, but could not be found\n"
                "ERROR: Encountered errors while bringing up the project.\n"
                "HINT: Read docker-compose.yml and remove 'external: true' from the network block."
            )

        if fault == "env_drift":
            return 1, "", (
                'ERROR: for api  Cannot create container for service api: '
                'invalid port specification: "not-a-number:5000"\n'
                "ERROR: Encountered errors while bringing up the project.\n"
                "HINT: Read docker-compose.yml and fix the port value to a valid integer (e.g. 5432)."
            )

        if fault == "invalid_database_url":
            return 1, (
                "Creating sample-app_api_1 ... done\n"
                "api-service  | INFO:     Started server process [1]\n"
                "api-service  | INFO:     Waiting for application startup.\n"
                "api-service  | INFO:     Application startup complete.\n"
                "api-service  | INFO:     Uvicorn running on http://0.0.0.0:5000 (Press CTRL+C to quit)\n"
                "api-service  | INFO:     GET /health -> 200 OK\n"
            ), (
                "api-service  | ERROR:    GET /items runtime probe failed\n"
                "api-service  | psycopg2.OperationalError: connection to server at \"db\" (172.18.0.2), port 15432 failed: Connection refused\n"
                "api-service  | DETAIL: DATABASE_URL came from .env and points at the wrong port.\n"
                "api-service  | HINT: Verify DATABASE_URL in .env before re-running the pipeline.\n"
                "ERROR: Deploy smoke test failed after startup during the first database-backed request."
            )

        if fault == "empty_secret_key":
            return 1, (
                "Creating sample-app_api_1 ... done\n"
                "api-service  | INFO:     Started server process [1]\n"
                "api-service  | INFO:     Waiting for application startup.\n"
                "api-service  | INFO:     Application startup complete.\n"
                "api-service  | INFO:     Uvicorn running on http://0.0.0.0:5000 (Press CTRL+C to quit)\n"
                "api-service  | INFO:     GET /health -> 200 OK\n"
            ), (
                "api-service  | ERROR:    GET /items runtime probe failed\n"
                "api-service  | RuntimeError: SECRET_KEY is empty; secure request context cannot be initialized\n"
                "api-service  | HINT: Restore SECRET_KEY in .env and rerun the deploy stage.\n"
                "ERROR: Runtime configuration fault surfaced only when request handling touched auth/session state."
            )

        if fault == "missing_pythonpath":
            return 1, (
                "Creating sample-app_api_1 ... done\n"
                "api-service  | INFO:     Started server process [1]\n"
                "api-service  | INFO:     Waiting for application startup.\n"
                "api-service  | INFO:     Application startup complete.\n"
                "api-service  | INFO:     Uvicorn running on http://0.0.0.0:5000 (Press CTRL+C to quit)\n"
                "api-service  | INFO:     GET /health -> 200 OK\n"
            ), (
                "api-service  | ERROR:    GET /items runtime probe failed\n"
                "api-service  | ModuleNotFoundError: No module named 'services.runtime_support'\n"
                "api-service  | DETAIL: virtualenv bootstrap file .venv/runtime.pth is missing /app/services.\n"
                "ERROR: Lazy runtime import failed even though build and startup succeeded.\n"
                "HINT: Read .venv/runtime.pth and add '/app/services' on a new line."
            )

        if fault == "circular_import_runtime":
            return 1, (
                "Creating sample-app_api_1 ... done\n"
                "api-service  | INFO:     Started server process [1]\n"
                "api-service  | INFO:     Waiting for application startup.\n"
                "api-service  | INFO:     Application startup complete.\n"
                "api-service  | INFO:     Uvicorn running on http://0.0.0.0:5000 (Press CTRL+C to quit)\n"
                "api-service  | INFO:     GET /health -> 200 OK\n"
            ), (
                "api-service  | ERROR:    GET /items runtime probe failed\n"
                "api-service  | ImportError: cannot import name 'load_runtime_probe' from partially initialized module 'services.api.runtime_probe' (most likely due to a circular import)\n"
                "api-service  | DETAIL: The circular import is triggered only by the request-path helper.\n"
                "ERROR: Runtime smoke test exposed a lazy circular import.\n"
                "HINT: Read services/api/runtime_probe.py and remove the FAULT_CIRCULAR_IMPORT_RUNTIME marker or the self-import line."
            )

        if fault == "missing_package_init":
            return 1, (
                "Creating sample-app_api_1 ... done\n"
                "api-service  | INFO:     Started server process [1]\n"
                "api-service  | INFO:     Waiting for application startup.\n"
                "api-service  | INFO:     Application startup complete.\n"
                "api-service  | INFO:     Uvicorn running on http://0.0.0.0:5000 (Press CTRL+C to quit)\n"
                "api-service  | INFO:     GET /health -> 200 OK\n"
            ), (
                "api-service  | ERROR:    GET /items runtime probe failed\n"
                "api-service  | ModuleNotFoundError: No module named 'services.runtime_support'\n"
                "api-service  | DETAIL: services/runtime_support/__init__.py is missing, so the lazy support package cannot be imported during request handling.\n"
                "ERROR: Package initialization fault only surfaced when runtime code path executed.\n"
                "HINT: Read services/runtime_support/__init__.py — it is empty or missing. Write it back with minimal content (e.g. a single comment line)."
            )

        if fault == "none_config_runtime":
            return 1, (
                "Creating sample-app_api_1 ... done\n"
                "api-service  | INFO:     Started server process [1]\n"
                "api-service  | INFO:     Waiting for application startup.\n"
                "api-service  | INFO:     Application startup complete.\n"
                "api-service  | INFO:     Uvicorn running on http://0.0.0.0:5000 (Press CTRL+C to quit)\n"
                "api-service  | INFO:     GET /health -> 200 OK\n"
            ), (
                "api-service  | ERROR:    GET /items runtime probe failed\n"
                "api-service  | TypeError: 'NoneType' object is not subscriptable\n"
                "api-service  | DETAIL: FEATURE_CACHE_BACKEND resolved to None from .env and crashed only when the request path dereferenced it.\n"
                "ERROR: Runtime-only config dereference fault triggered after startup.\n"
                "HINT: Read .env and set FEATURE_CACHE_BACKEND to a non-empty value (e.g. redis)."
            )

        if fault == "schema_drift":
            return 1, "", (
                'sqlalchemy.exc.OperationalError: (psycopg2.errors.UndefinedColumn) '
                'column "artifact_url" of relation "builds" does not exist\n'
                "LINE 1: SELECT id, task_key, status, started_at, finished_at, exit_code, artifact_url\n"
                "                                                                          ^\n"
                'HINT:  Perhaps you meant to reference the column "builds.exit_code".\n'
                "ERROR: Schema mismatch detected in database.py CANONICAL_COLUMNS\n"
                "Hint: Either add a migration to CREATE the column, or remove it from CANONICAL_COLUMNS."
            )

        if fault == "terraform_invalid_provider":
            return 1, "", (
                "Error: Failed to query available provider packages\n"
                "provider registry registry.terraform.io does not have a provider named invalidcorp/mock\n"
                "Terraform init failed.\n"
                "HINT: Read infra/main.tf and replace the 'provider \"invalidcorp\"' block with a valid provider (e.g. aws or hashicorp/random)."
            )

        if fault == "terraform_missing_variable":
            return 1, "", (
                "Error: No value for required variable\n"
                '  on infra/variables.tf line 1: variable "region" is required\n'
                "Terraform plan failed because required input variables are missing.\n"
                "HINT: Read infra/terraform.tfvars — it is empty. Write it with the required variable values (e.g. region = \"us-east-1\")."
            )

        if fault == "terraform_permission_denied":
            return 1, "", (
                "Error: AccessDenied: User is not authorized to perform this action\n"
                "Apply failed due to insufficient IAM permissions.\n"
                "HINT: Read infra/main.tf and remove the 'simulate_permission_denied' line that is injecting this failure."
            )

        # Generic fallback
        return 1, "", f"ERROR: Pipeline fault '{fault}' triggered at this stage."

    # ── Pipeline health ────────────────────────────────────────────────────

    def _compute_pipeline_health(self, stage_results: Dict[str, SimulatedStageResult]) -> float:
        health = 0.0
        for stage, weight in STAGE_WEIGHTS.items():
            if str(stage_results[stage].status) == StageStatus.PASSED:
                health += weight
        return round(health, 3)


# ── Compatibility Adapter ──────────────────────────────────────────────────

def cleanup_pipeline(result: SimulatedPipelineResult) -> None:
    """No-op cleanup for simulated pipeline (no Docker resources to clean)."""
    pass


def cleanup_cache_image(cache_tag: str) -> None:
    """No-op cleanup for simulated pipeline (no Docker images to remove)."""
    pass


# Type aliases for API compatibility with observation_builder and other consumers
PipelineResult = SimulatedPipelineResult
