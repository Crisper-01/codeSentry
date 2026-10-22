import pytest

import prompts


def test_every_template_splits_into_a_system_and_a_user_part():
    assert set(prompts.available()) == {
        "feedback_comment",
        "intent",
        "release_notes",
        "test_generation",
    }
    for name in prompts.available():
        system, user = prompts._load(name)
        assert system, f"{name} has an empty system part"
        assert user, f"{name} has an empty user part"


COMMON = {
    "repo": "acme/engine",
    "language": "Rust",
    "pr_number": "42",
    "author": "contributor",
    "base_ref": "main",
    "head_ref": "fix/ring-buffer",
    "title": "Fix the wrap-around",
    "body": "Fixes #7",
    "issue": "#7 - wrap-around bug",
    "files": "- src/ring.rs",
    "diff": "@@ -1 +1 @@\n-old\n+new",
}


def test_intent_template_renders():
    system, user = prompts.render("intent", **COMMON)
    assert "C++ and Rust" in system
    assert "acme/engine" in user
    assert "@@ -1 +1 @@" in user


def test_release_notes_template_renders():
    system, user = prompts.render(
        "release_notes", **COMMON, build_test_summary="Build: passed\nTests: passed"
    )
    assert "release note" in system.lower()
    assert "Build: passed" in user


def test_test_generation_template_renders():
    system, user = prompts.render(
        "test_generation",
        **COMMON,
        repo_files="- tests/existing.rs",
        test_command="cargo test",
    )
    assert "new" in system
    assert "cargo test" in user
    assert "tests/existing.rs" in user


def test_feedback_comment_template_renders():
    _, body = prompts.render(
        "feedback_comment",
        head_sha="abc123",
        llm_label="anthropic/claude-opus-5",
        build_line="passed",
        test_line="passed",
        intent_line="matches the issue",
        intent_body="Looks right.",
        release_notes="Fixed a thing.",
        tests_section="No additional tests needed.",
        logs_section="",
        run_id="deadbeef",
    )
    assert "## CodeSentry review" in body
    assert "abc123" in body
    assert "deadbeef" in body


def test_a_missing_placeholder_is_reported():
    incomplete = dict(COMMON)
    incomplete.pop("diff")
    with pytest.raises(prompts.PromptError, match="Missing placeholder"):
        prompts.render("intent", **incomplete)


def test_an_unknown_template_is_reported():
    with pytest.raises(prompts.PromptError, match="not found"):
        prompts.render("nope")
