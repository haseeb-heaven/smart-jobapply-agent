/**
 * Start the bounded, loopback-only host for an already-connected Codex Chrome.
 *
 * The server owns no browser session or window.  It only lists URL strings
 * from existing tabs and opens exact canonical LinkedIn/Indeed listing URLs
 * as manual-review tabs.  Its bearer token lives only in the returned object.
 */

import { timingSafeEqual, randomBytes } from "node:crypto";
import { createServer } from "node:http";

const API_ROOT = "/v1";
const MAX_REQUEST_BYTES = 16 * 1024;
const MAX_TAB_URLS = 512;
const MAX_TAB_URL_LENGTH = 8192;
const LINKEDIN_PATH = /^\/jobs\/view\/[A-Za-z0-9_-]+\/?$/;
const INDEED_JOB_ID = /^[A-Za-z0-9_-]+$/;
const LOOPBACK_HOSTS = new Set(["127.0.0.1"]);
const SAFE_TRACKING_KEYS = new Set(["campaign", "from", "mcid", "ref", "source", "trackingid", "trk"]);

function fail(message) {
  throw new Error(message);
}

function isSafeTrackingKey(key) {
  const normalized = key.toLowerCase();
  return SAFE_TRACKING_KEYS.has(normalized) || normalized.startsWith("utm_");
}

/** Return one credential-free canonical listing URL or throw. */
export function canonicalListingUrl(value) {
  if (typeof value !== "string" || value.length === 0 || value.length > MAX_TAB_URL_LENGTH) {
    fail("listing URL is invalid");
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    fail("listing URL is invalid");
  }
  const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
  if (
    parsed.protocol !== "https:" ||
    parsed.username ||
    parsed.password ||
    parsed.port ||
    parsed.hash ||
    !host ||
    [...parsed.searchParams.keys()].some((key) => !key || (key.toLowerCase() !== "jk" && !isSafeTrackingKey(key)))
  ) {
    fail("listing URL is invalid");
  }
  if (host === "linkedin.com" || host.endsWith(".linkedin.com")) {
    if (!LINKEDIN_PATH.test(parsed.pathname) || parsed.searchParams.has("jk")) {
      fail("listing URL is invalid");
    }
    return `https://${host}${parsed.pathname.replace(/\/$/, "")}`;
  }
  if (host === "indeed.com" || host.endsWith(".indeed.com")) {
    const jobIds = parsed.searchParams.getAll("jk");
    if (parsed.pathname.replace(/\/$/, "") !== "/viewjob" || jobIds.length !== 1 || !INDEED_JOB_ID.test(jobIds[0])) {
      fail("listing URL is invalid");
    }
    return `https://${host}/viewjob?jk=${encodeURIComponent(jobIds[0])}`;
  }
  fail("listing URL is invalid");
}

function requireExactCanonicalListing(value) {
  const canonical = canonicalListingUrl(value);
  if (value !== canonical) {
    fail("listing URL must already be canonical");
  }
  return canonical;
}

function requireChromeBinding(chrome) {
  if (
    chrome == null ||
    typeof chrome !== "object" ||
    chrome.user == null ||
    typeof chrome.user.openTabs !== "function" ||
    chrome.tabs == null ||
    typeof chrome.tabs.new !== "function"
  ) {
    throw new TypeError("chrome must be an already-connected Codex Chrome binding");
  }
  return chrome;
}

function requireHost(value) {
  if (value === undefined) return "127.0.0.1";
  if (typeof value !== "string" || !LOOPBACK_HOSTS.has(value)) {
    throw new TypeError("host must be a literal loopback address");
  }
  return value;
}

function requirePort(value) {
  if (value === undefined) return 0;
  if (!Number.isInteger(value) || value < 0 || value > 65535) {
    throw new TypeError("port must be an integer between 0 and 65535");
  }
  return value;
}

function requireToken(value) {
  const token = value ?? randomBytes(32).toString("base64url");
  if (typeof token !== "string" || !/^[A-Za-z0-9_-]{32,256}$/.test(token)) {
    throw new TypeError("token must be an in-memory opaque bearer token");
  }
  return token;
}

