"""Lazarus-CI GitHub REST API Client.

Interfaces directly with the GitHub REST API to fetch logs, inspect configs,
create branches, push commits, open pull requests, poll workflow runs, and squash merge.
"""

from __future__ import annotations

import base64
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from dotenv import load_dotenv
import httpx

load_dotenv()


def get_token() -> str:
    return os.getenv("GITHUB_TOKEN", "").strip()


def get_repo() -> str:
    raw = os.getenv("GITHUB_REPO", "").strip()
    if "github.com/" in raw:
        raw = raw.split("github.com/")[-1]
    raw = raw.strip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]
    return raw


def _client() -> httpx.Client:
    token = get_token()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(
        base_url="https://api.github.com",
        timeout=60.0,
        follow_redirects=True,
        headers=headers,
    )


def api(method: str, path: str, repo: Optional[str] = None, **kw) -> Any:
    target_repo = repo or get_repo()
    if not target_repo:
        raise ValueError("GITHUB_REPO is not configured. Set GITHUB_REPO in .env (e.g. username/repo-name)")
    with _client() as c:
        r = c.request(method, f"/repos/{target_repo}{path}", **kw)
        r.raise_for_status()
        return r.json() if r.content else {}


def get_file(path: str, ref: str = "main", repo: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """Fetch file content and commit blob SHA from the target repository."""
    j = api("GET", f"/contents/{path}", repo=repo, params={"ref": ref})
    content = base64.b64decode(j["content"]).decode("utf-8", errors="replace")
    return content, j.get("sha")


def put_file(
    path: str,
    content: str,
    message: str,
    branch: str,
    sha: Optional[str] = None,
    repo: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or update a file in the repository."""
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        "branch": branch,
    }
    if sha:
        body["sha"] = sha
    return api("PUT", f"/contents/{path}", repo=repo, json=body)


def delete_file(
    path: str,
    message: str,
    sha: str,
    branch: str = "main",
    repo: Optional[str] = None,
) -> Dict[str, Any]:
    """Delete a file from the repository."""
    return api("DELETE", f"/contents/{path}", repo=repo, json={"message": message, "sha": sha, "branch": branch})


def branch_sha(branch: str = "main", repo: Optional[str] = None) -> str:
    """Get the head commit SHA for a branch."""
    j = api("GET", f"/git/ref/heads/{branch}", repo=repo)
    return j["object"]["sha"]


def create_branch(name: str, base: str = "main", repo: Optional[str] = None) -> None:
    """Create a new git branch from base."""
    sha = branch_sha(base, repo=repo)
    try:
        api("POST", "/git/refs", repo=repo, json={"ref": f"refs/heads/{name}", "sha": sha})
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 422:  # 422 = branch already exists
            raise


def jobs(run_id: int, repo: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all jobs in a GitHub Actions workflow run."""
    res = api("GET", f"/actions/runs/{run_id}/jobs", repo=repo)
    return res.get("jobs", [])


def job_log(job_id: int, repo: Optional[str] = None) -> str:
    """Fetch raw console logs for a specific job."""
    target_repo = repo or get_repo()
    with _client() as c:
        r = c.get(f"/repos/{target_repo}/actions/jobs/{job_id}/logs")
        return r.text if r.status_code == 200 else ""


def wait_run(sha: str, timeout: int = 300, repo: Optional[str] = None) -> Dict[str, Any]:
    """Poll GitHub Actions runs until a run for the given commit head_sha completes."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            rs = api("GET", "/actions/runs", repo=repo, params={"head_sha": sha, "per_page": 5}).get("workflow_runs", [])
            done = [r for r in rs if r["status"] == "completed"]
            if done:
                return done[0]
        except Exception:
            pass
        time.sleep(4)
    raise TimeoutError(f"CI run for sha {sha[:8]} did not finish within {timeout} seconds")


_TS = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s?")
_ERR = re.compile(r"(error|failed|fatal|no matching distribution|not found|assert|exception|denied)", re.I)


def clean(text: str) -> List[str]:
    """Strip GitHub log timestamps and group wrappers."""
    return [_TS.sub("", ln) for ln in text.splitlines() if not ln.startswith(("##[group]", "##[endgroup]"))]


def extract_errors(text: str, n: int = 8) -> List[str]:
    """Extract distinct error lines from job logs."""
    out: List[str] = []
    for ln in clean(text):
        s = ln.strip()[:240]
        if s and _ERR.search(s) and s not in out:
            out.append(s)
    return out[:n]
