/**
 * Parent for the direct-stdio Smart Queue daemon bridge.
 *
 * The child owns its redacted status stdout.  Only child stderr (private
 * requests) and stdin (private responses) are connected to the bounded browser
 * service; no network listener, browser launch, or session creation exists.
 */

import { spawn as nodeSpawn } from "node:child_process";
import { Readable } from "node:stream";
import { fileURLToPath } from "node:url";

import { startCodexChromeExtensionHost } from "./codex_chrome_extension_host.mjs";

const DEFAULT_DAEMON_PATH = fileURLToPath(new URL("./smart_queue_daemon.py", import.meta.url));
const BRIDGE_CLOSE_EXIT_GRACE_MS = 100;
const MAX_STATUS_LINE_BYTES = 4096;
const MAX_STATUS_FAILURES = 1024;
const FINITE_STATUS_FIELDS = Object.freeze([
  "ticks_completed",
  "requested_open_count",
  "opened_count",
  "open_failed_count",
  "search_needed",
  "degraded_tick_count",
]);
const UNBOUNDED_STATUS_FIELDS = Object.freeze([
  "requested_open_count",
  "opened_count",
  "open_failed_count",
  "search_needed",
  "degraded_count",
]);
const STATUS_SCHEMAS = Object.freeze([FINITE_STATUS_FIELDS, UNBOUNDED_STATUS_FIELDS]);
const supervisedHosts = new Map();

function command(value, name) {
  if (typeof value !== "string" || value.length === 0 || value.length > 4096 || value.includes("\0")) {
    throw new TypeError(`${name} must be a non-empty command path`);
  }
  return value;
}

function daemonArguments(value) {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || item.includes("\0"))) {
    throw new TypeError("daemonArgs must be an array of strings");
  }
  if (value.filter((item) => item === "--bridge-stdio").length !== 1) {
    throw new TypeError("daemonArgs must include exactly one --bridge-stdio");
  }
  return [...value];
}

function optionalBindingId(value) {
  if (value === undefined) return undefined;
  if (typeof value !== "string" || value.includes("\0")) throw new TypeError("bindingId must be a string");
  return value.length > 0 ? value : undefined;
}

function daemonHostConfiguration(options) {
  if (options == null || typeof options !== "object" || Array.isArray(options)) throw new TypeError("options must be an object");
  const permitted = new Set(["bindingId", "daemonArgs", "daemonPath", "pythonExecutable", "spawn"]);
  if (Object.keys(options).some((key) => !permitted.has(key))) throw new TypeError("daemon host options are invalid");
  const spawn = options.spawn ?? nodeSpawn;
  if (typeof spawn !== "function") throw new TypeError("spawn must be a function");
  return Object.freeze({
    spawn,
    executable: command(options.pythonExecutable ?? "python3", "pythonExecutable"),
    daemonPath: command(options.daemonPath ?? DEFAULT_DAEMON_PATH, "daemonPath"),
    daemonArgs: daemonArguments(options.daemonArgs),
    bindingId: optionalBindingId(options.bindingId),
  });
}

function requireBrowserBinding(browser) {
  if (
    browser == null || typeof browser !== "object" || browser.user == null || browser.tabs == null ||
    typeof browser.user.openTabs !== "function" || typeof browser.tabs.new !== "function"
  ) throw new TypeError("browserBinding must be an already-connected browser binding");
  const rawNew = browser.tabs.new.bind(browser.tabs);
  // Legacy tabs are returned untouched: when markHandoff is absent the
  // downstream bridge fails closed as before. A default markHandoff exists
  // only on the generic wrapper's own synthetic tabs below.
  return {
    user: browser.user,
    tabs: {
      async new(...args) {
        return rawNew(...args);
      },
    },
  };
}

function isGenericBinding(browser) {
  return (
    browser != null && typeof browser === "object" &&
    typeof browser.listTabUrls === "function" && typeof browser.openListing === "function"
  );
}

function isLegacyBinding(browser) {
  return (
    browser != null && typeof browser === "object" &&
    browser.user != null && browser.tabs != null &&
    typeof browser.user.openTabs === "function" && typeof browser.tabs.new === "function"
  );
}

/**
 * Adapt either a generic `{listTabUrls, openListing}` binding or a legacy
 * Codex-shaped `{user.openTabs, tabs.new}` binding to the strict bridge.
 *
 * A binding matching BOTH shapes is ambiguous and fails closed with a
 * redacted TypeError rather than silently resolving as generic.
 */
