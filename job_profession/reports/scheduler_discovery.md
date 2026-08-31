# Scheduler and safe discovery design

`job_profession` schedules a local, discovery/export-only pass at 08:00 and
17:00 in the Mac's local time zone. It constructs transparent LinkedIn and
Indeed search URLs for Python backend, FastAPI, and API-integration roles.

The scheduler has no website client and no browser integration. Its required
adapter supplies only previously visible listing payloads; it cannot log in,
read browser cookies, bypass a CAPTCHA, click Apply, answer forms, or submit an
application. Every run reports `application_actions: 0`.

Only listings whose deterministic matcher returns `recommended` at a score of
at least 85 are exported. Each export records score, fixed 85-point threshold
metadata, evidence explanations, gaps, canonical URL, and a human-review
requirement. Lower-scoring and malformed payloads are counted in the run log
but are not exported.

State stores a stable listing fingerprint. Repeated runs and duplicate cards in
the same visible-page payload are skipped, so `recommended_jobs.jsonl` does not
gain duplicate rows. `discovery_runs.jsonl` provides one transparent result per
run. The optional cron wrapper uses only offline JSON supplied by
`JOB_PROFESSION_VISIBLE_PAYLOADS`; without one it safely creates an empty
heartbeat run.

The launch agent is opt-in: inspect `launchd/com.haseeb.job-profession.plist`
and run `scripts/install_launch_agent.py --install` manually to load it.
