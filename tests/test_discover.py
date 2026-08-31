from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]


def load_discover_module():
    script_path = PROJECT_ROOT / "job_profession" / "scripts" / "discover.py"
    spec = importlib.util.spec_from_file_location("discover_for_test", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_installer_module():
    script_path = PROJECT_ROOT / "job_profession" / "scripts" / "install_launch_agent.py"
    spec = importlib.util.spec_from_file_location("install_launch_agent_for_discover_test", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_candidate_profile(path: Path, professional: str, personal: str = "OpenAI") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""\
roles:
  include:
    - Python Backend Developer
  exclude_title_terms:
    - senior
hard_exclusions:
  mandatory_requirements:
    - ML model training or deployment ownership
skills:
  professional:
    backend: [{professional}]
  personal_open_source:
    tools: [{personal}]
  learning_or_exposure:
    - Docker
source_status:
  phone:
    status: needs_confirmation
    value: +971000000000
  compensation_uae:
    status: needs_confirmation
    value: 22000 AED
autofill_policy:
  secret_answer: never-import-this
""",
        encoding="utf-8",
    )


def test_profile_loader_reloads_approved_skill_revisions_for_matching(tmp_path: Path):
    discover = load_discover_module()
    from job_profession.matcher import score_job
    from job_profession.models import JobListing

    profile_path = tmp_path / "private" / "candidate_profile.yaml"
    job = JobListing(
        title="Python Backend Developer",
        description="Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL REST APIs.",
    )
    write_candidate_profile(profile_path, "Python, FastAPI, REST APIs, PostgreSQL, unit testing")

    first_profile = discover.approved_candidate_profile(profile_path)
    first_result = score_job(first_profile, job)

    write_candidate_profile(profile_path, "Python", "FastAPI, REST APIs, PostgreSQL, unit testing")
    revised_profile = discover.approved_candidate_profile(profile_path)
    revised_result = score_job(revised_profile, job)

    assert first_result.decision == "recommended"
    assert revised_result.decision != "recommended"
    assert "fastapi" in {skill.casefold() for skill in first_profile.professional_skills}
    assert "fastapi" in {skill.casefold() for skill in revised_profile.personal_open_source_skills}


def test_profile_loader_imports_only_evidence_and_role_fields(tmp_path: Path):
    discover = load_discover_module()
    profile_path = tmp_path / "private" / "candidate_profile.yaml"
    write_candidate_profile(profile_path, "Python, FastAPI")

    profile = discover.approved_candidate_profile(profile_path)
    profile_values = {
        *profile.professional_skills,
        *profile.personal_open_source_skills,
        *profile.learning_or_exposure_skills,
        *profile.role_targets,
        *profile.excluded_title_terms,
        *profile.mandatory_excluded_requirements,
        *profile.location_preferences,
        *profile.work_mode_preferences,
        *profile.evidence_by_skill,
    }

    assert {"python", "fastapi"}.issubset({value.casefold() for value in profile.professional_skills})
    assert profile.location_preferences == ()
    assert profile.work_mode_preferences == ()
    assert profile.mandatory_excluded_requirements == ("ML model training or deployment ownership",)
    assert all("971000000000" not in value for value in profile_values)
    assert all("22000" not in value for value in profile_values)
    assert all("never-import" not in value for value in profile_values)


def test_profile_loader_enforces_approved_mandatory_ml_exclusion(tmp_path: Path):
    discover = load_discover_module()
    from job_profession.matcher import score_job
    from job_profession.models import JobListing

    profile_path = tmp_path / "private" / "candidate_profile.yaml"
    write_candidate_profile(profile_path, "Python, FastAPI, REST APIs, PostgreSQL, unit testing")
    profile = discover.approved_candidate_profile(profile_path)
    job = JobListing(
        title="Python Backend Developer",
        description=(
            "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL. "
            "Must own ML model training and deployment."
        ),
    )

    result = score_job(profile, job)

    assert result.score == 0
    assert result.decision == "reject"
    assert "mandatory ML model training/deployment ownership is excluded" in result.reasons


def test_repository_profile_exposes_classified_evidence_without_confirmation_fields():
    discover = load_discover_module()

    profile = discover.approved_candidate_profile()

    assert "python" in {skill.casefold() for skill in profile.professional_skills}
    assert "openai" in {skill.casefold() for skill in profile.personal_open_source_skills}
    assert "docker" in {skill.casefold() for skill in profile.learning_or_exposure_skills}
    assert profile.location_preferences == ()
    assert profile.work_mode_preferences == ()


def test_runtime_profile_projection_keeps_only_evidence_fields(tmp_path: Path, monkeypatch):
    discover = load_discover_module()
    installer = load_installer_module()
    source_profile = tmp_path / "source" / "candidate_profile.yaml"
    write_candidate_profile(source_profile, "Python, FastAPI")
    runtime_private = tmp_path / "runtime" / "private"
    monkeypatch.setattr(installer, "SOURCE_CANDIDATE_PROFILE", source_profile)
    monkeypatch.setattr(installer, "RUNTIME_PRIVATE", runtime_private)

    installer._write_runtime_candidate_profile()

    runtime_profile_path = runtime_private / "candidate_profile.yaml"
    runtime_profile = discover.approved_candidate_profile(runtime_profile_path)
    runtime_text = runtime_profile_path.read_text(encoding="utf-8")
    assert "Python" in runtime_profile.professional_skills
    assert "OpenAI" in runtime_profile.personal_open_source_skills
    assert "Docker" in runtime_profile.learning_or_exposure_skills
    assert runtime_profile.mandatory_excluded_requirements == ("ML model training or deployment ownership",)
    assert "ML model training or deployment ownership" in runtime_text
    assert "+971000000000" not in runtime_text
    assert "22000 AED" not in runtime_text
    assert "never-import-this" not in runtime_text


def test_current_profile_queue_cli_exports_only_active_safe_recommendations(tmp_path: Path, capsys):
    discover = load_discover_module()
    from job_profession.matcher import matcher_policy_revision
    from job_profession.scheduler import candidate_profile_revision

    profile_path = tmp_path / "private" / "candidate_profile.yaml"
    write_candidate_profile(profile_path, "Python, FastAPI, REST APIs, PostgreSQL, unit testing")
    active_profile = discover.approved_candidate_profile(profile_path)
    active_revision = candidate_profile_revision(active_profile)
    output_dir = tmp_path / "data"
    export_path = output_dir / "recommended_jobs.jsonl"
    output_dir.mkdir()
    safe_row = {
        "record_type": "recommended_job_for_human_review",
        "discovery_mode": "export_only",
        "application_actions": 0,
        "profile_revision": active_revision,
        "matcher_policy_revision": matcher_policy_revision(),
        "decision": "recommended",
        "score": 85,
        "minimum_profile_fit_score": 85,
        "threshold_met": True,
        "fingerprint": "d" * 64,
        "platform": "indeed",
        "title": "Python Backend Developer",
        "company": "Acme",
        "location": "Bengaluru",
        "work_mode": "hybrid",
        "url": "https://in.indeed.com/viewjob?jk=current-profile-queue",
        "search_url": "https://www.indeed.com/jobs?q=Python+Backend+Developer",
        "discovered_at": "2026-09-01T00:00:00+00:00",
        "reasons": ["verified professional evidence"],
        "gaps": [],
    }
    stale_row = {**safe_row, "fingerprint": "e" * 64, "profile_revision": "e" * 64}
    fixture_row = {**safe_row, "fingerprint": "f" * 64, "is_test_fixture": True}
    export_path.write_text(
        "".join(json.dumps(row) + "\n" for row in (safe_row, stale_row, fixture_row)), encoding="utf-8"
    )
    historical_export = export_path.read_text(encoding="utf-8")
    queue_path = tmp_path / "output" / "Current_Profile_Recommended_Queue.csv"

    exit_code = discover.main(
        [
            "--candidate-profile",
            str(profile_path),
            "--output-dir",
            str(output_dir),
            "--export-current-recommendations",
            str(queue_path),
        ]
    )

    assert exit_code == 0
    assert "Exported current-profile recommendation queue" in capsys.readouterr().out
    with queue_path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["fingerprint"] == safe_row["fingerprint"]
    assert rows[0]["profile_revision"] == active_revision
    assert rows[0]["application_actions"] == "0"
    assert export_path.read_text(encoding="utf-8") == historical_export
