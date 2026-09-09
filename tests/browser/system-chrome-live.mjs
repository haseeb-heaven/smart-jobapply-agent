/** Explicit manual test of existing Chrome. Never starts or closes a browser. */
import assert from "node:assert/strict";
import { execFile as execFileCallback } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { fixtureAliases } from "./system_chrome_test_bridge.mjs";

const root = fileURLToPath(new URL("../..", import.meta.url));
const endpoint = "http://127.0.0.1:9222";
const execFile = promisify(execFileCallback);
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const command = async (program, args, env = {}) => {
  try {
    return (await execFile(program, args, { cwd: root, env: { ...process.env, ...env },
      encoding: "utf8", timeout: 90_000, maxBuffer: 64 * 1024 })).stdout;
  } catch (error) {
    // Child output and argv can contain private paths. Keep diagnostics bounded.
    throw Error(`fixture subprocess failed (exit ${error.code ?? "unknown"}, killed ${Boolean(error.killed)})`);
  }
};
const request = async path => {
  const response = await fetch(endpoint + path, { signal: AbortSignal.timeout(8000) });
  if (!response.ok) throw Error("existing Chrome request failed");
  return response.json();
};
export function parseJournal(text) {
  const ids = text.trim().split(/\s+/).filter(Boolean);
  assert.ok(ids.every(id => /^[A-Za-z0-9_-]+$/.test(id)), "invalid creation journal");
  return new Set(ids);
}
const readOwned = async journal => {
  try { return parseJournal(await readFile(journal, "utf8")); }
  catch (error) { if (error.code === "ENOENT") return new Set(); throw error; }
};
export function managedTabs(rows, owned, aliases) {
  return rows.filter(row => row.type === "page" && owned.has(row.id) && aliases.has(row.url));
}
export async function cleanupOwned(owned, initialIds, list, close) {
  assert.ok([...owned].every(id => !initialIds.has(id)), "creation journal overlaps existing tabs");
  const errors = [];
  const visible = new Set((await list()).map(row => row.id));
  for (const id of owned) {
    if (!visible.has(id)) continue;
    try { await close(id); } catch { errors.push(id); }
  }
  const remaining = (await list()).filter(row => owned.has(row.id));
  if (remaining.length || errors.length) throw Error(`synthetic cleanup failed (${remaining.length} owned targets remain, ${errors.length} close errors)`);
}
const closeTarget = async id => {
  const response = await fetch(endpoint + "/json/close/" + encodeURIComponent(id), {
    method: "PUT", signal: AbortSignal.timeout(8000),
  });
  await response.text();
  assert.ok(response.ok, "test-created target close failed");
};
const tick = async (prepared, journal) => {
  const stdout = await command("python3", [
    join(root, "skills/easy-apply-tab-monitor/scripts/smart_queue_daemon.py"),
    "--candidate-intake", prepared.intake, "--database", prepared.queue,
    "--adapter", "external", "--max-ticks", "1", "--adapter-command", "node",
    join(root, "tests/browser/system_chrome_test_bridge.mjs"),
  ], { SMART_QUEUE_TEST_URLS: JSON.stringify(prepared.urls), SMART_QUEUE_TEST_JOURNAL: journal });
  assert.ok(stdout.trim(), "daemon must emit status");
};

// The CLI is deliberately excluded from automatic browser test discovery.
export async function runSystemChromeHarness() {
  const before = await request("/json/list");
  const initialIds = new Set(before.map(row => row.id));
  const fixtureRoot = join(root, "jobapply_agent/private/browser-tests");
  await mkdir(fixtureRoot, { recursive: true, mode: 0o700 });
  const runtime = await mkdtemp(join(fixtureRoot, "system-chrome-"));
  const journal = join(runtime, "created-target-ids");
  let stage = "prepare";
  let result;
  let failure;
  try {
    const prepared = JSON.parse(await command("python3", [join(root, "tests/browser/system_chrome_fixture.py"), "--prepare", runtime]));
    await writeFile(journal, "", { flag: "wx", mode: 0o600 });
    const aliases = fixtureAliases(prepared.urls);
    const waitForManaged = async expected => {
      let latest = [];
      for (let attempt = 0; attempt < 20; attempt++) {
        latest = managedTabs(await request("/json/list"), await readOwned(journal), aliases);
        if (latest.length === expected) return latest;
        await pause(250);
      }
      throw Error(`synthetic managed count ${latest.length}, expected ${expected}`);
    };
    stage = "first tick";
    await tick(prepared, journal);
    const first = await waitForManaged(5);
    const firstOwned = await readOwned(journal);
    assert.equal(firstOwned.size, 5, "first tick must create exactly five targets");
    assert.ok([...firstOwned].every(id => !initialIds.has(id)), "created IDs must be new");
    stage = "close three owned test tabs";
    const closedUrls = new Set(first.slice(0, 3).map(row => aliases.get(row.url)));
    for (const tab of first.slice(0, 3)) await closeTarget(tab.id);
    await waitForManaged(2);
    stage = "refill tick";
    await tick(prepared, journal);
    const managed = await waitForManaged(5);
    const owned = await readOwned(journal);
    assert.equal(owned.size, 8, "refill must create exactly three replacements");
    const currentUrls = managed.map(row => aliases.get(row.url));
    assert.equal(new Set(currentUrls).size, 5, "managed listings must be distinct");
    assert.ok(currentUrls.every(url => !closedUrls.has(url)), "closed listings must not reopen");
    stage = "outcome and unrelated target assertions";
    const outcomeCount = JSON.parse(await command("python3", ["-c",
      "import sqlite3,sys,json; print(json.dumps(sqlite3.connect(sys.argv[1]).execute('select count(*) from candidate_memory_outcomes').fetchone()[0]))", prepared.memory]));
    assert.equal(outcomeCount, 0, "closure must not infer outcomes");
    const after = await request("/json/list");
    assert.deepEqual(new Set(after.filter(row => initialIds.has(row.id)).map(row => row.id)), initialIds,
      "initial targets must remain");
    result = { initial_tab_count: before.length, created_count: owned.size,
      closed_test_tab_count: 3, managed_counts: [5, 2, 5], inferred_outcome_count: outcomeCount };
  } catch (error) {
    failure = Error(`${stage}: ${error.message}`);
  } finally {
    try {
      await cleanupOwned(await readOwned(journal), initialIds, () => request("/json/list"), closeTarget);
      await rm(runtime, { recursive: true, force: true });
    } catch (error) {
      // Retain the exact-ID journal for recovery; never silently lose ownership.
      failure = Error(`${failure ? failure.message + "; " : ""}cleanup: ${error.message}; private recovery journal retained`);
    }
  }
  if (failure) throw failure;
  return result;
}
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  runSystemChromeHarness().then(result => {
    console.log(JSON.stringify(result)); process.exit(0);
  }, error => {
    console.error(`synthetic system-Chrome harness failed: ${error.message}`); process.exit(1);
  });
}
