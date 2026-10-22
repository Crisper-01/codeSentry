"""End-to-end coverage of the review graph with Docker, GitHub and the LLM faked out.

The four scenarios named in the plan are covered here:
  A - build OK, tests OK, issue aligned -> positive comment with release notes
  B - build or tests KO                  -> feedback with logs, no generated tests
  C - missing tests + auto_test_pr       -> follow-up pull request opened
  D - branch not watched                 -> lives in test_webhook.py
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import failed, fake_workspace, ok

import review_graph
from container_manager import CommandResult
from github_handler import RepoPermissions
from review_graph import (
    GeneratedTestFile,
    IntentVerdict,
    ReleaseNotes,
    TestPlan,
    _acceptable_test_path,
    _route_after_build,
    _route_after_release_notes,
)

# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


def test_a_failed_build_short_circuits_to_the_report():
    state = {"build_result": failed("no rustc"), "test_result": None}
    assert _route_after_build(state) == "post_feedback"


def test_failed_tests_short_circuit_to_the_report():
    state = {"build_result": ok(), "test_result": failed("assertion failed")}
    assert _route_after_build(state) == "post_feedback"


def test_a_green_run_continues_to_intent_validation():
    assert _route_after_build({"build_result": ok(), "test_result": ok()}) == "validate_intent"


def test_generation_is_skipped_when_the_pr_tests_its_own_change():
    state = {
        "changed_files": ["src/ring.rs", "tests/ring_wrap.rs"],
        "intent": IntentVerdict(
            verdict="aligned", confidence="high", justification="ok", concerns=[]
        ),
    }
    assert _route_after_release_notes(state) == "post_feedback"


def test_generation_runs_when_the_pr_ships_no_tests():
    state = {
        "changed_files": ["src/ring.rs"],
        "intent": IntentVerdict(
            verdict="aligned", confidence="high", justification="ok", concerns=[]
        ),
    }
    assert _route_after_release_notes(state) == "test_generator"


def test_generation_runs_when_concerns_were_raised_despite_tests():
    state = {
        "changed_files": ["src/ring.rs", "tests/ring.rs"],
        "intent": IntentVerdict(
            verdict="aligned",
            confidence="high",
            justification="ok",
            concerns=["the empty-buffer path is untested"],
        ),
    }
    assert _route_after_release_notes(state) == "test_generator"


# --------------------------------------------------------------------------
# Generated-path guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("src/ring.rs", "not under tests/"),
        ("tests/ring.py", "not a C++ or Rust source file"),
        ("/etc/passwd", "path escapes the repository"),
        ("tests/../../escape.rs", "path escapes the repository"),
        ("tests/existing.rs", "file already exists at the PR head"),
        ("", "empty path"),
    ],
)
def test_unsafe_generated_paths_are_rejected(path, reason):
    assert _acceptable_test_path(path, {"tests/existing.rs"}) == reason


def test_a_new_test_file_is_accepted():
    assert _acceptable_test_path("tests/codesentry_wrap.rs", {"tests/existing.rs"}) is None


def test_a_leading_dot_slash_is_tolerated():
    assert _acceptable_test_path("./tests/codesentry_wrap.cpp", set()) is None


# --------------------------------------------------------------------------
# Graph runs
# --------------------------------------------------------------------------


@pytest.fixture
def wired(monkeypatch, pull_request, issue, repo_config):
    """Wire every external dependency of the graph to an in-memory fake."""
    calls: dict[str, object] = {"comments": [], "pulls": [], "pushed": [], "written": {}}

    monkeypatch.setattr(review_graph, "get_pull_request", lambda repo, n: pull_request)
    monkeypatch.setattr(
        review_graph, "get_pull_request_diff", lambda repo, n: "@@ -1 +1 @@\n-old\n+new"
    )
    monkeypatch.setattr(review_graph, "get_pull_request_files", lambda repo, n: ["src/ring.rs"])
    monkeypatch.setattr(review_graph, "get_issue", lambda repo, n: issue)
    monkeypatch.setattr(review_graph, "pr_workspace", fake_workspace)
    monkeypatch.setattr(review_graph, "load_repo_config", lambda path: repo_config)
    monkeypatch.setattr(review_graph, "list_tracked_files", lambda sandbox: ["src/ring.rs"])
    monkeypatch.setattr(
        review_graph,
        "can_manage_repo",
        lambda repo: RepoPermissions(can_clone=True, can_create_pr=True, can_modify_pr=True),
    )

    def post_comment(repo, number, body):
        calls["comments"].append((repo, number, body))
        return "https://github.com/acme/engine/pull/42#issuecomment-1"

    def create_pr(repo, *, head, base, title, body):
        calls["pulls"].append({"repo": repo, "head": head, "base": base, "title": title})
        return "https://github.com/acme/engine/pull/43"

    def publish(sandbox, *, target_repo, branch, commit_message, paths):
        calls["pushed"].append({"repo": target_repo, "branch": branch, "paths": list(paths)})

    def write(sandbox, files):
        calls["written"].update(files)

    monkeypatch.setattr(review_graph, "post_pr_comment", post_comment)
    monkeypatch.setattr(review_graph, "create_pull_request", create_pr)
    monkeypatch.setattr(review_graph, "publish_branch", publish)
    monkeypatch.setattr(review_graph, "write_files", write)
    return calls


def set_llm(monkeypatch, *, intent=None, notes=None, plan=None):
    monkeypatch.setattr(review_graph.llm, "is_configured", lambda: True)
    monkeypatch.setattr(review_graph.llm, "describe", lambda: "anthropic/claude-opus-5")

    answers = {
        IntentVerdict: intent
        or IntentVerdict(
            verdict="aligned",
            confidence="high",
            justification="The diff fixes the wrap-around described in the issue.",
        ),
        ReleaseNotes: notes
        or ReleaseNotes(
            summary="Fixed the ring buffer wrap-around.",
            impact="patch",
            highlights=["Fixed the off-by-one in `src/ring.rs`"],
        ),
        TestPlan: plan or TestPlan(coverage_sufficient=True, reasoning="Already covered."),
    }

    def complete(system, user, schema):
        return answers[schema]

    monkeypatch.setattr(review_graph.llm, "complete_structured", complete)


def test_scenario_a_green_run_posts_release_notes(monkeypatch, repository, wired):
    monkeypatch.setattr(review_graph, "run_build", lambda repo, sandbox=None: ok("Compiling"))
    monkeypatch.setattr(review_graph, "run_tests", lambda repo, sandbox=None: ok("2 passed"))
    set_llm(monkeypatch)

    record = review_graph.review_pull_request(repository, 42)

    assert record.status == "ok"
    assert record.build_exit == 0
    assert record.test_exit == 0
    assert record.intent_verdict == "aligned"
    assert record.head_sha == "b" * 40
    assert record.comment_url.endswith("issuecomment-1")

    body = wired["comments"][0][2]
    assert "## CodeSentry review" in body
    assert "Fixed the ring buffer wrap-around." in body
    assert "matches the issue" in body
    assert "<details>" not in body  # a green run attaches no logs


def test_scenario_b_a_failed_build_reports_logs_and_stops(monkeypatch, repository, wired):
    monkeypatch.setattr(
        review_graph,
        "run_build",
        lambda repo, sandbox=None: failed("error[E0308]: mismatched types", exit_code=101),
    )
    monkeypatch.setattr(
        review_graph,
        "run_tests",
        lambda repo, sandbox=None: pytest.fail("tests must not run after a failed build"),
    )
    set_llm(monkeypatch)

    record = review_graph.review_pull_request(repository, 42)

    assert record.status == "build_failed"
    assert record.build_exit == 101
    assert record.test_exit is None
    assert record.intent_verdict is None
    assert record.followup_pr_url is None

    body = wired["comments"][0][2]
    assert "error[E0308]" in body
    assert "Build output (exit 101)" in body
    assert "_Not generated for this run._" in body
    assert wired["pulls"] == []


def test_scenario_b_failed_tests_also_stop_the_run(monkeypatch, repository, wired):
    monkeypatch.setattr(review_graph, "run_build", lambda repo, sandbox=None: ok())
    monkeypatch.setattr(
        review_graph, "run_tests", lambda repo, sandbox=None: failed("1 failed", exit_code=101)
    )
    set_llm(monkeypatch)

    record = review_graph.review_pull_request(repository, 42)

    assert record.status == "test_failed"
    assert record.intent_verdict is None
    assert wired["pulls"] == []


def test_scenario_c_generated_tests_open_a_follow_up_pr(
    monkeypatch, repository, wired, repo_config
):
    monkeypatch.setattr(review_graph, "load_repo_config", lambda path: replace(repo_config, auto_test_pr=True))
    monkeypatch.setattr(review_graph, "run_build", lambda repo, sandbox=None: ok())
    monkeypatch.setattr(review_graph, "run_tests", lambda repo, sandbox=None: ok("3 passed"))
    set_llm(
        monkeypatch,
        plan=TestPlan(
            coverage_sufficient=False,
            reasoning="The wrap boundary is untested.",
            files=[
                GeneratedTestFile(
                    path="tests/codesentry_wrap.rs",
                    content="#[test]\nfn wraps() { assert!(true); }\n",
                    rationale="Covers reading across the wrap boundary",
                )
            ],
        ),
    )

    record = review_graph.review_pull_request(repository, 42)

    assert record.status == "ok"
    assert record.generated_tests == ("tests/codesentry_wrap.rs",)
    assert record.followup_pr_url == "https://github.com/acme/engine/pull/43"
    assert wired["written"] == {
        "tests/codesentry_wrap.rs": "#[test]\nfn wraps() { assert!(true); }\n"
    }
    assert wired["pushed"][0]["branch"] == "codesentry/tests-42"
    assert wired["pushed"][0]["repo"] == "acme/engine"
    assert wired["pulls"][0]["base"] == "fix/ring-buffer"

    body = wired["comments"][0][2]
    assert "tests/codesentry_wrap.rs" in body
    assert "https://github.com/acme/engine/pull/43" in body


def test_generated_tests_are_only_suggested_when_auto_test_pr_is_off(
    monkeypatch, repository, wired
):
    monkeypatch.setattr(review_graph, "run_build", lambda repo, sandbox=None: ok())
    monkeypatch.setattr(review_graph, "run_tests", lambda repo, sandbox=None: ok())
    set_llm(
        monkeypatch,
        plan=TestPlan(
            coverage_sufficient=False,
            reasoning="Boundary untested.",
            files=[
                GeneratedTestFile(
                    path="tests/codesentry_wrap.rs", content="fn t() {}", rationale="boundary"
                )
            ],
        ),
    )

    record = review_graph.review_pull_request(repository, 42)

    assert record.followup_pr_url is None
    assert wired["pulls"] == []
    body = wired["comments"][0][2]
    assert "tests/codesentry_wrap.rs" in body
    assert "Not submitted as a pull request" in body


def test_generated_tests_that_fail_are_not_submitted(monkeypatch, repository, wired, repo_config):
    monkeypatch.setattr(
        review_graph, "load_repo_config", lambda path: replace(repo_config, auto_test_pr=True)
    )
    monkeypatch.setattr(review_graph, "run_build", lambda repo, sandbox=None: ok())

    results = iter([ok("3 passed"), failed("1 failed", exit_code=101)])
    monkeypatch.setattr(review_graph, "run_tests", lambda repo, sandbox=None: next(results))
    set_llm(
        monkeypatch,
        plan=TestPlan(
            coverage_sufficient=False,
            reasoning="Boundary untested.",
            files=[
                GeneratedTestFile(
                    path="tests/codesentry_wrap.rs", content="fn broken() {}", rationale="boundary"
                )
            ],
        ),
    )

    record = review_graph.review_pull_request(repository, 42)

    assert record.followup_pr_url is None
    assert record.generated_tests == ()
    assert wired["pulls"] == []
    assert any("did not pass locally" in error for error in record.errors)

    # The failing test is shown in full so a reviewer can tell a real defect
    # from a bad generated test.
    body = wired["comments"][0][2]
    assert "they fail against this branch" in body
    assert "fn broken()" in body
    assert "Failure output (exit 101)" in body


def test_a_generated_path_outside_tests_is_discarded(monkeypatch, repository, wired, repo_config):
    monkeypatch.setattr(
        review_graph, "load_repo_config", lambda path: replace(repo_config, auto_test_pr=True)
    )
    monkeypatch.setattr(review_graph, "run_build", lambda repo, sandbox=None: ok())
    monkeypatch.setattr(review_graph, "run_tests", lambda repo, sandbox=None: ok())
    set_llm(
        monkeypatch,
        plan=TestPlan(
            coverage_sufficient=False,
            reasoning="Boundary untested.",
            files=[
                GeneratedTestFile(
                    path="src/ring.rs", content="// clobbered", rationale="overwrite the source"
                )
            ],
        ),
    )

    record = review_graph.review_pull_request(repository, 42)

    assert wired["written"] == {}
    assert wired["pulls"] == []
    assert any("Discarded generated file" in error for error in record.errors)


def test_the_run_still_reports_build_and_tests_without_an_llm(monkeypatch, repository, wired):
    monkeypatch.setattr(review_graph.llm, "is_configured", lambda: False)
    monkeypatch.setattr(review_graph, "run_build", lambda repo, sandbox=None: ok())
    monkeypatch.setattr(review_graph, "run_tests", lambda repo, sandbox=None: ok())

    record = review_graph.review_pull_request(repository, 42)

    assert record.status == "ok"
    assert record.intent_verdict is None
    body = wired["comments"][0][2]
    assert "no LLM configured" in body
    assert "_Not evaluated for this run._" in body


def test_a_run_without_comment_permission_is_reported(monkeypatch, repository, wired):
    monkeypatch.setattr(
        review_graph,
        "can_manage_repo",
        lambda repo: RepoPermissions(can_clone=True, can_create_pr=False, can_modify_pr=False),
    )
    monkeypatch.setattr(review_graph, "run_build", lambda repo, sandbox=None: ok())
    monkeypatch.setattr(review_graph, "run_tests", lambda repo, sandbox=None: ok())
    set_llm(monkeypatch)

    record = review_graph.review_pull_request(repository, 42)

    assert wired["comments"] == []
    assert any("No permission to comment" in error for error in record.errors)


def test_a_repository_without_a_config_fails_fast(repository):
    record = review_graph.review_pull_request(replace(repository, config_path=None), 42)
    assert record.status == "error"
    assert "No config file bound" in record.errors[0]


def test_a_cancelled_run_stops_at_the_next_node(monkeypatch, repository, wired):
    monkeypatch.setattr(review_graph, "run_build", lambda repo, sandbox=None: ok())
    monkeypatch.setattr(review_graph, "run_tests", lambda repo, sandbox=None: ok())
    set_llm(monkeypatch)

    original = review_graph.node_fetch_context

    def cancel_then_fetch(state):
        state["record"].cancel.set()
        return original(state)

    monkeypatch.setattr(review_graph, "node_fetch_context", cancel_then_fetch)
    review_graph._compiled_graph = None  # rebuild against the patched node

    record = review_graph.review_pull_request(repository, 42)

    review_graph._compiled_graph = None
    assert record.status == "cancelled"
    assert wired["comments"] == []


def test_runs_are_recorded_and_listed(monkeypatch, repository, wired):
    monkeypatch.setattr(review_graph, "run_build", lambda repo, sandbox=None: ok())
    monkeypatch.setattr(review_graph, "run_tests", lambda repo, sandbox=None: ok())
    set_llm(monkeypatch)

    record = review_graph.review_pull_request(repository, 42)

    assert review_graph.last_run() is record
    assert review_graph.get_run(record.run_id) is record
    assert record in review_graph.recent_runs()
    assert review_graph.cancel_run(record.run_id) is False  # already finished


def test_a_timed_out_command_is_reported_as_a_failure(monkeypatch, repository, wired):
    timeout = CommandResult(stdout="", stderr="exceeded the 1800s timeout", exit_code=124)
    monkeypatch.setattr(review_graph, "run_build", lambda repo, sandbox=None: timeout)
    set_llm(monkeypatch)

    record = review_graph.review_pull_request(repository, 42)

    assert record.status == "build_failed"
    assert record.build_exit == 124
    assert "exceeded the 1800s timeout" in wired["comments"][0][2]


def test_captured_logs_keep_their_blank_lines(monkeypatch, repository, wired):
    """Blank-line squashing must not reflow what a command actually printed."""
    monkeypatch.setattr(
        review_graph,
        "run_build",
        lambda repo, sandbox=None: failed("line one\n\n\n\nline two", exit_code=1),
    )
    set_llm(monkeypatch)

    review_graph.review_pull_request(repository, 42)
    body = wired["comments"][0][2]

    assert "line one\n\n\n\nline two" in body
    # Outside the fence the layout is still tidy.
    assert "\n\n\n### " not in body
