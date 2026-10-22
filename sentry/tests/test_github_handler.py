import pytest

from github_handler import _parse_repo, find_linked_issue_number


def test_parse_repo_accepts_owner_slash_name():
    assert _parse_repo("acme/engine") == ("acme", "engine")


@pytest.mark.parametrize("value", ["engine", "acme/", "/engine", "", "   "])
def test_parse_repo_rejects_malformed_names(value):
    with pytest.raises(ValueError, match="Invalid repository format"):
        _parse_repo(value)


def _pr(title: str = "", body: str = ""):
    from github_handler import PullRequest

    return PullRequest(
        number=1,
        title=title,
        body=body,
        author="someone",
        state="open",
        draft=False,
        base_ref="main",
        head_ref="feature",
        head_sha="a" * 40,
        head_repo="acme/engine",
        html_url="",
    )


@pytest.mark.parametrize(
    "body",
    ["Fixes #7", "fixes #7", "Closes: #7", "resolved #7", "This PR fixes #7 at last"],
)
def test_closing_keywords_are_detected(body):
    assert find_linked_issue_number(_pr(body=body)) == 7


def test_a_bare_reference_is_used_as_a_fallback():
    assert find_linked_issue_number(_pr(body="Related to #12 in passing")) == 12


def test_a_closing_keyword_wins_over_a_bare_reference():
    pr = _pr(title="Refactor after #99", body="Fixes #7")
    assert find_linked_issue_number(pr) == 7


def test_no_reference_returns_none():
    assert find_linked_issue_number(_pr(title="Tidy up", body="No ticket.")) is None


def test_a_hash_inside_a_word_is_not_a_reference():
    assert find_linked_issue_number(_pr(body="colour is #1abc9e")) is None
