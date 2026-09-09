# Portable Agent and Browser Runtime Implementation Plan

> **For agentic workers:** Use the available `subagent-driven-development` or `executing-plans` skill at implementation time, subject to `AGENTS.md`. Steps use checkboxes for tracking. This document is a proposal; no implementation or runtime test is authorized by its creation.

**Goal:** Make Smart JobApply usable by any coding agent that supplies the required execution and browser capabilities, with explicit Playwright and Codex bindings, durable candidate history, and independently recorded automated and human validation.

**Architecture:** Retain the deterministic Python engine and private SQLite queue/memory. Put browser-specific code exclusively in the bounded tab-monitor skill. Connect host-owned sessions through one tested listing interface and the existing supervised Node host or standalone external argv bridge.

**Tech stack:** Python >=3.11, SQLite, JavaScript ES modules, Node test runner, pytest, coverage, Ruff; Playwright as an optional host/test dependency. Pin the tested Node and Playwright versions in the implementation PR and its lockfile after compatibility verification.

**Planning baseline:** 2026-09-08; inspected branch `develop`, initially clean. No browser session, installed extension, cloud runtime, or passing suite is asserted by this plan. Implementation starts on `feature/portable-agent-browser-runtime` from freshly inspected `develop`.

## 1. Global constraints and measurable outcome

- Core code in `jobapply_agent/src` and `jobapply_agent/scripts` has no browser or network authority.
- Candidate owns every application action. Never click application controls, fill, upload, accept attestations, authenticate, or submit.
- Production adapters attach to an existing host-owned session; they cannot launch a browser, create a context/window, or close tabs. A new listing tab inside that session is permitted.
- Browser authority is two operations: list URLs and open an exact approved canonical listing URL. Supported boards remain LinkedIn and Indeed.
- Eligibility precedes ranking. Unknown evidence stays unknown. Professional, project, and learning evidence remain distinct.
- Queue capacity is candidate-selected 1–10, default 5; unrelated tabs never count.
- Admission must filter through `CandidateMemory.filter_unsuppressed_candidates(candidates, queue=queue)` before adding returned candidates.
- Outcomes require an explicit candidate outcome plus vacancy confirmation. Missing tabs release slots without inventing outcomes.
- Runtime intake, databases, browser data, and candidate records stay in ignored private storage.
- Preserve both documented startup paths, legacy import aliases, strict NDJSON framing, redacted status, and health semantics.
- “Any agent/browser” means a published conforming capability contract. A screenshot-only tool without reliable URL snapshots cannot pass this contract.
- Agent-operated live tests and human-performed manual tests are separate evidence categories. Neither substitutes for the other.

Acceptance IDs:

| ID | Required outcome | Evidence required before release |
|---|---|---|
| A1 | Generic coding-agent operation | Two different real host agents follow the same runbook on synthetic intake; same deterministic results |
| A2 | Playwright operation (MVP) | Contract tests plus a real Chromium test-context run; Firefox and WebKit are deferred expansion evidence |
| A3 | Codex operation | Existing binding regression tests plus real connected Codex browser run |
| A4 | Cloud operation | Explicit durable storage and existing browser capability check in the actual cloud environment |
| A5 | Browser choice | Adapter conformance matrix records each browser/OS/transport/version and evidence level |
| A6 | Durable history | Restart, crash recovery, revision changes, suppression, and atomic outcome tests |
| A7 | Concurrency safety | Independent-process duplicate startup and delayed browser-operation tests |
| A8 | Human validation | Human-observed Playwright pass first, then Codex pass, recorded by the human |
| A9 | Regression safety | Authoritative `./scripts/verify.sh`, browser suite, independent review, privacy audit |
| A10 | Accurate support claims | Missing prerequisites are `blocked`/`not_run`; no skipped test is counted as browser support |

## 2. Verified current structure and gaps

| Existing location / symbol | Current responsibility | Proposed treatment |
|---|---|---|
| `skills/easy-apply-tab-monitor/scripts/smart_queue_daemon_host.mjs`: `toBridgeBinding`, `startOrGetSmartQueueDaemonHost` | Generic `listTabUrls/openListing` or legacy Codex binding, process supervision, readiness | Extend carefully; retain startup semantics |
| `codex_chrome_extension_host.mjs`: `canonicalListingUrl`, `startCodexChromeExtensionHost` | URL validation, bounded stream protocol, Codex tab handoff | Preserve aliases and transport; add parity cases |
| `browser_tab_adapter.py`: `ExternalCommandAdapter` | Two-command argv/JSON transport with bounded errors | Keep protocol compatible |
| `browser_bridge_adapter.py`: `StdioBridgeAdapter` | Strict ID-correlated NDJSON | Reuse, do not add a third transport |
| `smart_queue_daemon.py` | Intake/private-path preflight and daemon | Acquire runtime ownership before mutation |
| `smart_queue_coordinator.py`, `persistent_smart_queue_monitor.py` | Reconciliation and recovery | Preserve snapshot recovery boundary |
| `jobapply_agent/src/jobapply_agent/smart_queue.py` | Jobs, metadata, events, reservations, atomic outcomes | Add only justified ownership support; retain state model |
| `candidate_memory.py` | Durable queue-scoped URL suppression | Preserve scope and revision rules |
| `jobapply_agent/scripts/discover.py` | Deterministic discovery/admission | Reuse canonical admission in every host recipe |
| `tests/test_codex_chrome_extension_host.mjs` | Extensive stream, fake-binding, supervisor regression | Retain; fake-binding coverage is not live Codex proof |
| `tests/test_model_agnostic_bridge.py` | Generic stdio/external contract | Extend parity coverage |
| `scripts/verify.sh` | Nine-stage gate, explicit Node test file | Include new Node contracts; keep browser checks separately visible |

