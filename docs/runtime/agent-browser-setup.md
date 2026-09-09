# Portable Agent Browser Setup

Smart Queue accepts a browser only through a host-owned, existing session. The
core Python package has no browser dependency or browser authority. A coding
agent can run intake, matching, admission, and outcome recording without a
browser; persistent queue reconciliation requires a conforming connection.

## Required host capabilities

The host supplies a capability descriptor and exactly two operations:

```js
const capabilities = {
  protocolVersion: 1,
  sessionId: "opaque-existing-session-id",
  existingSession: true,
  completeUrlSnapshots: true,
  exactListingOpen: true,
  persistentConnection: true,
};
```

`sessionId` is an opaque host-local identifier. It is never a browser URL,
cookie, token, queue value, or daemon status field. A host must reject setup if
it cannot provide a complete snapshot for the selected session or cannot open
one exact canonical listing URL. Screenshot-only computer-use tools do not meet
this contract.

The browser bridge may list URLs and open a canonical LinkedIn/Indeed listing.
It must not inspect page content, access cookies or storage, click controls,
type, upload, authenticate, close tabs, or submit an application.

## Playwright host integration

The host selects and owns an already-connected `BrowserContext`; this project
does not launch or connect Playwright itself. The binding stays independent of
the Playwright package through structural dependency injection.

```js
import { startPlaywrightSmartQueueDaemonHost } from
  "../../skills/easy-apply-tab-monitor/scripts/playwright_listing_binding.mjs";

const host = startPlaywrightSmartQueueDaemonHost(connectedContext, capabilities, {
  daemonArgs: [
    "--candidate-intake", "jobapply_agent/private/candidate_intake.json",
    "--database", "jobapply_agent/private/smart-queue.sqlite3",
    "--bridge-stdio",
  ],
});
```

The helper derives the supervisor binding identity from `capabilities.sessionId`.
It rejects a conflicting `bindingId` before spawning a daemon. If page creation
or navigation fails after it may have created a tab, the binding pauses future
opens. URL snapshots continue so the normal reliable-snapshot recovery path can
resolve the reservation. The host must remediate the browser and build a fresh
binding before opening additional listings.

## Codex and other coding agents

Codex hosts with an already-connected compatible browser binding use the
existing supervised entry point:

```js
import { startOrGetSmartQueueDaemonHost } from
  "../../skills/easy-apply-tab-monitor/scripts/smart_queue_daemon_host.mjs";

const host = startOrGetSmartQueueDaemonHost(connectedCodexBinding, {
  bindingId: "opaque-existing-session-id",
  daemonArgs: [
    "--candidate-intake", "jobapply_agent/private/candidate_intake.json",
    "--database", "jobapply_agent/private/smart-queue.sqlite3",
    "--bridge-stdio",
  ],
});
```

The binding must either expose `listTabUrls()` and `openListing(url)`, or the
legacy Codex shape documented by the bridge. The supervised helper is required
for persistent stdio operation. It reports only redacted counts and keeps a
live singleton per runtime configuration.

An agent with an external bridge and no Node parent uses the standalone path:

```sh
python3 skills/easy-apply-tab-monitor/scripts/smart_queue_daemon.py \
  --candidate-intake jobapply_agent/private/candidate_intake.json \
  --database jobapply_agent/private/smart-queue.sqlite3 \
  --adapter external --adapter-command host-listing-bridge
```

The bridge command must implement `list-tabs` (JSON URL array on stdout) and
`open-listing <exact-canonical-url>`. It is an existing-session bridge, not a
browser launcher.

Before its initial browser preflight, the standalone daemon acquires the
database's private sibling lease and retains that lease through its complete
monitor run. A second standalone owner for the same queue fails closed; do not
start another monitor or record outcomes until the current runtime has stopped.

## Queue lifecycle and storage

Use the normal `discover.py admit-queue` workflow before monitoring. The daemon
never accepts candidates or searches when `search_needed` is positive. Private
queue and candidate-memory SQLite files remain under `jobapply_agent/private/`.
Candidate outcomes require the candidate’s explicit outcome and explicit
vacancy confirmation. The outcome recorder uses the shared queue lease, so it
fails closed while a live monitor owns that database; quiesce the monitor before
recording, then restart it.

## Verification record

Record transport, agent/runtime version, browser engine/version, storage scope,
and test result without raw URLs or browser state. Run synthetic Playwright
tests before live Playwright testing. Human observation follows the automated
session test. Test a Codex session separately; a Playwright result is not a
Codex computer-use result.
