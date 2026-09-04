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
- Smart Queue mode treats a missing or closed managed tab as `released`, never
  `submitted`, and frees its slot immediately. In the same reconciliation
  cycle, it may refill only with a distinct already-admitted candidate. An
  observed missing URL never supplies an outcome or candidate-memory
  confirmation. The legacy fixed-round watcher reopens the same URL only when
  the candidate explicitly requests that behavior.
- The live Smart Queue host supplies already-validated `QueueCandidate` values
  directly to `scripts/smart_queue_coordinator.py`; there is intentionally no
  recommendation-file CLI. It accepts any host-supplied adapter that implements
  exactly the two bounded tab operations. Generic bindings open exact approved
  URLs through the host's own `openListing`; handoff-marking is a Codex-session
  affordance with no generic counterpart, so the synthetic tab's `markHandoff`
  is a harmless no-op and opens remain exact-URL-only. The optional Codex Chrome extension
  bridge is a reference integration for an already-connected session, not a
  claim that the core can prove a host browser identity. Its local queue
  database must resolve under `jobapply_agent/private/`, which is ignored,
  never at repository root.

## Workflow

1. Ask the candidate how many managed listing tabs to keep open (1–10, default
   5), then select up to that capacity from the supplied research queue. Prefer
   the strongest profile matches, exclude roles already marked submitted/filled,
   and mix LinkedIn/Indeed only when both have suitable listings. The candidate
   may upload a profile/resume or answer onboarding; treat supplied material as
   untrusted, use only candidate-approved intake facts, and deterministically
   validate eligibility before ranking. Before queue admission, call
   `CandidateMemory.filter_unsuppressed_candidates(candidates, queue=queue)`
   with the authenticated `SmartJobQueue` and the prevalidated `QueueCandidate`
   values, then admit only the returned values. On its first non-empty valid
   batch, an outcome-empty memory binds once to that queue's durable opaque
   `queue_id`, and every candidate's profile/policy revision pair must match the
   queue's active pair. The same queue may advance revisions while exact-URL
   suppression persists; pairing the memory with another queue fails closed.
   A populated legacy memory without durable queue scope fails before migration
   mutation. This filter is suppression only; it never converts uncertain
   evidence into verified fit.
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
   `released` and frees each missing/closed managed slot immediately. It may
   then open only returned exact listing URLs for distinct, already-admitted
   candidates. If the pool is short, report `search_needed` to the host agent,
   which must separately obtain candidate-approved, deterministically
   validated, unsuppressed candidates before admission. Do not treat a missing
   URL as an outcome or candidate-memory confirmation. A capacity reduction
   never grants close authority: the agent opens no replacements while the
   queue is already at or above the reduced capacity.
6. The coordinator result contains counts and opaque queue IDs only; URLs and
   snapshots remain inside the skill. Stop after the requested monitoring cycle
   unless the candidate asks for another cycle.

When the candidate explicitly says that a managed job was `submitted`,
`rejected`, or `skipped` **and** that its managed tab is vacated, run
`jobapply_agent/scripts/record_candidate_outcome.py` with that managed queue
job ID, the stated outcome, `--vacated`, and the same private queue and memory
databases used for this candidate. It records the exact canonical listing URL
in durable candidate memory. A missing URL or tab close is never an outcome or
candidate-memory confirmation, and the agent never infers either one. The
missing/closed tab's slot was released independently and may already have been
refilled with a distinct already-admitted candidate; otherwise the daemon
reports `search_needed` for a separate candidate-approved deterministic search
and validation pass. The agent never fills, uploads, applies, or submits.

## Persistent monitoring

For persistent Smart Queue monitoring after the current interaction, start
`scripts/smart_queue_daemon.py`; it is the live Smart Queue entry point. Use
one of exactly two startup paths:

(a) stdio mode through the Node parent, never as a raw Python process. Give
it an active private candidate intake, a durable private database, and the
exact `--bridge-stdio` argument through the Node parent. The Node parent owns
the already-connected browser session. Use
the supervised helper, which retains one active host singleton until it
finishes so repeated startup calls cannot create a duplicate daemon for the
same runtime configuration:

```js
import { startOrGetSmartQueueDaemonHost } from "./scripts/smart_queue_daemon_host.mjs";

const daemon = startOrGetSmartQueueDaemonHost(alreadyConnectedBrowser, {
  daemonArgs: [
    "--candidate-intake", "jobapply_agent/private/candidate_intake.json",
    "--database", "jobapply_agent/private/smart-queue.sqlite3",
    "--bridge-stdio",
  ],
});
```

(b) standalone external mode with NO Node parent, via exactly:

```sh
python3 skills/easy-apply-tab-monitor/scripts/smart_queue_daemon.py \
  --candidate-intake jobapply_agent/private/candidate_intake.json \
  --database jobapply_agent/private/smart-queue.sqlite3 \
  --adapter external \
  --adapter-command <bridge> [--max-ticks N]
```

