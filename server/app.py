# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""
FastAPI application for the Meta Hackathon Environment.

This module creates an HTTP server that exposes the MetaHackathonEnvironment
over HTTP and WebSocket endpoints, compatible with EnvClient.

Automatically starts the CI/CD Dynamic API server on port 8001 for Theme #3
compliance (API-based dynamic system interactions).

Endpoints:
    - POST /reset: Reset the environment
    - POST /step: Execute an action
    - GET /state: Get current environment state
    - GET /schema: Get action/observation schemas
    - WS /ws: WebSocket endpoint for persistent sessions

Usage:
    # Development (with auto-reload):
    uvicorn server.app:app --reload --host 0.0.0.0 --port 8000

    # Production:
    uvicorn server.app:app --host 0.0.0.0 --port 8000 --workers 4

    # Or run directly:
    python -m server.app
    
    # The CI/CD API will be available at:
    # - http://localhost:8001 (API endpoints)
    # - http://localhost:8001/docs (Interactive documentation)
"""

import atexit
import html
import logging
import multiprocessing
import os
import re
import sys
import time
from typing import Any, Dict, List

try:
    from openenv.core.env_server.http_server import create_app
except Exception as e:  # pragma: no cover
    raise ImportError(
        "openenv is required for the web interface. Install dependencies with '\n    uv sync\n'"
    ) from e


CANONICAL_OPERATIONS = [
    "view_logs",
    "tail_logs",
    "inspect_config",
    "inspect_dockerfile",
    "inspect_permissions",
    "set_hypothesis",
    "modify_config",
    "add_dependency",
    "rerun_pipeline",
    "verify_fix",
    "finalize",
]

FAULT_FIX_HINTS = {
    "merge_conflict": {
        "operation": "modify_config",
        "target": "Dockerfile",
        "value": "resolve-merge-conflict",
        "title": "Resolve merge conflict",
        "reason": "Merge conflicts are fixed directly in the affected file.",
    },
    "dependency_conflict": {
        "operation": "add_dependency",
        "target": "services/api/requirements.txt",
        "value": "pin-compatible-requests-urllib3",
        "title": "Repair dependency pinning",
        "reason": "Dependency faults map to requirements changes.",
    },
    "docker_order": {
        "operation": "modify_config",
        "target": "Dockerfile",
        "value": "reorder-docker-install-steps",
        "title": "Fix Dockerfile order",
        "reason": "The Dockerfile usually needs instruction ordering corrected.",
    },
    "flaky_test": {
        "operation": "modify_config",
        "target": "tests/test_api.py",
        "value": "add-flaky-test-retry-wrapper",
        "title": "Stabilize flaky test",
        "reason": "The failing test should be made deterministic or retry-safe.",
    },
    "missing_permission": {
        "operation": "modify_config",
        "target": "docker-compose.yml",
        "value": "fix-docker-compose-network",
        "title": "Fix compose permissions/network",
        "reason": "Permission faults usually surface in compose or runtime config.",
    },
    "secret_exposure": {
        "operation": "modify_config",
        "target": "services/api/app.py",
        "value": "remove-hardcoded-secrets",
        "title": "Remove secret exposure",
        "reason": "Secrets are removed by editing the leaking source file.",
    },
    "env_drift": {
        "operation": "modify_config",
        "target": "docker-compose.yml",
        "value": "fix-docker-compose-network",
        "title": "Fix environment drift",
        "reason": "Environment drift is typically a compose wiring issue.",
    },
    "invalid_database_url": {
        "operation": "modify_config",
        "target": ".env",
        "value": "fix-database-url",
        "title": "Fix DATABASE_URL",
        "reason": "The service starts, but the first DB-backed request fails because runtime env wiring is wrong.",
    },
    "empty_secret_key": {
        "operation": "modify_config",
        "target": ".env",
        "value": "restore-secret-key",
        "title": "Restore SECRET_KEY",
        "reason": "Blank secrets usually live in runtime env config rather than application code.",
    },
    "missing_pythonpath": {
        "operation": "modify_config",
        "target": ".venv/runtime.pth",
        "value": "restore-pythonpath",
        "title": "Restore virtualenv path bootstrap",
        "reason": "The runtime import path is broken inside the virtual environment.",
    },
    "circular_import_runtime": {
        "operation": "modify_config",
        "target": "services/api/runtime_probe.py",
        "value": "break-circular-import",
        "title": "Break runtime circular import",
        "reason": "The failure only appears when the lazy request-time helper is imported.",
    },
    "missing_package_init": {
        "operation": "modify_config",
        "target": "services/runtime_support/__init__.py",
        "value": "restore-package-init",
        "title": "Restore missing package init",
        "reason": "The runtime support package needs its __init__.py back for lazy imports.",
    },
    "none_config_runtime": {
        "operation": "modify_config",
        "target": ".env",
        "value": "replace-none-runtime-config",
        "title": "Replace None runtime config",
        "reason": "The crash comes from a config value that only gets dereferenced during request handling.",
    },
    "log_pii_leak": {
        "operation": "modify_config",
        "target": "services/api/routes.py",
        "value": "remove-pii-log-line",
        "title": "Remove PII from logs",
        "reason": "The leak lives in application code, so a code edit is appropriate.",
    },
    "log_disabled": {
        "operation": "modify_config",
        "target": "services/api/logging_config.py",
        "value": "restore-info-logging",
        "title": "Restore logging",
        "reason": "Logging config needs to be restored before verification can work.",
    },
    "bad_migration_sql": {
        "operation": "modify_config",
        "target": "db/migrations/001_init.sql",
        "value": "fix-bad-migration-sql",
        "title": "Fix SQL migration",
        "reason": "The migration file itself needs correction.",
    },
    "schema_drift": {
        "operation": "modify_config",
        "target": "db/database.py",
        "value": "align-schema-columns",
        "title": "Align schema drift",
        "reason": "Schema drift is usually fixed in the schema definition layer.",
    },
    "terraform_invalid_provider": {
        "operation": "modify_config",
        "target": "infra/main.tf",
        "value": "fix-terraform-provider",
        "title": "Fix Terraform provider",
        "reason": "Provider registry failures are fixed in the Terraform provider block.",
    },
    "terraform_missing_variable": {
        "operation": "modify_config",
        "target": "infra/terraform.tfvars",
        "value": "add-terraform-variables",
        "title": "Supply Terraform variables",
        "reason": "Missing input variables are resolved by adding tfvars values.",
    },
    "terraform_permission_denied": {
        "operation": "modify_config",
        "target": "infra/main.tf",
        "value": "remove-terraform-permission-blocker",
        "title": "Resolve Terraform permission blocker",
        "reason": "Apply permission errors typically come from infrastructure permission configuration.",
    },
}


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            return value.dict()
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        try:
            return dict(value.__dict__)
        except Exception:
            pass
    return {}


def _safe_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def _compact_list(items: Any, limit: int = 6) -> List[str]:
    if not isinstance(items, list):
        return []
    return [_safe_text(item) for item in items[:limit]]


def _extract_path_from_text(text: str) -> str:
    match = re.search(r"\b((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|ya?ml|txt|env|sql|pth))\b", text)
    if match:
        return match.group(1).strip().strip(".,)")
    if "Dockerfile" in text:
        return "Dockerfile"
    return ""


def _episode_snapshot(web_manager: Any) -> Dict[str, Any]:
    episode_state = getattr(web_manager, "episode_state", None)
    current_observation = _as_dict(getattr(episode_state, "current_observation", None))
    action_logs = getattr(episode_state, "action_logs", None)
    manager_state: Dict[str, Any] = {}

    # Prefer direct state attributes when the runtime exposes pydantic-v1 models
    # (dict()) but not pydantic-v2 (model_dump()), because some get_state()
    # implementations unconditionally call model_dump().
    for attr_name in ("state", "_state"):
        raw_state = getattr(web_manager, attr_name, None)
        if raw_state is None:
            continue
        if hasattr(raw_state, "dict") and not hasattr(raw_state, "model_dump"):
            manager_state = _as_dict(raw_state)
            break

    if not manager_state and hasattr(web_manager, "get_state"):
        try:
            manager_state = _as_dict(web_manager.get_state())
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "Falling back to attribute state after get_state() failure: %s",
                exc,
            )

    if not manager_state:
        for attr_name in ("state", "_state"):
            candidate = _as_dict(getattr(web_manager, attr_name, None))
            if candidate:
                manager_state = candidate
                break

    return {
        "episode_id": _safe_text(getattr(episode_state, "episode_id", "")),
        "step_count": int(getattr(episode_state, "step_count", 0) or 0),
        "action_logs": action_logs if isinstance(action_logs, list) else [],
        "current_observation": current_observation,
        "state": manager_state,
    }


def _pick_config_target(observation: Dict[str, Any], stage: str) -> str:
    config_files = observation.get("config_files") if isinstance(observation, dict) else {}
    if isinstance(config_files, dict) and config_files:
        candidates = [_safe_text(key) for key in config_files.keys() if _safe_text(key)]
        if stage:
            lowered_stage = stage.lower()
            for candidate in candidates:
                if lowered_stage in candidate.lower():
                    return candidate
        return candidates[0]

    for error_line in _compact_list(observation.get("surfaced_errors", []), limit=12):
        candidate = _extract_path_from_text(error_line)
        if candidate:
            return candidate

    if stage == "deploy":
        return "docker-compose.yml"
    if stage == "test":
        return "tests/test_api.py"
    if stage == "build":
        return "Dockerfile"
    return "Dockerfile"


def _summarize_stage(observation: Dict[str, Any]) -> str:
    pipeline_stages = observation.get("pipeline_stages") if isinstance(observation, dict) else {}
    if not isinstance(pipeline_stages, dict) or not pipeline_stages:
        return "No stage breakdown is available yet."
    return "\n".join(
        f"- **{html.escape(_safe_text(stage))}**: {_safe_text(status)}"
        for stage, status in pipeline_stages.items()
    )


def _build_action_history_markdown(observation: Dict[str, Any]) -> str:
    actions = _compact_list(observation.get("action_history", []), limit=12)
    if not actions:
        return "No actions yet. Use the suggested action to start the episode."
    return "\n".join(f"- {html.escape(action)}" for action in actions)


def _build_findings_markdown(observation: Dict[str, Any]) -> str:
    findings = _compact_list(observation.get("findings", []), limit=12)
    if not findings:
        return "No findings recorded yet."
    return "\n".join(f"- {html.escape(finding)}" for finding in findings)


def _build_target_options(observation: Dict[str, Any], suggestion: Dict[str, str]) -> List[str]:
    options: List[str] = []
    config_files = observation.get("config_files") if isinstance(observation, dict) else {}
    if isinstance(config_files, dict):
        options.extend([_safe_text(key) for key in config_files.keys() if _safe_text(key)])
    options.extend(_compact_list(observation.get("available_stages", []), limit=8))
    for candidate in [suggestion.get("target", ""), observation.get("current_stage", ""), (observation.get("metadata") or {}).get("expected_fail_stage", "")]:
        candidate = _safe_text(candidate)
        if candidate:
            options.append(candidate)
    deduped: List[str] = []
    seen = set()
    for item in options:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped[:20]


def _build_summary_html(observation: Dict[str, Any], episode: Dict[str, Any], suggestion: Dict[str, str]) -> str:
    metadata = observation.get("metadata") if isinstance(observation, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    cards = [
        ("Episode", episode.get("episode_id") or "Waiting for reset"),
        ("Steps", episode.get("step_count", 0)),
        ("Pipeline", observation.get("pipeline_status", "unknown")),
        ("Stage", observation.get("current_stage", "")),
        ("Health", observation.get("pipeline_health", 1.0)),
        ("Finalize", "ready" if metadata.get("ready_to_finalize") else "not ready"),
    ]
    card_html = []
    for label, value in cards:
        card_html.append(
            f"<div class='mh-card'><div class='mh-card-label'>{html.escape(_safe_text(label))}</div>"
            f"<div class='mh-card-value'>{html.escape(_safe_text(value))}</div></div>"
        )
    return (
        "<div class='mh-summary-grid'>"
        + "".join(card_html)
        + "</div>"
        + "<div class='mh-suggestion'>"
        + f"<div class='mh-suggestion-title'>{html.escape(suggestion.get('title', 'Suggested next step'))}</div>"
        + f"<div class='mh-suggestion-body'><strong>Operation:</strong> {html.escape(suggestion.get('operation', ''))}"
        + f"<br><strong>Target:</strong> {html.escape(suggestion.get('target', ''))}"
        + f"<br><strong>Value:</strong> {html.escape(suggestion.get('value', ''))}"
        + f"<br><strong>Why:</strong> {html.escape(suggestion.get('reason', ''))}</div></div>"
    )


def _suggest_next_action(observation: Dict[str, Any]) -> Dict[str, str]:
    observation = observation if isinstance(observation, dict) else {}
    metadata = observation.get("metadata") if isinstance(observation.get("metadata"), dict) else {}
    fault_type = _safe_text(metadata.get("fault_type") or observation.get("task_id", "")).replace("sim_", "")
    fault_hint = FAULT_FIX_HINTS.get(fault_type)
    action_history = _compact_list(observation.get("action_history", []), limit=20)
    latest_action = action_history[-1] if action_history else ""
    current_stage = _safe_text(observation.get("current_stage", ""))
    pipeline_status = _safe_text(observation.get("pipeline_status", ""))
    current_hypothesis = _safe_text(observation.get("current_hypothesis", ""))
    attempted_fix = _safe_text(observation.get("attempted_fix", ""))
    surfaced_errors = _compact_list(observation.get("surfaced_errors", []), limit=12)

    if metadata.get("ready_to_finalize"):
        return {
            "operation": "finalize",
            "target": "",
            "value": "",
            "title": "Finalize episode",
            "reason": "The simulator says the fix is verified and ready to finish.",
        }

    if metadata.get("verification_required"):
        return {
            "operation": "verify_fix",
            "target": "",
            "value": "",
            "title": "Verify the fix",
            "reason": "A rerun already happened and the environment wants verification before finalizing.",
        }

    if attempted_fix and latest_action != "rerun_pipeline" and not metadata.get("verified_since_last_rerun"):
        return {
            "operation": "rerun_pipeline",
            "target": current_stage or metadata.get("expected_fail_stage", ""),
            "value": "",
            "title": "Rerun after the fix",
            "reason": "There is a recent fix attempt and the next useful step is to rerun the pipeline.",
        }

    if not current_hypothesis:
        if fault_type == "missing_permission":
            return {
                "operation": "inspect_permissions",
                "target": _pick_config_target(observation, current_stage),
                "value": "",
                "title": "Inspect permissions",
                "reason": "This fault type is usually clarified by checking file and compose permissions first.",
            }
        if surfaced_errors or current_stage or pipeline_status in {"failed", "error", "running"}:
            return {
                "operation": "tail_logs",
                "target": current_stage or metadata.get("expected_fail_stage", "") or "clone",
                "value": "",
                "title": "Tail the failing stage",
                "reason": "Start with the live tail so you can triage cheaply before spending budget on full logs.",
            }

    if current_hypothesis and not attempted_fix:
        if fault_hint:
            return {
                "operation": fault_hint["operation"],
                "target": fault_hint["target"],
                "value": fault_hint["value"],
                "title": fault_hint["title"],
                "reason": fault_hint["reason"],
            }
        return {
            "operation": "modify_config",
            "target": _pick_config_target(observation, current_stage),
            "value": "apply-targeted-config-fix",
            "title": "Apply the likely fix",
            "reason": "A hypothesis exists, so the next step is to make the matching configuration change.",
        }

    if fault_hint and attempted_fix and not metadata.get("verified_since_last_rerun"):
        return {
            "operation": fault_hint["operation"],
            "target": fault_hint["target"],
            "value": fault_hint["value"],
            "title": fault_hint["title"],
            "reason": fault_hint["reason"],
        }

    if pipeline_status == "passed":
        return {
            "operation": "verify_fix",
            "target": "",
            "value": "",
            "title": "Verify the passing build",
            "reason": "The pipeline already looks healthy, so confirm it before finalizing.",
        }

    return {
        "operation": "inspect_config",
        "target": _pick_config_target(observation, current_stage),
        "value": "",
        "title": "Inspect configuration",
        "reason": "Configuration inspection is the safest fallback when the next step is ambiguous.",
    }


def my_custom_ui(web_manager, action_fields, metadata, is_chat_env, title, quick_start_md):
    import gradio as gr

    metadata_dict = _as_dict(metadata)
    supported_operations = list(
        metadata_dict.get("supported_operations")
        or metadata_dict.get("canonical_operations")
        or CANONICAL_OPERATIONS
    )

    css = """
    .mh-shell { background: linear-gradient(180deg, #f8fafc 0%, #eef2f7 100%); }
    .mh-hero {
        border: 1px solid rgba(15, 23, 42, 0.10);
        border-radius: 22px;
        padding: 22px 24px;
        background: linear-gradient(135deg, #0f172a 0%, #1f2937 55%, #334155 100%);
        color: #f8fafc;
        box-shadow: 0 20px 40px rgba(15, 23, 42, 0.18);
        margin-bottom: 14px;
    }
    .mh-hero h1 { margin: 0; font-size: 30px; letter-spacing: -0.03em; }
    .mh-hero p { margin: 8px 0 0; max-width: 960px; line-height: 1.5; color: rgba(248, 250, 252, 0.86); }
    .mh-badge {
        display: inline-block;
        padding: 4px 10px;
        border-radius: 999px;
        background: rgba(255, 255, 255, 0.12);
        color: #f8fafc;
        font-size: 12px;
        margin-bottom: 10px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
    }
    .mh-summary-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
        gap: 12px;
        margin-bottom: 14px;
    }
    .mh-card {
        background: rgba(255, 255, 255, 0.88);
        border: 1px solid rgba(15, 23, 42, 0.10);
        border-radius: 18px;
        padding: 14px 16px;
        box-shadow: 0 8px 24px rgba(15, 23, 42, 0.05);
    }
    .mh-card-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: #64748b; }
    .mh-card-value { margin-top: 6px; font-size: 17px; font-weight: 700; color: #0f172a; word-break: break-word; }
    .mh-suggestion {
        background: linear-gradient(135deg, #fef3c7 0%, #fff7ed 100%);
        border: 1px solid rgba(180, 83, 9, 0.18);
        border-radius: 18px;
        padding: 16px 18px;
        box-shadow: 0 10px 24px rgba(180, 83, 9, 0.08);
    }
    .mh-suggestion-title { font-size: 15px; font-weight: 800; color: #7c2d12; margin-bottom: 8px; }
    .mh-suggestion-body { color: #451a03; line-height: 1.55; }
    """

    def _current_episode() -> Dict[str, Any]:
        episode = _episode_snapshot(web_manager)
        current_observation = episode.get("current_observation") or {}
        if not current_observation and isinstance(episode.get("state"), dict):
            maybe_state = episode["state"]
            if isinstance(maybe_state.get("current_observation"), dict):
                current_observation = maybe_state.get("current_observation", {})
        episode["current_observation"] = current_observation if isinstance(current_observation, dict) else {}
        return episode

    def _render(observation: Dict[str, Any], episode: Dict[str, Any]):
        observation = observation if isinstance(observation, dict) else {}
        episode = episode if isinstance(episode, dict) else {}
        suggestion = _suggest_next_action(observation)
        target_choices = _build_target_options(observation, suggestion)
        return (
            observation,
            episode,
            _build_summary_html(observation, episode, suggestion),
            _build_action_history_markdown(observation),
            _build_findings_markdown(observation),
            _summarize_stage(observation),
            gr.update(value=suggestion.get("operation", ""), choices=supported_operations),
            gr.update(value=suggestion.get("target", ""), choices=target_choices, allow_custom_value=True),
            gr.update(value=suggestion.get("value", "")),
            suggestion,
        )

    async def _refresh_from_current_state():
        episode = _current_episode()
        observation = episode.get("current_observation") or {}
        return _render(observation, episode)

    async def _reset_episode():
        observation = _as_dict(await web_manager.reset_environment())
        episode = _current_episode()
        episode["current_observation"] = observation
        return _render(observation, episode)

    async def _run_action(operation: str, target: str, value: str, current_observation: Dict[str, Any]):
        current_observation = current_observation if isinstance(current_observation, dict) else {}
        suggestion = _suggest_next_action(current_observation)
        payload = {
            "operation": _safe_text(operation, suggestion["operation"]),
            "target": _safe_text(target, suggestion["target"]),
            "value": _safe_text(value, suggestion["value"]),
        }
        if payload["operation"] not in supported_operations:
            payload["operation"] = suggestion["operation"]
        observation = _as_dict(await web_manager.step_environment(payload))
        episode = _current_episode()
        episode["current_observation"] = observation
        return _render(observation, episode)

    async def _run_suggested_action(current_observation: Dict[str, Any]):
        suggestion = _suggest_next_action(current_observation if isinstance(current_observation, dict) else {})
        observation = _as_dict(
            await web_manager.step_environment(
                {
                    "operation": suggestion["operation"],
                    "target": suggestion["target"],
                    "value": suggestion["value"],
                }
            )
        )
        episode = _current_episode()
        episode["current_observation"] = observation
        return _render(observation, episode)

    def _fill_suggestion(current_observation: Dict[str, Any]):
        suggestion = _suggest_next_action(current_observation if isinstance(current_observation, dict) else {})
        return (
            gr.update(value=suggestion["operation"]),
            gr.update(value=suggestion["target"]),
            gr.update(value=suggestion["value"]),
        )

    with gr.Blocks(elem_classes=["mh-shell"], title=title or "Meta Hackathon") as demo:
        observation_state = gr.State({})
        episode_state = gr.State({})
        suggestion_state = gr.State({})

        gr.HTML(f"<style>{css}</style>")

        gr.HTML(
            "<div class='mh-hero'>"
            f"<div class='mh-badge'>{html.escape(_safe_text(title, 'Meta Hackathon'))}</div>"
            "<h1>Testing-first episode UI with one-click action suggestions</h1>"
            "<p>This interface keeps the episode timeline, findings, pipeline status, and action builder visible at the same time. "
            "Use the suggested action button for the fastest path, or edit the fields manually when you need to test a specific branch.</p>"
            "</div>"
        )

        with gr.Row():
            with gr.Column(scale=1, min_width=360):
                gr.Markdown(quick_start_md or "### Quick Start\n1. Reset the episode.\n2. Review the suggested action.\n3. Run the suggested step or edit the fields manually.")
                reset_button = gr.Button("Reset episode", variant="primary")
                refresh_button = gr.Button("Refresh view")
                suggested_button = gr.Button("Run suggested action", variant="primary")
                fill_suggestion_button = gr.Button("Fill suggested action")
            with gr.Column(scale=2, min_width=520):
                summary_html = gr.HTML(elem_classes=["mh-suggestion"])

        with gr.Tabs():
            with gr.Tab("Action Builder"):
                with gr.Row():
                    operation = gr.Dropdown(
                        choices=supported_operations,
                        label="Operation",
                        value=supported_operations[0] if supported_operations else None,
                        allow_custom_value=True,
                    )
                    target = gr.Dropdown(
                        choices=[],
                        label="Target",
                        allow_custom_value=True,
                    )
                    value = gr.Textbox(label="Value", placeholder="Optional payload, hypothesis text, or fix label")
                step_button = gr.Button("Run manual action", variant="primary")

            with gr.Tab("Episode"):
                with gr.Row():
                    observation_json = gr.JSON(label="Current observation")
                    episode_json = gr.JSON(label="Episode state")
                with gr.Row():
                    stage_markdown = gr.Markdown()
                    findings_markdown = gr.Markdown()
                with gr.Row():
                    history_markdown = gr.Markdown()

        async def _load_view():
            results = await _refresh_from_current_state()
            return results

        reset_button.click(
            _reset_episode,
            inputs=[],
            outputs=[observation_state, episode_state, summary_html, history_markdown, findings_markdown, stage_markdown, operation, target, value, suggestion_state],
        )
        refresh_button.click(
            _refresh_from_current_state,
            inputs=[],
            outputs=[observation_state, episode_state, summary_html, history_markdown, findings_markdown, stage_markdown, operation, target, value, suggestion_state],
        )
        suggested_button.click(
            _run_suggested_action,
            inputs=[observation_state],
            outputs=[observation_state, episode_state, summary_html, history_markdown, findings_markdown, stage_markdown, operation, target, value, suggestion_state],
        )
        fill_suggestion_button.click(
            _fill_suggestion,
            inputs=[observation_state],
            outputs=[operation, target, value],
        )
        step_button.click(
            _run_action,
            inputs=[operation, target, value, observation_state],
            outputs=[observation_state, episode_state, summary_html, history_markdown, findings_markdown, stage_markdown, operation, target, value, suggestion_state],
        )
        demo.load(
            _load_view,
            inputs=[],
            outputs=[observation_state, episode_state, summary_html, history_markdown, findings_markdown, stage_markdown, operation, target, value, suggestion_state],
        )

    return demo
try:
    from ..models import MetaHackathonAction, MetaHackathonObservation
    from .environment import SimulatedCICDRepairEnvironment
except (ImportError, ModuleNotFoundError):
    from models import MetaHackathonAction, MetaHackathonObservation
    from server.environment import SimulatedCICDRepairEnvironment

logger = logging.getLogger(__name__)

_EnvClass = SimulatedCICDRepairEnvironment
logger.info("Using SimulatedCICDRepairEnvironment (no Docker/Git required)")

_SHARED_REST_ENV = _EnvClass()
_SHARED_REST_ENV.close = lambda: None  # prevent REST handlers from nuking _episode between requests

# Global reference to CI/CD API server process
_CICD_API_PROCESS = None


def _run_cicd_api_server(host: str, port: int) -> None:
    """Module-level target for multiprocessing.Process (must be picklable on Windows)."""
    try:
        import uvicorn
        from server.cicd_api import app as cicd_app

        logging.basicConfig(
            level=logging.INFO,
            format="[CI/CD API] %(levelname)s: %(message)s",
        )
        uvicorn.run(
            cicd_app,
            host=host,
            port=port,
            log_level="info",
            access_log=False,
            ws_ping_interval=30,
            ws_ping_timeout=60,
        )
    except Exception as exc:
        logging.error("CI/CD API server failed to start: %s", exc)
        sys.exit(1)


def _start_cicd_api_server(port: int = 8001, host: str = "0.0.0.0"):
    """Start the CI/CD Dynamic API server in a separate process"""
    global _CICD_API_PROCESS

    if _CICD_API_PROCESS is not None:
        logger.info("CI/CD API server already running")
        return

    # Start in separate process
    _CICD_API_PROCESS = multiprocessing.Process(
        target=_run_cicd_api_server,
        args=(host, port),
        daemon=True,
        name="CICD-API-Server",
    )
    _CICD_API_PROCESS.start()

    # Poll until the subprocess is accepting connections (up to 20 s).
    # Always probe 127.0.0.1 — 0.0.0.0 is not a valid destination on Windows.
    import requests as _req
    deadline = time.time() + 20
    healthy = False
    while time.time() < deadline:
        time.sleep(1)
        try:
            r = _req.get(f"http://127.0.0.1:{port}/health", timeout=2)
            if r.status_code == 200:
                healthy = True
                break
        except Exception:
            pass

    if healthy:
        logger.info(f"✅ CI/CD API server started successfully on http://0.0.0.0:{port}")
        logger.info(f"   API documentation: http://127.0.0.1:{port}/docs")
    else:
        logger.warning(f"⚠️  CI/CD API server did not become healthy within 20 s on port {port}")

    # Register cleanup handler
    atexit.register(_stop_cicd_api_server)


def _stop_cicd_api_server():
    """Stop the CI/CD API server process"""
    global _CICD_API_PROCESS
    
    if _CICD_API_PROCESS is not None and _CICD_API_PROCESS.is_alive():
        logger.info("Stopping CI/CD API server...")
        _CICD_API_PROCESS.terminate()
        _CICD_API_PROCESS.join(timeout=5)
        
        if _CICD_API_PROCESS.is_alive():
            logger.warning("CI/CD API server did not stop gracefully, forcing...")
            _CICD_API_PROCESS.kill()
        
        _CICD_API_PROCESS = None
        logger.info("CI/CD API server stopped")


def _shared_env_factory():
    # OpenEnv HTTP handlers call close() after every request; the no-op close above
    # preserves episode state across /reset and /step calls on the shared instance.
    return _SHARED_REST_ENV


# Create the app with the default OpenEnv playground UI
app = create_app(
    _shared_env_factory,
    MetaHackathonAction,
    MetaHackathonObservation,
    env_name="meta_hackathon",
    max_concurrent_envs=1,  # increase this number to allow more concurrent WebSocket sessions
)


# ── Middleware: normalise flat action payloads on /step ────────────────────
# Some clients send {"operation": ..., "target": ..., "value": ...} directly
# instead of the OpenEnv-required {"action": {"operation": ..., ...}}.
# This middleware rewrites the request body before FastAPI validates it so
# those clients get a 200 instead of a 422 Unprocessable Entity.

import json as _json
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as _StarletteRequest
from starlette.responses import Response as _StarletteResponse
from starlette.datastructures import Headers
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse as _JSONResponse


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok", "service": "meta-hackathon-env"}


def _build_landing_page() -> str:
        return """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Meta Hackathon CI/CD Repair Environment</title>
    <style>
        :root {
            color-scheme: light;
            --bg: #09111f;
            --panel: rgba(15, 23, 42, 0.78);
            --panel-2: rgba(30, 41, 59, 0.88);
            --text: #e2e8f0;
            --muted: #94a3b8;
            --accent: #f97316;
            --accent-2: #38bdf8;
            --border: rgba(148, 163, 184, 0.22);
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            min-height: 100vh;
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color: var(--text);
            background:
                radial-gradient(circle at top left, rgba(56, 189, 248, 0.22), transparent 30%),
                radial-gradient(circle at top right, rgba(249, 115, 22, 0.20), transparent 28%),
                linear-gradient(180deg, #050816 0%, var(--bg) 100%);
        }
        .wrap {
            max-width: 1120px;
            margin: 0 auto;
            padding: 32px 20px 48px;
        }
        .hero {
            position: relative;
            overflow: hidden;
            padding: 36px;
            border: 1px solid var(--border);
            border-radius: 28px;
            background: linear-gradient(135deg, rgba(15, 23, 42, 0.92), rgba(30, 41, 59, 0.88));
            box-shadow: 0 24px 60px rgba(2, 6, 23, 0.40);
        }
        .badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 6px 12px;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.08);
            color: #f8fafc;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            font-size: 11px;
            font-weight: 700;
        }
        h1 {
            margin: 18px 0 14px;
            font-size: clamp(34px, 5vw, 62px);
            line-height: 1.02;
            letter-spacing: -0.05em;
            max-width: 12ch;
        }
        .lede {
            max-width: 760px;
            margin: 0;
            font-size: 18px;
            line-height: 1.65;
            color: rgba(226, 232, 240, 0.82);
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
            margin-top: 18px;
        }
        .card {
            padding: 18px;
            border-radius: 20px;
            background: var(--panel);
            border: 1px solid var(--border);
            backdrop-filter: blur(14px);
        }
        .card h2 {
            margin: 0 0 10px;
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 0.10em;
            color: #f8fafc;
        }
        .card p, .card li {
            margin: 0;
            color: var(--muted);
            line-height: 1.6;
        }
        .card ul {
            margin: 0;
            padding-left: 18px;
        }
        .actions {
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            margin-top: 22px;
        }
        a.button {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 44px;
            padding: 0 16px;
            border-radius: 14px;
            text-decoration: none;
            font-weight: 700;
            border: 1px solid transparent;
            transition: transform 120ms ease, border-color 120ms ease, background 120ms ease;
        }
        a.button:hover { transform: translateY(-1px); }
        .primary {
            color: #0f172a;
            background: linear-gradient(135deg, #fde68a, #fb923c);
        }
        .secondary {
            color: var(--text);
            background: rgba(255, 255, 255, 0.06);
            border-color: var(--border);
        }
        .footer {
            margin-top: 16px;
            color: var(--muted);
            font-size: 13px;
        }
        code {
            padding: 2px 6px;
            border-radius: 8px;
            background: rgba(148, 163, 184, 0.14);
            color: #f8fafc;
        }
    </style>
</head>
<body>
    <main class="wrap">
        <section class="hero">
            <div class="badge">Meta Hackathon · CI/CD repair lab</div>
            <h1>Something should be visible here.</h1>
            <p class="lede">
                This Space exposes a full repair environment, but the landing page should not be blank.
                Use the controls below to inspect the API, check health, and jump into the interactive environment.
            </p>
            <div class="actions">
                <a class="button primary" href="/docs">Open API docs</a>
                <a class="button secondary" href="/health">Check health</a>
                <a class="button secondary" href="/">Open interactive UI</a>
            </div>
            <div class="grid">
                <article class="card">
                    <h2>What this is</h2>
                    <p>A deterministic benchmark for diagnosing and repairing broken CI/CD pipelines.</p>
                </article>
                <article class="card">
                    <h2>First step</h2>
                    <p>Reset an episode, inspect the surfaced logs, and apply the smallest safe fix.</p>
                </article>
                <article class="card">
                    <h2>Visible endpoints</h2>
                    <ul>
                        <li><code>GET /health</code></li>
                        <li><code>POST /api/workspace/create</code></li>
                        <li><code>WS /api/ws/{workspace_id}</code></li>
                    </ul>
                </article>
            </div>
            <div class="footer">If you still see a blank page, the backend is up and this route is the right place to diagnose next.</div>
        </section>
    </main>
</body>
</html>"""


@app.get("/", include_in_schema=False)
async def landing_page() -> HTMLResponse:
        return HTMLResponse(_build_landing_page())


@app.get("/web", include_in_schema=False)
@app.get("/web/", include_in_schema=False)
async def web_alias() -> HTMLResponse:
        # HF Spaces mount the app under /web, so serve a real landing page there.
        return HTMLResponse(_build_landing_page())


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: _StarletteRequest, exc: RequestValidationError):
    """Log 422 validation errors with the offending body so they are visible in server logs."""
    try:
        body = await request.body()
        body_preview = body.decode("utf-8", errors="replace")[:500]
    except Exception:
        body_preview = "<unreadable>"
    logger.error(
        "422 Unprocessable Entity on %s %s — errors=%s — body=%s",
        request.method,
        request.url.path,
        exc.errors(),
        body_preview,
    )
    return _JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "body_preview": body_preview},
    )


class _FlatActionNormalizerMiddleware(BaseHTTPMiddleware):
    """Wrap flat action dicts in {"action": ...} for /step requests."""

    _ACTION_FIELDS = frozenset({"operation", "target", "value"})

    async def dispatch(self, request: _StarletteRequest, call_next):
        if request.method == "POST" and "/step" in request.url.path:
            try:
                body_bytes = await request.body()
                if body_bytes:
                    body = _json.loads(body_bytes)
                    # Rewrite only when the payload is a flat action dict
                    # (has at least "operation" but no top-level "action" key).
                    if (
                        isinstance(body, dict)
                        and "action" not in body
                        and self._ACTION_FIELDS.intersection(body)
                    ):
                        body = {"action": body}
                        body_bytes = _json.dumps(body).encode()

                        # Rebuild the request with the patched body so FastAPI
                        # sees the correct payload. receive() must return an
                        # ASGI http.request message dict, not raw bytes.
                        async def _patched_receive(
                            _b=body_bytes,
                        ):
                            return {"type": "http.request", "body": _b, "more_body": False}

                        request = _StarletteRequest(
                            scope=request.scope,
                            receive=_patched_receive,
                        )
            except Exception:
                pass  # Let FastAPI handle malformed JSON normally

        return await call_next(request)


app.add_middleware(_FlatActionNormalizerMiddleware)


# Add startup event to launch CI/CD API server
@app.on_event("startup")
async def startup_event():
    """Start the CI/CD API server when main server starts"""
    import asyncio
    cicd_api_port = int(os.getenv("CICD_API_PORT", "8001"))
    cicd_api_host = os.getenv("CICD_API_HOST", "0.0.0.0")

    logger.info("=" * 60)
    logger.info("Starting Meta Hackathon Environment Server")
    logger.info("=" * 60)

    # Initialise SQLite DBs at runtime so they exist on HF Spaces (ephemeral FS,
    # no Docker build step runs there).
    try:
        from server.agent_memory import _init_db as _init_agent_memory
        _init_agent_memory()
        from server.curriculum import _conn as _curriculum_conn
        _curriculum_conn().close()
        logger.info("DB ready: %s", os.getenv("AGENT_MEMORY_DB_PATH", "<default>"))
    except Exception as exc:
        logger.error("DB init failed: %s", exc)

    # Run in thread pool so the sync polling loop doesn't block the event loop
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _start_cicd_api_server, cicd_api_port, cicd_api_host)

    logger.info("=" * 60)
    logger.info("All services started successfully")
    logger.info("=" * 60)


@app.on_event("shutdown")
async def shutdown_event():
    """Stop the CI/CD API server when main server stops"""
    logger.info("Shutting down services...")
    _stop_cicd_api_server()


def main():
    """
    Entry point for direct execution via uv run or python -m.

    This function enables running the server without Docker:
        uv run --project . server
        uv run --project . server --port 8001
        python -m meta_hackathon.server.app

    For production deployments, consider using uvicorn directly with
    multiple workers:
        uvicorn meta_hackathon.server.app:app --workers 4
        
    The CI/CD Dynamic API server will automatically start on port 8001
    (or CICD_API_PORT environment variable).
    """
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--cicd-api-port", type=int, default=None,
                       help="Port for CI/CD API server (default: 8001)")
    args = parser.parse_args()
    
    # Set CI/CD API port from args if provided
    if args.cicd_api_port:
        os.environ["CICD_API_PORT"] = str(args.cicd_api_port)

    print("\n" + "=" * 60)
    print("Meta Hackathon CI/CD Environment - Theme #3 Compliant")
    print("=" * 60)
    print(f"Main Server: http://{args.host}:{args.port}")
    print(f"CI/CD API: http://{args.host}:{os.getenv('CICD_API_PORT', '8001')}")
    print(f"API Docs: http://{args.host}:{os.getenv('CICD_API_PORT', '8001')}/docs")
    print("=" * 60 + "\n")

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
