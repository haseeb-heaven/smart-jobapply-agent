"""Safe search-profile parsing and visible-listing boundary regressions.

``load_search_profiles()`` is the live parser feeding ``discover.py`` main. It
reads a deliberately small YAML subset: flat ``key: value`` lines under ``-``
profile entries, ``#`` comments, and the ignored ``schema_version`` /
``minimum_profile_fit_score`` headers. Only ``platform``, ``keywords``, and
``location`` are honored; every other key is ignored.

``listing_from_visible_payload()`` is the caller-supplied data boundary: it
must reject non-mapping extractions, empty titles/URLs, platform mismatches,
and hostile URLs while preserving documented coercion of ``source_job_id``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobapply_agent.listing_extraction import ListingExtractionValidationError
from jobapply_agent.models import JobListing, JobRequirement
from jobapply_agent.sources import (
    SearchProfile,
    canonical_listing_url,
    listing_from_visible_payload,
    load_search_profiles,
)


def _write(tmp_path: Path, text: str, name: str = "profiles.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- load_search_profiles ---------------------------------------------------


def test_load_search_profiles_parses_quoted_keywords_and_ignores_comments(tmp_path: Path):
    path = _write(
        tmp_path,
        "schema_version: 1\n"
        "minimum_profile_fit_score: 85\n"
        "profiles:\n"
        '  - platform: linkedin\n    keywords: "Python Backend Developer"  # seniority gate\n'
        "  - platform: indeed\n    keywords: 'FastAPI Developer'\n"
        "  - platform: LINKEDIN\n    keywords: API Integration Developer\n    location: ' Hyderabad, India '\n",
    )

    profiles = load_search_profiles(path)

    assert [p.platform for p in profiles] == ["linkedin", "indeed", "linkedin"]
    assert [p.keywords for p in profiles] == [
        "Python Backend Developer",
        "FastAPI Developer",
        "API Integration Developer",
    ]
    assert profiles[0].location == ""
    assert profiles[2].location == "Hyderabad, India"
    assert profiles[0].search_url.startswith("https://www.linkedin.com/jobs/search/?")
    assert profiles[1].search_url.startswith("https://www.indeed.com/jobs?q=")


def test_load_search_profiles_returns_the_shipped_default_profiles():
    profiles = load_search_profiles()

    assert len(profiles) >= 2
    assert all(isinstance(profile, SearchProfile) for profile in profiles)
    assert {profile.platform for profile in profiles} == {"linkedin", "indeed"}
    assert ("linkedin", "Python Backend Developer") in {
        (profile.platform, profile.keywords) for profile in profiles
    }


def test_load_search_profiles_ignores_schema_and_minimum_profile_fit_score_lines(tmp_path: Path):
    path = _write(
        tmp_path,
        "minimum_profile_fit_score: 97\n"
        "schema_version: 2\n"
        "profiles:\n"
        "  - platform: linkedin\n    keywords: python\n",
    )

    profiles = load_search_profiles(path)

    assert len(profiles) == 1
    assert profiles[0].keywords == "python"


def test_load_search_profiles_ignores_unknown_keys_inside_a_profile(tmp_path: Path):
    path = _write(
        tmp_path,
        "profiles:\n"
        "  - platform: linkedin\n    keywords: python\n"
        "    cookies: secret-session\n    session: metadata\n    arbitrary: value\n",
    )

    profiles = load_search_profiles(path)

    assert len(profiles) == 1
    assert profiles[0].platform == "linkedin"
    assert profiles[0].keywords == "python"


@pytest.mark.parametrize("keywords", ("Java Developer", "123", ""))
def test_load_search_profiles_rejects_keywords_outside_the_high_fit_target(tmp_path: Path, keywords: str):
    path = _write(tmp_path, f"profiles:\n  - platform: linkedin\n    keywords: {keywords}\n")

    with pytest.raises(ValueError, match="Python, FastAPI, or API"):
        load_search_profiles(path)


def test_load_search_profiles_rejects_nested_keyword_lists(tmp_path: Path):
    path = _write(
        tmp_path,
        "profiles:\n"
        "  - platform: linkedin\n    keywords:\n"
        "      - python\n      - api\n",
    )

    with pytest.raises(ValueError, match="Python, FastAPI, or API"):
        load_search_profiles(path)


def test_load_search_profiles_rejects_profiles_that_declare_platform_without_keywords(tmp_path: Path):
    path = _write(
        tmp_path,
        "profiles:\n"
        "  - platform: linkedin\n    keywords: python\n"
        "  - platform: indeed\n",
    )

    with pytest.raises(ValueError, match="must declare keywords"):
        load_search_profiles(path)


def test_load_search_profiles_rejects_profiles_that_omit_platform(tmp_path: Path):
    path = _write(tmp_path, "profiles:\n  - keywords: python\n")

    with pytest.raises(ValueError, match="must declare platform"):
        load_search_profiles(path)


def test_load_search_profiles_rejects_unsupported_platforms(tmp_path: Path):
    path = _write(tmp_path, "profiles:\n  - platform: monster\n    keywords: python\n")

    with pytest.raises(ValueError, match="Unsupported search platform"):
        load_search_profiles(path)


@pytest.mark.parametrize("platform", (None, 7, "candidate-secret-platform"))
def test_search_and_visible_listing_platform_boundaries_reject_invalid_values_without_echoing_them(
    platform: object,
):
    factories = (
        lambda: SearchProfile(platform=platform, keywords="python"),  # type: ignore[arg-type]
        lambda: listing_from_visible_payload(
            {"title": "Python Developer", "url": "https://www.linkedin.com/jobs/view/1"},
            platform=platform,  # type: ignore[arg-type]
        ),
    )

    for factory in factories:
        with pytest.raises(ValueError, match="Unsupported .* platform") as error:
            factory()
        assert "candidate-secret-platform" not in str(error.value)


@pytest.mark.parametrize("platform", (7, "candidate-secret-platform"))
def test_canonical_listing_url_rejects_invalid_declared_platforms_without_echoing_them(platform: object):
    with pytest.raises(ValueError, match="Unsupported listing platform") as error:
        canonical_listing_url("https://www.linkedin.com/jobs/view/1", platform=platform)  # type: ignore[arg-type]

    assert "candidate-secret-platform" not in str(error.value)


@pytest.mark.parametrize("name", ("empty.yaml", "missing.yaml"))
def test_load_search_profiles_tolerates_empty_or_missing_files(tmp_path: Path, name: str):
    if name != "missing.yaml":
        _write(tmp_path, "", name)

    assert load_search_profiles(tmp_path / name) == ()


def test_load_search_profiles_ignores_non_mapping_scalar_content(tmp_path: Path):
    path = _write(tmp_path, "just a scalar line\n- an incomplete list item\njunk: 1\n")

    assert load_search_profiles(path) == ()


# --- listing_from_visible_payload -------------------------------------------


def _valid_extraction() -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_url": "https://www.linkedin.com/jobs/view/123456",
        "source_job_id": "123456",
        "observed_at": "2026-09-01T09:30:00+00:00",
        "title": "Python Backend Developer",
        "company": "Example Systems",
        "location": "Hyderabad, India",
        "work_mode": "hybrid",
        "employment_type": "full_time",
        "requirements": [
            {
                "id": "req-responsibility",
                "text": "Maintain and extend FastAPI APIs",
                "kind": "responsibility",
                "importance": "mandatory",
                "minimum_years": None,
                "source_evidence": "Maintain and extend FastAPI APIs.",
                "evaluation": {"value": "met", "candidate_evidence_ids": ["ev-requirement-fact"]},
            }
        ],
    }


def test_visible_payload_rejects_non_mapping_extraction_objects():
    with pytest.raises(ValueError, match="must be an object"):
        listing_from_visible_payload({"extraction": ["not", "a", "mapping"]}, platform="linkedin")


def test_visible_payload_rejects_malformed_extraction_objects():
    with pytest.raises(ListingExtractionValidationError, match="required"):
        listing_from_visible_payload({"extraction": {"title": "only-a-title"}}, platform="linkedin")


def test_visible_payload_accepts_validated_extraction_and_checks_platform():
    listing = listing_from_visible_payload({"extraction": _valid_extraction()}, platform="linkedin")

    assert isinstance(listing, JobListing)
    assert listing.platform == "linkedin"
    assert listing.url == "https://www.linkedin.com/jobs/view/123456"
    assert listing.source_job_id == "123456"
    # LLM candidate evaluations never reach the matcher model: only validated
    # requirement facts are carried over, with the evidence text as description.
    assert listing.requirements == (
        JobRequirement(
            requirement_id="req-responsibility",
            text="Maintain and extend FastAPI APIs",
            kind="responsibility",
            importance="mandatory",
            source_evidence="Maintain and extend FastAPI APIs.",
        ),
    )
    assert listing.description == "Maintain and extend FastAPI APIs."


@pytest.mark.parametrize(
    "payload",
    (
        {"title": " ", "url": "https://www.linkedin.com/jobs/view/1"},
        {"url": "https://www.linkedin.com/jobs/view/1"},
        {"title": "Title", "url": "  "},
        {"title": "Title"},
        {"title": "", "url": ""},
    ),
)
def test_visible_payload_requires_non_empty_title_and_url(payload: dict[str, object]):
    with pytest.raises(ValueError, match="non-empty title and url"):
        listing_from_visible_payload(payload, platform="linkedin")


def test_visible_payload_rejects_unknown_evaluations_that_carry_evidence_ids():
    payload = dict(_valid_extraction())
    payload["requirements"] = [dict(payload["requirements"][0])]
    payload["requirements"][0]["evaluation"] = {
        "value": "unknown",
        "candidate_evidence_ids": ["unexpected-evidence"],
    }

    with pytest.raises(ListingExtractionValidationError, match="empty for missing or unknown"):
        listing_from_visible_payload(payload, platform="linkedin")


def test_visible_payload_reads_extraction_self_schema_payload():
    payload = dict(_valid_extraction())

    listing = listing_from_visible_payload(payload, platform="linkedin")

    assert listing.source_job_id == "123456"
    assert listing.url == "https://www.linkedin.com/jobs/view/123456"


def test_visible_payload_rejects_extraction_platform_mismatch():
    payload = {"extraction": _valid_extraction()}

    with pytest.raises(ValueError, match="does not match the visible source"):
        listing_from_visible_payload(payload, platform="indeed")


def test_visible_payload_coerces_source_job_id_and_uses_job_url_fallback():
    listing = listing_from_visible_payload(
        {
            "title": "FastAPI Engineer",
            "job_url": "https://in.indeed.com/viewjob?jk=abc_123",
            "source_job_id": 4567,
        },
        platform="indeed",
    )

    assert listing.source_job_id == "4567"
    assert listing.url == "https://in.indeed.com/viewjob?jk=abc_123"
    assert listing.platform == "indeed"
    assert listing.title == "FastAPI Engineer"


def test_visible_payload_keeps_missing_source_job_id_as_none():
    listing = listing_from_visible_payload(
        {"title": "Title", "url": "https://www.linkedin.com/jobs/view/1", "source_job_id": None},
        platform="linkedin",
    )

    assert listing.source_job_id is None


def test_visible_payload_maps_supported_basic_fields_and_ignores_unknown_ones():
    listing = listing_from_visible_payload(
        {
            "title": "Python Backend Developer",
            "url": "https://www.linkedin.com/jobs/view/7",
            "company": "Example Co",
            "description": "Maintain FastAPI APIs.",
            "location": "Bengaluru",
            "work_mode": "remote",
            "employment_type": "full_time",
            "posted_at": "2026-09-01",
            "discovered_at": "2026-09-01T09:00:00+00:00",
            "cookies": "secret-session",
        },
        platform="linkedin",
    )

    assert listing.company == "Example Co"
    assert listing.description == "Maintain FastAPI APIs."
    assert listing.location == "Bengaluru"
    assert listing.work_mode == "remote"
    assert listing.employment_type == "full_time"
    assert listing.posted_at == "2026-09-01"
    assert listing.discovered_at == "2026-09-01T09:00:00+00:00"
    # Cookie/session metadata must never leak into the listing object.
    assert "secret-session" not in str(listing)


# --- canonical listing URL validation edges ----------------------------------


@pytest.mark.parametrize(
    ("platform", "url"),
    (
        ("linkedin", "ftp://www.linkedin.com/jobs/view/1"),
        ("linkedin", "https://www.linkedin.com:8443/jobs/view/1"),
        ("linkedin", "https://linkedin.com.evil.com/jobs/view/1"),
        ("linkedin", "https://evil.example.com/jobs/view/1"),
        ("linkedin", "https://www.linkedin.com/jobs/view/1?jk=2"),
        ("linkedin", "https://www.linkedin.com/jobs/view/1#application"),
        ("linkedin", "https://www.linkedin.com/jobs/view/1?access_token=secret"),
        ("indeed", "https://in.indeed.com/viewjob"),
        ("indeed", "https://in.indeed.com/viewjob?jk=abc&jk=def"),
        ("indeed", "https://in.indeed.com/viewjob?jk=ab/d"),
        ("indeed", "https://www.indeed.com/other?jk=abc"),
        ("indeed", "https://in.indeed.com/jobs?q=python"),
    ),
)
def test_visible_payload_rejects_hostile_or_noncanonical_listing_urls(platform: str, url: str):
    with pytest.raises(ValueError):
        listing_from_visible_payload({"title": "Title", "url": url}, platform=platform)


@pytest.mark.parametrize(
    ("platform", "url", "expected"),
    (
        ("linkedin", "https://uk.linkedin.com/jobs/view/123", "https://uk.linkedin.com/jobs/view/123"),
        ("linkedin", "https://www.linkedin.com/jobs/view/123/", "https://www.linkedin.com/jobs/view/123"),
        (
            "linkedin",
            "https://www.linkedin.com/jobs/view/123?utm_source=agent&trk=topcard-title",
            "https://www.linkedin.com/jobs/view/123",
        ),
        ("indeed", "https://uk.indeed.com/viewjob?jk=ABC_123-", "https://uk.indeed.com/viewjob?jk=ABC_123-"),
        (
            "indeed",
            "https://in.indeed.com/viewjob?jk=abc&trackingid=t1&from=shared",
            "https://in.indeed.com/viewjob?jk=abc",
        ),
        (
            "indeed",
            "https://in.indeed.com/viewjob?jk=abc",
            "https://in.indeed.com/viewjob?jk=abc",
        ),
    ),
)
def test_canonical_listing_url_accepts_board_routes_and_strips_tracking(
    platform: str, url: str, expected: str
):
    assert canonical_listing_url(url, platform) == expected


@pytest.mark.parametrize(
    ("platform", "url"),
    (
        ("linkedin", "https://www.indeed.com/viewjob?jk=abc"),
        ("indeed", "https://www.linkedin.com/jobs/view/123"),
    ),
)
def test_canonical_listing_url_rejects_platform_host_mismatch(platform: str, url: str):
    with pytest.raises(ValueError, match="host is not an allowed"):
        canonical_listing_url(url, platform)


def test_canonical_listing_url_rejects_blank_and_non_string_inputs():
    with pytest.raises(ValueError, match="non-empty strings"):
        canonical_listing_url("")
    with pytest.raises(ValueError, match="non-empty strings"):
        canonical_listing_url("   ")
    with pytest.raises(ValueError, match="non-empty strings"):
        canonical_listing_url(None)  # type: ignore[arg-type]


def test_canonical_listing_url_rejects_unsupported_declared_platforms():
    with pytest.raises(ValueError, match="Unsupported listing platform"):
        canonical_listing_url("https://www.linkedin.com/jobs/view/1", platform="monster")