function toBridgeBinding(browserBinding) {
  if (isGenericBinding(browserBinding)) {
    if (isLegacyBinding(browserBinding)) throw new TypeError("browserBinding is ambiguous");
    const generic = browserBinding;
    return {
      user: {
        async openTabs() {
          const urls = await generic.listTabUrls();
          // The generic wrapper's own synthetic tabs carry a no-op
          // markHandoff; this boundary check lives only here, never on
          // legacy bindings (see requireBrowserBinding).
          if (!Array.isArray(urls) || urls.some((url) => typeof url !== "string")) {
            throw new TypeError("browserBinding returned invalid tab data");
          }
          return urls.map((url) => ({ url }));
        },
      },
      tabs: {
        async new() {
          return {
            async goto(url) { await generic.openListing(url); },
            async markHandoff() {},
          };
        },
      },
    };
  }
  return requireBrowserBinding(browserBinding);
}

function requireChild(value) {
  if (
    value == null || typeof value !== "object" || value.stdin == null || value.stdout == null || value.stderr == null ||
    typeof value.stdin.write !== "function" || typeof value.stdout.on !== "function" || typeof value.stderr[Symbol.asyncIterator] !== "function" ||
    typeof value.once !== "function" || typeof value.kill !== "function"
  ) throw new TypeError("daemon did not provide required piped stdio");
  return value;
}

function boundedIncrement(value) {
  return value < MAX_STATUS_FAILURES ? value + 1 : MAX_STATUS_FAILURES;
}

function countOnlyStatus(line) {
  if (line.length === 0 || line.length > MAX_STATUS_LINE_BYTES) return null;
  let payload;
  try {
    payload = JSON.parse(line.toString("utf8"));
  } catch {
    return null;
  }
  if (payload == null || typeof payload !== "object" || Array.isArray(payload)) return null;
  const keys = Object.keys(payload);
  const schema = STATUS_SCHEMAS.find(
    (candidate) => keys.length === candidate.length && candidate.every((key) => keys.includes(key)),
  );
  if (schema === undefined) return null;
  const entries = schema.map((key) => [key, payload[key]]);
  if (entries.some(([, value]) => !Number.isSafeInteger(value) || value < 0)) return null;
  return Object.freeze(Object.fromEntries(entries));
}

function observeStatus(stdout) {
  let remainder = Buffer.alloc(0);
  let discardingOversizedFrame = false;
  let latestStatus = null;
  let invalidStatusCount = 0;
  let statusStreamErrored = false;
  let statusStreamEnded = false;
  let exposedStatusEnded = false;
  let statusBackpressured = false;
  let stdoutClosed = false;
  let terminal = null;
  let resolveDrained;
  const drained = new Promise((resolve) => { resolveDrained = resolve; });
  const status = new Readable({
    highWaterMark: MAX_STATUS_LINE_BYTES,
    read() {
      statusBackpressured = false;
    },
  });

  const recordInvalid = () => {
    latestStatus = null;
    invalidStatusCount = boundedIncrement(invalidStatusCount);
  };
  const record = (line) => {
    if (statusStreamEnded) return;
    const parsed = countOnlyStatus(line);
    if (parsed === null) recordInvalid();
    else {
      latestStatus = parsed;
      if (!statusBackpressured && !status.destroyed) {
        const frame = Buffer.from(`${JSON.stringify(parsed)}\n`, "utf8");
        statusBackpressured = !status.push(frame);
      }
    }
  };
  stdout.on("data", (chunk) => {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    let start = 0;
    while (start < bytes.length) {
      const newline = bytes.indexOf(0x0a, start);
      const end = newline === -1 ? bytes.length : newline;
      if (discardingOversizedFrame) {
        if (newline === -1) return;
        discardingOversizedFrame = false;
        start = newline + 1;
        continue;
      }

      const segment = bytes.subarray(start, end);
      if (remainder.length + segment.length > MAX_STATUS_LINE_BYTES) {
        recordInvalid();
        remainder = Buffer.alloc(0);
        if (newline === -1) {
          discardingOversizedFrame = true;
          return;
        }
        start = newline + 1;
        continue;
      }
      if (segment.length > 0) {
        remainder = remainder.length === 0 ? Buffer.from(segment) : Buffer.concat([remainder, segment]);
      }
      if (newline === -1) return;

      let line = remainder;
      if (line.length > 0 && line[line.length - 1] === 0x0d) line = line.subarray(0, -1);
      record(line);
      remainder = Buffer.alloc(0);
      start = newline + 1;
    }
  });
  const finish = () => {
    if (exposedStatusEnded || !stdoutClosed || terminal === null) return;
    exposedStatusEnded = true;
    if (!status.destroyed) {
      // This deliberately carries no daemon stderr, command, arguments,
      // URL, or browser details. It lets status consumers distinguish an
      // orderly daemon end from a failed spawn without retaining a dead host.
      status.push(Buffer.from(`${JSON.stringify({ terminal: terminal.terminalError ?? "exited" })}\n`, "utf8"));
      status.push(null);
    }
    resolveDrained();
  };
  const closeStdout = () => {
    if (stdoutClosed) return;
    if (remainder.length > 0) record(remainder);
    remainder = Buffer.alloc(0);
    stdoutClosed = true;
    statusStreamEnded = true;
    finish();
  };
  stdout.once("end", closeStdout);
  stdout.once("close", closeStdout);
  stdout.on("error", () => {
    statusStreamErrored = true;
    closeStdout();
  });
  return Object.freeze({
    status,
    latestStatus: () => latestStatus,
    health: () => Object.freeze({
      latestStatus,
      invalidStatusCount,
      statusBackpressured,
      statusStreamEnded,
      statusStreamErrored,
    }),
    drain(terminalStatus) {
      terminal = terminalStatus;
      finish();
      return drained;
    },
  });
}

