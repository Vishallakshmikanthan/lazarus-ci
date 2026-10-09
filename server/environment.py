"""Simulated CI/CD repair environment — no Docker, no Git required."""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

try:
    from ..models import MetaHackathonAction, MetaHackathonObservation
except (ImportError, ModuleNotFoundError):
    from models import MetaHackathonAction, MetaHackathonObservation

from cicd.simulated_runner import SimulatedPipelineRunner, PipelineStatus as SimPipelineStatus
_runner_mode = os.getenv("CICD_RUNNER_MODE", "simulated").strip().lower()
if _runner_mode == "simulated":
    _RunnerClass = SimulatedPipelineRunner  # type: ignore[assignment]
else:
    from cicd.subprocess_runner import SubprocessPipelineRunner as _RunnerClass
from cicd.simulated_fault_injector import inject_fault_simulated
from cicd.simulated_fix_applier import apply_fix_simulated
from cicd.observation_builder import (
    build_observation,
    build_stage_log_response,
    build_stage_log_tail,
    build_surfaced_errors,
    build_visible_alerts,
    build_visible_logs,
    build_visible_metrics,
    build_logs_by_stage,
    extract_error_lines,
    read_config_files,
    read_workspace_file,
)
from cicd.fault_types import (
    FaultMetadata,
    FAULT_TYPES,
    FAULT_KEYWORDS,
    FAULT_STAGE_MAP,
)
from cicd.procedural_generator import generate_scenario as procedural_generate_scenario, inject_procedural

try:
    from .curriculum import CurriculumController
    from .adversarial_designer import AdversarialDesigner
    from .adversarial_judge import AdversarialJudge
    from .rubric_judge import OpenEnvLLMJudgeAdapter, DEFAULT_OPENROUTER_MODEL
except (ImportError, ModuleNotFoundError):
    from server.curriculum import CurriculumController
    from server.adversarial_designer import AdversarialDesigner
    from server.adversarial_judge import AdversarialJudge
    from server.rubric_judge import OpenEnvLLMJudgeAdapter, DEFAULT_OPENROUTER_MODEL


# ── Constants ──────────────────────────────────────────────────────────────

CANONICAL_OPERATIONS = [
    "view_logs", "tail_logs", "inspect_config", "inspect_dockerfile",
    "inspect_permissions", "set_hypothesis", "modify_config",
    "add_dependency", "rerun_pipeline", "verify_fix", "finalize",
]

SAFE_FIXES = [
    "resolve-merge-conflict",
    "pin-compatible-requests-urllib3",
    "reorder-docker-install-steps",
    "add-flaky-test-retry-wrapper",
    "fix-docker-compose-network",
    "remove-hardcoded-secrets",
]

DESTRUCTIVE_FIXES = [
    "disable-all-tests",
    "force-push-main",
    "wipe-registry",
    "skip-deploy-validations",
]

WORKSPACE_TTL_SECONDS = 1800

SAMPLE_APP_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "sample-app",
)

STAGE_ORDER = ["clone", "build", "test", "deploy"]

KNOWN_CONFIG_PATHS = (
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
    "db/migrations/001_init.sql",
    "db/database.py",
)


@dataclass
class State:
    episode_id: str = ""
    step_count: int = 0


@dataclass
class EpisodeState:
    """All state for a single episode."""
    episode_id: str = ""
    workspace_dir: str = ""
    repo_dir: str = ""
    fault_metadata: Optional[FaultMetadata] = None
    pipeline_result: Optional[Any] = None
    pipeline_runner: Optional[Any] = None
    all_pipeline_results: List[Any] = field(default_factory=list)

    history: List[Dict[str, str]] = field(default_factory=list)
    action_keys: Set[str] = field(default_factory=set)
    findings: List[str] = field(default_factory=list)

    current_hypothesis: str = ""
    hypothesis_history: List[str] = field(default_factory=list)
    attempted_fix: str = ""
    hypothesis_attempts: int = 0
    hypothesis_correct: bool = False

    incident_resolved: bool = False
    pipeline_health: float = 1.0
    recovery_cost: int = 0
    redundant_actions: int = 0
    destructive_actions: int = 0
    wrong_fixes: int = 0

    last_fix_result: Optional[Any] = None
    pending_fix_outcome: str = "none"
    last_rerun_progressed: bool = False
    verified_for_latest_rerun: bool = False
    inspected_since_last_rerun: bool = True
    fix_hits: int = 0
    errors_stale_after_fix: bool = False

    adversarial_scenario: Optional[Any] = None
    cascading_faults: List[Any] = field(default_factory=list)
    curriculum_difficulty: float = 0.5

    deterministic_score: float = 0.0
    log_budget_total: int = 120
    log_tokens_remaining: int = 120
    last_log_access: str = "reset"

    used_inspections: Set[str] = field(default_factory=set)

    created_at: float = 0.0

    rerun_attempts: int = 0
    episode_seed: int = 0
    procedural_mode: bool = False


