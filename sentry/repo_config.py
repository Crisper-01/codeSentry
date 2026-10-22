"""Per-repository JSON configuration (build / test / watched branches)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class BuildMode(Enum):
    CMAKE = "cmake"
    GCC = "gcc"
    CARGO = "cargo"
    CUSTOM = "custom"


class TestMode(Enum):
    CARGO_TEST = "cargo test"
    CMAKE_TEST = "cmake test"
    CUSTOM = "custom"


class RepoConfigError(Exception):
    """Raised when a repository config file is missing or invalid."""


@dataclass(frozen=True)
class BuildConfig:
    mode: BuildMode
    command: str | None = None


@dataclass(frozen=True)
class TestConfig:
    mode: TestMode
    command: str | None = None


@dataclass(frozen=True)
class RepoConfig:
    watched_branches: tuple[str, ...]
    build: BuildConfig
    test: TestConfig
    auto_test_pr: bool = False

    def build_command(self) -> str:
        if self.build.command:
            return self.build.command
        if self.build.mode is BuildMode.CMAKE:
            return "cmake -S . -B build && cmake --build build"
        if self.build.mode is BuildMode.GCC:
            return "make"
        if self.build.mode is BuildMode.CARGO:
            return "cargo build"
        raise RepoConfigError("build.mode 'custom' requires build.command")

    def test_command(self) -> str:
        if self.test.command:
            return self.test.command
        if self.test.mode is TestMode.CARGO_TEST:
            return "cargo test"
        if self.test.mode is TestMode.CMAKE_TEST:
            return "ctest --test-dir build --output-on-failure"
        raise RepoConfigError("test.mode 'custom' requires test.command")


_BUILD_MODES = {m.value: m for m in BuildMode}
_TEST_MODES = {m.value: m for m in TestMode}


def _require_mapping(data: object, label: str) -> dict:
    if not isinstance(data, dict):
        raise RepoConfigError(f"{label} must be an object")
    return data


def _parse_build(raw: object) -> BuildConfig:
    data = _require_mapping(raw, "build")
    mode_raw = data.get("mode")
    if not isinstance(mode_raw, str) or mode_raw not in _BUILD_MODES:
        allowed = ", ".join(sorted(_BUILD_MODES))
        raise RepoConfigError(f"build.mode must be one of: {allowed}")
    command = data.get("command")
    if command is not None and not isinstance(command, str):
        raise RepoConfigError("build.command must be a string when provided")
    if command is not None and not command.strip():
        command = None
    mode = _BUILD_MODES[mode_raw]
    if mode is BuildMode.CUSTOM and not command:
        raise RepoConfigError("build.mode 'custom' requires build.command")
    return BuildConfig(mode=mode, command=command)


def _parse_test(raw: object) -> TestConfig:
    data = _require_mapping(raw, "test")
    mode_raw = data.get("mode")
    if not isinstance(mode_raw, str) or mode_raw not in _TEST_MODES:
        allowed = ", ".join(sorted(_TEST_MODES))
        raise RepoConfigError(f"test.mode must be one of: {allowed}")
    command = data.get("command")
    if command is not None and not isinstance(command, str):
        raise RepoConfigError("test.command must be a string when provided")
    if command is not None and not command.strip():
        command = None
    mode = _TEST_MODES[mode_raw]
    if mode is TestMode.CUSTOM and not command:
        raise RepoConfigError("test.mode 'custom' requires test.command")
    return TestConfig(mode=mode, command=command)


def load_repo_config(path: str | Path) -> RepoConfig:
    """Load and validate a repository JSON config from disk."""
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise RepoConfigError(f"Config file not found: {config_path}")

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RepoConfigError(f"Invalid JSON in {config_path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise RepoConfigError("Config root must be a JSON object")

    branches_raw = payload.get("watched_branches")
    if not isinstance(branches_raw, list) or not branches_raw:
        raise RepoConfigError("watched_branches must be a non-empty array of strings")
    branches: list[str] = []
    for item in branches_raw:
        if not isinstance(item, str) or not item.strip():
            raise RepoConfigError("watched_branches entries must be non-empty strings")
        branches.append(item.strip())

    if "build" not in payload or "test" not in payload:
        raise RepoConfigError("Config must include 'build' and 'test' objects")

    auto_test_pr = payload.get("auto_test_pr", False)
    if not isinstance(auto_test_pr, bool):
        raise RepoConfigError("auto_test_pr must be a boolean")

    return RepoConfig(
        watched_branches=tuple(branches),
        build=_parse_build(payload["build"]),
        test=_parse_test(payload["test"]),
        auto_test_pr=auto_test_pr,
    )
