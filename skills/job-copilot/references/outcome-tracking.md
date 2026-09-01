# Candidate-controlled outcome tracking

Use the append-only lifecycle tracker as an audit log, not as proof that a
browser action happened.

- Shortlist records may be created by an agent from validated recommendations.
- Group no more than five unique jobs in one review round.
- `opened`, `reopened`, and `closed` are tab observations only. They never imply
  `manual_applying` or `submitted`.
- A missing active tab becomes `awaiting_outcome` and keeps its queue slot
  reserved until the candidate confirms `submitted`, `rejected`, or `skipped`.
  Only then call `plan_refill` with the current visible URLs. If it reports
  `search_needed`, search for that many eligible unseen candidates, add them,
  plan again, and open only the returned exact listing URLs.
- Only explicit candidate input may record `manual_applying`, `submitted`,
  `interview`, `rejected`, `offer`, or `withdrawn`.
- Record missing answers and friction as attributed attention events. Record
  candidate-approved reminders as follow-up events.
- Derive application totals from the first candidate-owned `submitted` event
  per job. A duplicate event, reopened tab, saved job, or shortlist must not
  inflate the count.

Never inspect a form, inbox, browser session, or employer portal to manufacture
an outcome. If the candidate has not confirmed it, preserve `unknown`.
