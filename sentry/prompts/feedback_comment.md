# Feedback comment

The markdown CodeSentry posts on the reviewed pull request. This template is
rendered deterministically from the graph state - no model call - so the same
run always produces the same comment.

<!-- system -->

Not a model prompt: this template is filled in by `review_graph.render_comment`.

<!-- user -->

## CodeSentry review

Commit `$head_sha` - reviewed with $llm_label

| Step | Result |
| --- | --- |
| Build | $build_line |
| Tests | $test_line |
| Intent | $intent_line |

### Intent

$intent_body

### Release notes

$release_notes

### Suggested tests

$tests_section

$logs_section

<sub>Posted automatically by CodeSentry. Run id `$run_id`.</sub>
