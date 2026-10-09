# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""Meta Hackathon Environment Client."""

from typing import Dict

from openenv.core import EnvClient
from openenv.core.client_types import StepResult
from openenv.core.env_server.types import State

from .models import MetaHackathonAction, MetaHackathonObservation


class MetaHackathonEnv(
    EnvClient[MetaHackathonAction, MetaHackathonObservation, State]
):
    """
    Client for the Meta Hackathon Environment.

    This client maintains a persistent WebSocket connection to the environment server,
    enabling efficient multi-step interactions with lower latency.
    Each client instance has its own dedicated environment session on the server.

    Example:
        >>> # Connect to a running server
        >>> with MetaHackathonEnv(base_url="http://localhost:8000") as client:
        ...     result = client.reset()
        ...     print(result.observation.task_id)
        ...
        ...     result = client.step(
        ...         MetaHackathonAction(operation="view_logs", target="build", value="")
        ...     )
        ...     print(result.observation.pipeline_status)

    Example with Docker:
        >>> # Automatically start container and connect
        >>> client = MetaHackathonEnv.from_docker_image("meta_hackathon-env:latest")
        >>> try:
        ...     result = client.reset()
        ...     result = client.step(
        ...         MetaHackathonAction(operation="inspect_config", target="build", value="")
        ...     )
        ... finally:
        ...     client.close()
    """

    def _step_payload(self, action: MetaHackathonAction) -> Dict:
        """
        Convert MetaHackathonAction to JSON payload for step message.

        Args:
            action: MetaHackathonAction instance

        Returns:
            Dictionary representation suitable for JSON encoding
        """
        return {
            "operation": action.operation,
            "target": action.target,
            "value": action.value,
        }

    def _parse_result(self, payload: Dict) -> StepResult[MetaHackathonObservation]:
        """
        Parse server response into StepResult[MetaHackathonObservation].

        Args:
            payload: JSON response data from server

        Returns:
            StepResult with MetaHackathonObservation
        """
        obs_data = payload.get("observation", {})
        observation = MetaHackathonObservation(
            task_id=obs_data.get("task_id", ""),
            task_title=obs_data.get("task_title", ""),
            difficulty=obs_data.get("difficulty", ""),
            pipeline_status=obs_data.get("pipeline_status", "unknown"),
            current_stage=obs_data.get("current_stage", ""),
            pipeline_stages=obs_data.get("pipeline_stages", {}),
            available_stages=obs_data.get("available_stages", []),
            available_tools=obs_data.get("available_tools", []),
            visible_alerts=obs_data.get("visible_alerts", []),
            visible_logs=obs_data.get("visible_logs", []),
            log_tokens_remaining=obs_data.get("log_tokens_remaining", 0),
            log_access_mode=obs_data.get("log_access_mode", "full"),
            logs_by_stage=obs_data.get("logs_by_stage", {}),
            visible_metrics=obs_data.get("visible_metrics", []),
            config_files=obs_data.get("config_files", {}),
            surfaced_errors=obs_data.get("surfaced_errors", []),
            findings=obs_data.get("findings", []),
            action_history=obs_data.get("action_history", []),
            previous_actions=obs_data.get("previous_actions", []),
            current_hypothesis=obs_data.get("current_hypothesis", ""),
            attempted_fix=obs_data.get("attempted_fix", ""),
            hypothesis_history=obs_data.get("hypothesis_history", []),
            active_issue_index=obs_data.get("active_issue_index", 0),
            revealed_issue_count=obs_data.get("revealed_issue_count", 1),
            pipeline_health=obs_data.get("pipeline_health", 1.0),
            recovery_cost=obs_data.get("recovery_cost", 0),
            redundant_actions=obs_data.get("redundant_actions", 0),
            destructive_actions=obs_data.get("destructive_actions", 0),
            incident_resolved=obs_data.get("incident_resolved", False),
            final_score=obs_data.get("final_score", 0.0),
            deterministic_score=obs_data.get("deterministic_score", 0.0),
            rubric_score=obs_data.get("rubric_score", 0.0),
            delayed_reward=obs_data.get("delayed_reward", 0.0),
            rubric_blend_weight=obs_data.get("rubric_blend_weight", 0.0),
            rubric_judge_used=obs_data.get("rubric_judge_used", False),
            rubric_judge_error=obs_data.get("rubric_judge_error", ""),
            done=payload.get("done", False),
            reward=payload.get("reward"),
            metadata=obs_data.get("metadata", {}),
        )

        return StepResult(
            observation=observation,
            reward=payload.get("reward"),
            done=payload.get("done", False),
        )

    def _parse_state(self, payload: Dict) -> State:
        """
        Parse server response into State object.

        Args:
            payload: JSON response from state request

        Returns:
            State object with episode_id and step_count
        """
        return State(
            episode_id=payload.get("episode_id"),
            step_count=payload.get("step_count", 0),
        )
