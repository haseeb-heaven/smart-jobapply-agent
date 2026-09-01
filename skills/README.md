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
if a tab disappears during the requested monitoring cycle. Its persistent
runtime uses a configured browser adapter; macOS Chrome is one optional
compatibility adapter, not a project requirement.

## job-copilot

The primary agent workflow. It accepts any capable host LLM, operating system,
browser, and browser-control bridge while preserving deterministic intake,
eligibility, ranking, bounded tab opening, and candidate-reported tracking.
The candidate manually completes every application.

## Agent delivery set

Curated from `haseeb-heaven/heaven-skills` for this repository:

- `using-git-worktrees`: isolated `feature/<slug>` writing lanes.
- `dispatching-parallel-agents`: independent, non-overlapping agent dispatch.
- `domain-modeling`: Matt Pocock domain vocabulary and invariant discovery.
- `implementor`: bounded implementation with validation evidence.
- `diagnosing-bugs`: Matt Pocock reproduction-to-root-cause loop.
- `security-and-hardening`: candidate-data and integration trust-boundary review.
- `code-review`: Matt Pocock independent diff review.
- `browser-testing-with-devtools`: localhost-only synthetic browser QA.

Project-specific overrides inside each skill defer to `AGENTS.md`, preserve the
evidence-first/no-application-action contract, and prevent browser QA from
attaching to logged-in job-board, email, or daily-browser sessions.

Firstmate internal `.agents/skills` are intentionally excluded because they
depend on a live Firstmate home. Firstmate's two standalone skills (`stow` and
`smart-test-pipeline`) were already present. The available Matt Pocock `tdd`
copy is excluded because it references missing resources; unit-test ownership
is defined directly by `AGENTS.md` instead.
