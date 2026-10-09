"""Action parsing, normalization, and progression guards."""

import re
from typing import List, Tuple

from .tool_schemas import VALID_OPERATIONS

try:
    from ..models import MetaHackathonObservation
except ImportError:  # pragma: no cover - direct script execution
    from models import MetaHackathonObservation


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def parse_model_action(raw_text: str) -> Tuple[str, str, str]:
    """Parse first valid operation|target|value line from model output."""
    content = (raw_text or "").strip()
    if not content:
        return "", "", ""

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    candidates = [line for line in lines if "|" in line]
    if not candidates:
        candidates = [content]

    for line in candidates:
        parts = [segment.strip() for segment in line.split("|")]
        while len(parts) < 3:
            parts.append("")
        op = parts[0].lower()
        if op in VALID_OPERATIONS:
            return parts[0], parts[1], parts[2]

    parts = [segment.strip() for segment in candidates[0].split("|")]
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def normalize_model_action(
    *,
    operation: str,
    target: str,
    value: str,
    step: int,
) -> Tuple[str, str, str]:
    """Normalize common model formatting mistakes into valid high-signal actions."""
    op = (operation or "").strip().lower()
    tgt = (target or "").strip()
    val = (value or "").strip()

    if op and op not in VALID_OPERATIONS:
        return "", "", ""

    if op == "set_hypothesis":
        if not val and tgt:
            val = tgt
            tgt = ""
        else:
            tgt = ""

    if op in {"modify_config", "add_dependency"}:
        lowered = f"{tgt} {val}".lower()
        if any(token in lowered for token in ["requests", "urllib3", "requirements", "dependency", "pin"]):
            op = "add_dependency"

    if op == "modify_config":
        lowered = val.lower()
        if re.search(r"\b(rebase|sync|merge conflict|resolve conflict|branch)\b", lowered):
            tgt = "build" if step <= 5 else "test"

    if op == "finalize" and step < 4:
        op = "rerun_pipeline"
        tgt = ""
        val = ""

    return op, tgt, val


def ready_to_finalize(observation: MetaHackathonObservation) -> bool:
    metadata = observation.metadata if isinstance(observation.metadata, dict) else {}
    if metadata and "ready_to_finalize" in metadata:
        return bool(metadata.get("ready_to_finalize"))
    return bool(observation.incident_resolved)


def pre_finalize_guard_action(observation: MetaHackathonObservation) -> Tuple[str, str, str]:
    metadata = observation.metadata if isinstance(observation.metadata, dict) else {}
    if metadata and bool(metadata.get("verification_required")):
        return "verify_fix", "", ""
    if observation.incident_resolved:
        return "verify_fix", "", ""
    return "rerun_pipeline", "", ""


def _last_operation(history: List[str]) -> str:
    """Extract the previous operation from action history lines."""
    if not history:
        return ""
    prior = history[-1].split("->", 1)[0].strip()
    return prior.split("|", 1)[0].strip().lower()


def progression_guard_action(
    observation: MetaHackathonObservation,
    history: List[str],
    operation: str,
) -> Tuple[str, str, str] | None:
    """Force progression actions when the environment exposes strict next-step requirements."""
    metadata = observation.metadata if isinstance(observation.metadata, dict) else {}
    verification_required = bool(metadata.get("verification_required")) if metadata else False
    verified_since_last_rerun = bool(metadata.get("verified_since_last_rerun")) if metadata else False

    if verification_required and operation != "verify_fix":
        return "verify_fix", "", ""

    if ready_to_finalize(observation) and verified_since_last_rerun and operation != "finalize":
        return "finalize", "", ""

    previous_operation = _last_operation(history)
    if (
        previous_operation in {"modify_config", "add_dependency"}
        and operation not in {"rerun_pipeline", "verify_fix", "finalize"}
    ):
        return "rerun_pipeline", "", ""

    return None


