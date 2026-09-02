/**
 * Behavioral contract tests for the already-connected Codex Chrome host.
 *
 * These tests use an in-memory Chrome binding and loopback HTTP only. They
 * never start Chrome, attach to a browser, or navigate a real job board.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  startCodexChromeExtensionHost,
} from "../skills/easy-apply-tab-monitor/scripts/codex_chrome_extension_host.mjs";

const TOKEN = "node-host-contract-token-1234567890";
const LINKEDIN = "https://www.linkedin.com/jobs/view/123456";
const INDEED = "https://in.indeed.com/viewjob?jk=abc_123";

function existingTab(url = "https://in.indeed.com/jobs?q=python") {
  return { url };
}

function createChromeBinding({ tabs = [existingTab()] } = {}) {
  const events = [];
  const handoffTab = {
    async goto(url) {
      events.push(["goto", url]);
    },
    async markHandoff() {
      events.push(["markHandoff"]);
    },
  };
  const binding = {
    user: {
      async openTabs() {
        events.push(["openTabs"]);
        return tabs;
      },
    },
    tabs: {
      async new() {
        events.push(["new"]);
        return handoffTab;
      },
    },
    // Every prohibited capability is deliberately a throwing tripwire. The
    // host must never access it, even while servicing an allowed request.
    async click() { throw new Error("click is forbidden"); },
    async fill() { throw new Error("fill is forbidden"); },
    async upload() { throw new Error("upload is forbidden"); },
    async submit() { throw new Error("submit is forbidden"); },
    async close() { throw new Error("close is forbidden"); },
  };
  return { binding, events };
}

async function startHost(t, options = {}) {
  const chrome = createChromeBinding(options);
  const host = await startCodexChromeExtensionHost(chrome.binding, {
    token: TOKEN,
    ...options.hostOptions,
  });
  t.after(async () => host.close());
  return { ...chrome, host };
}

function request(host, path, options = {}) {
  return fetch(`${host.endpoint}${path}`, options);
}

function authorizedJson(url) {
  return {
    method: "POST",
    headers: {
      Authorization: `Bearer ${TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ url }),
  };
}

test("host requires its bearer credential before observing an existing session", async (t) => {
  const { events, host } = await startHost(t);

  const response = await request(host, "/v1/tab-urls");

  assert.equal(response.status, 404);
  assert.deepEqual(await response.json(), { error: "not found" });
  assert.deepEqual(events, []);
});

test("host returns only canonical supported listing URLs from the existing session", async (t) => {
  const { events, host } = await startHost(t, {
    tabs: [
      existingTab("https://mail.example.test/inbox?message=private"),
      existingTab("https://www.linkedin.com/jobs/view/123456/?trk=public_jobs"),
      existingTab("https://in.indeed.com/viewjob?jk=abc_123&utm_source=search"),
      existingTab("https://in.indeed.com/jobs?q=private-search"),
      existingTab("https://calendar.example.test/event?attendee=private"),
    ],
  });

  const response = await request(host, "/v1/tab-urls", {
    headers: { Authorization: `Bearer ${TOKEN}` },
  });

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { urls: [LINKEDIN, INDEED] });
  assert.deepEqual(events, [["openTabs"]]);
});

test("an empty existing session rejects an open request without creating a tab", async (t) => {
  const { events, host } = await startHost(t, { tabs: [] });

  const response = await request(host, "/v1/open-listing", authorizedJson(LINKEDIN));

  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), { error: "request failed" });
  assert.deepEqual(events, [["openTabs"]]);
});

test("a valid canonical listing opens exactly one handoff tab in the existing session", async (t) => {
  const { events, host } = await startHost(t);

  const response = await request(host, "/v1/open-listing", authorizedJson(INDEED));

  assert.equal(response.status, 204);
  assert.equal(await response.text(), "");
  assert.deepEqual(events, [
    ["openTabs"],
    ["new"],
    ["goto", INDEED],
    ["markHandoff"],
  ]);
});

test("malformed or noncanonical URLs never reach the new-tab capability", async (t) => {
  const { events, host } = await startHost(t);

  for (const url of [
    "https://in.indeed.com/jobs?q=python",
    "https://www.linkedin.com/jobs/view/123456/apply/",
    "https://in.indeed.com/viewjob?jk=abc_123&private=value",
    "not-a-url",
  ]) {
    const response = await request(host, "/v1/open-listing", authorizedJson(url));
    assert.equal(response.status, 400, url);
  }

  assert.deepEqual(events, []);
});

test("host exposes no interaction API beyond URL listing and exact listing opening", async (t) => {
  const { events, host } = await startHost(t);

  assert.deepEqual(Object.keys(host).sort(), ["close", "endpoint", "token"]);
  for (const forbiddenPath of [
    "/v1/click",
    "/v1/fill",
    "/v1/upload",
    "/v1/submit",
    "/v1/close",
  ]) {
    const response = await request(host, forbiddenPath, {
      method: "POST",
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    assert.equal(response.status, 404, forbiddenPath);
  }
  assert.deepEqual(events, []);
});
