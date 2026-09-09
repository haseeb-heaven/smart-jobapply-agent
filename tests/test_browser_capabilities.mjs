import assert from "node:assert/strict";
import test from "node:test";

import { validateCapabilities } from "../skills/easy-apply-tab-monitor/scripts/browser_capabilities.mjs";

const valid = Object.freeze({
  protocolVersion: 1,
  sessionId: "session-01",
  existingSession: true,
  completeUrlSnapshots: true,
  exactListingOpen: true,
  persistentConnection: true,
});

test("accepts and freezes a complete capability descriptor", () => {
  const result = validateCapabilities({ ...valid });
  assert.deepEqual(result, valid);
  assert.equal(Object.isFrozen(result), true);
  assert.throws(() => { result.sessionId = "changed"; }, TypeError);
});

test("returns a defensive copy rather than freezing caller-owned input", () => {
  const input = { ...valid };
  const result = validateCapabilities(input);
  input.sessionId = "caller-mutated";
  assert.equal(result.sessionId, "session-01");
});

test("rejects malformed descriptors without echoing private values", () => {
  const cases = [
    [null, "null"],
    [[], "array"],
    [{ ...valid, protocolVersion: 2 }, "version"],
    [{ ...valid, protocolVersion: "1" }, "version type"],
    [{ ...valid, sessionId: "" }, "empty id"],
    [{ ...valid, sessionId: "x".repeat(129) }, "long id"],
    [{ ...valid, sessionId: "session with spaces" }, "unsafe id"],
    [{ ...valid, existingSession: false }, "missing session"],
    [{ ...valid, completeUrlSnapshots: false }, "incomplete snapshots"],
    [{ ...valid, exactListingOpen: false }, "non-exact open"],
    [{ ...valid, persistentConnection: false }, "non-persistent"],
    [{ ...valid, completeUrlSnapshots: 1 }, "nonboolean capability"],
    [{ ...valid, privateToken: "do-not-echo" }, "extra key"],
  ];

  for (const [value, label] of cases) {
    assert.throws(
      () => validateCapabilities(value),
      (error) => {
        assert.equal(error instanceof TypeError, true, label);
        assert.match(String(error), /browser capabilities unavailable/);
        assert.doesNotMatch(String(error), /private-session|do-not-echo|session with spaces/);
        return true;
      },
      label,
    );
  }
});

test("rejects every missing required field", () => {
  for (const key of Object.keys(valid)) {
    const value = { ...valid };
    delete value[key];
    assert.throws(() => validateCapabilities(value), /browser capabilities unavailable/);
  }
});
