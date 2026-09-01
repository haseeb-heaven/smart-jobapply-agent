#!/usr/bin/env python3
"""Safely validate Browser Use credentials from the user's mixed credential file.

The source file is intentionally not treated as a dotenv file: it contains
human-readable labels and credentials from several unrelated services.  This
tool reads only the ``Browser Use:`` section, keeps the selected key in memory,
and reports only a redacted key fingerprint and HTTP outcome.  It never writes
or prints a credential.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import signal
import ssl
import threading
from typing import Iterable
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi


DEFAULT_CREDENTIAL_FILE = Path("/Users/haseeb-mir/Documents/Code/OAuthENV.txt")
DEFAULT_BASE_URL = "https://api.browser-use.com/api/v3"
_KEY_PATTERN = re.compile(r"^bu_[A-Za-z0-9_-]+$")
_TRUSTED_BROWSER_USE_HOST = "api.browser-use.com"


@dataclass(frozen=True)
class BrowserUseCredentials:
    """One in-memory Browser Use endpoint/key pair."""

    base_url: str
    api_key: str

    @property
    def fingerprint(self) -> str:
        return sha256(self.api_key.encode("utf-8")).hexdigest()[:12]


def is_board_cookie_domain(domain: object, board_root: str) -> bool:
    """Accept only the board root or one of its DNS subdomains."""

    normalized_domain = str(domain).casefold().strip().lstrip(".").rstrip(".")
    normalized_root = board_root.casefold().strip().rstrip(".")
    return normalized_domain == normalized_root or normalized_domain.endswith("." + normalized_root)


def _is_trusted_browser_use_base_url(value: str) -> bool:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.casefold() == "https"
        and parsed.hostname is not None
        and parsed.hostname.casefold().rstrip(".") == _TRUSTED_BROWSER_USE_HOST
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
    )


def _section_lines(lines: Iterable[str], heading: str = "Browser Use:") -> list[str]:
    capture = False
    result: list[str] = []
    for raw in lines:
        line = raw.strip()
        if line == heading:
            capture = True
            continue
        if capture and line.endswith(":") and not line.startswith(("http://", "https://")):
            break
        if capture and line:
            result.append(line)
    return result


def _read_credential_file(path: Path, *, timeout_seconds: float) -> str:
    """Read cloud-backed credentials without allowing an indefinite filesystem stall."""

    if timeout_seconds <= 0 or threading.current_thread() is not threading.main_thread():
        return path.read_text(encoding="utf-8")

    def _timeout_handler(_signum: int, _frame: object) -> None:
        raise TimeoutError("Credential file read timed out")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
        return path.read_text(encoding="utf-8")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer != (0.0, 0.0):
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def load_browser_use_credentials(
    path: Path,
    *,
    key_index: int = 0,
    file_read_timeout_seconds: float = 10.0,
) -> BrowserUseCredentials:
    """Extract one Browser Use key from its labelled section without logging it."""

    values = _section_lines(
        _read_credential_file(path, timeout_seconds=file_read_timeout_seconds).splitlines()
    )
    base_urls = [value.rstrip("/") for value in values if value.startswith(("https://", "http://"))]
    if any(not _is_trusted_browser_use_base_url(base_url) for base_url in base_urls):
        raise ValueError("Browser Use base URL must use the trusted HTTPS Browser Use host")
    keys = [value for value in values if _KEY_PATTERN.fullmatch(value)]
    if not keys:
        raise ValueError("No Browser Use API key was found in the Browser Use section")
    if key_index < 0 or key_index >= len(keys):
        raise ValueError(f"Browser Use key index {key_index} is unavailable; found {len(keys)} key(s)")
    base_url = base_urls[0] if base_urls else DEFAULT_BASE_URL
    if base_url.endswith("/sessions"):
        base_url = base_url.removesuffix("/sessions")
    return BrowserUseCredentials(base_url, keys[key_index])


def verify_credentials(credentials: BrowserUseCredentials, *, timeout_seconds: float = 15.0) -> dict[str, object]:
    """Perform the cheapest authenticated Browser Use request and redact output."""

    url = f"{credentials.base_url.rstrip('/')}/sessions?page_size=1"
    request = Request(url, headers={"X-Browser-Use-API-Key": credentials.api_key, "Accept": "application/json"})
    try:
        tls_context = ssl.create_default_context(cafile=certifi.where())
        with urlopen(request, timeout=timeout_seconds, context=tls_context) as response:
            # Parse enough to validate JSON but intentionally discard session data.
            json.loads(response.read().decode("utf-8"))
            return {"ok": True, "http_status": response.status, "base_url": credentials.base_url, "key_fingerprint": credentials.fingerprint}
    except HTTPError as error:
        return {"ok": False, "http_status": error.code, "base_url": credentials.base_url, "key_fingerprint": credentials.fingerprint}
    except URLError as error:
        return {"ok": False, "error": type(error.reason).__name__, "base_url": credentials.base_url, "key_fingerprint": credentials.fingerprint}


def profile_readiness(payload: object) -> dict[str, object]:
    """Summarize whether any persistent profile can support the job boards.

    Profile names, IDs, users, and cookie values are intentionally discarded.
    Only aggregate count and the presence of the two needed cookie domains are
    returned.
    """

    entries: object = payload.get("items", payload.get("profiles", ())) if isinstance(payload, dict) else ()
    profiles = entries if isinstance(entries, list) else ()
    domains: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        cookie_domains = profile.get("cookieDomains", ())
        if isinstance(cookie_domains, list):
            domains.update(str(domain).casefold() for domain in cookie_domains)
    return {
        "profile_count": len(profiles),
        "linkedin_login_ready": any(is_board_cookie_domain(domain, "linkedin.com") for domain in domains),
        "indeed_login_ready": any(is_board_cookie_domain(domain, "indeed.com") for domain in domains),
    }


def verify_profile_readiness(credentials: BrowserUseCredentials, *, timeout_seconds: float = 15.0) -> dict[str, object]:
    """Read aggregate Browser Use profile readiness without revealing profiles."""

    url = f"{credentials.base_url.rstrip('/')}/profiles?page_size=100"
    request = Request(url, headers={"X-Browser-Use-API-Key": credentials.api_key, "Accept": "application/json"})
    tls_context = ssl.create_default_context(cafile=certifi.where())
    try:
        with urlopen(request, timeout=timeout_seconds, context=tls_context) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return {"ok": True, "http_status": response.status, **profile_readiness(payload)}
    except HTTPError as error:
        return {"ok": False, "http_status": error.code}
    except URLError as error:
        return {"ok": False, "error": type(error.reason).__name__}


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely validate Browser Use connectivity without revealing credentials.")
    parser.add_argument("--credentials-file", type=Path, default=DEFAULT_CREDENTIAL_FILE)
    parser.add_argument("--key-index", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--file-read-timeout",
        type=float,
        default=10.0,
        help="Maximum seconds to wait for the cloud-backed credential file.",
    )
    parser.add_argument("--profile-readiness", action="store_true", help="Check only aggregate LinkedIn/Indeed login readiness.")
    args = parser.parse_args()
    try:
        credentials = load_browser_use_credentials(
            args.credentials_file,
            key_index=args.key_index,
            file_read_timeout_seconds=args.file_read_timeout,
        )
        result = verify_credentials(credentials, timeout_seconds=args.timeout)
        if result["ok"] and args.profile_readiness:
            result["profiles"] = verify_profile_readiness(credentials, timeout_seconds=args.timeout)
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
