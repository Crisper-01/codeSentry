"""Shell dispatch: argument validation and routing, without a terminal."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import main
import review_graph
from container_manager import MainLanguage, Repository


@pytest.fixture
def shell(monkeypatch, capsys):
    """Silence the prompt_toolkit output helpers and capture plain prints."""
    monkeypatch.setattr(main, "_print_error", lambda message: print(f"ERROR {message}"))
    monkeypatch.setattr(main, "_print_step", lambda message: print(f"STEP {message}"))
    return capsys


def run(line: str) -> bool:
    return main._dispatch(line)


def test_quit_and_exit_stop_the_loop():
    assert run("quit") is False
    assert run("exit") is False
    assert run("") is True


def test_help_lists_the_review_commands(shell):
    run("help")
    out = shell.readouterr().out
    for command in ("review", "status", "runs", "cancel", "check build"):
        assert command in out


def test_an_unknown_command_is_reported(shell):
    run("frobnicate")
    assert "Commande inconnue" in shell.readouterr().out


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("review", "Usage : review"),
        ("review acme/engine", "Usage : review"),
        ("review acme/engine 1 2", "Usage : review"),
        ("cancel", "Usage : cancel"),
        ("status extra", "Usage : status"),
        ("runs extra", "Usage : runs"),
        ("check build", "Usage : check"),
        ("check deploy acme/engine", "Usage : check"),
        ("set config acme/engine", "Usage : set config"),
        ("add", "Usage : add"),
    ],
)
def test_bad_arguments_print_the_usage(shell, line, expected):
    run(line)
    assert expected in shell.readouterr().out


def test_review_rejects_a_non_numeric_pr(shell, monkeypatch):
    monkeypatch.setattr(main, "get_repo", lambda name: None)
    run("review acme/engine abc")
    assert "numéro de PR invalide" in shell.readouterr().out


def test_review_rejects_a_malformed_repository(shell):
    run("review engine 42")
    assert "Invalid repository format" in shell.readouterr().out


def test_review_requires_a_known_repository(shell, monkeypatch):
    monkeypatch.setattr(main, "get_repo", lambda name: None)
    run("review acme/engine 42")
    assert "introuvable" in shell.readouterr().out


def test_review_requires_an_enabled_repository(shell, monkeypatch):
    repo = Repository(name="acme/engine", enabled=False, main_language=MainLanguage.RUST)
    monkeypatch.setattr(main, "get_repo", lambda name: repo)
    run("review acme/engine 42")
    assert "désactivé" in shell.readouterr().out


def test_review_runs_the_graph_and_prints_the_record(shell, monkeypatch, repository):
    record = review_graph.RunRecord(
        run_id="cafe1234",
        repo="acme/engine",
        pr_number=42,
        trigger="shell",
        started_at=datetime.now(timezone.utc),
        status="ok",
        head_sha="b" * 40,
        build_exit=0,
        test_exit=0,
        intent_verdict="aligned",
        generated_tests=("tests/codesentry_wrap.rs",),
        comment_url="https://github.com/acme/engine/pull/42#issuecomment-1",
        followup_pr_url="https://github.com/acme/engine/pull/43",
    )
    seen: dict[str, object] = {}

    def fake_review(repo, pr_number, *, trigger="shell", notify=None):
        seen["repo"] = repo.name
        seen["pr"] = pr_number
        if notify:
            notify("Running the build")
        return record

    monkeypatch.setattr(main, "get_repo", lambda name: repository)
    monkeypatch.setattr(main.llm, "is_configured", lambda: True)
    monkeypatch.setattr(main.llm, "describe", lambda: "anthropic/claude-opus-5")
    monkeypatch.setattr(main.review_graph, "review_pull_request", fake_review)

    run("review acme/engine 42")
    out = shell.readouterr().out

    assert seen == {"repo": "acme/engine", "pr": 42}
    assert "STEP Running the build" in out
    assert "cafe1234" in out
    assert "aligned" in out
    assert "tests/codesentry_wrap.rs" in out
    assert "https://github.com/acme/engine/pull/43" in out


def test_review_warns_when_no_llm_key_is_set(shell, monkeypatch, repository):
    monkeypatch.setattr(main, "get_repo", lambda name: repository)
    monkeypatch.setattr(main.llm, "is_configured", lambda: False)
    monkeypatch.setattr(
        main.review_graph,
        "review_pull_request",
        lambda repo, pr, **kw: review_graph.RunRecord(
            run_id="x", repo=repo.name, pr_number=pr, trigger="shell",
            started_at=datetime.now(timezone.utc), status="ok",
        ),
    )

    run("review acme/engine 42")
    assert "aucune clé LLM configurée" in shell.readouterr().out


def test_status_without_a_run(shell, monkeypatch):
    monkeypatch.setattr(main.review_graph, "last_run", lambda: None)
    run("status")
    assert "Aucun run de revue" in shell.readouterr().out


def test_runs_lists_recent_runs(shell, monkeypatch):
    records = [
        review_graph.RunRecord(
            run_id="aaa", repo="acme/engine", pr_number=1, trigger="webhook",
            started_at=datetime.now(timezone.utc), status="ok",
        ),
        review_graph.RunRecord(
            run_id="bbb", repo="acme/engine", pr_number=2, trigger="shell",
            started_at=datetime.now(timezone.utc), status="build_failed",
        ),
    ]
    monkeypatch.setattr(main.review_graph, "recent_runs", lambda: records)
    run("runs")
    out = shell.readouterr().out
    assert "aaa" in out and "bbb" in out
    assert "webhook" in out and "build_failed" in out


def test_cancel_reports_an_unknown_run(shell, monkeypatch):
    monkeypatch.setattr(main.review_graph, "cancel_run", lambda run_id: False)
    run("cancel nope")
    assert "aucun run en cours" in shell.readouterr().out


def test_cancel_acknowledges_a_running_run(shell, monkeypatch):
    monkeypatch.setattr(main.review_graph, "cancel_run", lambda run_id: True)
    run("cancel abc123")
    assert "Annulation demandée" in shell.readouterr().out


def test_completion_offers_the_review_commands():
    from prompt_toolkit.document import Document

    completer = main.CodeSentryCompleter()
    words = [
        c.text for c in completer.get_completions(Document("r", 1), None)
    ]
    assert "review" in words
    assert "runs" in words


def test_completion_offers_check_kinds():
    from prompt_toolkit.document import Document

    completer = main.CodeSentryCompleter()
    words = [c.text for c in completer.get_completions(Document("check ", 6), None)]
    assert sorted(words) == ["build", "test"]
