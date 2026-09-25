import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { spawnSync } from "node:child_process";
import { listingIdentity, runBridge } from "../skills/easy-apply-tab-monitor/scripts/system_chrome_listing_bridge.mjs";

const approved = "https://in.linkedin.com/jobs/view/123456";
const redirected = "https://www.linkedin.com/jobs/view/software-engineer-example-123456";
const unicodeRedirected = "https://www.linkedin.com/jobs/view/python-developer-%E2%80%93-full-time-123456";
const page = (id, url) => ({ id, type: "page", url });
const unrelated = page("existing", "https://example.org/personal");
async function fixture(t, responses) {
  const root = await fs.mkdtemp(path.join(await fs.realpath(os.tmpdir()), "chrome-listing-test-"));
  const state = path.join(root, "queue-chrome-bindings.json");
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const calls = [];
  const options = { privateRoot: root, fetchImpl: async (url, options) => {
    calls.push({ url, ...options });
    assert.ok(responses.length, "unexpected browser request");
    const value = responses.shift();
    if (value instanceof Error) throw value;
    return value instanceof Response ? value : new Response(JSON.stringify(value));
  } };
  return { root, state, calls, options, run: (...args) => runBridge([state, ...args], options) };
}
async function bind(f, bindings = [{ targetId: "created", approvedUrl: approved }]) {
  await fs.writeFile(f.state, JSON.stringify({ version: 1, bindings }), { mode: 0o600 });
}

test("opens exact approved URL, persists returned target, confirms redirect and survives later invocation", async (t) => {
  const f = await fixture(t, [[unrelated], page("created", "about:blank"),
    [unrelated, page("created", "about:blank")], [unrelated, page("created", redirected)],
    [unrelated, page("created", redirected)]]);
  assert.equal(await f.run("open-listing", approved), null);
  assert.equal(f.calls[1].url, `http://127.0.0.1:9222/json/new?${encodeURIComponent(approved)}`);
  assert.equal(f.calls[1].method, "PUT");
  assert.ok(f.calls.every((call) => call.redirect === "error" && call.signal instanceof AbortSignal));
  assert.ok(f.calls.every((call) => call.url.startsWith("http://127.0.0.1:9222/")));
  assert.deepEqual(JSON.parse(await fs.readFile(f.state, "utf8")), {
    version: 1, bindings: [{ targetId: "created", approvedUrl: approved }],
  });
  assert.equal((await fs.stat(f.state)).mode & 0o777, 0o600);
  assert.deepEqual(await f.run("list-tabs"), [unrelated.url, approved]);
});

test("uses IPv4 when the existing Chrome debug endpoint responds", async (t) => {
  const f = await fixture(t, [[unrelated]]);
  assert.deepEqual(await f.run("list-tabs"), [unrelated.url]);
  assert.deepEqual(f.calls.map(({ url, method, redirect }) => ({ url, method, redirect })), [
    { url: "http://127.0.0.1:9222/json/list", method: "GET", redirect: "error" },
  ]);
});

test("falls back to IPv6 loopback after IPv4 connection refusal", async (t) => {
  const refused = Object.assign(new Error("connection refused"), { code: "ECONNREFUSED" });
  const f = await fixture(t, [refused, [unrelated]]);
  assert.deepEqual(await f.run("list-tabs"), [unrelated.url]);
  assert.deepEqual(f.calls.map(({ url, method, redirect }) => ({ url, method, redirect })), [
    { url: "http://127.0.0.1:9222/json/list", method: "GET", redirect: "error" },
    { url: "http://[::1]:9222/json/list", method: "GET", redirect: "error" },
  ]);
});

test("falls back to IPv6 for a nested Undici connection cause", async (t) => {
  const cause = Object.assign(new Error("socket unavailable"), { code: "ECONNREFUSED" });
  const undiciError = Object.assign(new TypeError("fetch failed"), { cause });
  const f = await fixture(t, [undiciError, [unrelated]]);
  assert.deepEqual(await f.run("list-tabs"), [unrelated.url]);
  assert.deepEqual(f.calls.map(({ url, method, redirect }) => ({ url, method, redirect })), [
    { url: "http://127.0.0.1:9222/json/list", method: "GET", redirect: "error" },
    { url: "http://[::1]:9222/json/list", method: "GET", redirect: "error" },
  ]);
});

test("does not fall back to IPv6 for HTTP or non-connection failures", async (t) => {
  for (const [label, first] of [
    ["HTTP failure", new Response("unavailable", { status: 503 })],
    ["non-connection failure", new TypeError("invalid fetch request")],
    ["invalid response body", new Response("not json")],
  ]) {
    await t.test(label, async (subtest) => {
      const f = await fixture(subtest, [first]);
      await assert.rejects(f.run("list-tabs"));
      assert.deepEqual(f.calls.map(({ url, method, redirect }) => ({ url, method, redirect })), [
        { url: "http://127.0.0.1:9222/json/list", method: "GET", redirect: "error" },
      ]);
    });
  }
});

