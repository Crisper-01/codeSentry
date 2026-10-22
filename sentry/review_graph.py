"""LangGraph orchestrator for a full pull request review.

    fetch_context -> checkout_pr -> build_and_test -+-> post_feedback           (build/test KO)
                                                    |
                                                    +-> validate_intent
                                                          -> generate_release_note
                                                             -+-> post_feedback (coverage OK)
                                                              +-> test_generator -> post_feedback

The same entry point serves the shell (`review <owner/repo> <pr>`) and the
webhook, so a manual review and an automatic one go through identical steps.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

import llm
import prompts
from container_manager import (
    CommandResult,
    ContainerManagerError,
    Repository,
    Sandbox,
    list_tracked_files,
    pr_workspace,
    publish_branch,
    repo_lock,
    run_build,
    run_tests,
    write_files,
)
from github_handler import (
    GitHubHandlerError,
    Issue,
    PullRequest,
    can_manage_repo,
    create_pull_request,
    find_linked_issue_number,
    get_issue,
    get_pull_request,
    get_pull_request_diff,
    get_pull_request_files,
    post_pr_comment,
)
from repo_config import RepoConfig, RepoConfigError, load_repo_config

logger = logging.getLogger(__name__)

_MAX_DIFF_CHARS = 120_000
_MAX_LISTED_FILES = 400
_MAX_LOG_CHARS = 4000
_TEST_PATH_PREFIX = "tests/"
_TEST_SUFFIXES = (".rs", ".cpp", ".cc", ".cxx", ".hpp", ".h")


class ReviewCancelled(Exception):
    """Raised when a run is cancelled from the shell between two nodes."""


# --------------------------------------------------------------------------
# Structured model outputs
# --------------------------------------------------------------------------


class IntentVerdict(BaseModel):
    verdict: Literal["aligned", "partially_aligned", "misaligned", "no_issue_linked"]
    confidence: Literal["low", "medium", "high"]
    justification: str
    unmet_requirements: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)


class ReleaseNotes(BaseModel):
    summary: str
    impact: Literal["patch", "minor", "major"]
    highlights: list[str] = Field(default_factory=list)
    breaking_changes: list[str] = Field(default_factory=list)


class GeneratedTestFile(BaseModel):
    path: str
    content: str
    rationale: str


class TestPlan(BaseModel):
    coverage_sufficient: bool
    reasoning: str
    files: list[GeneratedTestFile] = Field(default_factory=list)
    integration_notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Run bookkeeping
# --------------------------------------------------------------------------


@dataclass
class RunRecord:
    """What the shell's `status` and `runs` commands report."""

    run_id: str
    repo: str
    pr_number: int
    trigger: str
    started_at: datetime
    status: str = "running"
    head_sha: str = ""
    build_exit: int | None = None
    test_exit: int | None = None
    intent_verdict: str | None = None
    generated_tests: tuple[str, ...] = ()
    comment_url: str | None = None
    followup_pr_url: str | None = None
    errors: list[str] = field(default_factory=list)
    finished_at: datetime | None = None
    cancel: threading.Event = field(default_factory=threading.Event)


_runs: dict[str, RunRecord] = {}
_runs_guard = threading.Lock()
_last_run_id: str | None = None


def _register_run(record: RunRecord) -> None:
    global _last_run_id
    with _runs_guard:
        _runs[record.run_id] = record
        _last_run_id = record.run_id


def get_run(run_id: str) -> RunRecord | None:
    with _runs_guard:
        return _runs.get(run_id)


def last_run() -> RunRecord | None:
    with _runs_guard:
        return _runs.get(_last_run_id) if _last_run_id else None


def recent_runs(limit: int = 10) -> list[RunRecord]:
    with _runs_guard:
        records = sorted(_runs.values(), key=lambda r: r.started_at, reverse=True)
    return records[:limit]


def cancel_run(run_id: str) -> bool:
    """Ask a running review to stop at the next node boundary."""
    record = get_run(run_id)
    if record is None or record.status != "running":
        return False
    record.cancel.set()
    return True


# --------------------------------------------------------------------------
# Graph state
# --------------------------------------------------------------------------


