# Candidate-controlled outcome tracking

Use the append-only lifecycle tracker as an audit log, not as proof that a
browser action happened.

- Shortlist records may be created by an agent from validated recommendations.
- Group no more than five unique jobs in one review round.
- `opened`, `reopened`, and `closed` are tab observations only. They never imply
  `manual_applying` or `submitted`.
- A missing or closed managed tab becomes `released` and frees its queue slot
  immediately. In the same reconciliation cycle, call `plan_refill` with the
  current visible URLs and open only the returned exact listing URLs for
  distinct, already-admitted candidates. If it reports `search_needed`,
  separately search for that many eligible unseen candidates, deterministically
  validate and suppress them, admit only the returned candidates, plan again,
  and open only the returned exact listing URLs. The missing/closed tab never
  creates an outcome or candidate-memory record.
- Treat each complete initial snapshot as the reliable recovery boundary after
  interruption: stale `waiting` jobs that are visible become `open`; stale
  `waiting` jobs that are absent become `open_failed`, release their slots, and
  are replaced only by distinct admitted candidates. If the post-open snapshot
  fails, preserve current-cycle jobs as `waiting` until the next reliable
  initial snapshot. Never infer an outcome or memory record from absence.
- Before admitting a non-empty prevalidated batch, call
  `CandidateMemory.filter_unsuppressed_candidates(candidates, queue=queue)`.
  It binds an outcome-empty memory once to the authenticated queue's durable
  opaque ID, validates the batch's profile/policy revision pair, and filters
  exact canonical URLs. Suppression survives revision changes on that queue;
  cross-queue pairing fails closed. Legacy outcomes without durable queue scope
  fail before migration mutation.
- Only explicit candidate input may record `manual_applying`, `submitted`,
  `interview`, `rejected`, `offer`, or `withdrawn`.
- Record missing answers and friction as attributed attention events. Record
  candidate-approved reminders as follow-up events.
- Derive application totals from the first candidate-owned `submitted` event
  per job. A duplicate event, reopened tab, saved job, or shortlist must not
  inflate the count.

Never inspect a form, inbox, browser session, or employer portal to manufacture
an outcome. If the candidate has not confirmed it, preserve `unknown`.
