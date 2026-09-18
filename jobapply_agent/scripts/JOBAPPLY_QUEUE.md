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

| `tabs N` | Set the managed tab capacity, `N` = 1–10 |
| `open [--ticks N]` | Run bounded reconciliation cycles (default 1) |
| `search --search-command "..." [--admit]` | Run your read-only search, stage the export privately, optionally admit it |
| `admit FILE` | Admit a validated discovery export into the queue |
| `watch [--interval-seconds S] [--duration 30m] [--search-command "..."] [--tabs N]` | Persistent host loop: each cycle reconciles tabs, and when the queue reports `search_needed` with a configured search command, it re-searches, admits, and refills so closed tabs reopen |
| `outcome JOB_ID --outcome submitted\|skipped\|rejected` | Record your explicit application outcome |
| `log [--lines N]` | Tail the watch status log |

## Common runs

```sh
# See everything: states, capacity, open tabs, confirmed applied
python3 jobapply_agent/scripts/jobapply_queue.py status

# Keep 3 managed listing tabs
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
| `--tabs N` | Managed tab capacity, 1–10 |

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