There is no dedicated Playwright binding in the inspected tracked tree. Generic compatibility exists, but a generic interface and fake tests do not prove any actual browser integration. The legacy Codex-shaped binding is repository code; its presence does not establish that the current Codex host exposes that API. Inspect the runtime at implementation time and map its actual supported API explicitly.

The current bridge checks raw session tabs before filtering unsupported URLs. A session with only `about:blank` already preflights and yields an empty managed listing set. Preserve this as a regression requirement; it does not require a framing change. A truly empty raw snapshot still fails the existing-session check.

## 3. Target runtime boundaries

```text
Coding agent: approved intake, evidence interpretation, host tool selection
    |
    +--> deterministic discovery --> queue-scoped suppression --> admission
    |
    +--> host-owned existing session
             |
             +-- Playwright context binding
             +-- actual Codex browser binding
             +-- other conforming host binding / external argv bridge
                         |
                  supervised host / bounded transport
                         |
                  Python daemon --> coordinator --> private SQLite queue
                                                    private SQLite memory
```

No LLM SDK belongs in the engine. A CLI-only agent can prepare evidence and run deterministic operations; persistent monitoring additionally needs a maintained browser connection and process lifecycle. A cloud host without a browser can perform offline operations but cannot claim live queue support. Durable storage must survive the declared session lifecycle; ephemeral filesystem storage qualifies only for a clearly labelled disposable test.

### Capability descriptor (new host-side type)

Create `skills/easy-apply-tab-monitor/scripts/browser_capabilities.mjs`. Expose `validateCapabilities(value)` returning a frozen descriptor or a generic typed error. Validate all keys, literal values, integer version, and nonempty opaque session ID; reject extras. This descriptor documents host guarantees; it does not cryptographically prove them.

```js
const capabilityExample = Object.freeze({
  protocolVersion: 1,
  sessionId: "opaque-session-01",
  existingSession: true,
  completeUrlSnapshots: true,
  exactListingOpen: true,
  persistentConnection: true,
});
```

Session identity stays host-local. It must change when a new browser context/session is selected, and cannot be derived from cookies or private URLs. Keep operational status count-only; do not add this descriptor to daemon stdout.

### Listing binding contract

```ts
export interface ListingBinding {
  listTabUrls(): Promise<string[]>;
  openListing(url: string): Promise<void>;
}
```

At the host boundary, `listTabUrls()` describes a complete snapshot of the explicitly selected session, including non-listing URLs if required by the existing transport’s session check. Filter non-listings inside the bridge before crossing into Python. Never log the raw array. An enumeration error, closed context, malformed entry, oversized snapshot, or interrupted enumeration is an error, not an empty snapshot. Do not silently truncate results.

For a future listing-filtered binding, carry session existence in a separate validated host-local preflight method; do not introduce a fake URL sentinel. Version that internal extension and retain legacy bindings. The first implementation can preserve the current complete-session array contract and avoid changing framing.

## 4. File-by-file delivery map

All names below marked “create” are proposed, not existing APIs.

| Task | Files to create | Files to modify |
|---|---|---|
| T1 contracts | `skills/easy-apply-tab-monitor/scripts/browser_capabilities.mjs`; `tests/test_browser_capabilities.mjs` | `skills/job-copilot/references/browser-capabilities.md` |
| T2 Playwright | `skills/easy-apply-tab-monitor/scripts/playwright_listing_binding.mjs`; `tests/test_playwright_listing_binding.mjs` | `skills/easy-apply-tab-monitor/SKILL.md` |
| T3 host recipes | `docs/runtime/agent-browser-setup.md`; `tests/test_agent_runtime_contract.py` | `smart_queue_daemon_host.mjs`; `tests/test_codex_chrome_extension_host.mjs`; `README.md` |
| T4 ownership | `tests/test_runtime_ownership.py`; `skills/easy-apply-tab-monitor/scripts/runtime_ownership.py`; `skills/easy-apply-tab-monitor/scripts/runtime_ownership_host.mjs` | `persistent_smart_queue_monitor.py`: `DatabaseLease`; `smart_queue_daemon.py`; `smart_queue_daemon_host.mjs`; `jobapply_agent/scripts/record_candidate_outcome.py` |
| T5 durability | `tests/test_portable_runtime_recovery.py`; `docs/runtime/storage-and-recovery.md` | `tests/test_candidate_memory.py`; `tests/test_record_candidate_outcome.py`; production storage only for proven findings |
| T6 browser integration | `tests/browser/package.json`; `tests/browser/package-lock.json`; `tests/browser/playwright.config.mjs`; `tests/browser/listing-binding.spec.mjs`; `tests/browser/queue-cycle.spec.mjs`; `tests/browser/fixture_runtime.py` | `scripts/verify.sh`; `.gitignore` |
| T7 live/human validation | `docs/testing/browser-live-runbook.md`; `docs/testing/browser-evidence.schema.json`; `tests/test_browser_evidence.py` | browser capability document; `CONTRIBUTING.md` |
| T8 release gate | `.github/workflows/portable-runtime.yml`; `docs/runtime/support-matrix.md` | `scripts/verify.sh`; `CHANGELOG.md`; `README.md` |

