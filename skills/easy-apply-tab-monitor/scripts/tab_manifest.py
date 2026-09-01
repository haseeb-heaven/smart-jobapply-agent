#!/usr/bin/env python3
"""Validate a bounded, review-only LinkedIn/Indeed tab queue."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


PLATFORMS = {"linkedin", "indeed"}
APPLY_PATHS = {"easy_apply", "apply_with_indeed", "external"}
STATUSES = {"unknown", "submitted", "not_found"}
_LINKEDIN_LISTING_PATH = re.compile(r"^/jobs/view/(?P<job_id>[A-Za-z0-9_-]+)/?$")
_INDEED_JOB_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_TRACKING_QUERY_KEYS = frozenset(
    {"campaign", "from", "mcid", "ref", "source", "trackingid", "trk"}
)


def _is_safe_tracking_key(key: str) -> bool:
    normalized = key.casefold()
    return normalized in _SAFE_TRACKING_QUERY_KEYS or normalized.startswith("utm_")


def _canonical_listing(url: str) -> tuple[str, str]:
    parsed = urlsplit(url.strip())
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("job URL must be a canonical credential-free listing URL") from None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise ValueError("job URL must be a canonical credential-free listing URL")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if parsed.query and not query:
        raise ValueError("job URL must be a canonical credential-free listing URL")
    if any(not key or (key.casefold() != "jk" and not _is_safe_tracking_key(key)) for key, _value in query):
        raise ValueError("job URL must be a canonical credential-free listing URL")
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        match = _LINKEDIN_LISTING_PATH.fullmatch(parsed.path)
        if match is None or any(key.casefold() == "jk" for key, _value in query):
            raise ValueError("job URL must be a canonical credential-free listing URL")
        canonical = urlunsplit(("https", host, f"/jobs/view/{match.group('job_id')}", "", ""))
        return "linkedin", canonical
    if host == "indeed.com" or host.endswith(".indeed.com"):
        if parsed.path.rstrip("/") != "/viewjob":
            raise ValueError("job URL must be a canonical credential-free listing URL")
        job_ids = [value for key, value in query if key.casefold() == "jk"]
        if len(job_ids) != 1 or _INDEED_JOB_ID.fullmatch(job_ids[0]) is None:
            raise ValueError("job URL must be a canonical credential-free listing URL")
        canonical = urlunsplit(("https", host, "/viewjob", urlencode({"jk": job_ids[0]}), ""))
        return "indeed", canonical
    raise ValueError("job URL must be a canonical HTTPS LinkedIn or Indeed listing URL")


def _listing_identity(canonical_url: str) -> tuple[str, str]:
    parsed = urlsplit(canonical_url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        match = _LINKEDIN_LISTING_PATH.fullmatch(parsed.path)
        if match is not None:
            return "linkedin", match.group("job_id")
    if host == "indeed.com" or host.endswith(".indeed.com"):
        query = parse_qsl(parsed.query, keep_blank_values=True)
        job_ids = [value for key, value in query if key.casefold() == "jk"]
        if len(job_ids) == 1:
            return "indeed", job_ids[0]
    raise ValueError("job URL must be a canonical listing URL")


def validate_jobs(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("queue must be a JSON array")
    if not 1 <= len(payload) <= 5:
        raise ValueError("queue must contain between one and five jobs")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in payload:
        if not isinstance(raw, dict):
            raise ValueError("each job must be an object")
        required = ("title", "company", "platform", "url", "apply_path")
        if any(not isinstance(raw.get(key), str) or not raw[key].strip() for key in required):
            raise ValueError("each job needs non-empty title, company, platform, url, and apply_path")
        platform = raw["platform"].strip().casefold()
        if platform not in PLATFORMS:
            raise ValueError(f"platform must be one of {sorted(PLATFORMS)}")
        url = raw["url"].strip()
        host_platform, url = _canonical_listing(url)
        if platform != host_platform:
            raise ValueError("platform must match the job URL host")
        identity = _listing_identity(url)
        if identity in seen:
            raise ValueError("job URLs must be unique")
        if raw["apply_path"] not in APPLY_PATHS:
            raise ValueError(f"apply_path must be one of {sorted(APPLY_PATHS)}")
        status = raw.get("prior_application", "unknown")
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError(f"prior_application must be one of {sorted(STATUSES)}")
        seen.add(identity)
        result.append(
            {
                "title": raw["title"].strip(),
                "company": raw["company"].strip(),
                "platform": platform,
                "url": url,
                "apply_path": raw["apply_path"],
                "recommendation": str(raw.get("recommendation", "")).strip(),
                "prior_application": status,
            }
        )
    return result


def create(input_path: Path, output_path: Path) -> None:
    jobs = validate_jobs(json.loads(input_path.read_text(encoding="utf-8")))
    output_path.write_text(json.dumps({"schemaVersion": 1, "jobs": jobs}, indent=2) + "\n", encoding="utf-8")


def missing(manifest_path: Path, observed_urls: list[str]) -> list[str]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    jobs = validate_jobs(payload.get("jobs") if isinstance(payload, dict) else None)
    observed: set[tuple[str, str]] = set()
    for observed_url in observed_urls:
        if not isinstance(observed_url, str):
            raise ValueError("observed URLs must be strings")
        try:
            _platform, canonical = _canonical_listing(observed_url)
        except ValueError:
            continue
        observed.add(_listing_identity(canonical))
    return [job["url"] for job in jobs if _listing_identity(job["url"]) not in observed]


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and compare a five-tab job queue.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--input", type=Path, required=True)
    create_parser.add_argument("--output", type=Path, required=True)
    missing_parser = subparsers.add_parser("missing")
    missing_parser.add_argument("--manifest", type=Path, required=True)
    missing_parser.add_argument("--observed-url", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.command == "create":
            create(args.input, args.output)
            print(
                json.dumps(
                    {"ok": True, "job_count": len(validate_jobs(json.loads(args.input.read_text(encoding="utf-8"))))}
                )
            )
        else:
            print(json.dumps({"ok": True, "missing_urls": missing(args.manifest, args.observed_url)}))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
