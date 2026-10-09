"""Subprocess-backed CI/CD pipeline runner.

Replaces synthetic log generation with real tool invocations:
- uv venv + uv pip install  → real resolver errors, real dependency conflicts
- pytest                    → real assertion tracebacks, real import errors
- uvicorn startup probe     → real import errors, real startup crashes

Each episode gets an isolated directory under /tmp/episode_{id}/ containing:
  workspace/  ← the fault-injected copy of sample-app (already created by environment.py)
  venv/       ← uv-managed venv, created on first run()

Git workflow:
  On first run: git init in workspace, commit faulted state on 'main',
                create feature branch 'fix/episode-{id[:8]}'.
  After each fix application: commit on the feature branch.
  On each pipeline run: show diff stat (simulated PR) in clone stage output.

Toggle: set CICD_SUBPROCESS_RUNNER=1 to activate; default falls back to
        SimulatedPipelineRunner.

Public API is identical to SimulatedPipelineRunner so environment.py needs
no changes other than the runner selection.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from models import AdversarialCICDScenario

from cicd.simulated_runner import (
    FAULT_STAGE_MAP,
    STAGE_ORDER,
    STAGE_WEIGHTS,
    StageStatus,
    PipelineStatus,
    SimulatedStageResult,
    SimulatedPipelineResult,
    _sw,
    _score_fix,
    _run_secret_scan,
    _run_log_config_check,
    _partial_fix_warnings,
)
from cicd.github_actions_simulator import (
    STAGE_ORDER as WORKFLOW_STAGE_ORDER,
    discover_workflow_files,
    execute_workflow_stage,
    parse_workflow_file,
)
from cicd.terraform_simulator import has_terraform_config, simulate_terraform_pipeline


# ── venv path helper ───────────────────────────────────────────────────────

def _venv_python(venv_dir: str) -> str:
    if sys.platform == "win32":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


# ── subprocess helper ──────────────────────────────────────────────────────

def _run(
    cmd: List[str],
    *,
    cwd: Optional[str] = None,
    timeout: int = 60,
    env: Optional[dict] = None,
) -> Tuple[int, str, str]:
    """Run a subprocess and return (exit_code, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"Stage timed out after {timeout}s"
    except OSError as exc:
        return 1, "", f"Failed to launch subprocess: {exc}"


# ── git helpers ────────────────────────────────────────────────────────────

def _git(args: List[str], cwd: str, timeout: int = 10) -> Tuple[int, str, str]:
    return _run(["git"] + args, cwd=cwd, timeout=timeout)


def _ensure_git_repo(workspace: str, episode_short: str) -> Tuple[bool, str]:
    """Initialise git repo with faulted-state baseline + feature branch.

    Returns (already_had_feature_branch, branch_name).
    """
    branch = f"fix/episode-{episode_short}"

    # Check if repo already initialised
    rc, out, _ = _git(["rev-parse", "--is-inside-work-tree"], workspace)
    if rc != 0:
        # Fresh init
        _git(["init", "-b", "main"], workspace)
        _git(["config", "user.email", "ci-runner@sandbox.local"], workspace)
        _git(["config", "user.name", "CI Runner"], workspace)
        _git(["add", "-A"], workspace)
        _git(["commit", "-m", "initial: faulted state"], workspace)
        _git(["checkout", "-b", branch], workspace)
        return False, branch

    # Repo exists — check if feature branch exists
    rc, _, _ = _git(["rev-parse", "--verify", branch], workspace)
    if rc != 0:
        _git(["checkout", "-b", branch], workspace)
        return False, branch

    # Feature branch already exists, switch to it
    _git(["checkout", branch], workspace)
    return True, branch


def _commit_fixes(workspace: str) -> None:
    """Stage all changes and commit on current branch (after agent applies a fix)."""
    rc, out, _ = _git(["status", "--porcelain"], workspace)
    if rc == 0 and out.strip():
        _git(["add", "-A"], workspace)
        _git(["commit", "-m", "fix: agent patch"], workspace)


def _pr_summary(workspace: str) -> str:
    """Return a diff stat between main and current branch (simulated PR view)."""
    rc, out, _ = _git(["diff", "main..HEAD", "--stat"], workspace, timeout=5)
    if rc == 0 and out.strip():
        return out.strip()
    return "(no changes vs main)"


def _git_log_short(workspace: str) -> str:
    rc, out, _ = _git(["log", "--oneline", "-5"], workspace, timeout=5)
    return out.strip() if rc == 0 else ""


# ── SubprocessPipelineRunner ───────────────────────────────────────────────

class SubprocessPipelineRunner:
    """Real subprocess-backed CI/CD pipeline runner.

    Drop-in replacement for SimulatedPipelineRunner.  Identical public API:
      run(workspace_dir=None) → SimulatedPipelineResult
      run_stage(stage_name, workspace_dir=None) → SimulatedStageResult
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
        self.episode_id = episode_id or "sub-episode"

        self._active_faults: List[str] = []
        if scenario and hasattr(scenario, "steps"):
            self._active_faults = [step.fault_type for step in scenario.steps]
        elif fault_type:
            self._active_faults = [fault_type]

        self._episode_short = self.episode_id[:8]

        # Per-episode venv lives next to the workspace
        episode_root = os.path.join(tempfile.gettempdir(), f"episode_{self.episode_id}")
        self._venv_dir = os.path.join(episode_root, "venv")
        self._venv_ready = False
        self._git_initialised = False
        self._workflow_loaded = False
        self._workflow: Optional[Any] = None

    # ── Cleanup ────────────────────────────────────────────────────────────

    def cleanup(self) -> None:
        """Remove the per-episode venv directory."""
        episode_root = os.path.dirname(self._venv_dir)
        if os.path.exists(episode_root):
            try:
                shutil.rmtree(episode_root, ignore_errors=True)
            except OSError:
                pass

    # ── Public run ─────────────────────────────────────────────────────────

    def run(self, workspace_dir: Optional[str] = None) -> SimulatedPipelineResult:
        ws = workspace_dir or self.workspace_path

        result = SimulatedPipelineResult(
            pipeline_id=f"sub-{self._episode_short}",
            status=_sw(PipelineStatus.RUNNING),
            workspace_dir=ws,
            image_tag=f"sample-app-{self._episode_short}",
            cache_tag="sample-app-cache",
        )

        start_time = time.time()

        fault_status = self._score_all_faults(ws)
        faults_by_stage = self._group_faults_by_stage(fault_status)

        for stage_name in STAGE_ORDER:
            stage = result.stages[stage_name]
            stage.status = _sw(StageStatus.RUNNING)

            exit_code, stdout, stderr, command = self._execute_stage(
                stage_name, ws, faults_by_stage.get(stage_name, []), fault_status
            )

            stage.exit_code = exit_code
            stage.stdout = stdout
            stage.stderr = stderr
            stage.command = command
            stage.duration_seconds = round(time.time() - start_time, 2)

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

        result.total_duration_seconds = round(time.time() - start_time, 2)
        return result

    def run_stage(
        self, stage_name: str, workspace_dir: Optional[str] = None
    ) -> SimulatedStageResult:
        ws = workspace_dir or self.workspace_path

        fault_status = self._score_all_faults(ws)
        faults_by_stage = self._group_faults_by_stage(fault_status)
        active = faults_by_stage.get(stage_name, [])

        t0 = time.time()
        exit_code, stdout, stderr, command = self._execute_stage(
            stage_name, ws, active, fault_status
        )
        duration = round(time.time() - t0, 2)

        return SimulatedStageResult(
            name=stage_name,
            status=_sw(StageStatus.PASSED if exit_code == 0 else StageStatus.FAILED),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            command=command,
        )

    # ── Internal dispatch ──────────────────────────────────────────────────

    def _execute_stage(
        self,
        stage_name: str,
        ws: str,
        active_faults: List[str],
        fault_status: Dict,
    ) -> Tuple[int, str, str, str]:
        workflow_stage = self._try_workflow_stage(stage_name, ws)
        if workflow_stage is not None:
            exit_code, stdout, stderr = workflow_stage
            return exit_code, stdout, stderr, f"github-actions-stage:{stage_name}"

        if stage_name == "clone":
            exit_code, stdout, stderr = self._stage_clone(ws)
            return exit_code, stdout, stderr, "git log --oneline -5"
        if stage_name == "build":
            exit_code, stdout, stderr = self._stage_build(ws, active_faults, fault_status)
            return exit_code, stdout, stderr, "uv pip install -r services/api/requirements.txt"
        if stage_name == "test":
            exit_code, stdout, stderr = self._stage_test(ws, active_faults, fault_status)
            return exit_code, stdout, stderr, "python -m pytest tests/ --tb=short -q"
        if stage_name == "deploy":
            exit_code, stdout, stderr = self._stage_deploy(ws, active_faults, fault_status)
            return exit_code, stdout, stderr, "uvicorn services.api.app:app --host 127.0.0.1 --port 0"
        return 1, "", f"Unknown stage: {stage_name}", ""

    def _try_workflow_stage(
        self,
        stage_name: str,
        workspace_path: str,
    ) -> Optional[Tuple[int, str, str]]:
        if stage_name not in WORKFLOW_STAGE_ORDER:
            return None
        workflow = self._load_workflow(workspace_path)
        if workflow is None:
            return None
        return execute_workflow_stage(
            workflow=workflow,
            stage_name=stage_name,
            workspace_path=workspace_path,
            base_env={},
        )

    def _load_workflow(self, workspace_path: str):
        if self._workflow_loaded:
            return self._workflow
        self._workflow_loaded = True
        for workflow_file in discover_workflow_files(workspace_path):
            parsed = parse_workflow_file(workflow_file)
            if parsed is not None:
                self._workflow = parsed
                break
        return self._workflow

    # ── Stage: clone ───────────────────────────────────────────────────────

    def _stage_clone(self, ws: str) -> Tuple[int, str, str]:
        # Ensure git repo + feature branch exist
        had_branch, branch = _ensure_git_repo(ws, self._episode_short)
        self._git_initialised = True

        log_lines = _git_log_short(ws)
        diff_stat = _pr_summary(ws)

        stdout = (
            f"Branch: {branch}\n"
            f"\n--- Simulated PR: fix/episode-{self._episode_short} -> main ---\n"
            f"{diff_stat}\n"
            f"\nRecent commits:\n{log_lines}\n"
        )
        return 0, stdout, ""

    # ── Stage: build ───────────────────────────────────────────────────────

    def _stage_build(
        self,
        ws: str,
        active_faults: List[str],
        fault_status: Dict,
    ) -> Tuple[int, str, str]:
        warnings = _partial_fix_warnings(self._active_faults, "build", fault_status)

        self._ensure_venv()

        req_file = os.path.join(ws, "services", "api", "requirements.txt")
        if not os.path.exists(req_file):
            stderr = "ERROR: services/api/requirements.txt not found"
            if warnings:
                stderr = warnings + "\n" + stderr
            return 1, "", stderr

        # Install uvicorn alongside app deps so deploy stage can use it
        extra_deps = ["uvicorn[standard]"]
        install_cmd = [
            "uv", "pip", "install",
            "--python", _venv_python(self._venv_dir),
            "-r", req_file,
        ] + extra_deps

        rc, stdout, stderr = _run(install_cmd, cwd=ws, timeout=90)

        if rc != 0:
            if warnings:
                stderr = warnings + "\n" + stderr
            return rc, stdout, stderr

        # Real secret scan
        scan_rc, scan_out, scan_err = _run_secret_scan(ws)
        if scan_rc != 0:
            combined_err = scan_err
            if warnings:
                combined_err = warnings + "\n" + combined_err
            return 1, scan_out, combined_err

        # Real log config check
        log_rc, log_out, log_err = _run_log_config_check(ws)
        if log_rc != 0:
            combined_err = log_err
            if warnings:
                combined_err = warnings + "\n" + combined_err
            return 1, log_out, combined_err

        full_stdout = stdout + "\n" + scan_out + log_out
        return 0, full_stdout, ""

    # ── Stage: test ────────────────────────────────────────────────────────

    def _stage_test(
        self,
        ws: str,
        active_faults: List[str],
        fault_status: Dict,
    ) -> Tuple[int, str, str]:
        warnings = _partial_fix_warnings(self._active_faults, "test", fault_status)

        python = _venv_python(self._venv_dir)
        if not os.path.exists(python):
            # venv wasn't built (build stage failed); fall back to sys python
            python = sys.executable

        rc, stdout, stderr = _run(
            [python, "-m", "pytest", "tests/", "--tb=short", "-q"],
            cwd=ws,
            timeout=60,
        )

        if warnings and rc != 0:
            stderr = warnings + "\n" + stderr

        return rc, stdout, stderr

    # ── Stage: deploy ──────────────────────────────────────────────────────

    def _stage_deploy(
        self,
        ws: str,
        active_faults: List[str],
        fault_status: Dict,
    ) -> Tuple[int, str, str]:
        warnings = _partial_fix_warnings(self._active_faults, "deploy", fault_status)
        if has_terraform_config(ws):
            tf_code, tf_out, tf_err = simulate_terraform_pipeline(ws)
            if tf_code != 0:
                if warnings and tf_err:
                    tf_err = warnings + "\n" + tf_err
                elif warnings:
                    tf_err = warnings
                return 1, tf_out, tf_err
            if tf_out:
                # IaC runs before application startup checks so state changes are visible.
                tf_prefix = tf_out + "\n\n"
            else:
                tf_prefix = ""
        else:
            tf_prefix = ""

        python = _venv_python(self._venv_dir)
        if not os.path.exists(python):
            python = sys.executable

        # Build a PYTHONPATH that includes the workspace so imports resolve
        env = os.environ.copy()
        env["PYTHONPATH"] = ws

        # Uvicorn startup probe with a short timeout
        # Port 0 = OS picks a free port, no cross-episode conflicts
        proc_cmd = [
            python, "-m", "uvicorn",
            "services.api.app:app",
            "--host", "127.0.0.1",
            "--port", "0",
            "--workers", "1",
            "--timeout-graceful-shutdown", "0",
        ]

        stdout_chunks: List[str] = []
        stderr_chunks: List[str] = []
        startup_ok = False

        try:
            proc = subprocess.Popen(
                proc_cmd,
                cwd=ws,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            deadline = time.time() + 12
            # Read stderr line-by-line; uvicorn writes startup messages there
            def _drain(pipe, buf):
                for line in pipe:
                    buf.append(line)

            t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_chunks), daemon=True)
            t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_chunks), daemon=True)
            t_out.start()
            t_err.start()

            while time.time() < deadline:
                combined = "".join(stderr_chunks)
                if "Application startup complete" in combined:
                    startup_ok = True
                    break
                # Import error / traceback detected → bail early
                if "Traceback (most recent call last)" in combined or "ModuleNotFoundError" in combined:
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.1)

            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

            t_out.join(timeout=2)
            t_err.join(timeout=2)

        except OSError as exc:
            stderr_chunks.append(f"Failed to launch uvicorn: {exc}")

        captured_out = "".join(stdout_chunks)
        captured_err = "".join(stderr_chunks)

        if startup_ok:
            stdout = (
                tf_prefix +
                captured_err + captured_out +
                "\nINFO:     Application startup complete.\n"
                "Deploy probe: startup successful — all services healthy."
            )
            return 0, stdout, ""
        else:
            err = captured_err or captured_out or "Uvicorn startup failed (no output)"
            if warnings:
                err = warnings + "\n" + err
            return 1, "", err

    # ── Helpers ────────────────────────────────────────────────────────────

    def _ensure_venv(self) -> None:
        if self._venv_ready and os.path.exists(_venv_python(self._venv_dir)):
            return
        os.makedirs(os.path.dirname(self._venv_dir), exist_ok=True)
        _run(["uv", "venv", self._venv_dir], timeout=30)
        self._venv_ready = True

    def _score_all_faults(self, ws: str) -> Dict:
        result = {}
        for fault in self._active_faults:
            score, failing = _score_fix(ws, fault)
            result[fault] = (score == 1.0, score, failing)
        return result

    def _group_faults_by_stage(self, fault_status: Dict) -> Dict[str, List[str]]:
        by_stage: Dict[str, List[str]] = {s: [] for s in STAGE_ORDER}
        for fault, (fully_fixed, _, _) in fault_status.items():
            if not fully_fixed:
                stage = FAULT_STAGE_MAP.get(fault, "build")
                by_stage[stage].append(fault)
        return by_stage

    def commit_agent_fixes(self) -> None:
        """Call after apply_fix_simulated to commit the changes on the feature branch."""
        _commit_fixes(self.workspace_path)
