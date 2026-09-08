import { expect, test } from "@playwright/test";

import { createPlaywrightListingBinding } from "../../skills/easy-apply-tab-monitor/scripts/playwright_listing_binding.mjs";

const LISTING = "https://www.linkedin.com/jobs/view/123456";
const CAPABILITIES = Object.freeze({
  protocolVersion: 1,
  sessionId: "synthetic-playwright-session",
  existingSession: true,
  completeUrlSnapshots: true,
  exactListingOpen: true,
  persistentConnection: true,
});

test("uses a supplied context to open an exact synthetic listing", async ({ context, page }) => {
  await context.route("**/*", async (route) => {
    if (route.request().url() !== LISTING) {
      await route.abort();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "text/html",
      body: "<!doctype html><title>Synthetic listing fixture</title>",
    });
  });
  expect(page.url()).toBe("about:blank");

  const binding = createPlaywrightListingBinding(context, CAPABILITIES);
  await binding.openListing(LISTING);

  expect(await binding.listTabUrls()).toContain(LISTING);
  expect(context.pages()).toHaveLength(2);
});

test("rejects an application route without creating a test page", async ({ context }) => {
  const binding = createPlaywrightListingBinding(context, CAPABILITIES);
  const before = context.pages().length;

  await expect(binding.openListing(`${LISTING}/apply/`)).rejects.toThrow("listing URL is invalid");

  expect(context.pages()).toHaveLength(before);
});