Paths abbreviated above resolve within `skills/easy-apply-tab-monitor/scripts/`. Do not refactor the 2,000-line queue module merely to add a browser binding. Keep each test author’s files distinct from implementer-owned focused tests.

## 5. Ordered execution and staffing

Indicative engineering effort: 7–12 working days after implementation is requested; availability of a real Codex cloud session and a human tester may extend elapsed time. Estimates are not promises or evidence. No work starts automatically from this plan.

| Phase | Dependencies | Effort | Exit |
|---|---|---|---|
| Intake and independent discovery | none | 0.5–1 day | Current diff, exact APIs, risk findings, ownership contract |
| T1–T3 contracts and bindings | discovery resolved | 2–3 days | Unit contracts, backward compatibility, host recipes |
| T4–T5 ownership and durability | session contract | 1.5–2.5 days | Process contention and crash matrix passes |
| T6 deterministic browser integration | T2–T5 | 1–2 days | Three-engine synthetic integration evidence |
| T7 live Playwright then Codex + human | previous gates | 1–2 days | Ordered real-session evidence and human signoff |
| T8 assurance and handoff | findings fixed | 1–1.5 days | Reviewed diff, gate, support matrix, PR into develop |

Use distinct Investigator, Critic, Implementer, Unit-test Author, Fixer, Reviewer, and Test Runner identities. Four available slots mean serialize later waves while preserving identities. Investigator and Critic investigate independently. Implementer and Unit-test Author edit disjoint files. Critic challenges the integrated change before ownership transfers to Fixer. Reviewer is read-only and cannot approve authored changes. Test Runner executes the authoritative gate. Coordinator maps all acceptance IDs to evidence. Use the exact YAML handoff and JSON final schema from `AGENTS.md` and persist only redacted reports under `jobapply_agent/output/agent-runs/portable-agent-browser-runtime/`.

## 6. T1 — freeze capabilities and compatibility contract

**Consumes:** current generic and legacy binding shapes. **Produces:** `validateCapabilities(value)` and documented support prerequisites.

- [ ] Add tests rejecting false session claims, unsupported versions, unexpected fields, empty IDs, nonboolean capabilities, and malformed descriptors.
- [ ] Run `node --test tests/test_browser_capabilities.mjs`; establish failure before adding the module.
- [ ] Implement strict validation using the following complete function shape.

```js
export function validateCapabilities(value) {
  const keys = ["protocolVersion", "sessionId", "existingSession",
    "completeUrlSnapshots", "exactListingOpen", "persistentConnection"];
  if (!value || typeof value !== "object" || Array.isArray(value)
      || Object.keys(value).length !== keys.length
      || !keys.every((key) => Object.hasOwn(value, key))
      || value.protocolVersion !== 1
      || typeof value.sessionId !== "string"
      || !/^[A-Za-z0-9_-]{1,128}$/.test(value.sessionId)
      || !keys.slice(2).every((key) => value[key] === true)) {
    throw new TypeError("browser capabilities unavailable");
  }
  return Object.freeze({ ...value });
}
```

```js
import assert from "node:assert/strict";
import test from "node:test";
import { validateCapabilities } from
  "../skills/easy-apply-tab-monitor/scripts/browser_capabilities.mjs";
test("rejects incomplete snapshots", () => {
  assert.throws(() => validateCapabilities({
    protocolVersion: 1, sessionId: "session-1", existingSession: true,
    completeUrlSnapshots: false, exactListingOpen: true,
    persistentConnection: true,
  }), /capabilities unavailable/);
});
```

- [ ] Pass tests and document that actual connected-session checks are still required.
- [ ] Test Runner runs `./scripts/verify.sh` before the focused commit; Reviewer inspects changes.

## 7. T2 — implement the injected Playwright binding

**Consumes:** host-selected existing Playwright `BrowserContext`; current canonicalizer. **Produces:** `createPlaywrightListingBinding(context): ListingBinding`.

Import no Playwright package from production code: structural dependency injection keeps Playwright optional and permits an already connected host. Host setup selects the context explicitly. Never choose `browser.contexts()[0]` silently, scan endpoints, discover remote debugging ports, or launch a production browser.

The code below specifies the minimal adapter behavior; delayed-operation fencing and ownership are additional acceptance requirements in T4. Do not claim it alone solves interruption races.

