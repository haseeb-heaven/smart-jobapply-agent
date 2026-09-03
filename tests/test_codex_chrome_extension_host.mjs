/** Direct NDJSON contract tests for the existing-session-only Chrome bridge. */

import assert from "node:assert/strict";
import { spawn as spawnChildProcess } from "node:child_process";
import { createHash } from "node:crypto";
import { EventEmitter } from "node:events";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import test from "node:test";
import { PassThrough } from "node:stream";
import { fileURLToPath } from "node:url";

import { startCodexChromeExtensionHost } from "../skills/easy-apply-tab-monitor/scripts/codex_chrome_extension_host.mjs";
import {
  startCodexSmartQueueDaemonHost,
  startOrGetCodexSmartQueueDaemonHost,
} from "../skills/easy-apply-tab-monitor/scripts/codex_smart_queue_daemon_host.mjs";

const LINKEDIN = "https://www.linkedin.com/jobs/view/123456";
const INDEED = "https://in.indeed.com/viewjob?jk=abc_123";

function tab(url = "https://in.indeed.com/jobs?q=private") {
  return { url };
}

function chromeBinding({ tabs = [tab()], delay = null, newDelay = null, openTabsError = null } = {}) {
  const events = [];
  return {
    events,
    binding: {
      user: {
        async openTabs() {
          events.push(["openTabs"]);
          if (delay !== null) await delay;
          if (openTabsError !== null) throw openTabsError;
          return tabs;
        },
      },
      tabs: {
        async new() {
          events.push(["new"]);
          if (newDelay !== null) await newDelay;
          return {
            async goto(url) { events.push(["goto", url]); },
            async markHandoff() { events.push(["markHandoff"]); },
          };
        },
      },
      async click() { throw new Error("forbidden"); },
      async fill() { throw new Error("forbidden"); },
      async upload() { throw new Error("forbidden"); },
      async submit() { throw new Error("forbidden"); },
      async close() { throw new Error("forbidden"); },
    },
  };
}

async function exchange(binding, messages) {
  const input = new PassThrough();
  const output = new PassThrough();
  const received = [];
  output.on("data", (chunk) => received.push(Buffer.from(chunk)));
  const service = startCodexChromeExtensionHost(binding, { input, output });
  input.end(messages.map((message) => `${message}\n`).join(""));
  await service.finished;
  return Buffer.concat(received).toString("utf8").trim().split("\n").map((line) => JSON.parse(line));
}

test("requires explicit streams; it never falls back to process stdio", () => {
  const { binding } = chromeBinding();
  assert.throws(() => startCodexChromeExtensionHost(binding), /explicit input and output/);
});

test("lists only canonical supported listing URLs through direct NDJSON", async () => {
  const { binding, events } = chromeBinding({
    tabs: [
      tab("https://mail.example.test/inbox?message=private"),
      tab("https://www.linkedin.com/jobs/view/123456/?trk=public_jobs"),
      tab("https://in.indeed.com/viewjob?jk=abc_123&utm_source=search"),
    ],
  });
  assert.deepEqual(await exchange(binding, [JSON.stringify({ id: "list", operation: "list_tab_urls" })]), [
    { id: "list", ok: true, urls: [LINKEDIN, INDEED] },
  ]);
  assert.deepEqual(events, [["openTabs"]]);
});

test("preserves duplicate canonical managed listing URLs for downstream ambiguity checks", async () => {
  const { binding, events } = chromeBinding({
    tabs: [
      tab("https://www.linkedin.com/jobs/view/123456/?trk=public_jobs"),
      tab(LINKEDIN),
    ],
  });

  assert.deepEqual(await exchange(binding, [JSON.stringify({ id: "list", operation: "list_tab_urls" })]), [
    { id: "list", ok: true, urls: [LINKEDIN, LINKEDIN] },
  ]);
  assert.deepEqual(events, [["openTabs"]]);
});

test("valid operation failures echo their opaque request ID", async () => {
  const listFailure = chromeBinding({ openTabsError: new Error("private browser failure") });
  assert.deepEqual(await exchange(listFailure.binding, [JSON.stringify({ id: "list", operation: "list_tab_urls" })]), [
    { id: "list", ok: false, error: "request_failed" },
  ]);
  assert.deepEqual(listFailure.events, [["openTabs"]]);

  const missing = chromeBinding({ tabs: [] });
  assert.deepEqual(await exchange(missing.binding, [JSON.stringify({ id: "open", operation: "open_listing", url: LINKEDIN })]), [
    { id: "open", ok: false, error: "request_failed" },
  ]);
  assert.deepEqual(missing.events, [["openTabs"]]);

  const active = chromeBinding();
  assert.deepEqual(await exchange(active.binding, [JSON.stringify({ id: "open", operation: "open_listing", url: INDEED })]), [
    { id: "open", ok: true },
  ]);
  assert.deepEqual(active.events, [["openTabs"], ["new"], ["goto", INDEED], ["markHandoff"]]);
});

test("an empty existing session fails closed for list-tab preflight", async () => {
  const empty = chromeBinding({ tabs: [] });
  assert.deepEqual(await exchange(empty.binding, [JSON.stringify({ id: "preflight", operation: "list_tab_urls" })]), [
    { id: "preflight", ok: false, error: "request_failed" },
  ]);
  assert.deepEqual(empty.events, [["openTabs"]]);
});

test("invalid frames echo a valid opaque ID and otherwise use null", async () => {
  const { binding, events } = chromeBinding();
  assert.deepEqual(await exchange(binding, [
    "not-json",
    JSON.stringify({ operation: "list_tab_urls" }),
    JSON.stringify({ id: "bad", operation: "open_listing", url: "https://in.indeed.com/jobs?q=private" }),
    JSON.stringify({ id: "extra", operation: "list_tab_urls", extra: true }),
  ]), [
    { id: null, ok: false, error: "request_failed" },
    { id: null, ok: false, error: "request_failed" },
    { id: "bad", ok: false, error: "request_failed" },
    { id: "extra", ok: false, error: "request_failed" },
  ]);
  assert.deepEqual(events, []);
});

