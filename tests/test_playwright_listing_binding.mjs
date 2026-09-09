import assert from "node:assert/strict";
import test from "node:test";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";

import { createPlaywrightListingBinding } from "../skills/easy-apply-tab-monitor/scripts/playwright_listing_binding.mjs";
import { startPlaywrightSmartQueueDaemonHost } from "../skills/easy-apply-tab-monitor/scripts/playwright_listing_binding.mjs";
import { validateCapabilities } from "../skills/easy-apply-tab-monitor/scripts/browser_capabilities.mjs";

const LINKEDIN = "https://www.linkedin.com/jobs/view/123456";
const INDEED = "https://www.indeed.com/viewjob?jk=abc_123";
const SEARCH = "https://www.linkedin.com/jobs/search/?keywords=python";
const APPLICATION = "https://www.linkedin.com/jobs/view/123456/apply/";
const CAPABILITIES = Object.freeze({
  protocolVersion: 1,
  sessionId: "playwright-test-session",
  existingSession: true,
  completeUrlSnapshots: true,
  exactListingOpen: true,
  persistentConnection: true,
});

function bindingFor(context, capabilities = CAPABILITIES) {
  return createPlaywrightListingBinding(context, capabilities);
}

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
    child.stdout.end();
    child.stderr.end();
    queueMicrotask(() => {
      child.emit("exit", null, signal);
      child.emit("close", null, signal);
    });
    return true;
  };
  return child;
}

function fakePage(url, { closed = false } = {}) {
  return {
    url: () => url,
    isClosed: () => closed,
  };
}

function fakeContext({ pages = [fakePage("about:blank")], newPage, forbidden = false } = {}) {
  const calls = { pages: 0, newPage: 0, goto: [], close: 0 };
  const context = {
    pages: () => {
      calls.pages += 1;
      if (forbidden) throw new Error("DOM/cookie access forbidden");
      return pages;
    },
    newPage: async () => {
      calls.newPage += 1;
      if (newPage) return newPage(calls);
      const page = {
        url: () => "about:blank",
        isClosed: () => false,
        goto: async (url, options) => { calls.goto.push([url, options]); },
      };
      pages.push(page);
      return page;
    },
  };
  if (forbidden) {
    for (const name of ["cookies", "storageState", "close", "browser", "newContext"]) {
      context[name] = () => { throw new Error(`${name} must not be called`); };
    }
  }
  return { context, calls, pages };
}

test("requires an existing Playwright-like BrowserContext", () => {
  for (const value of [null, {}, { pages() {} }, { newPage() {} }]) {
    assert.throws(() => bindingFor(value), /existing browser context required/);
  }
});

test("requires a valid host capability descriptor", () => {
  const fixture = fakeContext();
  for (const capabilities of [
    null,
    { ...CAPABILITIES, existingSession: false },
    { ...CAPABILITIES, sessionId: "" },
    { ...CAPABILITIES, unexpected: true },
  ]) {
    assert.throws(
      () => bindingFor(fixture.context, capabilities),
      /browser capabilities unavailable/,
    );
  }
  assert.deepEqual(validateCapabilities(CAPABILITIES), CAPABILITIES);
});

test("rejects a conflicting bindingId before touching the context or spawning", () => {
  const fixture = fakeContext();
  let spawnCalls = 0;
  assert.throws(
    () => startPlaywrightSmartQueueDaemonHost(fixture.context, CAPABILITIES, {
      bindingId: "different-session",
      daemonArgs: ["--bridge-stdio"],
      spawn: () => { spawnCalls += 1; return daemonChild(); },
    }),
    /browser binding session is invalid/,
  );
  assert.equal(fixture.calls.pages, 0);
  assert.equal(fixture.calls.newPage, 0);
  assert.equal(spawnCalls, 0);
});

test("valid host start delegates the capability sessionId to daemon singleton identity", async () => {
  const fixture = fakeContext();
  const otherCapabilities = { ...CAPABILITIES, sessionId: "other-session" };
  let spawnCalls = 0;
  const options = {
    daemonArgs: ["--bridge-stdio"],
    daemonPath: "/safe/playwright-test-daemon.py",
    spawn: () => {
      spawnCalls += 1;
      return daemonChild();
    },
  };
  const host = startPlaywrightSmartQueueDaemonHost(fixture.context, CAPABILITIES, options);
  assert.throws(
    () => startPlaywrightSmartQueueDaemonHost(fixture.context, otherCapabilities, options),
    /different session|distinct daemon configuration/,
  );
  assert.equal(spawnCalls, 1);
  await host.stop();
});

