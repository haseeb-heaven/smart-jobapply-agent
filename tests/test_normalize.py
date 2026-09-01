from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "jobapply_agent" / "src"))

from jobapply_agent.models import JobListing
from jobapply_agent.normalize import (
    canonicalize_url,
    listing_fingerprint,
    normalize_date,
    normalize_listing,
)


def test_normalize_listing_compacts_visible_fields_and_dates():
    job = JobListing(
        platform=" LinkedIn ",
        title="  Python   Backend\nDeveloper  ",
        company="  Acme\tLabs  ",
        location=" Bengaluru,   Karnataka ",
        work_mode=" Work from home ",
        description="  Maintain   APIs. ",
        posted_at="2026/08/30",
        discovered_at=datetime(2026, 8, 31, 9, 30, tzinfo=timezone.utc),
    )

    normalized = normalize_listing(job)

    assert normalized.platform == "linkedin"
    assert normalized.title == "Python Backend Developer"
    assert normalized.company == "Acme Labs"
    assert normalized.location == "Bengaluru, Karnataka"
    assert normalized.work_mode == "remote"
    assert normalized.description == "Maintain APIs."
    assert normalized.posted_at == "2026-08-30"
    assert normalized.discovered_at == "2026-08-31T09:30:00+00:00"


def test_fingerprint_is_stable_for_equivalent_listing_urls_and_text():
    first = listing_fingerprint(
        "LinkedIn",
        "https://www.linkedin.com/jobs/view/123/?utm_source=alert#details",
        " Python Backend Developer ",
        "ACME Labs",
    )
    second = listing_fingerprint(
        "linkedin",
        "https://www.linkedin.com/jobs/view/123?utm_medium=email",
        "python   backend developer",
        "acme labs",
    )

    assert first == second
    assert len(first) == 64
    assert canonicalize_url("https://example.com/job/?b=2&utm_source=x&a=1#top") == (
        "https://example.com/job?a=1&b=2"
    )


def test_normalize_date_returns_iso_8601_or_none():
    assert normalize_date("31 Aug 2026") == "2026-08-31"
    assert normalize_date("not a date") is None
