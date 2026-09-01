# Security policy

Smart JobApply Agent handles sensitive candidate information. Keep
`job_profession/private/` local-only and never commit it. Treat browser
sessions, cookies, API keys, resumes, compensation, visa details, and screening
answers as secrets.

The core package intentionally has no network, browser, login, CAPTCHA, upload,
or application-submit capability. Do not weaken that boundary in a patch.

If you find a security issue, do not publish private data in a public issue.
Contact the repository owner privately with a minimal reproduction and avoid
including credentials or personal documents.
