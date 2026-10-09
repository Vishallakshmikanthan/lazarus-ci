# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""Data models for the Meta Hackathon CI/CD repair environment."""

from dataclasses import dataclass, field
from typing import Dict, List

from openenv.core.env_server.types import Action, Observation
from pydantic import Field


@dataclass
class IncidentStep:
    """One fault in a multi-fault cascading CI/CD incident."""
    fault_type: str
    effect: str
    order: int
    is_root_cause: bool
    depends_on: List[int] = field(default_factory=list)


@dataclass
class AdversarialCICDScenario:
    """Multi-fault incident designed by the adversarial Groq judge."""
    title: str
    narrative: str
    steps: List[IncidentStep]
    expected_triage: List[str]
    expected_investigation: List[str]
    expected_hypothesis_terms: List[str]
    expected_fix_sequence: List[str]
    expected_verification: List[str]
    red_herrings: List[str]
    root_cause_explanation: str
    difficulty: float = 0.5
    alert_message: str = ""

    def __post_init__(self) -> None:
        # Deserialize nested IncidentStep dicts when loaded from JSON
        self.steps = [
            IncidentStep(**s) if isinstance(s, dict) else s
            for s in self.steps
        ]


class MetaHackathonAction(Action):
    """Action model for CI/CD pipeline investigation and repair."""

    operation: str = Field(
        ...,
        description=(
            "Operation name. Preferred values: view_logs, tail_logs, inspect_config, "
            "inspect_dockerfile, modify_config, add_dependency, rerun_pipeline, "
            "verify_fix, finalize, inspect_permissions, set_hypothesis. "
            "Legacy aliases are accepted for backward compatibility."
        ),
    )
    target: str = Field(
        default="",
        description="Optional target (stage/service/component) for inspect operations.",
    )
    value: str = Field(
        default="",
        description="Optional free-text payload for hypothesis/fix values.",
    )


class MetaHackathonObservation(Observation):
    """Observation model exposing CI/CD task state and visible evidence."""

    task_id: str = Field(default="", description="Task identifier.")
    task_title: str = Field(default="", description="Human-readable task title.")
    difficulty: str = Field(default="", description="Task difficulty: easy/medium/security/hard.")
    pipeline_status: str = Field(default="unknown", description="Current pipeline status.")
    current_stage: str = Field(default="", description="Current failing stage.")
    pipeline_stages: Dict[str, str] = Field(
        default_factory=dict,
        description="Per-stage status map (pending/running/failed/passed/blocked).",
    )
    available_stages: List[str] = Field(
        default_factory=list,
        description="Pipeline stages available for targeted inspection.",
    )
    available_tools: List[str] = Field(
        default_factory=list,
        description="High-level tools/actions an agent can invoke.",
    )
    visible_alerts: List[str] = Field(
        default_factory=list,
        description="Visible alert summaries.",
    )
    visible_logs: List[str] = Field(
        default_factory=list,
        description="Visible log lines (flattened across all apps, for backward compat).",
    )
    visible_logs_per_app: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Visible log lines per app: {'frontend': [...], 'api-service': [...], ...}.",
    )
    log_tokens_remaining: int = Field(
        default=0,
        description="Remaining observation budget for full log access in the current episode.",
    )
    log_access_mode: str = Field(
        default="full",
        description="Current log access mode: full when view_logs is allowed, tail_only after budget exhaustion.",
    )
    logs_by_stage: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Visible logs grouped by pipeline stage.",
    )
    visible_metrics: List[str] = Field(
        default_factory=list,
        description="Visible metrics/clues discovered so far.",
    )
    config_files: Dict[str, str] = Field(
        default_factory=dict,
        description="Current snapshots of key configuration files.",
    )
    surfaced_errors: List[str] = Field(
        default_factory=list,
        description="Current error signatures visible to the agent.",
    )
    findings: List[str] = Field(
        default_factory=list,
        description="Structured findings accumulated by investigation.",
    )
    action_history: List[str] = Field(
        default_factory=list,
        description="Compact history of actions already taken.",
    )
    previous_actions: List[str] = Field(
        default_factory=list,
        description="Alias for action_history for clarity in prompts/agents.",
    )
    current_hypothesis: str = Field(
        default="",
        description="Current root-cause hypothesis set by the agent.",
    )
    attempted_fix: str = Field(
        default="",
        description="Most recent fix attempted by the agent.",
    )
    hypothesis_history: List[str] = Field(
        default_factory=list,
        description="Chronological hypothesis updates proposed by the agent.",
    )
    active_issue_index: int = Field(
        default=0,
        description="Current issue index in the scenario's deterministic incident chain.",
    )
    revealed_issue_count: int = Field(
        default=1,
        description="How many incident-chain issues have been revealed so far.",
    )
    pipeline_health: float = Field(
        default=1.0,
        description="Pipeline health in [0.0, 1.0]; bad fixes degrade this value.",
    )
    recovery_cost: int = Field(
        default=0,
        description="Accumulated recovery cost from retries and incorrect actions.",
    )
    redundant_actions: int = Field(
        default=0,
        description="Count of repeated low-value actions.",
    )
    destructive_actions: int = Field(
        default=0,
        description="Count of unsafe fix attempts.",
    )
    incident_resolved: bool = Field(
        default=False,
        description="Whether the CI/CD incident is resolved.",
    )
    final_score: float = Field(
        default=0.0,
        description="Episode score in [0.0, 1.0] once done=True.",
    )
    deterministic_score: float = Field(
        default=0.0,
        description="Baseline deterministic episode score before rubric blending.",
    )
    rubric_score: float = Field(
        default=0.0,
        description="Rubric judge score in [0.0, 1.0] for delayed semantic quality.",
    )
    delayed_reward: float = Field(
        default=0.0,
        description="Delayed reward delta added at terminal step from rubric blending.",
    )
    rubric_blend_weight: float = Field(
        default=0.0,
        description="Blend weight applied to rubric score when computing final_score.",
    )
    rubric_judge_used: bool = Field(
        default=False,
        description="Whether OpenEnv LLMJudge (not fallback heuristic) produced rubric_score.",
    )
    rubric_judge_error: str = Field(
        default="",
        description="Judge error/fallback reason when OpenEnv LLMJudge is unavailable or fails.",
    )
    affected_apps: List[str] = Field(
        default_factory=list,
        description="Apps directly affected by the injected fault (empty for single-app faults).",
    )
    service_dependency_graph: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Adjacency map of service dependencies: {'frontend': ['api-service'], ...}.",
    )
