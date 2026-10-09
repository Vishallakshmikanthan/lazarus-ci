"""Structured logging helpers for inference runs."""

from typing import Any, Dict, List, Optional

from .config import INFERENCE_DETAIL_MAX_ITEMS

try:
    from ..models import MetaHackathonObservation
except ImportError:  # pragma: no cover - direct script execution
    from models import MetaHackathonObservation


# Colors for better readability
RESET = "\033[0m"
BOLD = "\033[1m"
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
MAGENTA = "\033[95m"
BLUE = "\033[94m"


def log_start(task: str, env: str, model: str) -> None:
    print(f"\n{BOLD}{CYAN}=== STARTING TASK: {task} ==={RESET}")
    print(f"{CYAN}Environment: {env} | Model: {model}{RESET}\n")


def log_step(step: int, action: str, reward: float, done: bool, error: Optional[str], llm_thought: str = "") -> None:
    error_str = f" | {RED}Error: {error}{RESET}" if error else ""
    reward_color = GREEN if reward > 0 else (RED if reward < 0 else YELLOW)
    
    print(f"\n{BOLD}{BLUE}Step {step}:{RESET}")
    if llm_thought:
        # Wrap long thoughts so they look nice
        import textwrap
        wrapped_thought = textwrap.indent(textwrap.fill(llm_thought, width=100), "    ")
        print(f"  {CYAN}Thought:{RESET}\n{wrapped_thought}")
        
    print(
        f"  {MAGENTA}Action:{RESET} {action} -> "
        f"{reward_color}Reward: {reward:+.2f}{RESET}{error_str}",
        flush=True,
    )


def log_end(
    success: bool,
    steps: int,
    score: float,
    resolved: bool,
    rewards: List[float],
    *,
    deterministic_score: float = 0.0,
    rubric_score: float = 0.0,
    rubric_judge_used: bool = False,
) -> None:
    color = GREEN if success else RED
    print(f"\n{BOLD}{color}=== TASK COMPLETED ==={RESET}")
    print(f"{color}Success: {success} | Resolved: {resolved} | Score: {score:.3f} | Steps: {steps}{RESET}")
    print(
        f"{color}Rubric: det={deterministic_score:.3f} rubric={rubric_score:.3f} "
        f"judge_used={str(rubric_judge_used).lower()}{RESET}"
    )
    print(f"{color}Rewards: {','.join(f'{r:.2f}' for r in rewards)}{RESET}\n")


def log_memory(message: str) -> None:
    if not message:
        return
    print(f"  {CYAN}{message}{RESET}", flush=True)


def _compact_list(values: List[Any], limit: int = INFERENCE_DETAIL_MAX_ITEMS) -> str:
    if not values:
        return "none"
    lines = []
    for item in values[-limit:]:
        text = str(item).replace("\n", " ").strip()
        if len(text) > 100:
            text = text[:97] + "..."
        lines.append(text)
    return " \n    - ".join(["", *lines])


def log_detail(
    *,
    step: int,
    action: str,
    observation: MetaHackathonObservation,
    reward: float,
    done: bool,
    error: Optional[str],
) -> None:
    """Emit verbose trajectory diagnostics for local debugging without changing strict logs."""
    # We only log details if there are meaningful findings or pipeline changes
    if not observation.findings and not observation.surfaced_errors:
        return

    findings = _compact_list(observation.findings)
    errors = _compact_list(observation.surfaced_errors)
    
    print(f"  {YELLOW}Findings:{RESET}{findings}")
    if errors and errors != "none":
        print(f"  {RED}Errors:{RESET}{errors}")

    if done:
        print(f"  {CYAN}Pipeline Status: {observation.pipeline_status}{RESET}")