class ReviewState(TypedDict, total=False):
    repository: Repository
    config: RepoConfig
    repo: str
    pr_number: int
    record: RunRecord
    stack: ExitStack
    notify: Callable[[str], None]

    pull_request: PullRequest
    issue: Issue | None
    diff: str
    changed_files: list[str]

    base_branch: str
    head_ref: str
    head_sha: str
    sandbox: Sandbox

    build_result: CommandResult | None
    test_result: CommandResult | None

    intent: IntentVerdict | None
    release_notes: ReleaseNotes | None
    test_plan: TestPlan | None
    generated_tests: dict[str, str]
    rejected_tests: dict[str, str]
    generated_test_result: CommandResult | None
    integration_notes: list[str]

    comment_url: str | None
    followup_pr_url: str | None
    status: str
    errors: list[str]


def _emit(state: ReviewState, message: str) -> None:
    notify = state.get("notify")
    if notify is not None:
        notify(message)
    logger.info("[%s] %s", state.get("record").run_id if state.get("record") else "-", message)


def _check_cancelled(state: ReviewState) -> None:
    record = state.get("record")
    if record is not None and record.cancel.is_set():
        raise ReviewCancelled(f"Run {record.run_id} cancelled")


def _add_error(state: ReviewState, message: str) -> list[str]:
    errors = list(state.get("errors") or [])
    errors.append(message)
    return errors


# --------------------------------------------------------------------------
# Context helpers
# --------------------------------------------------------------------------


def _truncate(text: str, limit: int, label: str) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n[... {label} truncated, {len(text) - limit} more characters ...]"


def _format_issue(issue: Issue | None) -> str:
    if issue is None:
        return "None. No issue is linked to this pull request; review the diff on its own merits."
    labels = ", ".join(issue.labels) if issue.labels else "none"
    return (
        f"#{issue.number} ({issue.state}, labels: {labels}) - {issue.title}\n\n"
        f"{issue.body.strip() or '(no description)'}"
    )


def _format_file_list(paths: list[str]) -> str:
    if not paths:
        return "(none)"
    shown = paths[:_MAX_LISTED_FILES]
    text = "\n".join(f"- {path}" for path in shown)
    if len(paths) > len(shown):
        text += f"\n- [... {len(paths) - len(shown)} more files ...]"
    return text


def _is_test_or_build_path(path: str) -> bool:
    lowered = path.lower()
    return (
        "test" in lowered
        or lowered.endswith("cmakelists.txt")
        or lowered.endswith("cargo.toml")
        or lowered.endswith("makefile")
        or lowered.endswith(".cmake")
    )


def _prioritised_repo_files(paths: list[str]) -> str:
    """Test and build files first, then a sample of the rest, capped for the prompt."""
    priority = [p for p in paths if _is_test_or_build_path(p)]
    rest = [p for p in paths if not _is_test_or_build_path(p)]
    combined = priority[:_MAX_LISTED_FILES] + rest
    return _format_file_list(combined)


def _command_summary(label: str, result: CommandResult | None) -> str:
    if result is None:
        return f"{label}: not run"
    return f"{label}: {'passed' if result.ok else 'failed'} (exit code {result.exit_code})"


def _build_test_summary(state: ReviewState) -> str:
    return "\n".join(
        [
            _command_summary("Build", state.get("build_result")),
            _command_summary("Tests", state.get("test_result")),
        ]
    )


def _prompt_context(state: ReviewState) -> dict[str, str]:
    pull_request = state["pull_request"]
    repository = state["repository"]
    return {
        "repo": repository.name,
        "language": repository.main_language.value,
        "pr_number": str(pull_request.number),
        "author": pull_request.author,
        "base_ref": pull_request.base_ref,
        "head_ref": pull_request.head_ref,
        "title": pull_request.title or "(no title)",
        "body": (pull_request.body or "").strip() or "(no description)",
        "issue": _format_issue(state.get("issue")),
        "files": _format_file_list(state.get("changed_files") or []),
        "diff": _truncate(state.get("diff") or "", _MAX_DIFF_CHARS, "diff"),
    }


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


