# Security policy

Smart JobApply Agent handles sensitive candidate information. Keep
`jobapply_agent/private/` local-only and never commit it. Treat browser
sessions, cookies, API keys, resumes, compensation, visa details, and screening
answers as secrets.

The core package intentionally has no network, browser, login, CAPTCHA, upload,
or application-submit capability. Do not weaken that boundary in a patch.

The host may use any LLM, browser, or operating system. An external tab bridge
is an untrusted process with only two permitted operations: list tab URLs and
open one exact approved listing URL. Never pass credentials, cookies, browser
storage, resume contents, or form data through that bridge. A remote LLM may
receive context supplied by its host, so document the actual data flow instead
of claiming all processing is local.

Smart Queue capacity is a candidate-selected limit of 1–10 managed approved
listing tabs (default 5), not permission to enumerate, count, or affect
unrelated browser tabs. A capacity change never grants close, form, upload, or
submission authority.

If you find a security issue, do not publish private data in a public issue.
Contact the repository owner privately with a minimal reproduction and avoid
including credentials or personal documents.
