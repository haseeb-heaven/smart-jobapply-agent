#!/usr/bin/env python3
"""Run local discovery against a pre-exported visible-page JSON payload.

This command intentionally has no URL fetching, browser, login, CAPTCHA, or
application-submission capability. Supplying no payload produces an auditable
zero-listing run, which is safe for a scheduler heartbeat.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if (SCRIPT_DIR / "src" / "job_profession").exists():
    PROJECT_ROOT = SCRIPT_DIR
elif (SCRIPT_DIR.parent / "src" / "job_profession").exists():
    PROJECT_ROOT = SCRIPT_DIR.parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from job_profession.models import CandidateProfile  # noqa: E402
from job_profession.scheduler import current_profile_recommendations, run_discovery  # noqa: E402
from job_profession.sources import MappingVisiblePageAdapter, load_search_profiles  # noqa: E402


DEFAULT_CANDIDATE_PROFILE_PATH = PROJECT_ROOT / "private" / "candidate_profile.yaml"
_APPROVED_SKILL_SECTIONS = frozenset({"professional", "personal_open_source", "learning_or_exposure"})
_APPROVED_ROLE_SECTIONS = frozenset({"include", "exclude_title_terms"})
_APPROVED_HARD_EXCLUSION_SECTIONS = frozenset({"mandatory_requirements"})
_CURRENT_RECOMMENDATION_QUEUE_COLUMNS = (
    "profile_revision",
    "fingerprint",
    "platform",
    "score",
    "title",
    "company",
    "location",
    "work_mode",
    "url",
    "search_url",
    "discovered_at",
    "why_recommended",
    "gaps_before_applying",
    "human_action_required",
    "application_actions",
)


def _parse_inline_list(value: str, *, path: Path, line_number: int) -> list[str]:
    """Parse the small list form used by the approved profile's skill groups."""

    value = value.strip()
    if not value.startswith("[") or not value.endswith("]"):
        raise ValueError(f"Expected a bracketed list in approved profile {path} line {line_number}")
    return [item.strip().strip("\"'") for item in value[1:-1].split(",") if item.strip()]


def _approved_profile_mapping(path: Path) -> dict[str, Any]:
    """Extract only role, exclusion, and classified skill evidence from private YAML.

    This intentionally does not deserialize the complete private profile.  In
    particular, source status, location, compensation, contacts, and autofill
    policy are never placed in the mapping passed to ``CandidateProfile``.
    """

    if not path.exists():
        raise ValueError(f"Approved candidate profile is missing: {path}")
    roles: dict[str, list[str]] = {key: [] for key in _APPROVED_ROLE_SECTIONS}
    hard_exclusions: dict[str, list[str]] = {key: [] for key in _APPROVED_HARD_EXCLUSION_SECTIONS}
    skills: dict[str, list[str]] = {key: [] for key in _APPROVED_SKILL_SECTIONS}
    active_top_level: str | None = None
    active_role_section: str | None = None
    active_hard_exclusion_section: str | None = None
    active_skill_section: str | None = None

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = re.split(r"\s+#", raw_line, maxsplit=1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == 0:
            active_top_level = stripped[:-1] if stripped.endswith(":") else None
            active_role_section = None
            active_hard_exclusion_section = None
            active_skill_section = None
            continue

        if active_top_level == "hard_exclusions":
            if indent == 2 and ":" in stripped:
                key, value = (part.strip() for part in stripped.split(":", 1))
                active_hard_exclusion_section = (
                    key if key in _APPROVED_HARD_EXCLUSION_SECTIONS else None
                )
                if active_hard_exclusion_section and value:
                    hard_exclusions[active_hard_exclusion_section].extend(
                        _parse_inline_list(value, path=path, line_number=line_number)
                    )
            elif active_hard_exclusion_section and stripped.startswith("- "):
                hard_exclusions[active_hard_exclusion_section].append(stripped[2:].strip().strip("\"'"))
            continue

        if active_top_level == "roles":
            if indent == 2 and ":" in stripped:
                key, value = (part.strip() for part in stripped.split(":", 1))
                active_role_section = key if key in _APPROVED_ROLE_SECTIONS else None
                if active_role_section and value:
                    roles[active_role_section].extend(_parse_inline_list(value, path=path, line_number=line_number))
            elif active_role_section and stripped.startswith("- "):
                roles[active_role_section].append(stripped[2:].strip().strip("\"'"))
            continue

        if active_top_level != "skills":
            continue
        if indent == 2 and ":" in stripped:
            key, value = (part.strip() for part in stripped.split(":", 1))
            active_skill_section = key if key in _APPROVED_SKILL_SECTIONS else None
            if active_skill_section and value:
                skills[active_skill_section].extend(_parse_inline_list(value, path=path, line_number=line_number))
        elif active_skill_section and stripped.startswith("- "):
            skills[active_skill_section].append(stripped[2:].strip().strip("\"'"))
        elif active_skill_section and ":" in stripped:
            _group, value = (part.strip() for part in stripped.split(":", 1))
            if value:
                skills[active_skill_section].extend(_parse_inline_list(value, path=path, line_number=line_number))

    return {"roles": roles, "hard_exclusions": hard_exclusions, "skills": skills}


def approved_candidate_profile(profile_path: Path | None = None) -> CandidateProfile:
    """Load the current evidence-only profile without importing autofill data."""

    return CandidateProfile.from_mapping(_approved_profile_mapping(profile_path or DEFAULT_CANDIDATE_PROFILE_PATH))


def _load_visible_payloads(path: Path | None) -> Mapping[str, Sequence[Mapping[str, Any]]]:
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("pages"), dict):
        raw = raw["pages"]
    if not isinstance(raw, dict):
        raise ValueError("Visible payload JSON must be an object mapping search URLs to listing arrays")
    payloads: dict[str, Sequence[Mapping[str, Any]]] = {}
    for url, listings in raw.items():
        if not isinstance(url, str) or not isinstance(listings, list) or not all(isinstance(item, dict) for item in listings):
            raise ValueError("Every visible payload entry must map a URL to an array of listing objects")
        payloads[url] = listings
    return payloads


