/** Strict, redacted control channel for the local runtime-ownership broker. */

const VERSION = 1;
const MAX_LINE_BYTES = 1024;
const STATES = {
  acquire: ["acquired", "busy", "unavailable"],
  quiesce: ["quiesced", "unavailable"],
  drained: ["drained", "unavailable"],
  release: ["released", "unavailable"],
};

function frame(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const keys = Object.keys(value);
  if (!keys.every((key) => ["version", "id", "operation", "state"].includes(key))) return null;
  if (value.version !== VERSION || typeof value.id !== "string" || value.id.length === 0 || value.id.length > 128) return null;
  if (typeof value.operation !== "string" || typeof value.state !== "string") return null;
  if (!Object.hasOwn(STATES, value.operation) || !STATES[value.operation].includes(value.state)) return null;
  return value;
}

/**
 * A tiny request/reply channel that accepts only fixed ownership frames.
 * It intentionally exposes no broker diagnostics or runtime paths.
 */
export function startRuntimeOwnershipControl(stream) {
  if (stream == null || typeof stream.write !== "function" || typeof stream.on !== "function") {
    throw new TypeError("runtime ownership control is unavailable");
  }
  let remainder = Buffer.alloc(0);
  let ended = false;
  let sequence = 0;
  const pending = new Map();
  const fail = () => {
    if (ended) return;
    ended = true;
    for (const { reject } of pending.values()) reject(new Error("runtime ownership unavailable"));
    pending.clear();
  };
  stream.on("data", (chunk) => {
    if (ended || !Buffer.isBuffer(chunk)) return fail();
    remainder = Buffer.concat([remainder, chunk]);
    if (remainder.length > MAX_LINE_BYTES && !remainder.includes(0x0a)) return fail();
    while (!ended) {
      const newline = remainder.indexOf(0x0a);
      if (newline === -1) break;
      const line = remainder.subarray(0, newline);
      remainder = remainder.subarray(newline + 1);
      if (line.length === 0 || line.length > MAX_LINE_BYTES) return fail();
      let parsed;
      try { parsed = frame(JSON.parse(line.toString("utf8"))); } catch { parsed = null; }
      if (parsed === null) return fail();
      const request = pending.get(parsed.id);
      if (request === undefined || request.operation !== parsed.operation) return fail();
      pending.delete(parsed.id);
      request.resolve(parsed.state);
    }
    if (remainder.length > MAX_LINE_BYTES) fail();
  });
  stream.once("error", fail);
  stream.once("end", fail);
  stream.once("close", fail);
  return Object.freeze({
    request(operation) {
      if (ended || !["acquire", "quiesce", "drained", "release"].includes(operation)) {
        return Promise.reject(new Error("runtime ownership unavailable"));
      }
      const id = `o${++sequence}`;
      return new Promise((resolve, reject) => {
        pending.set(id, { operation, resolve, reject });
        try {
          stream.write(`${JSON.stringify({ version: VERSION, id, operation })}\n`, (error) => {
            if (error) fail();
          });
        } catch { fail(); }
      });
    },
    get closed() { return ended; },
  });
}