test("oversized ID-bearing frames echo their bounded opaque ID", async () => {
  const { binding, events } = chromeBinding();
  const frame = JSON.stringify({
    id: "oversized-request",
    operation: "list_tab_urls",
    ignored: "x".repeat(16 * 1024),
  });
  assert.deepEqual(await exchange(binding, [frame]), [
    { id: "oversized-request", ok: false, error: "request_failed" },
  ]);
  assert.deepEqual(events, []);
});

test("request handling is serialized", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const { binding, events } = chromeBinding({ delay: gate });
  const input = new PassThrough();
  const output = new PassThrough();
  const service = startCodexChromeExtensionHost(binding, { input, output });
  input.write(`${JSON.stringify({ id: "one", operation: "list_tab_urls" })}\n`);
  input.write(`${JSON.stringify({ id: "two", operation: "open_listing", url: LINKEDIN })}\n`);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(events, [["openTabs"]]);
  release();
  input.end();
  await service.finished;
  assert.deepEqual(events, [["openTabs"], ["openTabs"], ["new"], ["goto", LINKEDIN], ["markHandoff"]]);
});

test("direct bridge close fences a read-only openTabs wait before any mutation", async () => {
  let releaseOpenTabs;
  const stalledOpenTabs = new Promise((resolve) => { releaseOpenTabs = resolve; });
  const { binding, events } = chromeBinding({ tabs: [tab(LINKEDIN)], delay: stalledOpenTabs });
  const input = new PassThrough();
  const output = new PassThrough();
  const service = startCodexChromeExtensionHost(binding, { input, output });

  input.write(`${JSON.stringify({ id: "open", operation: "open_listing", url: LINKEDIN })}\n`);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(events, [["openTabs"]]);

  service.close();
  await bounded(service.finished, "fenced direct bridge completion");
  assert.equal(service.mutationPending, false);
  releaseOpenTabs();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(events, [["openTabs"]]);
});

function daemonChild() {
  const child = new EventEmitter();
  child.stdin = new PassThrough();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.exitCode = null;
  child.signalCode = null;
  child.killCalls = [];
  child.kill = (signal) => {
    child.killCalls.push(signal);
    child.signalCode = signal;
    queueMicrotask(() => {
      child.stdout.end();
      child.stderr.end();
      child.emit("close", null, signal);
    });
    return true;
  };
  return child;
}

function collectedLines(stream) {
  const chunks = [];
  stream.on("data", (chunk) => chunks.push(Buffer.from(chunk)));
  const ended = new Promise((resolve, reject) => {
    stream.once("end", resolve);
    stream.once("error", reject);
  });
  return {
    ended,
    parse() {
      return Buffer.concat(chunks).toString("utf8").trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
    },
  };
}

function waitForStatus(stream, predicate) {
  let remainder = "";
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      stream.off("data", onData);
      stream.off("end", onEnd);
      stream.off("error", onError);
    };
    const onData = (chunk) => {
      remainder += chunk.toString("utf8");
      const lines = remainder.split("\n");
      remainder = lines.pop();
      for (const line of lines) {
        if (line.length === 0) continue;
        const status = JSON.parse(line);
        if (!predicate(status)) continue;
        cleanup();
        resolve(status);
        return;
      }
    };
    const onEnd = () => {
      cleanup();
      reject(new Error("status ended before the synchronized fixture event"));
    };
    const onError = (error) => {
      cleanup();
      reject(error);
    };
    stream.on("data", onData);
    stream.once("end", onEnd);
    stream.once("error", onError);
  });
}

function bounded(promise, label, timeoutMilliseconds = 2000) {
  let timeout;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timeout = setTimeout(() => reject(new Error(`timed out waiting for ${label}`)), timeoutMilliseconds);
    }),
  ]).finally(() => clearTimeout(timeout));
}

function syntheticActiveIntake() {
  const approvedFacts = { "targets.smart_queue_capacity": 5 };
  const revisionInput = {
    activated_by: "user",
    approved_facts: approvedFacts,
    contradictions: [],
    documents: [],
    pending_facts: [],
    schema_version: 1,
    state: "active",
    unknown_fields: [],
  };
  return {
    schema_version: 1,
    documents: [],
    approved_facts: approvedFacts,
    unknown_fields: [],
    contradictions: [],
    pending_facts: [],
    state: "active",
    activated_by: "user",
    confirmed_at: "2026-01-01T00:00:00+00:00",
    revision_hash: createHash("sha256").update(JSON.stringify(revisionInput)).digest("hex"),
  };
}

function realDaemonFixture(source) {
  const processes = [];
  const options = {
    daemonArgs: [source, "--", "--bridge-stdio"],
    daemonPath: "--eval",
    pythonExecutable: process.execPath,
    spawn(executable, args, spawnOptions) {
      const child = spawnChildProcess(executable, args, spawnOptions);
      const stderrEnded = new Promise((resolve, reject) => {
        child.stderr.once("end", resolve);
        child.stderr.once("error", reject);
      });
      const exited = new Promise((resolve, reject) => {
        child.once("exit", resolve);
        child.once("error", reject);
      });
      processes.push({ child, stderrEnded, exited });
      return child;
    },
  };
  return {
    options,
    processes,
    async releaseAll() {
      for (const { child } of processes) {
        if (child.exitCode === null && child.signalCode === null && child.stdin.writable) {
          child.stdin.write("release\n");
        }
      }
      for (const processRecord of processes) {
        try {
          await bounded(processRecord.exited, "fixture child exit");
        } catch {
          if (processRecord.child.exitCode === null && processRecord.child.signalCode === null) {
            processRecord.child.kill("SIGKILL");
          }
          await bounded(processRecord.exited, "fixture child forced exit");
        }
      }
    },
  };
}

