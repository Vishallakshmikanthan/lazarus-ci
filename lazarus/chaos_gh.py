"""Lazarus-CI GitHub Chaos Simulator & Fault Injector.

Injects synthetic pipeline faults into the target GitHub demo repository
to trigger real GitHub Actions failures, and restores pristine clean states.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from . import gh

logger = logging.getLogger(__name__)

PRISTINE_DIR = Path(__file__).resolve().parents[1] / "demo_repo_src"

FAULTS: Dict[str, List[Tuple[str, str, str]]] = {
    "deps": [("requirements.txt", "requests==2.32.3", "requests==99.0.0")],
    "regression": [("app/pricing.py", "(1 - pct / 100)", "(1 - pct / 10)")],
    "dockerfile": [("Dockerfile", "COPY requirements.txt .", "COPY requirments.txt .")],
    "pyversion": [(".github/workflows/ci.yml", 'python-version: "3.12"', 'python-version: "3.6"')],
    "secret": [
        (
            ".github/workflows/ci.yml",
            "APP_ENV: ci",
            "APP_ENV: ci\n      PAYMENT_API_KEY: ${{ secrets.PAYMENT_API_KEY }}",
        )
    ],
}

NEW_FILES: Dict[str, Dict[str, str]] = {
    "secret": {
        "tests/test_config.py": (
            "import os\n\n\n"
            "def test_payment_key_present():\n"
            '    assert os.environ.get("PAYMENT_API_KEY"), "PAYMENT_API_KEY secret is required"\n'
        )
    }
}


def commit_file(path: str, content: str, msg: str, repo: Optional[str] = None) -> None:
    """Safely commit or update a file on the main branch."""
    try:
        _, sha = gh.get_file(path, "main", repo=repo)
    except Exception:
        sha = None
    gh.put_file(path, content, msg, "main", sha=sha, repo=repo)


def break_repo(name: str, repo: Optional[str] = None) -> Dict[str, Any]:
    """Inject a specific fault into the target GitHub repository."""
    if name not in FAULTS:
        raise ValueError(f"Unknown fault '{name}'. Available faults: {list(FAULTS.keys())}")

    modified_files = []
    for path, old, new in FAULTS[name]:
        text, sha = gh.get_file(path, "main", repo=repo)
        if old not in text:
            # File might already be broken or in an unexpected state
            logger.warning("Target pattern '%s' not found in %s, committing forced replacement", old, path)
            text = text.replace("requests==99.0.0", "requests==2.32.3")
            text = text.replace("(1 - pct / 10)", "(1 - pct / 100)")
            text = text.replace("COPY requirments.txt .", "COPY requirements.txt .")
            text = text.replace('python-version: "3.6"', 'python-version: "3.12"')

        updated = text.replace(old, new)
        gh.put_file(
            path,
            updated,
            f"chore: update {Path(path).name} (simulated fault: {name})",
            "main",
            sha=sha,
            repo=repo,
        )
        modified_files.append(path)

    for path, content in NEW_FILES.get(name, {}).items():
        commit_file(path, content, f"test: add {Path(path).name} (simulated fault: {name})", repo=repo)
        modified_files.append(path)

    return {
        "status": "injected",
        "fault": name,
        "files": modified_files,
        "message": f"Fault '{name}' committed to main. GitHub Actions will fail shortly.",
    }


def reset(repo: Optional[str] = None) -> Dict[str, Any]:
    """Reset repository to pristine green state and close stale Lazarus branches."""
    if not PRISTINE_DIR.exists():
        raise FileNotFoundError(f"Pristine demo files not found at {PRISTINE_DIR}")

    restored = []
    for p in PRISTINE_DIR.rglob("*"):
        if p.is_file():
            rel = str(p.relative_to(PRISTINE_DIR)).replace("\\", "/")
            commit_file(rel, p.read_text(encoding="utf-8"), "chore: reset demo repo to pristine state", repo=repo)
            restored.append(rel)

    # Delete fault-only test files if present
    try:
        _, sha = gh.get_file("tests/test_config.py", "main", repo=repo)
        if sha:
            gh.delete_file("tests/test_config.py", "chore: remove secret test file", sha=sha, branch="main", repo=repo)
            restored.append("deleted: tests/test_config.py")
    except Exception:
        pass

    # Close stale Lazarus PRs and delete branches
    closed_prs = []
    try:
        prs = gh.api("GET", "/pulls", repo=repo, params={"state": "open"})
        for pr in prs:
            ref = pr.get("head", {}).get("ref", "")
            if ref.startswith("lazarus/"):
                gh.api("PATCH", f"/pulls/{pr['number']}", repo=repo, json={"state": "closed"})
                closed_prs.append(pr["number"])
                try:
                    gh.api("DELETE", f"/git/refs/heads/{ref}", repo=repo)
                except Exception:
                    pass
    except Exception as e:
        logger.warning("Could not close stale PRs: %s", e)

    return {
        "status": "reset",
        "restored_files": restored,
        "closed_prs": closed_prs,
        "message": "Repository reset to pristine state. Main pipeline will run green.",
    }


def init_repo(repo: Optional[str] = None) -> Dict[str, Any]:
    """Initialize a brand new or empty GitHub repository with pristine demo files."""
    return reset(repo=repo)
