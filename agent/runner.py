"""Orchestration loop for the agentic inference baseline."""

import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import requests

from .actions import (
    normalize_model_action,
    pre_finalize_guard_action,
    progression_guard_action,
    ready_to_finalize,
)
from .config import (
    API_BASE_URL,
    API_KEY,
    BENCHMARK,
    CICD_API_WS_URL,
    INFERENCE_VERBOSE,
    MAX_CONSECUTIVE_TOOL_CALL_MISSES,
    MAX_MODEL_CALLS_PER_TASK,
    MAX_STEPS,
    MIN_MODEL_CALLS_BEFORE_STRICT_FAIL,
    MODEL_NAME,
    NUM_EPISODES,
    SUCCESS_SCORE_THRESHOLD,
    TASK_ORDER,
    USE_WS_API,
    get_openai_client_kwargs,
)
from .http_environment import format_obs_for_llm, reset_env, step_env, trim_messages
from .model_client import get_model_action
from .prompts import build_system_prompt
from .trajectory_logging import log_detail, log_end, log_memory, log_start, log_step

try:
    from ..models import MetaHackathonObservation
except ImportError:  # pragma: no cover - direct script execution
    from models import MetaHackathonObservation

try:
    from server.agent_memory import fingerprint, recall, remember, remember_optimal_path, recall_optimal_path
except ImportError:  # pragma: no cover - direct script execution
    try:
        from .server.agent_memory import fingerprint, recall, remember, remember_optimal_path, recall_optimal_path  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - memory is optional
        fingerprint = None  # type: ignore[assignment]
        recall = None  # type: ignore[assignment]
        remember = None  # type: ignore[assignment]
        remember_optimal_path = None  # type: ignore[assignment]
        recall_optimal_path = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from openai import OpenAI


