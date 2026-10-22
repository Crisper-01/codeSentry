"""Sandbox execution: clone repositories into language containers and run commands.

The persistent clone lives at /workspace/<repo-name> and is never mutated by a
review run. Each pull request run gets a throwaway git worktree under
/workspace/.runs/, so concurrent runs on the same repository cannot fight over
the same working tree.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum

from github_handler import can_manage_repo
from repo_config import RepoConfigError, load_repo_config


class MainLanguage(Enum):
    RUST = "Rust"
    CPP = "C++"


@dataclass(frozen=True)
class Repository:
    name: str
    enabled: bool
    main_language: MainLanguage
    config_path: str | None = None


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class Sandbox:
    """A directory inside a language container where commands are executed."""

    container: str
    workdir: str


class ContainerManagerError(Exception):
    """Raised when a container operation fails."""


_CONTAINER_BY_LANGUAGE: dict[MainLanguage, str] = {
    MainLanguage.RUST: "codesentry-rust",
    MainLanguage.CPP: "codesentry-cpp",
}

_WORKSPACE = "/workspace"
_RUNS_DIR = f"{_WORKSPACE}/.runs"

_DEFAULT_COMMAND_TIMEOUT = 1800.0
_TIMEOUT_EXIT_CODE = 124

_COMMIT_AUTHOR_NAME = "CodeSentry"
_COMMIT_AUTHOR_EMAIL = "codesentry@users.noreply.github.com"

_repo_locks: dict[str, threading.Lock] = {}
_repo_locks_guard = threading.Lock()


def repo_lock(repo_name: str) -> threading.Lock:
    """Return the lock serialising sandbox work for one repository."""
    with _repo_locks_guard:
        return _repo_locks.setdefault(repo_name, threading.Lock())


def _repo_basename(repo: str) -> str:
    return repo.strip().split("/", maxsplit=1)[1]


def _container_for(repo: Repository) -> str:
    return _CONTAINER_BY_LANGUAGE[repo.main_language]


def _repo_path(repo: Repository) -> str:
    return f"{_WORKSPACE}/{_repo_basename(repo.name)}"


def base_sandbox(repo: Repository) -> Sandbox:
    """The sandbox pointing at the repository's persistent clone."""
    return Sandbox(container=_container_for(repo), workdir=_repo_path(repo))


def _clone_url(repo: str) -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return f"https://x-access-token:{token}@github.com/{repo}.git"
    return f"https://github.com/{repo}.git"


def _redact(text: str) -> str:
    """Strip the token out of git output before it reaches logs or a PR comment."""
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return text.replace(token, "***")
    return text


def _docker_exec(container: str, *args: str) -> None:
    result = subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = _redact((result.stderr or result.stdout).strip())
        raise ContainerManagerError(detail or f"docker exec failed in {container}")


def command_timeout() -> float:
    """Wall-clock budget for one sandbox command (override with SENTRY_COMMAND_TIMEOUT)."""
    raw = os.environ.get("SENTRY_COMMAND_TIMEOUT", "").strip()
    if not raw:
        return _DEFAULT_COMMAND_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_COMMAND_TIMEOUT
    return value if value > 0 else _DEFAULT_COMMAND_TIMEOUT