def _safe_csv_cell(value: Any) -> str | int:
    """Render review data without enabling spreadsheet-formula execution."""

    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, (list, tuple)):
        text = "; ".join(str(item) for item in value)
    elif value is None:
        text = ""
    else:
        text = str(value)
    return "'" + text if text[:1] in {"=", "+", "-", "@"} else text


def export_current_profile_recommendation_queue(
    profile: CandidateProfile, recommendation_export_path: Path, queue_path: Path
) -> int:
    """Write a CSV review queue for only the active sanitized profile revision.

    Raw JSONL remains the immutable audit history.  This command does not
    discover listings, browse a board, or perform an application action.
    """

    if queue_path.suffix.casefold() != ".csv":
        raise ValueError("Current-profile recommendation queues must use a .csv path")
    rows = current_profile_recommendations(profile, recommendation_export_path)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile("w", newline="", encoding="utf-8", dir=queue_path.parent, delete=False) as stream:
            temporary_path = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=_CURRENT_RECOMMENDATION_QUEUE_COLUMNS, extrasaction="raise")
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        "profile_revision": _safe_csv_cell(row["profile_revision"]),
                        "fingerprint": _safe_csv_cell(row["fingerprint"]),
                        "platform": _safe_csv_cell(row["platform"]),
                        "score": _safe_csv_cell(row["score"]),
                        "title": _safe_csv_cell(row.get("title")),
                        "company": _safe_csv_cell(row.get("company")),
                        "location": _safe_csv_cell(row.get("location")),
                        "work_mode": _safe_csv_cell(row.get("work_mode")),
                        "url": _safe_csv_cell(row["url"]),
                        "search_url": _safe_csv_cell(row.get("search_url")),
                        "discovered_at": _safe_csv_cell(row.get("discovered_at")),
                        "why_recommended": _safe_csv_cell(row.get("reasons", ())),
                        "gaps_before_applying": _safe_csv_cell(row.get("gaps", ())),
                        "human_action_required": _safe_csv_cell(row.get("human_action_required")),
                        "application_actions": _safe_csv_cell(row["application_actions"]),
                    }
                )
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(queue_path)
    except Exception:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
        raise
    return len(rows)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export only 85+ profile-fit jobs from injected visible-page JSON.")
    parser.add_argument("--visible-payloads", type=Path, help="Offline JSON mapping of visible search URLs to listing payloads.")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "data", help="Local directory for state, exports, and run logs.")
    parser.add_argument("--profiles", type=Path, default=PROJECT_ROOT / "config" / "search_profiles.yaml")
    parser.add_argument(
        "--candidate-profile", type=Path, default=DEFAULT_CANDIDATE_PROFILE_PATH,
        help="Private evidence-only candidate profile; contact and autofill data are ignored.",
    )
    parser.add_argument(
        "--export-current-recommendations",
        type=Path,
        nargs="?",
        const=PROJECT_ROOT / "output" / "Current_Profile_Recommended_Queue.csv",
        help="Write a read-only CSV queue for the current profile revision; defaults to the local output directory.",
    )
    arguments = parser.parse_args(argv)
    try:
        candidate_profile = approved_candidate_profile(arguments.candidate_profile)
        if arguments.export_current_recommendations is not None:
            queue_rows = export_current_profile_recommendation_queue(
                candidate_profile,
                arguments.output_dir / "recommended_jobs.jsonl",
                arguments.export_current_recommendations,
            )
            print(
                "Exported current-profile recommendation queue: "
                f"{arguments.export_current_recommendations} ({queue_rows} rows; application_actions=0)"
            )
            return 0
        payloads = _load_visible_payloads(arguments.visible_payloads)
        result = run_discovery(
            candidate_profile, load_search_profiles(arguments.profiles), MappingVisiblePageAdapter(payloads),
            state_path=arguments.output_dir / "discovery_state.json",
            export_path=arguments.output_dir / "recommended_jobs.jsonl",
            run_log_path=arguments.output_dir / "discovery_runs.jsonl",
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Discovery did not run: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