```js
import { canonicalListingUrl } from "./codex_chrome_extension_host.mjs";

export function createPlaywrightListingBinding(context) {
  if (!context || typeof context.pages !== "function"
      || typeof context.newPage !== "function") {
    throw new TypeError("existing browser context required");
  }
  function pages() {
    const values = context.pages();
    if (!Array.isArray(values) || values.length === 0 || values.length > 512
        || values.some((page) => !page || typeof page.url !== "function"
          || typeof page.isClosed !== "function" || page.isClosed())) {
      throw new Error("browser snapshot unavailable");
    }
    return values;
  }
  return Object.freeze({
    async listTabUrls() {
      try {
        const urls = pages().map((page) => page.url());
        if (urls.some((url) => typeof url !== "string"
            || url.length === 0 || url.length > 8192)) {
          throw new Error("invalid snapshot");
        }
        return urls;
      } catch {
        throw new Error("browser snapshot unavailable");
      }
    },
    async openListing(url) {
      const canonical = canonicalListingUrl(url);
      if (canonical !== url) throw new Error("listing URL must already be canonical");
      try {
        pages();
        const page = await context.newPage();
        await page.goto(canonical, { waitUntil: "commit", timeout: 10000 });
      } catch {
        throw new Error("listing open unavailable");
      }
    },
  });
}
```

Opening a tab and navigating it are separate external operations. A failed navigation may leave a blank tab; never close it as cleanup. A returned navigation does not prove queue state: the follow-up URL snapshot is authoritative. Redirects to login or applications are not inspected or followed with additional actions; the listing does not count as visible and the candidate handles the restriction.

Bound failed-open growth: after the first ambiguous page-creation/navigation failure in an ownership session, latch the binding into a state that rejects further opens without creating tabs. URL snapshots may continue for recovery. Report a generic host-side `browser_open_paused` diagnostic outside count-only daemon stdout. Resume only after the human/host has resolved the failed page and explicitly establishes a fresh binding after quiescence. Add ten repeated-open tests proving at most one new page after the first failure and zero automatic closes. This circuit breaker is part of the completed adapter; the minimal example above must be extended with a closure-scoped failure latch before release.

- [ ] Unit-test exact opens and URL rejection before `newPage()`.
- [ ] Test context closure, empty session, page enumeration exception, malformed URL, 513-tab overflow, `newPage()` rejection, and navigation rejection without URL/error leakage.
- [ ] Test that a session containing only an unrelated URL preflights successfully but passes zero managed URLs to Python.
- [ ] Use a fake context whose forbidden methods throw; assert no cookie/storage/DOM/locator/close/context-creation call.
- [ ] Run `node --test tests/test_playwright_listing_binding.mjs` through red/green, then reviewer and authoritative gate before commit.

Representative test:

```js
test("rejects application URLs before creating a tab", async () => {
  let created = 0;
  const binding = createPlaywrightListingBinding({
    pages: () => [{ url: () => "about:blank", isClosed: () => false }],
    newPage: async () => { created += 1; throw new Error("must not run"); },
  });
  await assert.rejects(binding.openListing("https://www.linkedin.com/jobs/view/123/apply/"));
  assert.equal(created, 0);
});
```

## 8. T3 — host recipes for Codex, other agents, and cloud

**Consumes:** T1 capabilities, T2 binding, existing supervised host. **Produces:** tested setup recipes and clear environment preflight failures.

All examples are host integration code. `connectedContext` and `connectedCodexBrowser` must be supplied by the actual host; they are not globals invented by the project.

```js
import { createPlaywrightListingBinding } from
  "./skills/easy-apply-tab-monitor/scripts/playwright_listing_binding.mjs";
import { startOrGetSmartQueueDaemonHost } from
  "./skills/easy-apply-tab-monitor/scripts/smart_queue_daemon_host.mjs";

export function startWithPlaywright(connectedContext) {
  return startOrGetSmartQueueDaemonHost(
    createPlaywrightListingBinding(connectedContext),
    { daemonArgs: [
      "--candidate-intake", "jobapply_agent/private/candidate_intake.json",
      "--database", "jobapply_agent/private/smart-queue.sqlite3",
      "--bridge-stdio",
    ] },
  );
}

export function startWithCodex(connectedCodexBrowser) {
  return startOrGetSmartQueueDaemonHost(connectedCodexBrowser, {
    daemonArgs: [
      "--candidate-intake", "jobapply_agent/private/candidate_intake.json",
      "--database", "jobapply_agent/private/smart-queue.sqlite3",
      "--bridge-stdio",
    ],
  });
}
```

For agents with an external bridge executable, retain exactly the documented standalone route. Example executable name below is a host-supplied requirement, not a bundled command:

```sh
python3 skills/easy-apply-tab-monitor/scripts/smart_queue_daemon.py \
  --candidate-intake jobapply_agent/private/candidate_intake.json \
  --database jobapply_agent/private/smart-queue.sqlite3 \
  --adapter external --adapter-command host-listing-bridge
```

`host-listing-bridge list-tabs` returns one bounded JSON URL array; `host-listing-bridge open-listing <canonical-url>` opens that URL and reports exit status. Its persistent session belongs to the host, not each subprocess. Do not route external mode through a Node parent.

Cloud preflight checklist for the future implementer: supported Python/Node; writable ignored private directory; durable volume surviving the claimed restart boundary; existing selected browser session; structured complete URL list; exact listing-tab open; connection lifetime sufficient for monitoring; host ability to observe process exit. If any requirement is missing, return a precise setup blocker before queue mutation. Cloud support is not established by local Linux CI.

