# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""Phase-aware adversarial judge for multi-fault CI/CD incident scoring."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

try:
    from models import AdversarialCICDScenario
except ImportError:
    from ..models import AdversarialCICDScenario

# Map each operation → the SRE phase it belongs to
OP_TO_PHASE: Dict[str, str] = {
    "view_logs": "triage",
    "tail_logs": "triage",
    "inspect_config": "investigation",
    "inspect_dockerfile": "investigation",
    "inspect_permissions": "investigation",
    "set_hypothesis": "hypothesis",
    "modify_config": "fix",
    "add_dependency": "fix",
    "rerun_pipeline": "verification",
    "verify_fix": "verification",
    "finalize": "verification",
}

# Correct ordering — skipping forward is penalized
PHASE_ORDER: List[str] = ["triage", "investigation", "hypothesis", "fix", "verification"]

# Per-action bonuses when following correct SRE workflow
PHASE_BONUS = 0.15
FIX_SKIP_PENALTY = 0.0        # jumping to fix without hypothesis
RED_HERRING_PENALTY = 0.0     # hypothesis matches red-herring symptom
CORRECT_HYPOTHESIS_BONUS = 0.20
FULL_RESOLUTION_BONUS = 1.0   # all faults resolved at finalize
PARTIAL_RESOLUTION_BONUS = 0.15


class AdversarialJudge:
    """
    Scores agent steps based on SRE phase progression for multi-fault scenarios.

    Layered on top of the existing deterministic per-operation rewards:
      - Bonus for following correct phase order
      - Penalty for skipping directly to fix
      - Bonus/penalty for hypothesis quality vs. red herrings
      - Big terminal bonus if ALL cascading faults are resolved
    """

    def score_step(
        self,
        operation: str,
        value: str,
        scenario: AdversarialCICDScenario,
        history: List[Dict[str, str]],
    ) -> Tuple[float, str]:
        """
        Returns (bonus_delta, note) to add on top of the base deterministic reward.
        bonus_delta is in roughly [-0.35, +0.25].
        """
        phase = OP_TO_PHASE.get(operation, "triage")
        bonus = 0.0
        notes: List[str] = []

        # Phase order bonus
        if self._phase_order_correct(phase, history):
            bonus += PHASE_BONUS
        elif phase == "fix" and not self._saw_phase("hypothesis", history):
            bonus += FIX_SKIP_PENALTY
            notes.append("fix attempted before hypothesis — skipped investigation")

        # Hypothesis quality scoring
        if operation == "set_hypothesis":
            root_terms = [t.lower() for t in scenario.expected_hypothesis_terms]
            v = value.lower()
            hits = sum(1 for t in root_terms if t in v)
            min_hits = max(2, len(root_terms) // 2)
            if hits >= min_hits:
                bonus += CORRECT_HYPOTHESIS_BONUS
                notes.append("hypothesis matches root cause")

            # Penalize biting red herrings
            rh_hit = any(
                any(token in v for token in rh.lower().split() if len(token) > 4)
                for rh in scenario.red_herrings
            )
            if rh_hit and hits < min_hits:
                bonus += RED_HERRING_PENALTY
                notes.append("hypothesis matches red herring")

        # Bonus for triaging the right stage (where root cause fault fails)
        if operation == "view_logs" and value:
            root_cause_step = next(
                (s for s in scenario.steps if s.is_root_cause), None
            )
            if root_cause_step:
                from cicd.fault_types import FAULT_STAGE_MAP
                expected_stage = FAULT_STAGE_MAP.get(root_cause_step.fault_type, "")
                if expected_stage and expected_stage in value.lower():
                    bonus += 0.08
                    notes.append(f"triaged correct root-cause stage ({expected_stage})")

        return round(bonus, 3), "; ".join(notes)

    def score_terminal(
        self,
        incident_resolved: bool,
        verified: bool,
        pipeline_passed: bool,
        cascading_fault_count: int,
    ) -> Tuple[float, str]:
        """
        Terminal bonus at finalize for multi-fault resolution quality.
        Replaces the standard finalize reward in adversarial mode.
        """
        if pipeline_passed and verified and incident_resolved:
            # All cascading faults fixed — full bonus scales with complexity
            complexity_factor = min(1.0, 0.7 + cascading_fault_count * 0.15)
            bonus = round(FULL_RESOLUTION_BONUS * complexity_factor, 3)
            return bonus, f"all {cascading_fault_count + 1} faults resolved"

        if incident_resolved and not verified:
            return 0.0, "resolved but not verified — run verify_fix first"

        if pipeline_passed:
            return PARTIAL_RESOLUTION_BONUS, "pipeline passed but not fully verified"

        return 0.0, "multi-fault incident not resolved"

    # ── internals ──────────────────────────────────────────────────────────

    def _phase_order_correct(self, current_phase: str, history: List[Dict[str, str]]) -> bool:
        """True if current_phase doesn't skip over any earlier required phase."""
        seen = {OP_TO_PHASE.get(h["operation"], "triage") for h in history}
        current_idx = PHASE_ORDER.index(current_phase) if current_phase in PHASE_ORDER else 0
        for phase in PHASE_ORDER[:current_idx]:
            # Only flag if we haven't done any prior phase AND it's required
            if phase in ("triage", "investigation", "hypothesis") and phase not in seen:
                return False
        return True

    def _saw_phase(self, phase: str, history: List[Dict[str, str]]) -> bool:
        return any(OP_TO_PHASE.get(h["operation"]) == phase for h in history)
