/**
 * Listing-only adapter for a host-selected, already-connected Playwright
 * BrowserContext.  It intentionally imports no Playwright package and never
 * selects, launches, or closes browser contexts.
 */

import { canonicalListingUrl } from "./codex_chrome_extension_host.mjs";
import { validateCapabilities } from "./browser_capabilities.mjs";
import { startOrGetSmartQueueDaemonHost } from "./smart_queue_daemon_host.mjs";

const MAX_TAB_URLS = 512;
const MAX_TAB_URL_LENGTH = 8192;

function requireExistingContext(context) {
  try {
    if (
      !context || typeof context !== "object" ||
      typeof context.pages !== "function" || typeof context.newPage !== "function"
    ) {
      throw new TypeError("existing browser context required");
    }
    return context;
  } catch (error) {
    if (error instanceof TypeError && error.message === "existing browser context required") {
      throw error;
    }
    throw new TypeError("existing browser context required");
  }
}

function snapshotPages(context) {
  const pages = context.pages();
  if (!Array.isArray(pages) || pages.length === 0 || pages.length > MAX_TAB_URLS) {
    throw new Error("browser snapshot unavailable");
  }
  for (const page of pages) {
    if (
      !page || typeof page.url !== "function" || typeof page.isClosed !== "function" ||
      page.isClosed() !== false
    ) {
      throw new Error("browser snapshot unavailable");
    }
  }
  return pages;
}

/**
 * Return the two-operation listing binding for one explicit existing context.
 * Raw complete-session URLs remain inside the host bridge, which filters
 * unsupported URLs before the Python daemon can observe them.
 */
export function createPlaywrightListingBinding(context, capabilities) {
  validateCapabilities(capabilities);
  const existingContext = requireExistingContext(context);
  // ``newPage`` and ``goto`` are external effects.  Either can fail after the
  // browser has acted, so a later retry must not create another ambiguous tab.
  // Snapshots remain available for the host's normal recovery boundary.
  let openPaused = false;

  return Object.freeze({
    async listTabUrls() {
      try {
        const urls = snapshotPages(existingContext).map((page) => page.url());
        if (urls.some((url) => typeof url !== "string" || url.length === 0 || url.length > MAX_TAB_URL_LENGTH)) {
          throw new Error("browser snapshot unavailable");
        }
        return urls;
      } catch {
        throw new Error("browser snapshot unavailable");
      }
    },

    async openListing(url) {
      const canonical = canonicalListingUrl(url);
      if (canonical !== url) throw new Error("listing URL must already be canonical");
      if (openPaused) throw new Error("browser_open_paused");
      let mutationStarted = false;
      try {
        snapshotPages(existingContext);
        mutationStarted = true;
        const page = await existingContext.newPage();
        if (!page || typeof page.goto !== "function") {
          throw new Error("listing open unavailable");
        }
        await page.goto(canonical, { waitUntil: "commit", timeout: 10000 });
      } catch {
        if (mutationStarted) openPaused = true;
        throw new Error("listing open unavailable");
      }
    },
  });
}

/**
 * Start the existing supervised daemon host for one capability-declared
 * Playwright session.  A caller cannot replace the descriptor's session
 * identity through the lower-level host options.
 */
export function startPlaywrightSmartQueueDaemonHost(context, capabilities, options) {
  const descriptor = validateCapabilities(capabilities);
  try {
    if (
      !options || typeof options !== "object" || Array.isArray(options) ||
      (Object.hasOwn(options, "bindingId") && options.bindingId !== descriptor.sessionId)
    ) {
      throw new TypeError("browser binding session is invalid");
    }
  } catch (error) {
    if (error instanceof TypeError && error.message === "browser binding session is invalid") {
      throw error;
    }
    throw new TypeError("browser binding session is invalid");
  }
  const binding = createPlaywrightListingBinding(context, descriptor);
  return startOrGetSmartQueueDaemonHost(binding, {
    ...options,
    bindingId: descriptor.sessionId,
  });
}
