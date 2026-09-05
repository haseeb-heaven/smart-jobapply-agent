#!/usr/bin/env python3
"""Bounded NDJSON bridge for a Node-parented, existing browser session.

The daemon is the child process. It writes one request frame to ``stderr`` and
reads exactly one matching response frame from ``stdin``. ``stdout`` is
reserved for redacted Smart Queue status and is never a bridge channel.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import sys
import threading
from typing import Final, Protocol

try:
    from browser_tab_adapter import BrowserAdapterError, canonical_listing_url
except ModuleNotFoundError:
    _SIBLING_ADAPTER_PATH = Path(__file__).with_name("browser_tab_adapter.py")
    _SIBLING_ADAPTER_SPEC = importlib.util.spec_from_file_location(
        "browser_tab_adapter", _SIBLING_ADAPTER_PATH
    )
    if _SIBLING_ADAPTER_SPEC is None or _SIBLING_ADAPTER_SPEC.loader is None:
        raise RuntimeError("shared listing URL validation is unavailable") from None
    _SIBLING_ADAPTER_MODULE = importlib.util.module_from_spec(_SIBLING_ADAPTER_SPEC)
    sys.modules[_SIBLING_ADAPTER_SPEC.name] = _SIBLING_ADAPTER_MODULE
    _SIBLING_ADAPTER_SPEC.loader.exec_module(_SIBLING_ADAPTER_MODULE)
    BrowserAdapterError = _SIBLING_ADAPTER_MODULE.BrowserAdapterError
    canonical_listing_url = _SIBLING_ADAPTER_MODULE.canonical_listing_url


_ADAPTER_KIND: Final = "browser-bridge-stdio"
_MAX_RESPONSE_BYTES: Final = 1_048_576
_MAX_TAB_URLS: Final = 512
_MAX_TAB_URL_LENGTH: Final = 8_192
_DEFAULT_RESPONSE_TIMEOUT_SECONDS: Final = 15.0
_UNAVAILABLE_MESSAGE: Final = "browser bridge is unavailable"
_INVALID_DATA_MESSAGE: Final = "browser bridge returned invalid data"


class _RequestStream(Protocol):
    def write(self, value: str) -> object: ...

    def flush(self) -> object: ...


class _ResponseStream(Protocol):
    def readline(self, size: int = -1) -> str: ...


class StdioBridgeAdapter:
    """Strict two-operation NDJSON client for the optional Node parent.

    Requests are ``{"id": "request-1", "operation": "list_tab_urls"}`` or
    ``{"id": "request-1", "operation": "open_listing", "url": URL}``.
    Responses are generic ``ok`` frames, with a bounded ``urls`` array only for
    a successful list request. Diagnostic fields are forbidden.
    """

    smart_queue_adapter: Final = _ADAPTER_KIND

    def __init__(
        self,
        *,
        response_stream: _ResponseStream | None = None,
        request_stream: _RequestStream | None = None,
        response_timeout_seconds: float | None = None,
    ) -> None:
        self._response_stream = response_stream or sys.stdin
        self._request_stream = request_stream or sys.stderr
        if not callable(getattr(self._response_stream, "readline", None)) or not callable(
            getattr(self._request_stream, "write", None)
        ) or not callable(getattr(self._request_stream, "flush", None)):
            raise TypeError("stdio bridge streams are invalid")
        if response_timeout_seconds is None:
            self._response_timeout_seconds = _DEFAULT_RESPONSE_TIMEOUT_SECONDS
        elif (
            isinstance(response_timeout_seconds, bool)
            or not isinstance(response_timeout_seconds, (int, float))
            or not math.isfinite(response_timeout_seconds)
            or response_timeout_seconds <= 0
        ):
            raise ValueError("response timeout is invalid")
        else:
            self._response_timeout_seconds = float(response_timeout_seconds)
        self._next_request_id = 1
        self._request_lock = threading.RLock()
        self._terminal = False

    @property
    def terminal(self) -> bool:
        """Whether this stdio stream has an unsafe response boundary."""

        with self._request_lock:
            return self._terminal

    def _reject_response(self, message: str) -> None:
        """Fence a response stream that must not be correlated again.

        A malformed, partial, or oversized frame can leave a later frame at an
        unknowable request boundary (notably after ``readline(size)`` returns a
        prefix).  A request/response stdio channel has no recovery delimiter,
        so fail closed rather than allowing a later request to consume it.
        """

        self._terminal = True
        raise BrowserAdapterError(message)

    def _read_response(self) -> str:
        """Read one frame with a portable deadline, without reusing timed-out stdio."""

        completed = threading.Event()
        result: dict[str, object] = {}

        def read() -> None:
            try:
                result["response"] = self._response_stream.readline(_MAX_RESPONSE_BYTES + 1)
            except Exception:
                result["failed"] = True
            finally:
                completed.set()

        try:
            # Accepted: the reader is a daemon thread, so a timed-out read
            # leaks at most one blocked daemon reader that cannot keep the
            # process alive. The stream is marked terminal on timeout and is
            # never reused, so no timeout-machinery redesign is needed.
            threading.Thread(target=read, daemon=True).start()
        except Exception:
            self._terminal = True
            raise BrowserAdapterError(_UNAVAILABLE_MESSAGE) from None
        if not completed.wait(self._response_timeout_seconds):
            # The reader may still consume a late frame. Do not issue or accept
            # another request on a stream whose response boundary is now unknown.
            self._terminal = True
            raise BrowserAdapterError("browser bridge response timed out")
        if result.get("failed") is True:
            self._terminal = True
            raise BrowserAdapterError(_UNAVAILABLE_MESSAGE)
        response = result.get("response")
        if not isinstance(response, str):
            self._reject_response(_INVALID_DATA_MESSAGE)
        return response

    def _request(
        self, operation: str, *, url: str | None = None
    ) -> tuple[dict[str, object], str]:
        with self._request_lock:
            if self._terminal:
                raise BrowserAdapterError(_UNAVAILABLE_MESSAGE)
            request_id = f"request-{self._next_request_id}"
            self._next_request_id += 1
            request: dict[str, object] = {"id": request_id, "operation": operation}
            if url is not None:
                request["url"] = url
            try:
                self._request_stream.write(json.dumps(request, separators=(",", ":"), sort_keys=True) + "\n")
                self._request_stream.flush()
            except Exception:
                # A failed write or flush may have emitted a partial request.
                # Do not let its eventual response satisfy a later request.
                self._terminal = True
                raise BrowserAdapterError(_UNAVAILABLE_MESSAGE) from None
            response = self._read_response()
            if (
                not response.endswith("\n")
                or response.endswith("\r\n")
                or len(response.encode("utf-8")) > _MAX_RESPONSE_BYTES
            ):
                self._reject_response(_INVALID_DATA_MESSAGE)
            try:
                payload = json.loads(response[:-1])
            except json.JSONDecodeError:
                self._reject_response(_INVALID_DATA_MESSAGE)
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("ok"), bool)
                or payload.get("id") != request_id
            ):
                self._reject_response(_INVALID_DATA_MESSAGE)
            return payload, request_id

    def list_tab_urls(self) -> tuple[str, ...]:
        """Return canonical supported listing URLs from one bounded response.

        Layered contract: this adapter speaks to an already-filtering host
        that only emits managed listing URLs, so any non-canonical URL in
        the response is a protocol violation and the whole batch fails
        closed. The explicit argv adapter
        (``browser_tab_adapter.ExternalCommandAdapter``) instead speaks to
        raw bridge output and skips non-listing tabs as not-managed.
        """

        payload, request_id = self._request("list_tab_urls")
        if payload.get("ok") is False:
            if payload != {"id": request_id, "ok": False, "error": "request_failed"}:
                self._reject_response(_INVALID_DATA_MESSAGE)
            raise BrowserAdapterError("browser bridge rejected the request")
        if set(payload) != {"id", "ok", "urls"} or payload.get("ok") is not True:
            self._reject_response("browser bridge returned invalid tab data")
        urls = payload.get("urls")
        if (
            not isinstance(urls, list)
            or len(urls) > _MAX_TAB_URLS
            or not all(isinstance(url, str) and 0 < len(url) <= _MAX_TAB_URL_LENGTH for url in urls)
        ):
            self._reject_response("browser bridge returned invalid tab data")
        try:
            return tuple(canonical_listing_url(url) for url in urls)
        except ValueError:
            self._reject_response("browser bridge returned invalid tab data")

    def open_listing(self, url: str) -> None:
        """Request one exact listing URL; never a page or form action."""

        try:
            canonical = canonical_listing_url(url)
        except ValueError:
            raise ValueError("refusing to open a non-listing LinkedIn/Indeed URL") from None
        payload, request_id = self._request("open_listing", url=canonical)
        if payload.get("ok") is False:
            if payload != {"id": request_id, "ok": False, "error": "request_failed"}:
                self._reject_response("browser bridge returned invalid data")
            raise BrowserAdapterError("browser bridge rejected the request")
        if set(payload) != {"id", "ok"} or payload.get("ok") is not True:
            self._reject_response("browser bridge returned invalid open result")


__all__ = ["StdioBridgeAdapter"]
