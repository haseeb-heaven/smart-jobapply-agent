"""Contract tests for the bounded Codex Chrome extension bridge.

These tests define the adapter seam used by ``SmartQueueCoordinator``.  They
use an injected HTTP connection factory only: no test starts a server, creates
a browser session, or reaches a job board.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest


_SCRIPTS_DIR = Path(__file__).parents[1] / "skills" / "easy-apply-tab-monitor" / "scripts"
_BROWSER_ADAPTER_PATH = _SCRIPTS_DIR / "browser_tab_adapter.py"
_BROWSER_ADAPTER_SPEC = importlib.util.spec_from_file_location("browser_tab_adapter", _BROWSER_ADAPTER_PATH)
assert _BROWSER_ADAPTER_SPEC and _BROWSER_ADAPTER_SPEC.loader
_BROWSER_ADAPTER_MODULE = importlib.util.module_from_spec(_BROWSER_ADAPTER_SPEC)
sys.modules[_BROWSER_ADAPTER_SPEC.name] = _BROWSER_ADAPTER_MODULE
_BROWSER_ADAPTER_SPEC.loader.exec_module(_BROWSER_ADAPTER_MODULE)
BrowserAdapterError = _BROWSER_ADAPTER_MODULE.BrowserAdapterError

_COORDINATOR_PATH = _SCRIPTS_DIR / "smart_queue_coordinator.py"
_COORDINATOR_SPEC = importlib.util.spec_from_file_location("smart_queue_coordinator", _COORDINATOR_PATH)
assert _COORDINATOR_SPEC and _COORDINATOR_SPEC.loader
_COORDINATOR_MODULE = importlib.util.module_from_spec(_COORDINATOR_SPEC)
sys.modules[_COORDINATOR_SPEC.name] = _COORDINATOR_MODULE
_COORDINATOR_SPEC.loader.exec_module(_COORDINATOR_MODULE)
BrowserTabAdapter = _COORDINATOR_MODULE.BrowserTabAdapter

_ADAPTER_PATH = _SCRIPTS_DIR / "codex_chrome_extension_adapter.py"
_SPEC = importlib.util.spec_from_file_location("codex_chrome_extension_adapter", _ADAPTER_PATH)
assert _SPEC and _SPEC.loader, "implement codex_chrome_extension_adapter.py before enabling the live bridge"
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
CodexChromeExtensionAdapter = _MODULE.CodexChromeExtensionAdapter


LINKEDIN = "https://www.linkedin.com/jobs/view/123456"
INDEED = "https://in.indeed.com/viewjob?jk=abc_123"
_ENDPOINT = "http://127.0.0.1:8765"
_TOKEN = "private-test-bridge-token"


class _Response:
    def __init__(self, status: int, payload: object = b"") -> None:
        self.status = status
        self._payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload


class _Connection:
    def __init__(self, response: _Response) -> None:
        self._response = response
        self.requests: list[tuple[str, str, bytes | None, dict[str, str]]] = []
        self.closed = False

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> None:
        self.requests.append((method, path, body, dict(headers or {})))

    def getresponse(self) -> _Response:
        return self._response

    def close(self) -> None:
        self.closed = True


def _adapter(
    response: _Response = _Response(200, {"urls": [LINKEDIN, INDEED]}),
) -> tuple[CodexChromeExtensionAdapter, _Connection]:
    connection = _Connection(response)

    def connection_factory(host: str, port: int, *, timeout: float) -> _Connection:
        assert (host, port, timeout) == ("127.0.0.1", 8765, 4.0)
        return connection

    return (
        CodexChromeExtensionAdapter(
            _ENDPOINT,
            token=_TOKEN,
            connection_factory=connection_factory,
            timeout_seconds=4,
        ),
        connection,
    )


def _assert_redacted(exception: BaseException, *private_values: str) -> None:
    rendered = str(exception)
    for value in private_values:
        assert value not in rendered


def test_adapter_is_a_bounded_listing_protocol_for_live_smart_queue() -> None:
    adapter, _connection = _adapter()

    assert adapter.smart_queue_adapter == "codex-chrome-extension"
    assert isinstance(adapter, BrowserTabAdapter)
    assert callable(adapter.list_tab_urls)
    assert callable(adapter.open_listing)
    for prohibited_name in ("click", "fill", "upload", "submit", "close_tab", "inspect_page"):
        assert not hasattr(adapter, prohibited_name)


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://127.0.0.1:8765",
        "http://localhost:8765",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:8765/extra",
        "http://127.0.0.1:8765?token=leaked",
        "http://127.0.0.1:8765#fragment",
        "http://user:password@127.0.0.1:8765",
        "http://192.168.1.8:8765",
        "http://[::1]:8765",
        "http://169.254.169.254:8765",
    ),
)
def test_constructor_rejects_every_non_exact_ipv4_loopback_origin(endpoint: str) -> None:
    with pytest.raises(ValueError) as raised:
        CodexChromeExtensionAdapter(endpoint, token=_TOKEN)

    _assert_redacted(raised.value, _TOKEN, endpoint)


@pytest.mark.parametrize("token", (None, "", " ", 7, object()))
def test_constructor_requires_a_nonempty_bearer_token(token: object) -> None:
    with pytest.raises((TypeError, ValueError)) as raised:
        CodexChromeExtensionAdapter(_ENDPOINT, token=token)

    _assert_redacted(raised.value, _TOKEN)


def test_list_tab_urls_uses_only_the_exact_authenticated_get_route() -> None:
    adapter, connection = _adapter()

    assert adapter.list_tab_urls() == (LINKEDIN, INDEED)

    assert connection.requests == [
        (
            "GET",
            "/v1/tab-urls",
            None,
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {_TOKEN}",
            },
        )
    ]
    assert connection.closed is True


def test_list_tab_urls_returns_only_canonical_listing_urls() -> None:
    adapter, _connection = _adapter(
        _Response(
            200,
            {
                "urls": [
                    "https://WWW.LinkedIn.com/jobs/view/123456/?trk=public_jobs&utm_source=host",
                    "https://IN.INDEED.com/viewjob/?jk=abc_123&utm_source=host",
                ]
            },
        )
    )

    assert adapter.list_tab_urls() == (LINKEDIN, INDEED)


@pytest.mark.parametrize(
    "untrusted_url",
    (
        "https://mail.example.test/inbox?private=value",
        "https://www.linkedin.com/jobs/search/?keywords=python",
        "https://www.linkedin.com/jobs/view/123456/apply/",
        "https://in.indeed.com/viewjob?jk=abc_123&session=private",
    ),
)
def test_list_tab_urls_rejects_private_or_noncanonical_urls_from_untrusted_snapshot(
    untrusted_url: str,
) -> None:
    adapter, _connection = _adapter(_Response(200, {"urls": [LINKEDIN, untrusted_url]}))

    with pytest.raises(BrowserAdapterError) as raised:
        adapter.list_tab_urls()

    _assert_redacted(raised.value, _TOKEN, LINKEDIN, untrusted_url, _ENDPOINT)


def test_list_tab_urls_rejects_malformed_or_non_string_json_without_leaking_response() -> None:
    private_payload = {"urls": [LINKEDIN, 7], "diagnostic": "private browser state"}
    adapter, _connection = _adapter(_Response(200, private_payload))

    with pytest.raises(BrowserAdapterError) as raised:
        adapter.list_tab_urls()

    _assert_redacted(raised.value, _TOKEN, LINKEDIN, "private browser state", _ENDPOINT)


@pytest.mark.parametrize("payload", (b"not-json", {"url": LINKEDIN}, [], "not-an-object"))
def test_list_tab_urls_rejects_nonconforming_payload_shapes(payload: object) -> None:
    adapter, _connection = _adapter(_Response(200, payload))

    with pytest.raises(BrowserAdapterError) as raised:
        adapter.list_tab_urls()

    _assert_redacted(raised.value, _TOKEN, LINKEDIN, _ENDPOINT)


def test_network_failures_are_typed_and_redacted() -> None:
    def connection_factory(_host: str, _port: int, *, timeout: float) -> Any:
        assert timeout == 15
        raise OSError(f"cannot reach {_ENDPOINT} with {_TOKEN}")

    adapter = CodexChromeExtensionAdapter(_ENDPOINT, token=_TOKEN, connection_factory=connection_factory)

    with pytest.raises(BrowserAdapterError) as raised:
        adapter.list_tab_urls()

    _assert_redacted(raised.value, _TOKEN, _ENDPOINT)


@pytest.mark.parametrize(
    "url",
    (
        "https://example.test/jobs/123",
        "https://www.linkedin.com/jobs/search/?keywords=python",
        "https://www.linkedin.com/jobs/view/123456/apply/",
        "https://in.indeed.com/jobs?q=python",
        "https://smartapply.indeed.com/beta/indeedapply/form?jk=abc_123",
        "https://in.indeed.com/viewjob?jk=abc_123&session=private",
    ),
)
def test_open_listing_rejects_noncanonical_or_application_urls_before_network_io(url: str) -> None:
    called = False

    def connection_factory(_host: str, _port: int, *, timeout: float) -> Any:
        nonlocal called
        called = True
        raise AssertionError("invalid listing URL must not create a bridge connection")

    adapter = CodexChromeExtensionAdapter(_ENDPOINT, token=_TOKEN, connection_factory=connection_factory)

    with pytest.raises(ValueError) as raised:
        adapter.open_listing(url)

    assert called is False
    _assert_redacted(raised.value, _TOKEN, url, _ENDPOINT)


def test_open_listing_canonicalizes_then_uses_only_the_exact_authenticated_post_route() -> None:
    adapter, connection = _adapter(_Response(204))

    adapter.open_listing("https://WWW.LinkedIn.com/jobs/view/123456/?trk=public_jobs&utm_source=host")

    assert connection.requests == [
        (
            "POST",
            "/v1/open-listing",
            json.dumps({"url": LINKEDIN}, separators=(",", ":")).encode("utf-8"),
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {_TOKEN}",
                "Content-Type": "application/json",
            },
        )
    ]
    assert connection.closed is True


@pytest.mark.parametrize("status", (200, 201, 400, 401, 403, 404, 500))
def test_open_listing_requires_no_content_success_and_redacts_listing_and_token(status: int) -> None:
    adapter, _connection = _adapter(_Response(status, {"detail": f"private {_TOKEN} {LINKEDIN}"}))

    with pytest.raises(BrowserAdapterError) as raised:
        adapter.open_listing(LINKEDIN)

    _assert_redacted(raised.value, _TOKEN, LINKEDIN, _ENDPOINT)
