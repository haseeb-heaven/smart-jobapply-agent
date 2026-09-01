/**
 * Persistent, review-only browser tab monitor.
 *
 * This module is intentionally browser-runtime agnostic: the caller supplies
 * the already-selected visible Chrome browser handle. It never clicks, fills,
 * uploads, submits, or follows an application link.
 */

const ALLOWED_ROOTS = ["linkedin.com", "indeed.com"];
const MAX_JOBS = 5;
const DEFAULT_INTERVAL_MS = 60_000;

function isAllowedListingUrl(value) {
  try {
    const parsed = new URL(value);
    const host = (parsed.hostname || "").toLowerCase().replace(/\.$/, "");
    return parsed.protocol === "https:" && ALLOWED_ROOTS.some((root) => host === root || host.endsWith(`.${root}`));
  } catch {
    return false;
  }
}

export function validateListingUrls(urls) {
  if (!Array.isArray(urls) || urls.length < 1 || urls.length > MAX_JOBS) {
    throw new Error(`urls must contain between one and ${MAX_JOBS} listings`);
  }
  const normalized = urls.map((value) => {
    if (typeof value !== "string" || !value.trim() || !isAllowedListingUrl(value.trim())) {
      throw new Error("every listing URL must be HTTPS LinkedIn or Indeed");
    }
    return value.trim();
  });
  if (new Set(normalized).size !== normalized.length) {
    throw new Error("listing URLs must be unique");
  }
  return normalized;
}

async function reconcile(browser, urls, state) {
  const openTabs = await browser.user.openTabs();
  const openUrls = new Set(openTabs.map((tab) => tab.url).filter(Boolean));
  const missing = urls.filter((url) => !openUrls.has(url));
  const reopened = [];
  const failed = [];
  for (const url of missing) {
    try {
      const tab = await browser.tabs.new();
      await tab.goto(url);
      await tab.markHandoff();
      reopened.push({ url, tabId: tab.id });
    } catch {
      // Keep the reason value-free; the next cycle can retry the same URL.
      failed.push({ url, reason: "navigation_failed" });
    }
  }
  const event = {
    at: new Date().toISOString(),
    observedOpenCount: openTabs.length,
    missingBeforeReopen: missing.map((url) => url),
    reopened,
    failed,
  };
  state.last = event;
  state.history.push(event);
  if (state.history.length > 20) state.history.shift();
  return event;
}

export async function startMonitor(browser, urls, { intervalMs = DEFAULT_INTERVAL_MS, runImmediately = true } = {}) {
  const normalizedUrls = validateListingUrls(urls);
  if (!Number.isFinite(intervalMs) || intervalMs < 1_000) {
    throw new Error("intervalMs must be at least 1000 milliseconds");
  }
  const state = {
    urls: normalizedUrls,
    intervalMs,
    running: true,
    busy: false,
    last: null,
    history: [],
    timer: null,
    stop() {
      if (this.timer !== null) clearInterval(this.timer);
      this.timer = null;
      this.running = false;
    },
    snapshot() {
      return {
        urls: [...this.urls],
        intervalMs: this.intervalMs,
        running: this.running,
        busy: this.busy,
        last: this.last,
        history: [...this.history],
      };
    },
  };
  const tick = async () => {
    if (!state.running || state.busy) return;
    state.busy = true;
    try {
      await reconcile(browser, normalizedUrls, state);
    } finally {
      state.busy = false;
    }
  };
  if (runImmediately) await tick();
  state.timer = setInterval(() => void tick(), intervalMs);
  return state;
}