def node_fetch_context(state: ReviewState) -> dict[str, Any]:
    _check_cancelled(state)
    repo = state["repo"]
    pr_number = state["pr_number"]
    _emit(state, f"Fetching pull request #{pr_number} metadata and diff")

    pull_request = get_pull_request(repo, pr_number)
    diff = get_pull_request_diff(repo, pr_number)
    changed_files = get_pull_request_files(repo, pr_number)

    issue: Issue | None = None
    issue_number = find_linked_issue_number(pull_request)
    if issue_number is not None:
        try:
            issue = get_issue(repo, issue_number)
            _emit(state, f"Linked issue #{issue_number}: {issue.title}")
        except GitHubHandlerError as exc:
            _emit(state, f"Referenced issue #{issue_number} could not be read: {exc}")
    else:
        _emit(state, "No linked issue found; reviewing the diff on its own")

    return {
        "pull_request": pull_request,
        "diff": diff,
        "changed_files": changed_files,
        "issue": issue,
        "base_branch": pull_request.base_ref,
        "head_ref": pull_request.head_ref,
    }


def node_checkout_pr(state: ReviewState) -> dict[str, Any]:
    _check_cancelled(state)
    repository = state["repository"]
    pr_number = state["pr_number"]
    _emit(state, f"Checking out PR #{pr_number} into a throwaway worktree")

    sandbox, head_sha = state["stack"].enter_context(pr_workspace(repository, pr_number))
    state["record"].head_sha = head_sha
    _emit(state, f"Checked out {head_sha[:12]}")
    return {"sandbox": sandbox, "head_sha": head_sha}


def node_build_and_test(state: ReviewState) -> dict[str, Any]:
    _check_cancelled(state)
    repository = state["repository"]
    sandbox = state["sandbox"]
    record = state["record"]

    _emit(state, "Running the build")
    build_result = run_build(repository, sandbox)
    record.build_exit = build_result.exit_code
    _emit(state, _command_summary("Build", build_result))

    if not build_result.ok:
        return {"build_result": build_result, "test_result": None, "status": "build_failed"}

    _check_cancelled(state)
    _emit(state, "Running the test suite")
    test_result = run_tests(repository, sandbox)
    record.test_exit = test_result.exit_code
    _emit(state, _command_summary("Tests", test_result))

    return {
        "build_result": build_result,
        "test_result": test_result,
        "status": "ok" if test_result.ok else "test_failed",
    }


def node_validate_intent(state: ReviewState) -> dict[str, Any]:
    _check_cancelled(state)
    if not llm.is_configured():
        _emit(state, "Skipping intent validation: no LLM API key configured")
        return {"errors": _add_error(state, "Intent validation skipped: no LLM API key configured")}

    _emit(state, "Validating that the diff answers the issue")
    system, user = prompts.render("intent", **_prompt_context(state))
    try:
        verdict = llm.complete_structured(system, user, IntentVerdict)
    except llm.LLMError as exc:
        _emit(state, f"Intent validation failed: {exc}")
        return {"errors": _add_error(state, f"Intent validation failed: {exc}")}

    state["record"].intent_verdict = verdict.verdict
    _emit(state, f"Intent verdict: {verdict.verdict} ({verdict.confidence} confidence)")
    return {"intent": verdict}


def node_generate_release_note(state: ReviewState) -> dict[str, Any]:
    _check_cancelled(state)
    if not llm.is_configured():
        return {"errors": _add_error(state, "Release notes skipped: no LLM API key configured")}

    _emit(state, "Writing the release notes")
    context = _prompt_context(state)
    context["build_test_summary"] = _build_test_summary(state)
    system, user = prompts.render("release_notes", **context)
    try:
        notes = llm.complete_structured(system, user, ReleaseNotes)
    except llm.LLMError as exc:
        _emit(state, f"Release notes failed: {exc}")
        return {"errors": _add_error(state, f"Release notes failed: {exc}")}

    _emit(state, f"Release notes ready (impact: {notes.impact})")
    return {"release_notes": notes}


def _normalise_path(path: str) -> str:
    """Strip a leading './' without turning an absolute path into a relative one."""
    cleaned = path.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _acceptable_test_path(path: str, existing: set[str]) -> str | None:
    """Return the reason a proposed test path is rejected, or None when it is fine."""
    normalised = _normalise_path(path)
    if not normalised:
        return "empty path"
    if normalised.startswith("/") or ".." in normalised.split("/"):
        return "path escapes the repository"
    if not normalised.startswith(_TEST_PATH_PREFIX):
        return f"not under {_TEST_PATH_PREFIX}"
    if not normalised.endswith(_TEST_SUFFIXES):
        return "not a C++ or Rust source file"
    if normalised in existing:
        return "file already exists at the PR head"
    return None


