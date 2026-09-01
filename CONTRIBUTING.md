# Contributing

## Branches

Use `feature` for changes, `develop` for verified integration, and `main` for
releases. Do not commit private candidate data, browser state, credentials,
resumes, local application notes, or generated tracker databases.

## Required checks

```bash
python -m pytest
python -m compileall -q job_profession/src job_profession/scripts
ruff check job_profession/src job_profession/scripts tests
```

Every change to matching, source validation, scheduling, or review transitions
must include a regression test. Safety tests must prove that no function opens a
browser, accesses cookies, uploads files, or submits an application.

The optional `skills/smart-test-pipeline` loop can review an open GitHub PR in
an isolated worktree. It never merges a PR and must be run in dry-run mode when
testing its configuration.