function observeExit(child) {
  let exit = null;
  let resolveFinished;
  const finished = new Promise((resolve) => { resolveFinished = resolve; });
  const record = (exitCode, signalCode, terminalError = null) => {
    if (exit !== null) return;
    const terminal = {
      exitCode: Number.isInteger(exitCode) ? exitCode : null,
      signalCode: typeof signalCode === "string" ? signalCode : null,
    };
    // Never expose a spawn exception, command path, or daemon arguments.
    if (terminalError !== null) terminal.terminalError = terminalError;
    exit = Object.freeze(terminal);
    resolveFinished(exit);
  };
  if (child.exitCode !== null || child.signalCode !== null) record(child.exitCode, child.signalCode);
  else {
    child.once("exit", (exitCode, signalCode) => record(exitCode, signalCode));
    child.once("close", (exitCode, signalCode) => record(exitCode, signalCode));
    child.once("error", () => record(null, null, "spawn_error"));
  }
  return Object.freeze({ finished, exit: () => exit });
}

function runtimeKey(configuration) {
  // The supervisor intentionally keys only local process configuration, never
  // browser bindings, URLs, candidate data, or external service identifiers.
  // An explicit non-empty bindingId selects a separate singleton slot.
  if (configuration.bindingId !== undefined) {
    return JSON.stringify([
      configuration.executable, configuration.daemonPath, configuration.daemonArgs, configuration.bindingId,
    ]);
  }
  return JSON.stringify([configuration.executable, configuration.daemonPath, configuration.daemonArgs]);
}

/**
 * Start a Python daemon with a private direct NDJSON browser bridge.
 *
 * The caller supplies daemon arguments, rather than this parent accepting an
 * endpoint or token.  This prevents accidental fallback to the old HTTP path.
 */
