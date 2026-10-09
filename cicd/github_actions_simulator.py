from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from cicd.terraform_simulator import simulate_terraform_command

try:
    import yaml
except Exception:  # pragma: no cover - optional dependency at runtime
    yaml = None


STAGE_ORDER = ["clone", "build", "test", "deploy"]


@dataclass
class WorkflowStep:
    name: str
    run: str = ""
    uses: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    retries: int = 0


@dataclass
class WorkflowJob:
    name: str
    env: Dict[str, str] = field(default_factory=dict)
    steps: List[WorkflowStep] = field(default_factory=list)


@dataclass
class WorkflowDefinition:
    name: str
    source_file: str
    jobs: List[WorkflowJob] = field(default_factory=list)


def discover_workflow_files(workspace_path: str) -> List[str]:
    workflows_dir = os.path.join(workspace_path, ".github", "workflows")
    files: List[str] = []
    if os.path.isdir(workflows_dir):
        for entry in sorted(os.listdir(workflows_dir)):
            if entry.endswith((".yml", ".yaml")):
                files.append(os.path.join(workflows_dir, entry))
    if files:
        return files
    fallback_github_dir = os.path.join(workspace_path, ".github")
    if os.path.isdir(fallback_github_dir):
        for entry in sorted(os.listdir(fallback_github_dir)):
            if entry.endswith((".yml", ".yaml")):
                files.append(os.path.join(fallback_github_dir, entry))
    return files


def parse_workflow_file(path: str) -> Optional[WorkflowDefinition]:
    if yaml is None:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = yaml.safe_load(f) or {}
    except Exception:
        return None
    jobs_raw = raw.get("jobs")
    if not isinstance(jobs_raw, dict) or not jobs_raw:
        return None

    workflow = WorkflowDefinition(
        name=str(raw.get("name") or os.path.basename(path)),
        source_file=path,
        jobs=[],
    )
    for job_id, job_raw in jobs_raw.items():
        if not isinstance(job_raw, dict):
            continue
        steps_raw = job_raw.get("steps") or []
        if not isinstance(steps_raw, list):
            continue
        job = WorkflowJob(name=str(job_id), env=_normalize_env(job_raw.get("env")))
        for index, step_raw in enumerate(steps_raw, start=1):
            if not isinstance(step_raw, dict):
                continue
            step_name = str(step_raw.get("name") or f"Step {index}")
            retries = _read_retries(step_raw)
            job.steps.append(
                WorkflowStep(
                    name=step_name,
                    run=str(step_raw.get("run") or ""),
                    uses=str(step_raw.get("uses") or ""),
                    env=_normalize_env(step_raw.get("env")),
                    retries=retries,
                )
            )
        if job.steps:
            workflow.jobs.append(job)
    return workflow if workflow.jobs else None


def infer_stage_for_step(step: WorkflowStep) -> str:
    label = f"{step.name} {step.run} {step.uses}".lower()
    if "checkout" in label:
        return "clone"
    if "pytest" in label or " test" in label or label.startswith("test"):
        return "test"
    if "deploy" in label or "kubectl" in label or "helm" in label or "compose" in label:
        return "deploy"
    if "docker build" in label or " build" in label or label.startswith("build"):
        return "build"
    return "build"


