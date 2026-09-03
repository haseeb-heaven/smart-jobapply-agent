"""Safe search URL construction and visible-page payload adapters.

This module deliberately contains no HTTP client, browser automation, cookie
handling, login flow, CAPTCHA logic, or application action.  A caller may pass
visible listing data through an adapter after it has been lawfully obtained;
this package will only normalize and score that supplied data.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence
from urllib.parse import parse_qsl, SplitResult, urlencode, urlsplit, urlunsplit

from .listing_extraction import listing_from_validated_extraction
from .matcher import is_non_mid_level_title
from .models import JobListing


SUPPORTED_PLATFORMS = frozenset({"linkedin", "indeed"})
_PLATFORM_HOSTS = {
    "linkedin": frozenset({"linkedin.com"}),
    "indeed": frozenset({"indeed.com"}),
}
_HIGH_FIT_QUERY = re.compile(r"\b(?:python|fastapi|api)\b", re.IGNORECASE)
_LINKEDIN_LISTING_PATH = re.compile(r"^/jobs/view/(?P<job_id>[A-Za-z0-9_-]+)/?$")
_INDEED_JOB_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_TRACKING_QUERY_KEYS = frozenset(
    {"campaign", "from", "mcid", "ref", "source", "trackingid", "trk"}
)


@dataclass(frozen=True, slots=True)
class SearchProfile:
    """One transparent, platform-specific query for the review queue."""

    platform: str
    keywords: str
    location: str = ""

    def __post_init__(self) -> None:
        platform = self.platform.casefold().strip()
        keywords = " ".join(self.keywords.split())
        location = " ".join(self.location.split())
        if platform not in SUPPORTED_PLATFORMS:
            raise ValueError(f"Unsupported search platform: {self.platform!r}")
        if not keywords or not _HIGH_FIT_QUERY.search(keywords):
            raise ValueError("Search profiles must target Python, FastAPI, or API integration work")
        if is_non_mid_level_title(keywords):
            raise ValueError("Search profiles may not target excluded senior, junior, entry, or explicit-level roles")
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "keywords", keywords)
        object.__setattr__(self, "location", location)

    @property
    def search_url(self) -> str:
        return build_search_url(self)


class VisiblePageAdapter(Protocol):
    """Explicit injection boundary for already-visible listing payloads.

    Implementations must return data that was supplied to them.  They must not
    fetch a website, use a browser session, read cookies, log in, or bypass a
    CAPTCHA.  The scheduler does not accept any other data source.
    """

    def read_visible_listings(self, search_url: str) -> Iterable[Mapping[str, Any]]:
        """Return payloads rendered on the already-visible search page."""


class MappingVisiblePageAdapter:
    """Test/local adapter backed by a caller-owned mapping of visible payloads."""

    def __init__(self, payloads_by_search_url: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
        self._payloads_by_search_url = {
            str(url): tuple(dict(payload) for payload in payloads)
            for url, payloads in payloads_by_search_url.items()
        }

    def read_visible_listings(self, search_url: str) -> Iterable[Mapping[str, Any]]:
        return self._payloads_by_search_url.get(search_url, ())


def build_linkedin_search_url(keywords: str, location: str = "") -> str:
    """Build a readable LinkedIn jobs search URL; it does not open the URL."""

    query: dict[str, str] = {"keywords": " ".join(keywords.split())}
    if location.strip():
        query["location"] = " ".join(location.split())
    # LinkedIn's experience-level value for Associate (mid-level) roles.
    query["f_E"] = "3"
    return "https://www.linkedin.com/jobs/search/?" + urlencode(query)


def build_indeed_search_url(keywords: str, location: str = "") -> str:
    """Build a readable Indeed jobs search URL; it does not open the URL."""

    query: dict[str, str] = {"q": " ".join(keywords.split())}
    if location.strip():
        query["l"] = " ".join(location.split())
    return "https://www.indeed.com/jobs?" + urlencode(query)


def build_search_url(profile: SearchProfile) -> str:
    if profile.platform == "linkedin":
        return build_linkedin_search_url(profile.keywords, profile.location)
    if profile.platform == "indeed":
        return build_indeed_search_url(profile.keywords, profile.location)
    raise ValueError(f"Unsupported search platform: {profile.platform!r}")


def build_search_urls(profiles: Iterable[SearchProfile]) -> tuple[str, ...]:
    """Return unique URLs in declared order without contacting either platform."""

    return tuple(dict.fromkeys(build_search_url(profile) for profile in profiles))


def listing_from_visible_payload(payload: Mapping[str, Any], *, platform: str) -> JobListing:
    """Convert only visible, caller-supplied fields to a listing.

    Unknown payload keys, including any cookie/session metadata, are ignored.
    """

    extraction: object | None = payload.get("extraction")
    if extraction is None and {"schema_version", "source_url", "requirements"} <= set(payload):
        extraction = payload
    if extraction is not None:
        if not isinstance(extraction, Mapping):
            raise ValueError("Visible listing extraction must be an object")
        listing = listing_from_validated_extraction(extraction)
        if listing.platform != platform.casefold().strip():
            raise ValueError("Validated listing extraction platform does not match the visible source")
        return listing

    title = str(payload.get("title", "")).strip()
    url = str(payload.get("url", payload.get("job_url", ""))).strip()
    if not title or not url:
        raise ValueError("Visible listing payloads require non-empty title and url fields")
    _validate_listing_url(url, platform)
    return JobListing(
        title=title,
        url=url,
        company=str(payload.get("company", "")),
        description=str(payload.get("description", "")),
        location=str(payload.get("location", "")),
        work_mode=str(payload.get("work_mode", "")),
        employment_type=str(payload.get("employment_type", "")),
        posted_at=payload.get("posted_at"),
        discovered_at=payload.get("discovered_at"),
        source_job_id=str(payload["source_job_id"]) if payload.get("source_job_id") is not None else None,
        platform=platform,
    )


def _is_safe_tracking_key(key: str) -> bool:
    normalized = key.casefold()
    return normalized in _SAFE_TRACKING_QUERY_KEYS or normalized.startswith("utm_")


def canonical_listing_url(url: str, platform: str | None = None) -> str:
    """Validate and canonicalize one credential-free board listing identity.

    Only the public listing routes are accepted. Query data is closed to the
    Indeed ``jk`` identity and harmless tracking keys; credentials, fragments,
    application/search/login routes, and all other query keys fail closed.
    """

    if not isinstance(url, str) or not url.strip():
        raise ValueError("Visible listing URLs must be non-empty strings")
    if platform is not None and not isinstance(platform, str):
        raise ValueError(f"Unsupported listing platform: {platform!r}")
    normalized_platform = None if platform is None else platform.casefold().strip()
    if normalized_platform is not None and normalized_platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported listing platform: {platform!r}")
    parsed: SplitResult = urlsplit(url.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Visible listing URL has an invalid authority") from exc
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise ValueError("Visible listing URLs must be credential-free HTTPS URLs without fragments")

    detected_platform: str | None = None
    for candidate, roots in _PLATFORM_HOSTS.items():
        if any(hostname == root or hostname.endswith("." + root) for root in roots):
            detected_platform = candidate
            break
    if detected_platform is None:
        if normalized_platform is not None:
            raise ValueError(f"Visible listing URL host is not an allowed {normalized_platform} host")
        raise ValueError("Visible listing URL host is not an allowed LinkedIn or Indeed host")

    normalized_platform = detected_platform if normalized_platform is None else normalized_platform
    if detected_platform != normalized_platform:
        raise ValueError(f"Visible listing URL host is not an allowed {normalized_platform} host")

    query = parse_qsl(parsed.query, keep_blank_values=True)
    if parsed.query and not query:
        raise ValueError("Visible listing URL query is invalid")
    if any(not key or (key.casefold() != "jk" and not _is_safe_tracking_key(key)) for key, _value in query):
        raise ValueError("Visible listing URL contains an unsupported query key")

    if normalized_platform == "linkedin":
        match = _LINKEDIN_LISTING_PATH.fullmatch(parsed.path)
        if match is None or any(key.casefold() == "jk" for key, _value in query):
            raise ValueError("Visible listing URL must use the canonical LinkedIn listing route")
        canonical_path = f"/jobs/view/{match.group('job_id')}"
        canonical_query: list[tuple[str, str]] = []
    else:
        if parsed.path.rstrip("/") != "/viewjob":
            raise ValueError("Visible listing URL must use the canonical Indeed listing route")
        job_ids = [value for key, value in query if key.casefold() == "jk"]
        if len(job_ids) != 1 or _INDEED_JOB_ID.fullmatch(job_ids[0]) is None:
            raise ValueError("Visible Indeed listing URL must contain one canonical jk identity")
        canonical_path = "/viewjob"
        canonical_query = [("jk", job_ids[0])]

    return urlunsplit(("https", hostname, canonical_path, urlencode(canonical_query), ""))


def _validate_listing_url(url: str, platform: str) -> None:
    """Accept only HTTPS listing URLs on the platform that supplied them.

    Visible payloads are an input boundary, so accepting arbitrary URLs would
    let an accidental or malicious payload turn the review queue into a
    phishing/link-tracking surface.  Subdomains are allowed for the boards'
    regional hosts (for example ``uk.indeed.com``), but the platform must
    still agree with the declared source.
    """

    canonical_listing_url(url, platform)


def _build_search_profile(entry: Mapping[str, str]) -> SearchProfile:
    """Construct one validated profile, fail-closed when keywords are absent."""

    if "keywords" not in entry:
        raise ValueError("Search profiles must declare keywords")
    return SearchProfile(**entry)


def load_search_profiles(path: str | Path | None = None) -> tuple[SearchProfile, ...]:
    """Load this project's small, reviewable YAML profile shape without PyYAML."""

    profile_path = Path(path) if path else Path(__file__).parents[2] / "config" / "search_profiles.yaml"
    current: dict[str, str] | None = None
    profiles: list[SearchProfile] = []
    if not profile_path.exists():
        return ()
    for raw_line in profile_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line in {"profiles:"} or line.startswith("schema_version:") or line.startswith("minimum_profile_fit_score:"):
            continue
        if line.startswith("- "):
            if current:
                profiles.append(_build_search_profile(current))
            current = {}
            line = line[2:].strip()
        if current is not None and ":" in line:
            key, value = line.split(":", 1)
            value = value.strip().strip("\"'")
            if key.strip() in {"platform", "keywords", "location"}:
                current[key.strip()] = value
    if current:
        profiles.append(_build_search_profile(current))
    return tuple(profiles)
