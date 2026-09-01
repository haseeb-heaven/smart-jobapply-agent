# Application preparation safety design

Application preparation is local and review-only. The composer creates a
job-specific cover-letter draft and hiring-manager message from supported
mid-level evidence. Professional backend experience and personal/open-source AI
project work are separate labels; the latter is never presented as employer
production-AI experience.

`prepare_application()` receives only inert local labels and approved draft
text. It creates field suggestions for approved non-sensitive text and prompts
the candidate for every missing, sensitive, attestation, CAPTCHA-like, EEO, or
screening field. A prompt stops the record at `needs_review`.

There is no browser client, browser-session dependency, credential inspection,
resume upload, form typing, Apply action, Submit action, CAPTCHA handler, or
attestation handler. `PreparedApplication` has no submit method. A `submitted`
tracker state records only a submission that the candidate reports as completed
manually after review.

## Verification

The preparation, composition, matching, and normalization tests passed locally:
`17 passed`. The offline CLI was also exercised with a cover-letter label and a
work-authorization question; it produced no form action and stopped with a
candidate-owned authorization prompt.
