"""Pure normalization and deterministic deduplication helpers."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from hashlib import sha256
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import JobListing


_WHITESPACE = re.compile(r"\s+")
_TRACKING_QUERY_KEYS = {"ref", "source", "trk", "trackingid", "mcid", "campaign"}
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%d %B %Y")
_WORK_MODE_ALIASES = {
    "remote": "remote",
    "work from home": "remote",
    "wfh": "remote",
    "hybrid": "hybrid",
    "on site": "on-site",
    "onsite": "on-site",
    "on-site": "on-site",
}


def normalize_whitespace(value: str | None) -> str:
    return _WHITESPACE.sub(" ", value or "").strip()


def normalize_title(value: str | None) -> str:
    return normalize_whitespace(value)


def normalize_company(value: str | None) -> str:
    return normalize_whitespace(value)


def normalize_location(value: str | None) -> str:
    value = normalize_whitespace(value)
    return re.sub(r"\s*,\s*", ", ", value)


def normalize_work_mode(value: str | None) -> str:
    cleaned = normalize_whitespace(value).casefold()
    return _WORK_MODE_ALIASES.get(cleaned, cleaned)


def normalize_date(value: Any) -> str | None:
    """Return a stable ISO date/timestamp where the visible value is parseable."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = normalize_whitespace(str(value))
    iso_candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso_candidate).isoformat()
    except ValueError:
        pass
    for format_ in _DATE_FORMATS:
        try:
            return datetime.strptime(text, format_).date().isoformat()
        except ValueError:
            continue
    return None


def canonicalize_url(url: str | None) -> str:
    """Remove fragments and tracking parameters while retaining job identifiers."""

    raw = normalize_whitespace(url)
    if not raw:
        return ""
    parsed = urlsplit(raw)
    scheme = parsed.scheme.casefold()
    netloc = parsed.netloc.casefold()
    path = re.sub(r"/+", "/", parsed.path).rstrip("/") or "/"
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in _TRACKING_QUERY_KEYS and not key.casefold().startswith("utm_")
    ]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


def listing_fingerprint(platform: str, canonical_url: str, title: str, company: str) -> str:
    """SHA-256 identity key for one platform listing, stable across superficial edits."""

    parts = (
        normalize_whitespace(platform).casefold(),
        canonicalize_url(canonical_url),
        normalize_title(title).casefold(),
        normalize_company(company).casefold(),
    )
    return sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def normalize_listing(job: JobListing) -> JobListing:
    """Return a copy with normalized visible-page fields and canonical URL."""

    return replace(
        job,
        platform=normalize_whitespace(job.platform).casefold(),
        title=normalize_title(job.title),
        company=normalize_company(job.company),
        description=normalize_whitespace(job.description),
        url=canonicalize_url(job.url),
        location=normalize_location(job.location),
        work_mode=normalize_work_mode(job.work_mode),
        posted_at=normalize_date(job.posted_at),
        discovered_at=normalize_date(job.discovered_at),
    )