- [ ] Add fixture parity tests comparing generic and Codex-shaped bindings for matching request/response results.
- [ ] Preserve ambiguous-binding rejection and legacy handoff requirements.
- [ ] Verify a new wrapper object does not cause duplicate startup for the same active configuration.
- [ ] Test connection loss before first status, invalid status, partial frame, stdout end, terminal status, and failed initialization; none are healthy.
- [ ] Update documentation with runtime-specific setup only after inspecting the actual exposed API. Screenshot computer use alone must report missing structured snapshot capability.
- [ ] Run focused host tests, independent review, and `./scripts/verify.sh` before commit.

## 9. T4 — process ownership and interruption safety

**Consumes:** durable queue path, session identity, supervisor lifecycle. **Produces:** one active runtime owner per queue and a tested replacement policy.

The current Node singleton is process-local. The repository also already has `DatabaseLease` in `persistent_smart_queue_monitor.py`, used by monitoring and admission. Reuse that serialization authority. The work here is to verify and, where necessary, extend ownership across pending parent-side browser operations; it is not to invent a second lock or claim that existing independent processes are entirely unlocked.

Proposed interface for an extracted ownership helper, only if required:

```python
from typing import Protocol

class RuntimeOwnership(Protocol):
    def assert_owned(self) -> None: ...
    def __enter__(self) -> "RuntimeOwnership": ...
    def __exit__(self, exc_type, exc_value, traceback) -> None: ...
```

This protocol defines the intended lifetime, not a replacement lock implementation. Extend the existing `DatabaseLease` with `assert_owned()` only if an ownership fence needs it; otherwise retain its API. Prove platform behavior, descriptor inheritance, release behavior, and private-path validation against the existing implementation. Lock files contain no candidate data. Do not introduce timestamp-based lock stealing or PID reuse assumptions.

The process that can still issue browser side effects must own the lock for that entire lifetime. For stdio, that is the Node supervisor; a child-only lock is insufficient if the parent still has a pending open. Define the lock helper protocol so the parent retains a live ownership process/handle and cannot replace it until both child and outstanding bridge operations finish. Standalone external mode keeps ownership in its supervising Python process until all bridge subprocesses finish.

Concrete ownership design for implementation: add a Python ownership broker that reuses `DatabaseLease` and launches the existing daemon as its child; the Node host starts the broker as part of its existing supervised stdio startup. The broker does not relay or gain browser authority. It owns the OS descriptor and provides a separate inherited private control pipe between Node, broker, and daemon. Queue construction waits for broker acquisition and successful browser preflight. The daemon receives a broker-managed ownership object on that inherited channel; ordinary CLI startup cannot disable locking or assert ownership using a flag. Do not recursively acquire the same lease in the broker-owned daemon. Standalone external mode retains direct Python startup and acquires the same lease before queue construction.

Control frames are bounded, strict, versioned JSON on the private control channel: `acquire`, `acquired`, `quiesce`, `drained`, `release`, `released`; opaque IDs correlate replies. No candidate facts, URLs, paths, or tokens enter frames. Acquisition errors return `busy` or `unavailable`. On normal stop, Node stops new dispatch, waits for all browser promises to settle, sends `drained`, waits for daemon exit, sends `release`, and waits for `released` before another runtime can start. Child exit alone never releases broker ownership while browser work remains.

On Node/control-pipe loss, the broker stops the daemon and retains its lock in an orphaned state because pending remote operations may still complete. Do not automatically expire/steal this state. Recovery requires the host to invalidate or demonstrably quiesce the old browser connection, then an explicit operator-controlled broker shutdown; persist an opaque recovery-required marker so broker/host machine crashes also require that check before takeover. The marker is not the locking mechanism and its presence can only block startup. This conservative recovery contract intentionally sacrifices unattended restart after ambiguous remote failure. Test orphaned-owner recovery; do not claim that process exit proves remote cancellation.

All mutators share the lease: monitor, admission, and the candidate-outcome recorder. Add the lease to the recorder around queue creation and the full attached transaction. Since monitoring holds lifetime ownership, the host must quiesce/release monitoring before admission or outcome recording, perform the leased mutation, then restart the supervised monitor. Record outcome-vs-monitor and outcome-vs-admission contention tests: contender makes no partial update, reports busy generically, and succeeds after owner release. Core database methods retain their existing transaction protection; orchestration and lease IPC remain outside the core.

Do not use timeouts as proof of cancellation. `Promise.race` may stop waiting while `newPage()` still completes. Mark an ambiguous runtime unhealthy, reject replacement while operations remain pending, and require host-confirmed quiescence if the browser transport cannot cancel/fence an in-flight mutation. Prefer availability loss over concurrent ownership. Document that exactly-once external browser effects cannot be guaranteed across an unacknowledged remote failure.

