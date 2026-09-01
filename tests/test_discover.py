from __future__ import annotations

import csv
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess


PROJECT_ROOT = Path(__file__).parents[1]


def load_discover_module():
    script_path = PROJECT_ROOT / "jobapply_agent" / "scripts" / "discover.py"
    spec = importlib.util.spec_from_file_location("discover_for_test", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_installer_module():
    script_path = PROJECT_ROOT / "jobapply_agent" / "scripts" / "install_launch_agent.py"
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


def write_active_candidate_intake(path: Path) -> None:
    from jobapply_agent.intake import activate_candidate_profile, validate_candidate_intake

    professional = ["Python", "FastAPI", "REST APIs", "PostgreSQL", "unit testing"]
    draft = validate_candidate_intake(
        {
            "schema_version": 1,
            "documents": [],
            "approved_facts": {
                "experience": {"total_years": 3},
                "roles": {
                    "include": ["Python Backend Developer"],
                    "exclude_title_terms": ["senior"],
                },
                "skills": {
                    "professional": professional,
                    "personal_open_source": ["OpenAI"],
                    "learning_or_exposure": ["Docker"],
                    "evidence_by_skill": {skill: "professional" for skill in professional},
                },
            },
            "unknown_fields": [],
            "contradictions": [],
            "pending_facts": [],
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(activate_candidate_profile(draft, actor="user")), encoding="utf-8")


def test_profile_loader_reloads_approved_skill_revisions_for_matching(tmp_path: Path):
    discover = load_discover_module()
    from jobapply_agent.matcher import score_job
    from jobapply_agent.models import JobListing

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


def test_profile_loader_projects_approved_eligibility_constraints(tmp_path: Path):
    discover = load_discover_module()
    profile_path = tmp_path / "private" / "candidate_profile.yaml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        """\
experience:
  years_experience: 3
location_preferences:
  locations: [Hyderabad]
  work_modes: [onsite, hybrid]
roles:
  include: [Python Backend Developer]
skills:
  professional:
    backend: [Python, FastAPI]
source_status:
  phone: +971000000000
""",
        encoding="utf-8",
    )

    profile = discover.approved_candidate_profile(profile_path)

    assert profile.years_experience == 3
    assert profile.location_preferences == ("Hyderabad",)
    assert profile.work_mode_preferences == ("onsite", "hybrid")
    assert "+971000000000" not in repr(profile)


def test_profile_loader_enforces_approved_mandatory_ml_exclusion(tmp_path: Path):
    discover = load_discover_module()
    from jobapply_agent.matcher import score_job
    from jobapply_agent.models import JobListing

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

    profile = discover.approved_candidate_profile(
        PROJECT_ROOT / "jobapply_agent" / "private.example" / "candidate_profile.yaml"
    )

    assert "python" in {skill.casefold() for skill in profile.professional_skills}
    assert "llm" in {skill.casefold() for skill in profile.personal_open_source_skills}
    assert "docker" in {skill.casefold() for skill in profile.learning_or_exposure_skills}
    assert profile.location_preferences == ()
    assert profile.work_mode_preferences == ()


def test_generated_runtime_bootstrap_uses_safe_active_intake_projection(tmp_path: Path, monkeypatch):
    from jobapply_agent.intake import activate_candidate_profile

    discover = load_discover_module()
    installer = load_installer_module()
    home = tmp_path / "home"
    runtime_root = home / "Library" / "Caches" / "smart_jobapply_agent_runtime"
    source_intake = tmp_path / "source" / "candidate_intake.json"
    secrets = (
        "candidate@example.invalid",
        "22000 AED",
        "source-status-private",
        "resume-private.pdf",
        "never-autofill-this",
    )
    draft = discover.validate_candidate_intake(
        {
            "schema_version": 1,
            "documents": [{"document_id": "resume", "path": "resume-private.pdf", "sha256": "0" * 64, "media_type": "application/pdf", "review_state": "untrusted"}],
            "approved_facts": {
                "identity": {"contact": "candidate@example.invalid"},
                "compensation": "22000 AED",
                "source_status": "source-status-private",
                "autofill_policy": "never-autofill-this",
                "experience": {"total_years": 3},
                "roles": {"include": ["Python Backend Developer"]},
                "skills": {"professional": ["Python", "FastAPI"], "personal_open_source": ["OpenAI"], "learning_or_exposure": ["Docker"], "evidence_by_skill": {"Python": "professional", "FastAPI": "professional", "OpenAI": "personal_open_source", "Docker": "learning_or_exposure"}},
            },
            "unknown_fields": [],
            "contradictions": [],
            "pending_facts": [],
        }
    )
    active = activate_candidate_profile(draft, actor="user")
    source_intake.parent.mkdir(parents=True)
    source_intake.write_text(json.dumps(active), encoding="utf-8")

    monkeypatch.setattr(installer, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(installer, "RUNTIME_BOOTSTRAP", runtime_root / "launchd_discover_bootstrap.sh")
    monkeypatch.setattr(installer, "RUNTIME_SRC", runtime_root / "src" / "jobapply_agent")
    monkeypatch.setattr(installer, "RUNTIME_DISCOVER_SCRIPT", runtime_root / "discover.py")
    monkeypatch.setattr(installer, "RUNTIME_CONFIG", runtime_root / "config")
    monkeypatch.setattr(installer, "RUNTIME_PRIVATE", runtime_root / "private")
    monkeypatch.setattr(installer, "RUNTIME_CANDIDATE_INTAKE", runtime_root / "private" / "candidate_intake.json")
    monkeypatch.setattr(installer, "SOURCE_CANDIDATE_INTAKE", source_intake)
    installer._sync_runtime_snapshot()
    installer._write_runtime_bootstrap()

    output_dir = tmp_path / "runtime-output"
    shell = shutil.which("zsh") or shutil.which("bash")
    assert shell is not None
    completed = subprocess.run(
        [shell, str(installer.RUNTIME_BOOTSTRAP), str(PROJECT_ROOT), str(output_dir)],
        cwd=PROJECT_ROOT,
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.splitlines()[0])["application_actions"] == 0
    assert discover.active_candidate_profile(installer.RUNTIME_CANDIDATE_INTAKE).professional_skills == ("Python", "FastAPI")
    assert json.loads(source_intake.read_text(encoding="utf-8"))["revision_hash"] == active["revision_hash"]
    assert (output_dir / "Current_Profile_Recommended_Queue.csv").exists()
    for runtime_file in runtime_root.rglob("*"):
        if runtime_file.is_file():
            runtime_bytes = runtime_file.read_bytes()
            assert all(secret.encode("utf-8") not in runtime_bytes for secret in secrets)


def test_launchd_template_renders_local_paths_without_publishing_machine_details(tmp_path: Path, monkeypatch):
    installer = load_installer_module()
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(installer, "PROJECT_ROOT", tmp_path / "checkout with & character")
    monkeypatch.setattr(installer, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(installer, "RUNTIME_BOOTSTRAP", runtime_root / "launchd_discover_bootstrap.sh")

    rendered = installer._render_plist()

    assert "__" not in rendered
    assert "/Users/private-candidate/" not in rendered
    assert "GoogleDrive-candidate@example.invalid" not in rendered
    assert "checkout with &amp; character" in rendered
    assert str(runtime_root / "data") in rendered


def test_current_profile_queue_cli_exports_only_active_safe_recommendations(tmp_path: Path, capsys):
    discover = load_discover_module()
    from jobapply_agent.matcher import matcher_policy_revision
    from jobapply_agent.scheduler import candidate_profile_revision

    intake_path = tmp_path / "private" / "candidate_intake.json"
    write_active_candidate_intake(intake_path)
    active_profile = discover.active_candidate_profile(intake_path)
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
            "--candidate-intake",
            str(intake_path),
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


def test_discovery_refuses_to_consume_an_unconfirmed_candidate_intake(
    tmp_path: Path, capsys,
):
    discover = load_discover_module()
    from jobapply_agent.intake import validate_candidate_intake

    intake_path = tmp_path / "private" / "candidate_intake.json"
    intake_path.parent.mkdir()
    draft = validate_candidate_intake(
        {
            "schema_version": 1,
            "documents": [],
            "approved_facts": {"experience.total_years": 3},
            "unknown_fields": ["work_authorization"],
            "contradictions": [],
            "pending_facts": [],
        }
    )
    intake_path.write_text(json.dumps(draft), encoding="utf-8")

    exit_code = discover.main(
        [
            "--candidate-intake",
            str(intake_path),
            "--output-dir",
            str(tmp_path / "data"),
        ]
    )

    assert exit_code == 2
    assert "blocked until intake is fully confirmed" in capsys.readouterr().err.casefold()


def test_discover_intake_questions_mode_lists_all_pending_questions(
    tmp_path: Path, capsys
):
    discover = load_discover_module()
    from jobapply_agent.intake import validate_candidate_intake

    draft = validate_candidate_intake(
        {
            "schema_version": 1,
            "documents": [],
            "approved_facts": {},
            "unknown_fields": ["work_authorization", "compensation"],
            "contradictions": [
                {
                    "field": "experience.current_title",
                    "status": "VERIFY",
                    "values": ["Backend Engineer", "Python Developer"],
                    "evidence_ids": ["resume-primary", "candidate-note-1"],
                }
            ],
            "pending_facts": [
                {
                    "field": "location_preferences",
                    "status": "VERIFY",
                    "question": "Which locations and work modes are acceptable?",
                    "reason": "No candidate-approved preference is available.",
                }
            ],
        }
    )
    intake_path = tmp_path / "candidate_intake.json"
    intake_path.write_text(json.dumps(draft), encoding="utf-8")

    exit_code = discover.main(
        [
            "--candidate-intake",
            str(intake_path),
            "--show-intake-questions",
        ]
    )

    captured_capture = capsys.readouterr()
    captured = captured_capture.out + captured_capture.err
    assert exit_code == 2
    assert "Candidate onboarding questions (ask all at once)" in captured
    assert "experience.current_title" in captured
    assert "work_authorization" in captured
    assert "Backend Engineer" not in captured
    assert "Python Developer" not in captured
    assert "resume-primary" not in captured
    assert "candidate-note-1" not in captured
    assert "Which locations and work modes are acceptable?" not in captured
    assert "No candidate-approved preference is available." not in captured


def test_discover_intake_questions_mode_can_emit_json_bundle(
    tmp_path: Path, capsys
):
    discover = load_discover_module()
    from jobapply_agent.intake import validate_candidate_intake

    draft = validate_candidate_intake(
        {
            "schema_version": 1,
            "documents": [],
            "approved_facts": {},
            "unknown_fields": ["work_authorization", "compensation"],
            "contradictions": [
                {
                    "field": "experience.current_title",
                    "status": "VERIFY",
                    "values": ["Backend Engineer", "Python Developer"],
                    "evidence_ids": ["resume-primary", "candidate-note-1"],
                }
            ],
            "pending_facts": [
                {
                    "field": "location_preferences",
                    "status": "VERIFY",
                    "question": "Which locations and work modes are acceptable?",
                    "reason": "No candidate-approved preference is available.",
                }
            ],
        }
    )
    intake_path = tmp_path / "candidate_intake.json"
    intake_path.write_text(json.dumps(draft), encoding="utf-8")

    exit_code = discover.main(
        [
            "--candidate-intake",
            str(intake_path),
            "--show-intake-questions",
            "--onboarding-format",
            "json",
        ]
    )

    captured_capture = capsys.readouterr()
    payload = json.loads(captured_capture.out or "null")
    assert exit_code == 2
    assert payload["status"] == "blocked"
    assert "verification conflict" in " ".join(payload["questions"])
    assert payload["requires_user_confirmation"] is True
    assert payload["question_count"] == 4
    assert payload["question_plan"]["ask_once"] is True
    assert payload["question_plan"]["unknown_count"] == 2
    assert payload["question_plan"]["contradiction_count"] == 1
    assert payload["question_plan"]["pending_count"] == 1
    assert len(payload["question_plan"]["prompts"]) == 1 + payload["question_count"]
    assert payload["question_plan"]["prompts"][0]["instructions"].startswith("Ask all unresolved")
    assert payload["question_plan"]["groups"]["unknown_fields"][0]["field"] in {
        "work_authorization",
        "compensation",
    }
    assert payload["question_plan"]["groups"]["contradictions"][0]["kind"] == "contradiction"
    assert payload["question_plan"]["groups"]["contradictions"][0]["field"] == "experience.current_title"
    assert set(payload["pending_verification_batch"]["unknown_fields"]) == {
        "work_authorization",
        "compensation",
    }
    assert payload["pending_verification_batch"]["contradictions"] == [
        {"field": "experience.current_title", "status": "VERIFY"}
    ]
    assert payload["pending_verification_batch"]["pending_facts"] == [
        {"field": "location_preferences", "status": "VERIFY"}
    ]
    rendered = json.dumps(payload)
    for private_value in (
        "Backend Engineer",
        "Python Developer",
        "resume-primary",
        "candidate-note-1",
        "Which locations and work modes are acceptable?",
        "No candidate-approved preference is available.",
    ):
        assert private_value not in rendered


def test_discover_intake_questions_mode_can_emit_ready_json_bundle(
    tmp_path: Path, capsys
):
    discover = load_discover_module()
    from jobapply_agent.intake import activate_candidate_profile, validate_candidate_intake

    draft = validate_candidate_intake(
        {
            "schema_version": 1,
            "documents": [],
            "approved_facts": {"experience": {"total_years": 3}},
            "unknown_fields": [],
            "contradictions": [],
            "pending_facts": [],
        }
    )
    intake_path = tmp_path / "candidate_intake.json"
    intake_path.write_text(
        json.dumps(activate_candidate_profile(draft, actor="user")),
        encoding="utf-8"
    )

    exit_code = discover.main(
        [
            "--candidate-intake",
            str(intake_path),
            "--show-intake-questions",
            "--onboarding-format",
            "json",
        ]
    )

    payload = json.loads((capsys.readouterr().out or "null"))
    assert exit_code == 0
    assert payload["status"] == "ready"
    assert payload["requires_user_confirmation"] is False
    assert payload["question_count"] == 0
    assert payload["question_plan"]["ask_once"] is True
    assert payload["question_plan"]["unknown_count"] == 0
    assert payload["question_plan"]["contradiction_count"] == 0
    assert payload["question_plan"]["pending_count"] == 0
    assert payload["question_plan"]["prompts"] == [
        {"kind": "ask_once", "instructions": "Ask all unresolved items in one candidate-facing round before continuing discovery."}
    ]
    assert payload["pending_verification_batch"]["unknown_fields"] == []
    assert payload["pending_verification_batch"]["contradictions"] == []
    assert payload["pending_verification_batch"]["pending_facts"] == []


def test_discover_intake_questions_mode_can_parse_state_wrapped_draft(
    tmp_path: Path,
    capsys,
):
    discover = load_discover_module()
    from jobapply_agent.intake import validate_candidate_intake

    draft = validate_candidate_intake(
        {
            "schema_version": 1,
            "documents": [],
            "approved_facts": {"experience": {"total_years": 4}},
            "unknown_fields": [],
            "contradictions": [
                {
                    "field": "experience.title",
                    "status": "VERIFY",
                    "values": ["Engineer", "Developer"],
                    "evidence_ids": ["resume-primary", "resume-secondary"],
                }
            ],
            "pending_facts": [],
        }
    )
    wrapped = {"state": "draft", **draft}

    intake_path = tmp_path / "candidate_intake_state.json"
    intake_path.write_text(json.dumps(wrapped), encoding="utf-8")

    exit_code = discover.main(
        [
            "--candidate-intake",
            str(intake_path),
            "--show-intake-questions",
            "--onboarding-format",
            "json",
        ]
    )

    captured = json.loads(capsys.readouterr().out or "null")
    assert exit_code == 2
    assert captured["status"] == "blocked"
    assert captured["requires_user_confirmation"] is True
    assert captured["pending_verification_batch"]["contradictions"] == [
        {"field": "experience.title", "status": "VERIFY"}
    ]


def test_discover_onboarding_uses_opaque_references_for_unallowlisted_fields(
    tmp_path: Path, capsys
):
    discover = load_discover_module()
    from jobapply_agent.intake import validate_candidate_intake

    draft = validate_candidate_intake(
        {
            "schema_version": 1,
            "documents": [
                {
                    "document_id": "PRIVATE_DOCUMENT_ID",
                    "path": "/private/candidate/resume.pdf",
                    "sha256": "a" * 64,
                    "media_type": "application/pdf",
                    "review_state": "untrusted",
                }
            ],
            "approved_facts": {},
            "unknown_fields": ["private.candidate.answer"],
            "contradictions": [
                {
                    "field": "private.candidate.conflict",
                    "status": "VERIFY",
                    "values": ["PRIVATE_VALUE_A", "PRIVATE_VALUE_B"],
                    "evidence_ids": ["PRIVATE_EVIDENCE_ID"],
                }
            ],
            "pending_facts": [
                {
                    "field": "private.candidate.pending",
                    "status": "VERIFY",
                    "question": "PRIVATE_USER_QUESTION",
                    "reason": "PRIVATE_USER_REASON",
                }
            ],
        }
    )
    intake_path = tmp_path / "private" / "candidate_intake.json"
    intake_path.parent.mkdir()
    intake_path.write_text(json.dumps(draft), encoding="utf-8")

    exit_code = discover.main(
        [
            "--candidate-intake",
            str(intake_path),
            "--show-intake-questions",
            "--onboarding-format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert exit_code == 2
    for private_value in (
        "PRIVATE_DOCUMENT_ID",
        "/private/candidate/resume.pdf",
        "PRIVATE_VALUE_A",
        "PRIVATE_VALUE_B",
        "PRIVATE_EVIDENCE_ID",
        "PRIVATE_USER_QUESTION",
        "PRIVATE_USER_REASON",
        "private.candidate",
    ):
        assert private_value not in rendered
    assert "review-unknown-1" in rendered
    assert "review-contradiction-1" in rendered
    assert "review-pending-1" in rendered


def test_discover_onboarding_errors_do_not_echo_private_intake_path(
    tmp_path: Path, capsys
):
    discover = load_discover_module()
    missing_path = tmp_path / "private candidate" / "candidate_intake.json"

    exit_code = discover.main(
        [
            "--candidate-intake",
            str(missing_path),
            "--show-intake-questions",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert str(missing_path) not in captured.out + captured.err
    assert "candidate intake is missing" in captured.err.casefold()


def test_active_intake_projects_only_candidate_approved_employment_and_authorization(
    tmp_path: Path,
):
    discover = load_discover_module()
    from jobapply_agent.intake import activate_candidate_profile, validate_candidate_intake

    draft = validate_candidate_intake(
        {
            "schema_version": 1,
            "documents": [],
            "approved_facts": {
                "skills": {
                    "professional": ["Python"],
                },
                "targets": {"employment_types": ["Full-Time"]},
                "work_authorization": {"authorized_locations": ["India"]},
            },
            "unknown_fields": [],
            "contradictions": [],
            "pending_facts": [],
        }
    )
    intake_path = tmp_path / "candidate_intake.json"
    intake_path.write_text(
        json.dumps(activate_candidate_profile(draft, actor="user")), encoding="utf-8"
    )

    profile = discover.active_candidate_profile(intake_path)

    assert profile.employment_type_preferences == ("full-time",)
    assert profile.work_authorizations == ("india",)
