/**
 * Parent for the direct-stdio Smart Queue daemon bridge.
 *
 * The child owns its redacted status stdout.  Only child stderr (private
 * requests) and stdin (private responses) are connected to the bounded Chrome
 * service; no network listener, browser launch, or session creation exists.
 */

import { spawn as nodeSpawn } from "node:child_process";
import { fileURLToPath } from "node:url";

import { startCodexChromeExtensionHost } from "./codex_chrome_extension_host.mjs";

const DEFAULT_DAEMON_PATH = fileURLToPath(new URL("./smart_queue_daemon.py", import.meta.url));

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

function requireChild(value) {
  if (
    value == null || typeof value !== "object" || value.stdin == null || value.stdout == null || value.stderr == null ||
    typeof value.stdin.write !== "function" || typeof value.stderr[Symbol.asyncIterator] !== "function" ||
    typeof value.once !== "function" || typeof value.kill !== "function"
  ) throw new TypeError("daemon did not provide required piped stdio");
  return value;
}

function waitForClose(child) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
  return new Promise((resolve) => child.once("close", resolve));
}

/**
 * Start a Python daemon with a private direct NDJSON browser bridge.
 *
 * The caller supplies daemon arguments, rather than this parent accepting an
 * endpoint or token.  This prevents accidental fallback to the old HTTP path.
 */
export function startCodexSmartQueueDaemonHost(chromeBinding, options) {
  if (options == null || typeof options !== "object" || Array.isArray(options)) throw new TypeError("options must be an object");
  const permitted = new Set(["daemonArgs", "daemonPath", "pythonExecutable", "spawn"]);
  if (Object.keys(options).some((key) => !permitted.has(key))) throw new TypeError("daemon host options are invalid");
  const spawn = options.spawn ?? nodeSpawn;
  if (typeof spawn !== "function") throw new TypeError("spawn must be a function");
  const child = requireChild(spawn(
    command(options.pythonExecutable ?? "python3", "pythonExecutable"),
    [command(options.daemonPath ?? DEFAULT_DAEMON_PATH, "daemonPath"), ...daemonArguments(options.daemonArgs)],
    { shell: false, stdio: ["pipe", "pipe", "pipe"] },
  ));
  let bridge;
  try {
    bridge = startCodexChromeExtensionHost(chromeBinding, { input: child.stderr, output: child.stdin });
  } catch (error) {
    child.kill("SIGTERM");
    throw error;
  }
  let stopping;
  return Object.freeze({
    // Do not consume or transform redacted daemon status.
    status: child.stdout,
    stop() {
      if (stopping !== undefined) return stopping;
      stopping = (async () => {
        bridge.close();
        if (typeof child.stdin.end === "function") child.stdin.end();
        if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
        await waitForClose(child);
        await bridge.finished.catch(() => undefined);
      })();
      return stopping;
    },
  });
}
