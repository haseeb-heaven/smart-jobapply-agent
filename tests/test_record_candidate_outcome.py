"""CLI contracts for recording one candidate-confirmed queue outcome.

Only synthetic URLs and ignored private runtime paths are used.  The command
must remain a local outcome-recording boundary and never drive browser work.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import uuid

import pytest

from jobapply_agent.candidate_memory import CandidateMemory
from jobapply_agent.smart_queue import QueueCandidate, SmartJobQueue


_PROJECT_ROOT = Path(__file__).parents[1]
_PRIVATE_ROOT = _PROJECT_ROOT / "jobapply_agent" / "private"
_SCRIPT_PATH = _PROJECT_ROOT / "jobapply_agent" / "scripts" / "record_candidate_outcome.py"
_PROFILE_REVISION = "record-outcome-profile-v1"
_POLICY_REVISION = "record-outcome-policy-v1"
_URL = "https://www.linkedin.com/jobs/view/920001?utm_source=synthetic"


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("record_candidate_outcome_for_test", _SCRIPT_PATH)
    assert spec and spec.loader, "record_candidate_outcome.py must be a loadable local CLI"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _private_paths(tmp_path: Path) -> tuple[Path, Path]:
    runtime = _PRIVATE_ROOT / "test-record-candidate-outcome" / f"{tmp_path.name}-{uuid.uuid4().hex}"
    return runtime / "smart-queue.sqlite3", runtime / "candidate-memory.sqlite3"


def _seed_open_queue_job(database_path: Path) -> QueueCandidate:
    queue = SmartJobQueue(database_path, target_size=1)
    candidate = QueueCandidate(
        job_id="cli-queue-job",
        source_url=_URL,
        fit_score=96,
        eligible=True,
        decision="recommended",
        evidence=("synthetic candidate-approved evidence",),
        profile_revision=_PROFILE_REVISION,
        matcher_policy_revision=_POLICY_REVISION,
    )
    queue.add_recommendations([candidate])
    action = queue.plan_refill(open_urls=[])
    queue.record_visible_snapshot(action.urls_to_open, actor="synthetic-browser-bridge")
    assert queue.get(candidate.job_id).state == "open"
    return candidate


def _seed_released_queue_job(database_path: Path) -> QueueCandidate:
    candidate = _seed_open_queue_job(database_path)
    queue = SmartJobQueue(database_path)
    queue.record_visible_snapshot((), actor="synthetic-browser-bridge")
    assert queue.get(candidate.job_id).state == "released"
    return candidate


def _arguments(queue_path: Path, memory_path: Path, *extra: str, outcome: str = "submitted") -> list[str]:
    return [
        "--queue-db",
        str(queue_path),
        "--memory-db",
        str(memory_path),
        "--job-id",
        "cli-queue-job",
        "--outcome",
        outcome,
        *extra,
    ]


def _run_cli(monkeypatch: pytest.MonkeyPatch, cli: object, arguments: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT_PATH), *arguments])
    return cli.main()


def test_cli_requires_explicit_vacated_attestation_and_never_enters_browser_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    queue_path, memory_path = _private_paths(tmp_path)
    _seed_open_queue_job(queue_path)
    cli = _load_cli_module()

    def forbidden_browser_flow(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("outcome CLI must not perform browser queue work")

    monkeypatch.setattr(SmartJobQueue, "record_visible_snapshot", forbidden_browser_flow)
    monkeypatch.setattr(SmartJobQueue, "plan_refill", forbidden_browser_flow)

    with pytest.raises(SystemExit) as raised:
        _run_cli(monkeypatch, cli, _arguments(queue_path, memory_path))

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert _URL not in (captured.out + captured.err)
    assert SmartJobQueue(queue_path).get("cli-queue-job").state == "open"
    assert CandidateMemory(memory_path).is_suppressed(_URL) is False


@pytest.mark.parametrize("outcome", ("submitted", "rejected", "skipped"))
def test_cli_records_only_allowed_candidate_confirmed_outcomes_with_redacted_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: str,
) -> None:
    queue_path, memory_path = _private_paths(tmp_path)
    _seed_open_queue_job(queue_path)
    cli = _load_cli_module()

    def forbidden_browser_flow(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("outcome CLI must not perform browser queue work")

    monkeypatch.setattr(SmartJobQueue, "record_visible_snapshot", forbidden_browser_flow)
    monkeypatch.setattr(SmartJobQueue, "plan_refill", forbidden_browser_flow)

    exit_code = _run_cli(monkeypatch, cli, _arguments(queue_path, memory_path, "--vacated", outcome=outcome))

    captured = capsys.readouterr()
    assert exit_code == 0
    stdout = captured.out.strip()
    assert _URL not in stdout
    assert "https://" not in stdout
    assert json.loads(stdout) == {"status": "ok", "reconciled": 1}
    assert SmartJobQueue(queue_path).get("cli-queue-job").state == outcome
    assert CandidateMemory(memory_path).is_suppressed(_URL) is True

    retry_exit_code = _run_cli(monkeypatch, cli, _arguments(queue_path, memory_path, "--vacated", outcome=outcome))
    retry_stdout = capsys.readouterr().out.strip()
    assert retry_exit_code == 0
    assert json.loads(retry_stdout) == {"status": "ok", "reconciled": 0}
    assert len(SmartJobQueue(queue_path).confirmed_outcome_events()) == 1
    assert CandidateMemory(memory_path).is_suppressed(_URL) is True


def test_cli_records_a_delayed_user_outcome_for_a_released_job_through_candidate_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    queue_path, memory_path = _private_paths(tmp_path)
    _seed_released_queue_job(queue_path)
    cli = _load_cli_module()

    def forbidden_browser_flow(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("outcome CLI must not perform browser queue work")

    monkeypatch.setattr(SmartJobQueue, "record_visible_snapshot", forbidden_browser_flow)
    monkeypatch.setattr(SmartJobQueue, "plan_refill", forbidden_browser_flow)

    exit_code = _run_cli(monkeypatch, cli, _arguments(queue_path, memory_path, "--vacated"))

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"status": "ok", "reconciled": 1}
    assert SmartJobQueue(queue_path).get("cli-queue-job").state == "submitted"
    assert CandidateMemory(memory_path).is_suppressed(_URL) is True


@pytest.mark.parametrize("outcome", ("", "opened", "manual_applying", "offer", "withdrawn"))
def test_cli_rejects_non_outcome_values_without_mutating_queue_or_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], outcome: str
) -> None:
    queue_path, memory_path = _private_paths(tmp_path)
    _seed_open_queue_job(queue_path)
    cli = _load_cli_module()

    with pytest.raises(SystemExit) as raised:
        _run_cli(monkeypatch, cli, _arguments(queue_path, memory_path, "--vacated", outcome=outcome))

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert _URL not in (captured.out + captured.err)
    assert SmartJobQueue(queue_path).get("cli-queue-job").state == "open"
    assert CandidateMemory(memory_path).is_suppressed(_URL) is False


def test_cli_rejects_queue_or_memory_paths_outside_the_private_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    private_queue, private_memory = _private_paths(tmp_path)
    _seed_open_queue_job(private_queue)
    outside_queue = tmp_path / "outside-queue.sqlite3"
    outside_memory = tmp_path / "outside-memory.sqlite3"
    cli = _load_cli_module()

    for queue_path, memory_path in ((outside_queue, private_memory), (private_queue, outside_memory)):
        exit_code = _run_cli(monkeypatch, cli, _arguments(queue_path, memory_path, "--vacated"))
        captured = capsys.readouterr()
        assert exit_code != 0
        assert _URL not in (captured.out + captured.err)

    assert not outside_queue.exists()
    assert not outside_memory.exists()
    assert SmartJobQueue(private_queue).get("cli-queue-job").state == "open"
    assert CandidateMemory(private_memory).is_suppressed(_URL) is False


def test_cli_rolls_back_the_queue_outcome_when_candidate_memory_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    queue_path, memory_path = _private_paths(tmp_path)
    _seed_open_queue_job(queue_path)
    CandidateMemory(memory_path)
    connection = sqlite3.connect(memory_path)
    try:
        connection.execute(
            """
            CREATE TRIGGER reject_synthetic_memory_write
            BEFORE INSERT ON candidate_memory_outcomes
            BEGIN
                SELECT RAISE(ABORT, 'synthetic candidate-memory write failure');
            END;
            """
        )
        connection.commit()
    finally:
        connection.close()
    cli = _load_cli_module()

    exit_code = _run_cli(monkeypatch, cli, _arguments(queue_path, memory_path, "--vacated"))

    captured = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(captured.out) == {"status": "blocked", "reconciled": 0}
    assert _URL not in (captured.out + captured.err)
    assert SmartJobQueue(queue_path).get("cli-queue-job").state == "open"
    assert CandidateMemory(memory_path).is_suppressed(_URL) is False


def test_cli_blocks_wal_candidate_memory_without_mutating_queue_or_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    queue_path, memory_path = _private_paths(tmp_path)
    _seed_open_queue_job(queue_path)
    CandidateMemory(memory_path)
    connection = sqlite3.connect(memory_path)
    try:
        journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        assert journal_mode is not None and str(journal_mode[0]).lower() == "wal"
    finally:
        connection.close()
    cli = _load_cli_module()

    exit_code = _run_cli(monkeypatch, cli, _arguments(queue_path, memory_path, "--vacated"))

    captured = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(captured.out) == {"status": "blocked", "reconciled": 0}
    assert _URL not in (captured.out + captured.err)
    assert SmartJobQueue(queue_path).get("cli-queue-job").state == "open"
    assert CandidateMemory(memory_path).is_suppressed(_URL) is False


def test_cli_rejects_cross_candidate_queue_and_memory_pairing_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    queue_path, memory_path = _private_paths(tmp_path)
    queue_candidate = _seed_open_queue_job(queue_path)
    other_profile_candidate = QueueCandidate(
        job_id="other-profile-job",
        source_url="https://www.indeed.com/viewjob?jk=other-profile-synthetic",
        fit_score=95,
        eligible=True,
        decision="recommended",
        evidence=("synthetic candidate-approved evidence",),
        profile_revision="record-outcome-other-profile-v1",
        matcher_policy_revision=_POLICY_REVISION,
    )
    other_profile_memory = CandidateMemory(memory_path)
    other_queue = SmartJobQueue(memory_path.with_name("other-smart-queue.sqlite3"))
    other_queue.add_recommendations([other_profile_candidate])
    assert other_profile_memory.filter_unsuppressed_candidates(
        [other_profile_candidate], queue=other_queue
    ) == (
        other_profile_candidate,
    )
    cli = _load_cli_module()

    exit_code = _run_cli(monkeypatch, cli, _arguments(queue_path, memory_path, "--vacated"))

    captured = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(captured.out) == {"status": "blocked", "reconciled": 0}
    assert _URL not in (captured.out + captured.err)
    assert "https://" not in (captured.out + captured.err)
    assert SmartJobQueue(queue_path).get(queue_candidate.job_id).state == "open"
    assert SmartJobQueue(queue_path).confirmed_outcome_events() == ()
    assert other_profile_memory.is_suppressed(queue_candidate.source_url) is False
    assert other_profile_memory.is_suppressed(other_profile_candidate.source_url) is False