def node_test_generator(state: ReviewState) -> dict[str, Any]:
    _check_cancelled(state)
    if not llm.is_configured():
        return {"errors": _add_error(state, "Test generation skipped: no LLM API key configured")}

    repository = state["repository"]
    sandbox = state["sandbox"]
    config = state["config"]
    errors = list(state.get("errors") or [])

    _emit(state, "Looking for edge cases the pull request leaves untested")

    try:
        existing = set(list_tracked_files(sandbox))
    except ContainerManagerError as exc:
        errors.append(f"Could not list the repository files: {exc}")
        return {"errors": errors}

    context = _prompt_context(state)
    context["repo_files"] = _prioritised_repo_files(sorted(existing))
    context["test_command"] = config.test_command()
    system, user = prompts.render("test_generation", **context)

    try:
        plan = llm.complete_structured(system, user, TestPlan)
    except llm.LLMError as exc:
        _emit(state, f"Test generation failed: {exc}")
        errors.append(f"Test generation failed: {exc}")
        return {"errors": errors}

    if plan.coverage_sufficient or not plan.files:
        _emit(state, "No extra tests proposed; existing coverage judged sufficient")
        return {"test_plan": plan, "errors": errors}

    accepted: dict[str, str] = {}
    for candidate in plan.files:
        reason = _acceptable_test_path(candidate.path, existing)
        if reason is not None:
            message = f"Discarded generated file {candidate.path!r}: {reason}"
            _emit(state, message)
            errors.append(message)
            continue
        accepted[_normalise_path(candidate.path)] = candidate.content

    if not accepted:
        return {"test_plan": plan, "errors": errors}

    _emit(state, f"Writing {len(accepted)} generated test file(s) and re-running the suite")
    try:
        write_files(sandbox, accepted)
    except ContainerManagerError as exc:
        errors.append(f"Could not write the generated tests: {exc}")
        return {"test_plan": plan, "errors": errors}

    _check_cancelled(state)
    generated_test_result = run_tests(repository, sandbox)
    _emit(state, _command_summary("Generated tests", generated_test_result))

    if not generated_test_result.ok:
        errors.append(
            "The generated tests did not pass locally, so they are reported but not submitted."
        )
        # Kept out of `generated_tests` so nothing is pushed, but kept around so
        # the comment can show what failed: a red generated test either found a
        # defect or is itself wrong, and only a human can tell which.
        return {
            "test_plan": plan,
            "generated_tests": {},
            "rejected_tests": accepted,
            "generated_test_result": generated_test_result,
            "integration_notes": list(plan.integration_notes),
            "errors": errors,
        }

    state["record"].generated_tests = tuple(accepted)
    return {
        "test_plan": plan,
        "generated_tests": accepted,
        "generated_test_result": generated_test_result,
        "integration_notes": list(plan.integration_notes),
        "errors": errors,
    }


def _open_followup_pr(state: ReviewState) -> dict[str, Any]:
    """Push the validated generated tests and open the follow-up pull request."""
    pull_request = state["pull_request"]
    config = state["config"]
    generated = state.get("generated_tests") or {}
    errors = list(state.get("errors") or [])

    if not generated:
        return {"errors": errors}
    if not config.auto_test_pr:
        _emit(state, "auto_test_pr is off: the generated tests are only suggested in the comment")
        return {"errors": errors}

    target_repo = pull_request.head_repo
    try:
        permissions = can_manage_repo(target_repo)
    except GitHubHandlerError as exc:
        errors.append(f"Could not read permissions on {target_repo}: {exc}")
        return {"errors": errors}

    if not permissions.can_create_pr:
        message = (
            f"No push permission on {target_repo}: the generated tests are suggested in the "
            "comment instead of being submitted as a pull request."
        )
        _emit(state, message)
        errors.append(message)
        return {"errors": errors}

    branch = f"codesentry/tests-{pull_request.number}"
    _emit(state, f"Pushing {branch} to {target_repo}")
    try:
        publish_branch(
            state["sandbox"],
            target_repo=target_repo,
            branch=branch,
            commit_message=(
                f"test: cover edge cases of #{pull_request.number}\n\n"
                "Generated by CodeSentry and validated against the repository test suite."
            ),
            paths=sorted(generated),
        )
        url = create_pull_request(
            target_repo,
            head=branch,
            base=pull_request.head_ref,
            title=f"Add tests for #{pull_request.number}: {pull_request.title}"[:250],
            body=_render_followup_body(state),
        )
    except (ContainerManagerError, GitHubHandlerError) as exc:
        message = f"Could not open the follow-up test pull request: {exc}"
        _emit(state, message)
        errors.append(message)
        return {"errors": errors}

    state["record"].followup_pr_url = url
    _emit(state, f"Follow-up test pull request opened: {url}")
    return {"followup_pr_url": url, "errors": errors}