test("does not fall back to IPv6 for timeout errors", async (t) => {
  const timeout = Object.assign(new Error("request timed out"), { name: "TimeoutError", code: "ETIMEDOUT" });
  const f = await fixture(t, [timeout]);
  await assert.rejects(f.run("list-tabs"));
  assert.deepEqual(f.calls.map(({ url, method, redirect }) => ({ url, method, redirect })), [
    { url: "http://127.0.0.1:9222/json/list", method: "GET", redirect: "error" },
  ]);
});



test("does not fall back after an HTTP-success initial response is not CDP-shaped", async (t) => {
  const malformed = new Response(JSON.stringify({ webSocketDebuggerUrl: "ws://127.0.0.1:9222/devtools/browser/synthetic" }), { status: 200 });
  const f = await fixture(t, [malformed]);
  await assert.rejects(f.run("open-listing", approved), (error) => {
    assert.equal(error.message, "existing Chrome listing bridge unavailable");
    return true;
  });
  assert.deepEqual(f.calls.map(({ url, method, redirect }) => ({ url, method, redirect })), [
    { url: "http://127.0.0.1:9222/json/list", method: "GET", redirect: "error" },
  ]);
  await assert.rejects(f.run("list-tabs"));
  assert.deepEqual(f.calls.map(({ url }) => url), [
    "http://127.0.0.1:9222/json/list",
    "http://127.0.0.1:9222/json/list",
  ]);
});

test("opens the exact approved URL through IPv6 and confirms its redirect", async (t) => {
  const f = await fixture(t, [new Response("not found", { status: 404 }), [unrelated],
    page("created", "about:blank"), [unrelated, page("created", "about:blank")],
    [unrelated, page("created", redirected)]]);
  assert.equal(await f.run("open-listing", approved), null);
  assert.deepEqual(f.calls.map(({ url, method, redirect }) => ({ url, method, redirect })), [
    { url: "http://127.0.0.1:9222/json/list", method: "GET", redirect: "error" },
    { url: "http://[::1]:9222/json/list", method: "GET", redirect: "error" },
    { url: "http://[::1]:9222/json/new?" + encodeURIComponent(approved), method: "PUT", redirect: "error" },
    { url: "http://[::1]:9222/json/list", method: "GET", redirect: "error" },
    { url: "http://[::1]:9222/json/list", method: "GET", redirect: "error" },
  ]);
  assert.ok(f.calls.every((call) => call.redirect === "error" && call.signal instanceof AbortSignal));
  assert.deepEqual(JSON.parse(await fs.readFile(f.state, "utf8")), {
    version: 1, bindings: [{ targetId: "created", approvedUrl: approved }],
  });
});

test("does not retry IPv6 after a pinned IPv4 PUT fails", async (t) => {
  const refused = Object.assign(new Error("connection refused"), { code: "ECONNREFUSED" });
  const f = await fixture(t, [[unrelated], refused]);
  await assert.rejects(f.run("open-listing", approved));
  assert.deepEqual(f.calls.map(({ url, method, redirect }) => ({ url, method, redirect })), [
    { url: "http://127.0.0.1:9222/json/list", method: "GET", redirect: "error" },
    { url: "http://127.0.0.1:9222/json/new?" + encodeURIComponent(approved), method: "PUT", redirect: "error" },
  ]);
});

test("does not switch endpoints when a pinned IPv4 follow-up snapshot fails", async (t) => {
  const f = await fixture(t, [[unrelated], page("created", "about:blank"),
    [unrelated, page("created", "about:blank")], new Response("not found", { status: 404 })]);
  await assert.rejects(f.run("open-listing", approved));
  assert.deepEqual(f.calls.map(({ url, method, redirect }) => ({ url, method, redirect })), [
    { url: "http://127.0.0.1:9222/json/list", method: "GET", redirect: "error" },
    { url: "http://127.0.0.1:9222/json/new?" + encodeURIComponent(approved), method: "PUT", redirect: "error" },
    { url: "http://127.0.0.1:9222/json/list", method: "GET", redirect: "error" },
    { url: "http://127.0.0.1:9222/json/list", method: "GET", redirect: "error" },
  ]);
});

test("fails closed with a generic error when both loopback endpoints are unavailable", async (t) => {
  const refused = () => Object.assign(new Error("connection refused"), { code: "ECONNREFUSED" });
  const f = await fixture(t, [refused(), refused()]);
  await assert.rejects(f.run("list-tabs"), (error) => {
    assert.equal(error.message, "existing Chrome listing bridge unavailable");
    return true;
  });
  assert.deepEqual(f.calls.map(({ url, method, redirect }) => ({ url, method, redirect })), [
    { url: "http://127.0.0.1:9222/json/list", method: "GET", redirect: "error" },
    { url: "http://[::1]:9222/json/list", method: "GET", redirect: "error" },
  ]);
});