def _normalize_hypothesis(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


# Structured fix hint injected after hypothesis is accepted — one variant per tool schema.
_STRUCTURED_FIX_HINT = (
    "Hypothesis accepted. Now apply the fix using modify_config with a structured JSON value:\n"
    '{"file": "<path/to/file>", "action": "replace", "old": "<exact broken lines>", "new": "<fixed lines>"}\n'
    "Use the file content you already inspected to fill in the exact old/new strings. "
    "Do not inspect or view_logs again — go straight to the fix."
)

_STRUCTURED_FIX_HINT_WS = (
    "Hypothesis accepted. Now apply the fix using write_file.\n"
    "You MUST supply BOTH arguments — path AND content:\n"
    '  path    = "<the exact relative file path you read, e.g. tests/test_api.py>"\n'
    '  content = "<complete new file content — the entire file, not just the changed lines>"\n'
    "Do NOT call write_file without a path. "
    "Do NOT trigger_pipeline until write_file succeeds."
)


def _extract_primary_surfaced_error_file(observation: MetaHackathonObservation) -> str:
    patterns = [
        r" in ([^:]+):\d+:",
        r"\b((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|ya?ml|txt|env))\b",
        r"\b(Dockerfile)\b",
    ]
    for err in observation.surfaced_errors or []:
        text = str(err)
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip().strip(".,)")
    return ""


def _memory_hint(errors: List[str], fault_type: str) -> Tuple[str, str]:
    if not errors or recall is None:
        return "", ""
    suggestion = recall(errors, fault_type=fault_type)
    if not suggestion.get("suggested_fix"):
        return "", ""
    confidence = float(suggestion.get("confidence", 0.0) or 0.0)
    times_seen = int(suggestion.get("times_seen", 0) or 0)
    fix_text = str(suggestion.get("suggested_fix", "")).strip()[:300]  # cap to avoid context overflow
    hint = (
        f"[MEMORY] High-confidence fix from a prior episode (success rate: {confidence:.0%}, seen {times_seen}x):\n"
        f"  Apply this fix FIRST before any inspection — skip view_logs and inspect_config:\n"
        f"  {fix_text}\n"
        f"  After applying, call rerun_pipeline immediately."
    )
    memory_log = str(suggestion.get("memory_log", "") or "")
    return hint, memory_log


def _fault_type_from_observation(observation: MetaHackathonObservation, fallback: str = "unknown") -> str:
    metadata = observation.metadata if isinstance(observation.metadata, dict) else {}
    if isinstance(metadata, dict) and metadata.get("fault_type"):
        return str(metadata.get("fault_type"))
    if observation.task_id and observation.task_id.startswith("real_"):
        return observation.task_id[len("real_") :]
    return fallback


def _repetition_escape_action(
    observation: MetaHackathonObservation,
    action_history: List[Tuple[str, str, str]],
    repeated_action: Tuple[str, str, str],
) -> Tuple[str, str, str]:
    stage = observation.current_stage or "build"
    repeated_op, repeated_target, _ = repeated_action
    repeated_text = "|".join(repeated_action).lower()

    # Build a broad candidate pool — prefer actions that surface new information
    # rather than immediately jumping to a different fix target.
    candidates: List[Tuple[str, str, str]] = []

    # If the agent is stuck on a fix, first make it re-read the error output so
    # the model can reason about the actual failure before trying something else.
    if repeated_op in {"modify_config", "add_dependency", "modify_dockerfile"}:
        candidates.extend(
            [
                ("view_logs", stage, ""),
                ("view_logs", "build", ""),
                ("view_logs", "test", ""),
            ]
        )

    if "requirements" in repeated_text or stage == "build":
        candidates.extend(
            [
                ("inspect_config", "services/api/requirements.txt", ""),
                ("inspect_config", "requirements.txt", ""),
                ("inspect_config", "Dockerfile", ""),
            ]
        )

    candidates.extend(
        [
            ("view_logs", stage, ""),
            ("inspect_config", "docker-compose.yml", ""),
            ("inspect_dockerfile", "build", ""),
            ("inspect_config", "services/api/Dockerfile", ""),
            ("view_logs", "build", ""),
        ]
    )

    for candidate in candidates:
        if candidate != repeated_action and candidate not in action_history:
            return candidate
    # Last resort: view logs for current stage even if seen before
    return ("view_logs", stage, "")


def _repetition_escape_action_ws(
    action_history: List[Tuple[str, str, str]],
    repeated_action: Tuple[str, str, str],
) -> Tuple[str, str, str]:
    """WS-mode equivalent of _repetition_escape_action — uses WS tool names."""
    repeated_op = repeated_action[0]
    candidates: List[Tuple[str, str, str]] = []

    if repeated_op == "write_file":
        candidates.extend([
            ("trigger_pipeline", "", ""),
            ("read_file", "Dockerfile", ""),
            ("read_file", "docker-compose.yml", ""),
        ])

    candidates.extend([
        ("trigger_pipeline", "", ""),
        ("read_file", "Dockerfile", ""),
        ("read_file", "docker-compose.yml", ""),
        ("read_file", "services/api/requirements.txt", ""),
        ("list_files", "", ""),
    ])

    for candidate in candidates:
        if candidate != repeated_action and candidate not in action_history:
            return candidate
    return ("trigger_pipeline", "", "")


def _step_rationale(operation: str, target: str, value: str, reward: float) -> str:
    """Produce a one-sentence explanation of why this step was useful."""
    if operation == "inspect_config":
        return f"Inspect '{target}' to read current file content and spot the fault."
    if operation == "view_logs":
        return f"View '{target}' stage logs to identify which error is blocking the pipeline."
    if operation == "inspect_dockerfile":
        return "Read Dockerfile to check layer ordering and COPY/RUN sequencing."
    if operation == "inspect_permissions":
        return "Check file/directory permissions that may block deploy or runtime."
    if operation == "set_hypothesis":
        verdict = "correct hypothesis" if reward > 0 else "incorrect hypothesis"
        return f"Formulate {verdict} about root cause before attempting a fix."
    if operation in {"modify_config", "add_dependency"}:
        return f"Apply fix to '{target}' — change the broken configuration or dependency."
    if operation == "rerun_pipeline":
        return "Rerun the pipeline to verify whether the applied fix resolved the failure."
    if operation == "verify_fix":
        return "Confirm the pipeline passed and the incident is resolved before finalising."
    if operation == "finalize":
        return "Finalise the episode after the fix is verified."
    return f"Execute {operation} step."


def _build_optimal_path(
    step_trace: List[Dict[str, Any]],
    resolved: bool,
) -> List[Dict[str, Any]]:
    """Distil the full step trace into the positive-reward steps worth teaching.

    Only steps with reward >= 0 are kept, and only the sequence up to and
    including the first 'finalize' (or the last step if there is none).
    This strips wasted/redundant moves so the stored path is a clean template.
    """
    if not resolved or not step_trace:
        return []
    positive = [s for s in step_trace if s["reward"] >= 0]
    # Truncate at finalize
    result: List[Dict[str, Any]] = []
    for s in positive:
        result.append(s)
        if s["operation"] == "finalize":
            break
    return result


def run_task(client: "OpenAI", session: requests.Session, fallback_task_name: str) -> Tuple[str, bool, int, float]:
    history: List[str] = []
    rewards: List[float] = []
    action_history: List[Tuple[str, str, str]] = []
    attempted_hypotheses: set[str] = set()
    forced_messages: List[str] = []
    inspected_config_targets: set[str] = set()
    injected_guardrails: set[str] = set()  # deduplicate guardrail messages
    fix_applied_since_rerun: bool = False   # allow rerun after a new fix
    hypothesis_accepted: bool = False       # True once set_hypothesis scores > 0
    steps_taken = 0
    score = 0.0
    deterministic_score = 0.0
    rubric_score = 0.0
    rubric_judge_used = False
    success = False
    resolved = False
    task_name = fallback_task_name
    tool_call_misses = 0
    model_calls_used = 0
    observation: Optional[MetaHackathonObservation] = None
    messages: List[Dict[str, Any]] = []
    task_max_steps = MAX_STEPS
    initial_surfaced_errors: List[str] = []
    last_fix_value: str = ""
    errors_at_last_fix: List[str] = []   # errors active when the last fix was applied
    last_memory_key: str = ""
    fault_type_for_memory: str = ""
    # Structured trace of every step taken — used to record the optimal path.
    step_trace: List[Dict[str, Any]] = []

    try:
        observation = reset_env(session)
        observed = observation.metadata or {}
        if isinstance(observed, dict) and observed.get("task_key"):
            task_name = str(observed.get("task_key"))
        if isinstance(observed, dict) and observed.get("max_steps"):
            task_max_steps = max(task_max_steps, int(observed.get("max_steps", MAX_STEPS)))
        initial_surfaced_errors = [str(item) for item in (observation.surfaced_errors or [])]
        last_memory_key = fingerprint(initial_surfaced_errors) if fingerprint is not None else ""

        messages = [{"role": "system", "content": build_system_prompt(task_name)}]

        log_start(task=task_name, env=BENCHMARK, model=MODEL_NAME)
        if INFERENCE_VERBOSE:
            log_detail(
                step=0,
                action="reset",
                observation=observation,
                reward=float(observation.reward or 0.0),
                done=bool(observation.done),
                error=None,
            )

        task_title = observation.task_title or task_name
        fault_type = _fault_type_from_observation(observation, task_name)
        fault_type_for_memory = fault_type
        _meta = observation.metadata if isinstance(observation.metadata, dict) else {}
        print(
            f"[EPISODE START] label={fallback_task_name}  fault={fault_type}  "
            f"stage={_meta.get('expected_fail_stage', '?')}  "
            f"difficulty={observation.difficulty or '?'}  "
            f"max_steps={task_max_steps}",
            flush=True,
        )
        memory_hint, memory_log = _memory_hint(initial_surfaced_errors, fault_type)
        if memory_log:
            log_memory(memory_log)

        # Inject optimal-path hint from a prior successful episode for this fault type.
        optimal_path_hint = ""
        if recall_optimal_path is not None:
            prior_path = recall_optimal_path(fault_type)
            if prior_path:
                mem = recall(initial_surfaced_errors, fault_type) if recall is not None else {}
                _path_confidence_high = float(mem.get("historical_success_rate", 0.0)) >= 1.0
                path_lines = "\n".join(
                    f"  {i+1}. [{s['operation']}] target={s.get('target','') or '—'}  "
                    f"{'value=' + repr(s['value'][:60]) + '  ' if s.get('value') else ''}"
                    f"→ {s.get('rationale', '')}"
                    for i, s in enumerate(prior_path)
                )
                if _path_confidence_high:
                    optimal_path_hint = (
                        f"HIGH-CONFIDENCE memory hit for fault_type={fault_type} (100% success rate).\n"
                        "Skip view_logs, inspect_config, and inspect_dockerfile — go directly to:\n"
                        f"{path_lines}\n"
                        "Do NOT inspect files first. Apply the fix, rerun_pipeline, verify_fix, finalize."
                    )
                else:
                    optimal_path_hint = (
                        f"Optimal path learned from a prior successful episode for fault_type={fault_type}:\n"
                        f"{path_lines}\n"
                        "Follow this sequence closely — it resolved the incident efficiently."
                    )

        task_intro = f"Task: {task_title}\n\n{format_obs_for_llm(observation, 0)}"
        if memory_hint:
            task_intro += f"\n\n{memory_hint}"
        if optimal_path_hint:
            task_intro += f"\n\n{optimal_path_hint}"
        messages.append(
            {
                "role": "user",
                "content": task_intro + "\n\nBegin debugging.",
            }
        )

        for step in range(1, task_max_steps + 1):
            if observation.done:
                break

            if forced_messages:
                for reminder in forced_messages:
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Guardrail: {reminder}",
                        }
                    )
                forced_messages.clear()
                messages = trim_messages(messages)

            operation = ""
            target = ""
            value = ""
            assistant_message: Dict[str, Any] = {
                "role": "assistant",
                "content": "",
            }
            tool_call_id: Optional[str] = None

            _model_failed = False
            guard_attempts = 0
            try:
              while True:
                guard_attempts += 1

                operation, target, value, assistant_message, tool_call_id = get_model_action(
                    client=client,
                    step=step,
                    messages=messages,
                )
                model_calls_used += 1
                if model_calls_used > MAX_MODEL_CALLS_PER_TASK:
                    raise RuntimeError(
                        "Model call budget exceeded while strict tool-calling is enabled. "
                        "Increase MAX_MODEL_CALLS_PER_TASK or use a model/provider with reliable tool calls."
                    )

                if tool_call_id is None:
                    tool_call_misses += 1
                    messages.append(
                        {
                            "role": "user",
                            "content": "Missing tool call. Return one valid tool call.",
                        }
                    )
                    if (
                        tool_call_misses >= MAX_CONSECUTIVE_TOOL_CALL_MISSES
                        and model_calls_used >= MIN_MODEL_CALLS_BEFORE_STRICT_FAIL
                    ):
                        raise RuntimeError(
                            "Model failed to emit required tool calls repeatedly. "
                            "Strict tool-calling is mandatory and fallback is disabled."
                        )
                    if guard_attempts < MAX_CONSECUTIVE_TOOL_CALL_MISSES:
                        messages = trim_messages(messages)
                        continue
                    raise RuntimeError(
                        "Model response missing tool call after retries in strict mode."
                    )
                tool_call_misses = 0

                operation, target, value = normalize_model_action(
                    operation=operation,
                    target=target,
                    value=value,
                    step=step,
                )

                if operation == "finalize" and not ready_to_finalize(observation):
                    operation, target, value = pre_finalize_guard_action(observation)
                    assistant_message = {
                        "role": "assistant",
                        "content": f"Guarded: {operation}|{target}|{value}",
                    }
                    tool_call_id = None

                guarded_progression = progression_guard_action(observation, history, operation)
                if guarded_progression is not None:
                    operation, target, value = guarded_progression
                    assistant_message = {
                        "role": "assistant",
                        "content": f"Progression guard: {operation}|{target}|{value}",
                    }
                    tool_call_id = None

                should_resample = False
                surfaced_file = _extract_primary_surfaced_error_file(observation)

                # Critical guardrails only - reduced verbosity
                _sf_key = f"sf:{surfaced_file}"
                if (
                    surfaced_file
                    and surfaced_file not in inspected_config_targets
                    and (operation != "inspect_config" or target != surfaced_file)
                    and _sf_key not in injected_guardrails
                ):
                    injected_guardrails.add(_sf_key)
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Inspect '{surfaced_file}' first.",
                        }
                    )
                    should_resample = True

                if operation == "set_hypothesis":
                    normalized_hypothesis = _normalize_hypothesis(value)
                    if normalized_hypothesis and normalized_hypothesis in attempted_hypotheses:
                        _dup_hyp_key = f"dup_hyp:{normalized_hypothesis}"
                        if _dup_hyp_key not in injected_guardrails:
                            injected_guardrails.add(_dup_hyp_key)
                            messages.append(
                                {
                                    "role": "user",
                                    "content": "That hypothesis failed. Try a different root cause.",
                                }
                            )
                        should_resample = True

                action_tuple = (operation, target, value)
                # Allow rerun_pipeline again if a new fix was applied since the last rerun
                rerun_blocked = (
                    action_tuple in action_history
                    and not (operation == "rerun_pipeline" and fix_applied_since_rerun)
                )
                if rerun_blocked:
                    _dup_act_key = f"dup_act:{operation}:{target}"
                    if _dup_act_key not in injected_guardrails:
                        injected_guardrails.add(_dup_act_key)
                        messages.append(
                            {
                                "role": "user",
                                "content": "Action already tried. Choose different operation or target.",
                            }
                        )
                    should_resample = True

                if should_resample and guard_attempts < 2:  # reduced from 3 to 2
                    messages = trim_messages(messages)
                    continue

                break
            except RuntimeError as _model_exc:
                _model_failed = True
                log_step(
                    step=step,
                    action="[model_failure]||",
                    reward=0.0,
                    done=True,
                    error=str(_model_exc),
                    llm_thought="",
                )

            if _model_failed:
                break

            action_tuple = (operation, target, value)
            # Allow harder episodes one extra attempt before forcing an escape.
            difficulty_str = (observation.difficulty or "").lower()
            repetition_threshold = 3 if difficulty_str in ["hard", "security"] else 2
            if action_history.count(action_tuple) >= repetition_threshold:
                operation, target, value = _repetition_escape_action(observation, action_history, action_tuple)
                assistant_message = {
                    "role": "assistant",
                    "content": (
                        f"Repetition guard triggered: forcing a different action after {repetition_threshold} identical attempts "
                        f"-> {operation}|{target}|{value}"
                    ),
                }
                tool_call_id = None

            # Capture errors active RIGHT BEFORE this fix so memory maps
            # the error pattern that prompted the fix, not the initial errors.
            pre_fix_errors: List[str] = []
            if operation in {"modify_config", "add_dependency"} and value:
                pre_fix_errors = [str(e) for e in (observation.surfaced_errors or [])]

            try:
                observation, reward, done, error = step_env(
                    session,
                    operation=operation,
                    target=target,
                    value=value,
                )
            except Exception as exc:
                reward = -0.25
                done = True
                error = str(exc)
                # Keep the previous observation to preserve end-state safety.

            rewards.append(reward)
            steps_taken = step
            llm_thought = assistant_message.get("content", "") if assistant_message else ""
            if operation in {"modify_config", "add_dependency"} and value:
                last_fix_value = value
                if pre_fix_errors:
                    errors_at_last_fix = pre_fix_errors

            action_text = f"{operation}|{target}|{value}"
            log_step(step=step, action=action_text, reward=reward, done=done, error=error, llm_thought=llm_thought)
            if INFERENCE_VERBOSE:
                log_detail(
                    step=step,
                    action=action_text,
                    observation=observation,
                    reward=reward,
                    done=done,
                    error=error,
                )
            history.append(f"{action_text} -> reward {reward:+.2f}")
            action_history.append((operation, target, value))
            step_trace.append({
                "operation": operation,
                "target": target,
                "value": value[:120] if value else "",
                "reward": round(reward, 3),
                "rationale": _step_rationale(operation, target, value, reward),
            })
            if operation == "inspect_config" and target:
                inspected_config_targets.add(target)
                # Re-enable surfaced-file guardrail if a new file appears later
                injected_guardrails.discard(f"sf:{target}")
                injected_guardrails.discard(f"hyp_sf:{target}")
            if operation in {"modify_config", "add_dependency"} and value:
                fix_applied_since_rerun = True
                fix_note = str(observation.findings[-1]) if observation.findings else ""
                fix_succeeded = "Fix applied" in fix_note or reward >= 0.0
                if fix_succeeded and not observation.incident_resolved and not done:
                    forced_messages.append(
                        "Fix applied. Call rerun_pipeline NOW to verify."
                    )
            if operation == "rerun_pipeline":
                fix_applied_since_rerun = False
            if operation == "set_hypothesis":
                normalized_hypothesis = _normalize_hypothesis(value)
                if normalized_hypothesis:
                    attempted_hypotheses.add(normalized_hypothesis)
                if reward > 0:
                    hypothesis_accepted = True
                    if "fix_hint_sent" not in injected_guardrails:
                        injected_guardrails.add("fix_hint_sent")
                        forced_messages.append(_STRUCTURED_FIX_HINT)
                elif reward < 0:
                    forced_messages.append(
                        "Hypothesis incorrect. Re-read surfaced_errors and try different root cause."
                    )

            messages.append(assistant_message)
            tool_result = format_obs_for_llm(observation, step)
            observation_errors = [str(item) for item in (observation.surfaced_errors or [])]
            current_memory_key = fingerprint(observation_errors) if fingerprint is not None else ""
            observation_fault_type = _fault_type_from_observation(observation, task_name)
            memory_hint, memory_log = (
                _memory_hint(observation_errors, observation_fault_type)
                if current_memory_key != last_memory_key
                else ("", "")
            )
            if memory_log:
                log_memory(memory_log)
            if memory_hint:
                tool_result = f"{tool_result}\n\n{memory_hint}"
                last_memory_key = current_memory_key
            if tool_call_id:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": tool_result,
                    }
                )
            else:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Observation update:\n{tool_result}\n\n"
                            "Choose one tool call for the next action."
                        ),
                    }
                )
            messages = trim_messages(messages)

            if done:
                break

        # Runner exhausted step budget without agent calling finalize — force one to trigger scoring.
        if observation is not None and not observation.done:
            try:
                observation, reward, done, error = step_env(
                    session,
                    operation="finalize",
                    target="",
                    value="",
                )
                rewards.append(reward)
                log_step(
                    step=steps_taken + 1,
                    action="finalize||",
                    reward=reward,
                    done=done,
                    error=error,
                    llm_thought="[forced finalize: step budget exhausted]",
                )
            except Exception as exc:
                pass  # best-effort; scoring may be partial

        if observation is not None:
            score = float(observation.final_score)
            deterministic_score = float(observation.deterministic_score)
            rubric_score = float(observation.rubric_score)
            rubric_judge_used = bool(observation.rubric_judge_used)
            resolved = bool(observation.incident_resolved)
        success = resolved and score >= SUCCESS_SCORE_THRESHOLD
        # Use the errors that were active when the fix was applied, not the initial errors.
        # This prevents multi-fault episodes from poisoning the memory: e.g. a dep_conflict
        # episode that also had log_path would previously store "dep errors → log_path fix",
        # teaching the wrong fix for dep errors in future episodes.
        memory_errors = errors_at_last_fix if errors_at_last_fix else initial_surfaced_errors
        if remember is not None and memory_errors and last_fix_value:
            try:
                remember(memory_errors, last_fix_value, success)
            except Exception:
                pass
        if remember_optimal_path is not None and resolved and fault_type_for_memory:
            try:
                optimal = _build_optimal_path(step_trace, resolved)
                if optimal:
                    remember_optimal_path(fault_type_for_memory, optimal)
                    print(
                        f"[MEMORY] Stored optimal path for fault_type={fault_type_for_memory} "
                        f"({len(optimal)} steps)",
                        flush=True,
                    )
            except Exception:
                pass
    except Exception as _episode_exc:
        import traceback as _tb
        print(
            f"\n[EPISODE ERROR] {type(_episode_exc).__name__}: {_episode_exc}",
            flush=True,
        )
        _tb.print_exc()
        raise
    finally:
        log_end(
            success=success,
            steps=steps_taken,
            score=score,
            resolved=resolved,
            rewards=rewards,
            deterministic_score=deterministic_score,
            rubric_score=rubric_score,
            rubric_judge_used=rubric_judge_used,
        )
        if INFERENCE_VERBOSE:
            print(
                "[DETAIL] "
                f"task={task_name} success={str(success).lower()} steps={steps_taken} "
                f"final_score={score:.3f} det={deterministic_score:.3f} "
                f"rubric={rubric_score:.3f} judge={str(rubric_judge_used).lower()} "
                f"resolved={str(resolved).lower()}",
                flush=True,
            )

    return task_name, success, steps_taken, score


