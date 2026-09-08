# Browser Live-Test Runbook

For the MVP, run Chromium only after the synthetic contract suite and
`./scripts/verify.sh` pass. Firefox and WebKit are deferred expansion targets;
do not count them as MVP support. Use synthetic, candidate-approved test listings and an isolated,
already-open browser session. Never use a personal or active application tab.

## 1. Playwright session and human observation

1. Record the host agent, Playwright/browser versions, runtime environment, and
   an opaque evidence ID. Do not record URLs, cookies, screenshots, or browser
   state in Git.
2. The human supplies a selected existing `BrowserContext`; create the bounded
   binding with its validated capability descriptor.
3. Admit two synthetic candidates using the normal active-intake and
   `discover.py admit-queue` workflow. Start the supervised host. Confirm its
   sequence is `running`, then `ready`, then `healthy`.
4. The human confirms two approved listing tabs appear. The adapter must only
   use URL snapshots; it must not inspect the page.
5. The human closes one managed test tab. Confirm it is released, a distinct
   already-admitted candidate fills the slot, and outcome/memory count remains
   zero.
6. Stop/quiesce the monitor. The human explicitly confirms `submitted`,
   `rejected`, or `skipped` and confirms the tab is vacated. Run the outcome
   recorder, restart monitoring, and confirm exact URL suppression persists.
7. Disconnect the browser session. Confirm the host becomes unhealthy and
   creates no replacement browser/session/tab. A human explicitly reconnects
   before another run.

## 2. Codex session

Perform this after the Playwright/human record exists. Record the actual Codex
browser transport used. A Chrome extension, DevTools connection, and
computer-use surface are different transports and must not be conflated.

Repeat the same sequence using the actual connected Codex binding. If the
environment only supplies screenshots/clicks and no structured complete URL
snapshot plus exact-open operation, mark the case `blocked`; do not use OCR or
page interaction as a substitute.

## 3. Evidence and failure handling

Create an ignored local evidence record conforming to
`browser-evidence.schema.json`. `human_manual: passed` requires an opaque human
signoff reference. A missing engine/session or blocked browser capability is a
real result and must remain `blocked` or `not_run`, never `passed`.

The candidate owns every application action. A closed tab does not produce an
outcome. Do not log private candidate data, URLs, browser content, or secrets.
