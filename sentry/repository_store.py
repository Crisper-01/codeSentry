"""Registry of watched repositories, persisted to disk.

The webhook server and the shell share this store, and it survives a restart so
an enabled repository keeps being watched without being re-added by hand.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import replace
from enum import Enum, auto
from pathlib import Path

from container_manager import (
    ContainerManagerError,
    MainLanguage,
    Repository,
    clone_repo,
    is_repo_cloned,
    remove_repo,
)
from github_handler import can_manage_repo, get_repo_language_bytes
from repo_config import RepoConfigError, load_repo_config

logger = logging.getLogger(__name__)

_repositories: dict[str, Repository] = {}
_state_lock = threading.Lock()


class AddRepoResult(Enum):
    ADDED = auto()
    ALREADY_EXISTS = auto()
    UNSUPPORTED_LANGUAGE = auto()


def state_file() -> Path:
    """Where the registry is persisted (override with SENTRY_STATE_FILE)."""
    configured = os.environ.get("SENTRY_STATE_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codesentry" / "repos.json"


def _save() -> None:
    path = state_file()
    payload = {
        "version": 1,
        "repositories": [
            {
                "name": repo.name,
                "enabled": repo.enabled,
                "main_language": repo.main_language.value,
                "config_path": repo.config_path,
            }
            for repo in sorted(_repositories.values(), key=lambda r: r.name)
        ],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
    except OSError as exc:
        logger.warning("Could not persist the repository registry to %s: %s", path, exc)


def load_state() -> None:
    """Restore the registry from disk. Invalid entries are skipped with a warning."""
    path = state_file()
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read the repository registry at %s: %s", path, exc)
        return

    languages = {lang.value: lang for lang in MainLanguage}
    restored = 0
    with _state_lock:
        _repositories.clear()
        for entry in payload.get("repositories", []):
            name = entry.get("name")
            language = languages.get(entry.get("main_language"))
            if not isinstance(name, str) or language is None:
                logger.warning("Skipping malformed registry entry: %r", entry)
                continue
            config_path = entry.get("config_path")
            _repositories[name] = Repository(
                name=name,
                enabled=bool(entry.get("enabled", False)),
                main_language=language,
                config_path=config_path if isinstance(config_path, str) else None,
            )
            restored += 1
    if restored:
        logger.info("Restored %d repositories from %s", restored, path)


def _detect_main_language(language_bytes: dict[str, int]) -> MainLanguage | None:
    rust_bytes = language_bytes.get("Rust", 0)
    cpp_bytes = language_bytes.get("C++", 0)
    if rust_bytes == 0 and cpp_bytes == 0:
        return None
    if rust_bytes >= cpp_bytes:
        return MainLanguage.RUST
    return MainLanguage.CPP


def add_repo(name: str, *, enabled: bool = False) -> AddRepoResult:
    """Add a repository after validating its main language (Rust or C++ only)."""
    if name in _repositories:
        return AddRepoResult.ALREADY_EXISTS

    main_language = _detect_main_language(get_repo_language_bytes(name))
    if main_language is None:
        return AddRepoResult.UNSUPPORTED_LANGUAGE

    with _state_lock:
        _repositories[name] = Repository(
            name=name,
            enabled=enabled,
            main_language=main_language,
        )
        _save()
    return AddRepoResult.ADDED


def _release_container_resources(repo: Repository) -> None:
    if is_repo_cloned(repo):
        remove_repo(repo)


def enable_repo(name: str) -> bool:
    """Enable monitoring for a repository. Returns False if not found.

    Requires a bound config file and clone permission (fetched live from GitHub).
    """
    repo = _repositories.get(name)
    if repo is None:
        return False
    if not repo.config_path:
        raise RepoConfigError(
            f"No config file bound for {name!r} (use: set config <owner/repo> <path>)"
        )

    permissions = can_manage_repo(name)
    if not permissions.can_clone:
        raise ContainerManagerError(f"No clone permission for {name!r}")

    if repo.enabled and is_repo_cloned(repo):
        return True
    if not is_repo_cloned(repo):
        clone_repo(repo)
    with _state_lock:
        _repositories[name] = replace(repo, enabled=True)
        _save()
    return True


def disable_repo(name: str) -> bool:
    """Disable monitoring for a repository. Returns False if not found."""
    repo = _repositories.get(name)
    if repo is None:
        return False
    if not repo.enabled and not is_repo_cloned(repo):
        return True
    _release_container_resources(repo)
    with _state_lock:
        _repositories[name] = replace(repo, enabled=False)
        _save()
    return True


def get_repo(name: str) -> Repository | None:
    """Return a stored repository by name, or None."""
    return _repositories.get(name)


def get_all_repos() -> list[Repository]:
    """Return all stored repositories."""
    return list(_repositories.values())


def set_repo_config(name: str, config_path: str) -> bool:
    """
    Bind a JSON config file to a repository after validating it.
    Returns False if the repository is not found.
    Raises RepoConfigError if the file is missing or invalid.
    """
    repo = _repositories.get(name)
    if repo is None:
        return False

    resolved = str(Path(config_path).expanduser().resolve())
    load_repo_config(resolved)  # validate early
    with _state_lock:
        _repositories[name] = replace(repo, config_path=resolved)
        _save()
    return True


def delete_repo(name: str) -> bool:
    """Remove a repository and release its container resources. Returns False if not found."""
    repo = _repositories.get(name)
    if repo is None:
        return False
    _release_container_resources(repo)
    with _state_lock:
        del _repositories[name]
        _save()
    return True
