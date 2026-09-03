/**
 * Private, direct-stream bridge for an already-connected Codex Chrome binding.
 * It has no network listener and never owns a browser session or window.
 */

const MAX_REQUEST_BYTES = 16 * 1024;
const MAX_TAB_URLS = 512;
const MAX_TAB_URL_LENGTH = 8192;
const MAX_REQUEST_ID_LENGTH = 128;
const LINKEDIN_PATH = /^\/jobs\/view\/[A-Za-z0-9_-]+\/?$/;
const INDEED_JOB_ID = /^[A-Za-z0-9_-]+$/;
const SAFE_TRACKING_KEYS = new Set(["campaign", "from", "mcid", "ref", "source", "trackingid", "trk"]);
const REQUEST_ID = /^[A-Za-z0-9_-]+$/;

function fail(message) {
  throw new Error(message);
}

function safeTrackingKey(key) {
  const normalized = key.toLowerCase();
  return SAFE_TRACKING_KEYS.has(normalized) || normalized.startsWith("utm_");
}

/** Return a canonical supported listing URL, or throw without exposing input. */
export function canonicalListingUrl(value) {
  if (typeof value !== "string" || value.length === 0 || value.length > MAX_TAB_URL_LENGTH) fail("listing URL is invalid");
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    fail("listing URL is invalid");
  }
  const host = parsed.hostname.toLowerCase().replace(/\.$/, "");
  if (
    parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.port || parsed.hash || !host ||
    [...parsed.searchParams.keys()].some((key) => !key || (key.toLowerCase() !== "jk" && !safeTrackingKey(key)))
  ) fail("listing URL is invalid");
  if (host === "linkedin.com" || host.endsWith(".linkedin.com")) {
    if (!LINKEDIN_PATH.test(parsed.pathname) || parsed.searchParams.has("jk")) fail("listing URL is invalid");
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

function exactCanonicalListing(value) {
  const canonical = canonicalListingUrl(value);
  if (value !== canonical) fail("listing URL must already be canonical");
  return canonical;
}

function requireChromeBinding(chrome) {
  if (
    chrome == null || typeof chrome !== "object" || chrome.user == null || chrome.tabs == null ||
    typeof chrome.user.openTabs !== "function" || typeof chrome.tabs.new !== "function"
  ) throw new TypeError("chrome must be an already-connected Codex Chrome binding");
  return chrome;
}

function requireStreams(options) {
  if (
    options == null || typeof options !== "object" || Array.isArray(options) ||
    Object.keys(options).length !== 2 || !("input" in options) || !("output" in options) ||
    options.input == null || typeof options.input[Symbol.asyncIterator] !== "function" ||
    options.output == null || typeof options.output.write !== "function" || typeof options.output.once !== "function"
  ) throw new TypeError("explicit input and output streams are required");
  return options;
}

function validRequestId(value) {
  return typeof value === "string" && value.length > 0 && value.length <= MAX_REQUEST_ID_LENGTH && REQUEST_ID.test(value);
}

/** Recover only a valid opaque ID; never expose any other untrusted field. */
function requestIdFromLine(line) {
  try {
    const payload = JSON.parse(line.toString("utf8"));
    if (payload != null && typeof payload === "object" && !Array.isArray(payload) && validRequestId(payload.id)) {
      return payload.id;
    }
  } catch {
    // A malformed frame has no trustworthy ID to echo.
  }
  return null;
}

/**
 * Recover a bounded opaque root-level ID while discarding an oversized frame.
 *
 * The complete frame remains invalid, but a syntactically recognizable ID can
 * safely be echoed in the generic failure response. This scanner retains no
 * URLs or unbounded payload data.
 */
class OversizedRequestIdScanner {
  constructor() {
    this.depth = 0;
    this.inString = false;
    this.escaped = false;
    this.captureId = false;
    this.value = null;
    this.current = "";
    this.lastRootString = null;
    this.pendingRootKey = null;
  }

  push(bytes) {
    if (this.value !== null) return;
    for (const byte of bytes) {
      if (this.inString) {
        if (this.escaped) {
          if (this.captureId || this.depth === 1) this.#append(byte);
          this.escaped = false;
          continue;
        }
        if (byte === 0x5c) {
          if (this.captureId || this.depth === 1) this.#append(byte);
          this.escaped = true;
          continue;
        }
        if (byte !== 0x22) {
          if (this.captureId || this.depth === 1) this.#append(byte);
          continue;
        }
        this.inString = false;
        if (this.captureId) {
          this.#finishId();
          this.captureId = false;
        } else if (this.depth === 1) {
          this.lastRootString = this.current;
        }
        this.current = "";
        continue;
      }
      if (byte === 0x22) {
        this.inString = true;
        this.current = "";
        this.captureId = this.depth === 1 && this.pendingRootKey === "id";
        continue;
      }
      if (byte === 0x7b || byte === 0x5b) {
        this.depth += 1;
        this.pendingRootKey = null;
        this.lastRootString = null;
        continue;
      }
      if (byte === 0x7d || byte === 0x5d) {
        this.depth = Math.max(0, this.depth - 1);
        this.pendingRootKey = null;
        this.lastRootString = null;
        continue;
      }
      if (byte === 0x3a && this.depth === 1 && this.lastRootString !== null) {
        this.pendingRootKey = this.lastRootString;
        this.lastRootString = null;
        continue;
      }
      if (byte === 0x2c) {
        this.pendingRootKey = null;
        this.lastRootString = null;
      } else if (byte !== 0x20 && byte !== 0x09 && byte !== 0x0d && byte !== 0x0a) {
        this.lastRootString = null;
      }
    }
  }

  #append(byte) {
    if (this.current.length <= MAX_REQUEST_ID_LENGTH * 6) this.current += String.fromCharCode(byte);
  }

  #finishId() {
    try {
      const candidate = JSON.parse(`"${this.current}"`);
      if (validRequestId(candidate)) this.value = candidate;
    } catch {
      // An invalid JSON string cannot supply a trustworthy opaque ID.
    }
  }
}

