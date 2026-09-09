"""Focused contracts for the private runtime ownership boundary."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


_PATH = Path(__file__).parents[1] / "skills" / "easy-apply-tab-monitor" / "scripts" / "runtime_ownership.py"
_SPEC = importlib.util.spec_from_file_location("runtime_ownership_test", _PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


@pytest.fixture(autouse=True)
def private_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(_MODULE, "PRIVATE_ROOT", tmp_path.resolve())


def test_control_frames_are_strict_and_never_admit_private_extension_fields() -> None:
    valid = json.dumps({"version": 1, "id": "opaque", "operation": "acquire"}).encode()
    assert _MODULE._frame(valid) == {"version": 1, "id": "opaque", "operation": "acquire"}
    assert _MODULE._frame(json.dumps({"version": 1, "id": "opaque", "operation": "acquire", "url": "private"}).encode()) is None
    assert _MODULE._frame(b"{") is None


def test_brokered_lease_requires_a_real_inherited_owned_handshake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b'{"version":1,"state":"owned"}')
        monkeypatch.setenv("SMART_QUEUE_BROKER_OWNERSHIP_FD", str(read_fd))
        lease = _MODULE.BrokeredLease.from_environment(tmp_path / "queue.sqlite3")
        assert lease is not None
        lease.acquire()
        lease.release()
        os.write(write_fd, b"R")
        os.close(write_fd)
        write_fd = -1
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def test_spoofed_environment_without_broker_handshake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    read_fd, write_fd = os.pipe()
    try:
        monkeypatch.setenv("SMART_QUEUE_BROKER_OWNERSHIP_FD", str(read_fd))
        os.write(write_fd, b"not-a-control-frame")
        with pytest.raises(_MODULE.RuntimeOwnershipError, match="runtime ownership unavailable"):
            _MODULE.BrokeredLease.from_environment(tmp_path / "queue.sqlite3")
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_recovery_marker_only_blocks_or_allows_explicit_operator_recovery(tmp_path: Path) -> None:
    database = tmp_path / "queue.sqlite3"
    _MODULE._write_marker(database)
    marker = _MODULE._recovery_marker(database)
    assert marker.read_text(encoding="utf-8") == "recovery-required\n"
    _MODULE._clear_marker(database)
    assert not marker.exists()


def test_broker_pipe_loss_marks_recovery_and_fences_next_monitor_acquire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database = tmp_path / "queue.sqlite3"
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b'{"version":1,"state":"owned"}')
        monkeypatch.setenv("SMART_QUEUE_BROKER_OWNERSHIP_FD", str(read_fd))
        lease = _MODULE.BrokeredLease.from_environment(database)
        lease.acquire()
        os.close(write_fd)
        write_fd = -1
        deadline = time.monotonic() + 1
        while not _MODULE._recovery_marker(database).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _MODULE._recovery_marker(database).exists()
        with pytest.raises(_MODULE.RuntimeOwnershipError, match="runtime ownership unavailable"):
            lease.acquire()
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize("kind", ["absolute", "traversal", "sibling", "directory", "database-link", "parent-link", "root-link", "marker-link", "marker-directory", "marker-hardlink", "lock-link"])
def test_invalid_paths_never_acquire_or_mutate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: bool, kind: str
) -> None:
    root = tmp_path / "private"
    root.mkdir()
    outside = tmp_path / "private-other"
    outside.mkdir()
    target = outside / "sentinel"
    target.write_text("preserve")
    monkeypatch.setattr(_MODULE, "PRIVATE_ROOT", root)
    database = root / "queue.sqlite3"
    if kind == "absolute":
        database = outside / "new.sqlite3"
    elif kind == "traversal":
        database = root / ".." / "private" / "queue.sqlite3"
    elif kind == "sibling":
        database = outside / "queue.sqlite3"
    elif kind == "directory":
        database.mkdir()
    elif kind == "database-link":
        database.symlink_to(target)
    elif kind == "parent-link":
        (root / "linked").symlink_to(outside, target_is_directory=True)
        database = root / "linked" / "queue.sqlite3"
    elif kind == "root-link":
        root.rmdir()
        root.symlink_to(outside, target_is_directory=True)
    elif kind == "marker-link":
        _MODULE._recovery_marker(database).symlink_to(target)
    elif kind == "marker-directory":
        _MODULE._recovery_marker(database).mkdir()
    elif kind == "marker-hardlink":
        os.link(target, _MODULE._recovery_marker(database))
    elif kind == "lock-link":
        database.with_name(database.name + ".persistent-smart-queue-monitor.lock").symlink_to(target)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid path reached lease/control access")
    monkeypatch.setattr(_MODULE, "DatabaseLease", forbidden)
    monkeypatch.setattr(_MODULE, "_control_stream", forbidden)
    args = ["--database", str(database)]
    args += ["--recover-after-browser-quiesced"] if recovery else ["--", "unused-daemon"]
    assert _MODULE.main(args) == 2
    assert target.read_text() == "preserve"
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before


@pytest.mark.parametrize("recovery", [False, True])
def test_real_cli_has_no_private_root_override(tmp_path: Path, recovery: bool) -> None:
    database = tmp_path / "outside.sqlite3"
    marker = _MODULE._recovery_marker(database)
    marker.write_text("preserve")
    args = [sys.executable, str(_PATH), "--database", str(database)]
    args += ["--recover-after-browser-quiesced"] if recovery else ["--", "unused-daemon"]
    result = subprocess.run(args, capture_output=True, timeout=5, check=False)
    assert result.returncode == 2
    assert result.stdout == result.stderr == b""
    assert marker.read_text() == "preserve"
    assert sorted(path.name for path in tmp_path.iterdir()) == [marker.name]


def test_recovery_and_marker_writes_require_exclusive_ownership(tmp_path: Path) -> None:
    database = tmp_path / "queue.sqlite3"
    _MODULE._write_marker(database)
    marker = _MODULE._recovery_marker(database)
    before = marker.read_bytes()
    lease = _MODULE.DatabaseLease(database)
    lease.acquire()
    try:
        assert _MODULE.main(["--database", str(database), "--recover-after-browser-quiesced"]) == 2
        with pytest.raises(_MODULE.MonitorLeaseError):
            _MODULE._write_marker(database)
        assert marker.read_bytes() == before
    finally:
        lease.release()
    assert _MODULE.main(["--database", str(database), "--recover-after-browser-quiesced"]) == 0
    assert not marker.exists()


def test_unowned_broker_eof_never_creates_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    database = tmp_path / "queue.sqlite3"
    monkeypatch.setattr(_MODULE, "_control_stream", lambda: (io.BytesIO(), io.BytesIO()))
    assert _MODULE.main(["--database", str(database), "--", "unused-daemon"]) == 2
    assert list(tmp_path.iterdir()) == []


def test_broker_requires_quiesce_then_drain_before_releasing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    operations = ["release", "acquire", "drained", "release", "quiesce", "drained", "release"]
    reader = io.BytesIO(b"".join(json.dumps({"version": 1, "id": str(index), "operation": operation}).encode() + b"\n"
                                for index, operation in enumerate(operations)))
    class Writer(io.BytesIO):
        def close(self) -> None:
            pass
    writer = Writer()
    class Child:
        def poll(self) -> int:
            return 0
        def wait(self) -> int:
            return 0
    descriptors = []
    def spawn(*args: object, **kwargs: object) -> Child:
        # Retain the inherited read end so the broker's release signal has a
        # reader, just as it does in the real child process.
        descriptors.append(os.dup(kwargs["pass_fds"][0]))
        return Child()
    monkeypatch.setattr(_MODULE, "_control_stream", lambda: (reader, writer))
    monkeypatch.setattr(_MODULE.subprocess, "Popen", spawn)
    try:
        assert _MODULE.main(["--database", str(tmp_path / "queue.sqlite3"), "--", "unused-daemon"]) == 0
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    assert [json.loads(line)["state"] for line in writer.getvalue().splitlines()] == [
        "unavailable", "acquired", "unavailable", "unavailable", "quiesced", "drained", "released"
    ]
    assert not _MODULE._recovery_marker(tmp_path / "queue.sqlite3").exists()
