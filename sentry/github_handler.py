"""GitHub REST client: repository metadata, permissions, pull requests, issues.

All calls go through the REST API with the token in GITHUB_TOKEN. This is the
single GitHub integration path used by the shell, the webhook and the review
graph.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

GITHUB_API = "https://api.github.com"
_DEFAULT_TIMEOUT = httpx.Timeout(10.0)


@dataclass(frozen=True)
class RepoPermissions:
    can_clone: bool
    can_create_pr: bool
    can_modify_pr: bool


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    author: str
    state: str
    draft: bool
    base_ref: str
    head_ref: str
    head_sha: str
    head_repo: str
    html_url: str


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    state: str
    labels: tuple[str, ...]
    html_url: str


class GitHubHandlerError(Exception):
    """Raised when the GitHub API returns an unexpected error."""


def _parse_repo(repo: str) -> tuple[str, str]:
    parts = repo.strip().split("/", maxsplit=1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid repository format: {repo!r} (expected 'owner/name')")
    return parts[0], parts[1]


def _auth_headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    accept: str = "application/vnd.github+json",
) -> httpx.Response:
    with httpx.Client(timeout=_DEFAULT_TIMEOUT) as client:
        response = client.request(
            method,
            f"{GITHUB_API}{path}",
            headers=_auth_headers(accept),
            json=json_body,
        )
    if response.status_code == 404:
        raise GitHubHandlerError(f"Repository or resource not found: {path}")
    if response.status_code == 401:
        raise GitHubHandlerError("GitHub authentication failed (check GITHUB_TOKEN)")
    if response.status_code == 403:
        raise GitHubHandlerError("GitHub API access forbidden (insufficient permissions or rate limit)")
    if not response.is_success:
        raise GitHubHandlerError(
            f"GitHub API error {response.status_code}: {response.text.strip()}"
        )
    return response


def get_repo_language_bytes(repo: str) -> dict[str, int]:
    """Return programming languages mapped to their byte count on the default branch."""
    owner, name = _parse_repo(repo)
    response = _request("GET", f"/repos/{owner}/{name}/languages")
    return response.json()


def get_repo_languages(repo: str) -> list[str]:
    """Return programming languages sorted by usage (most used first)."""
    languages = get_repo_language_bytes(repo)
    return sorted(languages, key=languages.get, reverse=True)


def can_manage_repo(repo: str) -> RepoPermissions:
    """Return clone and pull-request permissions for the authenticated user."""
    owner, name = _parse_repo(repo)
    response = _request("GET", f"/repos/{owner}/{name}")
    data = response.json()

    permissions = data.get("permissions") or {}
    is_public = not data.get("private", True)

    can_read = is_public or permissions.get("pull", False)
    can_write = any(
        permissions.get(role, False)
        for role in ("push", "maintain", "admin")
    )

    return RepoPermissions(
        can_clone=can_read,
        can_create_pr=can_write,
        can_modify_pr=can_write,
    )


def require_can_create_pr(repo: str) -> RepoPermissions:
    """Fetch permissions and raise if the user cannot open/modify pull requests."""
    permissions = can_manage_repo(repo)
    if not permissions.can_create_pr:
        raise GitHubHandlerError(
            f"Insufficient permissions to create a pull request on {repo!r}"
        )
    return permissions


def get_pull_request(repo: str, pr_number: int) -> PullRequest:
    """Fetch pull request metadata (base/head refs, head sha, author, description)."""
    owner, name = _parse_repo(repo)
    data = _request("GET", f"/repos/{owner}/{name}/pulls/{pr_number}").json()

    head = data.get("head") or {}
    base = data.get("base") or {}
    head_repo = (head.get("repo") or {}).get("full_name") or repo
    user = (data.get("user") or {}).get("login") or "unknown"

    return PullRequest(
        number=int(data["number"]),
        title=data.get("title") or "",
        body=data.get("body") or "",
        author=user,
        state=data.get("state") or "unknown",
        draft=bool(data.get("draft", False)),
        base_ref=base.get("ref") or "",
        head_ref=head.get("ref") or "",
        head_sha=head.get("sha") or "",
        head_repo=head_repo,
        html_url=data.get("html_url") or "",
    )


def get_pull_request_diff(repo: str, pr_number: int) -> str:
    """Fetch the unified diff of a pull request."""
    owner, name = _parse_repo(repo)
    response = _request(
        "GET",
        f"/repos/{owner}/{name}/pulls/{pr_number}",
        accept="application/vnd.github.v3.diff",
    )
    return response.text


def get_pull_request_files(repo: str, pr_number: int) -> list[str]:
    """Return the repository-relative paths touched by a pull request."""
    owner, name = _parse_repo(repo)
    paths: list[str] = []
    page = 1
    while True:
        data = _request(
            "GET",
            f"/repos/{owner}/{name}/pulls/{pr_number}/files?per_page=100&page={page}",
        ).json()
        if not data:
            break
        paths.extend(entry["filename"] for entry in data if "filename" in entry)
        if len(data) < 100:
            break
        page += 1
    return paths


def get_issue(repo: str, issue_number: int) -> Issue:
    """Fetch an issue (requirements the pull request is supposed to satisfy)."""
    owner, name = _parse_repo(repo)
    data = _request("GET", f"/repos/{owner}/{name}/issues/{issue_number}").json()
    labels = tuple(
        label["name"]
        for label in data.get("labels") or []
        if isinstance(label, dict) and "name" in label
    )
    return Issue(
        number=int(data["number"]),
        title=data.get("title") or "",
        body=data.get("body") or "",
        state=data.get("state") or "unknown",
        labels=labels,
        html_url=data.get("html_url") or "",
    )


_CLOSING_KEYWORD = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d+)\b",
    re.IGNORECASE,
)
_BARE_REFERENCE = re.compile(r"(?:^|\s)#(\d+)\b")


def find_linked_issue_number(pull_request: PullRequest) -> int | None:
    """Extract the issue a pull request claims to close.

    Closing keywords ("fixes #12") win over a bare "#12" mention, and the title
    is searched before the body.
    """
    for pattern in (_CLOSING_KEYWORD, _BARE_REFERENCE):
        for text in (pull_request.title, pull_request.body):
            match = pattern.search(text or "")
            if match:
                return int(match.group(1))
    return None


def post_pr_comment(repo: str, pr_number: int, body: str) -> str:
    """Post a markdown comment on a pull request and return the comment URL."""
    owner, name = _parse_repo(repo)
    data = _request(
        "POST",
        f"/repos/{owner}/{name}/issues/{pr_number}/comments",
        json_body={"body": body},
    ).json()
    return data.get("html_url") or ""


def create_pull_request(
    repo: str,
    *,
    head: str,
    base: str,
    title: str,
    body: str,
) -> str:
    """Open a pull request and return its URL. The head branch must already exist."""
    owner, name = _parse_repo(repo)
    data = _request(
        "POST",
        f"/repos/{owner}/{name}/pulls",
        json_body={"head": head, "base": base, "title": title, "body": body},
    ).json()
    return data.get("html_url") or ""
