"""Contract tests for browser-neutral, listing-only tab adapters.

These tests use injected subprocess runners exclusively.  They must never start
or inspect a real browser.
"""

from __future__ import annotations

import builtins
import importlib.util
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest


_SCRIPTS_DIR = Path(__file__).parents[1] / "skills" / "easy-apply-tab-monitor" / "scripts"
_ADAPTER_PATH = _SCRIPTS_DIR / "browser_tab_adapter.py"
_SPEC = importlib.util.spec_from_file_location("browser_tab_adapter", _ADAPTER_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

BrowserAdapterError = _MODULE.BrowserAdapterError
BrowserTabAdapter = _MODULE.BrowserTabAdapter
ExternalCommandAdapter = _MODULE.ExternalCommandAdapter
create_adapter = _MODULE.create_adapter
is_listing_url = _MODULE.is_listing_url


LINKEDIN = "https://www.linkedin.com/jobs/view/123456"
INDEED = "https://in.indeed.com/viewjob?jk=abc_123"


class InMemoryBrowser:
    """A structural adapter used to verify the public protocol."""

    def list_tab_urls(self) -> tuple[str, ...]:
        return (LINKEDIN,)

    def open_listing(self, url: str) -> None:
        assert url == INDEED


def test_protocol_accepts_a_browser_independent_structural_adapter():
    adapter = InMemoryBrowser()

    assert isinstance(adapter, BrowserTabAdapter)
    assert adapter.list_tab_urls() == (LINKEDIN,)
    adapter.open_listing(INDEED)


@pytest.mark.parametrize("value", (None, 3, object()))
def test_listing_boundary_rejects_non_string_values(value: object):
    assert is_listing_url(value) is False


@pytest.mark.parametrize(
    "url",
    (
        "https://user@www.linkedin.com/jobs/view/123456",
        "https://user:password@www.linkedin.com/jobs/view/123456",
        "https://www.linkedin.com:443/jobs/view/123456",
        "https://www.linkedin.com/jobs/view/123456?access_token=secret",
        "https://in.indeed.com/viewjob?jk=abc_123&session=secret",
        "https://in.indeed.com/viewjob?jk=abc_123&jk=other",
    ),
)
def test_listing_boundary_rejects_credentials_ports_and_noncanonical_query_data(url: str):
    assert is_listing_url(url) is False


def test_external_adapter_uses_argv_json_and_no_shell():
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(command: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((command, kwargs))
        if command[-1] == "list-tabs":
            return SimpleNamespace(returncode=0, stdout=json.dumps([LINKEDIN, INDEED]), stderr="")
        return SimpleNamespace(returncode=0, stdout="ignored", stderr="")

    adapter = ExternalCommandAdapter(
        ("browser-agent-bridge", "--browser", "firefox"),
        runner=runner,
        timeout_seconds=7,
    )

    assert adapter.list_tab_urls() == (LINKEDIN, INDEED)
    adapter.open_listing(INDEED)

    assert calls[0][0] == ["browser-agent-bridge", "--browser", "firefox", "list-tabs"]
    assert calls[1][0] == ["browser-agent-bridge", "--browser", "firefox", "open-listing", INDEED]
    for _command, kwargs in calls:
        assert kwargs.get("shell", False) is False
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 7


@pytest.mark.parametrize(
    "command",
    (
        ("/usr/local/bin/browser bridge", "--agent", "codex"),
        ("/Applications/Browser Bridge.app/Contents/MacOS/bridge", "--browser", "safari"),
        (r"C:\Program Files\Browser Bridge\bridge.exe", "--browser", "edge"),
    ),
)
def test_external_command_prefix_is_portable_and_preserved_as_argv(command: tuple[str, ...]):
    observed: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        observed.append(argv)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    assert ExternalCommandAdapter(command, runner=runner).list_tab_urls() == ()
    assert observed == [[*command, "list-tabs"]]


def test_listing_url_rejects_invalid_port_and_broken_query_strings() -> None:
    """Adapter validation should reject malformed authority and query payloads."""

    assert is_listing_url("https://www.linkedin.com:bad/jobs/view/123456") is False
    assert is_listing_url("https://www.linkedin.com/jobs/view/123456?&") is False


@pytest.mark.parametrize(
    "stdout",
    (
        "not-json",
        "{}",
        '"https://example.test"',
        "null",
        '["https://example.test", 3]',
    ),
)
def test_malformed_list_output_raises_a_redacted_typed_error(stdout: str):
    secret_command = "bridge-with-secret-token"

    def runner(_argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="private-stderr")

    with pytest.raises(BrowserAdapterError) as raised:
        ExternalCommandAdapter((secret_command,), runner=runner).list_tab_urls()

    rendered = str(raised.value)
    assert secret_command not in rendered
    assert stdout not in rendered
    assert "private-stderr" not in rendered


def test_is_listing_url_accepts_supported_listing_hosts() -> None:
    assert is_listing_url(LINKEDIN) is True
    assert is_listing_url(INDEED) is True


def test_nonzero_exit_redacts_command_stdout_stderr_and_listing_url():
    secret_command = "bridge --token=super-secret"
    secret_stdout = "private browser output"
    secret_stderr = "private browser error"

    def runner(_argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=23, stdout=secret_stdout, stderr=secret_stderr)

    with pytest.raises(BrowserAdapterError) as raised:
        ExternalCommandAdapter((secret_command,), runner=runner).open_listing(LINKEDIN)

    rendered = str(raised.value)
    for secret in (secret_command, secret_stdout, secret_stderr, LINKEDIN):
        assert secret not in rendered


def test_timeout_redacts_command_output_stderr_and_listing_url():
    secret_command = "private-browser-bridge"
    secret_stdout = "private stdout"
    secret_stderr = "private stderr"

    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(
            cmd=argv,
            timeout=kwargs["timeout"],
            output=secret_stdout,
            stderr=secret_stderr,
        )

    with pytest.raises(BrowserAdapterError) as raised:
        ExternalCommandAdapter((secret_command,), runner=runner).open_listing(INDEED)

    rendered = str(raised.value)
    for secret in (secret_command, secret_stdout, secret_stderr, INDEED):
        assert secret not in rendered


@pytest.mark.parametrize(
    "url",
    (
        "https://example.test/jobs/1",
        "https://www.linkedin.com/",
        "https://www.linkedin.com/login",
        "https://www.linkedin.com/jobs/search/?keywords=python",
        "https://www.linkedin.com/jobs/view/123456/apply/",
        "https://in.indeed.com/jobs?q=python",
        "https://smartapply.indeed.com/beta/indeedapply/form?jk=abc_123",
        LINKEDIN + "#application",
    ),
)
def test_external_adapter_rejects_non_listing_and_application_urls_before_invocation(url: str):
    invoked = False

    def runner(_argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        nonlocal invoked
        invoked = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(ValueError, match="listing"):
        ExternalCommandAdapter(("fake-bridge",), runner=runner).open_listing(url)

    assert invoked is False


@pytest.mark.parametrize("url", (LINKEDIN, INDEED))
def test_external_adapter_opens_only_an_exact_supported_listing(url: str):
    observed: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        observed.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    ExternalCommandAdapter(("fake-bridge",), runner=runner).open_listing(url)

    assert observed == [["fake-bridge", "open-listing", url]]
    assert all(action not in observed[0] for action in ("click", "fill", "upload", "submit"))


def test_external_adapter_strips_safe_tracking_before_opening_the_canonical_listing():
    observed: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        observed.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    ExternalCommandAdapter(("fake-bridge",), runner=runner).open_listing(
        "https://WWW.LinkedIn.com/jobs/view/123456/?trk=public_jobs&utm_source=agent"
    )

    assert observed == [["fake-bridge", "open-listing", LINKEDIN]]


def test_external_adapter_rejects_oversize_stdout_without_echo():
    oversized = "[" + json.dumps(LINKEDIN) + "," + " " * 1_048_576 + "]"

    def runner(_argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=oversized, stderr="")

    with pytest.raises(BrowserAdapterError) as raised:
        ExternalCommandAdapter(("fake-bridge",), runner=runner).list_tab_urls()

    assert LINKEDIN not in str(raised.value)


def test_default_runner_lists_tabs_through_bounded_capture_without_shell():
    """The production (default-runner) path streams stdout boundedly and parses it."""

    import sys

    script = (
        "import json, sys; sys.stdout.write("
        "json.dumps(['https://www.linkedin.com/jobs/view/123456']))"
    )
    adapter = ExternalCommandAdapter((sys.executable, "-c", script), timeout_seconds=30)

    assert adapter.list_tab_urls() == (LINKEDIN,)


def test_default_runner_bounds_oversize_stdout_before_parse_without_echo():
    """The default path kills an over-cap bridge before JSON parsing, redacted."""

    import sys

    script = "import sys; sys.stdout.write('[' + ' ' * 1_048_577 + ']')"
    adapter = ExternalCommandAdapter((sys.executable, "-c", script), timeout_seconds=30)

    with pytest.raises(BrowserAdapterError) as raised:
        adapter.list_tab_urls()

    rendered = str(raised.value)
    assert LINKEDIN not in rendered
    assert len(rendered) < 1_048_576


def test_default_runner_rejects_non_string_completed_stdout_without_echoing_bridge_data():
    def runner(_argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=None, stderr="private-stderr")

    with pytest.raises(BrowserAdapterError, match="invalid tab data") as raised:
        ExternalCommandAdapter(("private-bridge",), runner=runner).list_tab_urls()

    assert "private-bridge" not in str(raised.value)
    assert "private-stderr" not in str(raised.value)


class _NoopTimer:
    """Timer seam for deterministic default-runner subprocess failure tests."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


class _FakeChild:
    """Minimal Popen seam with controllable stdout, wait, and return-code behavior."""

    def __init__(
        self,
        *,
        stdout: object,
        returncode: object = 0,
        wait_error: Exception | None = None,
    ) -> None:
        self.pid = 4242
        self.stdout = stdout
        self._returncode = returncode
        self._wait_error = wait_error
        self.kill_calls = 0

    @property
    def returncode(self) -> object:
        if isinstance(self._returncode, Exception):
            raise self._returncode
        return self._returncode

    def wait(self, **_kwargs: Any) -> None:
        if self._wait_error is not None:
            raise self._wait_error

    def kill(self) -> None:
        self.kill_calls += 1


class _ExplodingReader:
    def read(self, _size: int) -> str:
        raise RuntimeError("private bridge read failure")


class _EmptyReader:
    def read(self, _size: int) -> str:
        return ""


def _default_adapter_with_fake_child(
    monkeypatch: pytest.MonkeyPatch,
    child: _FakeChild,
) -> tuple[ExternalCommandAdapter, list[int | None]]:
    killed_groups: list[int | None] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(_MODULE.threading, "Timer", _NoopTimer)
    monkeypatch.setattr(
        _MODULE,
        "_kill_bridge_process_tree",
        lambda _child, *, pgid=None: killed_groups.append(pgid),
    )
    return ExternalCommandAdapter(("private-bridge",), timeout_seconds=1), killed_groups


def test_default_runner_redacts_stdout_read_and_returncode_property_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    read_adapter, read_kills = _default_adapter_with_fake_child(
        monkeypatch,
        _FakeChild(stdout=_ExplodingReader()),
    )

    with pytest.raises(BrowserAdapterError, match="command failed") as read_error:
        read_adapter.list_tab_urls()

    assert "private bridge read failure" not in str(read_error.value)
    assert read_kills == [4242]

    returncode_adapter, returncode_kills = _default_adapter_with_fake_child(
        monkeypatch,
        _FakeChild(stdout=_EmptyReader(), returncode=RuntimeError("private returncode failure")),
    )
    with pytest.raises(BrowserAdapterError, match="command failed") as returncode_error:
        returncode_adapter.list_tab_urls()

    assert "private returncode failure" not in str(returncode_error.value)
    assert returncode_kills == [4242, 4242]


def test_default_runner_tolerates_wait_failure_but_rejects_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
):
    wait_adapter, wait_kills = _default_adapter_with_fake_child(
        monkeypatch,
        _FakeChild(stdout=StringIO("[]"), wait_error=RuntimeError("private wait failure")),
    )

    assert wait_adapter.list_tab_urls() == ()
    assert wait_kills == [4242]

    nonzero_adapter, nonzero_kills = _default_adapter_with_fake_child(
        monkeypatch,
        _FakeChild(stdout=_EmptyReader(), returncode=7),
    )
    with pytest.raises(BrowserAdapterError, match="command failed"):
        nonzero_adapter.list_tab_urls()
    assert nonzero_kills == [4242, 4242]


def test_default_runner_windows_branch_uses_no_process_group(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_MODULE, "os", SimpleNamespace(name="nt"))
    adapter, killed_groups = _default_adapter_with_fake_child(
        monkeypatch,
        _FakeChild(stdout=StringIO("[]")),
    )

    assert adapter.list_tab_urls() == ()
    assert killed_groups == [None]


def test_default_runner_timer_start_os_error_is_redacted_and_kills_the_child(
    monkeypatch: pytest.MonkeyPatch,
):
    class _FailingTimer(_NoopTimer):
        def start(self) -> None:
            raise OSError("private timer failure")

    child = _FakeChild(stdout=_EmptyReader())
    adapter, killed_groups = _default_adapter_with_fake_child(monkeypatch, child)
    monkeypatch.setattr(_MODULE.threading, "Timer", _FailingTimer)

    with pytest.raises(BrowserAdapterError, match="command failed") as raised:
        adapter.list_tab_urls()

    assert "private timer failure" not in str(raised.value)
    assert killed_groups == [4242]


def test_process_tree_killer_handles_group_lookup_and_child_kill_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    class _ExplodingKillChild:
        pid = 9

        def kill(self) -> None:
            raise RuntimeError("private kill failure")

    monkeypatch.setattr(_MODULE.os, "name", "posix")
    monkeypatch.setattr(_MODULE.os, "getpgid", lambda _pid: (_ for _ in ()).throw(OSError()))
    _MODULE._kill_bridge_process_tree(_ExplodingKillChild())

    killed: list[int] = []
    monkeypatch.setattr(_MODULE.os, "getpgid", lambda _pid: 77)
    monkeypatch.setattr(_MODULE.os, "killpg", lambda pgid, _signal: killed.append(pgid))
    _MODULE._kill_bridge_process_tree(_ExplodingKillChild())

    assert killed == [77]


def test_process_tree_killer_swallows_unexpected_group_resolution_errors(monkeypatch: pytest.MonkeyPatch):
    class _BrokenPidChild:
        @property
        def pid(self) -> int:
            raise RuntimeError("private pid failure")

        def kill(self) -> None:
            raise AssertionError("child kill must not run after broken pid")

    monkeypatch.setattr(_MODULE.os, "name", "posix")
    monkeypatch.setattr(_MODULE.os, "getpgid", lambda _pid: 1)

    _MODULE._kill_bridge_process_tree(_BrokenPidChild())


def test_process_tree_killer_with_unset_pid_still_kills_direct_child(
    monkeypatch: pytest.MonkeyPatch,
):
    """A bridge child without a pid skips group lookup but still kills direct child."""

    class _NonePidChild:
        pid = None
        kill_calls = 0

        def kill(self) -> None:
            type(self).kill_calls += 1

    _NonePidChild.kill_calls = 0
    getpgid_calls: list[object] = []
    monkeypatch.setattr(_MODULE.os, "name", "posix")
    monkeypatch.setattr(_MODULE.os, "getpgid", lambda pid: getpgid_calls.append(pid))

    _MODULE._kill_bridge_process_tree(_NonePidChild())

    assert getpgid_calls == []
    assert _NonePidChild.kill_calls == 1


def test_invoke_enforces_byte_cap_on_injected_stdout_before_parse():
    """An injected runner's over-cap stdout fails closed inside _invoke."""

    def runner(_argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="x" * (1_048_576 + 1), stderr="")

    with pytest.raises(BrowserAdapterError) as raised:
        ExternalCommandAdapter(("fake-bridge",), runner=runner)._invoke("list-tabs")

    assert "xxx" not in str(raised.value)


def test_default_runner_open_listing_uses_argv_without_shell(tmp_path):
    """The default path opens an exact listing through argv, never a shell."""

    import sys

    probe = tmp_path / "open_argv.json"
    script = (
        "import json, sys; "
        f"open({str(probe)!r}, 'w').write(json.dumps(sys.argv[1:]))"
    )
    adapter = ExternalCommandAdapter((sys.executable, "-c", script), timeout_seconds=30)

    adapter.open_listing(LINKEDIN)

    assert json.loads(probe.read_text(encoding="utf-8")) == ["open-listing", LINKEDIN]


@pytest.mark.skipif(os.name == "nt", reason="process-group timeout behavior is POSIX-only")
def test_default_runner_bounds_helper_wait_without_getpgid(monkeypatch: pytest.MonkeyPatch):
    """The group kill stays bounded even when getpgid is unavailable.

    With start_new_session=True the pgid deterministically equals the child
    pid, so the timeout kill must not depend on resolving the group via
    getpgid() (which races leader exit) or on Windows-only fallbacks. A
    helper holding the stdout pipe still cannot stall the bridge past the
    timeout, and failure output stays redacted.
    """

    import sys
    import time

    def unavailable_getpgid(_pid: int) -> int:
        raise OSError("getpgid unavailable")

    monkeypatch.setattr(os, "getpgid", unavailable_getpgid, raising=False)

    script = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
    )
    adapter = ExternalCommandAdapter((sys.executable, "-c", script), timeout_seconds=2)

    started = time.monotonic()
    with pytest.raises(BrowserAdapterError) as raised:
        adapter.list_tab_urls()
    elapsed = time.monotonic() - started

    assert elapsed < 25
    assert LINKEDIN not in str(raised.value)


@pytest.mark.parametrize("failure", (ValueError("bad argv"), TypeError("bad argv"), AttributeError("bad pipe")))
def test_default_runner_construction_failure_is_redacted(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
):
    """Popen construction failures stay redacted without echoing argv."""

    secret = "super-secret-bridge-token"

    def exploding_popen(*_args: Any, **_kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(subprocess, "Popen", exploding_popen)

    adapter = ExternalCommandAdapter(("fake-bridge", secret), timeout_seconds=5)

    with pytest.raises(BrowserAdapterError) as raised:
        adapter.list_tab_urls()

    assert secret not in str(raised.value)


@pytest.mark.skipif(os.name == "nt", reason="process-group timeout behavior is POSIX-only")
def test_default_runner_bounds_fd_inheriting_helpers_on_the_wall_clock():
    """A helper holding the stdout pipe cannot stall the bridge past timeout.

    The child exits at once while a background helper inherits the captured
    stdout descriptor and sleeps. Killing only the direct child would leave
    the pipe open and the wall clock unbounded; the process-group kill
    bounds it instead. Failure output stays redacted.
    """

    import sys
    import time

    script = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
    )
    adapter = ExternalCommandAdapter((sys.executable, "-c", script), timeout_seconds=2)

    started = time.monotonic()
    with pytest.raises(BrowserAdapterError) as raised:
        adapter.list_tab_urls()
    elapsed = time.monotonic() - started

    assert elapsed < 25
    assert LINKEDIN not in str(raised.value)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    (
        ("  https://www.linkedin.com/jobs/view/123456  ", LINKEDIN),
        ("https://in.indeed.com/viewjob/?jk=abc_123", INDEED),
        ("https://in.indeed.com/viewjob//?jk=abc_123", INDEED),
        ("https://www.linkedin.com../jobs/view/123456", LINKEDIN),
    ),
)
def test_canonicalizer_parity_vectors_match_js_bridge(raw: str, canonical: str):
    """Whitespace padding and trailing slashes canonicalize like the JS bridge."""

    assert _MODULE.canonical_listing_url(raw) == canonical


@pytest.mark.parametrize(
    "raw",
    (
        "https://www.linkedin.com/jobs/view/123456//",
        "https://www.linkedin.com/jobs/view/12 3456",
    ),
)
def test_canonicalizer_parity_rejections_match_js_bridge(raw: str):
    assert is_listing_url(raw) is False


def test_factory_requires_explicit_adapter_selection_and_external_command():
    def runner(_argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    external = create_adapter("external", command=("fake-bridge",), runner=runner)
    assert isinstance(external, ExternalCommandAdapter)
    assert external.list_tab_urls() == ()

    with pytest.raises(ValueError, match="command"):
        create_adapter("external")
    with pytest.raises(ValueError, match="adapter"):
        create_adapter("automatic")


@pytest.mark.parametrize("command", ((), "bridge", b"bridge", object(), ("",), ("bridge", "")))
def test_external_adapter_rejects_ambiguous_or_empty_command_configuration(command: object):
    with pytest.raises((TypeError, ValueError), match="command"):
        ExternalCommandAdapter(command)


@pytest.mark.parametrize("timeout", (True, "5", float("nan"), float("inf"), 0, -1))
def test_external_adapter_rejects_invalid_timeout(timeout: object):
    with pytest.raises(ValueError, match="timeout"):
        ExternalCommandAdapter(("bridge",), timeout_seconds=timeout)


def test_factory_retains_explicit_macos_chrome_applescript_compatibility():
    chrome = create_adapter("chrome-applescript")

    assert isinstance(chrome, BrowserTabAdapter)
    assert chrome.__class__.__name__ == "ChromeAppleScript"
    assert callable(chrome.list_tab_urls)
    assert callable(chrome.open_listing)


def test_factory_reuses_an_already_loaded_chrome_compatibility_adapter(monkeypatch: pytest.MonkeyPatch):
    class LoadedChromeAdapter(InMemoryBrowser):
        def __init__(self, *, runner: object):
            self.runner = runner

    loaded_module = SimpleNamespace(ChromeAppleScript=LoadedChromeAdapter)
    sentinel_runner = object()
    monkeypatch.setitem(sys.modules, "chrome_tab_watcher", loaded_module)

    chrome = create_adapter("chrome-applescript", runner=sentinel_runner)

    assert isinstance(chrome, LoadedChromeAdapter)
    assert chrome.runner is sentinel_runner


def test_factory_can_import_the_optional_chrome_adapter_from_the_scripts_directory(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delitem(sys.modules, "chrome_tab_watcher", raising=False)
    monkeypatch.syspath_prepend(str(_SCRIPTS_DIR))

    chrome = create_adapter("chrome-applescript")

    assert chrome.__class__.__name__ == "ChromeAppleScript"
    assert isinstance(chrome, BrowserTabAdapter)


def test_factory_fails_closed_when_optional_chrome_adapter_cannot_be_loaded(
    monkeypatch: pytest.MonkeyPatch,
):
    real_import = builtins.__import__

    def unavailable_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "chrome_tab_watcher":
            raise ModuleNotFoundError("optional adapter unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "chrome_tab_watcher", raising=False)
    monkeypatch.setattr(builtins, "__import__", unavailable_import)
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *_args, **_kwargs: None)

    with pytest.raises(BrowserAdapterError, match="unavailable"):
        create_adapter("chrome-applescript")


def test_factory_rejects_a_command_for_chrome_compatibility_adapter():
    with pytest.raises(ValueError, match="does not accept"):
        create_adapter("chrome-applescript", command=("must-not-run",))


def test_list_tabs_rejects_oversize_array_without_echoing_urls(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_MODULE, "_MAX_TAB_URLS", 2)
    payload = [LINKEDIN, INDEED, LINKEDIN]

    def runner(_argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    with pytest.raises(BrowserAdapterError) as raised:
        ExternalCommandAdapter(("fake-bridge",), runner=runner).list_tab_urls()

    rendered = str(raised.value)
    for secret in payload:
        assert secret not in rendered


def test_list_tabs_rejects_overlong_entry_without_echoing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_MODULE, "_MAX_TAB_URL_LENGTH", 8)
    overlong = "https://example.test/" + "x" * 64

    def runner(_argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=json.dumps([overlong]), stderr="")

    with pytest.raises(BrowserAdapterError) as raised:
        ExternalCommandAdapter(("fake-bridge",), runner=runner).list_tab_urls()

    assert overlong not in str(raised.value)


def test_list_tabs_rejects_empty_entry_without_echoing():
    def runner(_argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=json.dumps([""]), stderr="")

    with pytest.raises(BrowserAdapterError, match="invalid tab data"):
        ExternalCommandAdapter(("fake-bridge",), runner=runner).list_tab_urls()


def test_list_tabs_skips_non_listing_urls_and_returns_only_canonical():
    def runner(_argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([LINKEDIN, "https://example.com/"]),
            stderr="",
        )

    assert ExternalCommandAdapter(("fake-bridge",), runner=runner).list_tab_urls() == (LINKEDIN,)


def test_list_tabs_empty_array_returns_empty_tuple():
    def runner(_argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    assert ExternalCommandAdapter(("fake-bridge",), runner=runner).list_tab_urls() == ()


def test_list_tabs_all_non_listing_returns_empty_tuple():
    def runner(_argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(["https://example.com/", "https://www.linkedin.com/login"]),
            stderr="",
        )

    assert ExternalCommandAdapter(("fake-bridge",), runner=runner).list_tab_urls() == ()