for (const current of [
  "https://www.linkedin.com/jobs/view/software-engineer-example-654321",
  "https://www.linkedin.com/jobs/view/123456/apply",
  "https://www.linkedin.com/login",
  `${redirected}?applicationId=123`, `${redirected}?JK=123`,
  `${redirected}#apply`, "https://evil.example/jobs/view/123456",
  "https://www.linkedin.com:443/jobs/view/123456",
  "https://www.linkedin.com/jobs/view/../view/123456",
]) {
  test(`bound target preserves unsupported or different URL: ${current}`, async (t) => {
    const f = await fixture(t, [[page("created", current)]]);
    await bind(f);
    assert.deepEqual(await f.run("list-tabs"), [current]);
  });
}

test("same-job unbound targets remain raw; only created target is substituted", async (t) => {
  const f = await fixture(t, [[page("created", redirected), page("unbound", redirected)]]);
  await bind(f);
  assert.deepEqual(await f.run("list-tabs"), [approved, redirected]);
});

test("Unicode title redirect confirms exact approved open and maps only its created target", async (t) => {
  const f = await fixture(t, [[unrelated], page("created", "about:blank"),
    [unrelated, page("created", unicodeRedirected)],
    [page("created", unicodeRedirected), page("unbound", unicodeRedirected)]]);
  assert.equal(await f.run("open-listing", approved), null);
  assert.equal(f.calls[1].url, `http://127.0.0.1:9222/json/new?${encodeURIComponent(approved)}`);
  assert.deepEqual(await f.run("list-tabs"), [approved, unicodeRedirected]);
  assert.deepEqual(JSON.parse(await fs.readFile(f.state, "utf8")).bindings,
    [{ targetId: "created", approvedUrl: approved }]);
});

test("Unicode observation aliases never become approved open URLs", async (t) => {
  const f = await fixture(t, []);
  await assert.rejects(f.run("open-listing", unicodeRedirected));
  assert.equal(f.calls.length, 0);
  assert.equal(listingIdentity(`${unicodeRedirected}/?trk=public_jobs`), "linkedin:123456");
  assert.equal(listingIdentity("https://linkedin.com/jobs/view/d%C3%A9veloppeur-123456"), "linkedin:123456");
});

for (const current of [
  unicodeRedirected.replace("123456", "654321"),
  unicodeRedirected.replace("/jobs/view/", "/jobs/%76iew/"),
  unicodeRedirected.replace("python-developer", "%2e%2e"),
  unicodeRedirected.replace("python-developer", "python%2fapply"),
  unicodeRedirected.replace("python-developer", "python%5capply"),
  unicodeRedirected.replace("python-developer", "python%252fapply"),
  unicodeRedirected.replace("python-developer", "python%00developer"),
  unicodeRedirected.replace("python-developer", "python%E2%80%AEdeveloper"),
  unicodeRedirected.replace("python-developer", "python%ZZdeveloper"),
  unicodeRedirected.replace("www.linkedin.com", "www.linkedin.com:443"),
  unicodeRedirected.replace("www.linkedin.com", "user@www.linkedin.com"),
  unicodeRedirected.replace("www.linkedin.com", "linkedin.com.evil.example"),
  `${unicodeRedirected}/apply`, `${unicodeRedirected}/login`,
  `${unicodeRedirected}?applicationId=123`, `${unicodeRedirected}?%61pply=1`,
  `${unicodeRedirected}?%74rk=public_jobs`, `${unicodeRedirected}#`,
]) {
  test(`Unicode alias cannot mask unsupported or different target URL: ${current}`, async (t) => {
    const f = await fixture(t, [[page("created", current)]]);
    await bind(f);
    assert.deepEqual(await f.run("list-tabs"), [current]);
    if (!current.endsWith("654321")) assert.equal(listingIdentity(current), null);
  });
}

test("reliable absence removes binding without an outcome or closing any target", async (t) => {
  const f = await fixture(t, [[unrelated], [unrelated, page("created", redirected)]]);
  await bind(f);
  assert.deepEqual(await f.run("list-tabs"), [unrelated.url]);
  assert.deepEqual(JSON.parse(await fs.readFile(f.state, "utf8")).bindings, []);
  assert.deepEqual(await f.run("list-tabs"), [unrelated.url, redirected]);
  assert.ok(f.calls.every((call) => call.method === "GET"));
});

test("failed confirmation retains binding for next reliable snapshot", async (t) => {
  const f = await fixture(t, [[unrelated], page("created", "about:blank"), new Error("offline"),
    [unrelated, page("created", redirected)]]);
  await assert.rejects(f.run("open-listing", approved));
  assert.deepEqual(await f.run("list-tabs"), [unrelated.url, approved]);
});

