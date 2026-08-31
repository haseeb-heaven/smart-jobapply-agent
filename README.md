# Job Profession

Evidence-first job discovery and application preparation for a mid-level,
implementation-focused software developer.

This repository scores **visible LinkedIn and Indeed listing data** against an
approved candidate profile and produces an auditable review queue. It is not an
application bot. It never logs in, reads browser credentials, uploads a resume,
answers screening or EEO questions, clicks Apply/Submit, solves CAPTCHAs, or
accepts attestations.

## What it does

- Keeps professional, personal/open-source, and learning evidence separate.
- Rejects senior, staff, principal, lead, architect, manager, head, director,
  junior, entry-level, and explicit job-level titles.
- Rejects architecture/strategy ownership, people management, and mandatory
  production GenAI ownership outside the approved profile.
- Scores Python/FastAPI/API, database, testing, maintenance, feature,
  background-job, and integration responsibilities transparently.
- Accepts listing payloads only through an explicit visible-page adapter.
- Deduplicates listings, preserves description snapshots, and tracks profile and
  matcher-policy revisions.
- Writes a current-profile CSV queue and an append-only JSONL audit trail.
- Provides a local SQLite tracker and truthful, review-only application drafts.
- Includes a macOS `launchd`/cron wrapper for discovery and export only.

## Safety model

The core package is dependency-free and has no network or browser capability.
A browser or API integration, if added later, must supply already-visible data
through the adapter boundary and must preserve source provenance. Scheduled jobs
may refresh review data; they must never submit an application.

Scores describe profile fit, not hiring probability. The candidate must verify
the company, listing, compensation, location, work authorization, sponsorship,
availability, attachments, and every screening question before applying.

## Quick start

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest
```

The approved profile is intentionally private and is ignored by Git:
`job_profession/private/candidate_profile.yaml`. Copy the redacted templates
from `job_profession/private.example/` before adding local data. Never commit
private facts or generated databases.

Run a safe discovery heartbeat with no network access:

```bash
python job_profession/scripts/discover.py --output-dir job_profession/data
python job_profession/scripts/discover.py --output-dir job_profession/data \
  --export-current-recommendations job_profession/output/Current_Profile_Recommended_Queue.csv
```

To score visible listings, provide an offline JSON mapping of search URLs to
listing objects. See `job_profession/config/search_profiles.yaml` and the
tests for the payload shape.

## Repository layout

```text
job_profession/
  src/job_profession/       Pure models, normalization, matching, scheduling,
                            tracking, and review-only draft composition.
  scripts/                  Offline discovery, exports, macOS launchd setup,
                            and optional read-only browser health checks.
  config/                   Search profiles, scoring rules, and safety policy.
  private/                  Local-only candidate details (ignored by Git).
  reports/                  Design and operational documentation.
  launchd/                  User-level macOS scheduling template.
tests/                      Regression and safety tests.
skills/                     Reusable Smart Test Pipeline and stow skills.
```

## Smart Test Pipeline

`skills/smart-test-pipeline` is adapted from the `smart-test-pipeline` branch of
the `firstmate-heaven` repository. It provides a guarded PR review → test →
fix → CI loop. It refuses closed PRs, isolates worktrees, keeps validation
credential-free, never merges, and supports dry runs. It is optional and
requires an authenticated GitHub CLI, `jq`, Git, and an explicitly configured
fix agent. Read its `SKILL.md` before running it.

The `skills/stow` skill is also included for local, deliberate session-memory
maintenance; it never files credentials or silently writes to external systems.

## Development

```bash
python -m pytest
python -m compileall -q job_profession/src job_profession/scripts
ruff check job_profession/src job_profession/scripts tests
```

Pull requests should use the `feature` branch and include tests for every
policy or safety change. Do not add any code path that can submit an application.

## License

MIT. See [LICENSE](LICENSE). Private candidate files and generated local data
remain excluded by `.gitignore`.