def _extract_config_path_from_text(text: str) -> str:
    patterns = [
        r" in ([^:]+):\d+:",
        r"\b((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|ya?ml|txt|env|pth))\b",
        r"\b(Dockerfile)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        candidate = match.group(1).strip().strip(".,)")
        candidate = candidate.replace("\\", "/")
        if candidate in KNOWN_CONFIG_PATHS or "/" in candidate or candidate in {"Dockerfile", ".env"}:
            return candidate
    return ""


def _resolve_episode_config_target(ep: EpisodeState, target: str) -> str:
    filepath = target.replace("\\", "/").lstrip("./")
    if not filepath:
        return filepath

    direct_path = os.path.join(ep.workspace_dir, filepath)
    if os.path.exists(direct_path):
        return filepath

    normalized_target = filepath.split("/")[-1].lower()

    if normalized_target in STAGE_ORDER:
        surfaced_errors = build_surfaced_errors(ep.pipeline_result, ep.workspace_dir) if ep.pipeline_result else []
        for err in surfaced_errors:
            candidate = _extract_config_path_from_text(str(err))
            if candidate and os.path.exists(os.path.join(ep.workspace_dir, candidate)):
                return candidate

        if ep.last_fix_result and ep.last_fix_result.files_modified:
            candidate = ep.last_fix_result.files_modified[0].replace("\\", "/")
            if os.path.exists(os.path.join(ep.workspace_dir, candidate)):
                return candidate

        if ep.fault_metadata:
            for candidate in ep.fault_metadata.affected_files:
                normalized_candidate = candidate.replace("\\", "/")
                if os.path.exists(os.path.join(ep.workspace_dir, normalized_candidate)):
                    return normalized_candidate

    for cfg in KNOWN_CONFIG_PATHS:
        if normalized_target == os.path.basename(cfg).lower() or normalized_target in cfg.lower():
            return cfg

    for root, dirs, files in os.walk(ep.workspace_dir):
        dirs[:] = [d for d in dirs if d != ".git"]
        for file_name in files:
            if file_name.lower() == normalized_target:
                return os.path.relpath(
                    os.path.join(root, file_name), ep.workspace_dir
                ).replace("\\", "/")

    return filepath


def _canonical_operation(operation: str) -> str:
    aliases = {
        "view-logs": "view_logs",
        "viewlogs": "view_logs",
        "tail-logs": "tail_logs",
        "taillogs": "tail_logs",
        "inspect-config": "inspect_config",
        "inspectconfig": "inspect_config",
        "inspect-dockerfile": "inspect_dockerfile",
        "inspectdockerfile": "inspect_dockerfile",
        "inspect-permissions": "inspect_permissions",
        "inspectpermissions": "inspect_permissions",
        "set-hypothesis": "set_hypothesis",
        "sethypothesis": "set_hypothesis",
        "modify-config": "modify_config",
        "modifyconfig": "modify_config",
        "add-dependency": "add_dependency",
        "adddependency": "add_dependency",
        "rerun-pipeline": "rerun_pipeline",
        "rerunpipeline": "rerun_pipeline",
        "verify-fix": "verify_fix",
        "verifyfix": "verify_fix",
    }
    op = operation.lower().strip()
    return aliases.get(op, op)


try:
    from openenv.core.env_server.interfaces import Environment
except ImportError:
    class Environment:
        pass