def node_post_feedback(state: ReviewState) -> dict[str, Any]:
    repo = state["repo"]
    pull_request = state.get("pull_request")
    record = state["record"]

    updates: dict[str, Any] = {}
    if state.get("generated_tests"):
        updates.update(_open_followup_pr(state))
        state = {**state, **updates}  # the comment must mention the follow-up PR

    body = render_comment(state)
    if pull_request is None:
        return {**updates, "status": state.get("status", "error")}

    try:
        permissions = can_manage_repo(repo)
    except GitHubHandlerError as exc:
        errors = _add_error(state, f"Could not read permissions on {repo}: {exc}")
        return {**updates, "errors": errors}

    if not permissions.can_modify_pr:
        message = f"No permission to comment on {repo}; the report was not posted."
        _emit(state, message)
        return {**updates, "errors": _add_error(state, message)}

    try:
        comment_url = post_pr_comment(repo, pull_request.number, body)
    except GitHubHandlerError as exc:
        message = f"Could not post the review comment: {exc}"
        _emit(state, message)
        return {**updates, "errors": _add_error(state, message)}

    record.comment_url = comment_url
    _emit(state, f"Review comment posted: {comment_url}")
    return {**updates, "comment_url": comment_url}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _result_cell(result: CommandResult | None) -> str:
    if result is None:
        return "not run"
    return f"{'passed' if result.ok else 'failed'} (exit `{result.exit_code}`)"


def _intent_cell(intent: IntentVerdict | None) -> str:
    if intent is None:
        return "not evaluated"
    labels = {
        "aligned": "matches the issue",
        "partially_aligned": "partially matches the issue",
        "misaligned": "does not match the issue",
        "no_issue_linked": "no issue linked",
    }
    return f"{labels[intent.verdict]} ({intent.confidence} confidence)"


def _intent_body(intent: IntentVerdict | None) -> str:
    if intent is None:
        return "_Not evaluated for this run._"
    parts = [intent.justification.strip()]
    if intent.unmet_requirements:
        parts.append(
            "**Requirements not met**\n"
            + "\n".join(f"- {item}" for item in intent.unmet_requirements)
        )
    if intent.concerns:
        parts.append("**Concerns**\n" + "\n".join(f"- {item}" for item in intent.concerns))
    return "\n\n".join(parts)


def _release_notes_body(notes: ReleaseNotes | None) -> str:
    if notes is None:
        return "_Not generated for this run._"
    parts = [f"**Impact: {notes.impact}**", notes.summary.strip()]
    if notes.highlights:
        parts.append("\n".join(f"- {item}" for item in notes.highlights))
    if notes.breaking_changes:
        parts.append(
            "**Breaking changes**\n"
            + "\n".join(f"- {item}" for item in notes.breaking_changes)
        )
    return "\n\n".join(parts)


def _failing_tests_body(
    plan: TestPlan, rejected: dict[str, str], result: CommandResult
) -> str:
    """Show a red generated test in full: it is either a real defect or a bad test."""
    rationales = {_normalise_path(file.path): file.rationale for file in plan.files}
    lines = [
        "Tests were written for the edge cases this pull request leaves uncovered, and "
        "**they fail against this branch**. Either the change has a defect they caught, "
        "or the generated test is wrong - they are reported here rather than submitted "
        "so you can decide.",
        "",
    ]
    for path in sorted(rejected):
        rationale = rationales.get(path, "").strip()
        lines.append(f"- `{path}`" + (f" - {rationale}" if rationale else ""))
    lines.append("")

    for path in sorted(rejected):
        language = "rust" if path.endswith(".rs") else "cpp"
        lines.append(
            f"<details><summary>{path}</summary>\n\n"
            f"```{language}\n{_truncate(rejected[path], _MAX_LOG_CHARS, 'file')}\n```\n\n"
            "</details>"
        )

    output = (result.stdout + "\n" + result.stderr).strip()
    lines.append(
        f"<details><summary>Failure output (exit {result.exit_code})</summary>\n\n"
        f"```\n{_truncate(output, _MAX_LOG_CHARS, 'output')}\n```\n\n</details>"
    )
    return "\n".join(lines)


