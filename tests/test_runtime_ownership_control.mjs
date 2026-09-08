import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import { startRuntimeOwnershipControl } from "../skills/easy-apply-tab-monitor/scripts/runtime_ownership_host.mjs";

function harness(write) {
  const stream = new EventEmitter();
  const sent = [];
  stream.write = (raw, callback) => {
    sent.push(JSON.parse(raw));
    if (write) return write(raw, callback);
    callback();
    return true;
  };
  const control = startRuntimeOwnershipControl(stream);
  const reply = (request, state, extra = {}) => Buffer.from(`${JSON.stringify({ ...request, state, ...extra })}\n`);
  return { stream, sent, control, reply };
}

test("ordered acquisition, quiescence, drain and release use distinct correlated IDs", async () => {
  const { stream, sent, control, reply } = harness();
  for (const [operation, state] of [["acquire", "acquired"], ["quiesce", "quiesced"], ["drained", "drained"], ["release", "released"]]) {
    const result = control.request(operation);
    stream.emit("data", reply(sent.at(-1), state));
    assert.equal(await result, state);
  }
  assert.equal(new Set(sent.map(({ id }) => id)).size, 4);
  assert.equal(control.closed, false);
});

test("concurrent replies correlate by ID even when delivered out of order", async () => {
  const { stream, sent, control, reply } = harness();
  const first = control.request("acquire");
  const second = control.request("quiesce");
  stream.emit("data", Buffer.concat([reply(sent[1], "quiesced"), reply(sent[0], "acquired")]));
  assert.deepEqual(await Promise.all([first, second]), ["acquired", "quiesced"]);
});

test("partial frames resolve only after the complete newline-delimited reply", async () => {
  const { stream, sent, control, reply } = harness();
  let settled = false;
  const result = control.request("acquire").then((value) => { settled = true; return value; });
  const bytes = reply(sent[0], "acquired");
  stream.emit("data", bytes.subarray(0, bytes.length - 1));
  await Promise.resolve();
  assert.equal(settled, false);
  stream.emit("data", bytes.subarray(bytes.length - 1));
  assert.equal(await result, "acquired");
});

const invalid = {
  malformed: () => Buffer.from("{\n"),
  empty: () => Buffer.from("\n"),
  array: () => Buffer.from("[]\n"),
  oversize: () => Buffer.from(`${"x".repeat(1025)}\n`),
  "oversize partial": () => Buffer.alloc(1025, 120),
  "wrong ID": (request, reply) => reply(request, "acquired", { id: "unknown" }),
  "wrong operation": (request, reply) => reply(request, "quiesced", { operation: "quiesce" }),
  "wrong version": (request, reply) => reply(request, "acquired", { version: 2 }),
  "unknown state": (request, reply) => reply(request, "secret diagnostic"),
  "operation/state mismatch": (request, reply) => reply(request, "released"),
  "extra field": (request, reply) => reply(request, "acquired", { url: "private" }),
  "non-buffer chunk": () => "{}\n",
};
for (const [name, payload] of Object.entries(invalid)) {
  test(`${name} fails every pending request and closes the channel`, async () => {
    const { stream, sent, control, reply } = harness();
    const failures = [assert.rejects(control.request("acquire"), /^Error: runtime ownership unavailable$/),
      assert.rejects(control.request("quiesce"), /^Error: runtime ownership unavailable$/)];
    stream.emit("data", payload(sent[0], reply));
    await Promise.all(failures);
    assert.equal(control.closed, true);
    await assert.rejects(control.request("release"), /runtime ownership unavailable/);
    assert.equal(sent.length, 2);
  });
}

for (const event of ["end", "error", "close"]) {
  test(`stream ${event} rejects partial and concurrent requests`, async () => {
    const { stream, control } = harness();
    const failures = [assert.rejects(control.request("acquire")), assert.rejects(control.request("quiesce"))];
    stream.emit("data", Buffer.from('{"version":'));
    stream.emit(event, new Error("private diagnostic"));
    await Promise.all(failures);
    assert.equal(control.closed, true);
  });
}

for (const mode of ["throw", "callback"]) {
  test(`write ${mode} failure is redacted and terminal`, async () => {
    const { control } = harness((_raw, callback) => {
      if (mode === "throw") throw new Error("private diagnostic");
      queueMicrotask(() => callback(new Error("private diagnostic")));
    });
    await assert.rejects(control.request("acquire"), /^Error: runtime ownership unavailable$/);
    assert.equal(control.closed, true);
  });
}

test("oversize remainder after a valid line immediately rejects remaining requests", async () => {
  const { stream, sent, control, reply } = harness();
  const valid = control.request("acquire");
  const failure = assert.rejects(control.request("quiesce"));
  stream.emit("data", Buffer.concat([reply(sent[0], "acquired"), Buffer.alloc(1025, 120)]));
  assert.equal(await valid, "acquired");
  await failure;
  assert.equal(control.closed, true);
});

test("duplicate response cannot resolve a later request", async () => {
  const { stream, sent, control, reply } = harness();
  const first = control.request("acquire");
  const bytes = reply(sent[0], "acquired");
  stream.emit("data", bytes);
  await first;
  const failure = assert.rejects(control.request("quiesce"));
  stream.emit("data", bytes);
  await failure;
  assert.equal(control.closed, true);
});

test("invalid operations are never written", async () => {
  const { control, sent } = harness();
  await assert.rejects(control.request("open_listing"));
  assert.deepEqual(sent, []);
});