test("lists URLs from the supplied context without inspecting page content", async () => {
  const fixture = fakeContext({
    pages: [fakePage(LINKEDIN), fakePage("https://mail.example.test/inbox")],
    forbidden: false,
  });
  const binding = bindingFor(fixture.context);
  assert.deepEqual(await binding.listTabUrls(), [LINKEDIN, "https://mail.example.test/inbox"]);
  assert.equal(fixture.calls.newPage, 0);
});

test("allows an unrelated-only existing session and returns its URL snapshot", async () => {
  const fixture = fakeContext({ pages: [fakePage("about:blank")] });
  const binding = bindingFor(fixture.context);
  assert.deepEqual(await binding.listTabUrls(), ["about:blank"]);
});

test("opens only an already-canonical exact listing URL", async () => {
  const fixture = fakeContext();
  const binding = bindingFor(fixture.context);
  await binding.openListing(LINKEDIN);
  assert.equal(fixture.calls.newPage, 1);
  assert.equal(fixture.calls.goto.length, 1);
  assert.deepEqual(fixture.calls.goto[0], [LINKEDIN, { waitUntil: "commit", timeout: 10000 }]);
});

test("rejects malformed, noncanonical, search, credentialed, and application URLs before newPage", async () => {
  const rejected = [
    "not-a-url",
    "https://WWW.LinkedIn.com/jobs/view/123456/",
    SEARCH,
    APPLICATION,
    "https://user:pass@www.linkedin.com/jobs/view/123456",
    "http://www.linkedin.com/jobs/view/123456",
    "https://example.test/jobs/1",
  ];
  const fixture = fakeContext();
  const binding = bindingFor(fixture.context);
  for (const url of rejected) {
    await assert.rejects(binding.openListing(url), /listing|open unavailable/i);
  }
  assert.equal(fixture.calls.newPage, 0);
});

test("maps page enumeration failures to a generic snapshot/open error", async () => {
  const fixture = fakeContext({ newPage: () => { throw new Error("private page state"); } });
  fixture.context.pages = () => { throw new Error("private page state"); };
  const binding = bindingFor(fixture.context);
  await assert.rejects(binding.listTabUrls(), /browser snapshot unavailable/);
  await assert.rejects(binding.openListing(LINKEDIN), /listing open unavailable|browser snapshot unavailable/);
});

test("rejects empty, closed, malformed, and oversized snapshots without leaking data", async () => {
  const snapshots = [
    [],
    [fakePage(LINKEDIN, { closed: true })],
    [{ url: () => 3, isClosed: () => false }],
    [fakePage("")],
    [fakePage("x".repeat(8193))],
    Array.from({ length: 513 }, () => fakePage(LINKEDIN)),
  ];
  for (const pages of snapshots) {
    const fixture = fakeContext({ pages });
    const binding = bindingFor(fixture.context);
    await assert.rejects(binding.listTabUrls(), (error) => {
      assert.match(String(error), /browser snapshot unavailable/);
      assert.doesNotMatch(String(error), /8193|linkedin|123456/i);
      return true;
    });
  }
});

test("does not close a blank page when page creation or navigation fails", async () => {
  const fixture = fakeContext({
    newPage: async (calls) => ({
      url: () => "about:blank",
      isClosed: () => false,
      goto: async () => { calls.goto.push("attempted"); throw new Error("private navigation state"); },
    }),
  });
  const binding = bindingFor(fixture.context);
  await assert.rejects(binding.openListing(LINKEDIN), /listing open unavailable/);
  assert.equal(fixture.calls.close, 0);
  assert.equal(fixture.pages.length, 1);
  assert.equal(fixture.calls.goto.length, 1);
});

test("latches failed opens while preserving URL snapshots", async () => {
  const fixture = fakeContext({
    newPage: async () => { throw new Error("private page creation state"); },
  });
  const binding = bindingFor(fixture.context);
  await assert.rejects(binding.openListing(LINKEDIN), /listing open unavailable/);
  for (let i = 0; i < 10; i += 1) {
    await assert.rejects(binding.openListing(INDEED), /browser_open_paused|listing open unavailable/i);
  }
  assert.equal(fixture.calls.newPage, 1);
  assert.deepEqual(await binding.listTabUrls(), ["about:blank"]);
});
