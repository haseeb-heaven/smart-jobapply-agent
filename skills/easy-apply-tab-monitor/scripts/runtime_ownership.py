#!/usr/bin/env python3
"""Private lease broker for a Node-supervised Smart Queue runtime.

The broker has no browser authority.  It holds the existing database sibling
lease while its Node parent may still have an in-flight browser operation.  The
daemon receives only an inherited liveness pipe; there is deliberately no
command-line switch that disables daemon locking.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
from typing import Any


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIRECTORY.parents[2]
PACKAGE_SOURCE = PROJECT_ROOT / "jobapply_agent" / "src"
PRIVATE_ROOT = PROJECT_ROOT / "jobapply_agent" / "private"
if str(PACKAGE_SOURCE) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SOURCE))


def _load_monitor() -> object:
    spec = importlib.util.spec_from_file_location(
        "runtime_ownership_monitor", SCRIPT_DIRECTORY / "persistent_smart_queue_monitor.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("runtime ownership unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_monitor_module = _load_monitor()
DatabaseLease = _monitor_module.DatabaseLease
MonitorLeaseError = _monitor_module.MonitorLeaseError
PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1024
_OPERATIONS = frozenset({"acquire", "quiesce", "drained", "release"})


class RuntimeOwnershipError(RuntimeError):
    """A redacted ownership failure."""


def _control_stream() -> tuple[Any, Any]:
    """Return the broker-only duplex fd supplied as inherited descriptor 3."""

    try:
        reader = os.fdopen(os.dup(3), "rb", buffering=0)
        writer = os.fdopen(os.dup(3), "wb", buffering=0)
    except OSError:
        raise RuntimeOwnershipError("runtime ownership unavailable") from None
    return reader, writer


def _frame(raw: bytes) -> dict[str, str] | None:
    if not raw or len(raw) > MAX_FRAME_BYTES:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != {"version", "id", "operation"}:
        return None
    if value.get("version") != PROTOCOL_VERSION:
        return None
    if not isinstance(value.get("id"), str) or not 0 < len(value["id"]) <= 128:
        return None
    if value.get("operation") not in _OPERATIONS:
        return None
    return value


def _reply(writer: Any, request_id: str, operation: str, state: str) -> None:
    # The fixed fields intentionally cannot carry URLs, paths, candidate facts,
    # tokens, or diagnostic text.
    payload = json.dumps(
        {"version": PROTOCOL_VERSION, "id": request_id, "operation": operation, "state": state},
        separators=(",", ":"),
    ).encode("ascii") + b"\n"
    writer.write(payload)
    writer.flush()


class BrokeredLease:
    """Daemon-side monitor lease backed by a broker-owned inherited pipe."""

    def __init__(self, descriptor: int, database: Path) -> None:
        self._descriptor = descriptor
        self._database = database
        self._held = False
        self._lost = False
        self._released = False

    @classmethod
    def from_environment(cls, database: Path) -> "BrokeredLease | None":
        raw = os.environ.get("SMART_QUEUE_BROKER_OWNERSHIP_FD")
        if raw is None:
            return None
        database = _validated_database(database)
        try:
            descriptor = int(raw)
            if descriptor < 3:
                raise ValueError
            handshake = os.read(descriptor, 128)
            value = json.loads(handshake)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise RuntimeOwnershipError("runtime ownership unavailable") from None
        if value != {"version": PROTOCOL_VERSION, "state": "owned"}:
            raise RuntimeOwnershipError("runtime ownership unavailable")
        lease = cls(descriptor, database)
        # A zero-byte read is not a liveness check.  Hold a real blocking read
        # after the one-shot handshake so broker death is observable even when
        # its parent vanished abruptly. The broker writes `R` only during the
        # ordered drained/release path; EOF or any other byte is ambiguous.
        watcher = threading.Thread(target=lease._watch_broker, daemon=True)
        watcher.start()
        return lease

    def _watch_broker(self) -> None:
        try:
            terminal = os.read(self._descriptor, 1)
        except OSError:
            terminal = b""
        if terminal == b"R":
            self._released = True
            return
        self._lost = True
        try:
            _write_marker(self._database)
        except (OSError, RuntimeOwnershipError, MonitorLeaseError):
            # Fail closed at the daemon boundary even if a hostile filesystem
            # prevents marker creation; subsequent monitor acquisition raises.
            pass

    def acquire(self) -> None:
        if self._lost or self._released:
            raise RuntimeOwnershipError("runtime ownership unavailable") from None
        if self._held:
            return
        self._held = True

    def release(self) -> None:
        # Only the broker may release the OS lease after Node reports drained.
        self._held = False


def _recovery_marker(database: Path) -> Path:
    return database.with_name(f"{database.name}.runtime-ownership-recovery-required")


def _validated_private_file(path: Path) -> Path:
    """Reject lexical escapes and link-like components before filesystem access.

    The script-local private root has no CLI or environment override. As with
    intake validation, its namespace must remain owner-only and must not be
    concurrently changed by an untrusted process running as the same user.
    """
    path = Path(path)
    if ".." in path.parts:
        raise RuntimeOwnershipError("runtime ownership unavailable")
    candidate = path.absolute()
    root = PRIVATE_ROOT.absolute()
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        raise RuntimeOwnershipError("runtime ownership unavailable") from None
    if not relative.parts:
        raise RuntimeOwnershipError("runtime ownership unavailable")
    # Check the root's ancestors too: resolving first would hide a symlink.
    components = list(reversed(root.parents)) + [root]
    components += [root.joinpath(*relative.parts[:index]) for index in range(1, len(relative.parts) + 1)]
    for current in components:
        try:
            info = current.lstat()
        except FileNotFoundError:
            if current == candidate:
                return candidate
            raise RuntimeOwnershipError("runtime ownership unavailable") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeOwnershipError("runtime ownership unavailable")
        if current == candidate:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeOwnershipError("runtime ownership unavailable")
        elif not stat.S_ISDIR(info.st_mode):
            raise RuntimeOwnershipError("runtime ownership unavailable")
    return candidate


def _validated_database(database: Path) -> Path:
    database = _validated_private_file(database)
    _validated_private_file(_recovery_marker(database))
    _validated_private_file(database.with_name(f"{database.name}.persistent-smart-queue-monitor.lock"))
    return database


def _write_marker(database: Path, *, lease: Any = None) -> None:
    database = _validated_database(database)
    owned_here = lease is None
    lease = DatabaseLease(database) if owned_here else lease
    if owned_here:
        lease.acquire()
    try:
        if not lease.held:
            raise RuntimeOwnershipError("runtime ownership unavailable")
        marker = _recovery_marker(_validated_database(database))
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeOwnershipError("runtime ownership unavailable")
            os.ftruncate(descriptor, 0)
            os.write(descriptor, b"recovery-required\n")
        finally:
            os.close(descriptor)
    finally:
        if owned_here:
            lease.release()


def _clear_marker(database: Path) -> None:
    # Explicit operator confirmation of browser quiescence is required; the
    # exclusive lease also prevents recovery from clearing a live broker fence.
    database = _validated_database(database)
    lease = DatabaseLease(database)
    lease.acquire()
    try:
        marker = _recovery_marker(_validated_database(database))
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
    finally:
        lease.release()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--recover-after-browser-quiesced", action="store_true")
    parser.add_argument("daemon", nargs=argparse.REMAINDER)
    return parser


def _run_broker(database: Path, daemon: list[str]) -> int:
    database = _validated_database(database)
    if not daemon or daemon[0] != "--":
        raise RuntimeOwnershipError("runtime ownership unavailable")
    command = daemon[1:]
    if not command:
        raise RuntimeOwnershipError("runtime ownership unavailable")
    if _recovery_marker(database).exists():
        raise RuntimeOwnershipError("runtime ownership recovery required")
    reader, writer = _control_stream()
    lease = DatabaseLease(database)
    child: subprocess.Popen[bytes] | None = None
    ownership_write: int | None = None
    acquired = False
    quiesced = False
    drained = False
    try:
        for raw in reader:
            request = _frame(raw.rstrip(b"\r\n"))
            if request is None:
                raise RuntimeOwnershipError("runtime ownership unavailable")
            operation = request["operation"]
            if operation == "acquire":
                if acquired:
                    _reply(writer, request["id"], operation, "busy")
                    continue
                try:
                    _validated_database(database)
                    lease.acquire()
                except MonitorLeaseError:
                    _reply(writer, request["id"], operation, "busy")
                    continue
                acquired = True
                if _recovery_marker(_validated_database(database)).exists():
                    raise RuntimeOwnershipError("runtime ownership recovery required")
                read_fd, ownership_write = os.pipe()
                os.write(ownership_write, b'{"version":1,"state":"owned"}')
                environment = dict(os.environ)
                environment["SMART_QUEUE_BROKER_OWNERSHIP_FD"] = str(read_fd)
                child = subprocess.Popen(command, stdin=sys.stdin.buffer, stdout=sys.stdout.buffer,
                    stderr=sys.stderr.buffer, env=environment, pass_fds=(read_fd,))
                os.close(read_fd)
                _reply(writer, request["id"], operation, "acquired")
            elif operation == "quiesce" and acquired:
                quiesced = True
                _reply(writer, request["id"], operation, "quiesced")
            elif operation == "drained" and acquired and quiesced:
                drained = True
                _reply(writer, request["id"], operation, "drained")
            elif operation == "release" and acquired and drained:
                if child is not None:
                    # Node has already stopped dispatch and confirmed every
                    # browser promise settled.  It is now safe to interrupt
                    # the daemon's blocking bridge read before relinquishing
                    # the cross-process lease.
                    if ownership_write is not None:
                        os.write(ownership_write, b"R")
                    if child.poll() is None:
                        child.terminate()
                    child.wait()
                lease.release()
                acquired = False
                _reply(writer, request["id"], operation, "released")
                return 0
            else:
                _reply(writer, request["id"], operation, "unavailable")
        # EOF on the private control channel is parent loss, not a clean
        # release. It can occur while a browser-side operation is still
        # unobservable to this broker.
        raise RuntimeOwnershipError("runtime ownership unavailable")
    except (BrokenPipeError, RuntimeOwnershipError):
        # Losing the parent is ambiguous if a browser promise was dispatched.
        # Retain the lock and marker; another process must not take over.
        if not acquired:
            raise
        _write_marker(database, lease=lease)
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait()
        # Deliberately do not release lease. Keep this broker alive until an
        # operator terminates it after independently quiescing the browser.
        # Deliberately remain resident: OS descriptor lifetime is the lock
        # lifetime. A host crash must not make a pending remote `goto` appear
        # cancelled merely because the Python daemon has exited.
        threading.Event().wait()
        return 2  # pragma: no cover - explicit operator termination ends us.
    finally:
        if ownership_write is not None:
            os.close(ownership_write)
        writer.close()
        reader.close()
        # A normal release happened above. Any abnormal path leaves descriptor
        # ownership until process termination, paired with the marker.


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        arguments.database = _validated_database(arguments.database)
        if arguments.recover_after_browser_quiesced:
            if arguments.daemon:
                raise RuntimeOwnershipError("runtime ownership unavailable")
            _clear_marker(arguments.database)
            return 0
        return _run_broker(arguments.database, arguments.daemon)
    except (RuntimeOwnershipError, MonitorLeaseError, OSError, ValueError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
