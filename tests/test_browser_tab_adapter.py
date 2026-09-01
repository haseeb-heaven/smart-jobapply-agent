"""Contract tests for browser-neutral, listing-only tab adapters.

These tests use injected subprocess runners exclusively.  They must never start
or inspect a real browser.
"""

from __future__ import annotations

import builtins
import importlib.util
import json
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