function authenticated(request, token) {
  const expected = Buffer.from(`Bearer ${token}`, "utf8");
  const actual = Buffer.from(request.headers.authorization ?? "", "utf8");
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

async function readJson(request) {
  const pieces = [];
  let length = 0;
  for await (const piece of request) {
    length += piece.length;
    if (length > MAX_REQUEST_BYTES) fail("request is too large");
    pieces.push(piece);
  }
  try {
    const payload = JSON.parse(Buffer.concat(pieces).toString("utf8"));
    if (payload == null || typeof payload !== "object" || Array.isArray(payload)) fail("request is invalid");
    return payload;
  } catch {
    fail("request is invalid");
  }
}

function json(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
  });
  response.end(body);
}

async function listTabUrls(chrome) {
  const tabs = await chrome.user.openTabs();
  if (!Array.isArray(tabs) || tabs.length > MAX_TAB_URLS) fail("Chrome returned invalid tab data");
  const listingUrls = new Set();
  for (const tab of tabs) {
    const url = tab?.url;
    if (typeof url !== "string" || url.length === 0 || url.length > MAX_TAB_URL_LENGTH) {
      fail("Chrome returned invalid tab data");
    }
    try {
      // The existing browser can contain personal, account, search, or other
      // unrelated tabs.  They stay inside the host: only a canonical supported
      // listing URL may cross this URL-only bridge boundary.
      listingUrls.add(canonicalListingUrl(url));
    } catch {
      // Unsupported or noncanonical tab URLs are deliberately invisible to
      // the queue rather than being forwarded for downstream filtering.
    }
  }
  return [...listingUrls];
}

async function openListing(chrome, url) {
  // Require an existing browser session before requesting a new tab.  This
  // host never creates or activates a browser window/session itself.
  const existingTabs = await chrome.user.openTabs();
  if (!Array.isArray(existingTabs) || existingTabs.length === 0) {
    fail("an existing Chrome session is required");
  }
  const tab = await chrome.tabs.new();
  if (tab == null || typeof tab.goto !== "function" || typeof tab.markHandoff !== "function") {
    fail("Chrome returned an invalid manual-review tab");
  }
  await tab.goto(url);
  await tab.markHandoff();
}

/**
 * Start a local authenticated API for a pre-connected Chrome binding.
 *
 * The returned token is intentionally in-memory only.  Call ``close()`` when
 * this short-lived host is no longer needed; it performs no browser action.
 */
export async function startCodexChromeExtensionHost(chromeBinding, options = {}) {
  const chrome = requireChromeBinding(chromeBinding);
  if (options == null || typeof options !== "object" || Array.isArray(options)) {
    throw new TypeError("options must be an object");
  }
  const host = requireHost(options.host);
  const port = requirePort(options.port);
  const token = requireToken(options.token);
  const server = createServer(async (request, response) => {
    try {
      if (!authenticated(request, token)) {
        json(response, 404, { error: "not found" });
        return;
      }
      if (request.method === "GET" && request.url === `${API_ROOT}/tab-urls`) {
        json(response, 200, { urls: await listTabUrls(chrome) });
        return;
      }
      if (request.method === "POST" && request.url === `${API_ROOT}/open-listing`) {
        const payload = await readJson(request);
        if (Object.keys(payload).length !== 1 || typeof payload.url !== "string") fail("request is invalid");
        await openListing(chrome, requireExactCanonicalListing(payload.url));
        response.writeHead(204, { "Cache-Control": "no-store" });
        response.end();
        return;
      }
      json(response, 404, { error: "not found" });
    } catch {
      // Never reflect URLs, token material, or browser errors to the caller.
      if (!response.headersSent) json(response, 400, { error: "request failed" });
      else response.destroy();
    }
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen({ host, port, exclusive: true }, () => {
      server.off("error", reject);
      resolve();
    });
  });
  const address = server.address();
  if (address == null || typeof address === "string" || !LOOPBACK_HOSTS.has(address.address)) {
    await new Promise((resolve) => server.close(resolve));
    throw new Error("local host did not bind to loopback");
  }
  return Object.freeze({
    endpoint: `http://${address.address}:${address.port}`,
    token,
    close: () => new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve()))),
  });
}
