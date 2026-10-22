import json

import pytest

import repository_store
from container_manager import MainLanguage, Repository
from repository_store import (
    AddRepoResult,
    add_repo,
    delete_repo,
    get_all_repos,
    get_repo,
    load_state,
    set_repo_config,
    state_file,
)


@pytest.fixture
def rust_repo_on_github(monkeypatch):
    monkeypatch.setattr(
        repository_store,
        "get_repo_language_bytes",
        lambda name: {"Rust": 9000, "TOML": 40},
    )


def test_add_detects_the_main_language(rust_repo_on_github):
    assert add_repo("acme/engine") is AddRepoResult.ADDED
    stored = get_repo("acme/engine")
    assert stored is not None
    assert stored.main_language is MainLanguage.RUST
    assert stored.enabled is False


def test_add_is_idempotent(rust_repo_on_github):
    add_repo("acme/engine")
    assert add_repo("acme/engine") is AddRepoResult.ALREADY_EXISTS


def test_a_repository_in_another_language_is_refused(monkeypatch):
    monkeypatch.setattr(
        repository_store,
        "get_repo_language_bytes",
        lambda name: {"Python": 9000},
    )
    assert add_repo("acme/scripts") is AddRepoResult.UNSUPPORTED_LANGUAGE
    assert get_repo("acme/scripts") is None


def test_cpp_wins_when_it_dominates(monkeypatch):
    monkeypatch.setattr(
        repository_store,
        "get_repo_language_bytes",
        lambda name: {"Rust": 100, "C++": 8000},
    )
    add_repo("acme/renderer")
    assert get_repo("acme/renderer").main_language is MainLanguage.CPP


def test_the_registry_survives_a_restart(rust_repo_on_github, config_file):
    add_repo("acme/engine")
    set_repo_config("acme/engine", str(config_file))

    assert state_file().is_file()
    payload = json.loads(state_file().read_text(encoding="utf-8"))
    assert payload["repositories"][0]["name"] == "acme/engine"

    # Simulate a fresh process.
    repository_store._repositories.clear()
    assert get_all_repos() == []

    load_state()
    restored = get_repo("acme/engine")
    assert restored is not None
    assert restored.main_language is MainLanguage.RUST
    assert restored.config_path == str(config_file.resolve())


def test_set_config_rejects_an_invalid_file(rust_repo_on_github, tmp_path):
    from repo_config import RepoConfigError

    add_repo("acme/engine")
    broken = tmp_path / "broken.json"
    broken.write_text("{}", encoding="utf-8")

    with pytest.raises(RepoConfigError):
        set_repo_config("acme/engine", str(broken))
    assert get_repo("acme/engine").config_path is None


def test_set_config_on_an_unknown_repo_returns_false(config_file):
    assert set_repo_config("acme/ghost", str(config_file)) is False


def test_malformed_registry_entries_are_skipped(rust_repo_on_github):
    add_repo("acme/engine")
    state_file().write_text(
        json.dumps(
            {
                "version": 1,
                "repositories": [
                    {"name": "acme/engine", "main_language": "Rust", "enabled": True},
                    {"name": "acme/broken", "main_language": "Python"},
                    {"main_language": "Rust"},
                ],
            }
        ),
        encoding="utf-8",
    )
    repository_store._repositories.clear()
    load_state()

    assert [r.name for r in get_all_repos()] == ["acme/engine"]


def test_delete_removes_the_entry(rust_repo_on_github, monkeypatch):
    monkeypatch.setattr(repository_store, "is_repo_cloned", lambda repo: False)
    add_repo("acme/engine")

    assert delete_repo("acme/engine") is True
    assert get_repo("acme/engine") is None
    assert delete_repo("acme/engine") is False


def test_enable_requires_a_bound_config(rust_repo_on_github):
    from repo_config import RepoConfigError

    add_repo("acme/engine")
    with pytest.raises(RepoConfigError, match="No config file bound"):
        repository_store.enable_repo("acme/engine")


def test_enable_requires_clone_permission(rust_repo_on_github, config_file, monkeypatch):
    from container_manager import ContainerManagerError
    from github_handler import RepoPermissions

    add_repo("acme/engine")
    set_repo_config("acme/engine", str(config_file))
    monkeypatch.setattr(
        repository_store,
        "can_manage_repo",
        lambda name: RepoPermissions(can_clone=False, can_create_pr=False, can_modify_pr=False),
    )

    with pytest.raises(ContainerManagerError, match="No clone permission"):
        repository_store.enable_repo("acme/engine")


def test_enable_clones_and_persists(rust_repo_on_github, config_file, monkeypatch):
    from github_handler import RepoPermissions

    cloned: list[Repository] = []
    add_repo("acme/engine")
    set_repo_config("acme/engine", str(config_file))
    monkeypatch.setattr(
        repository_store,
        "can_manage_repo",
        lambda name: RepoPermissions(can_clone=True, can_create_pr=True, can_modify_pr=True),
    )
    monkeypatch.setattr(repository_store, "is_repo_cloned", lambda repo: bool(cloned))
    monkeypatch.setattr(repository_store, "clone_repo", cloned.append)

    assert repository_store.enable_repo("acme/engine") is True
    assert len(cloned) == 1
    assert get_repo("acme/engine").enabled is True

    payload = json.loads(state_file().read_text(encoding="utf-8"))
    assert payload["repositories"][0]["enabled"] is True
