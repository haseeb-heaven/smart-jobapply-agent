/**
 * Validate the host-declared browser capabilities required by Smart Queue.
 *
 * This descriptor is deliberately descriptive rather than an authentication
 * mechanism.  Callers must still perform a live bridge preflight before queue
 * mutation.
 */

const CAPABILITY_KEYS = Object.freeze([
  "protocolVersion",
  "sessionId",
  "existingSession",
  "completeUrlSnapshots",
  "exactListingOpen",
  "persistentConnection",
]);

const SESSION_ID = /^[A-Za-z0-9_-]{1,128}$/;

/** Return an immutable, strictly validated browser capability descriptor. */
export function validateCapabilities(value) {
  try {
    if (
      !value || typeof value !== "object" || Array.isArray(value) ||
      Object.keys(value).length !== CAPABILITY_KEYS.length ||
      !CAPABILITY_KEYS.every((key) => Object.hasOwn(value, key)) ||
      value.protocolVersion !== 1 ||
      typeof value.sessionId !== "string" ||
      !SESSION_ID.test(value.sessionId) ||
      !CAPABILITY_KEYS.slice(2).every((key) => value[key] === true)
    ) {
      throw new TypeError("browser capabilities unavailable");
    }
    return Object.freeze({
      protocolVersion: value.protocolVersion,
      sessionId: value.sessionId,
      existingSession: value.existingSession,
      completeUrlSnapshots: value.completeUrlSnapshots,
      exactListingOpen: value.exactListingOpen,
      persistentConnection: value.persistentConnection,
    });
  } catch (error) {
    if (error instanceof TypeError && error.message === "browser capabilities unavailable") {
      throw error;
    }
    throw new TypeError("browser capabilities unavailable");
  }
}