def _docker_exec_shell(container: str, workdir: str, command: str) -> CommandResult:
    # Use bash -c (not -lc): a login shell resets PATH and drops /usr/local/cargo/bin
    # from the official rust image env.
    timeout = command_timeout()
    try:
        result = subprocess.run(
            ["docker", "exec", "-w", workdir, container, "bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            stdout=_redact(exc.stdout or "" if isinstance(exc.stdout, str) else ""),
            stderr=f"CodeSentry: command exceeded the {timeout:.0f}s timeout and was aborted.",
            exit_code=_TIMEOUT_EXIT_CODE,
        )
    return CommandResult(
        stdout=_redact(result.stdout),
        stderr=_redact(result.stderr),
        exit_code=result.returncode,
    )


def _git(sandbox: Sandbox, *args: str) -> CommandResult:
    command = " ".join(shlex.quote(part) for part in ("git", *args))
    return _docker_exec_shell(sandbox.container, sandbox.workdir, command)


def _git_checked(sandbox: Sandbox, *args: str) -> str:
    result = _git(sandbox, *args)
    if not result.ok:
        detail = (result.stderr or result.stdout).strip()
        raise ContainerManagerError(f"git {args[0]} failed: {detail}")
    return result.stdout.strip()


def clone_repo(repository: Repository) -> None:
    """Clone a repository into the sandbox matching its main language."""
    permissions = can_manage_repo(repository.name)
    if not permissions.can_clone:
        raise ContainerManagerError(f"No clone permission for {repository.name!r}")

    container = _container_for(repository)
    destination = _repo_path(repository)
    _docker_exec(
        container,
        "git",
        "clone",
        _clone_url(repository.name),
        destination,
    )


def remove_repo(repository: Repository) -> None:
    """Remove a repository directory and its run worktrees from the sandbox."""
    container = _container_for(repository)
    _docker_exec(container, "rm", "-rf", _repo_path(repository))
    _docker_exec(
        container,
        "bash",
        "-c",
        f"rm -rf {shlex.quote(_RUNS_DIR)}/{shlex.quote(_repo_basename(repository.name))}-pr*",
    )


def is_repo_cloned(repository: Repository) -> bool:
    """Return True if the repository directory exists in its sandbox container."""
    container = _container_for(repository)
    result = subprocess.run(
        ["docker", "exec", container, "test", "-d", _repo_path(repository)],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = _redact((result.stderr or result.stdout).strip())
    raise ContainerManagerError(detail or f"docker exec failed in {container}")


def _require_ready_for_command(repository: Repository) -> str:
    if not repository.config_path:
        raise ContainerManagerError(
            f"No config file bound for {repository.name!r} (use: set config <owner/repo> <path>)"
        )
    if not is_repo_cloned(repository):
        raise ContainerManagerError(
            f"Repository {repository.name!r} is not cloned (enable it first)"
        )
    return repository.config_path


def _run_configured(repository: Repository, sandbox: Sandbox | None, kind: str) -> CommandResult:
    config_path = _require_ready_for_command(repository)
    try:
        config = load_repo_config(config_path)
        command = config.build_command() if kind == "build" else config.test_command()
    except RepoConfigError as exc:
        raise ContainerManagerError(str(exc)) from exc

    target = sandbox or base_sandbox(repository)
    return _docker_exec_shell(target.container, target.workdir, command)


def run_build(repository: Repository, sandbox: Sandbox | None = None) -> CommandResult:
    """Run the configured build command; defaults to the repository's persistent clone."""
    return _run_configured(repository, sandbox, "build")


def run_tests(repository: Repository, sandbox: Sandbox | None = None) -> CommandResult:
    """Run the configured test command; defaults to the repository's persistent clone."""
    return _run_configured(repository, sandbox, "test")


@contextmanager
def pr_workspace(repository: Repository, pr_number: int) -> Iterator[tuple[Sandbox, str]]:
    """Check a pull request head out into a throwaway worktree.

    Yields the sandbox and the commit sha actually checked out (which may differ
    from the sha in the webhook payload if the branch moved in between).
    """
    _require_ready_for_command(repository)
    base = base_sandbox(repository)
    local_ref = f"refs/codesentry/pr-{pr_number}"
    worktree = f"{_RUNS_DIR}/{_repo_basename(repository.name)}-pr{pr_number}"

    # A stale worktree from a crashed run would make `worktree add` fail.
    _git(base, "worktree", "remove", "--force", worktree)
    _docker_exec(base.container, "rm", "-rf", worktree)
    _git(base, "worktree", "prune")

    _git_checked(
        base,
        "fetch",
        "--force",
        "origin",
        f"pull/{pr_number}/head:{local_ref}",
    )
    _git_checked(base, "worktree", "add", "--detach", worktree, local_ref)

    sandbox = Sandbox(container=base.container, workdir=worktree)
    try:
        head_sha = _git_checked(sandbox, "rev-parse", "HEAD")
        yield sandbox, head_sha
    finally:
        _git(base, "worktree", "remove", "--force", worktree)
        _git(base, "worktree", "prune")


def list_tracked_files(sandbox: Sandbox) -> list[str]:
    """Repository-relative paths tracked by git in a sandbox worktree."""
    result = _git(sandbox, "ls-files")
    if not result.ok:
        raise ContainerManagerError(
            f"git ls-files failed: {(result.stderr or result.stdout).strip()}"
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def write_files(sandbox: Sandbox, files: Mapping[str, str]) -> None:
    """Write files (repository-relative paths) inside a sandbox, creating parents."""
    for relative_path, content in files.items():
        if relative_path.startswith("/") or ".." in relative_path.split("/"):
            raise ContainerManagerError(f"Refusing to write outside the workspace: {relative_path!r}")
        target = f"{sandbox.workdir}/{relative_path}"
        script = 'mkdir -p "$(dirname "$1")" && cat > "$1"'
        result = subprocess.run(
            [
                "docker", "exec", "-i", "-w", sandbox.workdir, sandbox.container,
                "bash", "-c", script, "_", target,
            ],
            input=content,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = _redact((result.stderr or result.stdout).strip())
            raise ContainerManagerError(f"Failed to write {relative_path}: {detail}")


def publish_branch(
    sandbox: Sandbox,
    *,
    target_repo: str,
    branch: str,
    commit_message: str,
    paths: list[str],
) -> None:
    """Commit the given paths in the sandbox worktree and push them as a new branch."""
    _git_checked(sandbox, "checkout", "-B", branch)
    _git_checked(sandbox, "add", "--", *paths)

    status = _git(sandbox, "status", "--porcelain", "--", *paths)
    if not status.stdout.strip():
        raise ContainerManagerError("Nothing to commit for the generated tests")

    _git_checked(
        sandbox,
        "-c", f"user.name={_COMMIT_AUTHOR_NAME}",
        "-c", f"user.email={_COMMIT_AUTHOR_EMAIL}",
        "commit",
        "-m", commit_message,
    )
    _git_checked(sandbox, "push", "--force", _clone_url(target_repo), f"HEAD:refs/heads/{branch}")