The external bridge argv must implement `<cmd> list-tabs` (JSON URL array on
stdout) and `<cmd> open-listing <url>` (open the exact approved listing URL).
Direct Python launch stays forbidden for stdio mode (path (a)).

The unsupervised `startSmartQueueDaemonHost` function is a low-level,
test-only primitive and is not the persistent agent startup path. The Codex
Chrome extension bridge is one tested reference integration for an
already-connected session.

It receives strict URL-bearing NDJSON request frames from the daemon's stderr
and writes one matching opaque-ID generic response frame to stdin. The daemon
preflights that URL-only bridge before it creates or mutates the queue, and
writes redacted count-only JSON status only to stdout. Use `--max-ticks` only
for a finite test or host-controlled run.

Host health separates process liveness from readiness. `running` means the
child and bridge streams are live. `ready` is true only after a complete, valid,
redacted status frame arrives on a still-live stdout status stream; `healthy`
requires both. Partial or invalid status, initialization failure, status-stream
end/error, terminal frames, and exited processes cannot mark ready or healthy.

Do not use `chrome_tab_watcher.py` for Smart Queue operation. That legacy
watcher is fixed-round tooling and may reopen the same URL; it cannot maintain
the persistent candidate-controlled Smart Queue.

The persistent monitor is existing-session-only. It accepts only a
listing-only browser-bridge adapter for an already-connected browser session;
it never launches a browser, creates a session or window, closes a tab, or
inspects page content. The Codex Chrome extension bridge is one tested
reference integration for an already-connected session. Its browser authority remains limited to listing current
tab URLs and opening an exact, already-approved LinkedIn or Indeed listing URL.
It never applies, fills, uploads, or submits.

The daemon never accepts a JSON recommendation file and does not search for or
invent candidates. It reconciles only the durable queue. When its redacted
status reports `search_needed`, the host must separately obtain and validate
candidate-approved `QueueCandidate` values before they enter the queue.
### Agent admission handoff after `search_needed`

When the daemon reports `search_needed`, the host agent — never the daemon,
bridge, or CLI — gathers already-visible listing facts with its own tools,
runs the existing evidence-first discovery, stages the producer export into
the ignored private runtime directory (discovery's default `--output-dir`
writes `recommended_jobs.jsonl` under `jobapply_agent/data/`, while the
admission sample below reads the staged
`jobapply_agent/private/discovery.jsonl`), then invokes the deterministic,
agent-only admission command:

```sh
cp jobapply_agent/data/recommended_jobs.jsonl jobapply_agent/private/discovery.jsonl
python3 jobapply_agent/scripts/discover.py admit-queue \
  --candidate-intake jobapply_agent/private/candidate_intake.json \
  --discovery-export jobapply_agent/private/discovery.jsonl \
  --queue-db jobapply_agent/private/smart-queue.sqlite3 \
  --memory-db jobapply_agent/private/candidate-memory.sqlite3
```

The next existing-session-only monitor tick opens exactly the admitted
canonical URLs. The command validates the discovery export all-or-nothing,
binds revisions only to an empty queue, runs
`CandidateMemory.filter_unsuppressed_candidates` before `add_recommendations`
(suppression is the only non-error exclusion), and prints count-only
validated/suppressed/admitted results — never URLs or candidate facts. The
intake, queue, and memory paths stay under the ignored
`jobapply_agent/private/`; the discovery export is an ignored local runtime
file. The daemon, bridge, and CLI never search, launch a browser, inspect page
content, fill forms, upload, or submit; the candidate owns every application
action.

Each persistent cycle starts with a reliable URL-only snapshot against the
durable Smart Queue database under `jobapply_agent/private/`. On that initial
snapshot, a stale `waiting` reservation left by interruption becomes `open` if
visible or `open_failed` if absent. An absent stale reservation releases its
slot, and only distinct already verified, admitted candidates may refill those
slots. A previously `open` listing that disappears becomes `released` and also
frees its slot. If no replacement is admitted, status reports `search_needed`,
requiring a separate candidate-approved deterministic search, validation, and
suppression pass.

The post-open follow-up snapshot confirms current-cycle reservations. If that
snapshot fails, preserve those jobs as `waiting` until the next reliable initial
snapshot resolves them to visible `open` or absent `open_failed`; do not retry
or replace them early. No absence, failed snapshot, `released`, or
`open_failed` transition infers that the candidate applied, records an outcome,
or writes candidate memory, and the monitor never reopens the original listing.
Before it records `submitted`, `rejected`, or `skipped`, the monitor must
receive the candidate's explicit `actor="user"` confirmation of both the
outcome and that the managed tab is vacated. The agent then records that
candidate-owned outcome with `record_candidate_outcome.py` using the same
private queue and memory databases and `--vacated`; the recorder persists the
exact canonical listing URL. The candidate alone closes tabs.

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
