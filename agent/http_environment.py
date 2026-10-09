"""HTTP helpers for talking to the OpenEnv REST server."""

from typing import Any, Dict, List, Optional, Tuple

import requests

from .config import BASE_URL, HTTP_TIMEOUT_SECONDS, MESSAGE_WINDOW

try:
    from ..models import MetaHackathonObservation
except ImportError:  # pragma: no cover - direct script execution
    from models import MetaHackathonObservation


def _endpoint(path: str) -> str:
    return f"{BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def parse_observation_payload(payload: Dict[str, Any]) -> MetaHackathonObservation:
    obs_data = payload.get("observation")
    if not isinstance(obs_data, dict):
        obs_data = payload

    return MetaHackathonObservation(
        task_id=obs_data.get("task_id", ""),
        task_title=obs_data.get("task_title", ""),
        difficulty=obs_data.get("difficulty", ""),
        pipeline_status=obs_data.get("pipeline_status", "unknown"),
        current_stage=obs_data.get("current_stage", ""),
        pipeline_stages=obs_data.get("pipeline_stages", {}),
        available_stages=obs_data.get("available_stages", []),
        available_tools=obs_data.get("available_tools", []),
        visible_alerts=obs_data.get("visible_alerts", []),
        visible_logs=obs_data.get("visible_logs", []),
        log_tokens_remaining=obs_data.get("log_tokens_remaining", 0),
        log_access_mode=obs_data.get("log_access_mode", "full"),
        logs_by_stage=obs_data.get("logs_by_stage", {}),
        visible_metrics=obs_data.get("visible_metrics", []),
        config_files=obs_data.get("config_files", {}),
        surfaced_errors=obs_data.get("surfaced_errors", []),
        findings=obs_data.get("findings", []),
        action_history=obs_data.get("action_history", []),
        previous_actions=obs_data.get("previous_actions", []),
        current_hypothesis=obs_data.get("current_hypothesis", ""),
        attempted_fix=obs_data.get("attempted_fix", ""),
        hypothesis_history=obs_data.get("hypothesis_history", []),
        active_issue_index=obs_data.get("active_issue_index", 0),
        revealed_issue_count=obs_data.get("revealed_issue_count", 1),
        pipeline_health=obs_data.get("pipeline_health", 1.0),
        recovery_cost=obs_data.get("recovery_cost", 0),
        redundant_actions=obs_data.get("redundant_actions", 0),
        destructive_actions=obs_data.get("destructive_actions", 0),
        incident_resolved=obs_data.get("incident_resolved", False),
        final_score=obs_data.get("final_score", 0.0),
        deterministic_score=obs_data.get("deterministic_score", 0.0),
        rubric_score=obs_data.get("rubric_score", 0.0),
        delayed_reward=obs_data.get("delayed_reward", 0.0),
        rubric_blend_weight=obs_data.get("rubric_blend_weight", 0.0),
        rubric_judge_used=obs_data.get("rubric_judge_used", False),
        rubric_judge_error=obs_data.get("rubric_judge_error", ""),
        done=payload.get("done", obs_data.get("done", False)),
        reward=payload.get("reward", obs_data.get("reward")),
        metadata=obs_data.get("metadata", {}),
    )


def reset_env(session: requests.Session) -> MetaHackathonObservation:
    response = session.post(_endpoint("/reset"), timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected /reset response payload")
    return parse_observation_payload(payload)


def step_env(
    session: requests.Session,
    *,
    operation: str,
    target: str,
    value: str,
) -> Tuple[MetaHackathonObservation, float, bool, Optional[str]]:
    response = session.post(
        _endpoint("/step"),
        json={"action": {"operation": operation, "target": target, "value": value}},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    if not response.ok:
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        print(
            f"[STEP ERROR] HTTP {response.status_code} from /step — "
            f"action=({operation!r}, {target!r}, {value[:60]!r}) — "
            f"body={detail}",
            flush=True,
        )
    response.raise_for_status()

    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected /step response payload")

    observation = parse_observation_payload(payload)
    reward = float(payload.get("reward", observation.reward or 0.0) or 0.0)
    done = bool(payload.get("done", observation.done))

    error: Optional[str] = None
    if isinstance(payload.get("error"), str):
        error = payload["error"]
    metadata = observation.metadata or {}
    if not error and isinstance(metadata, dict) and metadata.get("error"):
        error = str(metadata["error"])

    return observation, reward, done, error


def format_obs_for_llm(observation: MetaHackathonObservation, step_num: int) -> str:
    import re as _re

    def _redact_secrets(text: str) -> str:
        """Replace token-like values with a placeholder so they don't trip content filters."""
        # Matches common secret prefixes followed by hex/alphanumeric token bodies
        return _re.sub(
            r"(sk-live-|sk-test-|sk_live_|sk_test_|AKIA|ghp_|gho_|github_pat_|whsec_|gsk_)[A-Za-z0-9_\-]{8,}",
            r"\1[REDACTED]",
            text,
        )

    parts: List[str] = []
    parts.append(
        f"Log budget: {int(getattr(observation, 'log_tokens_remaining', 0) or 0)} "
        f"(mode={getattr(observation, 'log_access_mode', 'full')})"
    )
    errors = observation.surfaced_errors or []
    if errors:
        parts.append("⚠️  ACTIVE ERRORS (start here):")
        for item in errors[:5]:
            parts.append(f"  - {_redact_secrets(str(item))}")
        parts.append("")

    status = observation.pipeline_status or "?"
    stage = observation.current_stage or "?"
    parts.append(f"Pipeline status: {status} at stage: {stage}")
    parts.append(f"Step {step_num}")

    alerts = observation.visible_alerts or []
    if alerts:
        parts.append("Alerts: " + "; ".join(str(item) for item in alerts[:3]))

    logs = "\n".join(str(line) for line in (observation.visible_logs or [])[-6:])
    if logs and len(logs) < 800:
        parts.append(f"Logs: {logs}")
    elif logs:
        parts.append(f"Logs (truncated): {logs[:600]}...")

    if observation.current_hypothesis:
        parts.append(f"Current hypothesis: {observation.current_hypothesis}")

    if observation.attempted_fix:
        parts.append(f"Last fix attempted: {observation.attempted_fix}")

    if observation.incident_resolved:
        parts.append("*** ALL ISSUES RESOLVED - call finalize now ***")

    return "\n".join(parts)


def trim_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if len(messages) <= 1 + MESSAGE_WINDOW:
        return messages
    return messages[:1] + messages[-MESSAGE_WINDOW:]

