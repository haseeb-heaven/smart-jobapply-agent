# Tracker storage and export

## Local audit model

`Tracker` stores the current deduplicated job record in SQLite using the
normalized platform, canonical source URL, title, and company fingerprint as
the job ID. Each discovery adds an immutable `job_observations` row, including
the source URL, description snapshot hash, score, score explanation, gaps, and
evidence explanations. Status changes are separate immutable `status_history`
rows with timestamp and actor.

Allowed review progression is:

`discovered` → `reviewed` → `ready_to_apply` → `submitted`

A job may be rejected during review, a ready job may return to review, and a
rejected job may be reopened for review. The transition to `submitted` accepts
only actor `user`; it records a user-reported manual submission and cannot
access a job site or trigger an application action.

## Export status

The approved XLSX authoring dependency (`@oai/artifact-tool`) is unavailable in
this runtime. The export deliberately refuses `.xlsx` rather than writing CSV
content with an Excel filename. Consequently,
`output/Job_Application_Tracker.xlsx` has **not** been generated.

Use the supported interim command instead:

```bash
python3 jobapply_agent/scripts/export_tracker.py
```

The CLI's default now writes `jobapply_agent/output/Job_Application_Tracker.csv`;
passing an explicit `.xlsx` path remains blocked until the approved workbook
dependency is available.

Open that CSV in Google Sheets or Microsoft Excel and save it as an XLSX file
manually. The CSV is a complete, filterable all-jobs view and includes source
URL, platform, fit score, score explanation, gaps, status, and an explicit
`fit_score_is_not_offer_probability=true` marker. It also has fields required
for the intended review queue and applications log, including materials,
questions, manual submission, follow-up, and response status.

When the approved workbook tooling is supplied, the intended XLSX layout is:

| Sheet | Planned content |
| --- | --- |
| Recommended Jobs | Ranked recommended review queue |
| All Jobs | Complete deduplicated history with fingerprint/hash and last seen |
| Application Queue | Materials, review state, questions, owner, and deadline |
| Applications | Manual submitted record and follow-up fields |
| Metrics | Formula-driven new/qualified/reviewed/submitted/interview metrics |
| Profile QA | Conflicts, prohibited claims, target titles, and exclusions |

No personal contact details, pay history, work-authorization data, or
application answers are included by this tracker export.