- [ ] Add two-process tests: owner wins, competitor gets redacted busy error, no second queue mutation or browser open.
- [ ] Add delayed `newPage()` and delayed `goto()` tests spanning stop/restart; no second owner starts while the first can mutate.
- [ ] Test normal stop, killed child, killed parent, disconnected browser, leaked descendant, and unavailable cancellation separately.
- [ ] Test session switch with same queue and reject it until previous runtime is quiescent.
- [ ] Reuse waiting-reservation recovery after verified ownership transfer; do not retry uncertain opens immediately.
- [ ] Run ownership and supervisor tests on all supported operating systems, then authoritative gate and review before commit.

## 10. T5 — database design, migrations, recovery, and scale

**Consumes:** existing queue/memory schema and active intake. **Produces:** preserved durable behavior, validated recovery runbook, regression tests.

Retain the existing tables:

| Database | Tables / data | Durability rule |
|---|---|---|
| Queue | `smart_queue_jobs`, `smart_queue_metadata` | Durable opaque queue ID and active revisions |
| Queue | `smart_queue_events`, `smart_queue_capacity_events`, `smart_queue_revision_events` | Append-only audit, immutable historical facts |
| Memory | `candidate_memory_outcomes`, `candidate_memory_queue_scope`, `candidate_memory_schema_versions` | Exact canonical URL suppression bound to durable queue |

The code already uses `BEGIN IMMEDIATE` and a 10-second busy timeout. Outcome finalization attaches the memory database to the queue connection and explicitly rejects WAL. Preserve rollback journal modes (`delete`, `truncate`, `persist`) because the attached two-database commit is the atomic boundary. Do not add WAL as a generic performance optimization.

```sql
-- Diagnostic examples against a synthetic/private local test pair only.
PRAGMA main.journal_mode;
PRAGMA main.integrity_check;
SELECT queue_id, active_profile_revision, active_matcher_policy_revision
FROM smart_queue_metadata WHERE metadata_id = 1;
-- Never copy result values into public CI logs or reports.
```

No schema migration is required for the Playwright binding itself. If ownership metadata is needed, keep it separate from immutable candidate history and version its schema explicitly. Migration procedure: stop/quiesce owner; validate private paths; take a consistent backup of both databases; validate integrity and scope; migrate within transactions; verify fixture compatibility; retain the backup for restoration. Never independently snapshot two live files and call the pair consistent.

Backups: quiesce all writers, use SQLite backup APIs for each database, validate both and matching scope before marking the pair usable; preserve restrictive permissions and keep the manifest private. Restore both together while stopped, then preflight active intake and perform the first reliable URL snapshot. Cloud volumes must support SQLite locking and durable filesystem semantics; do not put live SQLite on an unverified network share or synchronize a running database through Git/object storage.

Scale target for this release: one active runtime per candidate queue, 1–10 managed tabs, many independent candidate deployments with separate private directories and processes. This is not a multi-tenant SaaS design. Benchmark 1,000 and 10,000 historical rows with 100 admitted candidates: target p95 local reconciliation below 250 ms excluding browser latency on a documented CI machine, zero capacity violations, bounded memory growth. Treat targets as acceptance goals to measure, not current performance claims.

If measured demand later requires shared multi-host writers, create a separate design for a transactional server database, tenant isolation, authentication, migration, outbox/idempotency, and operational ownership. Do not partially substitute PostgreSQL while relying on SQLite `ATTACH` atomicity.

Regression scenarios and assertions:

- [ ] Reopen the same database pair after a confirmed outcome: exact URL remains suppressed.
- [ ] Advance profile/policy on the same queue: old URL suppression persists; stale batch is rejected.
- [ ] Pair memory with a different queue: fail before any scope/history mutation.
- [ ] Populated unscoped legacy memory: fail before migration.
- [ ] Inject failure before and during attached outcome commit: both outcome event and suppression row commit, or neither does.
- [ ] Reject WAL, disk-full, read-only storage, and lock timeout with redacted errors; do not continue admission.
- [ ] Snapshot fails after open: reservation stays waiting; next reliable initial snapshot resolves it.
- [ ] Absent stale waiting becomes open_failed; visible stale waiting becomes open; no outcome inferred.
- [ ] Human lowers capacity below current open count: no browser closes and no new opens.
- [ ] Run `python -m pytest tests/test_candidate_memory.py tests/test_record_candidate_outcome.py tests/test_portable_runtime_recovery.py`; authoritative gate and review before commit.

## 11. T6 — unit, integration, and regression browser testing

**Consumes:** real adapter plus synthetic fixtures. **Produces:** deterministic browser evidence separate from private live sessions.

Create a dedicated test-only npm project in `tests/browser/` with an exact Playwright version and committed lockfile. CI uses `npm ci`, never a floating latest dependency. Test fixtures may provision disposable browsers because the fixture host owns setup; production bindings still cannot launch anything. Test-only close/route operations must stay under `tests/browser/` and use synthetic pages only.

Playwright config example:

```js
import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: ".",
  testMatch: "*.spec.mjs",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30000,
  use: { headless: true, screenshot: "off", video: "off", trace: "off" },
  projects: [
    { name: "chromium", use: { browserName: "chromium" } },
    { name: "firefox", use: { browserName: "firefox" } },
    { name: "webkit", use: { browserName: "webkit" } },
  ],
});
```

Concrete engine integration example:

