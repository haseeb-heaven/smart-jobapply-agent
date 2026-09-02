/** Direct NDJSON contract tests for the existing-session-only Chrome bridge. */

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { PassThrough } from "node:stream";

import { startCodexChromeExtensionHost } from "../skills/easy-apply-tab-monitor/scripts/codex_chrome_extension_host.mjs";
import { startCodexSmartQueueDaemonHost } from "../skills/easy-apply-tab-monitor/scripts/codex_smart_queue_daemon_host.mjs";

const LINKEDIN = "https://www.linkedin.com/jobs/view/123456";
const INDEED = "https://in.indeed.com/viewjob?jk=abc_123";

function tab(url = "https://in.indeed.com/jobs?q=private") {
  return { url };
}

function chromeBinding({ tabs = [tab()], delay = null, openTabsError = null } = {}) {
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

test("the daemon parent uses only piped stdio and leaves status stdout untouched", async () => {
  const child = new EventEmitter();
  child.stdin = new PassThrough();
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.exitCode = null;
  child.signalCode = null;
  child.kill = (signal) => {
    child.signalCode = signal;
    queueMicrotask(() => child.emit("close", null, signal));
    return true;
  };
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
  assert.equal(host.status, child.stdout);
  const replies = [];
  child.stdin.on("data", (chunk) => replies.push(chunk.toString("utf8")));
  child.stderr.end(`${JSON.stringify({ id: "list", operation: "list_tab_urls" })}\n`);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(JSON.parse(replies.join("")), { id: "list", ok: true, urls: [] });
  assert.deepEqual(events, [["openTabs"]]);
  await host.stop();
  assert.equal(child.signalCode, "SIGTERM");
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

test("has no HTTP listener dependency", async () => {
  const source = await readFile(new URL("../skills/easy-apply-tab-monitor/scripts/codex_chrome_extension_host.mjs", import.meta.url), "utf8");
  assert.doesNotMatch(source, /node:http|createServer|\.listen\s*\(/);
});
