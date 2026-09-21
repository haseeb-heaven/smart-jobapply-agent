# Smart JobApply Agent — Operational Guide

This guide explains how to set up your browser, run the Smart Queue CLI, open managed LinkedIn Easy Apply job tabs, and run the automatic monitoring and replenishment loop.

---

## 1. How It Works (Co-Pilot Model)

- **You are the Pilot**: You review the job requirements, fill out any details, and click "Submit Application". The agent **never** submits applications autonomously or handles login credentials.
- **The Agent is your Co-Pilot**: It finds matching jobs based on your verified intake profile, validates eligibility, scores them deterministically, opens **5 tabs** in your Google Chrome browser, monitors when you close or complete them, and immediately refills empty slots with fresh recommendations.

---

## 2. Setting Up Google Chrome

You have two options for connecting Chrome:

### Option A (Recommended): Use Normal Google Chrome (Zero Setup)
You can simply use your normal, already-running Google Chrome window:
1. Open Google Chrome normally.
2. Log into your LinkedIn account.
3. The Smart Queue uses the built-in macOS AppleScript bridge (`jobapply_agent/private/chrome_applescript_bridge.py`) to query open tabs and open approved job URLs. No special flags, ports, or profile switches are required.

### Option B: Launch Chrome with Remote Debugging (Port 9222)
If you prefer running Chrome with the CDP / Remote Debugging protocol on port `9222`:

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --remote-allow-origins='*' \
  --user-data-dir=/tmp/chrome-real-profile \
  --no-first-run
```

> **Note**: Chrome v136+ prevents opening port 9222 on the default profile if Chrome is already active. The `--user-data-dir=/tmp/chrome-real-profile` flag ensures a clean debugging endpoint.

---

## 3. Running the Smart Queue CLI

Run all commands from the repository root:

```bash
cd /Users/haseeb-mir/Documents/Code/smart-jobapply-agent
```

### Step 1: Health Check (`doctor`)
Verify that the queue database, candidate intake, and browser connection are working:

```bash
python3 jobapply_agent/scripts/jobapply_queue.py --bridge-command "python3 jobapply_agent/private/chrome_applescript_bridge.py" doctor
```
*Expected output: `ready: True`, `existing_session: True`.*

---

### Step 2: Check Queue Status (`status`)
View current capacity, number of open tabs, and queue states:

```bash
python3 jobapply_agent/scripts/jobapply_queue.py --bridge-command "python3 jobapply_agent/private/chrome_applescript_bridge.py" status
```

---

### Step 3: Set Capacity (`tabs`)
Set the number of managed job tabs to keep open simultaneously (e.g., 5).
Requires capacity authorization from your active candidate intake (`approved_facts["targets.smart_queue_capacity"]`).

```bash
python3 jobapply_agent/scripts/jobapply_queue.py --bridge-command "python3 jobapply_agent/private/chrome_applescript_bridge.py" tabs 5
```

---

### Step 4: Open 5 Managed Job Tabs (`open`)
Perform a single reconciliation pass that opens 5 recommended LinkedIn job listings in your Chrome browser.
Requires capacity authorization from your active candidate intake.

```bash
python3 jobapply_agent/scripts/jobapply_queue.py --bridge-command "python3 jobapply_agent/private/chrome_applescript_bridge.py" open
```

---

### Step 5: Start Continuous Monitoring & Auto-Refill (`watch`)
Run the persistent monitoring loop. Every 15 seconds, it checks your Chrome tabs. Whenever you finish or close a job tab, it automatically opens the next recommended listing.
Requires capacity authorization from your active candidate intake.

```bash
python3 jobapply_agent/scripts/jobapply_queue.py \
  --bridge-command "python3 jobapply_agent/private/chrome_applescript_bridge.py" \
  watch \
  --interval-seconds 15 \
  --search-command "python3 jobapply_agent/private/linkedin_search_refill.py"
```

To stop watching, press `Ctrl + C`. Stopping the watcher leaves your browser tabs intact.

### Capacity Authorization (New Feature)

The live reconciliation daemon validates your managed tab capacity against your **active candidate intake**. It accepts the documented default of 5 at any time, but it refuses a different size unless your intake explicitly approves it.

To approve capacity 3, add to `candidate_intake.json`:

```jsonc
"approved_facts": {
  "targets.smart_queue_capacity": 3
}
```

Live cycles (`open`, `watch`) fail fast with an actionable error if your queue holds a different capacity than your intake approves. Run `status` (or `doctor`) to check `capacity_live_authorized`.

---

## 4. Recording Completed Applications (`outcome`)

When you complete an application manually and close its tab, record the outcome into candidate memory so it won't be recommended again:

```bash
python3 jobapply_agent/scripts/jobapply_queue.py outcome <JOB_ID> --outcome submitted
```

Available outcomes:
- `submitted` — You completed and submitted the application.
- `skipped` — You reviewed the job and chose not to apply.
- `rejected` — You chose not to proceed with the role.

---

## 5. Summary of Files

| File | Description |
| --- | --- |
| `COMMANDS.txt` | Quick copy-paste command reference. |
| `HOW_TO_RUN.md` | This complete operational documentation. |
| `jobapply_agent/scripts/jobapply_queue.py` | Main CLI orchestration entrypoint (capacity authorization added). |
| `jobapply_agent/private/chrome_applescript_bridge.py` | macOS AppleScript bridge for Google Chrome. |
| `jobapply_agent/private/linkedin_search_refill.py` | Automated refill script for unadmitted jobs. |
| `jobapply_agent/private/candidate_intake.json` | Candidate verified profile and approved facts. |
| `jobapply_agent/private/smart-queue.sqlite3` | Local queue state database. |