def _tests_body(state: ReviewState) -> str:
    plan = state.get("test_plan")
    generated = state.get("generated_tests") or {}
    followup = state.get("followup_pr_url")

    if plan is None:
        return "_Not evaluated for this run._"
    if plan.coverage_sufficient and not generated:
        return f"No additional tests needed. {plan.reasoning.strip()}"
    if not generated:
        rejected = state.get("rejected_tests") or {}
        result = state.get("generated_test_result")
        if rejected and result is not None and not result.ok:
            return _failing_tests_body(plan, rejected, result)
        return f"No additional tests submitted. {plan.reasoning.strip()}"

    count = len(generated)
    lines = [
        f"{count} test file was generated and passes against this branch:"
        if count == 1
        else f"{count} test files were generated and pass against this branch:",
        "",
    ]
    rationales = {_normalise_path(file.path): file.rationale for file in plan.files}
    for path in sorted(generated):
        rationale = rationales.get(path, "").strip()
        lines.append(f"- `{path}`" + (f" - {rationale}" if rationale else ""))

    notes = state.get("integration_notes") or []
    if notes:
        lines.extend(["", "To wire them into the build:", ""])
        lines.extend(f"- {note}" for note in notes)

    if followup:
        lines.extend(["", f"Submitted as {followup}"])
    else:
        lines.extend(
            ["", "_Not submitted as a pull request; paste the files in if you want them._"]
        )
    return "\n".join(lines)


def _logs_section(state: ReviewState) -> str:
    """Only attach logs when something failed - a green run does not need them."""
    blocks: list[str] = []
    for label, result in (
        ("Build", state.get("build_result")),
        ("Tests", state.get("test_result")),
    ):
        if result is None or result.ok:
            continue
        output = (result.stdout + "\n" + result.stderr).strip()
        blocks.append(
            f"<details><summary>{label} output (exit {result.exit_code})</summary>\n\n"
            f"```\n{_truncate(output, _MAX_LOG_CHARS, 'output')}\n```\n\n</details>"
        )

    errors = state.get("errors") or []
    if errors:
        blocks.append("**Notes**\n" + "\n".join(f"- {item}" for item in errors))

    return "\n\n".join(blocks)


def render_comment(state: ReviewState) -> str:
    """Compose the markdown comment posted on the reviewed pull request."""
    record = state.get("record")
    _, body = prompts.render(
        "feedback_comment",
        head_sha=state.get("head_sha", "unknown"),
        llm_label=llm.describe() if llm.is_configured() else "no LLM configured",
        build_line=_result_cell(state.get("build_result")),
        test_line=_result_cell(state.get("test_result")),
        intent_line=_intent_cell(state.get("intent")),
        intent_body=_intent_body(state.get("intent")),
        release_notes=_release_notes_body(state.get("release_notes")),
        tests_section=_tests_body(state),
        logs_section=_logs_section(state),
        run_id=record.run_id if record else "unknown",
    )
    return _collapse_blank_runs(body).strip() + "\n"


def _collapse_blank_runs(text: str) -> str:
    """Squash the blank-line runs left by empty optional sections.

    Fenced blocks are left untouched: they hold captured build and test output,
    and reflowing a log would misreport what the command actually printed.
    """
    parts = text.split("```")
    for index in range(0, len(parts), 2):  # even indexes are outside a fence
        parts[index] = re.sub(r"\n{3,}", "\n\n", parts[index])
    return "```".join(parts)


def _render_followup_body(state: ReviewState) -> str:
    pull_request = state["pull_request"]
    plan = state.get("test_plan")
    lines = [
        f"Tests generated by CodeSentry for #{pull_request.number}.",
        "",
        "They were written against the head of that pull request and pass its test "
        "suite in the CodeSentry sandbox.",
        "",
    ]
    if plan is not None:
        for file in plan.files:
            path = _normalise_path(file.path)
            if path in (state.get("generated_tests") or {}):
                lines.append(f"- `{path}` - {file.rationale}")
        if plan.integration_notes:
            lines.extend(["", "Build wiring still needed:", ""])
            lines.extend(f"- {note}" for note in plan.integration_notes)
    lines.extend(["", f"Review run `{state['record'].run_id}`."])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


