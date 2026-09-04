from __future__ import annotations

from collections.abc import Iterator
import csv
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).parents[1]
DISCOVER_SCRIPT = PROJECT_ROOT / "jobapply_agent" / "scripts" / "discover.py"
PRIVATE_RUNTIME_ROOT = PROJECT_ROOT / "jobapply_agent" / "private"


@pytest.fixture
def private_test_dir() -> Iterator[Path]:
    PRIVATE_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    test_directory = Path(
        tempfile.mkdtemp(prefix="discover-sonar-path-", dir=PRIVATE_RUNTIME_ROOT)
    )
    try:
        yield test_directory
    finally:
        shutil.rmtree(test_directory, ignore_errors=True)


@pytest.fixture
def private_sibling_test_dir() -> Iterator[Path]:
    test_directory = Path(
        tempfile.mkdtemp(prefix="private-sonar-path-", dir=PRIVATE_RUNTIME_ROOT.parent)
    )
    try:
        yield test_directory
    finally:
        shutil.rmtree(test_directory, ignore_errors=True)


def load_discover_module():
    script_path = PROJECT_ROOT / "jobapply_agent" / "scripts" / "discover.py"
    spec = importlib.util.spec_from_file_location("discover_for_test", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
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


def write_onboarding_draft(path: Path, unknown_fields: list[str]) -> bytes:
    draft = {
        "schema_version": 1,
        "documents": [],
        "approved_facts": {
            "experience": {"total_years": 3},
            "roles": {"include": ["Backend Developer"]},
        },
        "unknown_fields": unknown_fields,
        "contradictions": [],
        "pending_facts": [],
        "state": "draft",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft), encoding="utf-8")
    return path.read_bytes()


def rich_review_payload(*, unknown_fields: list[str] | None = None) -> dict[str, object]:
    return {
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
        "approved_facts": {
            "experience": {
                "roles_and_dates": [
                    {
                        "title": "Backend Engineer",
                        "company": "Example Systems",
                        "dates": "2022-2024",
                    }
                ],
                "achievements_and_metrics": [
                    {
                        "achievement": "Reduced API latency",
                        "metric": "35 percent",
                    }
                ],
            },
            "education": [
                {
                    "degree": "B.Tech in Computer Science",
                    "institution": "Example University",
                    "year": 2020,
                }
            ],
            "projects": [
                {
                    "name": "Queue Planner",
                    "summary": "Bounded candidate review queue",
                    "technologies": "Python and SQLite",
                }
            ],
            "identity": {"contact": "PRIVATE_CONTACT@example.test"},
            "resume": {"extracted_text": "RAW_RESUME_TEXT_MUST_NEVER_RENDER"},
            "private": {"notes": "PRIVATE_CANDIDATE_NOTE"},
        },
        "unknown_fields": unknown_fields or [],
        "contradictions": [],
        "pending_facts": [],
    }


def run_discover_cli(intake_path: Path, *arguments: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(DISCOVER_SCRIPT),
            "--candidate-intake",
            str(intake_path),
            *arguments,
        ],
        cwd=intake_path.parent,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def assert_interactive_path_rejected_before_prompt(
    completed: subprocess.CompletedProcess[str], *sensitive_paths: Path
) -> None:
    rendered = completed.stdout + completed.stderr
    assert completed.returncode == 2
    assert completed.stderr.strip()
    assert "Candidate-approved answer" not in rendered
    assert "Privacy-safe structured candidate review" not in rendered
    assert "confirm activation" not in rendered
    for path in sensitive_paths:
        assert str(path) not in rendered
        assert path.parent.name not in rendered


def test_is_link_like_rejects_callable_junction(monkeypatch: pytest.MonkeyPatch):
    discover = load_discover_module()
    monkeypatch.setattr(Path, "is_junction", lambda _path: True, raising=False)
    regular_file_status = SimpleNamespace(st_mode=stat.S_IFREG)

    assert discover._is_link_like(Path("candidate_intake.json"), regular_file_status)


def test_is_link_like_rejects_windows_reparse_point(monkeypatch: pytest.MonkeyPatch):
    discover = load_discover_module()
    reparse_flag = 0x0400
    monkeypatch.setattr(Path, "is_junction", lambda _path: False, raising=False)
    monkeypatch.setattr(
        discover.stat,
        "FILE_ATTRIBUTE_REPARSE_POINT",
        reparse_flag,
        raising=False,
    )
    reparse_status = SimpleNamespace(
        st_mode=stat.S_IFREG,
        st_file_attributes=reparse_flag,
    )

    assert discover._is_link_like(Path("candidate_intake.json"), reparse_status)


