#!/usr/bin/env python3
"""Reference loopback adapter for an already-connected Codex Chrome host.

It exposes only the Smart Queue's URL-level operations. The Node host attests
that it is connected to an existing Chrome session; this Python client cannot
prove that claim and Smart Queue does not rely on it as a security boundary.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import math
from pathlib import Path
import sys
from typing import Callable, Final, Protocol
from urllib.parse import urlsplit

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


_ADAPTER_KIND: Final = "codex-chrome-extension"
_MAX_RESPONSE_BYTES: Final = 1_048_576
_MAX_TAB_URLS: Final = 512
_MAX_TAB_URL_LENGTH: Final = 8_192
_MAX_TIMEOUT_SECONDS: Final = 30.0
_MIN_TIMEOUT_SECONDS: Final = 0.1


class _HTTPConnection(Protocol):
    """Minimal injectable connection surface; no browser operations exist here."""

    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Issue one loopback request."""

    def getresponse(self) -> object:
        """Return an HTTP response-like object."""

    def close(self) -> None:
        """Close the loopback connection."""


def _validate_endpoint(value: object) -> tuple[str, int]:
    """Accept exactly a credential-free IPv4 loopback origin."""

    if not isinstance(value, str) or not value:
        raise ValueError("endpoint must be a loopback HTTP URL")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("endpoint must be a loopback HTTP URL") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65_535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("endpoint must be a loopback HTTP URL")
    return "127.0.0.1", port


def _validate_token(value: object) -> str:
    """Require a nonempty in-memory opaque bearer token."""

    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("token must be an opaque in-memory bearer token")
    if not all(character.isascii() and (character.isalnum() or character in "-_") for character in value):
        raise ValueError("token must be an opaque in-memory bearer token")
    return value


def _validate_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("timeout must be a finite number")
    timeout = float(value)
    if not _MIN_TIMEOUT_SECONDS <= timeout <= _MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout must be between 0.1 and 30 seconds")
    return timeout


class CodexChromeExtensionAdapter:
    """Authenticated two-operation client for the optional local reference host."""

    smart_queue_adapter: Final = _ADAPTER_KIND

    def __init__(
        self,
        endpoint: str,
        token: str,
        *,
        connection_factory: Callable[..., _HTTPConnection] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._host, self._port = _validate_endpoint(endpoint)
        self._token = _validate_token(token)
        self._timeout_seconds = _validate_timeout(timeout_seconds)
        self._connection_factory = connection_factory or self._default_connection

    @staticmethod
    def _default_connection(host: str, port: int, *, timeout: float) -> _HTTPConnection:
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def _request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection: _HTTPConnection | None = None
        try:
            connection = self._connection_factory(self._host, self._port, timeout=self._timeout_seconds)
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            status = getattr(response, "status", None)
            try:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)  # type: ignore[union-attr]
            except TypeError:
                # Minimal test doubles need not implement the optional read
                # size argument.  The production HTTPResponse path above is
                # still size-limited before allocating a response body.
                raw = response.read()  # type: ignore[union-attr]
        except Exception:
            raise BrowserAdapterError("local browser bridge is unavailable") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
        if not isinstance(status, int) or len(raw) > _MAX_RESPONSE_BYTES:
            raise BrowserAdapterError("local browser bridge returned invalid data")
        return status, raw

    def list_tab_urls(self) -> tuple[str, ...]:
        """Return only canonical supported listing URLs from the local host.

        The loopback host is untrusted input.  Reject the complete snapshot if
        any entry is not a supported LinkedIn/Indeed listing, rather than
        leaking unrelated browser URLs into queue reconciliation.
        """

        status, raw = self._request("GET", "/v1/tab-urls")
        if status != 200:
            raise BrowserAdapterError("local browser bridge rejected the request")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BrowserAdapterError("local browser bridge returned invalid data") from None
        if not isinstance(payload, dict) or set(payload) != {"urls"}:
            raise BrowserAdapterError("local browser bridge returned invalid tab data")
        urls = payload["urls"]
        if (
            not isinstance(urls, list)
            or len(urls) > _MAX_TAB_URLS
            or not all(isinstance(url, str) and 0 < len(url) <= _MAX_TAB_URL_LENGTH for url in urls)
        ):
            raise BrowserAdapterError("local browser bridge returned invalid tab data")
        try:
            return tuple(canonical_listing_url(url) for url in urls)
        except ValueError:
            raise BrowserAdapterError("local browser bridge returned invalid tab data") from None

    def open_listing(self, url: str) -> None:
        """Request one exact listing URL; never a form or page action."""

        try:
            canonical = canonical_listing_url(url)
        except ValueError:
            raise ValueError("refusing to open a non-listing LinkedIn/Indeed URL") from None
        body = json.dumps({"url": canonical}, separators=(",", ":")).encode("utf-8")
        status, _raw = self._request("POST", "/v1/open-listing", body)
        if status != 204:
            raise BrowserAdapterError("local browser bridge returned invalid open result")


__all__ = ["CodexChromeExtensionAdapter"]