const CLEAN_EOF_DAEMON = String.raw`
process.stderr.end(() => {
  process.stdout.write('{"ticks_completed":1,"requested_open_count":0,"opened_count":0,"open_failed_count":0,"search_needed":0,"degraded_tick_count":0}\n', () => {
    setImmediate(() => { process.exitCode = 0; });
  });
});
`;

const DELAYED_EXIT_DAEMON = String.raw`
process.on("SIGTERM", () => {
  process.stdout.write('{"ticks_completed":2,"requested_open_count":0,"opened_count":0,"open_failed_count":0,"search_needed":0,"degraded_tick_count":0}\n');
});
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  if (!chunk.includes("release\n")) return;
  process.stdin.destroy();
  process.stdout.end(() => { process.exitCode = 0; });
});
process.stderr.end(() => {
  process.stdout.write('{"ticks_completed":1,"requested_open_count":0,"opened_count":0,"open_failed_count":0,"search_needed":0,"degraded_tick_count":0}\n');
});
`;

const DUPLICATE_COORDINATOR_PROBE = String.raw`
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

root = Path.cwd()
sys.path.insert(0, str(root / "jobapply_agent" / "src"))
sys.path.insert(0, str(root / "skills" / "easy-apply-tab-monitor" / "scripts"))

from codex_chrome_extension_adapter import CodexChromeExtensionAdapter
from smart_queue_coordinator import SmartQueueCoordinator
from jobapply_agent.smart_queue import QueueCandidate, QueuePolicyError, SmartJobQueue

private_root = root / "jobapply_agent" / "private"
private_root.mkdir(parents=True, exist_ok=True)
with TemporaryDirectory(prefix="node-coordinator-duplicate-", dir=private_root) as temporary:
    queue = SmartJobQueue(Path(temporary) / "smart-queue.sqlite3")
    candidate = QueueCandidate(
        job_id="duplicate-probe-job",
        source_url="https://www.linkedin.com/jobs/view/123456",
        fit_score=90,
        eligible=True,
        decision="recommended",
        evidence=("synthetic verified evidence",),
        profile_revision="duplicate-probe-profile",
        matcher_policy_revision="duplicate-probe-policy",
    )
    coordinator = SmartQueueCoordinator(queue, CodexChromeExtensionAdapter())
    try:
        coordinator.cycle((candidate,))
    except QueuePolicyError as error:
        if "duplicate" not in str(error):
            raise
        print(json.dumps({
            "ticks_completed": 1,
            "requested_open_count": 0,
            "opened_count": 0,
            "open_failed_count": 0,
            "search_needed": 0,
            "degraded_tick_count": 1,
        }), flush=True)
    else:
        raise SystemExit(3)
`;

test("the real Node host preserves duplicate managed URLs through the Python coordinator boundary", { timeout: 10000 }, async () => {
  const duplicateTabs = chromeBinding({
    tabs: [
      tab("https://www.linkedin.com/jobs/view/123456/?trk=public_jobs"),
      tab(LINKEDIN),
    ],
  });
  const host = startCodexSmartQueueDaemonHost(duplicateTabs.binding, {
    daemonArgs: [DUPLICATE_COORDINATOR_PROBE, "--bridge-stdio"],
    daemonPath: "-c",
    pythonExecutable: "python3",
  });

  assert.deepEqual(await bounded(host.finished, "duplicate coordinator probe"), {
    exitCode: 0,
    signalCode: null,
  });
  assert.deepEqual(duplicateTabs.events, [["openTabs"]]);
});

