"""Lazarus-CI GitHub Actions Environment Adapter.

Provides the OpenEnv-compatible interface (reset, step) connected directly
to real GitHub Actions repositories and Pull Request rehearsals.
"""

from __future__ import annotations

import difflib
import logging
import os
from typing import Any, Dict, List, Optional
from . import gh

logger = logging.getLogger(__name__)

STAGE_MAP = {
    "success": "passed",
    "failure": "failed",
    "skipped": "blocked",
    "cancelled": "blocked",
    "in_progress": "running",
    "queued": "pending",
    "waiting": "pending",
    None: "pending",
}

DEFAULT_WATCH_FILES = [
    "requirements.txt",
    "Dockerfile",
    ".github/workflows/ci.yml",
    "app/pricing.py",
    "tests/test_pricing.py",
    "tests/test_config.py",
]


class GitHubEnv:
    """GitHub Actions environment adapter behaving like OpenEnv."""

    def __init__(self, repo: Optional[str] = None):
        self.repo = repo
        self.branch: Optional[str] = None
        self.pr: Optional[int] = None
        self.pr_url: Optional[str] = None
        self.run_url: Optional[str] = None
        self.failed_at: Optional[str] = None
        self.run_id: Optional[int] = None
        self.main_sha: Optional[str] = None
        self.head_sha: Optional[str] = None
        self.last_diff: str = ""
        self.job_ids: Dict[str, int] = {}
        self.obs: Dict[str, Any] = {}
        
        watch_env = os.getenv("WATCH_FILES", "")
        self.watch_files = watch_env.split(",") if watch_env else DEFAULT_WATCH_FILES

    def _read(self, path: str, ref: str) -> str:
        try:
            content, _ = gh.get_file(path, ref=ref, repo=self.repo)
            return content
        except Exception:
            return ""

    def _ref(self) -> str:
        return self.branch or "main"

    def _build_obs(self, run: Dict[str, Any]) -> Dict[str, Any]:
        run_id = run["id"]
        js = gh.jobs(run_id, repo=self.repo)
        self.job_ids = {j["name"]: j["id"] for j in js}
        failed = next((j for j in js if j.get("conclusion") == "failure"), None)
        text = gh.job_log(failed["id"], repo=self.repo) if failed else ""

        stages = {j["name"]: STAGE_MAP.get(j.get("conclusion") or j.get("status"), "pending") for j in js}
        if not stages:
            stages = {"ci": "failed" if run.get("conclusion") == "failure" else "passed"}

        conclusion = run.get("conclusion")
        pipeline_status = "passed" if conclusion == "success" else ("failed" if conclusion else "running")

        return {
            "pipeline_status": pipeline_status,
            "current_stage": failed["name"] if failed else (next(iter(stages.keys())) if stages else ""),
            "pipeline_stages": stages,
            "surfaced_errors": gh.extract_errors(text),
            "visible_alerts": [f"{run.get('name', 'CI')} #{run.get('run_number', run_id)} {conclusion} on {run.get('head_branch', 'main')}"],
            "visible_logs": gh.clean(text)[-60:],
            "findings": [],
            "config_files": {p: self._read(p, run.get("head_sha", "main")) for p in self.watch_files},
            "available_tools": [
                "view_logs",
                "inspect_config",
                "modify_config",
                "add_dependency",
                "rerun_pipeline",
                "finalize",
            ],
            "destructive_actions": 0,
            "incident_resolved": conclusion == "success",
            "_reward": 1.0 if conclusion == "success" else 0.0,
            "_done": conclusion == "success",
        }

    def reset(self, run_id: Optional[int] = None, **kw) -> Dict[str, Any]:
        """Reset environment to the target failed run state."""
        if run_id:
            run = gh.api("GET", f"/actions/runs/{run_id}", repo=self.repo)
        else:
            runs_resp = gh.api(
                "GET",
                "/actions/runs",
                repo=self.repo,
                params={"branch": "main", "status": "failure", "per_page": 1},
            )
            runs = runs_resp.get("workflow_runs", [])
            if not runs:
                # check if there's any recent run regardless of conclusion
                all_runs = gh.api("GET", "/actions/runs", repo=self.repo, params={"branch": "main", "per_page": 1}).get("workflow_runs", [])
                if all_runs and all_runs[0].get("conclusion") == "failure":
                    run = all_runs[0]
                else:
                    raise RuntimeError("No failed GitHub Actions run found on main branch.")
            else:
                run = runs[0]

        self.run_id = run["id"]
        self.run_url = run.get("html_url")
        self.failed_at = run.get("updated_at")
        self.main_sha = run.get("head_sha")
        self.head_sha = run.get("head_sha")
        self.branch = None
        self.pr = None
        self.pr_url = None
        self.last_diff = ""
        self.obs = self._build_obs(run)
        return self.obs

    def step(self, operation: str, target: str = "", value: str = "") -> Dict[str, Any]:
        """Execute an action against GitHub."""
        if operation in ("view_logs", "tail_logs"):
            jid = self.job_ids.get(target) or next(iter(self.job_ids.values()), None)
            text = gh.job_log(jid, repo=self.repo) if jid else ""
            o = dict(self.obs, visible_logs=gh.clean(text)[-80:], surfaced_errors=gh.extract_errors(text))
            self.obs = o
            return o

        if operation in ("inspect_config", "inspect_dockerfile", "inspect_permissions"):
            content = self._read(target, self._ref())
            self.obs["config_files"] = {**self.obs.get("config_files", {}), target: content}
            return self.obs

        if operation == "set_hypothesis":
            return self.obs

        if operation == "modify_config":
            self._commit(target, value)
            return self.obs

        if operation == "add_dependency":
            target_req = target or "requirements.txt"
            new_content = self._with_dependency(target_req, value)
            return self.step("modify_config", target_req, new_content)

        if operation == "rerun_pipeline":
            return self._ci_on_branch()

        if operation == "verify_fix":
            return self.obs

        if operation == "finalize":
            return self._merge()

        raise ValueError(f"Unsupported operation: {operation}")

    def _with_dependency(self, path: str, spec: str) -> str:
        text = self._read(path, self._ref())
        name = spec.split("==")[0].split(">=")[0].split("<=")[0].strip().lower()
        lines = [spec if ln.lower().startswith(name) else ln for ln in text.splitlines()]
        if spec not in lines and not any(ln.lower().startswith(name) for ln in lines):
            lines.append(spec)
        return "\n".join(lines) + "\n"

    def _commit(self, path: str, content: str) -> None:
        if not self.branch:
            self.branch = f"lazarus/fix-{self.run_id}"
            gh.create_branch(self.branch, repo=self.repo)

        old = self._read(path, self._ref())
        try:
            _, sha = gh.get_file(path, ref=self.branch, repo=self.repo)
        except Exception:
            sha = None

        resp = gh.put_file(
            path,
            content,
            f"fix({path}): Lazarus-CI automated repair",
            self.branch,
            sha=sha,
            repo=self.repo,
        )
        self.head_sha = resp.get("commit", {}).get("sha", self.head_sha)
        diff_str = "".join(
            difflib.unified_diff(
                old.splitlines(True),
                content.splitlines(True),
                f"a/{path}",
                f"b/{path}",
            )
        )
        self.last_diff += diff_str

    def _ci_on_branch(self) -> Dict[str, Any]:
        if not self.pr:
            pr = gh.api(
                "POST",
                "/pulls",
                repo=self.repo,
                json={
                    "title": f"Lazarus-CI repair for run #{self.run_id}",
                    "head": self.branch,
                    "base": "main",
                    "body": "Opened automatically by Lazarus-CI. CI rehearsal in progress...",
                },
            )
            self.pr = pr["number"]
            self.pr_url = pr.get("html_url")

        # Wait for the GitHub Actions run triggered on the PR branch
        completed_run = gh.wait_run(self.head_sha, timeout=300, repo=self.repo)
        self.obs = self._build_obs(completed_run)
        return self.obs

    def set_pr_body(self, text: str, labels: Optional[List[str]] = None) -> None:
        if self.pr:
            gh.api("PATCH", f"/pulls/{self.pr}", repo=self.repo, json={"body": text})
            if labels:
                try:
                    gh.api("POST", f"/issues/{self.pr}/labels", repo=self.repo, json={"labels": labels})
                except Exception:
                    pass

    def _merge(self) -> Dict[str, Any]:
        if not self.pr:
            raise RuntimeError("Cannot merge without an active PR")
        gh.api("PUT", f"/pulls/{self.pr}/merge", repo=self.repo, json={"merge_method": "squash"})
        main_sha = gh.branch_sha("main", repo=self.repo)
        run = gh.wait_run(main_sha, timeout=300, repo=self.repo)
        obs = self._build_obs(run)
        obs["incident_resolved"] = run.get("conclusion") == "success"
        self.obs = obs
        return obs

    def abandon(self) -> None:
        if self.pr:
            try:
                gh.api("PATCH", f"/pulls/{self.pr}", repo=self.repo, json={"state": "closed"})
            except Exception:
                pass
