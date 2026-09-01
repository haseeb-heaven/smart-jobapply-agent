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
from job_profession.intake import validate_active_candidate_profile  # noqa: E402
from job_profession.intake import (  # noqa: E402
    completion_questions,
    pending_verification_batch,
    validate_candidate_intake,
)
from job_profession.scheduler import current_profile_recommendations, run_discovery  # noqa: E402
from job_profession.sources import MappingVisiblePageAdapter, load_search_profiles  # noqa: E402


DEFAULT_CANDIDATE_PROFILE_PATH = PROJECT_ROOT / "private" / "candidate_profile.yaml"
DEFAULT_CANDIDATE_INTAKE_PATH = PROJECT_ROOT / "private" / "candidate_intake.json"
_APPROVED_SKILL_SECTIONS = frozenset({"professional", "personal_open_source", "learning_or_exposure"})
_APPROVED_ROLE_SECTIONS = frozenset({"include", "exclude_title_terms"})
_APPROVED_HARD_EXCLUSION_SECTIONS = frozenset({"mandatory_requirements"})
_APPROVED_LOCATION_PREFERENCE_SECTIONS = frozenset({"locations", "work_modes"})
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
    """Extract only approved matching constraints and evidence from private YAML.

    This intentionally does not deserialize the complete private profile.  In
    particular, source status, compensation, contacts, and autofill policy are
    never placed in the mapping passed to ``CandidateProfile``.
    """

    if not path.exists():
        raise ValueError(f"Approved candidate profile is missing: {path}")
    roles: dict[str, list[str]] = {key: [] for key in _APPROVED_ROLE_SECTIONS}
    hard_exclusions: dict[str, list[str]] = {key: [] for key in _APPROVED_HARD_EXCLUSION_SECTIONS}
    skills: dict[str, list[str]] = {key: [] for key in _APPROVED_SKILL_SECTIONS}
    location_preferences: dict[str, list[str]] = {
        key: [] for key in _APPROVED_LOCATION_PREFERENCE_SECTIONS
    }
    years_experience: int | None = None
    active_top_level: str | None = None
    active_role_section: str | None = None
    active_hard_exclusion_section: str | None = None
    active_skill_section: str | None = None
    active_location_preference_section: str | None = None

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
            active_location_preference_section = None
            continue

        if active_top_level == "experience":
            if indent == 2 and ":" in stripped:
                key, value = (part.strip() for part in stripped.split(":", 1))
                if key == "years_experience" and value:
                    try:
                        years_experience = int(value)
                    except ValueError as exc:
                        raise ValueError(
                            f"Expected an integer years_experience in approved profile {path} line {line_number}"
                        ) from exc
            continue

        if active_top_level == "location_preferences":
            if indent == 2 and ":" in stripped:
                key, value = (part.strip() for part in stripped.split(":", 1))
                active_location_preference_section = (
                    key if key in _APPROVED_LOCATION_PREFERENCE_SECTIONS else None
                )
                if active_location_preference_section and value:
                    location_preferences[active_location_preference_section].extend(
                        _parse_inline_list(value, path=path, line_number=line_number)
                    )
            elif active_location_preference_section and stripped.startswith("- "):
                location_preferences[active_location_preference_section].append(
                    stripped[2:].strip().strip("\"'")
                )
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

    return {
        "experience": {"years_experience": years_experience},
        "location_preferences": location_preferences,
        "roles": roles,
        "hard_exclusions": hard_exclusions,
        "skills": skills,
    }


def approved_candidate_profile(profile_path: Path | None = None) -> CandidateProfile:
    """Load the current evidence-only profile without importing autofill data."""

    return CandidateProfile.from_mapping(_approved_profile_mapping(profile_path or DEFAULT_CANDIDATE_PROFILE_PATH))


def _approved_fact(facts: Mapping[str, Any], dotted_path: str) -> Any:
    """Read an exact dotted fact or its equivalent nested representation."""

    if dotted_path in facts:
        return facts[dotted_path]
    current: Any = facts
    for segment in dotted_path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return None
        current = current[segment]
    return current