test("the real Node parent runs one actual daemon tick through a strict private stdio bridge", { timeout: 10000 }, async () => {
  const privateRoot = fileURLToPath(new URL("../jobapply_agent/private/", import.meta.url));
  await mkdir(privateRoot, { recursive: true });
  const runtime = await mkdtemp(join(privateRoot, "node-actual-daemon-"));
  const intakePath = join(runtime, "synthetic-active-intake.json");
  const databasePath = join(runtime, "synthetic-queue.sqlite3");
  await writeFile(intakePath, JSON.stringify(syntheticActiveIntake()), "utf8");

  const session = chromeBinding({ tabs: [tab(LINKEDIN)] });
  const rawChildStdout = [];
  const host = startCodexSmartQueueDaemonHost(session.binding, {
    daemonArgs: [
      "--candidate-intake", intakePath,
      "--database", databasePath,
      "--max-ticks", "1",
      "--bridge-stdio",
    ],
    daemonPath: fileURLToPath(new URL("../skills/easy-apply-tab-monitor/scripts/smart_queue_daemon.py", import.meta.url)),
    pythonExecutable: "python",
    spawn(executable, args, options) {
      const child = spawnChildProcess(executable, args, options);
      child.stdout.on("data", (chunk) => rawChildStdout.push(Buffer.from(chunk)));
      return child;
    },
  });
  const statuses = collectedLines(host.status);

  try {
    assert.deepEqual(await bounded(host.finished, "actual smart queue daemon exit", 5000), {
      exitCode: 0,
      signalCode: null,
    });
    await statuses.ended;
    assert.deepEqual(statuses.parse(), [
      {
        ticks_completed: 1,
        requested_open_count: 0,
        opened_count: 0,
        open_failed_count: 0,
        search_needed: 5,
        degraded_tick_count: 0,
      },
      { terminal: "exited" },
    ]);
    assert.deepEqual(session.events, [["openTabs"], ["openTabs"], ["openTabs"]]);
    assert.equal(host.health.running, false);
    assert.equal(host.health.ready, false);
    assert.equal(host.health.healthy, false);
    assert.doesNotMatch(JSON.stringify(statuses.parse()), /https:|synthetic-active-intake|sqlite|candidate/);
    const rawFrames = Buffer.concat(rawChildStdout).toString("utf8").trim().split("\n").filter(Boolean).map(JSON.parse);
    assert.ok(rawFrames.length > 0);
    for (const frame of rawFrames) {
      const keys = Object.keys(frame).sort();
      const finiteKeys = ["degraded_tick_count", "open_failed_count", "opened_count", "requested_open_count", "search_needed", "ticks_completed"];
      const unboundedKeys = ["degraded_count", "open_failed_count", "opened_count", "requested_open_count", "search_needed"];
      assert.ok(
        JSON.stringify(keys) === JSON.stringify(finiteKeys) || JSON.stringify(keys) === JSON.stringify(unboundedKeys),
        "raw child stdout must use a count-only status schema",
      );
      for (const value of Object.values(frame)) assert.ok(Number.isSafeInteger(value) && value >= 0);
    }
    assert.doesNotMatch(Buffer.concat(rawChildStdout).toString("utf8"), /https:\/\//);
  } finally {
    await rm(runtime, { recursive: true, force: true });
  }
});

test("the daemon parent uses only piped stdio, drains stdout, and publishes bounded count-only status", async () => {
  const child = daemonChild();
  let spawned;
  const { binding, events } = chromeBinding();
  const host = startCodexSmartQueueDaemonHost(binding, {
    daemonArgs: ["--bridge-stdio"],
    daemonPath: "/safe/smart_queue_daemon.py",
    pythonExecutable: "python3",
    spawn(executable, args, options) {
      spawned = { executable, args, options };
      return child;
    },
  });
  assert.deepEqual(spawned, {
    executable: "python3",
    args: ["/safe/smart_queue_daemon.py", "--bridge-stdio"],
    options: { shell: false, stdio: ["pipe", "pipe", "pipe"] },
  });
  assert.notEqual(host.status, child.stdout);
  const statuses = collectedLines(host.status);
  const replies = [];
  child.stdin.on("data", (chunk) => replies.push(chunk.toString("utf8")));
  const privateUrl = "https://www.linkedin.com/jobs/view/private-candidate";
  child.stdout.write(`${JSON.stringify({
    ticks_completed: 1,
    requested_open_count: 2,
    opened_count: 1,
    open_failed_count: 0,
    search_needed: 1,
    degraded_tick_count: 0,
    url: privateUrl,
    candidate: "sensitive",
  })}\n`);
  child.stdout.write(`${JSON.stringify({
    ticks_completed: 1,
    requested_open_count: 2,
    opened_count: 1,
    open_failed_count: 0,
    search_needed: 1,
    degraded_tick_count: 0,
  })}\n`);
  child.stdout.write(`${JSON.stringify({ id: "attempt", operation: "open_listing", url: privateUrl })}\n`);
  child.stderr.write(`${JSON.stringify({ id: "list", operation: "list_tab_urls" })}\n${JSON.stringify({
    id: "forbidden",
    operation: "open_listing",
    url: `${LINKEDIN}?candidate=private`,
  })}\n`);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(replies.join("").trim().split("\n").map((line) => JSON.parse(line)), [
    { id: "list", ok: true, urls: [] },
    { id: "forbidden", ok: false, error: "request_failed" },
  ]);
  assert.deepEqual(events, [["openTabs"]]);
  child.exitCode = 0;
  child.stdout.end();
  child.emit("close", 0, null);
  child.stderr.end();
  assert.deepEqual(await host.finished, { exitCode: 0, signalCode: null });
  await statuses.ended;
  assert.deepEqual(statuses.parse(), [{
    ticks_completed: 1,
    requested_open_count: 2,
    opened_count: 1,
    open_failed_count: 0,
    search_needed: 1,
    degraded_tick_count: 0,
  }, { terminal: "exited" }]);
  assert.doesNotMatch(JSON.stringify(statuses.parse()), /private|sensitive|open_listing/);
});

test("the daemon host reports a signal exit and stop is idempotent", async () => {
  const child = daemonChild();
  const { binding } = chromeBinding();
  const host = startCodexSmartQueueDaemonHost(binding, {
    daemonArgs: ["--bridge-stdio"],
    spawn() { return child; },
  });

  const first = host.stop();
  const second = host.stop();
  assert.equal(first, second);
  await first;
  assert.deepEqual(child.killCalls, ["SIGTERM"]);
  assert.deepEqual(await host.finished, { exitCode: null, signalCode: "SIGTERM" });
});

test("the daemon host exposes redacted terminal status for an early exit or spawn error", async () => {
  const { binding } = chromeBinding();
  const exited = daemonChild();
  const exitedHost = startCodexSmartQueueDaemonHost(binding, {
    daemonArgs: ["--bridge-stdio"],
    spawn() { return exited; },
  });
  const exitedStatuses = collectedLines(exitedHost.status);
  exited.exitCode = 2;
  exited.emit("exit", 2, null);
  exited.stdout.end();
  exited.stderr.end();
  assert.deepEqual(await exitedHost.finished, { exitCode: 2, signalCode: null });
  await exitedStatuses.ended;
  assert.deepEqual(exitedStatuses.parse(), [{ terminal: "exited" }]);
  assert.deepEqual(exitedHost.health.exit, { exitCode: 2, signalCode: null });
  assert.equal(exitedHost.health.running, false);

  const failed = daemonChild();
  const failedHost = startCodexSmartQueueDaemonHost(binding, {
    daemonArgs: ["--bridge-stdio"],
    spawn() { return failed; },
  });
  const failedStatuses = collectedLines(failedHost.status);
  failed.emit("error", new Error("private launch failure"));
  failed.stdout.end();
  failed.stderr.end();
  assert.deepEqual(await failedHost.finished, { exitCode: null, signalCode: null, terminalError: "spawn_error" });
  await failedStatuses.ended;
  assert.deepEqual(failedStatuses.parse(), [{ terminal: "spawn_error" }]);
  assert.doesNotMatch(JSON.stringify(failedHost.health), /private/);
});

test("daemon health rejects partial status and terminal hosts are never ready or healthy", async () => {
  const child = daemonChild();
  const { binding } = chromeBinding();
  const host = startCodexSmartQueueDaemonHost(binding, {
    daemonArgs: ["--bridge-stdio"],
    spawn() { return child; },
  });

  assert.equal(host.health.running, true);
  assert.equal(host.health.ready, false);
  assert.equal(host.health.healthy, false);
  assert.equal(host.health.latestStatus, null);

  child.stdout.write(`${JSON.stringify({ url: "https://www.linkedin.com/jobs/view/private-status" })}\n`);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(host.health.running, true);
  assert.equal(host.health.ready, false);
  assert.equal(host.health.healthy, false);
  assert.doesNotMatch(JSON.stringify(host.health), /https:|private-status/);

  child.stdout.write(`${JSON.stringify({ ticks_completed: 1, opened_count: 0, search_needed: 0 })}\n`);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(host.health.running, true);
  assert.equal(host.health.ready, false);
  assert.equal(host.health.healthy, false);

  child.stdout.write(`${JSON.stringify({
    ticks_completed: 1,
    requested_open_count: 0,
    opened_count: 0,
    open_failed_count: 0,
    search_needed: 0,
    degraded_tick_count: 0,
  })}\n`);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(host.health.running, true);
  assert.equal(host.health.ready, true);
  assert.equal(host.health.healthy, true);

  child.exitCode = 0;
  child.stdout.end();
  child.emit("close", 0, null);
  child.stderr.end();
  assert.deepEqual(await host.finished, { exitCode: 0, signalCode: null });
  assert.equal(host.health.running, false);
  assert.equal(host.health.ready, false);
  assert.equal(host.health.healthy, false);
  assert.notEqual(host.health.terminal, null);
  assert.doesNotMatch(JSON.stringify(host.health), /https:|private-status/);
});

test("an invalid status after a valid status clears readiness until a later valid frame", async () => {
  const child = daemonChild();
  const { binding } = chromeBinding();
  const host = startCodexSmartQueueDaemonHost(binding, {
    daemonArgs: ["--bridge-stdio"],
    spawn() { return child; },
  });
  const validStatus = {
    ticks_completed: 1,
    requested_open_count: 0,
    opened_count: 0,
    open_failed_count: 0,
    search_needed: 0,
    degraded_tick_count: 0,
  };

  child.stdout.write(`${JSON.stringify(validStatus)}\n`);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(host.health.ready, true);
  assert.equal(host.health.healthy, true);

  child.stdout.write('{"ticks_completed":2}\n');
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(host.health.running, true);
  assert.equal(host.health.ready, false);
  assert.equal(host.health.healthy, false);
  assert.equal(host.health.invalidStatusCount, 1);

  child.stdout.write(`${JSON.stringify({ ...validStatus, ticks_completed: 3 })}\n`);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(host.health.ready, true);
  assert.equal(host.health.healthy, true);
  await host.stop();
});

test("an oversized split status stays discarded through its newline boundary", async () => {
  const child = daemonChild();
  const { binding } = chromeBinding();
  const host = startCodexSmartQueueDaemonHost(binding, {
    daemonArgs: ["--bridge-stdio"],
    spawn() { return child; },
  });
  const statuses = collectedLines(host.status);
  const validStatus = {
    ticks_completed: 2,
    requested_open_count: 0,
    opened_count: 0,
    open_failed_count: 0,
    search_needed: 0,
    degraded_tick_count: 0,
  };

  child.stdout.write("x".repeat(4097));
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(host.health.invalidStatusCount, 1);
  assert.equal(host.health.ready, false);

  child.stdout.write(`${JSON.stringify(validStatus)}\n`);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(host.health.invalidStatusCount, 1);
  assert.equal(host.latestStatus, null);
  assert.equal(host.health.ready, false);
  assert.equal(host.health.healthy, false);

  child.stdout.write(`${JSON.stringify(validStatus)}\n`);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(host.latestStatus, validStatus);
  assert.equal(host.health.ready, true);
  assert.equal(host.health.healthy, true);

  await host.stop();
  await statuses.ended;
  assert.deepEqual(statuses.parse(), [validStatus, { terminal: "exited" }]);
});

test("startOrGet retains a terminating daemon until finished and then restarts", async () => {
  const terminating = daemonChild();
  const replacementChild = daemonChild();
  const children = [terminating, replacementChild];
  let spawnCalls = 0;
  const { binding } = chromeBinding();
  const options = {
    daemonArgs: ["--bridge-stdio"],
    daemonPath: "/safe/smart_queue_daemon.py",
    spawn() {
      spawnCalls += 1;
      return children.shift();
    },
  };

  const initial = startOrGetCodexSmartQueueDaemonHost(binding, options);
  assert.equal(spawnCalls, 1);
  terminating.exitCode = 1;
  terminating.emit("exit", 1, null);
  assert.deepEqual(initial.exit, { exitCode: 1, signalCode: null });
  assert.equal(initial.health.running, false);

  assert.equal(startOrGetCodexSmartQueueDaemonHost(binding, options), initial);
  assert.equal(spawnCalls, 1);

  terminating.stdout.end();
  terminating.stderr.end();
  assert.deepEqual(await initial.finished, { exitCode: 1, signalCode: null });

  const replacement = startOrGetCodexSmartQueueDaemonHost(binding, options);
  assert.notEqual(replacement, initial);
  assert.equal(spawnCalls, 2);
  assert.equal(startOrGetCodexSmartQueueDaemonHost(binding, options), replacement);
  assert.equal(spawnCalls, 2);
  await replacement.stop();
});

test("child exit fences a stalled read-only openTabs request so a replacement starts safely", async () => {
  const terminating = daemonChild();
  const replacementChild = daemonChild();
  const children = [terminating, replacementChild];
  let releaseOpen;
  const delayedOpen = new Promise((resolve) => { releaseOpen = resolve; });
  let spawnCalls = 0;
  const { binding, events } = chromeBinding({ tabs: [tab(LINKEDIN)], delay: delayedOpen });
  const options = {
    daemonArgs: ["--bridge-stdio"],
    daemonPath: "/safe/in-flight-bridge-daemon.py",
    spawn() {
      spawnCalls += 1;
      return children.shift();
    },
  };

  const initial = startOrGetCodexSmartQueueDaemonHost(binding, options);
  terminating.stderr.write(`${JSON.stringify({ id: "open", operation: "open_listing", url: LINKEDIN })}\n`);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(events, [["openTabs"]]);

  terminating.exitCode = 0;
  terminating.emit("exit", 0, null);
  terminating.stdout.end();
  terminating.stderr.end();
  assert.deepEqual(await bounded(initial.finished, "fenced read-only host completion"), { exitCode: 0, signalCode: null });
  assert.equal(initial.health.quarantined, false);
  assert.deepEqual(events, [["openTabs"]]);

  const replacement = startOrGetCodexSmartQueueDaemonHost(binding, options);
  assert.notEqual(replacement, initial);
  assert.equal(spawnCalls, 2);
  releaseOpen();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(events, [["openTabs"]]);
  await replacement.stop();
});

test("child exit quarantines a dispatched mutation until it settles before replacement", async () => {
  const terminating = daemonChild();
  const replacementChild = daemonChild();
  const children = [terminating, replacementChild];
  let releaseNew;
  const stalledNew = new Promise((resolve) => { releaseNew = resolve; });
  let spawnCalls = 0;
  const { binding, events } = chromeBinding({ tabs: [tab(LINKEDIN)], newDelay: stalledNew });
  const options = {
    daemonArgs: ["--bridge-stdio"],
    daemonPath: "/safe/quarantined-bridge-daemon.py",
    spawn() {
      spawnCalls += 1;
      return children.shift();
    },
  };

  const initial = startOrGetCodexSmartQueueDaemonHost(binding, options);
  terminating.stderr.write(`${JSON.stringify({ id: "open", operation: "open_listing", url: LINKEDIN })}\n`);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(events, [["openTabs"], ["new"]]);

  terminating.exitCode = 0;
  terminating.emit("exit", 0, null);
  terminating.stdout.end();
  terminating.stderr.end();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(initial.health.running, false);
  assert.equal(initial.health.healthy, false);
  assert.equal(initial.health.quarantined, true);
  assert.equal(initial.health.terminal, "quarantined");
  assert.doesNotMatch(JSON.stringify(initial.health), /https:|open_listing|\"open\"/);
  assert.equal(startOrGetCodexSmartQueueDaemonHost(binding, options), initial);
  assert.equal(spawnCalls, 1);

  releaseNew();
  assert.deepEqual(await bounded(initial.finished, "quarantined mutation settlement"), { exitCode: 0, signalCode: null });
  assert.deepEqual(events, [["openTabs"], ["new"]]);
  assert.equal(initial.health.quarantined, false);
  assert.notEqual(initial.health.terminal, "quarantined");

  const replacement = startOrGetCodexSmartQueueDaemonHost(binding, options);
  assert.notEqual(replacement, initial);
  assert.equal(spawnCalls, 2);
  await replacement.stop();
});

test("the daemon host rejects a partial final status frame after child exit", async () => {
  const child = daemonChild();
  const { binding } = chromeBinding();
  const host = startCodexSmartQueueDaemonHost(binding, {
    daemonArgs: ["--bridge-stdio"],
    spawn() { return child; },
  });
  const statuses = collectedLines(host.status);

  child.exitCode = 0;
  child.emit("exit", 0, null);
  child.stdout.write(`${JSON.stringify({ ticks_completed: 2, opened_count: 1 })}\n`);
  child.stdout.end();
  child.stderr.end();

  assert.deepEqual(await host.finished, { exitCode: 0, signalCode: null });
  await statuses.ended;
  assert.deepEqual(statuses.parse(), [{ terminal: "exited" }]);
  assert.equal(host.latestStatus, null);
  assert.equal(host.health.ready, false);
});

test("stdout EOF or error clears readiness while process and bridge remain live", async () => {
  const { binding } = chromeBinding();
  for (const terminalEvent of ["end", "error"]) {
    const child = daemonChild();
    const host = startCodexSmartQueueDaemonHost(binding, {
      daemonArgs: ["--bridge-stdio"],
      spawn() { return child; },
    });
    child.stdout.write(`${JSON.stringify({
      requested_open_count: 0,
      opened_count: 0,
      open_failed_count: 0,
      search_needed: 0,
      degraded_count: 0,
    })}\n`);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(host.health.ready, true);

    if (terminalEvent === "end") child.stdout.end();
    else child.stdout.emit("error", new Error("private stdout failure"));
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(host.health.running, true);
    assert.equal(host.health.ready, false);
    assert.equal(host.health.healthy, false);
    assert.equal(host.health.statusStreamEnded, true);
    assert.equal(host.health.statusStreamErrored, terminalEvent === "error");
    assert.doesNotMatch(JSON.stringify(host.health), /private stdout failure/);
    await host.stop();
  }
});

test("clean bridge EOF lets a next-turn clean daemon exit preserve its final search-needed status", async () => {
  const child = daemonChild();
  const { binding } = chromeBinding();
  const host = startCodexSmartQueueDaemonHost(binding, {
    daemonArgs: ["--bridge-stdio"],
    spawn() { return child; },
  });
  const statuses = collectedLines(host.status);
  const finalStatus = {
    ticks_completed: 4,
    requested_open_count: 2,
    opened_count: 0,
    open_failed_count: 0,
    search_needed: 2,
    degraded_tick_count: 0,
  };

  child.stdout.write(`${JSON.stringify(finalStatus)}\n`);
  const exitedNextTurn = new Promise((resolve) => {
    setImmediate(() => {
      child.exitCode = 0;
      child.emit("exit", 0, null);
      child.stdout.end();
      resolve();
    });
  });
  child.stderr.end();
  await exitedNextTurn;

  assert.deepEqual(child.killCalls, []);
  assert.deepEqual(await host.finished, { exitCode: 0, signalCode: null });
  await statuses.ended;
  assert.deepEqual(statuses.parse(), [finalStatus, { terminal: "exited" }]);
  assert.deepEqual(host.latestStatus, finalStatus);
});

test("real children repeatedly preserve a normal terminal when stderr EOF precedes exit", { timeout: 15000 }, async () => {
  const { binding } = chromeBinding();

  for (let iteration = 0; iteration < 20; iteration += 1) {
    const fixture = realDaemonFixture(CLEAN_EOF_DAEMON);
    try {
      const host = startCodexSmartQueueDaemonHost(binding, fixture.options);
      assert.equal(fixture.processes.length, 1);
      await bounded(fixture.processes[0].stderrEnded, `stderr EOF in iteration ${iteration}`);

      const terminal = await bounded(host.finished, `normal daemon exit in iteration ${iteration}`);
      assert.deepEqual(terminal, { exitCode: 0, signalCode: null }, `iteration ${iteration}`);
      assert.notEqual(host.health.terminal, "bridge_closed", `iteration ${iteration}`);
    } finally {
      await fixture.releaseAll();
    }
  }
});

test("startOrGet retains a real child after bridge EOF until its delayed exit finishes", { timeout: 10000 }, async () => {
  const fixture = realDaemonFixture(DELAYED_EXIT_DAEMON);
  const { binding } = chromeBinding();
  const hosts = new Set();

  try {
    const initial = startOrGetCodexSmartQueueDaemonHost(binding, fixture.options);
    hosts.add(initial);
    const terminationSignalObserved = bounded(
      waitForStatus(initial.status, (status) => status.ticks_completed === 2),
      "bridge-close termination signal",
    );
    assert.equal(fixture.processes.length, 1);
    await bounded(fixture.processes[0].stderrEnded, "original child stderr EOF");
    await terminationSignalObserved;
    assert.equal(fixture.processes[0].child.exitCode, null);
    assert.equal(initial.health.terminal, "bridge_closed");

    const whileAlive = startOrGetCodexSmartQueueDaemonHost(binding, fixture.options);
    hosts.add(whileAlive);
    assert.equal(whileAlive, initial);
    assert.equal(fixture.processes.length, 1);

    fixture.processes[0].child.stdin.write("release\n");
    assert.deepEqual(await bounded(initial.finished, "original child delayed exit"), {
      exitCode: 0,
      signalCode: null,
    });

    const replacement = startOrGetCodexSmartQueueDaemonHost(binding, fixture.options);
    hosts.add(replacement);
    assert.notEqual(replacement, initial);
    assert.equal(fixture.processes.length, 2);
    await bounded(fixture.processes[1].stderrEnded, "replacement child stderr EOF");
    fixture.processes[1].child.stdin.write("release\n");
    assert.deepEqual(await bounded(replacement.finished, "replacement child delayed exit"), {
      exitCode: 0,
      signalCode: null,
    });
  } finally {
    await fixture.releaseAll();
    await Promise.all([...hosts].map((host) => bounded(host.finished, "supervised host cleanup")));
  }
});

test("clean bridge EOF becomes a bounded bridge-closed fatal when the child stays alive", { timeout: 1000 }, async () => {
  const child = daemonChild();
  const { binding } = chromeBinding();
  const host = startCodexSmartQueueDaemonHost(binding, {
    daemonArgs: ["--bridge-stdio"],
    spawn() { return child; },
  });
  const statuses = collectedLines(host.status);

  child.stderr.end();

  const terminal = await host.finished;
  assert.equal(terminal.exitCode, null);
  assert.equal(terminal.signalCode, "SIGTERM");
  assert.deepEqual(child.killCalls, ["SIGTERM"]);
  assert.equal(host.health.running, false);
  assert.equal(host.health.healthy, false);
  assert.equal(host.health.terminal, "bridge_closed");
  await statuses.ended;
  assert.deepEqual(statuses.parse(), [{ terminal: "exited" }]);
});

test("a rejected bridge remains immediately terminal without the clean-EOF grace turn", { timeout: 1000 }, async () => {
  const child = daemonChild();
  const brokenOutput = new EventEmitter();
  brokenOutput.write = () => false;
  brokenOutput.end = () => undefined;
  child.stdin = brokenOutput;
  const { binding } = chromeBinding();
  const host = startCodexSmartQueueDaemonHost(binding, {
    daemonArgs: ["--bridge-stdio"],
    spawn() { return child; },
  });
  const statuses = collectedLines(host.status);

  child.stderr.write(`${JSON.stringify({ id: "list", operation: "list_tab_urls" })}\n`);
  await new Promise((resolve) => setImmediate(resolve));
  brokenOutput.emit("error", new Error("private bridge output failure"));
  await new Promise((resolve) => {
    setImmediate(() => {
      if (child.killCalls.length === 0) {
        child.exitCode = 0;
        child.emit("exit", 0, null);
        child.stdout.end();
      }
      resolve();
    });
  });

  assert.deepEqual(child.killCalls, ["SIGTERM"]);
  assert.equal(host.health.running, false);
  assert.equal(host.health.healthy, false);
  assert.equal(host.health.terminal, "bridge_failed");
  await host.finished;
  await statuses.ended;
  assert.deepEqual(statuses.parse(), [{ terminal: "exited" }]);
});

test("a bridge failure makes the supervisor terminal and startOrGet replaces it", async () => {
  const failed = daemonChild();
  const brokenOutput = new EventEmitter();
  brokenOutput.write = () => false;
  brokenOutput.end = () => undefined;
  failed.stdin = brokenOutput;
  const replacement = daemonChild();
  const children = [failed, replacement];
  let spawnCalls = 0;
  const { binding } = chromeBinding();
  const options = {
    daemonArgs: ["--bridge-stdio"],
    daemonPath: "/safe/bridge-failure-daemon.py",
    spawn() {
      spawnCalls += 1;
      return children.shift();
    },
  };

  const terminal = startOrGetCodexSmartQueueDaemonHost(binding, options);
  failed.stderr.write(`${JSON.stringify({ id: "list", operation: "list_tab_urls" })}\n`);
  await new Promise((resolve) => setImmediate(resolve));
  brokenOutput.emit("error", new Error("private bridge output failure"));
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(terminal.health.healthy, false);
  assert.equal(terminal.health.running, false);
  assert.equal(terminal.health.terminal, "bridge_failed");
  assert.deepEqual(failed.killCalls, ["SIGTERM"]);

  const restarted = startOrGetCodexSmartQueueDaemonHost(binding, options);
  assert.notEqual(restarted, terminal);
  assert.equal(spawnCalls, 2);
  await restarted.stop();
});

test("the daemon parent requires the exact bridge-stdio spelling", () => {
  const { binding } = chromeBinding();
  let spawnCalls = 0;
  const spawn = () => {
    spawnCalls += 1;
    throw new Error("must not spawn without the required bridge flag");
  };
  for (const daemonArgs of [[], ["--stdio-bridge"], ["--bridge-stdio=true"], ["--bridge-stdio", "--bridge-stdio"]]) {
    assert.throws(
      () => startCodexSmartQueueDaemonHost(binding, { daemonArgs, spawn }),
      /exactly one --bridge-stdio/,
    );
  }
  assert.equal(spawnCalls, 0);
});

test("authoritative startup docs require the singleton daemon host entry point", async () => {
  const startupDocs = [
    "../AGENTS.md",
    "../README.md",
    "../skills/easy-apply-tab-monitor/SKILL.md",
  ];

  for (const relativePath of startupDocs) {
    const source = await readFile(new URL(relativePath, import.meta.url), "utf8");
    assert.match(
      source,
      /\bstartOrGetCodexSmartQueueDaemonHost\b/,
      `${relativePath} must name startOrGetCodexSmartQueueDaemonHost`,
    );
    assert.doesNotMatch(
      source,
      /\bstartCodexSmartQueueDaemonHost\s*\(/,
      `${relativePath} must not invoke startCodexSmartQueueDaemonHost`,
    );
    assert.doesNotMatch(
      source,
      /\bimport\s*\{[^}]*\bstartCodexSmartQueueDaemonHost\b[^}]*\}\s*from\b/s,
      `${relativePath} must not import startCodexSmartQueueDaemonHost for operational use`,
    );
  }
});

test("has no HTTP listener dependency", async () => {
  const source = await readFile(new URL("../skills/easy-apply-tab-monitor/scripts/codex_chrome_extension_host.mjs", import.meta.url), "utf8");
  assert.doesNotMatch(source, /node:http|createServer|\.listen\s*\(/);
});

test("the direct bridge parent admits no admission payload, recommendation JSON, or browser authority", async () => {
  const parentSource = await readFile(
    new URL("../skills/easy-apply-tab-monitor/scripts/codex_smart_queue_daemon_host.mjs", import.meta.url),
    "utf8",
  );
  // The parent accepts daemon arguments only from the caller and enforces
  // exactly one bridge flag; documented intake/database paths stay caller-owned
  // and no admission command, discovery export, or memory flag ever reaches it.
  assert.match(parentSource, /exactly one --bridge-stdio/);
  assert.doesNotMatch(parentSource, /admit-queue|discover\.py|--discovery-export|--memory-db|record_candidate_outcome/);
  assert.doesNotMatch(parentSource, /recommend|admission/i);
  assert.doesNotMatch(parentSource, /node:http|node:https|createServer|\.listen\s*\(|puppeteer|playwright|webdriver/i);

  // The bounded bridge performs only the two URL operations; no application
  // action exists because the candidate owns every application action.
  const bridgeSource = await readFile(
    new URL("../skills/easy-apply-tab-monitor/scripts/codex_chrome_extension_host.mjs", import.meta.url),
    "utf8",
  );
  assert.match(bridgeSource, /operation === "list_tab_urls"/);
  assert.match(bridgeSource, /operation === "open_listing"/);
  assert.doesNotMatch(bridgeSource, /operation === "(?!list_tab_urls|open_listing)/);
  assert.doesNotMatch(bridgeSource, /\bfill\b|\bupload\b|\bsubmit\b|\bclick\b/i);
});

test("agent handoff docs route search_needed through the private agent-only admit-queue CLI", async () => {
  const handoffDocs = [
    "../README.md",
    "../skills/easy-apply-tab-monitor/SKILL.md",
  ];

  for (const relativePath of handoffDocs) {
    const source = await readFile(new URL(relativePath, import.meta.url), "utf8");
    assert.match(source, /discover\.py admit-queue/, `${relativePath} must document the admit-queue command`);
    assert.match(source, /jobapply_agent\/private\/candidate_intake\.json/, `${relativePath} must keep intake under jobapply_agent/private/`);
    assert.match(source, /jobapply_agent\/private\/discovery\.jsonl/, `${relativePath} must keep the discovery export an ignored local runtime file`);
    assert.match(source, /jobapply_agent\/private\/smart-queue\.sqlite3/, `${relativePath} must keep the queue database under jobapply_agent/private/`);
    assert.match(source, /jobapply_agent\/private\/candidate-memory\.sqlite3/, `${relativePath} must keep candidate memory under jobapply_agent/private/`);
    assert.match(source, /filter_unsuppressed_candidates/, `${relativePath} must document suppression before admission`);
  }
});
