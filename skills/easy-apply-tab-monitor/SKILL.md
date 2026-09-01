---
name: easy-apply-tab-monitor
description: Open a bounded set of recommended LinkedIn and Indeed job tabs for human review, check for prior-application evidence, and reopen only the same tabs if they disappear.
---

# Easy Apply Tab Monitor

Use this skill when the candidate wants job listings opened for manual application, not automated application submission.

## Safety contract

- Open at most five job tabs per round, using the user's existing visible Chrome session.
- LinkedIn and Indeed are the only supported hosts. Keep the exact canonical listing URL in the manifest.
- Never click Apply, Easy Apply, Continue, Next, Submit, or any equivalent control. Do not fill, upload, or transmit candidate data.
- Do not handle credentials, CAPTCHA, MFA, consent, or account creation. Pause if the board requests them.
- A listing may be Easy Apply or an external/company-site path, but label the path clearly so the candidate can choose.
- Check prior-application evidence read-only: first inspect the visible listing status, then search the user's visible mail UI for the exact employer/title. Record only `submitted`, `not_found`, or `unknown`; never store or print message bodies.
- Monitoring is bounded and idempotent: after the requested interval, compare the manifest URLs with visible open tabs. Reopen only missing URLs from the same manifest; never replace them with new search results.

## Workflow

1. Select up to five recommendations from the supplied research queue. Prefer the strongest profile matches, exclude roles already marked submitted/filled, and mix LinkedIn/Indeed only when both have suitable listings.
2. Create a local manifest with title, company, platform, exact URL, fit rationale, and visible apply path (`easy_apply`, `apply_with_indeed`, or `external`). Do not include email contents, resume text, credentials, or screening answers.
3. Open each exact URL in a separate visible Chrome tab. Verify the title, company, location, posting age, and apply-path label without opening a form.
4. Search the visible email UI by employer and title. Treat no matching message as `not_found`, an explicit application receipt as `submitted`, and anything ambiguous as `unknown`.
5. Keep the five tabs open for the requested interval (default 60 seconds). At the check, list visible tab URLs and reopen only missing manifest URLs. If a tab remains open, do nothing.
6. Return a compact table of the five URLs, recommendation, apply path, prior-application status, and whether the tab was reopened. Stop after the requested monitoring cycle unless the candidate asks for another cycle.

## Persistent monitoring

For a monitor that must keep running after the current interaction, use
`scripts/monitor_runtime.mjs` with the already-selected visible Chrome browser
handle. Start one monitor with the validated manifest URLs and
`intervalMs: 60000`; it performs an immediate reconciliation, then polls every
minute. Keep the returned state handle so the caller can report `last`, inspect
`history`, or call `stop()`. Starting a second monitor for the same manifest is
not allowed—stop the old handle first.

The runtime is intentionally a browser-session module rather than a standalone
network bot: the browser skill supplies the authenticated visible session and
the runtime only calls `user.openTabs()`, `tabs.new()`, `goto()`, and
`markHandoff()`. It never applies, fills, uploads, or submits.

## Local manifest helper

`scripts/tab_manifest.py` validates and compares the bounded queue without network or browser access. Use it to make the monitor repeatable; use the browser skill for all visible Chrome actions.

```sh
python3 skills/easy-apply-tab-monitor/scripts/tab_manifest.py create \
  --input queue.json --output .job-tab-monitor.json
python3 skills/easy-apply-tab-monitor/scripts/tab_manifest.py missing \
  --manifest .job-tab-monitor.json --observed-url https://www.linkedin.com/jobs/view/123
```

The helper rejects non-LinkedIn/Indeed hosts, duplicate URLs, more than five jobs, and unsupported apply-path values. It stores no credentials or message content.
