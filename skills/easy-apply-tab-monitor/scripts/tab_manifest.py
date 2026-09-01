#!/usr/bin/env python3
"""Validate a bounded, review-only LinkedIn/Indeed tab queue."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ALLOWED_ROOTS = ("linkedin.com", "indeed.com")
PLATFORMS = {"linkedin", "indeed"}
APPLY_PATHS = {"easy_apply", "apply_with_indeed", "external"}
STATUSES = {"unknown", "submitted", "not_found"}


def _allowed_host(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    return parsed.scheme == "https" and any(host == root or host.endswith("." + root) for root in ALLOWED_ROOTS)


def validate_jobs(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise ValueError("queue must be a JSON array")
    if not 1 <= len(payload) <= 5:
        raise ValueError("queue must contain between one and five jobs")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
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
        if not _allowed_host(url):
            raise ValueError("job URL must be an HTTPS LinkedIn or Indeed URL")
        host = urlsplit(url).hostname.casefold().rstrip(".") if urlsplit(url).hostname else ""
        host_platform = "linkedin" if host == "linkedin.com" or host.endswith(".linkedin.com") else "indeed"
        if platform != host_platform:
            raise ValueError("platform must match the job URL host")
        if url in seen:
            raise ValueError("job URLs must be unique")
        if raw["apply_path"] not in APPLY_PATHS:
            raise ValueError(f"apply_path must be one of {sorted(APPLY_PATHS)}")
        status = raw.get("prior_application", "unknown")
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError(f"prior_application must be one of {sorted(STATUSES)}")
        seen.add(url)
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
    observed = set(observed_urls)
    return [job["url"] for job in jobs if job["url"] not in observed]


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
            print(json.dumps({"ok": True, "job_count": len(validate_jobs(json.loads(args.input.read_text(encoding="utf-8"))))}))
        else:
            print(json.dumps({"ok": True, "missing_urls": missing(args.manifest, args.observed_url)}))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
