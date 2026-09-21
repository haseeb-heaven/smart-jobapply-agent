# `jobapply_queue.py` — Smart Queue Command Reference

The host-side command-line tool for candidate-controlled job listing tabs.
It orchestrates the existing bounded queue machinery and adds no browser,
form, upload, or submission authority of its own.

## Starting Chrome (the only manual prerequisite)

The tool attaches to your **already-running** Chrome session. Start Chrome once
with remote debugging while you are logged in:

```sh
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 --remote-allow-origins='*' \
  --user-data-dir=/tmp/chrome-real-profile --no-first-run
```

> On Chrome v136+ with the default profile this is refused, so the run uses a
> symlinked data directory that reuses your real logged-in profile. Fully quit
> Chrome and relaunch normally from the Dock when you want normal mode back.

## One-line setup check

```sh
cd <repo-checkout>
python3 jobapply_agent/scripts/jobapply_queue.py doctor
```

`doctor` mutates nothing. `ready: True` means the queue, bridge, daemon, and
your Chrome session are all reachable.

## Commands

| Command | What it does |
| --- | --- |
| `status [--json]` | Queue counts by state, capacity, open listing-tab count, confirmed submitted |
- run this tool from the current working directory, which is the repository root

| `tabs N` | Set the managed tab capacity, `N` = 1–10 (live cycles accept it only if your intake approves `N`; see below) |
| `open [--ticks N]` | Run bounded reconciliation cycles (default 1; requires authorized capacity) |
| `search --search-command "..." [--admit]` | Run your read-only search, stage the export privately, optionally admit it |
| `admit FILE` | Admit a validated discovery export into the queue |
| `watch [--interval-seconds S] [--duration 30m] [--search-command "..."] [--tabs N]` | Persistent host loop: each cycle reconciles tabs, and when the queue reports `search_needed` with a configured search command, it re-searches, admits, and refills so closed tabs reopen (requires authorized capacity) |
| `outcome JOB_ID --outcome submitted\|skipped\|rejected` | Record your explicit application outcome |
| `log [--lines N]` | Tail the watch status log |

## Capacity and your intake

The live reconciliation daemon rebuilds capacity from your **active** candidate
intake. It accepts the documented default of five at any time, but it refuses a
*different* size unless your intake explicitly approves it. So to run 3 managed
tabs, your intake must approve capacity 3:

```jsonc
"approved_facts": {
  // ... your other confirmed facts ...
  "targets.smart_queue_capacity": 3
}
```

`tabs N` and `watch --tabs N` always record what you asked for. When the intake
approves `N`, the choice is bound to your intake revision and live cycles accept
it. Otherwise the value is stored as `host-configured`, which live cycles refuse,
and the command prints a warning naming the exact fact to approve. Run
`doctor` (or `status`) to see `capacity_live_authorized` at any time.

### Fail-fast capacity diagnostics for live cycles

Live cycles (`open` and `watch`) require authorized capacity and fail fast before
starting reconciliation if capacity is not authorized:

* **Unauthorized capacity:** If the queue holds a host-configured capacity that the
  active candidate intake does not approve, `open` and `watch` halt immediately
  without launching a daemon cycle. The error specifies the intake-approved
  capacity and provides the exact remediation command:
  ```text
  error: capacity is not authorized for live reconciliation cycles; active candidate intake approves 5. Run 'jobapply_queue.py tabs 5' to use the approved capacity, or approve targets.smart_queue_capacity in candidate_intake.json.
  ```
* **Unverifiable capacity:** If the intake file is missing, unreadable, or invalid,
  reconciliation cycles fail fast with actionable details rather than running with
  unverified capacity or failing closed deep inside the daemon child process.
* **Separation of readiness and authorization:** In `jobapply_queue.py doctor`,
  `ready: True` confirms that all prerequisites (queue database, bridge, daemon,
  browser session) are available, while `capacity_live_authorized` separately
  indicates whether live cycles are currently authorized to run.

## Common runs

```sh
# See everything: states, capacity, open tabs, confirmed applied
python3 jobapply_agent/scripts/jobapply_queue.py status

# Keep 3 managed listing tabs (requires approved_facts["targets.smart_queue_capacity"] = 3)
python3 jobapply_agent/scripts/jobapply_queue.py tabs 3

# One reconciliation cycle right now
python3 jobapply_agent/scripts/jobapply_queue.py open

# Run for 30 minutes, checking every 60 seconds
python3 jobapply_agent/scripts/jobapply_queue.py watch --duration 30m

# Run with automatic refill: re-search and admit when slots free up
python3 jobapply_agent/scripts/jobapply_queue.py watch --duration 2h \
  --search-command "node /path/to/search.mjs"

# When YOU applied to a managed job: record it (never inferred from a close)
python3 jobapply_agent/scripts/jobapply_queue.py outcome <JOB_ID> --outcome submitted
```

## Time controls

| Flag | Meaning |
| --- | --- |
| `--interval-seconds S` | Seconds between reconciliation cycles (default 60) |
| `--duration 90 / 30s / 10m / 2h` | Stop after that long; omit to run until Ctrl+C |
| `--ticks N` | How many bounded cycles `open` runs (default 1) |
| `--tabs N` | Managed tab capacity, 1–10; must match your intake-approved capacity to stay live |

Only non-negative integers are accepted; a bad duration stops instead of
silently running forever.

## Where state lives

* **Queue state:** `jobapply_agent/private/smart-queue.sqlite3` — append-only
events; each job is `recommended → waiting → open`, and a closed tab becomes
`missing`, which frees its slot immediately.
* **Application outcomes:** `jobapply_agent/private/candidate-memory.sqlite3`
— written **only** by your explicit `outcome` command, never by closing a tab.
* **Candidate facts:** `jobapply_agent/private/` — git-ignored, never committed.

## Safety rules (built in, not optional)

* The tool never clicks Apply / Easy Apply, never fills a form, never uploads
your documents, and never submits. You own every application action.
* The daemon itself never searches; searches happen only through the explicit
`--search-command` **you** supply to the host `watch` loop.
* A closed or missing tab is recorded as released capacity, never as an
application outcome. Only your explicit `outcome` command writes outcomes.
* Status output is counts and opaque IDs only: no listing URLs, snapshots,
candidate facts, or page content.