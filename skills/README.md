# Included skills

## smart-test-pipeline

Copied from the `smart-test-pipeline` branch of the `firstmate-heaven`
repository. It is a guarded PR review/test/fix/CI loop. It never merges a PR,
requires an open PR, isolates worktrees, validates without model credentials,
and supports dry-run mode. Use only after reading its `SKILL.md`.

## stow

Local-only durable-session-notes workflow. It does not store credentials, does
not infer permission to write to external systems, and keeps private fallback
notes ignored.

## easy-apply-tab-monitor

Opens and monitors a bounded set of LinkedIn/Indeed listings for human review.
It never fills or submits an application; it only reopens the same listing URL
if a tab disappears during the requested monitoring cycle.
