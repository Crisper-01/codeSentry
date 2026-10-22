"""GitHub webhook receiver for pull_request opened / synchronize.

Expose this HTTP endpoint publicly (or via a tunnel) so GitHub can reach it:
  smee -u https://smee.io/<channel> --target http://127.0.0.1:8080/webhook/github
  cloudflared tunnel --url http://127.0.0.1:8080
Configure the GitHub app/webhook URL to POST to /webhook/github.
Optional: set GITHUB_WEBHOOK_SECRET and the same secret on the GitHub webhook.

A review takes minutes, far longer than the few seconds GitHub waits for a
webhook response, so the endpoint accepts the event, answers immediately, and
runs the review graph on a background thread.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

import review_graph
from container_manager import ContainerManagerError, Repository
from github_handler import GitHubHandlerError
from repo_config import RepoConfigError, load_repo_config
from repository_store import get_repo

logger = logging.getLogger(__name__)

app = FastAPI(title="CodeSentry webhook", docs_url=None, redoc_url=None, openapi_url=None)

_WATCHED_ACTIONS = frozenset({"opened", "synchronize", "reopened"})

# (repo, head_sha) pairs already reviewed, so a redelivered or duplicated event
# does not start the same run twice.
_seen_commits: set[tuple[str, str]] = set()
_seen_guard = threading.Lock()


def _claim_commit(repo: str, head_sha: str) -> bool:
    """Return True if this (repo, sha) has not been reviewed yet, and claim it."""
    key = (repo, head_sha)
    with _seen_guard:
        if key in _seen_commits:
            return False
        _seen_commits.add(key)
        return True


def release_commit(repo: str, head_sha: str) -> None:
    """Forget a claimed commit so it can be reviewed again (used when a run fails to start)."""
    with _seen_guard:
        _seen_commits.discard((repo, head_sha))


def _verify_signature(body: bytes, signature_header: str | None) -> None:
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        return
    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    received = signature_header.removeprefix("sha256=")
    if not hmac.compare_digest(expected, received):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


def _run_review(repo: Repository, pr_number: int, head_sha: str) -> None:
    try:
        record = review_graph.review_pull_request(repo, pr_number, trigger="webhook")
    except (ContainerManagerError, GitHubHandlerError) as exc:
        release_commit(repo.name, head_sha)
        logger.error("Review of %s#%s failed to start: %s", repo.name, pr_number, exc)
        return
    logger.info(
        "Review %s finished for %s#%s: %s",
        record.run_id,
        repo.name,
        pr_number,
        record.status,
    )


def _handle_pull_request(payload: dict[str, Any]) -> dict[str, Any]:
    action = payload.get("action")
    if action not in _WATCHED_ACTIONS:
        return {"status": "ignored", "reason": f"action {action!r}"}

    pr = payload.get("pull_request") or {}
    base = pr.get("base") or {}
    head = pr.get("head") or {}
    target_branch = base.get("ref")
    head_sha = head.get("sha") or ""
    pr_number = pr.get("number")
    repository = payload.get("repository") or {}
    full_name = repository.get("full_name")

    if not isinstance(full_name, str) or not full_name:
        raise HTTPException(status_code=400, detail="Missing repository.full_name")
    if not isinstance(target_branch, str) or not target_branch:
        raise HTTPException(status_code=400, detail="Missing pull_request.base.ref")
    if not isinstance(pr_number, int):
        raise HTTPException(status_code=400, detail="Missing pull_request.number")

    if pr.get("draft"):
        return {"status": "ignored", "reason": "draft pull request"}

    repo = get_repo(full_name)
    if repo is None or not repo.enabled:
        return {"status": "ignored", "reason": "repo not enabled"}

    if not repo.config_path:
        return {"status": "ignored", "reason": "no config bound"}

    try:
        config = load_repo_config(repo.config_path)
    except RepoConfigError as exc:
        logger.warning("Invalid config for %s: %s", full_name, exc)
        return {"status": "error", "reason": str(exc)}

    if target_branch not in config.watched_branches:
        return {
            "status": "ignored",
            "reason": f"branch {target_branch!r} not watched",
        }

    if head_sha and not _claim_commit(full_name, head_sha):
        return {"status": "ignored", "reason": f"commit {head_sha[:12]} already reviewed"}

    threading.Thread(
        target=_run_review,
        args=(repo, pr_number, head_sha),
        name=f"review-{full_name}-{pr_number}",
        daemon=True,
    ).start()

    logger.info(
        "Accepted PR #%s on %s (base=%s, head=%s)",
        pr_number,
        full_name,
        target_branch,
        head_sha[:12],
    )
    return {
        "status": "accepted",
        "repo": full_name,
        "pr": pr_number,
        "branch": target_branch,
        "head_sha": head_sha,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict[str, Any]:
    body = await request.body()
    _verify_signature(body, x_hub_signature_256)

    if x_github_event == "ping":
        return {"status": "pong"}
    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": f"event {x_github_event!r}"}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Webhook payload must be a JSON object")
    return _handle_pull_request(payload)


def start_webhook_server(*, host: str | None = None, port: int | None = None) -> None:
    """Start uvicorn in a daemon thread so the interactive shell can keep running.

    Blocks briefly until the server reports it has started, so the shell prompt
    does not appear before uvicorn's startup INFO lines.
    """
    import time

    import uvicorn

    host = host or os.environ.get("SENTRY_WEBHOOK_HOST", "0.0.0.0")
    port = port or int(os.environ.get("SENTRY_WEBHOOK_PORT", "8080"))

    config = uvicorn.Config(app, host=host, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, name="sentry-webhook", daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)

    if not server.started:
        logger.warning(
            "Webhook server did not report ready in time (http://%s:%s/webhook/github)",
            host,
            port,
        )
    else:
        logger.info("Webhook listening on http://%s:%s/webhook/github", host, port)
