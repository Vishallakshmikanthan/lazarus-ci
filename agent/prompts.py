"""Prompt construction for the agentic inference baseline."""

import os
import textwrap
from pathlib import Path
from typing import Dict, List

BASE_SYSTEM_PROMPT_WS = textwrap.dedent(
    """
        You are a CI/CD repair agent. The pipeline has failed and you have the failure logs.
        
        CORE RULES:
        1. Read the incident alert completely — it names the file and often hints at the fix
        2. Read ONLY the file mentioned in the error (Exception: Docker build errors → read Dockerfile)
        3. write_file requires COMPLETE file content, not a diff
        4. Merge conflicts: remove <<<<<<< HEAD, =======, >>>>>>> markers and keep correct code
        5. Never repeat failed actions or negative-reward hypotheses
        6. Multiple errors = multiple independent fixes (cascading faults)

        TOOLS:
        read_file → set_hypothesis → write_file → trigger_pipeline → finalize

        CRITICAL: 
        - trigger_pipeline is for verification ONLY (logs already provided)
        - finalize ONLY when pipeline passes
        - write_file ONLY after read_file on same path
    """
).strip()

BASE_SYSTEM_PROMPT = textwrap.dedent(
    """
        You are a CI/CD repair agent. Debug broken pipelines efficiently.

        CORE RULES:
        1. surfaced_errors names the file — inspect that file first
        2. modify_config requires JSON: {"file": "path", "action": "replace", "old": "...", "new": "..."}
        3. Never repeat failed actions or negative-reward hypotheses
        4. Cascading faults: new error after fix = new independent fault, needs separate fix
        5. Merge conflicts: remove <<<<<<< HEAD, =======, >>>>>>> markers with JSON replace

        SEQUENCE:
        view_logs → inspect_config → set_hypothesis → modify_config/add_dependency → 
        rerun_pipeline → verify_fix → finalize

        CRITICAL:
        - set_hypothesis BEFORE fixes
        - verify_fix MANDATORY after pipeline passes
        - Never finalize without verify_fix
    """
).strip()

GENERAL_SKILL_CARDS: Dict[str, str] = {
    "Evidence-First Triage": (
        "Before any fix, collect at least one log signal and one config/infra clue for the active stage."
    ),
    "Hypothesis Quality": (
        "Hypotheses must mention concrete entities from evidence (service/stage/error signature), not generic guesses."
    ),
    "Safe Remediation": (
        "Prefer minimal reversible fixes. Never use destructive shortcuts like disabling checks or skipping validations."
    ),
    "Verification Discipline": (
        "After rerun_pipeline, verify_fix is mandatory before finalize. If verification fails, return to evidence gathering."
    ),
    "Efficiency Control": (
        "Avoid repeated identical low-signal actions; when progress stalls, switch stage or tool based on newest surfaced error."
    ),
}

TASK_SKILL_CARDS: Dict[str, List[str]] = {
    "merge_conflict": [
        "Trigger pipeline FIRST — the build error will name the exact file with conflict markers.",
        "Read the named file with read_file to see the full <<<<<<< HEAD / ======= / >>>>>>> block.",
        "Choose one side of the conflict (usually the feature branch side after >>>>>>>), remove all three marker lines, write the clean file back with write_file.",
        "Trigger pipeline again to verify, then finalize.",
    ],
    "easy": [
        "Trigger pipeline first to identify which file contains the fault.",
        "Read the specific file named in the error before writing any fix.",
        "Use write_file with the complete corrected file content to resolve the fault.",
    ],
    "flaky": [
        "Treat intermittent test failures as flaky/timing candidates when logs show pass-on-retry behavior.",
        "Prefer retry policy or test isolation fixes over broad application logic rewrites.",
    ],
    "docker_order": [
        "The error 'Step N/N : RUN pip install ... No such file or directory' is a Dockerfile build failure.",
        "The requirements file exists in the workspace — it just has not been COPY'd into the image yet.",
        "Read Dockerfile. Move the COPY line for requirements.txt BEFORE the RUN uv pip install line.",
        "Write the corrected Dockerfile, then trigger_pipeline to verify.",
    ],
    "medium": [
        "Solve dependency compatibility first (requests/urllib3), then Docker install order.",
        "Use add_dependency for version pinning and modify_config for Docker order corrections.",
        "Expect cascading faults: after fixing the first error, re-read surfaced_errors for a new independent fault (e.g. logging config, Docker order). Apply a separate fix for each.",
    ],
    "network": [
        "Classify DNS and timeout upload failures as transient external dependency outages when evidence supports it.",
        "Use retry/backoff or proxy fallback mitigation; avoid rewriting application upload logic.",
    ],
    "security": [
        "Treat IAM writer permission and secret exposure as separate required remediations.",
        "Do not finalize until both security issues are fixed and verified.",
    ],
    "hard": [
        "Resolve upstream publisher permissions before downstream deploy tuning.",
        "After rollback, collect fresh deploy evidence before timeout hypothesis and tuning.",
    ],
}


def _load_external_skill_text() -> str:
    """Load optional user-provided skill text from env for quick prompt iteration."""
    inline_skills = (os.getenv("EXTRA_SKILLS") or "").strip()
    if inline_skills:
        return inline_skills

    skills_file = (os.getenv("LLM_SKILLS_FILE") or "").strip()
    if not skills_file:
        return ""

    path = Path(skills_file)
    if not path.exists() or not path.is_file():
        return ""

    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def build_system_prompt(task_name: str, ws_mode: bool = False) -> str:
    base = BASE_SYSTEM_PROMPT_WS if ws_mode else BASE_SYSTEM_PROMPT
    
    # Only include task-specific hints for known complex patterns
    task_lines = TASK_SKILL_CARDS.get(task_name, [])
    if task_lines:
        task_section = "\n\nTask hints:\n" + "\n".join(f"- {line}" for line in task_lines[:2])  # Max 2 hints
    else:
        task_section = ""

    external_skills = _load_external_skill_text()
    external_section = f"\n\n{external_skills}" if external_skills else ""

    return f"{base}{task_section}{external_section}"

