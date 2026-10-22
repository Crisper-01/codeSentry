# CodeSentry

Agentic pre-review for C++ and Rust pull requests. Watched repositories are
cloned into language sandboxes; every pull request targeting a configured branch
is built and tested there, then reviewed by an LLM that checks the change
against its issue, writes the release notes, and proposes the tests the change
is missing.

The interactive shell is the main interface. A GitHub webhook drives the same
pipeline automatically.

---

## Install

```bash
cd sentry
uv venv && uv pip install -e ".[dev]"      # or: python -m venv .venv && pip install -e ".[dev]"
python main.py
```

Bring the sandboxes up first, they are where builds and tests run:

```bash
docker compose -f containers/docker-compose.yml up -d cpp-sandbox rust-sandbox
```

Or run everything, CodeSentry included, in Docker:

```bash
docker compose -f containers/docker-compose.yml up --build
docker attach codesentry-sentry      # the shell needs a TTY
```

## Environment

| Variable | Purpose |
| --- | --- |
| `GITHUB_TOKEN` | Clone private repositories, post comments, open the follow-up test PR. |
| `GITHUB_WEBHOOK_SECRET` | Optional. When set, webhook signatures are verified. |
| `SENTRY_LLM_PROVIDER` | `anthropic` (default) or `openai`. |
| `SENTRY_LLM_MODEL` | Overrides the default model (`claude-opus-5` / `gpt-5`). |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Key for the selected provider. |
| `SENTRY_STATE_FILE` | Registry location. Default `~/.codesentry/repos.json`. |
| `SENTRY_COMMAND_TIMEOUT` | Seconds a single build or test command may run. Default `1800`. |
| `SENTRY_WEBHOOK_HOST` / `SENTRY_WEBHOOK_PORT` | Webhook bind address. Default `0.0.0.0:8080`. |

Without an LLM key CodeSentry still clones, builds, tests and reports; the
intent, release-note and test-generation steps are skipped and the comment says
so.

## Shell commands

| Command | What it does |
| --- | --- |
| `add <owner/repo>` | Register a repository. Rejected unless its main language is Rust or C++. |
| `set config <owner/repo> <path>` | Bind and validate its JSON config. |
| `enable <owner/repo>` | Clone it into its sandbox and start watching it. |
| `disable <owner/repo>` | Stop watching and drop the clone. |
| `delete <owner/repo>` | Remove it from the registry. |
| `list` | Registered repositories with their live GitHub permissions. |
| `check build\|test <owner/repo>` | Run one command against the persistent clone. |
| `review <owner/repo> <pr>` | Full agentic review of a pull request, with live per-node progress. |
| `status` | Detail of the last review run. |
| `runs` | The recent review runs. |
| `cancel <run_id>` | Stop a running review at its next node. |

Tab completes commands, registered repositories and config paths. The registry
is written to disk on every change, so enabled repositories survive a restart.

## Repository config

One JSON file per repository, its path given with `set config`:

```json
{
  "watched_branches": ["main", "develop"],
  "build": { "mode": "cargo" },
  "test":  { "mode": "cargo test" },
  "auto_test_pr": true
}
```

* `watched_branches` - a pull request is reviewed only when its **target**
  branch is listed here. These are the branches CodeSentry protects.
* `build.mode` - `cmake`, `gcc`, `cargo`, or `custom`.
* `test.mode` - `cargo test`, `cmake test`, or `custom`.
* `command` - optional; required when the mode is `custom`.
* `auto_test_pr` - when true, generated tests that pass are pushed as a
  follow-up pull request. Default false: they are only suggested in the comment.

Defaults when `command` is omitted:

| Mode | Command |
| --- | --- |
| `cmake` | `cmake -S . -B build && cmake --build build` |
| `gcc` | `make` |
| `cargo` | `cargo build` |
| `cargo test` | `cargo test` |
| `cmake test` | `ctest --test-dir build --output-on-failure` |

Examples live in [`../configs/`](../configs).

## The review pipeline

```
fetch_context -> checkout_pr -> build_and_test -+-> post_feedback              (build or tests red)
                                                |
                                                +-> validate_intent
                                                      -> generate_release_note
                                                         -+-> post_feedback    (coverage sufficient)
                                                          +-> test_generator -> post_feedback
```

* **fetch_context** - pull request metadata, unified diff, changed files, and
  the linked issue (`fixes #12`, else a bare `#12`).
* **checkout_pr** - fetches `pull/<n>/head` and attaches a throwaway git
  worktree under `/workspace/.runs/`. The persistent clone is never mutated, so
  two reviews of the same repository cannot corrupt each other.
* **build_and_test** - the configured commands, in that worktree. A red build
  skips the tests; either failure short-circuits straight to the report.
* **validate_intent** - does the diff deliver what the issue asked for?
* **generate_release_note** - impact level, summary, highlights, breaking changes.
* **test_generator** - proposes tests for the uncovered edge cases, writes them
  in the sandbox and re-runs the suite. Proposed files must be new and under
  `tests/`; anything else is discarded and reported.
* **post_feedback** - one markdown comment on the reviewed pull request.

A generated test that fails is never submitted. It is quoted in full in the
comment with its failure output instead, because a red generated test is either
a real defect or a bad test and only a human can tell which.

## Webhook

`POST /webhook/github`, events `pull_request` `opened` / `synchronize` /
`reopened`. An event is accepted only when the repository is registered and
enabled, a config is bound, the pull request is not a draft, and its target
branch is listed in `watched_branches`. The endpoint answers immediately and
runs the review on a background thread, because a review takes far longer than
GitHub waits. The same `(repo, head_sha)` is never reviewed twice.

Expose it locally with a tunnel:

```bash
smee -u https://smee.io/<channel> --target http://127.0.0.1:8080/webhook/github
cloudflared tunnel --url http://127.0.0.1:8080
```

## Permissions

CodeSentry reads its permissions from GitHub before acting and degrades instead
of failing: no push rights on the head repository means the generated tests are
suggested in the comment rather than submitted, and no write access at all means
the report is rendered but not posted. Both are stated in the run's notes.

## Tests

```bash
cd sentry && python -m pytest
```

117 tests, no Docker, no GitHub and no LLM key required - every external
dependency is faked. They cover the four scenarios from `plan.md`: a green run,
a red build or suite, generated tests opening a follow-up pull request, and a
webhook event on an unwatched branch.

## Demo repository

[`../demo/ring-buffer`](../demo/ring-buffer) is a small Rust crate whose
`drain` has an off-by-one bug on the wrap-around path that its committed tests
do not reach. Build and tests are green, so a review runs end to end and the
test generator has something real to find.
