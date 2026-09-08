import { expect, test } from "@playwright/test";
import { spawnSync } from "node:child_process";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { createPlaywrightListingBinding } from "../../skills/easy-apply-tab-monitor/scripts/playwright_listing_binding.mjs";
import { startSmartQueueDaemonHost } from "../../skills/easy-apply-tab-monitor/scripts/smart_queue_daemon_host.mjs";

// Synthetic canonical listing URLs. The test routes every request and never
// reads page content; page closure is strictly a test-fixture simulation of a
// candidate vacating a tab, not an application action.
const LISTINGS = Object.freeze([
  "https://www.linkedin.com/jobs/view/910001",
  "https://www.linkedin.com/jobs/view/910002",
  "https://www.linkedin.com/jobs/view/910003",
]);
const CAPABILITIES = Object.freeze({
  protocolVersion: 1,
  sessionId: "synthetic-queue-cycle",
  existingSession: true,
  completeUrlSnapshots: true,
  exactListingOpen: true,
  persistentConnection: true,
});

test("synthetic capacity-two cycle refills a vacated listing without inferring an outcome", async ({ context }) => {
  await context.route("**/*", async (route) => {
    if (!LISTINGS.includes(route.request().url())) {
      await route.abort();
      return;
    }
    await route.fulfill({ status: 200, contentType: "text/html", body: "<!doctype html>" });
  });

  // The initially supplied page is the existing selected session.
  await context.newPage();
  const binding = createPlaywrightListingBinding(context, CAPABILITIES);
  await binding.openListing(LISTINGS[0]);
  await binding.openListing(LISTINGS[1]);
  expect(await binding.listTabUrls()).toEqual(expect.arrayContaining(LISTINGS.slice(0, 2)));

  const vacated = context.pages().find((page) => page.url() === LISTINGS[0]);
  expect(vacated).toBeDefined();
  await vacated.close(); // Test-only simulated human tab vacancy.

  expect(await binding.listTabUrls()).not.toContain(LISTINGS[0]);
  await binding.openListing(LISTINGS[2]);
  const urls = await binding.listTabUrls();
  expect(urls).toEqual(expect.arrayContaining([LISTINGS[1], LISTINGS[2]]));
  expect(urls).not.toContain(LISTINGS[0]);
});

test("real Node stdio host, Python daemon, and SQLite queue refill a synthetic browser vacancy", { timeout: 30_000 }, async ({ context }) => {
  const repository = fileURLToPath(new URL("../..", import.meta.url));
  const fixtureRoot = fileURLToPath(new URL("../../jobapply_agent/private/browser-tests/", import.meta.url));
  await mkdir(fixtureRoot, { recursive: true });
  const runtime = await mkdtemp(join(fixtureRoot, "playwright-daemon-"));
  let completed = false;
  try {
    const prepared = spawnSync("python3", ["tests/browser/fixture_runtime.py", "--prepare", runtime], {
      cwd: repository,
      encoding: "utf8",
    });
    expect(prepared.status, prepared.stderr).toBe(0);
    const paths = JSON.parse(prepared.stdout);
    await context.route("**/*", async (route) => {
      if (!LISTINGS.includes(route.request().url())) return route.abort();
      return route.fulfill({ status: 200, contentType: "text/html", body: "<!doctype html>" });
    });
    const page = await context.newPage(); // Existing selected test context.
    const binding = createPlaywrightListingBinding(context, CAPABILITIES);
    const daemonArgs = (ticks) => [
      "--candidate-intake", paths.intake,
      "--database", paths.queue,
      "--max-ticks", String(ticks),
      "--bridge-stdio",
    ];
    const daemonPath = fileURLToPath(new URL("../../skills/easy-apply-tab-monitor/scripts/smart_queue_daemon.py", import.meta.url));

    const first = startSmartQueueDaemonHost(binding, { daemonArgs: daemonArgs(1), daemonPath });
    expect(await first.finished).toMatchObject({ exitCode: 0, signalCode: null });
    expect(await binding.listTabUrls()).toEqual(expect.arrayContaining(LISTINGS.slice(0, 2)));
    const vacated = context.pages().find((candidate) => candidate.url() === LISTINGS[0]);
    expect(vacated).toBeDefined();
    await vacated.close(); // Test-only simulated candidate vacancy.

    const second = startSmartQueueDaemonHost(binding, { daemonArgs: daemonArgs(1), daemonPath });
    expect(await second.finished).toMatchObject({ exitCode: 0, signalCode: null });
    const urls = await binding.listTabUrls();
    expect(urls).toEqual(expect.arrayContaining([LISTINGS[1], LISTINGS[2]]));
    expect(urls).not.toContain(LISTINGS[0]);
    expect(page.isClosed()).toBe(false);
    completed = true;
  } finally {
    // Preserve a failing synthetic fixture for local diagnosis, but only the
    // exact directory made by this test can be removed after a full pass.
    if (completed) await rm(runtime, { recursive: true, force: true });
  }
});
