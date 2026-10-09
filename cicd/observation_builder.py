"""Build observations from real pipeline state and workspace file contents."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from cicd.simulated_runner import PipelineResult, PipelineStatus, StageStatus, STAGE_ORDER


# ── Error pattern matching ─────────────────────────────────────────────────

_ERROR_PATTERNS = [
    re.compile(r"(?i)\berror\b"),
    re.compile(r"(?i)\bfailed\b"),
    re.compile(r"(?i)\bFAILED\b"),
    re.compile(r"(?i)\bexit code\b"),
    re.compile(r"(?i)\bCannot\b"),
    re.compile(r"(?i)\bModuleNotFoundError\b"),
    re.compile(r"(?i)\bImportError\b"),
    re.compile(r"(?i)\bSyntaxError\b"),
    re.compile(r"(?i)\bPermissionError\b"),
    re.compile(r"(?i)\bFileNotFoundError\b"),
    re.compile(r"(?i)\bConnectionError\b"),
    re.compile(r"(?i)\bResolutionImpossible\b"),
    re.compile(r"(?i)\bCONFLICT\b"),
    re.compile(r"(?i)\bfatal\b"),
    re.compile(r"(?i)\bdenied\b"),
    re.compile(r"(?i)\brejected\b"),
    re.compile(r"(?i)\btimeout\b"),
    re.compile(r"(?i)\bsecret\b.*\bdetect"),
    re.compile(r"(?i)plaintext\s+credential"),
    re.compile(r"(?i)(requires|conflicts with|incompatible)"),
    re.compile(r"(?i)(urllib3|requests|flask|gunicorn).*version"),
]


def extract_error_lines(text: str, max_lines: int = 10) -> List[str]:
    """Return up to max_lines lines from text that match error patterns."""
    errors, seen = [], set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped in seen:
            continue
        if any(p.search(stripped) for p in _ERROR_PATTERNS):
            seen.add(stripped)
            errors.append(stripped)
            if len(errors) >= max_lines:
                break
    return errors


# ── File readers ───────────────────────────────────────────────────────────

_CONFIG_PATHS = [
    "Dockerfile",
    "docker-compose.yml",
    ".env",
    ".venv/runtime.pth",
    "services/api/requirements.txt",
    "services/api/routes.py",
    "services/api/app.py",
    "services/api/logging_config.py",
    "services/api/runtime_probe.py",
    "services/runtime_support/__init__.py",
    "services/runtime_support/request_context.py",
    "tests/test_api.py",
    ".github/ci.yml",
    ".github/workflows/ci.yml",
    "infra/main.tf",
    "infra/variables.tf",
    "infra/terraform.tfvars",
]


def _find_line_number(src: str, pattern: str) -> int:
    regex = re.compile(pattern)
    for idx, line in enumerate(src.splitlines(), 1):
        if regex.search(line):
            return idx
    return 1


def read_workspace_file(workspace_dir: str, relative_path: str) -> str:
    filepath = os.path.join(workspace_dir, relative_path)
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return f"[File not found: {relative_path}]"
    except OSError as e:
        return f"[Error reading {relative_path}: {e}]"


def read_config_files(workspace_dir: str) -> Dict[str, str]:
    configs = {}
    for path in _CONFIG_PATHS:
        content = read_workspace_file(workspace_dir, path)
        if not content.startswith("[File not found"):
            configs[path] = content
    return configs


# ── Log builders ───────────────────────────────────────────────────────────

def build_visible_logs(
    pipeline_result: PipelineResult,
    max_lines: int = 20,
    app_name: str = "frontend",
) -> Dict[str, List[str]]:
    """Return per-app log dict. Single-app pipelines emit logs under *app_name* ('frontend')."""
    logs: List[str] = []
    for stage_name in STAGE_ORDER:
        stage = pipeline_result.stages.get(stage_name)
        if not stage or stage.status == StageStatus.PENDING:
            continue
        icon = "✓" if stage.status == StageStatus.PASSED else ("✗" if stage.status == StageStatus.FAILED else "⊘")
        logs.append(f"[{icon}] Stage '{stage_name}' — {stage.status.value} ({stage.duration_seconds:.1f}s)")
        combined = (stage.stdout + "\n" + stage.stderr).strip()
        if combined:
            lines = [l.strip() for l in combined.splitlines() if l.strip()]
            tail = lines[-12:] if stage.status == StageStatus.FAILED else lines[-3:]
            logs.extend(tail)
    return {app_name: logs[:max_lines]}


def build_stage_log_tail(
    pipeline_result: PipelineResult,
    stage_name: str,
    tail_lines: int = 10,
) -> str:
    stage = pipeline_result.stages.get(stage_name)
    if not stage:
        return f"No logs available for stage '{stage_name}'"

    combined = ((stage.stdout or "") + "\n" + (stage.stderr or "")).strip()
    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    tail = lines[-tail_lines:] if lines else []
    header = (
        f"=== Stage tail: {stage_name} ===\n"
        f"Status: {stage.status.value}  Exit code: {stage.exit_code}  "
        f"Showing last {tail_lines} line(s)\n"
    )
    return header + ("\n".join(tail) if tail else "(no output yet)")


def build_logs_by_stage(pipeline_result: PipelineResult) -> Dict[str, List[str]]:
    result = {}
    for stage_name in STAGE_ORDER:
        stage = pipeline_result.stages.get(stage_name)
        if not stage:
            result[stage_name] = []
            continue
        combined = (stage.stdout + "\n" + stage.stderr).strip()
        result[stage_name] = [l.strip() for l in combined.splitlines() if l.strip()][-20:] if combined else []
    return result


def build_stage_log_response(pipeline_result: PipelineResult, stage_name: str) -> str:
    """Detailed log view for a specific stage (used by the view_logs action)."""
    stage = pipeline_result.stages.get(stage_name)
    if not stage:
        return f"No logs available for stage '{stage_name}'"

    header = (
        f"=== Stage: {stage_name} ===\n"
        f"Status: {stage.status.value}  Exit code: {stage.exit_code}  "
        f"Duration: {stage.duration_seconds:.1f}s\n"
        f"Command: {stage.command}\n"
    )

    combined = ((stage.stdout or "") + "\n" + (stage.stderr or "")).strip()
    all_lines = combined.splitlines() if combined else []

    # Filter out noisy import-machinery lines that obscure the real error location
    _noise = ("<frozen importlib", "_bootstrap", "importlib._")
    meaningful = [l for l in all_lines if not any(n in l for n in _noise)]

    error_lines = extract_error_lines("\n".join(meaningful), max_lines=15)
    tail_lines = meaningful[-30:]

    parts = []
    if error_lines:
        parts.append("--- key errors ---")
        parts.extend(error_lines)
    if tail_lines:
        parts.append("--- log tail ---")
        parts.extend(tail_lines)

    return header + "\n".join(parts)


def build_surfaced_errors(pipeline_result: PipelineResult, workspace_dir: str = "") -> List[str]:
    """Extract errors from failed stage logs and scan source files for conflict markers.

    Conflict markers are surfaced FIRST so the LLM anchors on the real root cause,
    not on downstream ImportError/SyntaxError symptoms in the test stage.
    """
    conflict_errors: List[str] = []
    stage_errors: List[str] = []

    # 1. Scan source files for merge conflict markers (primary — shown first)
    if workspace_dir:
        for rel_path in ["services/api/routes.py", "services/api/app.py",
                          "services/api/requirements.txt", "services/api/logging_config.py",
                          "tests/test_api.py", "Dockerfile", "docker-compose.yml"]:
            full_path = os.path.join(workspace_dir, rel_path)
            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                if "<<<<<<< " in content:
                    for i, line in enumerate(content.splitlines(), 1):
                        if line.startswith(("<<<<<<<", "=======", ">>>>>>>")):
                            conflict_errors.append(f"MERGE CONFLICT in {rel_path}:{i}: {line.strip()}")
            except OSError:
                continue

    # 2. Extract error lines from failed stage logs (secondary)
    for stage_name in STAGE_ORDER:
        stage = pipeline_result.stages.get(stage_name)
        if not stage or stage.status != StageStatus.FAILED:
            continue
        combined = (stage.stdout or "") + "\n" + (stage.stderr or "")
        for line in extract_error_lines(combined, max_lines=8):
            # Skip generic import-machinery lines that obscure the real cause
            if any(skip in line for skip in ["<frozen importlib", "_bootstrap", "importlib._"]):
                continue
            # If there are conflict errors, skip generic SyntaxError/IndentationError lines
            # since they are downstream symptoms of the conflict, not root causes
            if conflict_errors and any(skip in line for skip in ["SyntaxError", "IndentationError"]):
                continue
            # Filter registry cache noise — this is a benign Docker BuildKit cache miss
            # that appears in every build regardless of fault type and always misleads the agent.
            if "failed to configure registry cache importer" in line or "insufficient_scope" in line:
                continue
            stage_errors.append(line)

    clue_errors = _build_config_clues(stage_errors, workspace_dir)
    return (conflict_errors + clue_errors + stage_errors)[:10]


def _build_config_clues(stage_errors: List[str], workspace_dir: str) -> List[str]:
    if not workspace_dir or not stage_errors:
        return []

    joined = "\n".join(stage_errors).lower()
    clues: List[str] = []

    if (
        "requirements.txt" in joined
        and ("could not open requirements file" in joined or "no such file or directory" in joined)
    ):
        root_req = os.path.join(workspace_dir, "requirements.txt")
        svc_req = os.path.join(workspace_dir, "services", "api", "requirements.txt")
        if not os.path.exists(root_req) and os.path.exists(svc_req):
            clues.append(
                "Config clue: requirements.txt is at services/api/requirements.txt (not repository root)."
            )

    compose_path = os.path.join(workspace_dir, "docker-compose.yml")
    if os.path.exists(compose_path):
        compose_text = read_workspace_file(workspace_dir, "docker-compose.yml")
        compose_lower = compose_text.lower()
        deploy_env_error = (
            ("port" in joined and "invalid" in joined)
            or ("interpolation" in joined and "port" in joined)
            or ("compose" in joined and "port" in joined)
        )
        if "${port}:5000" in compose_lower and "port=not-a-number" in compose_lower and deploy_env_error:
            clues.append(
                "Config clue: docker-compose.yml sets PORT=not-a-number while ports uses ${PORT}:5000."
            )

    clues.extend(_build_logging_fault_clues(joined, workspace_dir))
    return clues


def _build_logging_fault_clues(joined_errors: str, workspace_dir: str) -> List[str]:
    clues: List[str] = []

    logging_rel = "services/api/logging_config.py"
    logging_text = read_workspace_file(workspace_dir, logging_rel)
    if not logging_text.startswith("[File not found"):
        path_m = re.search(r'LOG_PATH\s*(?::\s*\w+)?\s*=\s*["\']([^"\']+)["\']', logging_text)
        if path_m:
            declared = path_m.group(1)
            if declared.startswith(("/var/log", "/root", "/sys", "/proc")) and any(
                token in joined_errors
                for token in ("log_path", "restricted system directory", "/var/log", "cannot write", "unwritable")
            ):
                line_no = _find_line_number(logging_text, r'LOG_PATH\s*(?::\s*\w+)?\s*=')
                clues.append(
                    "Config issue in services/api/logging_config.py:"
                    f"{line_no}: LOG_PATH default {declared!r} points to a restricted system directory."
                )

        level_m = re.search(r'LOG_LEVEL\s*(?::\s*\w+)?\s*=\s*["\']([A-Z]+)["\']', logging_text)
        if level_m:
            level = level_m.group(1)
            if level == "CRITICAL" and any(
                token in joined_errors
                for token in ("log_level", "critical", "silences all log output", "silent")
            ):
                line_no = _find_line_number(logging_text, r'LOG_LEVEL\s*(?::\s*\w+)?\s*=')
                clues.append(
                    "Config issue in services/api/logging_config.py:"
                    f"{line_no}: LOG_LEVEL is hardcoded to {level!r}, which silences normal log output."
                )

        if "json.dumps" not in logging_text and any(
            token in joined_errors
            for token in ("formatter", "json", "valid json", "malformed")
        ):
            line_no = _find_line_number(logging_text, r"return\s+")
            clues.append(
                "Config issue in services/api/logging_config.py:"
                f"{line_no}: formatter does not call json.dumps(), so logs will not be valid JSON."
            )

        if "RotatingFileHandler" not in logging_text and any(
            token in joined_errors
            for token in ("rotation", "rotatingfilehandler", "filehandler", "unbounded")
        ):
            line_no = _find_line_number(logging_text, r"FileHandler|RotatingFileHandler")
            clues.append(
                "Config issue in services/api/logging_config.py:"
                f"{line_no}: RotatingFileHandler is missing, so logs can grow without rotation."
            )

    routes_rel = "services/api/routes.py"
    routes_text = read_workspace_file(workspace_dir, routes_rel)
    if (
        not routes_text.startswith("[File not found")
        and any(token in joined_errors for token in ("pii", "credential", "token", "routes.py", "log call"))
    ):
        pii_patterns = [
            r"sk-live",
            r"sk-test",
            r"AKIA",
            r"log_pii_leak",
        ]
        for pattern in pii_patterns:
            if re.search(pattern, routes_text):
                line_no = _find_line_number(routes_text, pattern)
                clues.append(
                    "Config issue in services/api/routes.py:"
                    f"{line_no}: a log call may emit credential values (PII leak risk)."
                )
                break

    compose_text = read_workspace_file(workspace_dir, "docker-compose.yml")
    if (
        not compose_text.startswith("[File not found")
        and "./logs:/app/logs" not in compose_text
        and any(token in joined_errors for token in ("log file not found", "logs volume", "/app/logs", "log_path"))
    ):
        line_no = _find_line_number(compose_text, r"/app/logs|volumes:")
        clues.append(
            "Config issue in docker-compose.yml:"
            f"{line_no}: the ./logs:/app/logs volume mount is missing."
        )

    return clues


def build_visible_alerts(pipeline_result: PipelineResult) -> List[str]:
    if pipeline_result.status == PipelineStatus.FAILED:
        stage = pipeline_result.stages.get(pipeline_result.failed_stage)
        if not stage:
            return []
        alerts = [f"Pipeline FAILED at stage '{pipeline_result.failed_stage}' with exit code {stage.exit_code}"]
        combined = (stage.stdout + "\n" + stage.stderr).strip()
        alerts.extend(f"  → {e}" for e in extract_error_lines(combined, max_lines=2))
        return alerts
    if pipeline_result.status == PipelineStatus.PASSED:
        return ["Pipeline PASSED — all stages completed successfully"]
    return []


def build_visible_metrics(pipeline_result: PipelineResult) -> List[str]:
    metrics = []
    for stage_name in STAGE_ORDER:
        stage = pipeline_result.stages.get(stage_name)
        if not stage or stage.status in (StageStatus.PENDING, StageStatus.SKIPPED):
            continue
        metrics.append(
            f"{stage_name}: status={stage.status.value} "
            f"exit_code={stage.exit_code} duration={stage.duration_seconds:.1f}s"
        )
    metrics.append(f"total_duration={pipeline_result.total_duration_seconds:.1f}s")
    return metrics


# ── Main observation builder ───────────────────────────────────────────────

_DEFAULT_SERVICE_DEPENDENCY_GRAPH: Dict[str, List[str]] = {}


def build_observation(
    pipeline_result: PipelineResult,
    workspace_dir: str,
    *,
    task_id: str = "",
    task_title: str = "",
    difficulty: str = "",
    available_tools: Optional[List[str]] = None,
    reward: float = 0.0,
    done: bool = False,
    action_history: Optional[List[str]] = None,
    current_hypothesis: str = "",
    attempted_fix: str = "",
    hypothesis_history: Optional[List[str]] = None,
    incident_resolved: bool = False,
    pipeline_health: float = 1.0,
    recovery_cost: int = 0,
    redundant_actions: int = 0,
    destructive_actions: int = 0,
    final_score: float = 0.0,
    deterministic_score: float = 0.0,
    rubric_score: float = 0.0,
    delayed_reward: float = 0.0,
    rubric_blend_weight: float = 0.0,
    rubric_judge_used: bool = False,
    rubric_judge_error: str = "",
    log_tokens_remaining: int = 0,
    log_access_mode: str = "full",
    metadata: Optional[Dict[str, Any]] = None,
    findings: Optional[List[str]] = None,
    affected_apps: Optional[List[str]] = None,
    service_dependency_graph: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    """Construct a complete observation dict from real pipeline state."""
    failed_stage = pipeline_result.failed_stage or ""
    if pipeline_result.status == PipelineStatus.PASSED:
        current_stage = "deploy"
    elif failed_stage:
        current_stage = failed_stage
    else:
        current_stage = next(
            (sn for sn in STAGE_ORDER
             if (s := pipeline_result.stages.get(sn)) and s.status in (StageStatus.RUNNING, StageStatus.PENDING)),
            "",
        )

    _logs_per_app = build_visible_logs(pipeline_result)

    return {
        "task_id": task_id,
        "task_title": task_title,
        "difficulty": difficulty,
        "pipeline_status": pipeline_result.status.value,
        "current_stage": current_stage,
        "pipeline_stages": pipeline_result.get_stage_statuses(),
        "available_stages": list(STAGE_ORDER),
        "available_tools": available_tools or [
            "view_logs", "tail_logs", "inspect_config", "inspect_dockerfile", "inspect_permissions",
            "set_hypothesis", "modify_config", "add_dependency",
            "rerun_pipeline", "verify_fix", "finalize",
        ],
        "visible_alerts": build_visible_alerts(pipeline_result),
        "visible_logs_per_app": _logs_per_app,
        "visible_logs": [line for lines in _logs_per_app.values() for line in lines],
        "log_tokens_remaining": int(log_tokens_remaining),
        "log_access_mode": str(log_access_mode or "full"),
        "affected_apps": affected_apps or [],
        "service_dependency_graph": service_dependency_graph if service_dependency_graph is not None else _DEFAULT_SERVICE_DEPENDENCY_GRAPH,
        "logs_by_stage": build_logs_by_stage(pipeline_result),
        "visible_metrics": build_visible_metrics(pipeline_result),
        "config_files": read_config_files(workspace_dir),
        "surfaced_errors": build_surfaced_errors(pipeline_result, workspace_dir),
        "findings": findings or ["Incident acknowledged. Investigate before changing configuration."],
        "action_history": action_history or [],
        "previous_actions": action_history or [],
        "current_hypothesis": current_hypothesis,
        "attempted_fix": attempted_fix,
        "hypothesis_history": hypothesis_history or [],
        "active_issue_index": 0,
        "revealed_issue_count": 1,
        "pipeline_health": round(pipeline_health, 3),
        "recovery_cost": recovery_cost,
        "redundant_actions": redundant_actions,
        "destructive_actions": destructive_actions,
        "incident_resolved": incident_resolved,
        "final_score": final_score,
        "deterministic_score": deterministic_score,
        "rubric_score": rubric_score,
        "delayed_reward": delayed_reward,
        "rubric_blend_weight": rubric_blend_weight,
        "rubric_judge_used": rubric_judge_used,
        "rubric_judge_error": rubric_judge_error,
        "reward": reward,
        "done": done,
        "metadata": metadata or {},
    }