test("strict source job identity allows tracking and Indeed country redirects", () => {
  assert.equal(listingIdentity(`${redirected}?trk=public_jobs`), "linkedin:123456");
  assert.equal(listingIdentity("https://in.indeed.com/viewjob?jk=abc123&from=search"), "indeed:abc123");
  assert.equal(listingIdentity("https://indeed.com/viewjob?jk=abc123&jk=other"), null);
  assert.equal(listingIdentity("https://linkedin.com/jobs/view/no-numeric-id"), null);
});

for (const bad of ["{", JSON.stringify({ version: 2, bindings: [] }),
  JSON.stringify({ version: 1, bindings: [{ targetId: "bad", approvedUrl: "https://example.org" }] }),
  JSON.stringify({ version: 1, bindings: [{ targetId: "x", approvedUrl: approved }, { targetId: "x", approvedUrl: approved }] }),
  JSON.stringify({ version: 1, bindings: [], extra: true }), " ".repeat(1024 * 1024 + 1)]) {
  test("malformed private state fails before any browser call", async (t) => {
    const f = await fixture(t, []);
    await fs.writeFile(f.state, bad, { mode: 0o600 });
    await assert.rejects(f.run("list-tabs"));
    assert.equal(f.calls.length, 0);
  });
}

test("rejects escaped private paths, symlinks, hardlinks and permissive state", async (t) => {
  const f = await fixture(t, []);
  await assert.rejects(runBridge([path.join(f.root, "..", "outside.json"), "list-tabs"], f.options));
  const other = path.join(f.root, "other.json");
  await fs.writeFile(other, "{}", { mode: 0o600 });
  await fs.symlink(other, f.state);
  await assert.rejects(f.run("list-tabs"));
  await fs.unlink(f.state);
  await fs.link(other, f.state);
  await assert.rejects(f.run("list-tabs"));
  await fs.unlink(f.state);
  await bind(f);
  await fs.chmod(f.state, 0o644);
  await assert.rejects(f.run("list-tabs"));
  assert.equal(f.calls.length, 0);
});

test("rejects a symlinked ancestor directory", async (t) => {
  const f = await fixture(t, []);
  await fs.symlink(f.root, path.join(f.root, "linked"));
  await assert.rejects(runBridge([path.join(f.root, "linked", "state.json"), "list-tabs"], f.options));
});

for (const snapshot of [[], [page("same", approved), page("same", redirected)],
  [{ type: "page", url: approved }], [page("x", "")], Array(513).fill(unrelated)]) {
  test("invalid or ambiguous snapshot cannot open a target", async (t) => {
    const f = await fixture(t, [snapshot]);
    await assert.rejects(f.run("open-listing", approved));
    assert.equal(f.calls.length, 1);
  });
}

test("ambiguous creation response never binds an existing target", async (t) => {
  const f = await fixture(t, [[unrelated], page("existing", approved)]);
  await assert.rejects(f.run("open-listing", approved));
  await assert.rejects(fs.stat(f.state), { code: "ENOENT" });
});

test("unsupported commands and noncanonical opens fail before network", async (t) => {
  const f = await fixture(t, []);
  for (const args of [["close", approved], ["list-tabs", approved], ["open-listing"],
    ["open-listing", `${approved}?trk=tracking`], ["open-listing", "https://www.linkedin.com/login"]]) {
    await assert.rejects(f.run(...args));
  }
  assert.equal(f.calls.length, 0);
});

test("oversized browser response fails before opening", async (t) => {
  const f = await fixture(t, [{ ignored: "x".repeat(1024 * 1024) }]);
  await assert.rejects(f.run("open-listing", approved));
  assert.equal(f.calls.length, 1);
});

test("an unconfirmed different-job target never receives a success acknowledgement", async (t) => {
  const f = await fixture(t, [[unrelated], page("created", "about:blank"),
    ...Array.from({ length: 20 }, () => [unrelated, page("created", "https://www.linkedin.com/jobs/view/999999")])]);
  await assert.rejects(f.run("open-listing", approved));
  assert.equal(f.calls.filter((call) => call.method === "PUT").length, 1);
  assert.equal(f.calls.length, 22);
});

test("CLI rejects wrong argv with redacted error and empty stdout", () => {
  const result = spawnSync(process.execPath, [
    "skills/easy-apply-tab-monitor/scripts/system_chrome_listing_bridge.mjs",
    "jobapply_agent/private/test.json", "close", "https://private.example/sensitive",
  ], { encoding: "utf8" });
  assert.equal(result.status, 2);
  assert.equal(result.stdout, "");
  assert.equal(result.stderr, "existing Chrome listing bridge unavailable\n");
});