```js
import { test, expect } from "@playwright/test";
import { createPlaywrightListingBinding } from
  "../../skills/easy-apply-tab-monitor/scripts/playwright_listing_binding.mjs";

test("opens an exact listing inside the supplied context", async ({ context, page }) => {
  const url = "https://www.linkedin.com/jobs/view/123456";
  await context.route("**/*", async (route) => {
    if (route.request().url() === url) {
      await route.fulfill({ status: 200, contentType: "text/html",
        body: "<!doctype html><title>Synthetic listing</title><p>Fixture</p>" });
    } else {
      await route.abort();
    }
  });
  expect(page.url()).toBe("about:blank");
  const binding = createPlaywrightListingBinding(context);
  await binding.openListing(url);
  expect(await binding.listTabUrls()).toContain(url);
  expect(context.pages()).toHaveLength(2);
});
```

The synthetic route preserves the production URL validator and prevents actual board traffic. It proves browser behavior, not availability of a real listing or authenticated site behavior. No localhost bypass enters production validation.

Full queue-cycle integration must run the real supervised Node host and Python daemon, using a unique ignored private fixture directory for each case. `fixture_runtime.py` creates synthetic approved intake, derives valid candidates through deterministic discovery, passes suppression, and admits them. Reuse existing fixture builders from `tests/test_agent_workflow_integration.py` after inspecting their API; do not insert SQL recommendations directly.

Fixture location is specifically `jobapply_agent/private/browser-tests/<opaque-run-id>/` beneath the script's fixed private root, with owner-only directory permissions. Never substitute an arbitrary temporary root or add a production path override. Preserve failed-run fixtures for local diagnosis; delete only the exact fixture directory identified by its test-created manifest after all owners exit, and never traverse candidate-owned neighboring directories.

Test sequence: capacity 2; admit A/B/C; start daemon; await valid ready status; verify two listing tabs; test fixture simulates user closing A; next cycle opens C; verify A released and zero memory outcomes; explicitly invoke outcome recorder as a simulated user with dual confirmation; verify suppression and restart persistence. The simulation actor is labelled in evidence and never presented as a real candidate confirmation.

| Test layer | Coverage | Test owner |
|---|---|---|
| Unit | URL validation, adapter calls, malformed data, generic redaction | Unit-test Author |
| Protocol integration | NDJSON framing, limits, IDs, startup, health, external argv | Implementer focused tests + independent contracts |
| Storage integration | Admission, scope, rollback, crash, lock contention | Unit-test Author |
| Real-engine integration | Actual injected context + actual host/daemon + SQLite | Test Runner |
| Regression | All existing queue, intake, matcher, memory, portability tests | Test Runner |
| Live operational | Actual selected browser/session/agent | Agent operator |
| Human manual | Human closes a managed tab and verifies behavior | Candidate/test volunteer |

- [ ] Add all new Node unit files to stage 2 of `scripts/verify.sh` using an explicit maintained file list.
- [ ] Include test-only Playwright files in lint/syntax validation without importing Playwright into the core.
- [ ] Run `./scripts/verify.sh`, then from `tests/browser/` run `npx playwright test --config playwright.config.mjs` using the installed locked dependency.
- [ ] Missing browser dependencies fail the required browser CI job; never silently skip it.
- [ ] Report actual pass/fail/skipped totals and engine versions separately; do not equate coverage percentage with runtime support.

## 12. T7 — ordered live testing and human acceptance

**Consumes:** passing synthetic gates, selected real sessions, synthetic or candidate-approved inputs. **Produces:** explicit evidence for each environment tested.

No live testing is performed during planning. During implementation, begin with synthetic data and a dedicated user-provided test session. Never read an existing candidate’s private manifests merely to run a smoke test.

### Stage L1: live Playwright, then human observation

1. Record OS, agent, Playwright version, browser engine/build, selected session identifier privately, and storage persistence scope.
2. Human/host provides an already-open dedicated browser context. The adapter attaches only to that context.
3. Candidate approves test listing URLs and capacity 2; use canonical admission with synthetic candidate facts where possible.
4. Start through `startOrGetSmartQueueDaemonHost`. Observe `running`, then `ready`, then `healthy` correctly.
5. Verify two approved listing tabs are visible. Human checks titles/page availability; adapter reads URLs only.
6. Human closes one managed tab. Observe immediate slot release and refill from a distinct admitted candidate. Check outcome count remains zero.
7. Human states skipped and vacated for the managed test job. Quiesce/release monitoring, record that dual confirmation through the leased outcome CLI, verify suppression, and restart monitoring.
8. Stop/quiesce and restart against the same private pair. Verify history and no duplicate reopen.
9. Disconnect the session; verify unhealthy state and no replacement session/window. Human reconnects explicitly before another run.
10. Human records pass/fail for each observed behavior. Store no screenshots or raw tabs by default.

### Stage L2: real Codex browser/computer-use session

Run only after L1 results are recorded. Inspect capabilities in the actual Codex runtime. An installed Chrome extension, a DevTools process, and a Codex cloud computer are different possible transports; record which one was used. Do not relabel Playwright as Codex computer use.

