---
name: easy-apply-tab-monitor
description: Open a bounded set of recommended LinkedIn and Indeed listing tabs for candidate review through a browser-neutral adapter; never interact with an application.
---

# Easy Apply Tab Monitor

Use this skill when the candidate wants job listings opened for manual application, not automated application submission.

## Safety contract

- Open at most five job tabs per round through an explicitly configured browser
  adapter. The browser and operating system are supplied by the host agent.
- LinkedIn and Indeed are the only supported hosts. Keep the exact canonical listing URL in the manifest.
- Never click Apply, Easy Apply, Continue, Next, Submit, or any equivalent control. Do not fill, upload, or transmit candidate data.
- Do not handle credentials, CAPTCHA, MFA, consent, or account creation. Pause if the board requests them.
- A listing may be Easy Apply or an external/company-site path, but label the path clearly so the candidate can choose.
- Read prior-application state only from the local candidate-confirmed tracker.
  Do not inspect email, employer portals, or application pages.
- Smart Queue mode treats a missing tab as `awaiting_outcome`, never
  `submitted`; it keeps the slot reserved until the candidate confirms an
  outcome. The legacy fixed-round watcher reopens the same URL only when the
  candidate explicitly requests that behavior.

## Workflow

1. Select up to five recommendations from the supplied research queue. Prefer the strongest profile matches, exclude roles already marked submitted/filled, and mix LinkedIn/Indeed only when both have suitable listings.
2. Create a local manifest with title, company, platform, exact URL, fit rationale, and visible apply path (`easy_apply`, `apply_with_indeed`, or `external`). Do not include email contents, resume text, credentials, or screening answers.
3. Open each exact URL in a separate visible browser tab. Use only supplied
   listing metadata and candidate review to verify title, company, location,
   posting age, and apply-path label; the tab adapter never inspects page
   content or opens a form.
4. Read prior-application state from the local tracker. Anything not explicitly
   candidate-confirmed remains `unknown`.
5. In Smart Queue mode, compare visible URLs and record missing jobs as
   `awaiting_outcome`. Wait for candidate-confirmed outcomes before planning
   replacements, then open only returned exact listing URLs. If the pool is
   short, report `search_needed` to the host agent.
6. Return a compact table of active, waiting, awaiting-outcome, confirmed, and
   replacement jobs. Stop after the requested monitoring cycle unless the
   candidate asks for another cycle.

## Persistent monitoring

For a fixed-round monitor that must keep running after the current interaction,
use
`scripts/chrome_tab_watcher.py --manifest <local-manifest> --watch --adapter
external --adapter-command <bridge-argv...>`. The legacy filename is retained
for compatibility; the external bridge supports any browser and operating
system through the `list-tabs`/`open-listing` argv/JSON protocol. The optional
`chrome-applescript` adapter supports macOS Chrome only. The monitor reopens
only missing listing URLs and writes one counts-only, redacted JSON status line
per cycle; exact approved/browser URLs remain internal to reconciliation and are
never included in CLI status output.
It never applies, fills, uploads, or submits. A monitor manifest remains local in
`job_profession/private/` and can include an `active_url_prefixes` list for an
expected in-progress route such as Indeed Smart Apply; this prevents a duplicate
listing tab while the candidate is applying.

## Local manifest helper

`scripts/tab_manifest.py` validates and compares the bounded queue without
network or browser access. Use it to make the monitor repeatable; the host agent
must provide the browser adapter and its permissions.

```sh
python3 skills/easy-apply-tab-monitor/scripts/tab_manifest.py create \
  --input queue.json --output .job-tab-monitor.json
python3 skills/easy-apply-tab-monitor/scripts/tab_manifest.py missing \
  --manifest .job-tab-monitor.json --observed-url https://www.linkedin.com/jobs/view/123
```

The helper rejects non-LinkedIn/Indeed hosts, duplicate URLs, more than five jobs, and unsupported apply-path values. It stores no credentials or message content.
