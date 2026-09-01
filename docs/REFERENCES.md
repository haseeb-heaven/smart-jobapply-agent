# External reference audit

These MIT-licensed projects were inspected as design references. Smart JobApply
Agent reimplements selected concepts; it does not import their application
automation. Preserve the upstream license notice if substantial upstream code
is ever copied. The product remains a candidate-pilot co-pilot: deterministic
ranking and queue orchestration only, no form navigation, field entry, uploads, or
submission.

| Project | Audited revision | Concepts retained | Explicitly rejected |
| --- | --- | --- | --- |
| [privacydied/job-application-skill](https://github.com/privacydied/job-application-skill/tree/b85c3b66c4491fcdeb55853a02e560e023d712a9) | `b85c3b66c4491fcdeb55853a02e560e023d712a9` | profile templates, provenance, deduplication, journal integrity | credentials, anti-detect browsers, form/upload/submit, OTP/CAPTCHA behavior |
| [neonwatty/job-apply-plugin](https://github.com/neonwatty/job-apply-plugin/tree/081a5d9d793da29111e2d5331767021718f1d8b5) | `081a5d9d793da29111e2d5331767021718f1d8b5` | versioned local records, privacy fixtures, redacted QA oracle | live form filling, uploads, campaign or submission automation |
| [sameergdogg/job-search-skills](https://github.com/sameergdogg/job-search-skills/tree/2db63263979fe8e240ee571e262ff633111b533d) | `2db63263979fe8e240ee571e262ff633111b533d` | guided intake, hard filters, direct versus transferable skills | form controls, uploads, inferred screening answers, network scraping |
| [Yodablues/claude-code-job-search](https://github.com/Yodablues/claude-code-job-search/tree/1a0c1c4a31cac98bf05ef24cd76c36b1c4ca04b4) | `1a0c1c4a31cac98bf05ef24cd76c36b1c4ca04b4` | broad gather followed by evidence-based judgment | silent errors, guessed salary fit, unvalidated generated claims |
| [vaibhavarora14/job-application-agent](https://github.com/vaibhavarora14/job-application-agent/tree/171d65f4b7d58bfe599f286d0a575c65c8b9fa13) | `171d65f4b7d58bfe599f286d0a575c65c8b9fa13` | assessment schemas, rounds, blockers, outcome states | routine-auto, submission, recruiting email, telemetry and sharing |
| [mattohan567/job-application-agent](https://github.com/mattohan567/job-application-agent/tree/d0136a2283e7e138d65b5f69a984efc90422bad3) | `d0136a2283e7e138d65b5f69a984efc90422bad3` | verified intake, evidence records, conservative outcome reconciliation | application field filling, document uploads, authorized submission |
| [DanielPan12/JobHuntBot](https://github.com/DanielPan12/JobHuntBot/tree/f35c67e34ed95d7d170e88c254c7bcc68a533ede) | `f35c67e34ed95d7d170e88c254c7bcc68a533ede` | runtime-neutral skill entrypoint, evidence bank, blockers, count integrity | form/browser actions, OTP/email access, uploads, autofill and submit |
| [suraj-davariya/ai-job-search](https://github.com/suraj-davariya/ai-job-search/tree/39d037a8f2f228a1352346f1670df8ecff0b2ecc) | `39d037a8f2f228a1352346f1670df8ecff0b2ecc` | pluggable CLI/JSON boundary, provenance, explicit state machine | Claude/Bun coupling, subprocess application workflow, unsafe privacy claims |

The competitor scan above includes the repositories you named in this cycle.
The audited Neonwatty revision remained current on 2026-09-01. Upstream
project popularity was not used as evidence of correctness or safety.
