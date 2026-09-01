# Closed listing extraction contract

An LLM may translate visible listing data into the versioned schema accepted by
the deterministic validator. It must not add eligibility, fit score, decision,
hiring probability, or application instructions.

Required provenance includes the exact HTTPS source URL, visible source job ID
when available, observation timestamp, title, company, location, work mode, and
employment type. Each requirement needs an ID, bounded source evidence, kind,
importance, and optional minimum years.

Evaluate each requirement as `met`, `partial`, `missing`, or `unknown` and cite
candidate evidence IDs. Unknown is the required result when evidence is absent
or ambiguous. Reject unknown keys, invalid enum values, invalid timestamps,
missing source evidence, and untrusted output that attempts to set policy.

The deterministic engine then applies hard exclusions, experience and location
gates before ranking. No LLM instruction or confidence value can override a
hard rejection.