def execute_workflow_stage(
    *,
    workflow: WorkflowDefinition,
    stage_name: str,
    workspace_path: str,
    base_env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    stage_stdout: List[str] = []
    stage_stderr: List[str] = []
    default_env = dict(os.environ)
    if base_env:
        default_env.update(base_env)

    stage_stdout.append(f"::group::Workflow {workflow.name} ({os.path.basename(workflow.source_file)})")
    matched_steps = 0
    for job in workflow.jobs:
        stage_stdout.append(f"##[group]Job: {job.name}")
        for step_index, step in enumerate(job.steps, start=1):
            if infer_stage_for_step(step) != stage_name:
                continue
            matched_steps += 1
            code, out, err = _execute_step(
                job=job,
                step=step,
                step_index=step_index,
                workspace_path=workspace_path,
                default_env=default_env,
            )
            if out:
                stage_stdout.append(out)
            if err:
                stage_stderr.append(err)
            if code != 0:
                stage_stdout.append("##[endgroup]")
                stage_stdout.append("::endgroup::")
                return code, "\n".join(stage_stdout), "\n".join(stage_stderr)
        stage_stdout.append("##[endgroup]")
    if matched_steps == 0:
        stage_stdout.append(f"No workflow steps mapped to stage '{stage_name}'.")
    stage_stdout.append("::endgroup::")
    return 0, "\n".join(stage_stdout), "\n".join(stage_stderr)


def _execute_step(
    *,
    job: WorkflowJob,
    step: WorkflowStep,
    step_index: int,
    workspace_path: str,
    default_env: Dict[str, str],
) -> Tuple[int, str, str]:
    attempts = max(1, step.retries + 1)
    merged_env = dict(default_env)
    merged_env.update(job.env)
    merged_env.update(step.env)
    env_keys = ", ".join(sorted(step.env.keys())) if step.env else "none"
    title = f"##[group]Step {step_index}: {step.name}"
    if step.uses:
        if "actions/checkout" in step.uses.lower():
            body = (
                f"{title}\n"
                f"Using action `{step.uses}`\n"
                "Syncing repository state into workspace...\n"
                "Checkout complete.\n"
                "##[endgroup]"
            )
            return 0, body, ""
        body = (
            f"{title}\n"
            f"Using action `{step.uses}` (simulated)\n"
            "Action execution complete.\n"
            "##[endgroup]"
        )
        return 0, body, ""

    if not step.run.strip():
        body = f"{title}\nSkipping empty step.\n##[endgroup]"
        return 0, body, ""

    step_stdout: List[str] = [title, f"Environment keys: {env_keys}"]
    step_stderr: List[str] = []
    for attempt in range(1, attempts + 1):
        if attempts > 1:
            step_stdout.append(f"Attempt {attempt}/{attempts}")
        step_stdout.append(f"$ {step.run}")
        started = time.time()
        if "terraform " in step.run.lower():
            return_code, tf_stdout, tf_stderr = simulate_terraform_command(workspace_path, step.run)
            elapsed = time.time() - started
            if tf_stdout:
                step_stdout.append(tf_stdout.rstrip())
            if tf_stderr:
                step_stderr.append(tf_stderr.rstrip())
        else:
            proc = subprocess.run(
                step.run,
                cwd=workspace_path,
                shell=True,
                text=True,
                capture_output=True,
                env=merged_env,
            )
            elapsed = time.time() - started
            return_code = proc.returncode
            if proc.stdout:
                step_stdout.append(proc.stdout.rstrip())
            if proc.stderr:
                step_stderr.append(proc.stderr.rstrip())
        if return_code == 0:
            step_stdout.append(f"Step succeeded in {elapsed:.2f}s")
            step_stdout.append("##[endgroup]")
            return 0, "\n".join(step_stdout), "\n".join(step_stderr)
        step_stderr.append(f"Step failed with exit code {return_code} ({elapsed:.2f}s)")
        if attempt < attempts:
            step_stdout.append("Retrying failed step...")
    step_stdout.append("##[endgroup]")
    return 1, "\n".join(step_stdout), "\n".join(step_stderr)


def _normalize_env(raw_env: Any) -> Dict[str, str]:
    if not isinstance(raw_env, dict):
        return {}
    return {str(k): str(v) for k, v in raw_env.items()}


def _read_retries(step_raw: Dict[str, Any]) -> int:
    value = step_raw.get("retry")
    if value is None:
        value = step_raw.get("retries")
    if value is None and isinstance(step_raw.get("with"), dict):
        value = step_raw["with"].get("retries")
    try:
        parsed = int(value)
    except Exception:
        parsed = 0
    return max(0, min(parsed, 5))
