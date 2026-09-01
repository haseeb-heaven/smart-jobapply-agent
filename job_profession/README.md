# Agent execution boundary

> ⚠️ This subtree is consumed by an AI agent, not operated as a human-facing
> application. Load the repository-root `AGENTS.md`, `README.md`, security
> policy, and `skills/job-copilot/SKILL.md` before using any module here.

The deterministic package accepts candidate-confirmed evidence and supplied
listing payloads. It validates intake and LLM extraction, applies hard
eligibility before ranking, deduplicates recommendations, maintains the
five-listing Smart Job Queue, and records candidate-confirmed outcomes.

No module in this subtree may browse, authenticate, inspect email or browser
state, fill a field, upload a document, click an application control, accept an
attestation, or submit. Suggested wording is local reference material only; it
is never eligible for pre-fill. The candidate manually reviews and performs
every application action.

Private inputs belong only under ignored `job_profession/private/`. Never copy
them into source, tests, logs, issues, commits, examples, or shared agent
reports. Unknown and unresolved facts remain explicit; do not infer sensitive
answers or convert them into approved profile evidence.