def test_interactive_onboarding_uses_injected_private_root_for_direct_test(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    discover = load_discover_module()
    trusted_root = tmp_path / "trusted-private"
    intake_path = trusted_root / "candidate_intake.json"
    write_onboarding_draft(intake_path, ["candidate_profile"])
    answers = iter(("Backend profile", "yes"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    exit_code = discover.main(
        ["--candidate-intake", str(intake_path), "--interactive-onboarding"],
        _private_root=trusted_root,
    )

    assert exit_code == 0, capsys.readouterr().err
    persisted = json.loads(intake_path.read_text(encoding="utf-8"))
    assert persisted["state"] == "active"
    assert persisted["activated_by"] == "user"
    assert persisted["approved_facts"]["candidate_profile"] == "Backend profile"
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == [intake_path]


def test_interactive_onboarding_rejects_target_outside_injected_private_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    discover = load_discover_module()
    trusted_root = tmp_path / "trusted-private"
    trusted_root.mkdir()
    outside_path = tmp_path / "outside" / "candidate_intake.json"
    original = write_onboarding_draft(outside_path, ["candidate_profile"])

    def fail_if_prompted(_prompt: str) -> str:
        pytest.fail("path validation must reject before interactive prompting")

    monkeypatch.setattr("builtins.input", fail_if_prompted)

    exit_code = discover.main(
        ["--candidate-intake", str(outside_path), "--interactive-onboarding"],
        _private_root=trusted_root,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert outside_path.read_bytes() == original
    assert not any(trusted_root.iterdir())
    assert str(outside_path) not in captured.out + captured.err


def test_interactive_onboarding_rejects_symlink_injected_private_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    discover = load_discover_module()
    real_root = tmp_path / "real-private"
    real_root.mkdir()
    linked_root = tmp_path / "trusted-private"
    try:
        linked_root.symlink_to(real_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    intake_path = linked_root / "candidate_intake.json"

    def fail_if_prompted(_prompt: str) -> str:
        pytest.fail("trusted-root validation must reject before interactive prompting")

    monkeypatch.setattr("builtins.input", fail_if_prompted)

    exit_code = discover.main(
        ["--candidate-intake", str(intake_path), "--interactive-onboarding"],
        _private_root=linked_root,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert linked_root.is_symlink()
    assert not any(real_root.iterdir())
    assert str(intake_path) not in captured.out + captured.err


def test_interactive_onboarding_rejects_non_regular_target_under_injected_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    discover = load_discover_module()
    trusted_root = tmp_path / "trusted-private"
    intake_path = trusted_root / "candidate_intake.json"
    intake_path.mkdir(parents=True)

    def fail_if_prompted(_prompt: str) -> str:
        pytest.fail("target validation must reject before interactive prompting")

    monkeypatch.setattr("builtins.input", fail_if_prompted)

    exit_code = discover.main(
        ["--candidate-intake", str(intake_path), "--interactive-onboarding"],
        _private_root=trusted_root,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert intake_path.is_dir()
    assert not any(intake_path.iterdir())
    assert str(intake_path) not in captured.out + captured.err


def test_injected_private_root_does_not_restrict_read_only_intake_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    discover = load_discover_module()
    trusted_root = tmp_path / "trusted-private"
    trusted_root.mkdir()
    outside_path = tmp_path / "outside" / "candidate_intake.json"
    original = write_onboarding_draft(outside_path, ["candidate_profile"])

    exit_code = discover.main(
        [
            "--candidate-intake",
            str(outside_path),
            "--show-intake-questions",
            "--onboarding-format",
            "json",
        ],
        _private_root=trusted_root,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["status"] == "blocked"
    assert payload["question_count"] == 1
    assert outside_path.read_bytes() == original
    assert not any(trusted_root.iterdir())


def test_interactive_onboarding_cli_rejects_absolute_outside_path_before_prompt_or_mutation(
    tmp_path: Path,
):
    intake_path = tmp_path / "absolute-outside" / "candidate_intake.json"
    original = write_onboarding_draft(intake_path, ["candidate_profile"])
    original_entries = sorted(path.name for path in intake_path.parent.iterdir())

    completed = run_discover_cli(
        intake_path,
        "--interactive-onboarding",
        stdin="Backend profile\nyes\n",
    )

    assert_interactive_path_rejected_before_prompt(completed, intake_path)
    assert intake_path.read_bytes() == original
    assert sorted(path.name for path in intake_path.parent.iterdir()) == original_entries


@pytest.mark.parametrize("path_style", ("sibling-prefix", "traversal"))
def test_interactive_onboarding_cli_rejects_private_root_lexical_escapes(
    private_test_dir: Path,
    private_sibling_test_dir: Path,
    path_style: str,
):
    escaped_intake = private_sibling_test_dir / "candidate_intake.json"
    original = write_onboarding_draft(escaped_intake, ["candidate_profile"])
    supplied_path = escaped_intake
    if path_style == "traversal":
        supplied_path = (
            private_test_dir
            / ".."
            / ".."
            / private_sibling_test_dir.name
            / escaped_intake.name
        )

    completed = run_discover_cli(
        supplied_path,
        "--interactive-onboarding",
        stdin="Backend profile\nyes\n",
    )

    assert_interactive_path_rejected_before_prompt(
        completed, supplied_path, escaped_intake
    )
    assert escaped_intake.read_bytes() == original
    assert [path for path in private_test_dir.iterdir()] == []
    assert [path.name for path in private_sibling_test_dir.iterdir()] == [
        escaped_intake.name
    ]


def test_interactive_onboarding_cli_rejects_symlink_directory_escape_without_touching_target(
    private_test_dir: Path,
    tmp_path: Path,
):
    external_intake = tmp_path / "external-directory" / "candidate_intake.json"
    original = write_onboarding_draft(external_intake, ["candidate_profile"])
    linked_directory = private_test_dir / "linked-directory"
    try:
        linked_directory.symlink_to(external_intake.parent, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    supplied_path = linked_directory / external_intake.name
    completed = run_discover_cli(
        supplied_path,
        "--interactive-onboarding",
        stdin="Backend profile\nyes\n",
    )

    assert_interactive_path_rejected_before_prompt(
        completed, supplied_path, external_intake
    )
    assert linked_directory.is_symlink()
    assert external_intake.read_bytes() == original
    assert [path.name for path in external_intake.parent.iterdir()] == [
        external_intake.name
    ]


def test_interactive_onboarding_cli_rejects_symlink_file_escape_without_touching_target(
    private_test_dir: Path,
    tmp_path: Path,
):
    external_intake = tmp_path / "external-file" / "candidate_intake.json"
    original = write_onboarding_draft(external_intake, ["candidate_profile"])
    linked_intake = private_test_dir / "candidate_intake.json"
    try:
        linked_intake.symlink_to(external_intake)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    completed = run_discover_cli(
        linked_intake,
        "--interactive-onboarding",
        stdin="Backend profile\nyes\n",
    )

    assert_interactive_path_rejected_before_prompt(
        completed, linked_intake, external_intake
    )
    assert linked_intake.is_symlink()
    assert external_intake.read_bytes() == original
    assert [path.name for path in external_intake.parent.iterdir()] == [
        external_intake.name
    ]


def test_interactive_onboarding_cli_activates_after_all_answers_and_confirmation(
    private_test_dir: Path,
):
    intake_path = private_test_dir / "candidate_intake.json"
    write_onboarding_draft(intake_path, ["candidate_profile", "education"])

    completed = run_discover_cli(
        intake_path,
        "--interactive-onboarding",
        stdin="Backend profile\nExample University\nyes\n",
    )

    assert completed.returncode == 0, completed.stderr
    assert "candidate_profile" in completed.stdout
    assert "education" in completed.stdout
    persisted = json.loads(intake_path.read_text(encoding="utf-8"))
    assert persisted["state"] == "active"
    assert persisted["activated_by"] == "user"
    assert persisted["unknown_fields"] == []
    assert persisted["approved_facts"]["candidate_profile"] == "Backend profile"
    assert persisted["approved_facts"]["education"] == "Example University"
    assert len([path for path in private_test_dir.rglob("*") if path.is_file()]) == 1


def test_interactive_onboarding_cli_keeps_blank_answer_unresolved_and_does_not_persist(
    private_test_dir: Path,
):
    intake_path = private_test_dir / "candidate_intake.json"
    original = write_onboarding_draft(intake_path, ["candidate_profile"])

    completed = run_discover_cli(intake_path, "--interactive-onboarding", stdin="\n")

    assert completed.returncode != 0
    assert "candidate_profile" in completed.stdout
    assert intake_path.read_bytes() == original
    assert json.loads(intake_path.read_text(encoding="utf-8"))["state"] == "draft"


@pytest.mark.parametrize("terminal_answer", ("unknown", "uncertain", "ambiguous"))
def test_interactive_onboarding_terminal_uncertainty_stays_unresolved_and_unpersisted(
    private_test_dir: Path, terminal_answer: str
):
    intake_path = private_test_dir / "candidate_intake.json"
    original = write_onboarding_draft(intake_path, ["candidate_profile"])

    completed = run_discover_cli(
        intake_path,
        "--interactive-onboarding",
        stdin=f"{terminal_answer}\nyes\n",
    )

    assert completed.returncode == 2
    assert intake_path.read_bytes() == original
    persisted = json.loads(intake_path.read_text(encoding="utf-8"))
    assert persisted["state"] == "draft"
    assert persisted["unknown_fields"] == ["candidate_profile"]
    assert "candidate_profile" not in persisted["approved_facts"]


def test_interactive_onboarding_cli_declined_confirmation_does_not_overwrite_draft(
    private_test_dir: Path,
):
    intake_path = private_test_dir / "candidate_intake.json"
    original = write_onboarding_draft(intake_path, ["candidate_profile"])

    completed = run_discover_cli(
        intake_path,
        "--interactive-onboarding",
        stdin="Backend profile\nno\n",
    )

    assert completed.returncode != 0
    assert intake_path.read_bytes() == original
    assert "Backend profile" not in completed.stderr
    assert json.loads(intake_path.read_text(encoding="utf-8"))["state"] == "draft"


def test_public_question_modes_remain_backward_compatible_and_privacy_safe(tmp_path: Path):
    intake_path = tmp_path / "private" / "candidate_intake.json"
    original = write_onboarding_draft(intake_path, ["private.candidate.secret"])

    text_mode = run_discover_cli(intake_path, "--show-intake-questions")
    assert text_mode.returncode == 2
    assert "review reference 'review-unknown-1'" in text_mode.stdout
    assert "private.candidate.secret" not in text_mode.stdout
    assert intake_path.read_bytes() == original

    json_mode = run_discover_cli(
        intake_path,
        "--show-intake-questions",
        "--onboarding-format",
        "json",
    )
    assert json_mode.returncode == 2
    payload = json.loads(json_mode.stdout)
    assert payload["status"] == "blocked"
    assert payload["question_count"] == 1
    assert "private.candidate.secret" not in json_mode.stdout
    assert intake_path.read_bytes() == original


def test_interactive_onboarding_cli_absent_target_uses_redacted_starter(
    private_test_dir: Path,
):
    intake_path = private_test_dir / "candidate_intake.json"
    starter_path = PROJECT_ROOT / "jobapply_agent" / "private.example" / "candidate_intake.json"
    starter = json.loads(starter_path.read_text(encoding="utf-8"))
    unresolved_count = len(starter["unknown_fields"]) + len(starter["pending_facts"])

    completed = run_discover_cli(
        intake_path,
        "--interactive-onboarding",
        stdin="\n" * unresolved_count,
    )

    assert completed.returncode == 2
    assert "identity.contact" in completed.stdout
    assert "candidate_profile" in completed.stdout
    assert "resume-primary" not in completed.stdout
    assert "Interactive onboarding ended before confirmation" in completed.stderr
    assert not intake_path.exists()
    assert not [path for path in private_test_dir.rglob("*") if path.is_file()]


def test_interactive_onboarding_cli_activates_absent_redacted_starter_with_skill_arrays(
    private_test_dir: Path,
):
    intake_path = private_test_dir / "candidate_intake.json"
    starter = json.loads(
        (PROJECT_ROOT / "jobapply_agent" / "private.example" / "candidate_intake.json").read_text(
            encoding="utf-8"
        )
    )
    answers = {
        "skills.professional": ["Python", "FastAPI"],
        "skills.personal_open_source": ["OpenAI"],
        "skills.learning_or_exposure": ["Docker"],
    }
    answer_lines = [
        json.dumps(answers[field]) if field in answers else f"candidate-confirmed {field}"
        for field in starter["unknown_fields"]
    ]
    answer_lines.extend(
        f"candidate-confirmed {item['field']}" for item in starter["pending_facts"]
    )

    completed = run_discover_cli(
        intake_path,
        "--interactive-onboarding",
        stdin="\n".join([*answer_lines, "yes", ""]),
    )

    assert completed.returncode == 0, completed.stderr
    persisted = json.loads(intake_path.read_text(encoding="utf-8"))
    assert persisted["state"] == "active"
    assert persisted["activated_by"] == "user"
    assert persisted["unknown_fields"] == []
    assert persisted["pending_facts"] == []
    assert persisted["approved_facts"]["skills.professional"] == ["Python", "FastAPI"]
    assert persisted["approved_facts"]["skills.personal_open_source"] == ["OpenAI"]
    assert persisted["approved_facts"]["skills.learning_or_exposure"] == ["Docker"]


def test_missing_intake_question_mode_returns_safe_redacted_question_bundle(tmp_path: Path):
    intake_path = tmp_path / "private" / "candidate_intake.json"
    intake_path.parent.mkdir()

    completed = run_discover_cli(
        intake_path,
        "--show-intake-questions",
        "--onboarding-format",
        "json",
    )

    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["status"] == "blocked"
    assert payload["requires_user_confirmation"] is True
    assert payload["question_count"] > 0
    assert "identity.contact" in payload["pending_verification_batch"]["unknown_fields"]
    rendered = completed.stdout + completed.stderr
    assert "resume-primary" not in rendered
    assert "documents/resume.pdf" not in rendered
    assert not intake_path.exists()


def test_interactive_onboarding_invalid_json_skill_answer_fails_without_writing(
    private_test_dir: Path,
):
    intake_path = private_test_dir / "candidate_intake.json"
    starter = json.loads(
        (PROJECT_ROOT / "jobapply_agent" / "private.example" / "candidate_intake.json").read_text(
            encoding="utf-8"
        )
    )
    answer_lines = [
        "[not valid JSON" if field == "skills.professional" else "candidate-confirmed"
        for field in starter["unknown_fields"]
    ]
    answer_lines.extend("candidate-confirmed" for _ in starter["pending_facts"])

    completed = run_discover_cli(
        intake_path,
        "--interactive-onboarding",
        stdin="\n".join([*answer_lines, "yes", ""]),
    )

    assert completed.returncode == 2
    assert "not valid JSON" not in completed.stdout + completed.stderr
    assert not intake_path.exists()


def test_noninteractive_missing_intake_still_fails_without_creating_a_target(tmp_path: Path):
    intake_path = tmp_path / "private" / "candidate_intake.json"
    intake_path.parent.mkdir()

    completed = run_discover_cli(intake_path)

    assert completed.returncode == 2
    assert "candidate intake is missing" in completed.stderr.casefold()
    assert not intake_path.exists()


def test_interactive_onboarding_cli_leaves_existing_active_intake_unchanged(
    private_test_dir: Path,
):
    intake_path = private_test_dir / "candidate_intake.json"
    write_active_candidate_intake(intake_path)
    original = intake_path.read_bytes()

    completed = run_discover_cli(intake_path, "--interactive-onboarding")

    assert completed.returncode == 0, completed.stderr
    assert "already active" in completed.stdout
    assert intake_path.read_bytes() == original
    assert len([path for path in private_test_dir.rglob("*") if path.is_file()]) == 1


@pytest.mark.parametrize("raised_exception", [KeyboardInterrupt, OSError])
def test_active_intake_persistence_cleans_temp_files_on_failure(
    private_test_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    raised_exception: type[BaseException],
):
    discover = load_discover_module()
    intake_path = private_test_dir / "candidate_intake.json"
    original = write_onboarding_draft(intake_path, [])

    def fail_during_flush(_file_descriptor: int) -> None:
        raise raised_exception()

    monkeypatch.setattr(discover.os, "fsync", fail_during_flush)

    with pytest.raises(raised_exception):
        discover._persist_active_candidate_intake(intake_path, {"state": "active"})

    assert intake_path.read_bytes() == original
    assert [path for path in intake_path.parent.iterdir() if path != intake_path] == []


def test_interactive_onboarding_cli_fails_closed_for_invalid_active_target(
    private_test_dir: Path,
):
    intake_path = private_test_dir / "candidate_intake.json"
    write_active_candidate_intake(intake_path)
    invalid_active = json.loads(intake_path.read_text(encoding="utf-8"))
    invalid_active["revision_hash"] = "0" * 64
    intake_path.write_text(json.dumps(invalid_active), encoding="utf-8")
    original = intake_path.read_bytes()

    completed = run_discover_cli(intake_path, "--interactive-onboarding")

    assert completed.returncode != 0
    assert "Candidate-approved answer" not in completed.stdout
    assert "Type yes to confirm activation" not in completed.stdout
    assert intake_path.read_bytes() == original


def test_interactive_confirmation_summarizes_safe_fields_with_review_values(
    private_test_dir: Path,
):
    intake_path = private_test_dir / "candidate_intake.json"
    write_onboarding_draft(intake_path, ["candidate_profile", "education"])

    completed = run_discover_cli(
        intake_path,
        "--interactive-onboarding",
        stdin="Backend profile\nExample University\nyes\n",
    )

    assert completed.returncode == 0, completed.stderr
    confirmation_lines = [
        line
        for line in completed.stdout.splitlines()
        if "candidate_profile" in line and "education" in line
    ]
    assert len(confirmation_lines) == 1
    confirmation = confirmation_lines[0]
    assert "candidate_profile" in confirmation
    assert "education" in confirmation
    assert "2" in confirmation
    assert "Backend profile" not in completed.stdout + completed.stderr
    assert "Example University" in completed.stdout


def test_interactive_onboarding_cli_accepts_whitespace_around_literal_yes(
    private_test_dir: Path,
):
    intake_path = private_test_dir / "candidate_intake.json"
    write_onboarding_draft(intake_path, ["candidate_profile"])

    completed = run_discover_cli(
        intake_path,
        "--interactive-onboarding",
        stdin="Backend profile\n  yes  \n",
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(intake_path.read_text(encoding="utf-8"))["state"] == "active"


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


def test_candidate_facing_onboarding_summary_omits_raw_document_text(
    tmp_path: Path, capsys
):
    discover = load_discover_module()
    from jobapply_agent.intake import validate_candidate_intake

    raw_document_text = "RAW_DOCUMENT_TEXT_MUST_NEVER_BE_RENDERED_TO_CANDIDATE"
    draft = validate_candidate_intake(
        {
            "schema_version": 1,
            "documents": [
                {
                    "document_id": "resume-primary",
                    "path": "resume.pdf",
                    "sha256": "a" * 64,
                    "media_type": "application/pdf",
                    "review_state": "untrusted",
                }
            ],
            "approved_facts": {"resume.extracted_text": raw_document_text},
            "unknown_fields": ["work_authorization"],
            "contradictions": [
                {
                    "field": "experience.current_title",
                    "status": "VERIFY",
                    "values": [raw_document_text, "Python Developer"],
                    "evidence_ids": ["resume-primary", "candidate-note-1"],
                }
            ],
            "pending_facts": [
                {
                    "field": "location_preferences",
                    "status": "VERIFY",
                    "question": f"Document excerpt: {raw_document_text}",
                    "reason": f"Source text contains {raw_document_text}",
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

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 2
    assert payload["questions"]
    assert raw_document_text not in captured.out + captured.err


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


def test_discover_intake_questions_mode_requires_confirmation_for_resolved_draft(
    tmp_path: Path, capsys
):
    discover = load_discover_module()
    from jobapply_agent.intake import validate_candidate_intake

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
    intake_path.write_text(json.dumps({"state": "draft", **draft}), encoding="utf-8")

    exit_code = discover.main(
        [
            "--candidate-intake",
            str(intake_path),
            "--show-intake-questions",
            "--onboarding-format",
            "json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["status"] == "blocked"
    assert payload["requires_user_confirmation"] is True
    assert payload["candidate_review"]["status"] == "ready-for-confirmation"
    assert payload["candidate_review"]["facts"] == [
        {
            "field": "experience.total_years",
            "source": "candidate-provided-structured-intake",
            "uncertainty": "requires-candidate-confirmation",
            "value": 3,
        }
    ]


def test_candidate_review_renders_safe_values_and_active_provenance_labels(
    tmp_path: Path, capsys
):
    discover = load_discover_module()
    from jobapply_agent.intake import activate_candidate_profile, validate_candidate_intake

    document = {
        "document_id": "resume-primary",
        "path": "resume.pdf",
        "sha256": "a" * 64,
        "media_type": "application/pdf",
        "review_state": "untrusted",
    }
    draft = validate_candidate_intake(
        {
            "schema_version": 1,
            "documents": [document],
            "approved_facts": {
                "roles": {"include": ["Backend Developer"]},
                "resume.extracted_text": "RAW_DOCUMENT_TEXT_MUST_NOT_RENDER",
            },
            "unknown_fields": [],
            "contradictions": [],
            "pending_facts": [],
        }
    )
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(json.dumps(draft), encoding="utf-8")

    exit_code = discover.main(
        [
            "--candidate-intake",
            str(draft_path),
            "--show-intake-questions",
            "--onboarding-format",
            "json",
        ]
    )
    draft_payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert draft_payload["candidate_review"]["facts"] == [
        {
            "field": "roles.include",
            "source": "source-unattributed",
            "uncertainty": "requires-candidate-confirmation",
            "value": ["Backend Developer"],
        }
    ]
    assert "RAW_DOCUMENT_TEXT_MUST_NOT_RENDER" not in json.dumps(draft_payload)

    active_path = tmp_path / "active.json"
    active_path.write_text(
        json.dumps(activate_candidate_profile(draft, actor="user")), encoding="utf-8"
    )
    exit_code = discover.main(
        [
            "--candidate-intake",
            str(active_path),
            "--show-intake-questions",
            "--onboarding-format",
            "json",
        ]
    )
    active_payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert active_payload["candidate_review"]["status"] == "active"
    assert active_payload["candidate_review"]["facts"] == [
        {
            "field": "roles.include",
            "source": "candidate-approved",
            "uncertainty": "confirmed",
            "value": ["Backend Developer"],
        }
    ]


def test_candidate_review_renders_allowlisted_rich_facts_as_bounded_structured_values(
    tmp_path: Path, capsys
):
    discover = load_discover_module()
    from jobapply_agent.intake import validate_candidate_intake

    draft = validate_candidate_intake(rich_review_payload())
    intake_path = tmp_path / "draft.json"
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

    payload = json.loads(capsys.readouterr().out)
    facts = {
        item["field"]: item for item in payload["candidate_review"]["facts"]
    }
    expected_values = {
        "education": [
            {
                "degree": "B.Tech in Computer Science",
                "institution": "Example University",
                "year": 2020,
            }
        ],
        "experience.achievements_and_metrics": [
            {
                "achievement": "Reduced API latency",
                "metric": "35 percent",
            }
        ],
        "experience.roles_and_dates": [
            {
                "title": "Backend Engineer",
                "company": "Example Systems",
                "dates": "2022-2024",
            }
        ],
        "projects": [
            {
                "name": "Queue Planner",
                "summary": "Bounded candidate review queue",
                "technologies": "Python and SQLite",
            }
        ],
    }

    assert exit_code == 2
    assert payload["status"] == "blocked"
    assert payload["candidate_review"]["status"] == "ready-for-confirmation"
    assert set(facts) == set(expected_values)
    assert payload["candidate_review"]["fact_count"] == len(expected_values)
    for field, expected_value in expected_values.items():
        assert facts[field]["value"] == expected_value
        assert isinstance(facts[field]["value"], list)
        assert all(isinstance(item, dict) for item in facts[field]["value"])

    rendered = json.dumps(payload)
    for private_value in (
        "PRIVATE_DOCUMENT_ID",
        "/private/candidate/resume.pdf",
        "identity.contact",
        "PRIVATE_CONTACT@example.test",
        "resume.extracted_text",
        "RAW_RESUME_TEXT_MUST_NEVER_RENDER",
        "private.notes",
        "PRIVATE_CANDIDATE_NOTE",
    ):
        assert private_value not in rendered


def test_active_candidate_review_labels_allowlisted_rich_facts_candidate_approved(
    tmp_path: Path, capsys
):
    discover = load_discover_module()
    from jobapply_agent.intake import activate_candidate_profile, validate_candidate_intake

    draft = validate_candidate_intake(rich_review_payload())
    active = activate_candidate_profile(draft, actor="user")
    intake_path = tmp_path / "active.json"
    intake_path.write_text(json.dumps(active), encoding="utf-8")

    exit_code = discover.main(
        [
            "--candidate-intake",
            str(intake_path),
            "--show-intake-questions",
            "--onboarding-format",
            "json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    facts = payload["candidate_review"]["facts"]

    assert exit_code == 0
    assert payload["status"] == "ready"
    assert payload["candidate_review"]["status"] == "active"
    assert payload["candidate_review"]["required_before_activation"] is False
    assert {item["field"] for item in facts} == {
        "education",
        "experience.achievements_and_metrics",
        "experience.roles_and_dates",
        "projects",
    }
    assert all(item["source"] == "candidate-approved" for item in facts)
    assert all(item["uncertainty"] == "confirmed" for item in facts)
    assert "RAW_RESUME_TEXT_MUST_NEVER_RENDER" not in json.dumps(payload)
    assert "PRIVATE_CONTACT@example.test" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("state", "unknown_fields", "revision_hash", "review_status"),
    [
        ("draft", ["work_authorization"], None, "blocked"),
        ("active", [], "0" * 64, "ready-for-confirmation"),
    ],
    ids=["incomplete-draft", "invalid-active-revision"],
)
def test_candidate_review_keeps_incomplete_or_invalid_intake_blocked(
    tmp_path: Path,
    capsys,
    state: str,
    unknown_fields: list[str],
    revision_hash: str | None,
    review_status: str,
):
    discover = load_discover_module()
    from jobapply_agent.intake import activate_candidate_profile, validate_candidate_intake

    draft = validate_candidate_intake(
        rich_review_payload(unknown_fields=unknown_fields)
    )
    if state == "active":
        payload_to_write = activate_candidate_profile(draft, actor="user")
        payload_to_write["revision_hash"] = revision_hash
    else:
        payload_to_write = {"state": state, **draft}
    intake_path = tmp_path / f"{state}.json"
    intake_path.write_text(json.dumps(payload_to_write), encoding="utf-8")

    exit_code = discover.main(
        [
            "--candidate-intake",
            str(intake_path),
            "--show-intake-questions",
            "--onboarding-format",
            "json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["status"] == "blocked"
    assert payload["requires_user_confirmation"] is True
    assert payload["candidate_review"]["status"] == review_status


def test_candidate_review_never_trusts_an_unvalidated_active_state(
    tmp_path: Path, capsys
):
    discover = load_discover_module()
    from jobapply_agent.intake import validate_candidate_intake

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
    intake_path = tmp_path / "claimed-active.json"
    intake_path.write_text(json.dumps({"state": "active", **draft}), encoding="utf-8")

    exit_code = discover.main(
        [
            "--candidate-intake",
            str(intake_path),
            "--show-intake-questions",
            "--onboarding-format",
            "json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["status"] == "blocked"
    assert payload["requires_user_confirmation"] is True
    assert payload["candidate_review"]["status"] == "ready-for-confirmation"


def test_text_intake_questions_mode_blocks_resolved_inactive_draft(
    tmp_path: Path, capsys
):
    discover = load_discover_module()
    from jobapply_agent.intake import validate_candidate_intake

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
    intake_path = tmp_path / "resolved-draft.json"
    intake_path.write_text(json.dumps({"state": "draft", **draft}), encoding="utf-8")

    exit_code = discover.main(
        ["--candidate-intake", str(intake_path), "--show-intake-questions"]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "final candidate confirmation" in captured.out


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


def test_discover_missing_intake_question_mode_does_not_echo_private_intake_path(
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
    assert "Candidate onboarding questions (ask all at once)" in captured.out
    assert "identity.contact" in captured.out


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


def test_active_intake_queue_factory_requires_integrity_checked_candidate_capacity(tmp_path: Path):
    discover = load_discover_module()
    from jobapply_agent.intake import activate_candidate_profile, validate_candidate_intake
    from jobapply_agent.smart_queue import QueuePolicyError

    payload = {
        "schema_version": 1,
        "documents": [],
        "approved_facts": {"targets.smart_queue_capacity": 3},
        "unknown_fields": [],
        "contradictions": [],
        "pending_facts": [],
    }
    active_path = tmp_path / "private" / "candidate_intake.json"
    active_path.parent.mkdir()
    active_path.write_text(
        json.dumps(activate_candidate_profile(validate_candidate_intake(payload), actor="user")),
        encoding="utf-8",
    )

    queue = discover.smart_queue_for_active_intake(active_path, tmp_path / "queue.sqlite3")

    assert queue.target_size == 3
    with pytest.raises(QueuePolicyError, match="conflict|target_size|capacity"):
        discover.smart_queue_for_active_intake(
            active_path,
            tmp_path / "queue.sqlite3",
            target_size=5,
        )

    tampered = json.loads(active_path.read_text(encoding="utf-8"))
    tampered["approved_facts"]["targets.smart_queue_capacity"] = 5
    tampered_path = tmp_path / "private" / "tampered.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="revision|integrity|active"):
        discover.smart_queue_for_active_intake(tampered_path, tmp_path / "tampered.sqlite3")
    assert not (tmp_path / "tampered.sqlite3").exists()


def admission_fixture(
    private_test_dir: Path,
    *,
    row_count: int = 5,
) -> tuple[object, Path, Path, Path, Path, list[dict[str, object]]]:
    """Create a complete, current-revision export using synthetic listing facts."""

    discover = load_discover_module()
    from jobapply_agent.matcher import matcher_policy_revision
    from jobapply_agent.scheduler import candidate_profile_revision

    intake_path = private_test_dir / "candidate_intake.json"
    write_active_candidate_intake(intake_path)
    profile_revision = candidate_profile_revision(discover.active_candidate_profile(intake_path))
    policy_revision = matcher_policy_revision()
    rows = [
        {
            "schema_version": 2,
            "record_type": "recommended_job_for_human_review",
            "discovery_mode": "export_only",
            "application_actions": 0,
            "fingerprint": f"{index + 1:064x}",
            "profile_revision": profile_revision,
            "matcher_policy_revision": policy_revision,
            "run_id": "synthetic-admission-run",
            "discovered_at": "2026-09-03T00:00:00+00:00",
            "search_url": "https://www.indeed.com/jobs?q=python",
            "platform": "indeed",
            "title": "Python Backend Developer",
            "company": f"Synthetic Company {index + 1}",
            "url": f"https://www.indeed.com/viewjob?jk=admit{index + 1:04d}",
            "location": "Synthetic City",
            "work_mode": "hybrid",
            "posted_at": None,
            "score": 85,
            "decision": "recommended",
            "minimum_profile_fit_score": 85,
            "threshold_met": True,
            "reasons": ["verified professional Python evidence"],
            "gaps": [],
            "evidence_explanations": ["professional backend evidence"],
            "score_explanation": "Synthetic deterministic score explanation.",
            "human_action_required": "Candidate reviews the listing manually.",
        }
        for index in range(row_count)
    ]
    export_path = private_test_dir / "discovery.jsonl"
    write_admission_export(export_path, rows)
    return (
        discover,
        intake_path,
        export_path,
        private_test_dir / "smart-queue.sqlite3",
        private_test_dir / "candidate-memory.sqlite3",
        rows,
    )


def write_admission_export(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def admission_database_counts(database_path: Path) -> tuple[int, int]:
    """Return durable recommendation/event totals without exposing row contents."""

    if not database_path.exists():
        return (0, 0)
    with sqlite3.connect(database_path) as connection:
        return tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("smart_queue_jobs", "smart_queue_events")
        )


def run_admit_queue_cli(
    intake_path: Path,
    export_path: Path,
    queue_path: Path,
    memory_path: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(DISCOVER_SCRIPT),
            "admit-queue",
            "--candidate-intake",
            str(intake_path),
            "--discovery-export",
            str(export_path),
            "--queue-db",
            str(queue_path),
            "--memory-db",
            str(memory_path),
        ],
        cwd=PROJECT_ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_admit_queue_accepts_a_valid_current_five_row_batch(private_test_dir: Path):
    discover, intake_path, export_path, queue_path, memory_path, _rows = admission_fixture(private_test_dir)

    status = discover.admit_current_recommendations_for_active_queue(
        intake_path, export_path, queue_path, memory_path
    )

    assert status == discover.AdmissionStatus(validated_count=5, suppressed_count=0, admitted_count=5)
    assert admission_database_counts(queue_path) == (5, 5)


@pytest.mark.parametrize(
    "invalid_row",
    (
        pytest.param(lambda rows: {**rows[-1], "schema_version": 1}, id="invalid-contract"),
        pytest.param(lambda rows: {**rows[-1], "profile_revision": "0" * 64}, id="stale-profile"),
        pytest.param(lambda rows: {**rows[-1], "matcher_policy_revision": "0" * 64}, id="stale-policy"),
        pytest.param(lambda rows: {**rows[-1], "platform": "unsupported"}, id="unsupported-platform"),
        pytest.param(
            lambda rows: {**rows[-1], "url": rows[-1]["url"] + "&unexpected=1"},
            id="noncanonical-url",
        ),
        pytest.param(lambda rows: {**rows[-1], "is_test_fixture": True}, id="fixture-flag"),
        pytest.param(lambda rows: {**rows[-1], "application_actions": 1}, id="application-actions"),
        pytest.param(lambda rows: {**rows[-1], "decision": "review"}, id="wrong-decision"),
        pytest.param(lambda rows: {**rows[-1], "threshold_met": False}, id="threshold-mismatch"),
        pytest.param(lambda rows: {**rows[-1], "score": 84}, id="below-score-threshold"),
        pytest.param(lambda rows: {**rows[-1], "evidence_explanations": []}, id="missing-evidence"),
        pytest.param(lambda rows: {**rows[-1], "fingerprint": rows[0]["fingerprint"]}, id="duplicate-fingerprint"),
        pytest.param(lambda rows: {**rows[-1], "url": rows[0]["url"]}, id="duplicate-canonical-url"),
    ),
)
def test_admit_queue_rejects_one_invalid_row_without_durable_mutation(
    private_test_dir: Path,
    invalid_row,
):
    discover, intake_path, export_path, queue_path, memory_path, rows = admission_fixture(private_test_dir)
    rows[-1] = invalid_row(rows)
    write_admission_export(export_path, rows)

    with pytest.raises(discover.AdmissionError, match="^queue admission failed$"):
        discover.admit_current_recommendations_for_active_queue(
            intake_path, export_path, queue_path, memory_path
        )

    assert admission_database_counts(queue_path) == (0, 0)
    assert not memory_path.exists()


def test_admit_queue_cli_keeps_success_and_failure_output_redacted_and_count_only(
    private_test_dir: Path,
):
    _discover, intake_path, export_path, queue_path, memory_path, rows = admission_fixture(private_test_dir)

    success = run_admit_queue_cli(intake_path, export_path, queue_path, memory_path)

    assert success.returncode == 0
    assert success.stderr == ""
    assert json.loads(success.stdout) == {
        "admitted_count": 5,
        "suppressed_count": 0,
        "validated_count": 5,
    }
    for secret in (str(intake_path), str(export_path), rows[0]["url"], rows[0]["fingerprint"], rows[0]["title"]):
        assert secret not in success.stdout + success.stderr

    rows[-1]["application_actions"] = 1
    write_admission_export(export_path, rows)
    failed = run_admit_queue_cli(intake_path, export_path, queue_path, memory_path)

    assert failed.returncode == 2
    assert failed.stdout == ""
    assert json.loads(failed.stderr) == {"error": "admission_failed"}
    for secret in (str(intake_path), str(export_path), rows[0]["url"], rows[0]["fingerprint"], rows[0]["title"]):
        assert secret not in failed.stdout + failed.stderr


def test_documented_admission_path_with_staged_export_end_to_end(
    private_test_dir: Path, tmp_path: Path
):
    """Prove the documented producer-stage-admit path works against temp DBs.

    Discovery's default ``--output-dir`` writes ``recommended_jobs.jsonl``;
    the documented handoff stages it to ``jobapply_agent/private/discovery.jsonl``
    and runs the exact documented ``admit-queue`` flag spelling as a subprocess.
    """
    discover, intake_path, staged_export_path, queue_path, memory_path, rows = admission_fixture(
        private_test_dir
    )
    assert staged_export_path.name == "discovery.jsonl"

    producer_export = tmp_path / "recommended_jobs.jsonl"
    write_admission_export(producer_export, rows)
    staged_export_path.unlink()
    shutil.copy(producer_export, staged_export_path)

    completed = run_admit_queue_cli(intake_path, staged_export_path, queue_path, memory_path)

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "admitted_count": 5,
        "suppressed_count": 0,
        "validated_count": 5,
    }
    assert admission_database_counts(queue_path) == (5, 5)


def test_admit_queue_rejects_nonprivate_paths_symlinks_and_database_collisions(
    private_test_dir: Path,
    tmp_path: Path,
):
    discover, intake_path, export_path, queue_path, memory_path, _rows = admission_fixture(private_test_dir)
    outside_queue = tmp_path / "outside-queue.sqlite3"
    outside_memory = tmp_path / "outside-memory.sqlite3"

    with pytest.raises(discover.AdmissionError, match="^queue admission failed$"):
        discover.admit_current_recommendations_for_active_queue(
            intake_path, export_path, outside_queue, memory_path
        )
    with pytest.raises(discover.AdmissionError, match="^queue admission failed$"):
        discover.admit_current_recommendations_for_active_queue(
            intake_path, export_path, queue_path, outside_memory
        )
    with pytest.raises(discover.AdmissionError, match="^queue admission failed$"):
        discover.admit_current_recommendations_for_active_queue(
            intake_path, export_path, queue_path, queue_path
        )

    linked_queue = private_test_dir / "linked-queue.sqlite3"
    try:
        linked_queue.symlink_to(outside_queue)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")
    with pytest.raises(discover.AdmissionError, match="^queue admission failed$"):
        discover.admit_current_recommendations_for_active_queue(
            intake_path, export_path, linked_queue, memory_path
        )

    assert not outside_queue.exists()
    assert not outside_memory.exists()
    assert not memory_path.exists()


def test_admit_queue_identical_rerun_is_idempotent(private_test_dir: Path):
    discover, intake_path, export_path, queue_path, memory_path, _rows = admission_fixture(private_test_dir)

    first = discover.admit_current_recommendations_for_active_queue(
        intake_path, export_path, queue_path, memory_path
    )
    before_rerun = admission_database_counts(queue_path)
    second = discover.admit_current_recommendations_for_active_queue(
        intake_path, export_path, queue_path, memory_path
    )

    assert first == discover.AdmissionStatus(validated_count=5, suppressed_count=0, admitted_count=5)
    assert second == discover.AdmissionStatus(validated_count=5, suppressed_count=0, admitted_count=0)
    assert admission_database_counts(queue_path) == before_rerun == (5, 5)
def _memory_scope_queue_ids(memory_path: Path) -> list[str]:
    with sqlite3.connect(memory_path) as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT queue_id FROM candidate_memory_queue_scope ORDER BY scope_id"
            ).fetchall()
        ]


def test_admit_queue_rolls_back_newly_bound_revisions_when_suppression_fails(
    private_test_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    discover, intake_path, export_path, queue_path, memory_path, _rows = admission_fixture(
        private_test_dir
    )
    from jobapply_agent.candidate_memory import CandidateMemory, CandidateMemoryPolicyError
    from jobapply_agent.smart_queue import SmartJobQueue

    def failing_suppression(self, candidates, *, queue):
        raise CandidateMemoryPolicyError("synthetic suppression failure after binding")

    with monkeypatch.context() as patch_scope:
        patch_scope.setattr(CandidateMemory, "filter_unsuppressed_candidates", failing_suppression)
        with pytest.raises(discover.AdmissionError, match="^queue admission failed$"):
            discover.admit_current_recommendations_for_active_queue(
                intake_path, export_path, queue_path, memory_path
            )

    assert SmartJobQueue(queue_path).active_revisions == (None, None)
    assert admission_database_counts(queue_path) == (0, 0)
    if memory_path.exists():
        assert _memory_scope_queue_ids(memory_path) == []

    status = discover.admit_current_recommendations_for_active_queue(
        intake_path, export_path, queue_path, memory_path
    )

    assert status == discover.AdmissionStatus(validated_count=5, suppressed_count=0, admitted_count=5)
    assert admission_database_counts(queue_path) == (5, 5)
    assert SmartJobQueue(queue_path).active_revisions != (None, None)
    assert _memory_scope_queue_ids(memory_path) == [SmartJobQueue(queue_path).queue_id]


def test_admit_queue_rolls_back_newly_bound_state_when_queue_insertion_fails(
    private_test_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    discover, intake_path, export_path, queue_path, memory_path, _rows = admission_fixture(
        private_test_dir
    )
    from jobapply_agent.smart_queue import QueueStorageError, SmartJobQueue

    def failing_insertion(self, candidates):
        raise QueueStorageError("smart queue storage operation failed")

    monkeypatch.setattr(SmartJobQueue, "add_recommendations", failing_insertion)

    with pytest.raises(discover.AdmissionError, match="^queue admission failed$"):
        discover.admit_current_recommendations_for_active_queue(
            intake_path, export_path, queue_path, memory_path
        )

    assert SmartJobQueue(queue_path).active_revisions == (None, None)
    assert admission_database_counts(queue_path) == (0, 0)
    assert _memory_scope_queue_ids(memory_path) == []


def test_admit_queue_failure_after_prior_binding_keeps_preexisting_pair_and_scope(
    private_test_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    discover, intake_path, export_path, queue_path, memory_path, _rows = admission_fixture(
        private_test_dir
    )
    from jobapply_agent.smart_queue import QueueStorageError, SmartJobQueue

    first = discover.admit_current_recommendations_for_active_queue(
        intake_path, export_path, queue_path, memory_path
    )
    assert first == discover.AdmissionStatus(validated_count=5, suppressed_count=0, admitted_count=5)
    bound_pair = SmartJobQueue(queue_path).active_revisions
    assert bound_pair != (None, None)
    scope_before = _memory_scope_queue_ids(memory_path)
    assert scope_before != []

    def failing_insertion(self, candidates):
        raise QueueStorageError("smart queue storage operation failed")

    monkeypatch.setattr(SmartJobQueue, "add_recommendations", failing_insertion)

    with pytest.raises(discover.AdmissionError, match="^queue admission failed$"):
        discover.admit_current_recommendations_for_active_queue(
            intake_path, export_path, queue_path, memory_path
        )

    assert SmartJobQueue(queue_path).active_revisions == bound_pair
    assert admission_database_counts(queue_path) == (5, 5)
    assert _memory_scope_queue_ids(memory_path) == scope_before


@pytest.mark.parametrize("failure_stage", ("suppression", "insertion"))
def test_changed_revision_admission_failure_restores_prior_pair_history_scope_and_jobs(
    private_test_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
):
    """A failed advance compensates append-only; a later valid advance succeeds."""

    discover, intake_path, export_path, queue_path, memory_path, rows = admission_fixture(
        private_test_dir
    )
    from jobapply_agent.candidate_memory import CandidateMemory, CandidateMemoryPolicyError
    from jobapply_agent.intake import activate_candidate_profile, validate_candidate_intake
    from jobapply_agent.smart_queue import QueueStorageError, SmartJobQueue

    first = discover.admit_current_recommendations_for_active_queue(
        intake_path, export_path, queue_path, memory_path
    )
    assert first == discover.AdmissionStatus(validated_count=5, suppressed_count=0, admitted_count=5)

    prior_queue = SmartJobQueue(queue_path)
    prior_pair = prior_queue.active_revisions
    prior_history = prior_queue.revision_history()
    prior_jobs = admission_database_counts(queue_path)
    prior_scope = _memory_scope_queue_ids(memory_path)
    assert prior_pair != (None, None)
    assert prior_scope == [prior_queue.queue_id]

    active_intake = json.loads(intake_path.read_text(encoding="utf-8"))
    revised_draft = {
        key: active_intake[key]
        for key in (
            "schema_version",
            "documents",
            "approved_facts",
            "unknown_fields",
            "contradictions",
            "pending_facts",
        )
    }
    revised_draft["approved_facts"] = {
        **revised_draft["approved_facts"],
        "experience": {"total_years": 4},
    }
    intake_path.write_text(
        json.dumps(
            activate_candidate_profile(
                validate_candidate_intake(revised_draft),
                actor="user",
            )
        ),
        encoding="utf-8",
    )
    revised_profile = discover.active_candidate_profile(intake_path)
    revised_profile_revision = discover.candidate_profile_revision(revised_profile)
    revised_policy_revision = "b" * 64
    monkeypatch.setattr(discover, "matcher_policy_revision", lambda: revised_policy_revision)
    revised_rows = [
        {
            **row,
            "fingerprint": f"{100 + index:064x}",
            "profile_revision": revised_profile_revision,
            "matcher_policy_revision": revised_policy_revision,
            "url": f"https://www.indeed.com/viewjob?jk=advance{100 + index:04d}",
        }
        for index, row in enumerate(rows)
    ]
    write_admission_export(export_path, revised_rows)

    with monkeypatch.context() as failure_patch:
        if failure_stage == "suppression":
            def fail_suppression(self, candidates, *, queue):
                raise CandidateMemoryPolicyError("synthetic suppression failure after revision advance")

            failure_patch.setattr(CandidateMemory, "filter_unsuppressed_candidates", fail_suppression)
        else:
            def fail_insertion(self, candidates):
                raise QueueStorageError("synthetic insertion failure after revision advance")

            failure_patch.setattr(SmartJobQueue, "add_recommendations", fail_insertion)

        with pytest.raises(discover.AdmissionError, match="^queue admission failed$"):
            discover.admit_current_recommendations_for_active_queue(
                intake_path, export_path, queue_path, memory_path
            )

    restored = SmartJobQueue(queue_path)
    assert restored.active_revisions == prior_pair
    restored_history = restored.revision_history()
    assert restored_history[: len(prior_history)] == prior_history
    assert len(restored_history) == len(prior_history) + 2
    provisional_advance, compensation = restored_history[len(prior_history) :]
    assert provisional_advance.prior_profile_revision == prior_pair[0]
    assert provisional_advance.prior_matcher_policy_revision == prior_pair[1]
    assert provisional_advance.profile_revision == revised_profile_revision
    assert provisional_advance.matcher_policy_revision == revised_policy_revision
    assert provisional_advance.actor == "host"
    assert compensation.prior_profile_revision == revised_profile_revision
    assert compensation.prior_matcher_policy_revision == revised_policy_revision
    assert compensation.profile_revision == prior_pair[0]
    assert compensation.matcher_policy_revision == prior_pair[1]
    assert compensation.actor == "admission-rollback"
    assert compensation.event_id == provisional_advance.event_id + 1
    assert admission_database_counts(queue_path) == prior_jobs
    assert _memory_scope_queue_ids(memory_path) == prior_scope

    successful_advance = discover.admit_current_recommendations_for_active_queue(
        intake_path, export_path, queue_path, memory_path
    )

    assert successful_advance == discover.AdmissionStatus(
        validated_count=5,
        suppressed_count=0,
        admitted_count=5,
    )
    advanced = SmartJobQueue(queue_path)
    assert advanced.active_revisions == (revised_profile_revision, revised_policy_revision)
    advanced_history = advanced.revision_history()
    assert advanced_history[:-1] == restored_history
    assert advanced_history[-1].prior_profile_revision == prior_pair[0]
    assert advanced_history[-1].prior_matcher_policy_revision == prior_pair[1]
    assert advanced_history[-1].profile_revision == revised_profile_revision
    assert advanced_history[-1].matcher_policy_revision == revised_policy_revision
    assert advanced_history[-1].actor == "host"
    assert admission_database_counts(queue_path) == (10, 10)
    assert _memory_scope_queue_ids(memory_path) == prior_scope


def test_admit_queue_skips_blank_lines_like_scheduler(private_test_dir: Path):
    discover, intake_path, export_path, queue_path, memory_path, rows = admission_fixture(
        private_test_dir
    )
    export_path.write_text(
        "\n".join(["", *[json.dumps(row, sort_keys=True) for row in rows], "", ""]),
        encoding="utf-8",
    )

    status = discover.admit_current_recommendations_for_active_queue(
        intake_path, export_path, queue_path, memory_path
    )

    assert status == discover.AdmissionStatus(validated_count=5, suppressed_count=0, admitted_count=5)
    assert admission_database_counts(queue_path) == (5, 5)


def test_admission_export_root_lstat_rejects_links(tmp_path: Path):
    discover = load_discover_module()

    assert discover._validate_admission_export_root(tmp_path / "missing-root") is None
    assert discover._validate_admission_export_root(tmp_path) == tmp_path.absolute()

    target = tmp_path / "target"
    target.mkdir()
    linked_dir = tmp_path / "linked-dir"
    linked_dir.symlink_to(target, target_is_directory=True)
    with pytest.raises(discover.AdmissionError, match="^queue admission failed$"):
        discover._validate_admission_export_root(linked_dir)

    regular_file = tmp_path / "regular-file"
    regular_file.write_text("x", encoding="utf-8")
    with pytest.raises(discover.AdmissionError, match="^queue admission failed$"):
        discover._validate_admission_export_root(regular_file)


def test_admit_queue_rejects_malformed_jsonl_export_without_durable_mutation(
    private_test_dir: Path,
):
    discover, intake_path, export_path, queue_path, memory_path, rows = admission_fixture(
        private_test_dir
    )
    lines = export_path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[1] = '{"record_type": "recommended_job_for_human_review"\n'
    export_path.write_text("".join(lines), encoding="utf-8")

    with pytest.raises(discover.AdmissionError, match="^queue admission failed$"):
        discover.admit_current_recommendations_for_active_queue(
            intake_path, export_path, queue_path, memory_path
        )

    assert admission_database_counts(queue_path) == (0, 0)
    assert not memory_path.exists()


def test_admit_queue_accepts_tracking_tagged_indeed_urls_and_stores_strict_form(
    private_test_dir: Path,
):
    """Loose-but-strict-canonicalizable export URLs admit in strict form.

    Discovery exports may carry harmless tracking query data such as an
    Indeed ``from=search`` tag. Admission re-canonicalizes each row URL
    with the strict canonicalizer, compares canonical-to-canonical for
    duplicates, and stores only the strict form that suppression keys use.
    """
    discover, intake_path, export_path, queue_path, memory_path, rows = admission_fixture(
        private_test_dir
    )
    for row in rows:
        assert row["platform"] == "indeed"
        row["url"] = f"{row['url']}&from=search"
    write_admission_export(export_path, rows)

    status = discover.admit_current_recommendations_for_active_queue(
        intake_path, export_path, queue_path, memory_path
    )

    assert status == discover.AdmissionStatus(validated_count=5, suppressed_count=0, admitted_count=5)
    assert admission_database_counts(queue_path) == (5, 5)
    with sqlite3.connect(queue_path) as connection:
        stored_urls = [
            row[0]
            for row in connection.execute("SELECT source_url FROM smart_queue_jobs ORDER BY rowid").fetchall()
        ]
    assert len(stored_urls) == 5
    assert all("from=" not in url for url in stored_urls)
    assert stored_urls == [f"https://www.indeed.com/viewjob?jk=admit{index + 1:04d}" for index in range(5)]


def test_admit_queue_contending_lease_holder_fails_closed_without_mutation(
    private_test_dir: Path,
):
    """Admission is single-admitter via the monitor's sibling-file lease.

    While another holder owns the lease for the same queue database, the
    whole admission operation fails closed before reading intake/export
    state into durable queue or memory rows.
    """
    discover, intake_path, export_path, queue_path, memory_path, _rows = admission_fixture(
        private_test_dir
    )

    with discover._monitor_database_lease(queue_path):
        with pytest.raises(discover.AdmissionError, match="^queue admission failed$"):
            discover.admit_current_recommendations_for_active_queue(
                intake_path, export_path, queue_path, memory_path
            )

    assert not queue_path.exists()
    assert not memory_path.exists()

    status = discover.admit_current_recommendations_for_active_queue(
        intake_path, export_path, queue_path, memory_path
    )
    assert status == discover.AdmissionStatus(validated_count=5, suppressed_count=0, admitted_count=5)


def test_admit_queue_title_edited_reexport_of_known_url_fails_whole_batch_closed(
    private_test_dir: Path,
):
    """Re-exports of URL-known rows must be byte-identical or fail closed.

    A title edit changes the discovery fingerprint, so the re-exported row
    carries a new job identity for an already-known listing URL. Admission
    keeps failing the whole batch closed rather than rewriting the
    immutable recommendation history. No output-field changes are involved.
    """
    discover, intake_path, export_path, queue_path, memory_path, rows = admission_fixture(
        private_test_dir
    )

    first = discover.admit_current_recommendations_for_active_queue(
        intake_path, export_path, queue_path, memory_path
    )
    assert first == discover.AdmissionStatus(validated_count=5, suppressed_count=0, admitted_count=5)

    edited = [dict(row) for row in rows]
    edited[0] = {
        **edited[0],
        "title": "Senior Python Backend Developer",
        "fingerprint": f"{0xED17ED:064x}",
    }
    write_admission_export(export_path, edited)

    with pytest.raises(discover.AdmissionError, match="^queue admission failed$"):
        discover.admit_current_recommendations_for_active_queue(
            intake_path, export_path, queue_path, memory_path
        )

    assert admission_database_counts(queue_path) == (5, 5)
