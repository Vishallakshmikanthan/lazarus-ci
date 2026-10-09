"""Procedural multi-fault scenario generator (no LLM required).

Deterministically composes 2-3 compatible faults per episode from the fault
primitives defined in `fault_injector.py`. Unlike `adversarial_designer.py`
this requires no external API — it uses a seeded RNG so runs are reproducible.

Used when `task_key == "procedural"` (or `"combo"`) in reset(). Each episode
draws a root cause + 1-2 cascading faults + optional red herring fault,
respecting compatibility rules (e.g., avoid two faults that mutate the same file).
"""

from __future__ import annotations

import random
from typing import List, Optional

from cicd.fault_types import (
    FAULT_KEYWORDS,
    FAULT_STAGE_MAP,
    FAULT_TYPES,
    FaultMetadata,
)
from cicd.simulated_fault_injector import inject_fault_simulated as inject_fault

try:
    from models import AdversarialCICDScenario, IncidentStep
except ImportError:  # pragma: no cover
    from ..models import AdversarialCICDScenario, IncidentStep


# Which faults touch which files. Two faults that edit the same file will
# clobber each other, so we refuse those combinations.
_FAULT_FILES = {
    # Core faults
    "merge_conflict":      {"services/api/routes.py"},
    "dependency_conflict": {"services/api/requirements.txt"},
    "docker_order":        {"Dockerfile"},
    "flaky_test":          {"tests/test_api.py"},
    "missing_permission":  {"docker-compose.yml"},
    "secret_exposure":     {"services/api/app.py"},
    "env_drift":           {"docker-compose.yml"},
    # Logging faults
    "log_pii_leak":        {"services/api/routes.py"},
    "log_disabled":        {"services/api/logging_config.py"},
    # DB faults
    "bad_migration_sql":   {"db/migrations/001_init.sql"},
    "schema_drift":        {"db/database.py"},
}


def _pick_compatible_extras(root: str, rng: random.Random, count: int) -> List[str]:
    """Pick `count` extra faults that don't touch the same file as any already-picked fault."""
    root_files = _FAULT_FILES.get(root, set())
    used_files = set(root_files)
    picks: List[str] = []
    candidates = [f for f in FAULT_TYPES if f != root]
    rng.shuffle(candidates)
    for cand in candidates:
        if len(picks) >= count:
            break
        cand_files = _FAULT_FILES.get(cand, set())
        if cand_files & used_files:
            continue
        picks.append(cand)
        used_files |= cand_files
    return picks


def generate_scenario(
    difficulty: float = 0.5,
    seed: Optional[int] = None,
    root_cause: Optional[str] = None,
) -> AdversarialCICDScenario:
    """Generate a procedural multi-fault scenario, purely in-process.

    If root_cause is provided, it's used as the fixed root cause.
    Otherwise, a random root is selected (not used in curriculum-driven mode).
    """
    rng = random.Random(seed)

    # Difficulty controls the fault count: 1 at low, 2 mid, 3 high
    if difficulty < 0.35:
        extra_count = 0
    elif difficulty < 0.7:
        extra_count = 1
    else:
        extra_count = 2

    root = root_cause or rng.choice(FAULT_TYPES)
    extras = _pick_compatible_extras(root, rng, extra_count)

    steps: List[IncidentStep] = [
        IncidentStep(
            fault_type=root,
            effect=f"{FAULT_STAGE_MAP[root]} stage fails via {root.replace('_', ' ')}",
            order=1,
            is_root_cause=True,
            depends_on=[],
        )
    ]
    for idx, extra in enumerate(extras, start=2):
        steps.append(
            IncidentStep(
                fault_type=extra,
                effect=f"cascading {extra.replace('_', ' ')} surfaces during repair",
                order=idx,
                is_root_cause=False,
                depends_on=[1],
            )
        )

    keywords = FAULT_KEYWORDS.get(root, [root])
    red_herrings: List[str] = []
    if difficulty >= 0.6 and extras:
        # First extra fault becomes a red-herring symptom: its keywords may
        # mislead the agent into chasing the wrong root cause.
        red_herrings = FAULT_KEYWORDS.get(extras[0], [])[:2]

    return AdversarialCICDScenario(
        title=f"Procedural incident: {root.replace('_', ' ')} (+{len(extras)} cascade)",
        narrative=(
            f"Procedurally generated CI/CD incident. Root cause is {root.replace('_', ' ')}; "
            f"{len(extras)} additional fault(s) cascade after remediation begins."
        ),
        alert_message=f"ALERT: pipeline failing at {FAULT_STAGE_MAP[root]} stage",
        steps=steps,
        expected_triage=[f"view_logs:{FAULT_STAGE_MAP[root]}"],
        expected_investigation=["inspect_config", "inspect_dockerfile"],
        expected_hypothesis_terms=keywords,
        expected_fix_sequence=[root.replace("_", "-")],
        expected_verification=["rerun_pipeline", "verify_fix"],
        red_herrings=red_herrings,
        root_cause_explanation=(
            f"True root cause is {root.replace('_', ' ')}; other surfaced failures "
            f"are downstream effects of the same incident."
        ),
        difficulty=difficulty,
        # DB selection: easy tiers prefer sqlite, harder tiers prefer postgres
    )


def inject_procedural(
    workspace: str,
    scenario: AdversarialCICDScenario,
) -> List[FaultMetadata]:
    """Inject every fault in the scenario, ordered by step.order.

    Failures are logged and the failed step is removed from the scenario so
    the episode contract stays consistent — the agent is never shown a fault
    that wasn't actually injected.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    injected: List[FaultMetadata] = []
    surviving_steps = []
    for step in sorted(scenario.steps, key=lambda s: s.order):
        try:
            meta = inject_fault(workspace, step.fault_type)
            injected.append(meta)
            surviving_steps.append(step)
        except Exception as exc:
            _log.warning(
                "inject_procedural: skipping fault '%s' (step %d) — injection failed: %s",
                step.fault_type, step.order, exc,
            )

    # Prune scenario steps to match what was actually injected so callers
    # don't present a 3-fault narrative when only 2 faults exist.
    scenario.steps = surviving_steps
    return injected
