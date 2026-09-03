/**
 * Deprecated Codex Chrome alias for the generic Smart Queue daemon host.
 *
 * New code must import `startSmartQueueDaemonHost` /
 * `startOrGetSmartQueueDaemonHost` from `./smart_queue_daemon_host.mjs`.
 * The Codex Chrome extension bridge remains one tested reference integration.
 */

export {
  startSmartQueueDaemonHost as startCodexSmartQueueDaemonHost,
  startOrGetSmartQueueDaemonHost as startOrGetCodexSmartQueueDaemonHost,
} from "./smart_queue_daemon_host.mjs";