Repeat L1 steps with the actual Codex-connected binding, including any real handoff semantics the host provides. If computer use exposes only screenshots/clicks, record the structured snapshot blocker; do not emulate URLs through OCR. Obtain a supported host tab API or conforming extension bridge before claiming persistent queue support.

### Stage L3: alternative agent and user-selected browser

Run the same contract through a second real coding agent. For Chrome/Edge, test the requested actual build; for Firefox/WebKit, record the tested runtime. WebKit CI is not Safari certification. Other browsers qualify only after a real conforming bridge pass. Test cloud separately, restart across its declared lifecycle, and verify persistence. Do not imply local Codex results establish cloud behavior.

### Evidence format

Create a strict JSON Schema with `additionalProperties: false` and required fields corresponding to this example. Validate enumerations and nonnegative counts. Evidence validator must reject `human_manual: passed` without a human signoff reference and reject `live: passed` without runtime metadata. References are opaque artifact IDs, not raw personal/browser data.

```json
{
  "schema_version": 1,
  "case_id": "live-refill-01",
  "agent": "codex",
  "transport": "codex-session",
  "environment": "local",
  "browser": "chrome",
  "runtime_versions": {"agent": "record-observed-version", "browser": "record-observed-version"},
  "automated": "not_run",
  "live": "not_run",
  "human_manual": "not_run",
  "human_signoff_ref": null,
  "opened_count": 0,
  "inferred_outcome_count": 0,
  "artifact_ref": "run-opaque-01"
}
```

Example intentionally starts `not_run`. Version strings are fields the future operator must populate from observed runtime metadata, not support claims.

- [ ] Add schema validation tests for unsupported transport, missing versions, invalid state, extra URL/token fields, and fabricated human pass without signoff.
- [ ] Execute L1, L2, L3 in order and record environment-specific blockers without suppressing them.
- [ ] Human feedback becomes a recorded finding; transfer affected file ownership to Fixer, repeat only affected tests plus required authoritative gate.

## 13. T8 — CI, review, rollout, and rollback

CI jobs: existing authoritative gate on supported Python/OS combinations; Node contracts on the chosen supported Node line; Linux Playwright Chromium for the MVP; Firefox/WebKit and selected macOS/Windows browser smoke runs are deferred expansion work; synthetic storage contention/recovery. Use exact committed dependency lockfiles. Keep private live Codex sessions out of public CI and record their evidence separately.

Required future command set:

```sh
./scripts/verify.sh
cd tests/browser
npm ci
npx playwright install --with-deps chromium firefox webkit
npx playwright test --config playwright.config.mjs
```

The browser install belongs to an explicit CI/test setup job, never the daemon or production adapter. OS-specific installation prerequisites are documented per runner.

Rollout: expose the new binding as opt-in; preserve old aliases; run synthetic and live evidence before changing support claims. Reviewer maps A1–A10 to diff/tests/evidence. Test Runner captures exact gate exit and totals. Commit focused changes only after the gate; open PR into `develop`; do not merge implicitly. Promote `main` only via the repository’s assurance process.

Rollback: stop the owner, wait for browser operations to quiesce, revert the opt-in binding selection to the previously validated binding, preserve queue/memory files, run reliable initial snapshot recovery. If a migration was introduced, restore the validated paired backup only through an explicit migration rollback procedure; never erase history to make a runtime start.

## 14. Final completion audit for future implementation

- [ ] Every acceptance ID has direct evidence, including actual cloud and human tests where claimed.
- [ ] No required Critic/Reviewer finding remains open.
- [ ] New adapters expose exactly the listing contract; core imports no browser or vendor library.
- [ ] All private test fixtures and reports remain ignored; no raw browser trace or candidate fact in tracked artifacts.
- [ ] `./scripts/verify.sh` passes with exact totals; new browser job passes with explicit engine versions.
- [ ] Live Playwright precedes live Codex; human manual results are separately attributed.
- [ ] Database pair survives restart, failed commit, revision change, and suppression scenarios.
- [ ] Cross-process startup and delayed browser mutation tests pass, with documented ambiguous-failure limitations.
- [ ] Support matrix lists `verified`, `contract_only`, `blocked`, or `not_run` per environment.
- [ ] Handoff contains task ID, branch, actual agent identities, findings, acceptance evidence, validation, privacy audit, remote verification, and remaining work as required by `AGENTS.md`.

## 15. Sources and planning limitations

Repository anchors above were inspected; implementation-time owners must recheck the then-current diff. External API references consulted for this plan:

- [Playwright BrowserContext](https://playwright.dev/docs/api/class-browsercontext): context-scoped pages and page creation support the injected adapter design.
- [Playwright BrowserType](https://playwright.dev/docs/api/class-browsertype): connection mechanisms differ; CDP is Chromium-specific and not a universal attachment recipe.
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html): durable multi-file transactions depend on the documented filesystem/journal assumptions.

This plan does not promise flawless operation, universal browser access, or exactly-once remote UI effects. It defines specific interfaces, failure handling, test evidence, and release criteria so those claims can be evaluated accurately. The deliverable is documentation only. Earlier investigation reported a focused Node test run; it is not the authoritative gate and is not evidence that the proposed system has been implemented or validated. No further test or browser execution is part of this planning scope.