function parseRequest(line) {
  if (line.length === 0 || line.length > MAX_REQUEST_BYTES) fail("request is invalid");
  let payload;
  try {
    payload = JSON.parse(line.toString("utf8"));
  } catch {
    fail("request is invalid");
  }
  if (payload == null || typeof payload !== "object" || Array.isArray(payload)) fail("request is invalid");
  const id = payload.id;
  if (!validRequestId(id)) fail("request is invalid");
  if (payload.operation === "list_tab_urls" && Object.keys(payload).length === 2) return { id, operation: "list_tab_urls" };
  if (payload.operation === "open_listing" && Object.keys(payload).length === 3 && typeof payload.url === "string") {
    return { id, operation: "open_listing", url: exactCanonicalListing(payload.url) };
  }
  fail("request is invalid");
}

async function listTabUrls(chrome) {
  const tabs = await chrome.user.openTabs();
  if (!Array.isArray(tabs) || tabs.length === 0) fail("existing session is required");
  if (tabs.length > MAX_TAB_URLS) fail("invalid tab data");
  const urls = [];
  for (const tab of tabs) {
    if (typeof tab?.url !== "string" || tab.url.length === 0 || tab.url.length > MAX_TAB_URL_LENGTH) fail("invalid tab data");
    try {
      // Browser-private tabs are never sent over this bridge.
      urls.push(canonicalListingUrl(tab.url));
    } catch {
      // Unsupported tabs are deliberately not observable to the daemon.
    }
  }
  return urls;
}

async function openListing(chrome, url) {
  // Existing-session check is intentionally before tabs.new().
  const tabs = await chrome.user.openTabs();
  if (!Array.isArray(tabs) || tabs.length === 0) fail("existing session is required");
  const tab = await chrome.tabs.new();
  if (tab == null || typeof tab.goto !== "function" || typeof tab.markHandoff !== "function") fail("invalid handoff tab");
  await tab.goto(url);
  await tab.markHandoff();
}

function failure(id = null) {
  return { id, ok: false, error: "request_failed" };
}

async function writeJson(output, payload) {
  const bytes = Buffer.from(`${JSON.stringify(payload)}\n`, "utf8");
  if (bytes.length > MAX_TAB_URLS * (MAX_TAB_URL_LENGTH + 4) + 256) fail("response is invalid");
  if (output.write(bytes)) return;
  await new Promise((resolve, reject) => {
    const drain = () => { output.off("error", error); resolve(); };
    const error = (reason) => { output.off("drain", drain); reject(reason); };
    output.once("drain", drain);
    output.once("error", error);
  });
}

async function handle(chrome, line) {
  const requestId = requestIdFromLine(line);
  try {
    const request = parseRequest(line);
    if (request.operation === "list_tab_urls") return { id: request.id, ok: true, urls: await listTabUrls(chrome) };
    await openListing(chrome, request.url);
    return { id: request.id, ok: true };
  } catch {
    // Never reflect URLs, browser state, or binding exceptions.
    return failure(requestId);
  }
}

/**
 * Start the strict, serialized NDJSON service using explicit streams only.
 *
 * Requests are ``{"id":"opaque","operation":"list_tab_urls"}`` or
 * ``{"id":"opaque","operation":"open_listing","url":"https://..."}``.
 */
export function startCodexChromeExtensionHost(chromeBinding, options) {
  const chrome = requireChromeBinding(chromeBinding);
  const { input, output } = requireStreams(options);
  let closed = false;
  const finished = (async () => {
    let parts = [];
    let length = 0;
    let oversized = false;
    let oversizedId = new OversizedRequestIdScanner();
    for await (const chunk of input) {
      if (closed) break;
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      let start = 0;
      for (let index = 0; index < bytes.length; index += 1) {
        if (bytes[index] !== 0x0a) continue;
        if (!oversized) {
          let final = bytes.subarray(start, index);
          if (final.length > 0 && final[final.length - 1] === 0x0d) final = final.subarray(0, -1);
          if (length + final.length > MAX_REQUEST_BYTES) {
            oversizedId.push(Buffer.concat(parts, length));
            oversizedId.push(final);
            await writeJson(output, failure(oversizedId.value));
          } else {
            parts.push(final);
            length += final.length;
            await writeJson(output, await handle(chrome, Buffer.concat(parts, length)));
          }
        } else {
          oversizedId.push(bytes.subarray(start, index));
          await writeJson(output, failure(oversizedId.value));
        }
        parts = [];
        length = 0;
        oversized = false;
        oversizedId = new OversizedRequestIdScanner();
        start = index + 1;
      }
      const remainder = bytes.subarray(start);
      if (!oversized) {
        if (length + remainder.length > MAX_REQUEST_BYTES) {
          oversizedId.push(Buffer.concat(parts, length));
          oversizedId.push(remainder);
          parts = [];
          length = 0;
          oversized = true;
        } else if (remainder.length > 0) {
          parts.push(remainder);
          length += remainder.length;
        }
      }
    }
    if (!closed && (length > 0 || oversized)) await writeJson(output, failure(oversized ? oversizedId.value : null));
  })();
  return Object.freeze({
    finished,
    close() {
      closed = true;
      if (typeof input.destroy === "function") input.destroy();
    },
  });
}