def run_task_ws(client: "OpenAI", episode_label: str) -> Tuple[str, bool, int, float]:
    """WS-mode episode runner — full feature parity with run_task.

    Creates a workspace on the CI/CD API, opens a persistent WebSocket
    connection, and dispatches each LLM tool call directly to the API
    via execute_tool(). Does NOT call /reset or /step on port 8000.

    Returns (task_name, success, steps_taken, score).
    """
    import textwrap as _tw
    from .api_client import create_ws_client, execute_tool, format_tool_result

    # ── Select fault type via curriculum UCB1 ────────────────────────────────
    try:
        from server.curriculum import CurriculumController
        _curriculum = CurriculumController()
        selected_fault = _curriculum.select_fault_type()
        curriculum_difficulty = _curriculum.get_difficulty()
    except Exception as exc:
        print(f"[WS-MODE] Curriculum unavailable ({exc}), using random fault", flush=True)
        import random
        from cicd.fault_types import FAULT_TYPES
        selected_fault = random.choice(FAULT_TYPES)
        curriculum_difficulty = 0.5

    # ── Bootstrap workspace via REST (one-time per episode) ─────────────────
    import requests as _req
    cicd_rest = CICD_API_WS_URL.replace("ws://", "http://").replace("wss://", "https://")
    try:
        resp = _req.post(
            f"{cicd_rest}/api/workspace/create",
            json={"fault_type": selected_fault},
            timeout=30,
        )
        resp.raise_for_status()
        ws_resp = resp.json()
        workspace_id = ws_resp["workspace_id"]
        fault_injected = ws_resp.get("fault_injected") or selected_fault
        initial_failure_logs = ws_resp.get("initial_failure_logs") or ""
    except Exception as exc:
        print(f"[WS-MODE] Failed to create workspace: {exc}", flush=True)
        return episode_label, False, 0, 0.0

    task_name = fault_injected if fault_injected != "unknown" else episode_label
    success = False
    steps_taken = 0
    score = 0.0
    rewards: List[float] = []
    history: List[str] = []
    action_history: List[Tuple[str, str, str]] = []
    attempted_hypotheses: set[str] = set()
    forced_messages: List[str] = []
    injected_guardrails: set[str] = set()
    fix_applied_since_rerun: bool = False
    hypothesis_accepted: bool = False
    pipeline_passed = False
    pipeline_triggered_once: bool = False  # tracks whether agent has seen failure output
    model_calls_used = 0
    tool_call_misses = 0
    last_fix_value: str = ""
    step_trace: List[Dict[str, Any]] = []
    task_max_steps = MAX_STEPS
    read_files: set[str] = set()  # tracks paths that have been read_file'd this episode

    log_start(task=task_name, env=BENCHMARK, model=MODEL_NAME)
    print(
        f"[EPISODE START] workspace={workspace_id}  fault={fault_injected}  "
        f"difficulty={curriculum_difficulty:.2f}  max_steps={task_max_steps}",
        flush=True,
    )

    # ── Memory: recall fix hint and optimal path ──────────────────────────────
    memory_hint, memory_log = _memory_hint([fault_injected], fault_injected) if fault_injected != "unknown" else ("", "")
    if memory_log:
        log_memory(memory_log)

    optimal_path_hint = ""
    memory_confidence_high = False
    if recall_optimal_path is not None and fault_injected != "unknown":
        prior_path = recall_optimal_path(fault_injected)
        if prior_path:
            # Check if the stored fix has a 1.0 success rate in the recall memory.
            mem = recall([fault_injected], fault_injected) if recall is not None else {}
            memory_confidence_high = float(mem.get("historical_success_rate", 0.0)) >= 1.0

            path_lines = "\n".join(
                f"  {i+1}. [{s['operation']}] target={s.get('target','') or '—'}  "
                f"{'value=' + repr(s['value'][:60]) + '  ' if s.get('value') else ''}"
                f"→ {s.get('rationale', '')}"
                for i, s in enumerate(prior_path)
            )
            if memory_confidence_high:
                optimal_path_hint = (
                    f"HIGH-CONFIDENCE memory hit for fault_type={fault_injected} (100% success rate).\n"
                    "Skip list_files and all read_file steps — go directly to the fix using this exact sequence:\n"
                    f"{path_lines}\n"
                    "Do NOT read Dockerfile, docker-compose.yml, or requirements.txt first. "
                    "Apply the write_file fix, trigger_pipeline, then finalize."
                )
            else:
                optimal_path_hint = (
                    f"Optimal path learned from a prior successful episode for fault_type={fault_injected}:\n"
                    f"{path_lines}\n"
                    "Follow this sequence closely — it resolved the incident efficiently."
                )
            print(f"[MEMORY] Recalled optimal path for fault_type={fault_injected} ({len(prior_path)} steps)", flush=True)

    if initial_failure_logs:
        # Incident alert already available — agent does not need a discovery pipeline run.
        pipeline_triggered_once = True
        task_intro = (
            f"You are debugging a CI/CD pipeline in workspace {workspace_id}.\n"
            "An incident was detected. The pipeline has already been run and the failure "
            "logs are included below as your incident alert.\n\n"
            "Read ONLY the file the failure output names, set your hypothesis, apply the fix, "
            "then call trigger_pipeline once to verify. Call finalize when the pipeline passes.\n\n"
            f"=== INCIDENT ALERT — PIPELINE FAILURE LOGS ===\n{initial_failure_logs}\n"
            "=== END OF INCIDENT ALERT ==="
        )
    else:
        task_intro = (
            f"You are debugging a CI/CD pipeline in workspace {workspace_id}.\n"
            "Start by calling trigger_pipeline to see the current failure logs. "
            "Read ONLY the file the failure output names, diagnose the fault, apply a fix, "
            "and re-run the pipeline until it passes. Call finalize when the pipeline passes."
        )
    if memory_hint:
        task_intro += f"\n\n{memory_hint}"
    if optimal_path_hint:
        task_intro += f"\n\n{optimal_path_hint}"

    messages: List[Dict[str, Any]] = [{"role": "system", "content": build_system_prompt(task_name, ws_mode=True)}]
    messages.append({"role": "user", "content": task_intro + "\n\nBegin debugging."})

    with create_ws_client(workspace_id, base_url=CICD_API_WS_URL) as ws:
        for step in range(1, task_max_steps + 1):
            # Inject forced guardrail messages before the model call
            if forced_messages:
                for reminder in forced_messages:
                    messages.append({"role": "user", "content": f"Guardrail: {reminder}"})
                forced_messages.clear()
                messages = trim_messages(messages)

            _model_failed = False
            operation = target = value = ""
            assistant_message: Dict[str, Any] = {"role": "assistant", "content": ""}
            tool_call_id: Optional[str] = None

            try:
                guard_attempts = 0
                while True:
                    guard_attempts += 1
                    operation, target, value, assistant_message, tool_call_id = get_model_action(
                        client=client, step=step, messages=messages
                    )
                    model_calls_used += 1
                    if model_calls_used > MAX_MODEL_CALLS_PER_TASK:
                        raise RuntimeError("Model call budget exceeded")

                    if tool_call_id is None:
                        tool_call_misses += 1
                        messages.append({
                            "role": "user",
                            "content": "Your previous response did not include a tool call. Return exactly one valid tool call.",
                        })
                        if tool_call_misses >= MAX_CONSECUTIVE_TOOL_CALL_MISSES and model_calls_used >= MIN_MODEL_CALLS_BEFORE_STRICT_FAIL:
                            raise RuntimeError("Model failed to emit tool calls repeatedly.")
                        messages = trim_messages(messages)
                        continue
                    tool_call_misses = 0

                    should_resample = False

                    # Deduplication guard
                    action_tuple = (operation, target, value)
                    rerun_blocked = (
                        action_tuple in action_history
                        and not (operation == "trigger_pipeline" and fix_applied_since_rerun)
                    )
                    if rerun_blocked:
                        _dup_key = f"dup:{operation}:{target}"
                        if _dup_key not in injected_guardrails:
                            injected_guardrails.add(_dup_key)
                            messages.append({
                                "role": "user",
                                "content": "Action already tried. Choose different operation or target.",
                            })
                        should_resample = True

                    # Hypothesis dedup
                    if operation == "set_hypothesis":
                        norm = _normalize_hypothesis(value)
                        if norm and norm in attempted_hypotheses:
                            _dup_hyp_key = f"dup_hyp:{norm}"
                            if _dup_hyp_key not in injected_guardrails:
                                injected_guardrails.add(_dup_hyp_key)
                                messages.append({
                                    "role": "user",
                                    "content": "Hypothesis failed. Try different root cause.",
                                })
                            should_resample = True

                    # Hard block: write_file with no path
                    if operation == "write_file" and not target:
                        _last_read = next(
                            (t for op, t, v in reversed(action_history) if op == "read_file" and t),
                            "",
                        )
                        _path_hint = f"'{_last_read}'" if _last_read else "<path>"
                        messages.append({
                            "role": "user",
                            "content": f"write_file needs path. Use: path={_path_hint}, content='...'",
                        })
                        should_resample = True

                    # Read-before-write guardrail: block write_file if the file was never read
                    if operation == "write_file" and target and target not in read_files:
                        _rbw_key = f"rbw:{target}"
                        if _rbw_key not in injected_guardrails:
                            injected_guardrails.add(_rbw_key)
                            messages.append({
                                "role": "user",
                                "content": f"Read '{target}' first before writing.",
                            })
                        should_resample = True

                    if should_resample and guard_attempts < 2:  # reduced from 3 to 2
                        messages = trim_messages(messages)
                        continue

                    break

            except RuntimeError as exc:
                _model_failed = True
                log_step(step=step, action="[model_failure]||", reward=0.0, done=True,
                         error=str(exc), llm_thought="")

            if _model_failed:
                break

            # Hard redirect: write_file with empty path must never reach the API
            if operation == "write_file" and not target:
                _last_read = next(
                    (t for op, t, v in reversed(action_history) if op == "read_file" and t),
                    "",
                )
                operation = "read_file" if _last_read else "trigger_pipeline"
                target = _last_read
                value = ""
                assistant_message = {
                    "role": "assistant",
                    "content": f"Redirect: {operation}|{target}",
                }
                tool_call_id = None
                forced_messages.append(
                    f"write_file needs path. Use: write_file(path='{_last_read or '<path>'}', content='...')"
                )

            # Repetition escape — force a different action after too many identical attempts
            action_tuple = (operation, target, value)
            repetition_threshold = 2
            if action_history.count(action_tuple) >= repetition_threshold:
                # Map WS ops to HTTP-equivalent for the escape helper
                _escaped = _repetition_escape_action_ws(action_history, action_tuple)
                operation, target, value = _escaped
                assistant_message = {
                    "role": "assistant",
                    "content": f"Repetition guard: forcing {operation}|{target}|{value}",
                }
                tool_call_id = None

            action_history.append((operation, target, value))
            history.append(f"{operation}|{target}|{value}")
            steps_taken = step
            if operation == "write_file" and value:
                last_fix_value = value

            # ── Dispatch tool call to WS API ──────────────────────────────
            tool_args: Dict[str, Any] = {}
            if operation == "read_file":
                tool_args = {"path": target}
            elif operation == "write_file":
                tool_args = {"path": target, "content": value}
            elif operation == "list_files":
                tool_args = {"directory": target}
            elif operation == "set_hypothesis":
                tool_args = {"hypothesis": value}

            result = execute_tool(operation, tool_args, ws)
            tool_result_str = format_tool_result(operation, result)

            # ── Reward — all values in [0.0, 1.0] ────────────────────────
            op_success = result.get("success", False)
            if operation == "trigger_pipeline":
                pipeline_passed = result.get("passed", False)
                pipeline_triggered_once = True
                if pipeline_passed:
                    reward = 0.35
                elif op_success:
                    reward = 0.05
                else:
                    reward = 0.0
            elif operation == "set_hypothesis":
                reward = 0.10 if op_success else 0.0
            elif operation == "write_file":
                reward = 0.15 if op_success else 0.0
            elif operation == "read_file":
                if op_success and result.get("exists"):
                    reward = 0.05 if pipeline_triggered_once else 0.0
                else:
                    reward = 0.0
            elif operation == "list_files":
                reward = 0.0
            elif operation == "finalize":
                reward = 1.0 if pipeline_passed else 0.0
                score = reward
                success = pipeline_passed
            else:
                reward = 0.0

            rewards.append(reward)
            step_trace.append({
                "operation": operation,
                "target": target,
                "value": value[:120] if value else "",
                "reward": round(reward, 3),
                "rationale": _step_rationale(operation, target, value, reward),
            })

            tool_error = result.get("error") if not op_success else None
            llm_thought = assistant_message.get("content") or ""
            log_step(
                step=step,
                action=f"{operation}|{target}|{value[:60]}",
                reward=reward,
                done=(operation == "finalize"),
                error=tool_error,
                llm_thought=llm_thought,
            )

            if tool_result_str:
                truncated = tool_result_str[:1500] + ("..." if len(tool_result_str) > 1500 else "")
                print(_tw.indent(truncated, "    "), flush=True)

            # Track successfully read files so the read-before-write guardrail can allow them.
            # Also clear the guardrail key so re-attempting write_file on the same path is allowed.
            if operation == "read_file" and op_success and result.get("exists"):
                read_files.add(target)
                injected_guardrails.discard(f"rbw:{target}")

            # ── Post-step forced messages ─────────────────────────────────
            if operation == "write_file":
                fix_applied_since_rerun = True
                if op_success and not pipeline_passed:
                    forced_messages.append(
                        "Fix applied. Call trigger_pipeline NOW to verify."
                    )
            if operation == "trigger_pipeline":
                fix_applied_since_rerun = False
                if pipeline_passed:
                    forced_messages.append(
                        "Pipeline PASSED. Call finalize NOW."
                    )
            if operation == "set_hypothesis":
                norm = _normalize_hypothesis(value)
                if norm:
                    attempted_hypotheses.add(norm)
                if reward > 0:
                    hypothesis_accepted = True
                    if "fix_hint_sent" not in injected_guardrails:
                        injected_guardrails.add("fix_hint_sent")
                        forced_messages.append(_STRUCTURED_FIX_HINT_WS)
                elif reward < 0:
                    forced_messages.append(
                        "Hypothesis incorrect. Re-read logs and try different root cause."
                    )

            # Recovery: write_file failed because path was missing
            if operation == "write_file" and not op_success and "requires 'path'" in (tool_error or ""):
                _rbw_recovery_key = "write_file_path_missing"
                if _rbw_recovery_key not in injected_guardrails:
                    injected_guardrails.add(_rbw_recovery_key)
                    forced_messages.append(
                        "write_file needs BOTH path and content. Example: write_file(path='file.py', content='...')"
                    )

            messages.append(assistant_message)
            if tool_call_id:
                obs_msg: Dict[str, Any] = {"role": "tool", "tool_call_id": tool_call_id, "content": tool_result_str}
            else:
                obs_msg = {"role": "user", "content": f"Result:\n{tool_result_str}\n\nNext action?"}
            messages.append(obs_msg)
            messages = trim_messages(messages)

            if operation == "finalize":
                break

        # Forced finalize if the loop exhausted steps without one
        if not success and steps_taken > 0:
            result = execute_tool("finalize", {}, ws)
            steps_taken += 1
            reward = 1.0 if pipeline_passed else 0.0
            score = reward
            success = pipeline_passed
            rewards.append(reward)
            log_step(step=steps_taken, action="finalize||", reward=reward, done=True,
                     error=None, llm_thought="[forced finalize: step budget exhausted]")

    # ── Record episode outcome in curriculum ──────────────────────────────────
    try:
        from server.curriculum import CurriculumController
        CurriculumController().record_episode(
            fault_type=fault_injected,
            difficulty=curriculum_difficulty,
            final_score=score,
            resolved=success,
            steps_used=steps_taken,
        )
    except Exception:
        pass

    # ── Persist memory ────────────────────────────────────────────────────────
    memory_key = fault_injected if fault_injected != "unknown" else task_name
    if remember is not None and last_fix_value:
        try:
            remember([memory_key], last_fix_value, success)
        except Exception:
            pass

    if remember_optimal_path is not None and success:
        try:
            optimal = _build_optimal_path(step_trace, resolved=success)
            if optimal:
                remember_optimal_path(memory_key, optimal)
                print(f"[MEMORY] Stored optimal path for fault_type={memory_key} ({len(optimal)} steps)", flush=True)
        except Exception:
            pass

    log_end(
        success=success, steps=steps_taken, score=score,
        resolved=success, rewards=rewards,
        deterministic_score=score, rubric_score=0.0, rubric_judge_used=False,
    )
    return task_name, success, steps_taken, score


def main() -> None:
    if not API_KEY:
        raise RuntimeError(
            "Missing API credentials. Set provider-specific key: "
            "HF_TOKEN (hf), OPENROUTER_API_KEY (openrouter), or GROQ_API_KEY (groq)."
        )

    from openai import OpenAI

    client = OpenAI(**get_openai_client_kwargs())

    with requests.Session() as session:
        session.headers.update({"Accept": "application/json"})
        task_scores: List[Tuple[str, float, bool]] = []
        print(f"[INFERENCE] Running {NUM_EPISODES} episodes — fault seed selected per-episode by curriculum UCB1 (based on prior scores), scenario composed by LLM adversarial designer.", flush=True)
        for episode_label in TASK_ORDER:
            # episode_label is just a slot name ("episode_1", etc.).
            # The actual fault/scenario is generated fresh by LLM for each reset.
            if USE_WS_API:
                task_name, success, _steps, score = run_task_ws(client, episode_label)
            else:
                task_name, success, _steps, score = run_task(client, session, episode_label)
            task_scores.append((task_name, score, success))
