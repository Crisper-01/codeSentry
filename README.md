# CodeSentry

Agentic pre-review for C++ and Rust pull requests.

CodeSentry watches a set of repositories you register. Each repository is
cloned into a per-language Docker sandbox; every pull request targeting one of
the repository's watched branches is built and tested there, then reviewed by
an LLM pipeline (LangGraph) that checks the diff against its linked issue,
writes the release notes, and proposes the tests the change is missing. The
result is posted as a single markdown comment on the pull request, and — when
enabled — passing generated tests are opened as a follow-up pull request.

The same pipeline is driven either by the interactive shell or automatically
by a GitHub webhook on `pull_request` events.

## Repository layout

| Path | Contents |
| --- | --- |
| [`sentry/`](sentry/) | The application: shell, LangGraph review graph, GitHub REST client, container manager, webhook daemon. Full documentation in [sentry/README.md](sentry/README.md). |
| [`configs/`](configs/) | Example repository configs (one JSON per repo). |
| [`containers/`](containers/) | Docker Compose file: `cpp-sandbox`, `rust-sandbox`, `sentry`. |
| [`demo/ring-buffer/`](demo/ring-buffer/) | Small Rust crate with a deliberately uncovered off-by-one bug, for exercising the full flow. |
| `plan.md` | Project plan and delivery log, phases 1–5 (in French). |

## Quickstart

1. Start the language sandboxes — builds and tests run inside them:

   ```bash
   docker compose -f containers/docker-compose.yml up -d cpp-sandbox rust-sandbox
   ```

2. Install and run the shell:

   ```bash
   cd sentry
   uv venv && uv pip install -e ".[dev]"
   python main.py
   ```

3. Register, configure and enable a repository (Rust or C++ only):

   ```
   add owner/ring-buffer
   set config owner/ring-buffer ../configs/cargo-main-auto-tests.json
   enable owner/ring-buffer
   ```

4. Review a pull request, with live per-node progress:

   ```
   review owner/ring-buffer <pr_number>
   ```

Or run the whole stack, CodeSentry included, in Docker:

```bash
docker compose -f containers/docker-compose.yml up --build
docker attach codesentry-sentry
```

## How a review runs

```
fetch_context -> checkout_pr -> build_and_test -+-> post_feedback          (build or tests red)
                                                |
                                                +-> validate_intent
                                                      -> generate_release_note
                                                         -+-> post_feedback (coverage sufficient)
                                                          +-> test_generator -> post_feedback
```

* The PR head is fetched into a throwaway git worktree inside the sandbox; the
  persistent clone is never mutated, so concurrent reviews cannot corrupt
  each other.
* A red build or a red test suite short-circuits straight to the report,
  with the logs.
* When both are green, the LLM validates the diff against the linked issue,
  drafts the release notes, then generates tests for the uncovered edge cases.
* A generated test that fails is **never submitted**: it is quoted in full in
  the comment instead, because a red generated test is either a real defect or
  a bad test, and that call belongs to a human.
* Without an LLM key, CodeSentry still clones, builds, tests and reports; the
  LLM steps are skipped and the comment says so.

## Configuration

One JSON file per repository, bound with `set config` — examples in
[`configs/`](configs):

```json
{
  "watched_branches": ["main"],
  "build": { "mode": "cargo" },
  "test":  { "mode": "cargo test" },
  "auto_test_pr": false
}
```

* `watched_branches` — a pull request is reviewed only when its **target**
  branch is listed here.
* `build.mode` / `test.mode` — `cmake`, `gcc`, `cargo` / `cargo test`,
  `cmake test`, or `custom` with an explicit `command`.
* `auto_test_pr` — when `true`, passing generated tests are pushed as a
  follow-up pull request; by default they are only suggested in the comment.

Environment variables (`GITHUB_TOKEN`, `SENTRY_LLM_PROVIDER`, `SENTRY_STATE_FILE`, …)
and the full command reference: [sentry/README.md](sentry/README.md#environment).

## Webhook

`POST /webhook/github`, events `pull_request` `opened` / `synchronize` /
`reopened`. An event is accepted only when the repository is registered,
enabled and configured, the pull request is not a draft, and its target branch
is watched. The endpoint answers immediately and runs the review on a
background thread; the same `(repo, head_sha)` is never reviewed twice.

## Tests

```bash
cd sentry && python -m pytest
```

117 tests, no Docker, no GitHub and no LLM key required — every external
dependency is faked.

## Demo

[`demo/ring-buffer`](demo/ring-buffer/README.md) is a small Rust crate whose
`drain` has an off-by-one bug on the wrap-around path that its committed tests
do not reach. Push it to a repository you own, open a PR against `main`, and
the generated wrap-around test will fail against the branch — something real
for the test generator to find.
