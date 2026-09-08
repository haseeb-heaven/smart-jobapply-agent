/** Test-only CDP bridge: exact creation IDs are journaled before navigation. */
import { appendFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const endpoint = "http://127.0.0.1:9222";
export function fixtureAliases(urls) {
  return new Map(urls.flatMap(url => new URL(url).hostname === "www.linkedin.com"
    ? [[url, url], [url + "/", url]] : [[url, url]]));
}

export async function openSyntheticTarget(url, journal, { api, connect = url => new WebSocket(url), record = appendFileSync } = {}) {
  // Ownership is durable before any listing navigation can fail.
  const target = await api("/json/new?about%3Ablank", "PUT");
  if (typeof target.id !== "string" || !/^[A-Za-z0-9_-]+$/.test(target.id)) throw Error("invalid target ID");
  record(journal, target.id + "\n", { mode: 0o600 });
  const socket = connect(target.webSocketDebuggerUrl);
  let sequence = 0;
  const pending = new Map();
  let loaded;
  let navigationLoader;
  const loadedIds = new Set();
  let failed;
  const load = new Promise(resolve => { loaded = resolve; });
  const failure = new Promise((resolve, reject) => { failed = reject; });
  failure.catch(() => {});
  const timer = setTimeout(() => failed(Error("synthetic navigation timed out")), 8000);
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    pending.set(id, { resolve, reject });
    socket.send(JSON.stringify({ id, method, params }));
  });
  socket.addEventListener("message", event => {
    const frame = JSON.parse(event.data);
    if (frame.id && pending.has(frame.id)) {
      const waiter = pending.get(frame.id); pending.delete(frame.id);
      if (frame.error) waiter.reject(Error("synthetic CDP command failed"));
      else waiter.resolve(frame.result);
    } else if (frame.method === "Fetch.requestPaused") {
      const { requestId, request } = frame.params;
      const action = fixtureAliases([url]).has(request.url)
        ? send("Fetch.fulfillRequest", { requestId, responseCode: 200,
          responseHeaders: [{ name: "Content-Type", value: "text/html" }],
          body: Buffer.from("<!doctype html>").toString("base64") })
        : send("Fetch.failRequest", { requestId, errorReason: "BlockedByClient" });
      action.catch(failed);
    } else if (frame.method === "Page.lifecycleEvent" && frame.params.name === "load") {
      loadedIds.add(frame.params.loaderId);
      if (frame.params.loaderId === navigationLoader) loaded();
    }
  });
  const onFailure = () => {
    const error = Error("synthetic CDP connection ended");
    failed(error);
    for (const waiter of pending.values()) waiter.reject(error);
    pending.clear();
  };
  socket.addEventListener("error", onFailure);
  socket.addEventListener("close", onFailure);
  try {
    await Promise.race([failure, (async () => {
      await new Promise(resolve => socket.addEventListener("open", resolve, { once: true }));
      await send("Page.enable");
      await send("Page.setLifecycleEventsEnabled", { enabled: true });
      // This interception belongs only to the newly created ID. Unexpected
      // requests fail locally; no other browser target is intercepted.
      await send("Fetch.enable", { patterns: [{ urlPattern: "*" }] });
      const result = await send("Page.navigate", { url });
      if (result.errorText) throw Error("synthetic navigation failed");
      navigationLoader = result.loaderId;
      if (!navigationLoader) throw Error("synthetic navigation did not create a loader");
      if (loadedIds.has(navigationLoader)) loaded();
      await load;
      await send("Fetch.disable");
    })()]);
  } finally {
    clearTimeout(timer);
    socket.close();
  }
}

async function main() {
  const allowed = new Set(JSON.parse(process.env.SMART_QUEUE_TEST_URLS ?? "[]"));
  const journal = process.env.SMART_QUEUE_TEST_JOURNAL;
  const api = async (path, method = "GET") => {
    const response = await fetch(endpoint + path, { method, signal: AbortSignal.timeout(8000) });
    if (!response.ok) throw Error("CDP unavailable");
    return response.json();
  };
  const [operation, url, ...rest] = process.argv.slice(2);
  if (rest.length) throw Error("invalid arguments");
  if (operation === "list-tabs" && url === undefined) {
    const rows = await api("/json/list");
    process.stdout.write(JSON.stringify(rows.filter(row => row.type === "page").map(row => row.url)) + "\n");
  } else if (operation === "open-listing" && allowed.has(url) && journal) {
    await openSyntheticTarget(url, journal, { api });
  } else throw Error("invalid operation");
}
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main().then(() => process.exit(0), () => process.exit(2));
}
