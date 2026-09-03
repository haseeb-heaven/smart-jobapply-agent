"""Contract tests for the bounded Codex Chrome stdio bridge."""

from __future__ import annotations

import importlib.util
import json
import threading
import time
from pathlib import Path
import sys

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
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
CodexChromeExtensionAdapter = _MODULE.CodexChromeExtensionAdapter


LINKEDIN = "https://www.linkedin.com/jobs/view/123456"
INDEED = "https://in.indeed.com/viewjob?jk=abc_123"


class ResponseStream:
    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.read_limits: list[int] = []

    def readline(self, size: int = -1) -> str:
        self.read_limits.append(size)
        return self.frames.pop(0) if self.frames else ""


class RequestStream:
    def __init__(self) -> None:
        self.frames: list[str] = []
        self.flush_count = 0

    def write(self, value: str) -> int:
        self.frames.append(value)
        return len(value)

    def flush(self) -> None:
        self.flush_count += 1


class StalledResponseStream:
    """A bridge response that remains blocked until test cleanup releases it."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.read_limits: list[int] = []

    def readline(self, size: int = -1) -> str:
        self.read_limits.append(size)
        self.started.set()
        self.release.wait(timeout=5)
        return _frame({"id": "request-1", "ok": True, "urls": []})


def _frame(payload: object) -> str:
    return json.dumps(payload, separators=(",", ":")) + "\n"


def _adapter(*responses: object) -> tuple[CodexChromeExtensionAdapter, RequestStream, ResponseStream]:
    request_stream = RequestStream()
    frames = [response if isinstance(response, str) else _frame(response) for response in responses]
    response_stream = ResponseStream(frames)
    return (
        CodexChromeExtensionAdapter(response_stream=response_stream, request_stream=request_stream),
        request_stream,
        response_stream,
    )


def _assert_redacted(exception: BaseException, *private_values: str) -> None:
    for value in private_values:
        assert value not in str(exception)


def test_adapter_is_a_bounded_listing_protocol_for_live_smart_queue() -> None:
    adapter, _requests, _responses = _adapter({"id": "request-1", "ok": True, "urls": []})

    assert adapter.smart_queue_adapter == "codex-chrome-extension-stdio"
    assert isinstance(adapter, BrowserTabAdapter)
    for prohibited_name in ("click", "fill", "upload", "submit", "close_tab", "inspect_page"):
        assert not hasattr(adapter, prohibited_name)


def test_list_and_open_use_sequence_matched_ndjson_stderr_requests() -> None:
    adapter, requests, responses = _adapter(
        {"id": "request-1", "ok": True, "urls": [LINKEDIN, INDEED]},
        {"id": "request-2", "ok": True},
    )

    assert adapter.list_tab_urls() == (LINKEDIN, INDEED)
    adapter.open_listing(LINKEDIN)

    assert requests.frames == [
        '{"id":"request-1","operation":"list_tab_urls"}\n',
        f'{{"id":"request-2","operation":"open_listing","url":"{LINKEDIN}"}}\n',
    ]
    assert requests.flush_count == 2
    assert responses.read_limits == [1_048_577, 1_048_577]


def test_stalled_response_times_out_redacted_and_terminals_the_adapter() -> None:
    requests = RequestStream()
    responses = StalledResponseStream()
    adapter = CodexChromeExtensionAdapter(
        response_stream=responses,
        request_stream=requests,
        response_timeout_seconds=0.025,
    )

    started = time.monotonic()
    try:
        with pytest.raises(BrowserAdapterError) as raised:
            adapter.list_tab_urls()
        elapsed = time.monotonic() - started

        assert responses.started.is_set()
        assert elapsed < 0.5
        assert adapter.terminal is True
        _assert_redacted(raised.value, "request-1", "private browser state")

        with pytest.raises(BrowserAdapterError) as later:
            adapter.list_tab_urls()
        assert time.monotonic() - started < 0.6
        _assert_redacted(later.value, "request-1", "private browser state")
        assert requests.frames == [
            '{"id":"request-1","operation":"list_tab_urls"}\n'
        ]
        assert requests.flush_count == 1
    finally:
        responses.release.set()


def test_list_tab_urls_canonicalizes_the_bounded_url_response() -> None:
    adapter, _requests, _responses = _adapter(
        {
            "id": "request-1",
            "ok": True,
            "urls": [
                "https://WWW.LinkedIn.com/jobs/view/123456/?trk=public_jobs&utm_source=host",
                "https://IN.INDEED.com/viewjob/?jk=abc_123&utm_source=host",
            ],
        }
    )

    assert adapter.list_tab_urls() == (LINKEDIN, INDEED)


@pytest.mark.parametrize(
    "response",
    (
        "not-json\n",
        '{"id":"request-1","ok":true,"urls":[]}\r\n',
        '{"id":"request-1","ok":true,"urls":[]}',
        {"id": "request-2", "ok": True, "urls": []},
        {"id": "request-1", "ok": True, "urls": [], "diagnostic": "private browser state"},
        {"id": None, "ok": False, "error": "private browser state"},
        {"id": "request-1", "ok": True, "urls": ["https://mail.example.test/inbox?private=value"]},
    ),
)
def test_list_rejects_malformed_or_untrusted_responses_without_leaking_them(response: object) -> None:
    private_value = "private browser state"
    adapter, _requests, _responses = _adapter(response)

    with pytest.raises(BrowserAdapterError) as raised:
        adapter.list_tab_urls()

    _assert_redacted(raised.value, private_value, "https://mail.example.test/inbox?private=value")


@pytest.mark.parametrize("operation", ("list", "open"))
def test_generic_rejection_response_requires_the_matching_request_id(operation: str) -> None:
    adapter, _requests, _responses = _adapter(
        {"id": "request-1", "ok": False, "error": "request_failed"}
    )

    with pytest.raises(BrowserAdapterError) as raised:
        if operation == "list":
            adapter.list_tab_urls()
        else:
            adapter.open_listing(LINKEDIN)

    assert str(raised.value) == "browser bridge rejected the request"
    _assert_redacted(raised.value, LINKEDIN)


@pytest.mark.parametrize("response_id", (None, "request-2"))
def test_generic_rejection_response_rejects_a_missing_or_mismatched_request_id(
    response_id: object,
) -> None:
    adapter, _requests, _responses = _adapter(
        {"id": response_id, "ok": False, "error": "request_failed"}
    )

    with pytest.raises(BrowserAdapterError) as raised:
        adapter.list_tab_urls()

    assert str(raised.value) == "browser bridge returned invalid data"


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
def test_open_listing_rejects_noncanonical_or_application_urls_before_stream_io(url: str) -> None:
    adapter, requests, responses = _adapter({"id": "request-1", "ok": True})

    with pytest.raises(ValueError) as raised:
        adapter.open_listing(url)

    assert requests.frames == []
    assert responses.read_limits == []
    _assert_redacted(raised.value, url)


def test_open_listing_rejects_non_generic_response_without_leaking_listing_url() -> None:
    adapter, _requests, _responses = _adapter({"id": "request-1", "ok": True, "url": LINKEDIN})

    with pytest.raises(BrowserAdapterError) as raised:
        adapter.open_listing(LINKEDIN)

    _assert_redacted(raised.value, LINKEDIN)
