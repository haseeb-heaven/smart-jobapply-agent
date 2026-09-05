# Agent Queue Admission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a host agent one fail-closed command that admits current validated discovery recommendations into the durable queue so the existing bounded watcher can refill capacity.

**Architecture:** The command lives beside discovery, but never fetches pages or touches a browser. It validates a private JSONL discovery export as one atomic batch, converts rows to existing `QueueCandidate` objects, acquires the monitor's `DatabaseLease`, binds an empty queue to the active intake revisions, suppresses using `CandidateMemory`, then calls the existing queue admission API. The daemon remains unchanged as a count-only URL-list/open bridge.

**Tech Stack:** Python 3, SQLite, existing `SmartJobQueue`, `CandidateMemory`, `DatabaseLease`, pytest, Node direct-stdio contract tests.

## Global Constraints

- Candidates retain ownership of every application action; no form fill, upload, attestation, apply click, or submit is introduced.
- Core code has no browser launch, page-content inspection, fetching, login, CAPTCHA, or application authority.
- Admission accepts only repository-local ignored runtime data and emits count-only redacted output on both success and failure.
- Every batch is validated all-or-nothing; suppression is the sole non-error exclusion and must precede `add_recommendations`.
- Admission uses the exact monitor `DatabaseLease` across revision binding, memory suppression, and durable insertion.
- Private candidate, listing, browser, and email data never enter Git or public output.

---

### Task 1: Atomic queue revision binding

**Files:**
- Modify: `jobapply_agent/src/jobapply_agent/smart_queue.py`
- Test: `tests/test_smart_queue.py`

**Interfaces:**
- Produces `SmartJobQueue.bind_empty_queue_revisions(profile_revision: str, matcher_policy_revision: str) -> None`.
- It must reject unversioned values, any non-empty queue, any existing active pair other than the exact pair, and legacy/unversioned stored jobs.
- A later admission service calls this only while holding `DatabaseLease`.

- [ ] **Step 1: Write failing tests**

```python
def test_bind_empty_queue_revisions_sets_exact_pair(queue):
    queue.bind_empty_queue_revisions("profile-r1", "policy-r1")
    assert queue.active_revisions() == ("profile-r1", "policy-r1")

def test_bind_empty_queue_revisions_rejects_conflict_or_existing_job(queue, candidate):
    queue.bind_empty_queue_revisions("profile-r1", "policy-r1")
    with pytest.raises(QueuePolicyError):
        queue.bind_empty_queue_revisions("profile-r2", "policy-r1")
```

- [ ] **Step 2: Run the focused test**

Run: `python -m pytest -q tests/test_smart_queue.py -k bind_empty_queue_revisions`

Expected: failure because the method does not exist.

- [ ] **Step 3: Implement the smallest transactional method**

Use the queue's existing transaction/read-active-revisions/row helpers. Validate a complete revision pair, reject any stored queue row before writing, accept an absent pair or exact existing pair, and commit exactly one durable active pair.

- [ ] **Step 4: Verify focused tests**

Run: `python -m pytest -q tests/test_smart_queue.py -k bind_empty_queue_revisions`

Expected: pass.

- [ ] **Step 5: Commit Task 1**

```sh
git add jobapply_agent/src/jobapply_agent/smart_queue.py tests/test_smart_queue.py
git commit -m "Add atomic queue revision binding"
```

### Task 2: Fail-closed admission service and CLI

**Files:**
- Modify: `jobapply_agent/scripts/discover.py`
- Test: `tests/test_discover.py`
- Test: `tests/test_candidate_memory.py`

**Interfaces:**
- Produces `admit_current_recommendations_for_active_queue(candidate_intake, discovery_export, queue_db, memory_db) -> AdmissionStatus`.
- `AdmissionStatus` carries only `validated_count`, `suppressed_count`, and `admitted_count` non-negative integers.
- CLI subcommand: `admit-queue --candidate-intake PATH --discovery-export PATH --queue-db PATH --memory-db PATH`.

- [ ] **Step 1: Write failing admission tests**

```python
def test_admit_queue_validates_then_suppresses_then_admits(tmp_path, active_intake, export_rows):
    status = admit_current_recommendations_for_active_queue(...)
    assert status.validated_count == 5
    assert status.admitted_count == 5

def test_admit_queue_rejects_one_invalid_row_without_durable_mutation(...):
    with pytest.raises(AdmissionError):
        admit_current_recommendations_for_active_queue(...)
    assert queue_job_count(...) == 0
```

Include tests for malformed JSON, unknown/invalid row contract, duplicate canonical URL, duplicate fingerprint conflict, stale intake/profile/policy revisions, unsupported/noncanonical URL, fixture flag, application actions, decision/threshold/evidence mismatch, queue-memory path collision, symlink escape, memory scope mismatch, and idempotent rerun.

- [ ] **Step 2: Run focused failure tests**

Run: `python -m pytest -q tests/test_discover.py -k 'admit_queue or admission'`

Expected: failure because the API and command do not exist.

- [ ] **Step 3: Implement validation and serialization**

Validate every JSONL row into a temporary in-memory tuple before opening queue/memory state. Derive revisions from the validated active intake and current matcher policy; require every row to match. Use existing `canonical_listing_url`, `QueueCandidate`, and `DatabaseLease`. With the lease held: create/validate queue and memory private paths, bind an empty queue, call `filter_unsuppressed_candidates(candidates, queue=queue)` exactly once, recheck queue revisions, and call `add_recommendations` only with its result. Convert all expected errors to redacted `AdmissionError`; CLI prints one count-only JSON object or one redacted error token.

