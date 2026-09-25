/** URL-only bridge to the host's existing Chrome on loopback port 9222.
 * The queue host MUST serialize commands for its dedicated private state file.
 * This module does not launch a browser, inspect content, or close targets.
 */
import { constants } from "node:fs";
import * as fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";
import { canonicalListingUrl } from "./codex_chrome_extension_host.mjs";

const PRIVATE_ROOT = fileURLToPath(new URL("../../../jobapply_agent/private/", import.meta.url));
const MAX_BYTES = 1024 * 1024;
const MAX_TARGETS = 512;
const MAX_URL = 8192;
const invalid = () => { throw new Error("existing Chrome listing bridge unavailable"); };
const targetId = (id) => typeof id === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(id);
const keysAre = (value, keys) => value !== null && typeof value === "object" && !Array.isArray(value)
  && Object.keys(value).sort().join(",") === [...keys].sort().join(",");

/** Identity is local to a created target, never a queue canonicalization rule. */
export function listingIdentity(value) {
  try {
    if (typeof value !== "string" || value.length > MAX_URL || /[\s\\\u0000-\u001f\u007f#]/.test(value)) return null;
    // Check the original spelling before WHATWG parsing can normalize a path,
    // authority, or explicit default port.
    const spelling = value.match(/^https:\/\/([A-Za-z0-9.-]+)(\/[^?]*)(\?[^#]*)?$/);
    if (!spelling || /(?:^|\/)\.{1,2}(?:\/|$)/.test(spelling[2])) return null;
    const raw = new URL(value);
    if (raw.hostname === "linkedin.com" || raw.hostname.endsWith(".linkedin.com")) {
      const segment = spelling[2].match(/^\/jobs\/view\/([^/]+)\/?$/)?.[1];
      if (!segment || /%/.test(spelling[3] ?? "")) return null;
      // Only the title segment may be decoded. Slash, backslash, dot, percent,
      // controls and route/query delimiters are absent from this allowlist.
      const title = decodeURIComponent(segment);
      if (!/^[\p{L}\p{N}_\-'’()\u2010-\u2015]+$/u.test(title)) return null;
      const match = segment.match(/(?:^|-)([0-9]+)$/);
      if (!match) return null;
      if ([...raw.searchParams.keys()].some((key) => key.toLowerCase() === "jk")) return null;
      // Reuse the existing host/query policy with a plain numeric path. This
      // value is validation-only: opens and durable approvals remain exact.
      canonicalListingUrl(`https://${spelling[1]}/jobs/view/${match[1]}${spelling[3] ?? ""}`);
      return `linkedin:${match[1]}`;
    }
    const url = new URL(canonicalListingUrl(value));
    return `indeed:${url.searchParams.get("jk")}`;
  } catch { return null; }
}

function approved(value) {
  if (!listingIdentity(value) || canonicalListingUrl(value) !== value) invalid();
  return value;
}

async function privatePath(value, root) {
  if (typeof value !== "string" || !value || value.includes("\0")) invalid();
  const base = path.resolve(root);
  const file = path.resolve(value);
  const relative = path.relative(base, file);
  if (!relative || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)
      || relative === ".." || !file.endsWith(".json")) invalid();
  // Existing ancestors must not be links; no directories are created implicitly.
  for (let current = path.parse(base).root, parts = path.dirname(file).slice(current.length).split(path.sep); parts.length;) {
    current = path.join(current, parts.shift());
    const stat = await fs.lstat(current);
    if (!stat.isDirectory() || stat.isSymbolicLink()) invalid();
    if (current === base || current.startsWith(`${base}${path.sep}`)) {
      if ((stat.mode & 0o077) !== 0 || (process.getuid && stat.uid !== process.getuid())) invalid();
    }
  }
  try {
    const stat = await fs.lstat(file);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size > MAX_BYTES
        || (stat.mode & 0o077) !== 0 || (process.getuid && stat.uid !== process.getuid())) invalid();
  } catch (error) { if (error.code !== "ENOENT") throw error; }
  return file;
}

function validateState(state) {
  if (!keysAre(state, ["version", "bindings"]) || state.version !== 1
      || !Array.isArray(state.bindings) || state.bindings.length > MAX_TARGETS) invalid();
  const ids = new Set();
  for (const row of state.bindings) {
    if (!keysAre(row, ["targetId", "approvedUrl"]) || !targetId(row.targetId) || ids.has(row.targetId)) invalid();
    approved(row.approvedUrl);
    ids.add(row.targetId);
  }
  return state;
}

async function loadState(file) {
  let handle;
  try {
    handle = await fs.open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
    const stat = await handle.stat();
    if (!stat.isFile() || stat.nlink !== 1 || stat.size > MAX_BYTES) invalid();
    return validateState(JSON.parse(await handle.readFile("utf8")));
  } catch (error) {
    if (error.code === "ENOENT") return { version: 1, bindings: [] };
    throw error;
  } finally { await handle?.close(); }
}

async function saveState(file, state, root) {
  const data = JSON.stringify(validateState(state));
  if (Buffer.byteLength(data) > MAX_BYTES) invalid();
  await privatePath(file, root);
  const temporary = `${file}.${randomUUID()}.tmp`;
  const handle = await fs.open(temporary, "wx", 0o600);
  try {
    await handle.writeFile(data);
    await handle.sync();
    await handle.close();
    await privatePath(file, root);
    await fs.rename(temporary, file);
  } finally {
    await handle.close();
    await fs.unlink(temporary).catch((error) => { if (error.code !== "ENOENT") throw error; });
  }
}

function pagesFrom(value) {
  if (!Array.isArray(value) || value.length > MAX_TARGETS) invalid();
  const ids = new Set();
  for (const tab of value) {
    if (!tab || !targetId(tab.id) || ids.has(tab.id) || typeof tab.type !== "string") invalid();
    ids.add(tab.id);
    if (tab.type === "page" && (typeof tab.url !== "string" || !tab.url || tab.url.length > MAX_URL)) invalid();
  }
  const pages = value.filter((tab) => tab.type === "page");
  // Opening a target must not create the first window/session.
  if (!pages.length) invalid();
  return pages;
}

/** Dependency injection is for offline tests; CLI has no endpoint/root override. */
export async function runBridge(args, { fetchImpl = globalThis.fetch, privateRoot = PRIVATE_ROOT } = {}) {
  const [statePath, operation, url, ...extra] = args;
  if (extra.length || !["list-tabs", "open-listing"].includes(operation)
      || (operation === "list-tabs" ? url !== undefined : url === undefined)) invalid();
  if (operation === "open-listing") approved(url);
  const file = await privatePath(statePath, privateRoot);
  const state = await loadState(file);
  const signal = AbortSignal.timeout(7000);
  let endpoint;
  const readJson = async (response) => {
    if (!response.ok || !response.body) invalid();
    const chunks = [];
    let size = 0;
    for await (const chunk of response.body) {
      size += chunk.length;
      if (size > MAX_BYTES) { await response.body.cancel?.().catch(() => {}); invalid(); }
      chunks.push(Buffer.from(chunk));
    }
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  };
  const unavailableCodes = new Set(["ECONNREFUSED", "EHOSTUNREACH", "ENETUNREACH", "EADDRNOTAVAIL"]);
  const isUnavailable = (error) => {
    let cause = error;
    while (cause && typeof cause === "object") {
      if (unavailableCodes.has(cause.code)) return true;
      cause = cause.cause;
    }
    return false;
  };
  const request = async (route, method = "GET") => {
    if (endpoint) {
      return readJson(await fetchImpl(`${endpoint}${route}`, { method, redirect: "error", signal }));
    }
    // The initial validated URL snapshot chooses one fixed loopback endpoint
    // for this invocation. Never switch endpoints after a mutation begins.
    if (route !== "/json/list" || method !== "GET") invalid();
    let response;
    let selectedEndpoint = "http://127.0.0.1:9222";
    try {
      response = await fetchImpl(`http://127.0.0.1:9222${route}`, { method, redirect: "error", signal });
    } catch (error) {
      if (!isUnavailable(error) || signal.aborted) throw error;
      response = null;
    }
    if (response?.status === 404 || !response) {
      if (response?.status === 404) {
        try { await response.body?.cancel?.(); } catch { /* best-effort connection cleanup */ }
      }
      selectedEndpoint = "http://[::1]:9222";
      try {
        response = await fetchImpl(`${selectedEndpoint}${route}`, { method, redirect: "error", signal });
      } catch { invalid(); }
    }
    const payload = await readJson(response);
    pagesFrom(payload);
    endpoint = selectedEndpoint;
    return payload;
  };
  const pages = pagesFrom(await request("/json/list"));
  const present = new Set(pages.map((tab) => tab.id));
  const retained = state.bindings.filter((row) => present.has(row.targetId));
  if (retained.length !== state.bindings.length) {
    state.bindings = retained;
    await saveState(file, state, privateRoot);
  }
  if (operation === "list-tabs") {
    const bindings = new Map(state.bindings.map((row) => [row.targetId, row.approvedUrl]));
    return pages.map((tab) => {
      const original = bindings.get(tab.id);
      return original && listingIdentity(tab.url) === listingIdentity(original) ? original : tab.url;
    });
  }
  if (state.bindings.length >= MAX_TARGETS) invalid();
  const created = await request(`/json/new?${encodeURIComponent(url)}`, "PUT");
  if (!created || created.type !== "page" || !targetId(created.id) || present.has(created.id)) invalid();
  // Persist creation before confirmation: a timeout must not lose the only
  // source-specific binding for a target that Chrome already opened.
  state.bindings.push({ targetId: created.id, approvedUrl: url });
  await saveState(file, state, privateRoot);
  for (let attempt = 0; attempt < 20; attempt++) {
    const current = pagesFrom(await request("/json/list")).find((tab) => tab.id === created.id);
    if (current && listingIdentity(current.url) === listingIdentity(url)) return null;
    if (signal.aborted) invalid();
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  invalid();
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const result = await runBridge(process.argv.slice(2));
    if (result !== null) process.stdout.write(`${JSON.stringify(result)}\n`);
  } catch {
    process.stderr.write("existing Chrome listing bridge unavailable\n");
    process.exitCode = 2;
  }
}
