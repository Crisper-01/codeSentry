"""Webhook filtering, signature verification and idempotence (scenario D of the plan)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import webhook
from container_manager import MainLanguage, Repository


@pytest.fixture
def started(monkeypatch):
    """Capture the reviews the webhook would launch instead of launching them."""
    launched: list[tuple[str, int]] = []

    class ImmediateThread:
        def __init__(self, target, args=(), **kwargs):
            self._target = target
            self._args = args

        def start(self):
            repo, pr_number, _head_sha = self._args
            launched.append((repo.name, pr_number))

    monkeypatch.setattr(webhook.threading, "Thread", ImmediateThread)
    webhook._seen_commits.clear()
    return launched


@pytest.fixture
def client():
    return TestClient(webhook.app)


@pytest.fixture
def watched_repo(monkeypatch, config_file):
    repo = Repository(
        name="acme/engine",
        enabled=True,
        main_language=MainLanguage.RUST,
        config_path=str(config_file),
    )
    monkeypatch.setattr(webhook, "get_repo", lambda name: repo if name == repo.name else None)
    return repo


def payload(
    *,
    action="opened",
    repo="acme/engine",
    base="main",
    number=42,
    head_sha="a" * 40,
    draft=False,
):
    return {
        "action": action,
        "number": number,
        "pull_request": {
            "number": number,
            "draft": draft,
            "base": {"ref": base},
            "head": {"sha": head_sha, "ref": "feature"},
        },
        "repository": {"full_name": repo},
    }


def post(client, body, event="pull_request"):
    return client.post("/webhook/github", json=body, headers={"X-GitHub-Event": event})


def test_health_endpoint(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_ping_is_answered(client):
    assert post(client, {}, event="ping").json() == {"status": "pong"}


def test_other_events_are_ignored(client):
    assert post(client, {}, event="push").json()["status"] == "ignored"


def test_a_watched_branch_starts_a_review(client, watched_repo, started):
    response = post(client, payload())

    assert response.json()["status"] == "accepted"
    assert started == [("acme/engine", 42)]


def test_scenario_d_an_unwatched_branch_is_a_no_op(client, watched_repo, started):
    response = post(client, payload(base="experimental"))

    assert response.json() == {
        "status": "ignored",
        "reason": "branch 'experimental' not watched",
    }
    assert started == []


def test_an_unknown_repository_is_ignored(client, watched_repo, started):
    assert post(client, payload(repo="other/project")).json()["status"] == "ignored"
    assert started == []


def test_a_disabled_repository_is_ignored(client, monkeypatch, config_file, started):
    repo = Repository(
        name="acme/engine",
        enabled=False,
        main_language=MainLanguage.RUST,
        config_path=str(config_file),
    )
    monkeypatch.setattr(webhook, "get_repo", lambda name: repo)

    assert post(client, payload()).json()["reason"] == "repo not enabled"
    assert started == []


def test_a_repository_without_a_config_is_ignored(client, monkeypatch, started):
    repo = Repository(name="acme/engine", enabled=True, main_language=MainLanguage.RUST)
    monkeypatch.setattr(webhook, "get_repo", lambda name: repo)

    assert post(client, payload()).json()["reason"] == "no config bound"
    assert started == []


def test_draft_pull_requests_are_ignored(client, watched_repo, started):
    assert post(client, payload(draft=True)).json()["reason"] == "draft pull request"
    assert started == []


@pytest.mark.parametrize("action", ["closed", "labeled", "assigned"])
def test_uninteresting_actions_are_ignored(client, watched_repo, started, action):
    assert post(client, payload(action=action)).json()["status"] == "ignored"
    assert started == []


def test_synchronize_and_reopened_are_watched(client, watched_repo, started):
    post(client, payload(action="synchronize", head_sha="b" * 40))
    post(client, payload(action="reopened", head_sha="c" * 40))
    assert started == [("acme/engine", 42), ("acme/engine", 42)]


def test_the_same_commit_is_only_reviewed_once(client, watched_repo, started):
    first = post(client, payload(head_sha="d" * 40))
    second = post(client, payload(action="synchronize", head_sha="d" * 40))

    assert first.json()["status"] == "accepted"
    assert second.json()["status"] == "ignored"
    assert "already reviewed" in second.json()["reason"]
    assert started == [("acme/engine", 42)]


def test_a_new_commit_on_the_same_pr_is_reviewed_again(client, watched_repo, started):
    post(client, payload(head_sha="d" * 40))
    response = post(client, payload(action="synchronize", head_sha="e" * 40))

    assert response.json()["status"] == "accepted"
    assert len(started) == 2


def test_a_failed_run_releases_its_commit(watched_repo):
    assert webhook._claim_commit("acme/engine", "f" * 40) is True
    assert webhook._claim_commit("acme/engine", "f" * 40) is False

    webhook.release_commit("acme/engine", "f" * 40)
    assert webhook._claim_commit("acme/engine", "f" * 40) is True


def test_a_malformed_payload_is_rejected(client, watched_repo):
    response = client.post(
        "/webhook/github",
        content=b"{ not json",
        headers={"X-GitHub-Event": "pull_request", "Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_a_payload_without_a_repository_is_rejected(client, watched_repo):
    body = payload()
    del body["repository"]
    assert post(client, body).status_code == 400


def test_a_valid_signature_is_accepted(client, watched_repo, started, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cret")
    body = json.dumps(payload()).encode()
    digest = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()

    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": f"sha256={digest}",
            "Content-Type": "application/json",
        },
    )
    assert response.json()["status"] == "accepted"


def test_a_wrong_signature_is_refused(client, watched_repo, started, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cret")
    body = json.dumps(payload()).encode()

    response = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 401
    assert started == []


def test_a_missing_signature_is_refused_when_a_secret_is_set(client, watched_repo, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "s3cret")
    assert post(client, payload()).status_code == 401
