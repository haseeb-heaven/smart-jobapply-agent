# Agent Queue Admission Design

## Objective

Close the Smart Job Queue refill loop without giving the core package or tab
watcher search, page-content, application, or browser-launch authority. After
an agent receives a positive `search_needed` count, it may use its own tools to
collect already-visible listing facts, run the existing evidence-first ranking,
and invoke one deterministic, agent-only admission command. The next bounded
tab-monitor tick opens only the resulting approved canonical listing URLs.

The candidate remains the pilot: no form fill, upload, attestation, apply
click, submit, or outcome inference is added.

## Design

1. Discovery remains offline and host-supplied. It accepts only the existing
   visible-listing payload format and current active intake; it performs no
   fetching, login, CAPTCHA handling, browser control, or application action.
2. A new admission operation reads only the current discovery export and
   accepts only current-revision, eligible, `recommended` rows with canonical
   supported listing URLs and no application actions.
3. The operation validates the complete export before durable mutation. Any
   malformed, stale, unsupported, duplicate, below-threshold, non-recommended,
   fixture, or conflicting row rejects the entire batch. The only non-error
   exclusion is an otherwise valid candidate suppressed by durable memory.
4. It converts the validated rows into versioned `QueueCandidate` values,
   initializes an empty queue with the active revision pair only when there is
   no durable conflict, suppresses through
   `CandidateMemory.filter_unsuppressed_candidates(..., queue=queue)`, then
   admits the returned candidates. Queue revision is rechecked immediately
   before insertion.
5. Admission acquires the monitor's exact sibling-file `DatabaseLease` before
   constructing or mutating either queue or memory and holds it through
   validation, revision binding, suppression, and insertion. It releases the
   lease on every path. Queue/memory scope binding is part of this operation:
   failure after a new binding rolls it back or fails before binding.
5. The operation emits only aggregate counts. URLs, candidate facts, discovery
   records, browser state, and memory contents remain private.

## Interface

The agent-facing CLI is a separate command in `jobapply_agent/scripts/discover.py`:

```sh
python jobapply_agent/scripts/discover.py admit-queue \
  --candidate-intake jobapply_agent/private/candidate_intake.json \
  --discovery-export jobapply_agent/private/discovery.jsonl \
  --queue-db jobapply_agent/private/smart-queue.sqlite3 \
  --memory-db jobapply_agent/private/candidate-memory.sqlite3
```

It neither starts the daemon nor accepts browser bridge data. Its successful
output is a count-only JSON object. The daemon continues to accept only its
existing bridge/timing configuration and can open only exact admitted URLs.

## Safety Rules

- Eligibility is decided before ranking and the existing evidence categories
  remain separate.
- Stale revisions, malformed exports, unsupported/noncanonical URLs,
  non-recommended decisions, below-threshold rows, test fixtures, duplicate
  identity, conflicting revisions, and suppression failures fail closed with
  no partial admission or durable revision/memory mutation.
- Suppression is mandatory and precedes `add_recommendations`.
- Intake, queue DB, and memory DB must be regular non-symlink paths under
  `jobapply_agent/private/`; the discovery export must be an ignored,
  repository-local runtime file. Aliases, symlink escapes, and queue/memory
  identity collisions fail closed. Success and failure output stay redacted.
- The core package never fetches job pages or launches/controls a browser.
- The existing bridge remains limited to URL listing and reopening an exact
  approved listing URL; it never interacts with an application flow.

## Acceptance Criteria

- A fresh capacity-five queue can accept five valid current discovery rows,
  suppress duplicates/history, and a synthetic URL-only monitor cycle opens
  exactly five approved distinct URLs.
- A later capacity deficit is replenished only from admitted candidates; no
  direct recommendation payload enters the daemon or bridge.
- A suppressed, stale, malformed, unsupported, or conflicting row produces no
  admission, revision/memory mutation, or open action.
- Admission and monitor activity are serialized through one queue lease.
- Re-running an identical valid command is idempotent: no duplicate jobs or
  events, with count-only output.
- CLI and daemon output remain count-only, and the full verification gate
  passes.

## Testing

Unit tests cover export validation, conversion, revision seeding, suppression
ordering, idempotency, and lease contention. Integration tests cover an empty
capacity-five queue through admission and a synthetic bounded URL-only opening
cycle. Existing direct-stdio tests prove that no browser content or application
operation becomes available.

## Boundaries

- Always: use current active intake, deterministic ranking, durable suppression,
  and count-only output.
- Ask first: new external search providers, browser capabilities, dependencies,
  or changes to application policy.
- Never: automatic applications, form interaction, browser launch, private data
  in Git, or URL/candidate data in public status output.