def _fact_strings(value: Any) -> list[str]:
    """Project explicit string evidence while ignoring unsupported fact shapes."""

    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Mapping):
        strings: list[str] = []
        for nested in value.values():
            strings.extend(_fact_strings(nested))
        return strings
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def _active_intake_profile_mapping(active_intake: Mapping[str, Any]) -> dict[str, Any]:
    """Project only matcher-approved evidence from a verified intake revision."""

    facts = active_intake["approved_facts"]
    years_experience = _approved_fact(facts, "experience.years_experience")
    if years_experience is None:
        years_experience = _approved_fact(facts, "experience.total_years")
    if isinstance(years_experience, bool) or not isinstance(years_experience, int):
        years_experience = None

    evidence_by_skill = _approved_fact(facts, "skills.evidence_by_skill")
    if not isinstance(evidence_by_skill, Mapping):
        evidence_by_skill = _approved_fact(facts, "evidence_by_skill")
    approved_evidence = {
        str(skill): label
        for skill, label in evidence_by_skill.items()
        if isinstance(skill, str)
        and skill.strip()
        and label in {"professional", "personal_open_source", "learning_or_exposure"}
    } if isinstance(evidence_by_skill, Mapping) else {}

    employment_types = _fact_strings(_approved_fact(facts, "targets.employment_types"))
    if not employment_types:
        employment_types = _fact_strings(
            _approved_fact(facts, "employment_type_preferences")
        )
    work_authorizations = _fact_strings(
        _approved_fact(facts, "work_authorization.authorized_locations")
    )
    if not work_authorizations:
        work_authorizations = _fact_strings(_approved_fact(facts, "work_authorizations"))

    return {
        "experience": {"years_experience": years_experience},
        "roles": {
            "include": _fact_strings(_approved_fact(facts, "roles.include")),
            "exclude_title_terms": _fact_strings(
                _approved_fact(facts, "roles.exclude_title_terms")
            ),
        },
        "hard_exclusions": {
            "mandatory_requirements": _fact_strings(
                _approved_fact(facts, "hard_exclusions.mandatory_requirements")
            )
        },
        "skills": {
            "professional": _fact_strings(_approved_fact(facts, "skills.professional")),
            "personal_open_source": _fact_strings(
                _approved_fact(facts, "skills.personal_open_source")
            ),
            "learning_or_exposure": _fact_strings(
                _approved_fact(facts, "skills.learning_or_exposure")
            ),
        },
        "location_preferences": {
            "locations": _fact_strings(
                _approved_fact(facts, "location_preferences.locations")
            ),
            "work_modes": _fact_strings(
                _approved_fact(facts, "location_preferences.work_modes")
            ),
        },
        "targets": {"employment_types": employment_types},
        "work_authorizations": work_authorizations,
        "evidence_by_skill": approved_evidence,
    }