def _route_after_build(state: ReviewState) -> str:
    """A red build or a red suite goes straight to the report: nothing else is trustworthy."""
    build_result = state.get("build_result")
    test_result = state.get("test_result")
    if build_result is None or not build_result.ok:
        return "post_feedback"
    if test_result is None or not test_result.ok:
        return "post_feedback"
    return "validate_intent"


def _route_after_release_notes(state: ReviewState) -> str:
    """Skip generation when the pull request already tests what it changed."""
    intent = state.get("intent")
    changed = state.get("changed_files") or []
    touches_tests = any(_is_test_or_build_path(path) for path in changed)
    if touches_tests and intent is not None and intent.verdict == "aligned" and not intent.concerns:
        return "post_feedback"
    return "test_generator"


def build_graph():
    """Compile the review graph. Kept separate so tests can exercise it directly."""
    graph = StateGraph(ReviewState)
    graph.add_node("fetch_context", node_fetch_context)
    graph.add_node("checkout_pr", node_checkout_pr)
    graph.add_node("build_and_test", node_build_and_test)
    graph.add_node("validate_intent", node_validate_intent)
    graph.add_node("generate_release_note", node_generate_release_note)
    graph.add_node("test_generator", node_test_generator)
    graph.add_node("post_feedback", node_post_feedback)

    graph.add_edge(START, "fetch_context")
    graph.add_edge("fetch_context", "checkout_pr")
    graph.add_edge("checkout_pr", "build_and_test")
    graph.add_conditional_edges(
        "build_and_test",
        _route_after_build,
        {"post_feedback": "post_feedback", "validate_intent": "validate_intent"},
    )
    graph.add_edge("validate_intent", "generate_release_note")
    graph.add_conditional_edges(
        "generate_release_note",
        _route_after_release_notes,
        {"post_feedback": "post_feedback", "test_generator": "test_generator"},
    )
    graph.add_edge("test_generator", "post_feedback")
    graph.add_edge("post_feedback", END)
    return graph.compile()


_compiled_graph = None


def _graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    return _compiled_graph


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def review_pull_request(
    repository: Repository,
    pr_number: int,
    *,
    trigger: str = "shell",
    notify: Callable[[str], None] | None = None,
) -> RunRecord:
    """Run the full review for one pull request. Used by the shell and the webhook."""
    record = RunRecord(
        run_id=uuid.uuid4().hex[:8],
        repo=repository.name,
        pr_number=pr_number,
        trigger=trigger,
        started_at=datetime.now(timezone.utc),
    )
    _register_run(record)

    if not repository.config_path:
        record.status = "error"
        record.errors.append("No config file bound for this repository")
        record.finished_at = datetime.now(timezone.utc)
        return record

    try:
        config = load_repo_config(repository.config_path)
    except RepoConfigError as exc:
        record.status = "error"
        record.errors.append(str(exc))
        record.finished_at = datetime.now(timezone.utc)
        return record

    initial: ReviewState = {
        "repository": repository,
        "config": config,
        "repo": repository.name,
        "pr_number": pr_number,
        "record": record,
        "notify": notify,
        "errors": [],
        "generated_tests": {},
    }

    # One review at a time per repository: runs share the repository's clone,
    # which is where PR refs are fetched and worktrees are attached.
    with repo_lock(repository.name):
        with ExitStack() as stack:
            initial["stack"] = stack
            try:
                final = _graph().invoke(initial)
            except ReviewCancelled:
                record.status = "cancelled"
                record.errors.append("Cancelled from the shell")
                record.finished_at = datetime.now(timezone.utc)
                return record
            except (ContainerManagerError, GitHubHandlerError, prompts.PromptError) as exc:
                record.status = "error"
                record.errors.append(str(exc))
                record.finished_at = datetime.now(timezone.utc)
                logger.exception("Review run %s failed", record.run_id)
                return record

    record.status = final.get("status") or "ok"
    record.errors.extend(final.get("errors") or [])
    record.finished_at = datetime.now(timezone.utc)
    return record
