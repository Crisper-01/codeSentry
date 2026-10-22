"""Shared fixtures. No test here touches Docker, GitHub or an LLM provider."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from container_manager import CommandResult, MainLanguage, Repository, Sandbox
from github_handler import Issue, PullRequest
from repo_config import RepoConfig


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep every test out of the developer's real ~/.codesentry registry."""
    monkeypatch.setenv("SENTRY_STATE_FILE", str(tmp_path / "repos.json"))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("SENTRY_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("SENTRY_LLM_MODEL", raising=False)

    import repository_store

    repository_store._repositories.clear()
    yield
    repository_store._repositories.clear()


@pytest.fixture
def config_file(tmp_path) -> Path:
    path = tmp_path / "cargo.json"
    path.write_text(
        json.dumps(
            {
                "watched_branches": ["main"],
                "build": {"mode": "cargo"},
                "test": {"mode": "cargo test"},
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def repository(config_file) -> Repository:
    return Repository(
        name="acme/engine",
        enabled=True,
        main_language=MainLanguage.RUST,
        config_path=str(config_file),
    )


@pytest.fixture
def repo_config() -> RepoConfig:
    from repo_config import BuildConfig, BuildMode, TestConfig, TestMode

    return RepoConfig(
        watched_branches=("main",),
        build=BuildConfig(mode=BuildMode.CARGO),
        test=TestConfig(mode=TestMode.CARGO_TEST),
        auto_test_pr=False,
    )


@pytest.fixture
def pull_request() -> PullRequest:
    return PullRequest(
        number=42,
        title="Fix the ring buffer wrap-around",
        body="Fixes #7",
        author="contributor",
        state="open",
        draft=False,
        base_ref="main",
        head_ref="fix/ring-buffer",
        head_sha="a" * 40,
        head_repo="acme/engine",
        html_url="https://github.com/acme/engine/pull/42",
    )


@pytest.fixture
def issue() -> Issue:
    return Issue(
        number=7,
        title="Ring buffer drops the last element when it wraps",
        body="Reading across the wrap boundary returns one element short.",
        state="open",
        labels=("bug",),
        html_url="https://github.com/acme/engine/issues/7",
    )


def ok(stdout: str = "") -> CommandResult:
    return CommandResult(stdout=stdout, stderr="", exit_code=0)


def failed(stderr: str = "error", exit_code: int = 1) -> CommandResult:
    return CommandResult(stdout="", stderr=stderr, exit_code=exit_code)


@contextmanager
def fake_workspace(repository, pr_number):
    yield Sandbox(container="codesentry-rust", workdir="/workspace/.runs/engine-pr42"), "b" * 40
