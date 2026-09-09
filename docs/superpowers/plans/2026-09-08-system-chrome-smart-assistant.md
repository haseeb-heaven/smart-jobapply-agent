# System Chrome Smart Assistant Implementation Plan

> Implement using the subagent-driven-development skill and AGENTS.md squad identities. User has authorized execution and live testing without further questions.

**Goal:** Maintain five managed LinkedIn/Indeed listing tabs in the current system Chrome, automatically finding validated distinct replacements when inventory is exhausted.

**Architecture:** A host-side loop invokes one finite standalone external daemon tick, then a bounded public-search provider when inventory is short, then the existing deterministic discovery/admission commands. Provider has web-search-only authority, never private candidate context, browser control, shell, approvals or queue access. Existing core retains eligibility, score and suppression authority. No use of the unfinished stdio ownership broker.

**Tech stack:** Python 3.12 local host; Node existing-session CDP bridge; optional hardened read-only Codex public-search provider. macOS and installed Chrome only for this delivery.

## Intake and constraints

- Branch: feature/portable-agent-browser-runtime; existing dirty portability changes preserved. No commit/push/merge until gate and review.
- Target five; user explicitly permits closing three test-created tabs for a live 5 -> 2 -> 5 test. Never close existing personal tabs or application tabs.
- No form actions, login, upload, submission, inferred outcomes, fake listing facts, lowered thresholds, queue reset or history deletion.
- Same private approved intake, queue and memory. New test databases separate from real candidate history.
- Production browser authority remains list URLs/open exact approved canonical URL only. Test-only closure uses exact IDs created by its harness.
- Prior broad persistent Codex automatic-approval launch was rejected. The approved replacement is bounded public search only, read-only sandbox, approvals never, shell/apps/plugins/browser/computer/delegation disabled.
- Healthy empty process is not task completion. Record actual visible managed counts and live refill evidence.

### Task 1: Host replenishment loop

Files: create skills/easy-apply-tab-monitor/scripts/external_smart_queue_assistant.py; skills/job-copilot/references/public-search-provider.schema.json; tests/test_external_smart_queue_assistant.py.

Interfaces: provider argv consumes bounded public search criteria JSON on stdin and returns one strict JSON public-listing batch. No candidate/profile/browser/state paths in provider input. Parent consumes validated public facts, creates mapping for existing committed SearchProfile URLs, runs discover.py, then admit-queue with authenticated SAME queue/memory.

- [ ] Independent tests establish shortage -> provider -> discover -> suppression/admit -> immediate finite daemon tick.
- [ ] Hold separate host singleton lock for the loop; run finite daemon and admission sequentially under their existing DB leases. Never overlap providers or start duplicate queue owners.
- [ ] Parse count-only daemon status only after successful exit. On shortage query provider with bounded timeout/output/rounds; reject malformed, extra-key, unsupported URL and private-shaped output. Filter already-managed canonical URLs without changing history. Never accept worker scores/eligibility.
- [ ] Loop with bounded backoff when no eligible jobs, explicit shutdown, process timeout cleanup and redacted status. Emit useful counts, not false success.
- [ ] Test failure/nonzero/timeout/oversize/schema/duplicate/busy/no-progress/shutdown and actual synthetic 5 -> 2 -> 5 lifecycle.

### Task 2: Narrow local search provider and run recipe

Files: create skills/easy-apply-tab-monitor/scripts/codex_public_search_provider.py (optional host adapter) and docs/runtime/system-chrome-assistant.md; update skill routing only as needed.

- [ ] Spawn Codex with ignore-user-config, ephemeral, read-only sandbox, approval_policy=never, web_search=live, shell_tool/unified_exec/apps/plugins/browser_use/browser_use_external/computer_use/multi_agent/view_image disabled; retain code_mode_host for the web tool runtime. No auto-approve/full-access flags.
- [ ] Use closed JSON response schema and bounded public-only query. Parent extracts validated last output; worker cannot modify files or act on browser/queue. Capture verbose output only privately and report generic failure/counts.
- [ ] Confirm a real public-web request works. Initial one-shot approved probe successfully found a public Wissen listing; it is not yet admitted or claimed live.
- [ ] Document exact local launch and stop commands, 15-second reconciliation, search latency, source exhaustion and machine/session-lifetime limits.

### Task 3: Assurance and real Chrome exercise

Files: create tests/browser/system-chrome-live.mjs (or equivalent test-only harness), ignored report under jobapply_agent/output/agent-runs/system-chrome-smart-assistant/.

- [ ] Independent Critic challenge and Fixer dispositions before final Reviewer.
- [ ] Test Runner runs ./scripts/verify.sh on frozen identified worktree. Fix failures, then rerun as required; capture exact totals.
- [ ] In existing system Chrome create isolated synthetic test tabs through exact approved LinkedIn/Indeed fixtures, use separate synthetic private queue/memory, close only three IDs created by harness, verify three distinct replacements and five total managed tabs, zero inferred outcomes; preserve unrelated tabs. Clearly label synthetic browser evidence versus real listing evidence.
- [ ] Run real public search for both boards, deterministic admission with current approved intake, launch actual assistant and verify steady process plus visible managed listings. Do not count redirected/expired listing pages as fulfilled inventory.
- [ ] Final independent review maps every criterion to evidence. Report remaining external source/capability blockers honestly; never claim infinite uptime or completion without observed acceptance.

### Task 4: Local Chrome listing redirect identity

Files: create skills/easy-apply-tab-monitor/scripts/system_chrome_listing_bridge.mjs and tests/test_system_chrome_listing_bridge.mjs.

- [ ] Existing-session-only localhost CDP adapter, list-tabs/open-listing only; never launch a browser, inspect content, close tabs, or access application controls.
- [ ] Open the exact approved canonical URL. Persist the exact returned target ID and approved URL in an ignored per-queue private binding file.
- [ ] For that exact created target only, report its approved URL when its current URL is still a strictly supported listing with the same board job ID. This accommodates LinkedIn country/slug redirects without changing core canonical URLs or global suppression semantics.
- [ ] Never substitute an application, login, unrelated, unsupported-query, or different-job URL. Preserve raw URL snapshots for unbound targets. Fail closed on malformed state or ambiguous IDs.
- [ ] Bound requests and startup; protect private path/state; atomic updates. Commands for a binding file are serialized by its queue host. Tests cover redirect acceptance, non-listing refusal, different-job refusal, unbound target preservation, exact opening, and malformed state.
- [ ] Verify real opened listing survives a later daemon tick. Existing tabs and historical queue rows are not rewritten or deleted.
