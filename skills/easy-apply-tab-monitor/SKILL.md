---
name: easy-apply-tab-monitor
description: Open a bounded set of recommended LinkedIn and Indeed listing tabs for candidate review through a browser-neutral adapter; never interact with an application.
---

# Easy Apply Tab Monitor

Use this skill when the candidate wants job listings opened for manual application, not automated application submission.

## Safety contract

- In Smart Queue mode, maintain the candidate-selected number of managed,
  approved listing tabs: an integer from 1 through 10, default 5. This is not a
  count of unrelated browser tabs, searches, account pages, or application
  flows. The browser and operating system are supplied by the host agent.
- Attach only to an already-existing, host-provided browser session. Never
  launch Chrome or another browser, create a browser session or window, close a
  tab, or inspect page content. The bounded adapter may only list tab URLs and
  open one exact approved listing URL.
- LinkedIn and Indeed are the only supported hosts. Keep the exact canonical listing URL in the manifest.
- Never click Apply, Easy Apply, Continue, Next, Submit, or any equivalent control. Do not fill, upload, or transmit candidate data.
- Do not handle credentials, CAPTCHA, MFA, consent, or account creation. Pause if the board requests them.
- A listing may be Easy Apply or an external/company-site path, but label the path clearly so the candidate can choose.
- Read prior-application state only from the local candidate-confirmed tracker.
  Do not inspect email, employer portals, or application pages.
- Smart Queue mode treats a missing tab as `awaiting_outcome`, never
  `submitted`; it keeps the slot reserved until the candidate explicitly
  confirms both an outcome and that the managed tab is vacated. An observed
  missing URL never supplies the vacancy confirmation. The legacy fixed-round
  watcher reopens the same URL only when the candidate explicitly requests that
  behavior.
- The live Smart Queue host supplies already-validated `QueueCandidate` values
  directly to `scripts/smart_queue_coordinator.py`; there is intentionally no
  recommendation-file CLI. It accepts any host-supplied adapter that implements
  exactly the two bounded tab operations. The optional Codex Chrome extension
  bridge is a reference integration for an already-connected session, not a
  claim that the core can prove a host browser identity. Its local queue
  database must resolve under `jobapply_agent/private/`, which is ignored,
  never at repository root.

## Workflow

1. Ask the candidate how many managed listing tabs to keep open (1–10, default
   5), then select up to that capacity from the supplied research queue. Prefer
   the strongest profile matches, exclude roles already marked submitted/filled,
   and mix LinkedIn/Indeed only when both have suitable listings.
2. Create a local manifest with title, company, platform, exact URL, fit rationale, and visible apply path (`easy_apply`, `apply_with_indeed`, or `external`). Do not include email contents, resume text, credentials, or screening answers.
3. Open each exact URL in a separate visible browser tab. Use only supplied
   listing metadata and candidate review to verify title, company, location,
   posting age, and apply-path label; the tab adapter never inspects page
   content or opens a form.
4. Read prior-application state from the local tracker. Anything not explicitly
   candidate-confirmed remains `unknown`.
5. In Smart Queue mode, the host gives `SmartQueueCoordinator(queue, browser)`
   up to the candidate-selected capacity of prevalidated candidates directly.
   It compares visible managed listing URLs and records missing jobs as
   `awaiting_outcome`. Before recording an outcome or planning a replacement,
   require the candidate to explicitly confirm both the outcome and that the
   managed tab is vacated. Do not treat a missing URL as a vacancy: the
   candidate may be manually applying or continuing on another route. Then
   open only returned exact listing URLs. If the pool is short, report
   `search_needed` to the host agent. A capacity reduction never grants close
   authority: the agent opens no replacements until candidate-confirmed
   vacancies bring the managed queue below capacity.
6. The coordinator result contains counts and opaque queue IDs only; URLs and
   snapshots remain inside the skill. Stop after the requested monitoring cycle
   unless the candidate asks for another cycle.

## Persistent monitoring

For persistent Smart Queue monitoring after the current interaction, use
`scripts/persistent_smart_queue_monitor.py`. It is the live Smart Queue entry
point; do not use `chrome_tab_watcher.py` for Smart Queue operation.

The persistent monitor is existing-session-only. It accepts only a
host-provided, listing-only adapter for an already-connected browser session;
it never launches a browser, creates a session or window, closes a tab, or
inspects page content. Its browser authority remains limited to listing current
tab URLs and opening an exact, already-approved LinkedIn or Indeed listing URL.
It never applies, fills, uploads, or submits.

Each persistent cycle records a URL-only snapshot against the durable Smart
Queue database under `jobapply_agent/private/`. If a managed listing disappears,
the monitor records `awaiting_outcome`; it does not infer that the candidate
applied, that the slot is vacant, reopen that listing, or open a replacement.
A physical tab closure creates no application outcome or candidate-confirmed
vacancy. Before it records `submitted`, `rejected`, or `skipped`, the monitor
must receive the candidate's explicit `actor="user"` confirmation of both the
outcome and that the managed tab is vacated. This is required even when a
URL-only snapshot no longer contains the listing, since a manual application or
continuation route can create a false vacancy. Only that dual confirmation
permits a refill. The candidate alone closes tabs.

Persistent-monitor status is counts and opaque queue IDs only. Exact approved
URLs and browser snapshots stay inside reconciliation and are never printed in
status output.

### Legacy fixed-round watcher

`scripts/chrome_tab_watcher.py` is retained only for a candidate who explicitly
asks to keep one fixed manifest round open by reopening its same missing URLs.
It is not a Smart Queue capacity authority and must not be used for persistent
Smart Queue replenishment. Its external bridge supports any browser and
operating system through the `list-tabs`/`open-listing` argv/JSON protocol. The
optional `chrome-applescript` adapter is legacy compatibility only: it refuses
to create a Chrome window, cannot provide the live queue's atomic session
boundary, and is never a Smart Queue adapter.

Legacy manifests remain local in `jobapply_agent/private/` and can include an
`active_url_prefixes` list for an expected in-progress route such as Indeed
Smart Apply; this prevents a duplicate listing tab while the candidate is
applying.

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

The helper is legacy fixed-round tooling: it rejects non-LinkedIn/Indeed hosts,
duplicate URLs, more than five jobs, and unsupported apply-path values. It is
not the Smart Queue capacity authority. It stores no credentials or message
content.
