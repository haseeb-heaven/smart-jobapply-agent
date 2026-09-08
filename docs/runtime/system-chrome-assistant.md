# System Chrome assistant

Run the public provider only through the bounded host loop. It receives public
queries only; it never receives candidate data, queue paths, browser state, or
application authority. With blank location, use generic India, UAE, and global
remote public queries. Results are leads, not fit decisions: preserve stated
requirements, alternatives/preferred qualifications, and unknowns for the
deterministic admission workflow.

The host sends only `schema_version`, `limit`, and public `queries` containing
`query_id`, `platform`, `keywords`, and `location`. The provider validates the
closed public schema and query binding. The host separately validates canonical
listing URLs, applies deterministic eligibility before ranking, checks durable
candidate-memory suppression, and admits eligible results. A web snippet is
only partial evidence and never establishes an unseen requirement or fit.
The provider gathers multiple distinct listings per query when accessible,
within the batch limit. Explicit work-mode synonyms are normalized to `on-site`,
`hybrid`, or `remote`; an unstated mode remains empty. Location retains city,
state, and country only when visible on the source page.

One-shot provider protocol (public facts on stdout, no queue/browser action):

```sh
printf '%s' '{"schema_version":1,"limit":5,"queries":[{"query_id":"q0","platform":"linkedin","keywords":"Python backend","location":""},{"query_id":"q1","platform":"indeed","keywords":"Python backend","location":""}]}' | python3 skills/easy-apply-tab-monitor/scripts/codex_public_search_provider.py
```

The optional Codex integration starts an ephemeral read-only worker with
`--ignore-user-config`, `approval_policy="never"`, and `web_search="live"`.
Shell, unified exec, apps, plugins, browser use (including external browser use),
computer use, delegation, and image viewing are disabled. `code_mode_host`
remains available for public web tools. Its working directory is `/private/tmp`;
the worker receives no intake, queue, memory, resume, or browser data. The cloud
service receives the public query and public listing context, so this is not
an entirely offline search. No auto-approval or full-access flag is used.

The provider reads at most 16 KiB of input and drains stdout/stderr under a
combined 1 MiB cap in memory. It accepts at most 128 KiB of final JSON, extracted
from Codex's final `item.completed` event with `item.type="agent_message"` and
`item.text`. Verbose worker events and diagnostics are discarded; errors return
exit status 2 without printing their contents. A 300-second worker deadline and
process-group cleanup bound execution. The host timeout is 310 seconds.

With an already-running Chrome session and an explicitly approved, existing-session
external bridge, run the foreground host from the repository root. The tracked
`system_chrome_listing_bridge.mjs` supports `list-tabs` and
`open-listing <exact-approved-url>` through the fixed existing loopback Chrome
endpoint. It never starts a browser, inspects page content, closes tabs, or acts
on forms. The private directory must already exist with mode `0700`; the bridge
creates its dedicated binding file with mode `0600` automatically:

```sh
python3 skills/easy-apply-tab-monitor/scripts/external_smart_queue_assistant.py \
  --candidate-intake jobapply_agent/private/candidate_intake.json \
  --queue-db jobapply_agent/private/smart-queue.sqlite3 \
  --memory-db jobapply_agent/private/candidate-memory.sqlite3 \
  --interval-seconds 15 \
  --provider-timeout-seconds 310 \
  --max-rounds 1 \
  --bridge-command node skills/easy-apply-tab-monitor/scripts/system_chrome_listing_bridge.mjs jobapply_agent/private/smart-queue-chrome-bindings.json \
  --provider-command python3 skills/easy-apply-tab-monitor/scripts/codex_public_search_provider.py
```

Stop the foreground host with Ctrl-C. For a separately supervised host, send
`kill -TERM <verified-host-pid>` to that exact host PID. It stops its owned
children; it never closes the candidate's browser tabs. Add `--max-cycles 1`
for a bounded single-cycle check.

The host invokes standalone external daemon ticks and sleeps 15 seconds between
completed cycles. Public search runs synchronously when supply is short and
can add up to five minutes per attempt; 15 seconds is therefore not a guaranteed
wall-clock refill latency. A cycle searches at most the selected number of
rounds. Source exhaustion, inaccessible public evidence, browser/session
termination, unsupported redirects, and no eligible admitted listings remain shortages.
The process requires the machine to stay awake, its supervisor/terminal to remain
alive, the approved browser session to remain available, and the search service
to remain reachable. It is not an always-on remote service. Closing a tab never
records an application outcome.

The tracked bridge retains each created target ID and its exact approved URL
only in that ignored binding file. A supported redirect on the same target to
the same board job ID is reported as the original approved URL. Unbound targets
retain their observed URL. Different jobs, application/login routes, unsupported
query parameters, fragments, explicit ports, and malformed paths are never
substituted. This mapping changes neither core canonicalization nor exact-URL
candidate-memory suppression, queue records, or outcomes.

Use a dedicated binding file for this queue under the host's existing queue
lock; do not share it among independently running hosts. A reliable snapshot
removes absent target bindings, while a failed snapshot preserves them for
recovery. Browser restarts may invalidate target IDs: the bridge cannot assume
that a restored tab retains its earlier binding. Private files must not be
modified concurrently by an untrusted process running as the same user.

Bridge requests have a shared seven-second deadline and bounded response sizes;
only URL listing and exact approved listing opening are available. Offline tests
cover supported redirects and restart-style invocations, but live listing
survival across a later daemon tick must be verified in the actual session
before claiming the deployment is working.