- [ ] **Step 4: Verify focused tests and lint**

Run: `python -m pytest -q tests/test_discover.py tests/test_candidate_memory.py -k 'admit_queue or admission or suppression'`

Run: `python -m ruff check jobapply_agent/scripts/discover.py tests/test_discover.py tests/test_candidate_memory.py`

Expected: pass.

- [ ] **Step 5: Commit Task 2**

```sh
git add jobapply_agent/scripts/discover.py tests/test_discover.py tests/test_candidate_memory.py
git commit -m "Add fail-closed queue admission command"
```

### Task 3: Serialized refill integration

**Files:**
- Test: `tests/test_smart_queue_coordinator.py`
- Test: `tests/test_persistent_smart_queue_monitor.py`
- Test: `tests/test_discover.py`

**Interfaces:**
- Consumes Task 2's admission service and the existing `SnapshotBrowser` / coordinator cycle.
- Proves a capacity-five queue opens only exact admitted URLs after a synthetic visible URL snapshot.

- [ ] **Step 1: Write failing end-to-end tests**

```python
def test_five_admitted_current_rows_refill_five_url_only_slots(...):
    admit_current_recommendations_for_active_queue(...)
    result = coordinator.cycle()
    assert result.opened_count == 5
    assert browser.opened_count == 5

def test_monitor_lease_blocks_admission_without_queue_or_memory_mutation(...):
    with held_monitor_lease(queue_db):
        with pytest.raises(AdmissionError):
            admit_current_recommendations_for_active_queue(...)
```

- [ ] **Step 2: Run focused failures**

Run: `python -m pytest -q tests/test_discover.py tests/test_smart_queue_coordinator.py tests/test_persistent_smart_queue_monitor.py -k 'admit_queue or refill_five or lease_blocks_admission'`

Expected: failure until Task 2 serialization/integration is complete.

- [ ] **Step 3: Add only fixture/test wiring required by the existing coordinator**

Use synthetic canonical URLs and `SnapshotBrowser`; do not introduce browser drivers, page content, or application controls. Assert suppressed rows never enter `opened_urls`; assert public status never includes URLs.

- [ ] **Step 4: Verify integration tests**

Run: `python -m pytest -q tests/test_discover.py tests/test_smart_queue_coordinator.py tests/test_persistent_smart_queue_monitor.py`

Expected: pass.

- [ ] **Step 5: Commit Task 3**

```sh
git add tests/test_discover.py tests/test_smart_queue_coordinator.py tests/test_persistent_smart_queue_monitor.py
git commit -m "Test serialized Smart Queue refill"
```

### Task 4: Agent handoff documentation

**Files:**
- Modify: `README.md`
- Modify: `skills/easy-apply-tab-monitor/SKILL.md`
- Test: `tests/test_codex_chrome_extension_host.mjs`

**Interfaces:**
- Documents the exact sequence: receive `search_needed` -> host agent gathers visible facts -> discovery -> `admit-queue` -> next existing-session-only monitor tick.

- [ ] **Step 1: Add documentation assertions/contract checks**

Add a Node source-contract assertion that daemon bridge arguments remain limited to `--bridge-stdio` and no admission payload/browser authority enters the direct bridge parent.

- [ ] **Step 2: Write concise agent-only handoff docs**

Include the CLI command, count-only success/failure behavior, private runtime path rule, suppression-before-admission ordering, and explicit prohibition on applying, form interaction, or browser launch.

- [ ] **Step 3: Verify docs contract**

Run: `node --test tests/test_codex_chrome_extension_host.mjs`

Expected: pass.

- [ ] **Step 4: Commit Task 4**

```sh
git add README.md skills/easy-apply-tab-monitor/SKILL.md tests/test_codex_chrome_extension_host.mjs
git commit -m "Document agent queue admission handoff"
```

### Final verification and live-safe smoke

- [ ] **Step 1: Reviewer inspects the actual diff**

Map every global constraint and admission criterion to test/code evidence. Reject any direct browser/application authority or private output.

- [ ] **Step 2: Test Runner executes the authoritative gate**

Run: `./scripts/verify.sh`

Expected: all stages pass from a cleanly identified worktree.

- [ ] **Step 3: Run a finite existing-Chrome smoke**

Use only `startOrGetCodexSmartQueueDaemonHost` with an already-connected Chrome binding and `--max-ticks 1`. Record only numeric tab counts, count-only status fields, exit code, and redacted health. Never launch a browser, close tabs, inspect page content, or apply.

- [ ] **Step 4: Commit and prepare review**

Commit only verified feature files under `Codex <codex@openai.com>`, push a non-destructive feature-compatible remote branch, and open a PR targeting `develop` after review evidence is complete.

## Plan Self-Review

- Spec coverage: Tasks 1-3 cover revision integrity, private path validation, suppression, lease serialization, count-only output, and synthetic five-slot refill; Task 4 covers the host-agent contract; final steps cover full and live-safe validation.
- Placeholder scan: no deferred implementation markers or unspecified tests remain.
- Type consistency: Task 2 defines the admission service and status consumed by Task 3; Task 1 defines the queue binding consumed by Task 2.
