# Implementation Plan: Agent Queue Admission

## Overview

Implement the missing safe handoff from ranked discovery output to the durable
Smart Job Queue so an agent can refill a requested queue capacity without
granting the core daemon any search or application authority.

## Tasks

### Task 1: Admission domain contract

- Add a deterministic converter/admission service and focused tests.
- Acceptance: all-or-nothing validation, empty-queue-only revision binding,
  suppression before durable admission, rollback-safe memory scope handling,
  and aggregate-only results.
- Verify: focused smart-queue, candidate-memory, and discovery tests.

### Task 2: Agent-only CLI and lease serialization

- Wire the service into `discover.py` as `admit-queue` and use the monitor's
  exact sibling-file durable queue lease.
- Acceptance: CLI rejects invalid input without mutation and reports counts only.
- Verify: CLI and contention tests.

### Task 3: End-to-end synthetic refill

- Add a URL-only integration test from five validated discovery rows through
  admission to five synthetic opens; preserve the empty-queue live-smoke path.
- Acceptance: five distinct approved URLs open, no suppressed/stale row opens.
- Verify: Python/Node focused tests and `./scripts/verify.sh`.

### Task 4: Agent instructions and docs

- Document exact host-agent sequence after `search_needed`.
- Acceptance: docs state no core searching/browser/apply authority and name the
  executable admission command.

## Risks

| Risk | Mitigation |
| --- | --- |
| Suppression bypass | Make it internal to admission and test ordering. |
| Queue/monitor race | Acquire one durable queue lease around admission. |
| Private data leakage | Accept private files but output only aggregates. |
| Scope creep into autonomous apply | Keep discovery host-owned and bridge unchanged. |