export function startSmartQueueDaemonHost(browserBinding, options) {
  const configuration = daemonHostConfiguration(options);
  let child;
  try {
    child = requireChild(configuration.spawn(
      configuration.executable,
      [configuration.daemonPath, ...configuration.daemonArgs],
      { shell: false, stdio: ["pipe", "pipe", "pipe"] },
    ));
  } catch {
    throw new Error("smart queue daemon failed to start");
  }
  const statusObserver = observeStatus(child.stdout);
  const exitObserver = observeExit(child);
  let stopping;
  let bridgeTerminal = null;
  let bridgeQuarantined = false;
  let bridgeEnded = false;
  let bridgeCloseTimer = null;
  let bridge;
  try {
    bridge = startCodexChromeExtensionHost(toBridgeBinding(browserBinding), { input: child.stderr, output: child.stdin });
  } catch (error) {
    child.kill("SIGTERM");
    throw error;
  }
  const finished = exitObserver.finished.then(async (terminal) => {
    // Closing the logical bridge fences every browser operation that has not
    // yet been dispatched. A dispatched mutation cannot be cancelled through
    // the generic browser Promise API, so retain this host until it settles.
    bridge.close();
    bridgeQuarantined = bridge.mutationPending;
    await statusObserver.drain(terminal);
    await bridge.quiescent.catch(() => undefined);
    bridgeQuarantined = false;
    return terminal;
  });
  // A bridge stream failure is terminal to this host but never publishes its
  // error object, which could contain private request data. A clean premature
  // bridge end is terminal too, but stderr EOF can arrive just before the
  // child's exit/close event during an orderly shutdown.
  const terminateForBridge = (reason) => {
    if (
      stopping !== undefined || bridgeTerminal !== null || exitObserver.exit() !== null ||
      child.exitCode !== null || child.signalCode !== null
    ) return;
    bridgeTerminal = reason;
    try {
      child.kill("SIGTERM");
    } catch {
      // Health is already terminal; do not expose child-process details.
    }
  };
  const cancelBridgeCloseWait = () => {
    if (bridgeCloseTimer === null) return;
    clearTimeout(bridgeCloseTimer);
    bridgeCloseTimer = null;
  };
  const waitForExitAfterBridgeClose = () => {
    if (
      stopping !== undefined || bridgeTerminal !== null || exitObserver.exit() !== null ||
      child.exitCode !== null || child.signalCode !== null
    ) return;
    bridgeCloseTimer = setTimeout(() => {
      bridgeCloseTimer = null;
      terminateForBridge("bridge_closed");
    }, BRIDGE_CLOSE_EXIT_GRACE_MS);
  };
  void exitObserver.finished.then(cancelBridgeCloseWait);
  void bridge.finished.then(
    () => {
      bridgeEnded = true;
      waitForExitAfterBridgeClose();
    },
    () => {
      bridgeEnded = true;
      cancelBridgeCloseWait();
      terminateForBridge("bridge_failed");
    },
  );
  const host = Object.freeze({
    // The child stdout is always drained internally. This separate readable
    // exposes only bounded, count-only frames and cannot backpressure Python.
    status: statusObserver.status,
    get latestStatus() { return statusObserver.latestStatus(); },
    get health() {
      const statusHealth = statusObserver.health();
      const terminalExit = exitObserver.exit();
      const running = terminalExit === null && bridgeTerminal === null && !bridgeEnded;
      const ready = (
        statusHealth.latestStatus !== null
        && !statusHealth.statusStreamEnded
        && !statusHealth.statusStreamErrored
        && terminalExit === null
        && bridgeTerminal === null
        && !bridgeEnded
        && stopping === undefined
      );
      return Object.freeze({
        ...statusHealth,
        running,
        ready,
        healthy: running && ready,
        exit: terminalExit,
        quarantined: bridgeQuarantined,
        terminal: bridgeQuarantined ? "quarantined" : (bridgeTerminal ?? terminalExit),
      });
    },
    get exit() { return exitObserver.exit(); },
    getLatestStatus() { return statusObserver.latestStatus(); },
    getHealth() { return this.health; },
    finished,
    stop() {
      if (stopping !== undefined) return stopping;
      stopping = (async () => {
        cancelBridgeCloseWait();
        bridge.close();
        if (typeof child.stdin.end === "function") child.stdin.end();
        if (exitObserver.exit() === null) child.kill("SIGTERM");
        await finished;
        await bridge.finished.catch(() => undefined);
      })();
      return stopping;
    },
  });
  return host;
}

/**
 * Start a daemon once per safe local runtime configuration in this module.
 *
 * This opt-in helper retains the active handle until it exits, so repeated
 * calls cannot create duplicate monitors for the same private queue process.
 * It deliberately does not retain a browser binding as part of the key unless
 * the caller supplies an explicit non-empty bindingId.
 */
export function startOrGetSmartQueueDaemonHost(browserBinding, options) {
  const configuration = daemonHostConfiguration(options);
  const key = runtimeKey(configuration);
  const existing = supervisedHosts.get(key);
  if (existing !== undefined) return existing;
  const host = startSmartQueueDaemonHost(browserBinding, options);
  supervisedHosts.set(key, host);
  host.finished.then(() => {
    if (supervisedHosts.get(key) === host) supervisedHosts.delete(key);
  });
  return host;
}