class SimulatedCICDRepairEnvironment(Environment):
    """Fully simulated CI/CD repair environment — no Docker, no Git required.

    Uses pure-Python equivalents for all pipeline operations:
      - Workspace setup: shutil.copytree (no git init/commit)
      - Fault injection: SimulatedFaultInjector (file mutations, no git)
      - Pipeline execution: SimulatedPipelineRunner (synthetic logs)
      - Fix application: SimulatedFixApplier (file mutations, no git)
    """

    SUPPORTS_CONCURRENT_SESSIONS = True

    def __init__(self, task_key: str = ""):
        self._task_key = task_key or os.getenv("META_HACKATHON_TASK_MODE", "cycle")
        self._state = State()
        self._episode: Optional[EpisodeState] = None

        self._task_order = FAULT_TYPES
        self._task_cursor = 0

        self._curriculum = CurriculumController()

        _llm_provider = os.getenv("LLM_PROVIDER", "hf").strip().lower()

        def _resolve_api_key() -> str:
            explicit = (os.getenv("API_KEY") or "").strip()
            if explicit:
                return explicit
            if _llm_provider == "openrouter":
                return (os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
                        or os.getenv("HF_TOKEN") or os.getenv("GROQ_API_KEY") or "")
            if _llm_provider == "groq":
                return (os.getenv("GROQ_API_KEY") or os.getenv("OPENAI_API_KEY")
                        or os.getenv("HF_TOKEN") or os.getenv("OPENROUTER_API_KEY") or "")
            return (os.getenv("HF_TOKEN") or os.getenv("OPENAI_API_KEY")
                    or os.getenv("OPENROUTER_API_KEY") or os.getenv("GROQ_API_KEY") or "")

        def _resolve_base_url() -> str:
            explicit = (os.getenv("CICD_ADV_BASE_URL") or os.getenv("API_BASE_URL") or "").strip()
            if explicit:
                return explicit
            if _llm_provider == "openrouter":
                return "https://openrouter.ai/api/v1"
            if _llm_provider == "groq":
                return "https://api.groq.com/openai/v1"
            return "https://router.huggingface.co/v1"

        self._adv_designer = AdversarialDesigner(
            api_key=_resolve_api_key(),
            base_url=_resolve_base_url(),
        )
        self._adv_judge = AdversarialJudge()

        self._cleanup_lock = threading.Lock()
        self._stale_workspaces: List[tuple] = []

        self._template_dir = SAMPLE_APP_TEMPLATE
        if not os.path.isdir(self._template_dir):
            self._template_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", "sample-app"
            )

    # ── Workspace setup ─────────────────────────────────────────────────────

    def _setup_workspace(self, target_dir: str) -> None:
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir)
        shutil.copytree(self._template_dir, target_dir, dirs_exist_ok=False)

    # ── OpenEnv API ─────────────────────────────────────────────────────────

    async def reset_async(
        self, seed: Optional[int] = None, episode_id: Optional[str] = None, **kwargs: Any
    ) -> MetaHackathonObservation:
        import asyncio
        options = kwargs.get("reset_options", {})
        task_key = options.get("task_key") or kwargs.get("task_key", "")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self.reset(task_key=task_key))

    def reset(self, task_key: str = "") -> MetaHackathonObservation:
        if self._episode:
            self._cleanup_episode(self._episode)

        episode_id = str(uuid.uuid4())
        self._state = State(episode_id=episode_id, step_count=0)

        eff_task_key = task_key or self._task_key
        use_procedural_style = eff_task_key in ("procedural", "combo")

        curriculum_difficulty = self._curriculum.get_difficulty()
        skill_profile = self._curriculum.get_skill_profile()

        workspace_base = tempfile.mkdtemp(prefix="cicd-sim-episode-")
        repo_dir = os.path.join(workspace_base, "repo")
        self._setup_workspace(repo_dir)

        episode_seed = int(uuid.UUID(episode_id).int & 0xFFFFFFFF)

        seed_fault = self._curriculum.select_fault_type()
        adversarial_scenario = self._adv_designer.design(
            root_cause_fault=seed_fault,
            difficulty=curriculum_difficulty,
            skill_profile=skill_profile,
        )

        if use_procedural_style and adversarial_scenario.steps:
            root_cause_ft = adversarial_scenario.steps[0].fault_type
            adversarial_scenario = procedural_generate_scenario(
                difficulty=curriculum_difficulty,
                seed=episode_seed,
                root_cause=root_cause_ft,
            )
            injected = self._inject_scenario(repo_dir, adversarial_scenario)
        else:
            if curriculum_difficulty < 0.65 and len(adversarial_scenario.steps) > 1:
                adversarial_scenario.steps = adversarial_scenario.steps[:1]
            injected = self._inject_scenario(repo_dir, adversarial_scenario)

        fault_type = adversarial_scenario.steps[0].fault_type if adversarial_scenario.steps else "merge_conflict"
        cascade_types = [s.fault_type for s in adversarial_scenario.steps[1:]] if adversarial_scenario.steps else []
        fail_stage = FAULT_STAGE_MAP.get(fault_type, "unknown")
        print(
            f"[SIM-EPISODE] fault={fault_type}  cascades={cascade_types}  "
            f"fail_stage={fail_stage}  difficulty={curriculum_difficulty:.2f}  "
            f"mode={'procedural' if use_procedural_style else 'llm-adversarial'}",
            flush=True,
        )

        fault_metadata = injected[0] if injected else inject_fault_simulated(repo_dir, fault_type)
        cascading_faults: list = injected[1:] if len(injected) > 1 else []

        # Use the actually-injected fault type so the runner checks for what's in the files,
        # not whatever the LLM scenario may have returned (which can diverge from seed_fault).
        injected_fault_type = fault_metadata.fault_type if fault_metadata else fault_type

        runner = _RunnerClass(
            workspace_path=repo_dir,
            fault_type=injected_fault_type,
            scenario=adversarial_scenario,
            episode_id=episode_id,
        )
        pipeline_result = runner.run(workspace_dir=repo_dir)

        _retry = 0
        try:
            while str(pipeline_result.status) == SimPipelineStatus.PASSED and _retry < 2:
                _retry += 1
                logger.warning(
                    "[sim-reset] Simulated pipeline passed after injection — retrying "
                    "(fault_type=%s, attempt %d/2).", fault_type, _retry,
                )
                fallback = inject_fault_simulated(repo_dir, fault_type)
                if fallback:
                    fault_metadata = fallback
                pipeline_result = runner.run(workspace_dir=repo_dir)
        except Exception:
            logger.exception("[sim-reset] Fault injection retry failed.")

        episode = EpisodeState(
            episode_id=episode_id,
            workspace_dir=repo_dir,
            repo_dir=repo_dir,
            fault_metadata=fault_metadata,
            pipeline_result=pipeline_result,
            pipeline_runner=runner,
            all_pipeline_results=[pipeline_result],
            findings=["Incident acknowledged. Investigate before changing configuration."],
            created_at=time.time(),
            adversarial_scenario=adversarial_scenario,
            cascading_faults=cascading_faults,
            curriculum_difficulty=curriculum_difficulty,
            episode_seed=episode_seed,
            procedural_mode=use_procedural_style,
            log_budget_total=max(20, int(os.getenv("META_HACKATHON_LOG_BUDGET", "120"))),
            log_tokens_remaining=max(20, int(os.getenv("META_HACKATHON_LOG_BUDGET", "120"))),
        )
        self._episode = episode

        difficulty = self._get_difficulty(fault_type)
        max_steps = self._get_max_steps(fault_type, cascade_count=len(cascading_faults))

        obs_dict = build_observation(
            pipeline_result=pipeline_result,
            workspace_dir=repo_dir,
            task_id=f"sim_{fault_type}",
            task_title=f"Fix {fault_type.replace('_', ' ')} in CI/CD pipeline (simulated)",
            difficulty=difficulty,
            available_tools=self._available_operations_for_episode(episode),
            reward=0.0,
            done=False,
            log_tokens_remaining=episode.log_tokens_remaining,
            log_access_mode="full",
            findings=episode.findings,
            metadata={
                "task_key": fault_type,
                "max_steps": max_steps,
                "fault_type": fault_type,
                "expected_fail_stage": fault_metadata.expected_fail_stage,
                "ready_to_finalize": False,
                "verification_required": False,
                "verified_since_last_rerun": False,
                "supported_operations": self._available_operations_for_episode(episode),
                "canonical_operations": CANONICAL_OPERATIONS,
                "log_tokens_remaining": episode.log_tokens_remaining,
                "log_budget_total": episode.log_budget_total,
                "log_access_mode": "full",
                "simulated": True,
            },
        )
        return self._dict_to_observation(obs_dict)

    def step(self, action: MetaHackathonAction) -> MetaHackathonObservation:
        self._state.step_count += 1
        episode = self._episode
        if not episode:
            return self._error_observation("No active episode. Call /reset first.")

        raw_operation = (action.operation or "").strip()
        operation = _canonical_operation(raw_operation)
        target = (action.target or "").strip()
        value = (action.value or "").strip()

        if operation not in CANONICAL_OPERATIONS:
            return self._error_observation(
                f"Unsupported operation '{raw_operation}'",
                reward=0.0,
            )

        history_entry = {"operation": operation, "target": target, "value": value}
        episode.history.append(history_entry)

        action_key = f"{operation}:{target}:{value}"
        was_redundant = action_key in episode.action_keys
        if was_redundant:
            episode.redundant_actions += 1
        episode.action_keys.add(action_key)

        finalize_blocked = operation == "finalize" and not episode.verified_for_latest_rerun

        if operation == "view_logs":
            self._handle_view_logs(episode, target, full=True)
        elif operation == "tail_logs":
            self._handle_view_logs(episode, target, full=False)
        elif operation == "inspect_config":
            self._handle_inspect_config(episode, target)
        elif operation == "inspect_dockerfile":
            self._handle_inspect_dockerfile(episode)
        elif operation == "inspect_permissions":
            self._handle_inspect_permissions(episode, target)
        elif operation == "set_hypothesis":
            self._handle_set_hypothesis(episode, value)
        elif operation in ("modify_config", "add_dependency"):
            self._handle_modify(episode, operation, target, value)
        elif operation == "rerun_pipeline":
            self._handle_rerun_pipeline(episode)
        elif operation == "verify_fix":
            self._handle_verify_fix(episode)
        elif operation == "finalize":
            self._handle_finalize(episode)

        if episode.adversarial_scenario is not None and not finalize_blocked:
            phase_bonus, phase_note = self._adv_judge.score_step(
                operation=operation,
                value=value,
                scenario=episode.adversarial_scenario,
                history=episode.history,
            )
            if phase_note:
                episode.findings.append(f"[Judge] {phase_note} (legacy bonus suppressed: {phase_bonus:+.2f})")

        done = False
        fault_type_for_steps = episode.fault_metadata.fault_type if episode.fault_metadata else "unknown"
        cascade_count_for_steps = len(episode.cascading_faults) if episode.cascading_faults else 0
        max_steps = self._get_max_steps(fault_type_for_steps, cascade_count=cascade_count_for_steps)

        if operation == "finalize":
            done = True
        elif self._state.step_count >= max_steps:
            done = True

        reward = 0.0
        logger.info("[STEP] episode=%s step=%s action=%s", episode.episode_id, self._state.step_count, operation)

        obs_dict = self._build_step_observation(episode, reward, done)
        return self._dict_to_observation(obs_dict)

    @property
    def state(self):
        return self._state

    # ── Action handlers ──────────────────────────────────────────────────────

    def _handle_view_logs(self, ep: EpisodeState, target: str, *, full: bool) -> None:
        stage = target.lower() if target else ""
        ep.used_inspections.add("view_logs" if full else "tail_logs")
        ep.inspected_since_last_rerun = True

        if ep.pipeline_result:
            if stage and stage in STAGE_ORDER:
                if full:
                    log_text = build_stage_log_response(ep.pipeline_result, stage)
                    token_cost = self._consume_log_budget(ep, log_text, minimum_cost=8)
                    if token_cost is None:
                        tail_text = build_stage_log_tail(ep.pipeline_result, stage, tail_lines=10)
                        ep.findings.append(
                            f"Full log budget exhausted; tail only for stage '{stage}':\n{tail_text[:400]}"
                        )
                        ep.last_log_access = "tail_only"
                        return
                    ep.findings.append(
                        f"Full logs for stage '{stage}' (cost {token_cost}, remaining {ep.log_tokens_remaining}):\n{log_text[:500]}"
                    )
                    ep.last_log_access = "full"
                else:
                    tail_text = build_stage_log_tail(ep.pipeline_result, stage, tail_lines=10)
                    token_cost = self._consume_log_budget(ep, tail_text, minimum_cost=0)
                    ep.findings.append(
                        f"Tail logs for stage '{stage}' (cost {token_cost}, remaining {ep.log_tokens_remaining}):\n{tail_text[:300]}"
                    )
                    ep.last_log_access = "tail"
            else:
                failed_stage = ep.pipeline_result.failed_stage
                if failed_stage:
                    if full:
                        log_text = build_stage_log_response(ep.pipeline_result, failed_stage)
                        token_cost = self._consume_log_budget(ep, log_text, minimum_cost=8)
                        if token_cost is None:
                            tail_text = build_stage_log_tail(ep.pipeline_result, failed_stage, tail_lines=10)
                            ep.findings.append(
                                f"Full log budget exhausted; tail only for failed stage '{failed_stage}':\n{tail_text[:400]}"
                            )
                            ep.last_log_access = "tail_only"
                            return
                        ep.findings.append(
                            f"Full logs for failed stage '{failed_stage}' (cost {token_cost}, remaining {ep.log_tokens_remaining}):\n{log_text[:500]}"
                        )
                        ep.last_log_access = "full"
                    else:
                        tail_text = build_stage_log_tail(ep.pipeline_result, failed_stage, tail_lines=10)
                        token_cost = self._consume_log_budget(ep, tail_text, minimum_cost=0)
                        ep.findings.append(
                            f"Tail logs for failed stage '{failed_stage}' (cost {token_cost}, remaining {ep.log_tokens_remaining}):\n{tail_text[:300]}"
                        )
                        ep.last_log_access = "tail"

        return

    def _handle_inspect_config(self, ep: EpisodeState, target: str) -> None:
        ep.used_inspections.add("inspect_config")
        ep.inspected_since_last_rerun = True

        if target:
            filepath = _resolve_episode_config_target(ep, target)
            content = read_workspace_file(ep.workspace_dir, filepath)
            configs = {filepath: content}
        else:
            configs = read_config_files(ep.workspace_dir)

        for filename, content in configs.items():
            lines = content.splitlines()
            conflict_indices = [i for i, l in enumerate(lines) if l.startswith(("<<<<<<<", "=======", ">>>>>>>"))]
            if conflict_indices:
                start_ctx = max(0, conflict_indices[0] - 2)
                end_ctx = min(len(lines), conflict_indices[-1] + 3)
                conflict_section = "\n".join(
                    f"  {i:3d}: {line}" for i, line in enumerate(lines[start_ctx:end_ctx], start_ctx + 1)
                )
                ep.findings.append(f"⚠ MERGE CONFLICT in '{filename}':\n{conflict_section}")
            else:
                ep.findings.append(f"Config file '{filename}':\n{content[:400]}")

        return

    def _handle_inspect_dockerfile(self, ep: EpisodeState) -> None:
        ep.used_inspections.add("inspect_dockerfile")
        ep.inspected_since_last_rerun = True
        content = read_workspace_file(ep.workspace_dir, "Dockerfile")
        ep.findings.append(f"Dockerfile content:\n{content[:300]}")
        return

    def _handle_inspect_permissions(self, ep: EpisodeState, target: str) -> None:
        ep.used_inspections.add("inspect_permissions")
        ep.inspected_since_last_rerun = True
        files_to_check = ["Dockerfile", "docker-compose.yml", "services/api/app.py"]
        if target:
            files_to_check.insert(0, target)
        for filepath in files_to_check:
            full_path = os.path.join(ep.workspace_dir, filepath)
            if os.path.exists(full_path):
                try:
                    stat = os.stat(full_path)
                    ep.findings.append(f"File '{filepath}': mode={oct(stat.st_mode)} size={stat.st_size}B")
                except OSError as e:
                    ep.findings.append(f"Cannot stat '{filepath}': {e}")
        return

    def _handle_set_hypothesis(self, ep: EpisodeState, value: str) -> None:
        ep.current_hypothesis = value
        ep.hypothesis_history.append(value)
        ep.hypothesis_attempts += 1

        if not ep.fault_metadata:
            return

        hypothesis_lower = value.lower()

        if ep.adversarial_scenario is not None:
            keywords = list(ep.adversarial_scenario.expected_hypothesis_terms)
            root_fault_type = ep.fault_metadata.fault_type
            for kw in FAULT_KEYWORDS.get(root_fault_type, []):
                if kw not in keywords:
                    keywords.append(kw)
        else:
            keywords = ep.fault_metadata.keywords or FAULT_KEYWORDS.get(ep.fault_metadata.fault_type, [])

        match_count = sum(1 for kw in keywords if kw.lower() in hypothesis_lower)
        match_ratio = match_count / max(len(keywords), 1)
        file_mentioned = any(
            os.path.basename(f).lower() in hypothesis_lower
            for f in ep.fault_metadata.affected_files
        )

        if match_count >= 1 or file_mentioned:
            ep.hypothesis_correct = True
            ep.findings.append(f"Hypothesis partially aligns (matched {match_count}/{len(keywords)} keywords). Apply the fix.")
            return
        else:
            root_fault = ep.fault_metadata.fault_type
            hint_keywords = FAULT_KEYWORDS.get(root_fault, keywords)
            ep.findings.append("Hypothesis does not match current evidence. Try mentioning: " + ", ".join(hint_keywords))
            return

    def _handle_modify(self, ep: EpisodeState, operation: str, target: str, value: str) -> None:
        ep.attempted_fix = value
        ep.verified_for_latest_rerun = False

        if self._is_destructive_fix(value):
            ep.destructive_actions += 1
            ep.pipeline_health = max(0.0, ep.pipeline_health - 0.20)
            ep.recovery_cost += 4
            ep.pending_fix_outcome = "destructive"
            ep.findings.append("Unsafe fix worsened system stability and increased recovery cost.")
            return

        fault_type = ep.fault_metadata.fault_type if ep.fault_metadata else ""
        fix_result = apply_fix_simulated(ep.workspace_dir, value, target, fault_type=fault_type)
        ep.last_fix_result = fix_result

        if fix_result.success:
            ep.findings.append(f"Fix applied: {fix_result.description}")
            ep.pending_fix_outcome = "applied"
            ep.errors_stale_after_fix = True
            if hasattr(ep.pipeline_runner, "commit_agent_fixes"):
                ep.pipeline_runner.commit_agent_fixes()
            return 0.10
        else:
            ep.findings.append(f"Fix could not be applied: {fix_result.error}")
            ep.pending_fix_outcome = "failed"
            ep.wrong_fixes += 1
            ep.pipeline_health = max(0.0, ep.pipeline_health - 0.10)
            ep.recovery_cost += 2
            return

    def _handle_rerun_pipeline(self, ep: EpisodeState) -> None:
        ep.recovery_cost += 1
        ep.verified_for_latest_rerun = False
        ep.inspected_since_last_rerun = False
        ep.rerun_attempts += 1

        if not ep.pipeline_runner:
            ep.findings.append("No pipeline runner available for rerun.")
            return

        old_status = str(ep.pipeline_result.status) if ep.pipeline_result else SimPipelineStatus.FAILED
        old_failed_stage = ep.pipeline_result.failed_stage if ep.pipeline_result else ""

        new_result = ep.pipeline_runner.run(workspace_dir=ep.workspace_dir)
        ep.all_pipeline_results.append(new_result)
        ep.pipeline_result = new_result
        ep.errors_stale_after_fix = False

        for stage_name in STAGE_ORDER:
            stage = new_result.stages.get(stage_name)
            if not stage or str(stage.status) in ("pending", "skipped"):
                continue
            combined = ((stage.stdout or "") + "\n" + (stage.stderr or "")).strip()
            tail = "\n".join(combined.splitlines()[-20:]) if combined else "(no output)"
            ep.findings.append(
                f"[Rerun] Stage '{stage_name}' → {stage.status.value} "
                f"(exit {stage.exit_code}, {stage.duration_seconds:.1f}s)\n{tail[:600]}"
            )

        progressed = False
        if str(new_result.status) == SimPipelineStatus.PASSED:
            progressed = True
            ep.incident_resolved = True
            ep.findings.append("Pipeline PASSED — all stages completed successfully!")
        elif old_failed_stage and new_result.failed_stage:
            old_idx = STAGE_ORDER.index(old_failed_stage) if old_failed_stage in STAGE_ORDER else 0
            new_idx = STAGE_ORDER.index(new_result.failed_stage) if new_result.failed_stage in STAGE_ORDER else 0
            if new_idx > old_idx:
                progressed = True
                ep.findings.append(f"Pipeline advanced from '{old_failed_stage}' to '{new_result.failed_stage}'.")
            else:
                msg = "Pipeline still failing at the same stage."
                if ep.cascading_faults:
                    msg += (
                        " This incident has cascading faults — the previous fix may have "
                        "resolved the first fault but exposed a second independent fault."
                    )
                ep.findings.append(msg)
        elif old_status == SimPipelineStatus.FAILED and str(new_result.status) == SimPipelineStatus.FAILED:
            ep.findings.append("Rerun shows failure unchanged; refine diagnosis.")

        ep.last_rerun_progressed = progressed

        if progressed:
            ep.fix_hits += 1
            if str(new_result.status) == SimPipelineStatus.PASSED:
                ep.findings.append(
                    "Pipeline PASSED. Call verify_fix next (required before finalize) — "
                    "then immediately call finalize. Do not apply more fixes."
                )
            else:
                ep.findings.append(
                    "Pipeline progressed but is not yet passing. Call verify_fix next."
                )
            return
        return

    def _handle_verify_fix(self, ep: EpisodeState) -> None:
        if not ep.pipeline_result:
            ep.findings.append("No pipeline run to verify.")
            return
        if ep.verified_for_latest_rerun:
            ep.findings.append("Latest rerun is already verified.")
            return
        if ep.rerun_attempts == 0:
            ep.findings.append("Cannot verify: run rerun_pipeline after applying a fix before calling verify_fix.")
            return
        if str(ep.pipeline_result.status) == SimPipelineStatus.PASSED:
            ep.incident_resolved = True
            ep.verified_for_latest_rerun = True
            ep.findings.append("VERIFIED: fix resolved the incident. Call finalize now.")
            return
        elif ep.last_rerun_progressed:
            ep.verified_for_latest_rerun = True
            ep.findings.append("Verification confirms partial progress.")
            return
        else:
            ep.findings.append("Verification failed: pipeline still shows unresolved failures.")
            return

    def _handle_finalize(self, ep: EpisodeState) -> None:
        if not ep.verified_for_latest_rerun:
            ep.findings.append("Run verify_fix before finalize")
            return
        if ep.rerun_attempts == 0:
            ep.findings.append("Finalize without running rerun_pipeline — no terminal bonus awarded.")
            return
        pipeline_passed = (
            ep.pipeline_result is not None
            and str(ep.pipeline_result.status) == SimPipelineStatus.PASSED
        )
        bonus, note = self._adv_judge.score_terminal(
            incident_resolved=ep.incident_resolved,
            verified=ep.verified_for_latest_rerun,
            pipeline_passed=pipeline_passed,
            cascading_fault_count=len(ep.cascading_faults),
        )
        ep.findings.append(f"[Judge] terminal: {note} (legacy bonus suppressed: {bonus:+.2f})")
        return

    # ── Scenario injection helpers ──────────────────────────────────────────

    def _inject_scenario(self, repo_dir: str, scenario) -> list:
        results = []
        for step in scenario.steps:
            try:
                meta = inject_fault_simulated(repo_dir, step.fault_type)
                results.append(meta)
            except Exception as exc:
                logger.warning("[sim] Could not inject fault %s: %s", step.fault_type, exc)
        return results

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _get_difficulty(self, fault_type: str) -> str:
        difficulty_map = {
            "merge_conflict":      "easy",
            "dependency_conflict": "medium",
            "docker_order":        "medium",
            "flaky_test":          "easy",
            "missing_permission":  "hard",
            "secret_exposure":     "security",
            "env_drift":           "network",
            "invalid_database_url": "network",
            "empty_secret_key":     "security",
            "missing_pythonpath":   "hard",
            "circular_import_runtime": "hard",
            "missing_package_init": "medium",
            "none_config_runtime":  "medium",
            "log_pii_leak":        "security",
            "log_disabled":        "medium",
            "bad_migration_sql":   "medium",
            "schema_drift":        "medium",
        }
        return difficulty_map.get(fault_type, "medium")

    def _get_max_steps(self, fault_type: str, cascade_count: int = 0) -> int:
        difficulty = self._get_difficulty(fault_type)
        max_steps_map = {"easy": 16, "medium": 20, "network": 20, "security": 20, "hard": 25}
        base = max_steps_map.get(difficulty, 20)
        return base + cascade_count * 4

    def _is_destructive_fix(self, value: str) -> bool:
        normalized = (value or "").strip().lower()
        return any(phrase in normalized for phrase in DESTRUCTIVE_FIXES)

    def _consume_log_budget(self, ep: EpisodeState, log_text: str, *, minimum_cost: int) -> Optional[int]:
        if minimum_cost <= 0:
            return 0
        if minimum_cost <= 1:
            cost = 1
        else:
            line_count = max(1, len([line for line in str(log_text or "").splitlines() if line.strip()]))
            cost = max(minimum_cost, min(40, line_count // 4))
        if cost > ep.log_tokens_remaining:
            return None
        ep.log_tokens_remaining = max(0, ep.log_tokens_remaining - cost)
        return cost

    def _available_operations_for_episode(self, ep: EpisodeState) -> List[str]:
        if ep.log_tokens_remaining <= 0:
            return [op for op in CANONICAL_OPERATIONS if op != "view_logs"]
        return list(CANONICAL_OPERATIONS)

    def _build_step_observation(self, ep: EpisodeState, reward: float, done: bool) -> dict:
        fault_type = ep.fault_metadata.fault_type if ep.fault_metadata else "unknown"
        difficulty = self._get_difficulty(fault_type)

        action_history = [
            f"{e['operation']}:{e.get('target', '')}:{e.get('value', '')}".strip(":")
            for e in ep.history[-16:]
        ]

        final_score = 0.0
        deterministic_score = 0.0

        if done:
            deterministic_score = self._compute_final_score(ep)
            final_score = deterministic_score
            ep.deterministic_score = deterministic_score

            self._curriculum.record_episode(
                fault_type=fault_type,
                difficulty=ep.curriculum_difficulty,
                final_score=final_score,
                resolved=ep.incident_resolved,
                steps_used=self._state.step_count,
            )

        obs = build_observation(
            pipeline_result=ep.pipeline_result,
            workspace_dir=ep.workspace_dir,
            task_id=f"sim_{fault_type}",
            task_title=f"Fix {fault_type.replace('_', ' ')} in CI/CD pipeline (simulated)",
            difficulty=difficulty,
            available_tools=self._available_operations_for_episode(ep),
            reward=reward,
            done=done,
            action_history=action_history,
            current_hypothesis=ep.current_hypothesis,
            attempted_fix=ep.attempted_fix,
            hypothesis_history=ep.hypothesis_history[-8:],
            incident_resolved=ep.incident_resolved,
            pipeline_health=ep.pipeline_health,
            recovery_cost=ep.recovery_cost,
            redundant_actions=ep.redundant_actions,
            destructive_actions=ep.destructive_actions,
            final_score=final_score,
            deterministic_score=deterministic_score,
            log_tokens_remaining=ep.log_tokens_remaining,
            log_access_mode="tail_only" if ep.log_tokens_remaining <= 0 else "full",
            findings=ep.findings[-10:],
            metadata={
                "task_key": fault_type,
                "fault_type": fault_type,
                "expected_fail_stage": ep.fault_metadata.expected_fail_stage if ep.fault_metadata else "",
                "ready_to_finalize": ep.incident_resolved and ep.verified_for_latest_rerun,
                "verification_required": ep.incident_resolved and not ep.verified_for_latest_rerun,
                "verified_since_last_rerun": ep.verified_for_latest_rerun,
                "supported_operations": self._available_operations_for_episode(ep),
                "canonical_operations": CANONICAL_OPERATIONS,
                "log_tokens_remaining": ep.log_tokens_remaining,
                "log_budget_total": ep.log_budget_total,
                "log_access_mode": "tail_only" if ep.log_tokens_remaining <= 0 else "full",
                "last_log_access": ep.last_log_access,
                "simulated": True,
            },
        )

        if ep.errors_stale_after_fix:
            obs["surfaced_errors"] = [
                "[Errors from previous run — rerun pipeline to see current state]"
            ]

        return obs

    def _compute_final_score(self, ep: EpisodeState) -> float:
        score = 0.0
        success_bonus = 0.0
        if ep.incident_resolved and ep.verified_for_latest_rerun:
            success_bonus = 0.08
        elif ep.last_rerun_progressed:
            success_bonus = 0.03

        penalty = min(0.25, (ep.redundant_actions * 0.04) + (ep.destructive_actions * 0.12) + (ep.wrong_fixes * 0.05))
        score = (score - penalty) * ep.pipeline_health

        genuine_work = ep.fix_hits > 0 and ep.rerun_attempts > 0
        if not genuine_work:
            success_bonus = 0.0

        return round(max(0.0, min(1.0, score + success_bonus)), 3)

    def _dict_to_observation(self, obs_dict: dict) -> MetaHackathonObservation:
        return MetaHackathonObservation(**obs_dict)

    def _error_observation(self, message: str, reward: float = 0.0) -> MetaHackathonObservation:
        return MetaHackathonObservation(
            pipeline_status="error",
            reward=reward,
            done=False,
            metadata={"error": message},
        )

    def _cleanup_episode(self, ep: EpisodeState) -> None:
        if ep.pipeline_runner and hasattr(ep.pipeline_runner, "cleanup"):
            try:
                ep.pipeline_runner.cleanup()
            except Exception:
                pass
        workspace_base = os.path.dirname(ep.workspace_dir)
        if workspace_base and os.path.exists(workspace_base) and "cicd-sim-episode" in workspace_base:
            try:
                shutil.rmtree(workspace_base, ignore_errors=True)
            except Exception:
                pass

    def close(self) -> None:
        if self._episode:
            self._cleanup_episode(self._episode)
            self._episode = None
