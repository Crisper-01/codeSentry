import json

import pytest

from repo_config import BuildMode, RepoConfigError, TestMode, load_repo_config


def write(tmp_path, payload):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_loads_a_cargo_config_with_defaults(tmp_path):
    path = write(
        tmp_path,
        {
            "watched_branches": ["main", "develop"],
            "build": {"mode": "cargo"},
            "test": {"mode": "cargo test"},
        },
    )
    config = load_repo_config(path)

    assert config.watched_branches == ("main", "develop")
    assert config.build.mode is BuildMode.CARGO
    assert config.test.mode is TestMode.CARGO_TEST
    assert config.build_command() == "cargo build"
    assert config.test_command() == "cargo test"
    assert config.auto_test_pr is False


def test_cmake_defaults(tmp_path):
    path = write(
        tmp_path,
        {
            "watched_branches": ["main"],
            "build": {"mode": "cmake"},
            "test": {"mode": "cmake test"},
        },
    )
    config = load_repo_config(path)

    assert config.build_command() == "cmake -S . -B build && cmake --build build"
    assert config.test_command() == "ctest --test-dir build --output-on-failure"


def test_custom_mode_uses_the_given_command(tmp_path):
    path = write(
        tmp_path,
        {
            "watched_branches": ["main"],
            "build": {"mode": "custom", "command": "./scripts/build.sh"},
            "test": {"mode": "custom", "command": "./scripts/test.sh"},
        },
    )
    config = load_repo_config(path)

    assert config.build_command() == "./scripts/build.sh"
    assert config.test_command() == "./scripts/test.sh"


def test_auto_test_pr_opt_in(tmp_path):
    path = write(
        tmp_path,
        {
            "watched_branches": ["main"],
            "build": {"mode": "cargo"},
            "test": {"mode": "cargo test"},
            "auto_test_pr": True,
        },
    )
    assert load_repo_config(path).auto_test_pr is True


def test_auto_test_pr_must_be_a_boolean(tmp_path):
    path = write(
        tmp_path,
        {
            "watched_branches": ["main"],
            "build": {"mode": "cargo"},
            "test": {"mode": "cargo test"},
            "auto_test_pr": "yes",
        },
    )
    with pytest.raises(RepoConfigError, match="auto_test_pr"):
        load_repo_config(path)


def test_custom_mode_without_command_is_rejected(tmp_path):
    path = write(
        tmp_path,
        {
            "watched_branches": ["main"],
            "build": {"mode": "custom"},
            "test": {"mode": "cargo test"},
        },
    )
    with pytest.raises(RepoConfigError, match="requires build.command"):
        load_repo_config(path)


def test_empty_watched_branches_is_rejected(tmp_path):
    path = write(
        tmp_path,
        {"watched_branches": [], "build": {"mode": "cargo"}, "test": {"mode": "cargo test"}},
    )
    with pytest.raises(RepoConfigError, match="watched_branches"):
        load_repo_config(path)


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(RepoConfigError, match="not found"):
        load_repo_config(tmp_path / "absent.json")


def test_invalid_json_is_reported(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(RepoConfigError, match="Invalid JSON"):
        load_repo_config(path)
