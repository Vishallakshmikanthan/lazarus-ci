"""Deterministic benchmark evaluator for the Ctrl + AI Agentic AI Hackathon environment."""

from __future__ import annotations

import os
from collections import Counter
from typing import Dict, List, Tuple

try:
    from .server.meta_hackathon_environment import MetaHackathonEnvironment
    from .models import MetaHackathonAction
except ImportError:
    from server.meta_hackathon_environment import MetaHackathonEnvironment
    from models import MetaHackathonAction

SUCCESS_SCORE_THRESHOLD = float(os.getenv("SUCCESS_SCORE_THRESHOLD", "0.20"))
EPISODES_PER_TASK = int(os.getenv("EVAL_EPISODES_PER_TASK", "3"))
TASK_ORDER = ["easy", "flaky", "medium", "network", "security", "hard"]


def scripted_action(task_name: str, step: int) -> Tuple[str, str, str]:
    plans: Dict[str, List[Tuple[str, str, str]]] = {
        "easy": [
            ("view_logs", "build", ""),
            ("inspect_config", "build", ""),
            ("set_hypothesis", "", "merge conflict markers are blocking build validation"),
            ("modify_config", "build", "sync branch and resolve merge conflict"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
        "flaky": [
            ("view_logs", "test", ""),
            ("inspect_config", "test", ""),
            ("set_hypothesis", "", "flaky timing-sensitive test is intermittently failing in CI"),
            ("modify_config", "test", "add retry policy for flaky test isolation"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
        "medium": [
            ("view_logs", "build", ""),
            ("inspect_config", "build", ""),
            ("inspect_dockerfile", "build", ""),
            ("set_hypothesis", "", "requests and urllib3 are incompatible"),
            ("add_dependency", "build", "pin compatible requests urllib3 versions"),
            ("rerun_pipeline", "", ""),
            ("set_hypothesis", "", "docker install order mismatch still causing flaky build"),
            ("modify_config", "build", "reorder docker install steps"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
        "network": [
            ("view_logs", "deploy", ""),
            ("inspect_config", "deploy", ""),
            ("inspect_permissions", "deploy", ""),
            ("set_hypothesis", "", "transient network dns outage is blocking artifact upload"),
            ("modify_config", "deploy", "configure retry backoff for artifact upload with dns fallback"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
        "security": [
            ("view_logs", "deploy", ""),
            ("inspect_permissions", "deploy", ""),
            ("set_hypothesis", "", "artifact registry push fails because deployer lacks writer permissions"),
            ("modify_config", "deploy", "grant artifactregistry writer to ci-deployer"),
            ("rerun_pipeline", "", ""),
            ("view_logs", "deploy", ""),
            ("inspect_dockerfile", "build", ""),
            ("set_hypothesis", "", "Dockerfile exposes API_KEY and must use secret manager reference"),
            ("modify_config", "deploy", "replace Dockerfile API_KEY with secret manager reference"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
        "hard": [
            ("inspect_permissions", "build", ""),
            ("set_hypothesis", "", "service-a publish is failing because artifactregistry writer permission is missing"),
            ("modify_config", "build", "grant artifactregistry writer to service-a publisher"),
            ("rerun_pipeline", "", ""),
            ("inspect_config", "deploy", ""),
            ("set_hypothesis", "", "service-b should rollback to the last stable image revision"),
            ("modify_config", "deploy", "rollback service-b to stable image revision"),
            ("rerun_pipeline", "", ""),
            ("view_logs", "deploy", ""),
            ("set_hypothesis", "", "service-b rollout timeout should be increased to 20m after rollback"),
            ("modify_config", "deploy", "increase rollout timeout to 20m"),
            ("rerun_pipeline", "", ""),
            ("verify_fix", "", ""),
            ("finalize", "", ""),
        ],
    }
    sequence = plans.get(task_name, plans["medium"])
    if step <= len(sequence):
        return sequence[step - 1]
    return sequence[-1]


def run_episode(task_name: str, env: MetaHackathonEnvironment) -> dict:
    observation = env.reset()
    total_reward = 0.0
    steps = 0

    max_steps = int((observation.metadata or {}).get("max_steps", 12))
    variant_id = str((observation.metadata or {}).get("variant_id", "unknown"))

    for step in range(1, max_steps + 1):
        if observation.done:
            break

        operation, target, value = scripted_action(task_name, step)
        result = env.step(MetaHackathonAction(operation=operation, target=target, value=value))
        observation = result
        steps = step
        total_reward += float(result.reward or 0.0)

        if observation.done:
            break

    score = float(observation.final_score)
    deterministic_score = float(observation.deterministic_score)
    rubric_score = float(observation.rubric_score)
    delayed_reward = float(observation.delayed_reward)
    rubric_judge_used = bool(observation.rubric_judge_used)
    rubric_judge_error = str(observation.rubric_judge_error or "")
    resolved = bool(observation.incident_resolved)
    success = resolved and score >= SUCCESS_SCORE_THRESHOLD

    failure_reason = ""
    if not resolved:
        findings = list(observation.findings)
        failure_reason = findings[-1] if findings else "unresolved"

    return {
        "task": task_name,
        "variant_id": variant_id,
        "steps": steps,
        "reward_sum": round(total_reward, 3),
        "score": score,
        "deterministic_score": deterministic_score,
        "rubric_score": rubric_score,
        "delayed_reward": delayed_reward,
        "rubric_judge_used": rubric_judge_used,
        "rubric_judge_error": rubric_judge_error,
        "resolved": resolved,
        "success": success,
        "failure_reason": failure_reason,
    }


def main() -> None:
    all_results: List[dict] = []

    print(f"[EVAL] episodes_per_task={EPISODES_PER_TASK} success_threshold={SUCCESS_SCORE_THRESHOLD:.2f}")

    for task_name in TASK_ORDER:
        env = MetaHackathonEnvironment(task_key=task_name)
        for episode in range(EPISODES_PER_TASK):
            result = run_episode(task_name, env)
            all_results.append(result)
            print(
                "[EP] "
                f"task={task_name} episode={episode + 1} variant={result['variant_id']} "
                f"steps={result['steps']} resolved={str(result['resolved']).lower()} "
                f"score={result['score']:.3f} det={result['deterministic_score']:.3f} "
                f"rubric={result['rubric_score']:.3f} delayed={result['delayed_reward']:.3f} "
                f"judge={str(result['rubric_judge_used']).lower()} success={str(result['success']).lower()}"
            )

    print("\n[SUMMARY]")
    for task_name in TASK_ORDER:
        task_results = [item for item in all_results if item["task"] == task_name]
        count = len(task_results)
        resolved_rate = sum(1 for item in task_results if item["resolved"]) / max(count, 1)
        success_rate = sum(1 for item in task_results if item["success"]) / max(count, 1)
        avg_score = sum(item["score"] for item in task_results) / max(count, 1)
        avg_det_score = sum(item["deterministic_score"] for item in task_results) / max(count, 1)
        avg_rubric_score = sum(item["rubric_score"] for item in task_results) / max(count, 1)
        avg_delayed_reward = sum(item["delayed_reward"] for item in task_results) / max(count, 1)
        avg_steps = sum(item["steps"] for item in task_results) / max(count, 1)
        fallback_count = sum(
            1
            for item in task_results
            if item["rubric_judge_error"] and item["rubric_judge_error"] != "rubric judging disabled"
        )
        print(
            f"- task={task_name} resolved_rate={resolved_rate:.2%} "
            f"success_rate={success_rate:.2%} avg_score={avg_score:.3f} det={avg_det_score:.3f} "
            f"rubric={avg_rubric_score:.3f} delayed={avg_delayed_reward:.3f} "
            f"avg_steps={avg_steps:.2f} rubric_fallbacks={fallback_count}"
        )

    unresolved = [item for item in all_results if not item["resolved"]]
    if unresolved:
        reasons = Counter(item["failure_reason"] for item in unresolved)
        print("\n[TOP_FAILURE_REASONS]")
        for reason, count in reasons.most_common(3):
            print(f"- count={count} reason={reason}")


if __name__ == "__main__":
    main()