def active_candidate_profile(intake_path: Path) -> CandidateProfile:
    """Load only an integrity-checked, explicitly user-confirmed intake."""

    if not intake_path.exists():
        raise ValueError("Candidate intake is missing")
    payload = json.loads(intake_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("Candidate intake JSON must be an object")
    active_intake = validate_active_candidate_profile(payload)
    return CandidateProfile.from_mapping(_active_intake_profile_mapping(active_intake))


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


def _read_candidate_intake(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        raise ValueError("Candidate intake is missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("Candidate intake JSON must be an object")
    return payload


def _question_draft_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Select draft fields without exposing the source payload."""

    if "state" in payload:
        return {
            "schema_version": payload.get("schema_version"),
            "documents": payload.get("documents"),
            "approved_facts": payload.get("approved_facts"),
            "unknown_fields": payload.get("unknown_fields"),
            "contradictions": payload.get("contradictions"),
            "pending_facts": payload.get("pending_facts"),
        }
    return payload


def _print_pending_intake_questions(payload: Mapping[str, Any]) -> tuple[int, str]:
    draft = validate_candidate_intake(_question_draft_payload(payload))
    questions = completion_questions(draft)
    if not questions:
        return (
            0,
            "Candidate intake has no unresolved items. Activate with actor='user' and rerun discovery.",
        )
    rendered = ["Candidate onboarding questions (ask all at once):"]
    rendered.extend(f"- {question}" for question in questions)
    rendered.append(
        "Do not infer or default sensitive values. Apply only explicit candidate-confirmed answers."
    )
    return (2, "\n".join(rendered))


def _intake_question_bundle(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build a machine-readable onboarding payload for host orchestration."""

    draft = validate_candidate_intake(_question_draft_payload(payload))
    requires_confirmation = bool(
        draft["unknown_fields"] or draft["contradictions"] or draft["pending_facts"]
    )
    questions = completion_questions(draft)
    safe_batch = pending_verification_batch(draft)
    contradiction_questions = iter(questions)
    safe_contradictions = [
        {
            "kind": "contradiction",
            "field": item["field"],
            "prompt": next(contradiction_questions),
        }
        for item in safe_batch["contradictions"]
    ]
    safe_unknowns = [
        {
            "kind": "unknown",
            "field": item,
            "prompt": next(contradiction_questions),
        }
        for item in safe_batch["unknown_fields"]
    ]
    safe_pending = [
        {
            "kind": "pending",
            "field": item["field"],
            "prompt": next(contradiction_questions),
        }
        for item in safe_batch["pending_facts"]
    ]
    question_groups = {
        "contradictions": safe_contradictions,
        "unknown_fields": safe_unknowns,
        "pending_facts": safe_pending,
    }
    prompt_block = [
        {
            "kind": "ask_once",
            "instructions": "Ask all unresolved items in one candidate-facing round before continuing discovery."
        }
    ] + safe_contradictions + safe_unknowns + safe_pending
    return {
        "status": "ready" if not requires_confirmation else "blocked",
        "schema_version": draft["schema_version"],
        "questions": questions,
        "question_count": len(questions),
        "question_plan": {
            "ask_once": True,
            "unknown_count": len(draft["unknown_fields"]),
            "contradiction_count": len(draft["contradictions"]),
            "pending_count": len(draft["pending_facts"]),
            "groups": question_groups,
            "prompts": prompt_block,
        },
        "pending_verification_batch": {
            **safe_batch,
        },
        "requires_user_confirmation": requires_confirmation,
    }


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
        "--candidate-intake",
        type=Path,
        help="Required active, user-confirmed candidate intake revision.",
    )
    parser.add_argument(
        "--show-intake-questions",
        action="store_true",
        help="Validate a draft intake and print unresolved onboarding questions before discovery.",
    )
    parser.add_argument(
        "--onboarding-format",
        choices=("text", "json"),
        default="text",
        help="Render onboarding output as plain text or JSON.",
    )
    parser.add_argument(
        "--candidate-profile",
        type=Path,
        help=argparse.SUPPRESS,
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
        if arguments.candidate_intake is None:
            raise ValueError("--candidate-intake is required and must reference an active user-confirmed intake")
        raw_candidate_intake = _read_candidate_intake(arguments.candidate_intake)
        if arguments.show_intake_questions:
            if arguments.onboarding_format == "json":
                bundle = _intake_question_bundle(raw_candidate_intake)
                print(json.dumps(bundle, indent=2, sort_keys=True))
                return 0 if bundle["status"] == "ready" else 2

            status, message = _print_pending_intake_questions(raw_candidate_intake)
            print(message)
            return status

        try:
            active_intake = validate_active_candidate_profile(raw_candidate_intake)
        except ValueError:
            status, message = _print_pending_intake_questions(raw_candidate_intake)
            if status == 0:
                message = "Discovery is blocked until candidate intake is activated with actor='user'."
            print(f"Discovery blocked until intake is fully confirmed: {message}", file=sys.stderr)
            return 2 if status == 0 else status

        candidate_profile = CandidateProfile.from_mapping(_active_intake_profile_mapping(active_intake))
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
    except json.JSONDecodeError:
        print("Discovery did not run: local input is not valid JSON.", file=sys.stderr)
        return 2
    except OSError:
        print("Discovery did not run: unable to read or write local input.", file=sys.stderr)
        return 2
    except ValueError as exc:
        if str(exc) == "Candidate intake is missing":
            print("Discovery did not run: candidate intake is missing.", file=sys.stderr)
            return 2
        print("Discovery did not run: local input failed validation.", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
