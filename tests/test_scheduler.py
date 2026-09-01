from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty

import pytest

try:
    import fcntl
except ModuleNotFoundError:  # Windows uses the separately exercised msvcrt backend.
    fcntl = None  # type: ignore[assignment]

sys.path.insert(0, str(Path(__file__).parents[1] / "job_profession" / "src"))

from job_profession.models import CandidateProfile
import job_profession.scheduler as scheduler_module
from job_profession.scheduler import JobDiscoveryScheduler, candidate_profile_revision, current_profile_recommendations
from job_profession.matcher import matcher_policy_revision
from job_profession.sources import MappingVisiblePageAdapter, SearchProfile, build_search_url, listing_from_visible_payload


PROJECT_ROOT = Path(__file__).parents[1]
CRON_WRAPPER = PROJECT_ROOT / "job_profession" / "scripts" / "cron_wrapper.sh"


def _command_path(tmp_path: Path, *, include_python3: bool = False) -> Path:
    """Create a PATH containing only the commands needed by cron_wrapper.sh."""

    command_dir = tmp_path / "bin"
    command_dir.mkdir(parents=True)
    commands = ("bash", "dirname", "mkdir", "readlink")
    for command in commands:
        resolved = shutil.which(command)
        if resolved is None:
            pytest.skip(f"{command} is required for the cron wrapper subprocess test")
        try:
            (command_dir / command).symlink_to(resolved)
        except OSError as exc:
            pytest.skip(f"symlink setup is unavailable: {exc}")
    if include_python3:
        try:
            (command_dir / "python3").symlink_to(sys.executable)
        except OSError as exc:
            pytest.skip(f"symlink setup is unavailable: {exc}")
    return command_dir


def profile() -> CandidateProfile:
    professional = ("python", "fastapi", "rest api", "postgresql", "background jobs", "unit testing", "integrations")
    return CandidateProfile(professional_skills=professional, evidence_by_skill={skill: "professional" for skill in professional})


class _BlockingVisiblePageAdapter:
    """Test adapter that holds one scheduler inside its exclusive run section."""

    def __init__(
        self,
        payloads_by_search_url: dict[str, list[dict[str, str]]],
        entered: multiprocessing.synchronize.Event | None = None,
        release: multiprocessing.synchronize.Event | None = None,
    ) -> None:
        self._payloads_by_search_url = payloads_by_search_url
        self._entered = entered
        self._release = release

    def read_visible_listings(self, search_url: str):
        if self._entered is not None:
            self._entered.set()
        if self._release is not None and not self._release.wait(timeout=10):
            raise TimeoutError("test process did not release the first scheduler")
        return self._payloads_by_search_url.get(search_url, ())


def _concurrent_scheduler_worker(
    state_path: str,
    export_path: str,
    hold_lock: bool,
    entered: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    read_started: multiprocessing.synchronize.Event | None,
    ready_to_run: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    search = SearchProfile("linkedin", "Python Backend Developer")
    payloads = {
        build_search_url(search): [
            {
                "title": "Python Backend Developer",
                "company": "Acme",
                "url": "https://www.linkedin.com/jobs/view/concurrent-lock-test",
                "description": "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
            }
        ]
    }
    adapter = _BlockingVisiblePageAdapter(payloads, entered, release) if hold_lock else _BlockingVisiblePageAdapter(payloads, read_started)
    scheduler = JobDiscoveryScheduler(profile(), [search], state_path=state_path, export_path=export_path)
    ready_to_run.set()
    try:
        result = scheduler.run(adapter)
    except Exception as exc:
        results.put({"error": f"{type(exc).__name__}: {exc}"})
    else:
        results.put(
            {
                "application_actions": result.application_actions,
                "duplicate_listings": result.duplicate_listings,
                "recommended_exports": result.recommended_exports,
            }
        )


def _hold_scheduler_lock(
    lock_path: str, acquired: multiprocessing.synchronize.Event, release: multiprocessing.synchronize.Event
) -> None:
    if fcntl is None:
        raise RuntimeError("POSIX flock is unavailable")
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        acquired.set()
        release.wait(timeout=10)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def test_search_urls_are_transparent_platform_specific_and_constrained():
    linkedin = SearchProfile("linkedin", "FastAPI Developer")
    indeed = SearchProfile("indeed", "API Integration Developer", "Bengaluru")
    assert build_search_url(linkedin).startswith("https://www.linkedin.com/jobs/search/?")
    assert "keywords=FastAPI+Developer" in build_search_url(linkedin)
    assert "f_E=3" in build_search_url(linkedin)
    assert build_search_url(indeed).startswith("https://www.indeed.com/jobs?")
    assert "q=API+Integration+Developer" in build_search_url(indeed)
    try:
        SearchProfile("linkedin", "Marketing Manager")
    except ValueError as exc:
        assert "Python, FastAPI, or API" in str(exc)
    else:
        raise AssertionError("unrelated role query should be rejected")
    try:
        SearchProfile("linkedin", "Senior Python Engineer")
    except ValueError as exc:
        assert "may not target" in str(exc)
    else:
        raise AssertionError("senior query should be rejected")
    for excluded_query in ("Junior Python Developer", "Entry FastAPI Developer", "Python Developer II"):
        try:
            SearchProfile("linkedin", excluded_query)
        except ValueError as exc:
            assert "may not target" in str(exc)
        else:
            raise AssertionError(f"excluded-level query should be rejected: {excluded_query}")


def test_repeated_runs_dedupe_and_never_take_application_actions(tmp_path: Path):
    search = SearchProfile("linkedin", "Python Backend Developer")
    url = build_search_url(search)
    adapter = MappingVisiblePageAdapter(
        {
            url: [
                {
                    "title": "Python Backend Developer",
                    "company": "Acme",
                    "url": "https://www.linkedin.com/jobs/view/123?utm_source=alert",
                    "description": "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
                },
                {
                    "title": "Python Backend Developer",
                    "company": "Acme",
                    "url": "https://www.linkedin.com/jobs/view/123?utm_source=alert",
                    "description": "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
                },
            ]
        }
    )
    scheduler = JobDiscoveryScheduler(
        profile(), [search], state_path=tmp_path / "state.json", export_path=tmp_path / "jobs.jsonl", run_log_path=tmp_path / "runs.jsonl"
    )
    fixed_now = datetime(2026, 8, 31, 8, tzinfo=timezone.utc)
    first = scheduler.run(adapter, now=fixed_now)
    second = scheduler.run(adapter, now=fixed_now)

    assert first.application_actions == 0
    assert first.minimum_profile_fit_score == 85
    assert first.recommended_exports == 1
    assert first.duplicate_listings == 1
    assert second.application_actions == 0
    assert second.recommended_exports == 0
    assert second.duplicate_listings == 2
    rows = [json.loads(line) for line in (tmp_path / "jobs.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["score"] >= 85
    assert rows[0]["minimum_profile_fit_score"] == 85
    assert rows[0]["application_actions"] == 0


def test_profile_revision_re_evaluates_same_listing_and_preserves_same_revision_dedupe(tmp_path: Path):
    """A safe evidence revision must not inherit seen state from an older profile."""

    search = SearchProfile("linkedin", "Python Backend Developer")
    adapter = MappingVisiblePageAdapter(
        {
            build_search_url(search): [
                {
                    "title": "Python Backend Developer",
                    "company": "Acme",
                    "url": "https://www.linkedin.com/jobs/view/profile-revision-test",
                    "description": "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL REST APIs.",
                }
            ]
        }
    )
    profile_a = CandidateProfile(
        professional_skills=("python",),
        personal_open_source_skills=("fastapi", "rest api", "postgresql", "unit testing"),
        evidence_by_skill={
            "python": "professional",
            "fastapi": "personal_open_source",
            "rest api": "personal_open_source",
            "postgresql": "personal_open_source",
            "unit testing": "personal_open_source",
        },
    )
    profile_b = profile()
    state_path = tmp_path / "state.json"
    export_path = tmp_path / "jobs.jsonl"

    first = JobDiscoveryScheduler(profile_a, [search], state_path=state_path, export_path=export_path).run(adapter)
    revised = JobDiscoveryScheduler(profile_b, [search], state_path=state_path, export_path=export_path).run(adapter)
    repeated = JobDiscoveryScheduler(profile_b, [search], state_path=state_path, export_path=export_path).run(adapter)

    assert first.profile_revision != revised.profile_revision
    assert first.recommended_exports == 0
    assert first.below_threshold == 1
    assert revised.new_listings == 1
    assert revised.recommended_exports == 1
    assert repeated.recommended_exports == 0
    assert repeated.duplicate_listings == 1
    rows = [json.loads(line) for line in export_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["profile_revision"] == revised.profile_revision
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert set(state["fingerprints_by_profile_revision"]) == {first.profile_revision, revised.profile_revision}


def test_policy_revision_re_scores_seen_listing_and_hides_pre_policy_queue_rows(tmp_path: Path):
    search = SearchProfile("linkedin", "Python Backend Developer")
    adapter = MappingVisiblePageAdapter(
        {
            build_search_url(search): [
                {
                    "title": "Python Backend Developer",
                    "company": "Acme",
                    "url": "https://www.linkedin.com/jobs/view/policy-revision-test",
                    "description": (
                        "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL. forbidden-token"
                    ),
                }
            ]
        }
    )
    rules_path = tmp_path / "scoring_rules.yaml"
    state_path = tmp_path / "state.json"
    export_path = tmp_path / "jobs.jsonl"

    first = JobDiscoveryScheduler(
        profile(), [search], state_path=state_path, export_path=export_path, rules_path=rules_path
    ).run(adapter)
    rules_path.write_text("hard_reject_terms:\n  - forbidden-token\n", encoding="utf-8")
    revised = JobDiscoveryScheduler(
        profile(), [search], state_path=state_path, export_path=export_path, rules_path=rules_path
    ).run(adapter)

    assert first.recommended_exports == 1
    assert revised.profile_revision != first.profile_revision
    assert revised.matcher_policy_revision != first.matcher_policy_revision
    assert revised.new_listings == 1
    assert revised.below_threshold == 1
    assert current_profile_recommendations(profile(), export_path, rules_path=rules_path) == []
    assert len(export_path.read_text(encoding="utf-8").splitlines()) == 1


def test_current_profile_recommendations_hide_historical_profile_revisions(tmp_path: Path):
    """The review view must never surface a row from an older evidence revision."""

    search = SearchProfile("linkedin", "Python Backend Developer")
    adapter = MappingVisiblePageAdapter(
        {
            build_search_url(search): [
                {
                    "title": "Python Backend Developer",
                    "company": "Acme",
                    "url": "https://www.linkedin.com/jobs/view/current-profile-view-test",
                    "description": "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
                }
            ]
        }
    )
    profile_a = profile()
    profile_b = CandidateProfile(
        professional_skills=profile_a.professional_skills,
        role_targets=("Python Backend Developer",),
        evidence_by_skill=profile_a.evidence_by_skill,
    )
    state_path = tmp_path / "state.json"
    export_path = tmp_path / "jobs.jsonl"

    first = JobDiscoveryScheduler(profile_a, [search], state_path=state_path, export_path=export_path).run(adapter)
    revised = JobDiscoveryScheduler(profile_b, [search], state_path=state_path, export_path=export_path).run(adapter)
    historical_export = export_path.read_text(encoding="utf-8")

    assert first.profile_revision != revised.profile_revision
    assert first.recommended_exports == 1
    assert revised.recommended_exports == 1
    assert len(historical_export.splitlines()) == 2
    assert [row["profile_revision"] for row in current_profile_recommendations(profile_a, export_path)] == [
        first.profile_revision
    ]
    current_rows = current_profile_recommendations(profile_b, export_path)
    assert [row["profile_revision"] for row in current_rows] == [revised.profile_revision]
    assert all(row["application_actions"] == 0 for row in current_rows)
    assert export_path.read_text(encoding="utf-8") == historical_export


def test_current_profile_recommendations_fail_closed_for_invalid_active_rows(tmp_path: Path):
    active_profile = profile()
    revision = candidate_profile_revision(active_profile)
    export_path = tmp_path / "jobs.jsonl"
    valid_row = {
        "record_type": "recommended_job_for_human_review",
        "discovery_mode": "export_only",
        "application_actions": 0,
        "profile_revision": revision,
        "matcher_policy_revision": matcher_policy_revision(),
        "decision": "recommended",
        "score": 85,
        "minimum_profile_fit_score": 85,
        "threshold_met": True,
        "platform": "linkedin",
        "fingerprint": "a" * 64,
        "title": "Python Backend Developer",
        "url": "https://www.linkedin.com/jobs/view/current-profile-filter-test",
    }
    invalid_score_row = {**valid_row, "fingerprint": "invalid-score", "score": 0, "threshold_met": False}
    invalid_platform_row = {**valid_row, "fingerprint": "invalid-platform", "platform": "other"}
    invalid_url_row = {**valid_row, "fingerprint": "b" * 64, "url": "http://www.linkedin.com/jobs/view/not-https"}
    invalid_fingerprint_row = {**valid_row, "fingerprint": "not-a-sha256"}
    test_fixture_row = {**valid_row, "fingerprint": "c" * 64, "is_test_fixture": True}
    export_path.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in (
                valid_row,
                invalid_score_row,
                invalid_platform_row,
                invalid_url_row,
                invalid_fingerprint_row,
                test_fixture_row,
            )
        ),
        encoding="utf-8",
    )

    rows = current_profile_recommendations(active_profile, export_path)

    assert rows == [valid_row]
    assert rows[0]["application_actions"] == 0


def test_profile_revision_changes_for_safe_roles_and_classified_evidence_only():
    base = CandidateProfile(
        professional_skills=("python",),
        role_targets=("Python Developer",),
        evidence_by_skill={"python": "professional"},
    )
    changed_roles = CandidateProfile(
        professional_skills=("python",),
        role_targets=("FastAPI Developer",),
        evidence_by_skill={"python": "professional"},
    )
    changed_evidence = CandidateProfile(
        professional_skills=("python", "fastapi"),
        role_targets=("Python Developer",),
        evidence_by_skill={"python": "professional", "fastapi": "professional"},
    )
    changed_mandatory_exclusion = CandidateProfile(
        professional_skills=("python",),
        role_targets=("Python Developer",),
        mandatory_excluded_requirements=("ML model training or deployment ownership",),
        evidence_by_skill={"python": "professional"},
    )
    changed_preferences = CandidateProfile(
        professional_skills=("python",),
        role_targets=("Python Developer",),
        location_preferences=("Hyderabad",),
        work_mode_preferences=("onsite",),
        evidence_by_skill={"python": "professional"},
    )

    assert candidate_profile_revision(base) != candidate_profile_revision(changed_roles)
    assert candidate_profile_revision(base) != candidate_profile_revision(changed_evidence)
    assert candidate_profile_revision(base) != candidate_profile_revision(changed_mandatory_exclusion)
    assert candidate_profile_revision(base) != candidate_profile_revision(changed_preferences)


def test_profile_revision_changes_for_each_approved_eligibility_constraint():
    base = CandidateProfile(
        professional_skills=("python",),
        years_experience=3,
        role_targets=("Python Developer",),
        location_preferences=("Hyderabad",),
        work_mode_preferences=("onsite",),
        evidence_by_skill={"python": "professional"},
    )
    changed_years = CandidateProfile(
        professional_skills=("python",),
        years_experience=4,
        role_targets=("Python Developer",),
        location_preferences=("Hyderabad",),
        work_mode_preferences=("onsite",),
        evidence_by_skill={"python": "professional"},
    )
    changed_location = CandidateProfile(
        professional_skills=("python",),
        years_experience=3,
        role_targets=("Python Developer",),
        location_preferences=("Bengaluru",),
        work_mode_preferences=("onsite",),
        evidence_by_skill={"python": "professional"},
    )
    changed_work_mode = CandidateProfile(
        professional_skills=("python",),
        years_experience=3,
        role_targets=("Python Developer",),
        location_preferences=("Hyderabad",),
        work_mode_preferences=("remote",),
        evidence_by_skill={"python": "professional"},
    )

    revision = candidate_profile_revision(base)

    assert revision != candidate_profile_revision(changed_years)
    assert revision != candidate_profile_revision(changed_location)
    assert revision != candidate_profile_revision(changed_work_mode)


def test_only_85_plus_recommendations_are_exported(tmp_path: Path):
    search = SearchProfile("indeed", "FastAPI Developer")
    adapter = MappingVisiblePageAdapter(
        {
            build_search_url(search): [
                {
                    "title": "Software Engineer",
                    "company": "Acme",
                    "url": "https://www.indeed.com/viewjob?jk=lower-fit",
                    "description": "Maintain Python REST APIs, add features, write unit tests, and work with PostgreSQL.",
                }
            ]
        }
    )
    result = JobDiscoveryScheduler(profile(), [search], state_path=tmp_path / "state.json", export_path=tmp_path / "jobs.jsonl").run(adapter)
    assert result.below_threshold == 1
    assert result.recommended_exports == 0
    assert not (tmp_path / "jobs.jsonl").exists()


def test_visible_listing_urls_require_https_and_matching_platform_host():
    payload = {"title": "Python Backend Developer", "url": "http://www.linkedin.com/jobs/view/1"}
    try:
        listing_from_visible_payload(payload, platform="linkedin")
    except ValueError as error:
        assert "HTTPS" in str(error)
    else:
        raise AssertionError("HTTP listing URL should be rejected")

    payload["url"] = "https://example.test/jobs/1"
    try:
        listing_from_visible_payload(payload, platform="linkedin")
    except ValueError as error:
        assert "allowed linkedin host" in str(error)
    else:
        raise AssertionError("non-platform listing URL should be rejected")

    listing = listing_from_visible_payload(
        {"title": "Python Backend Developer", "url": "https://uk.indeed.com/viewjob?jk=1"},
        platform="indeed",
    )
    assert listing.platform == "indeed"


def test_system_design_and_architecture_requirements_are_rejected(tmp_path: Path):
    search = SearchProfile("linkedin", "Python Backend Developer")
    adapter = MappingVisiblePageAdapter(
        {build_search_url(search): [{
            "title": "Python Backend Developer",
            "company": "Acme",
            "url": "https://www.linkedin.com/jobs/view/architecture-1",
            "description": "Maintain FastAPI APIs, write unit tests, and own system design for distributed services.",
        }]}
    )
    result = JobDiscoveryScheduler(
        profile(), [search], state_path=tmp_path / "state.json", export_path=tmp_path / "jobs.jsonl"
    ).run(adapter)
    assert result.recommended_exports == 0
    assert result.below_threshold == 1


def test_failed_job_export_leaves_listing_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A failed recommendation export must not commit the listing to state."""

    search = SearchProfile("linkedin", "Python Backend Developer")
    adapter = MappingVisiblePageAdapter(
        {
            build_search_url(search): [
                {
                    "title": "Python Backend Developer",
                    "company": "Acme",
                    "url": "https://www.linkedin.com/jobs/view/retry-after-export-failure",
                    "description": "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
                }
            ]
        }
    )
    state_path = tmp_path / "state.json"
    export_path = tmp_path / "jobs.jsonl"
    scheduler = JobDiscoveryScheduler(profile(), [search], state_path=state_path, export_path=export_path)

    def fail_export(_path: Path, _rows: object) -> None:
        raise OSError("simulated export failure")

    with monkeypatch.context() as patch:
        patch.setattr(JobDiscoveryScheduler, "_append_json_lines", staticmethod(fail_export))
        with pytest.raises(OSError, match="simulated export failure"):
            scheduler.run(adapter)

    assert not state_path.exists()
    assert not export_path.exists()

    retry = scheduler.run(adapter)

    assert retry.new_listings == 1
    assert retry.duplicate_listings == 0
    assert retry.recommended_exports == 1
    assert len(export_path.read_text(encoding="utf-8").splitlines()) == 1


def test_failed_audit_export_leaves_state_uncommitted_without_duplicate_recommendation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """If audit output fails after the job row writes, retry must not duplicate that row."""

    search = SearchProfile("indeed", "FastAPI Developer")
    adapter = MappingVisiblePageAdapter(
        {
            build_search_url(search): [
                {
                    "title": "FastAPI Developer",
                    "company": "Acme",
                    "url": "https://www.indeed.com/viewjob?jk=retry-after-audit-failure",
                    "description": "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
                }
            ]
        }
    )
    state_path = tmp_path / "state.json"
    export_path = tmp_path / "jobs.jsonl"
    run_log_path = tmp_path / "runs.jsonl"
    scheduler = JobDiscoveryScheduler(
        profile(), [search], state_path=state_path, export_path=export_path, run_log_path=run_log_path
    )

    def fail_audit(_scheduler: JobDiscoveryScheduler, _result: object) -> None:
        raise OSError("simulated audit failure")

    with monkeypatch.context() as patch:
        patch.setattr(JobDiscoveryScheduler, "_log_result", fail_audit)
        with pytest.raises(OSError, match="simulated audit failure"):
            scheduler.run(adapter)

    assert not state_path.exists()
    assert len(export_path.read_text(encoding="utf-8").splitlines()) == 1
    assert not run_log_path.exists()

    retry = scheduler.run(adapter)

    assert retry.new_listings == 0
    assert retry.duplicate_listings == 1
    assert retry.recommended_exports == 0
    assert len(export_path.read_text(encoding="utf-8").splitlines()) == 1
    assert len(run_log_path.read_text(encoding="utf-8").splitlines()) == 1
    assert json.loads(state_path.read_text(encoding="utf-8"))["fingerprints"]


def test_failed_state_write_does_not_duplicate_already_exported_recommendation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A state-write failure can be retried from the durable append-only export."""

    search = SearchProfile("linkedin", "Python Backend Developer")
    adapter = MappingVisiblePageAdapter(
        {
            build_search_url(search): [
                {
                    "title": "Python Backend Developer",
                    "company": "Acme",
                    "url": "https://www.linkedin.com/jobs/view/retry-after-state-failure",
                    "description": "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
                }
            ]
        }
    )
    state_path = tmp_path / "state.json"
    export_path = tmp_path / "jobs.jsonl"
    scheduler = JobDiscoveryScheduler(profile(), [search], state_path=state_path, export_path=export_path)

    original_state_write = JobDiscoveryScheduler._atomic_json_write

    def fail_state_write(_path: Path, _state: object) -> None:
        raise OSError("simulated state write failure")

    with monkeypatch.context() as patch:
        patch.setattr(JobDiscoveryScheduler, "_atomic_json_write", staticmethod(fail_state_write))
        with pytest.raises(OSError, match="simulated state write failure"):
            scheduler.run(adapter)

    assert not state_path.exists()
    assert len(export_path.read_text(encoding="utf-8").splitlines()) == 1

    monkeypatch.setattr(JobDiscoveryScheduler, "_atomic_json_write", staticmethod(original_state_write))
    retry = scheduler.run(adapter)

    assert retry.new_listings == 0
    assert retry.duplicate_listings == 1
    assert retry.recommended_exports == 0
    assert len(export_path.read_text(encoding="utf-8").splitlines()) == 1
    assert json.loads(state_path.read_text(encoding="utf-8"))["fingerprints"]


def test_concurrent_schedulers_export_one_recommendation(tmp_path: Path):
    """The second process must re-read durable state before it can append."""

    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    second_read_started = context.Event()
    first_ready_to_run = context.Event()
    second_ready_to_run = context.Event()
    results = context.Queue()
    state_path = tmp_path / "state.json"
    export_path = tmp_path / "jobs.jsonl"
    first = context.Process(
        target=_concurrent_scheduler_worker,
        args=(str(state_path), str(export_path), True, entered, release, None, first_ready_to_run, results),
    )
    second = context.Process(
        target=_concurrent_scheduler_worker,
        args=(str(state_path), str(export_path), False, entered, release, second_read_started, second_ready_to_run, results),
    )

    first.start()
    try:
        assert first_ready_to_run.wait(timeout=10), "first scheduler worker did not start"
        assert entered.wait(timeout=10), "first scheduler did not enter its locked run"
        second.start()
        assert second_ready_to_run.wait(timeout=10), "second scheduler worker did not start"
        assert not second_read_started.wait(timeout=0.5), "second scheduler bypassed the exclusive run lock"
        release.set()
        first.join(timeout=15)
        second.join(timeout=15)
    finally:
        release.set()
        if first.is_alive():
            first.terminate()
            first.join(timeout=5)
        if second.is_alive():
            second.terminate()
            second.join(timeout=5)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert second_read_started.is_set()
    try:
        worker_results = [results.get(timeout=5), results.get(timeout=5)]
    except Empty as exc:
        raise AssertionError("both scheduler workers must report their result") from exc
    assert not any("error" in result for result in worker_results)
    assert sum(result["recommended_exports"] for result in worker_results) == 1
    assert all(result["application_actions"] == 0 for result in worker_results)
    rows = [json.loads(line) for line in export_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert len({row["fingerprint"] for row in rows}) == 1


def test_shared_export_serializes_schedulers_with_different_state_paths(tmp_path: Path):
    """Any shared output artifact must serialize its scheduler writers."""

    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    second_read_started = context.Event()
    first_ready_to_run = context.Event()
    second_ready_to_run = context.Event()
    results = context.Queue()
    export_path = tmp_path / "shared" / "jobs.jsonl"
    first = context.Process(
        target=_concurrent_scheduler_worker,
        args=(
            str(tmp_path / "first-state" / "state.json"),
            str(export_path),
            True,
            entered,
            release,
            None,
            first_ready_to_run,
            results,
        ),
    )
    second = context.Process(
        target=_concurrent_scheduler_worker,
        args=(
            str(tmp_path / "second-state" / "state.json"),
            str(export_path),
            False,
            entered,
            release,
            second_read_started,
            second_ready_to_run,
            results,
        ),
    )

    first.start()
    try:
        assert first_ready_to_run.wait(timeout=10), "first scheduler worker did not start"
        assert entered.wait(timeout=10), "first scheduler did not enter its locked run"
        second.start()
        assert second_ready_to_run.wait(timeout=10), "second scheduler worker did not start"
        assert not second_read_started.wait(timeout=0.5), "shared export was not protected by the scheduler lock"
        release.set()
        first.join(timeout=15)
        second.join(timeout=15)
    finally:
        release.set()
        if first.is_alive():
            first.terminate()
            first.join(timeout=5)
        if second.is_alive():
            second.terminate()
            second.join(timeout=5)

    assert first.exitcode == 0
    assert second.exitcode == 0
    assert second_read_started.is_set()
    try:
        worker_results = [results.get(timeout=5), results.get(timeout=5)]
    except Empty as exc:
        raise AssertionError("both scheduler workers must report their result") from exc
    assert not any("error" in result for result in worker_results)
    assert sum(result["recommended_exports"] for result in worker_results) == 1
    rows = [json.loads(line) for line in export_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert len({row["fingerprint"] for row in rows}) == 1


def test_stale_scheduler_lock_file_is_recoverable(tmp_path: Path):
    """A left-behind lock file must not block the next process indefinitely."""

    search = SearchProfile("linkedin", "Python Backend Developer")
    state_path = tmp_path / "state.json"
    scheduler = JobDiscoveryScheduler(
        profile(), [search], state_path=state_path, export_path=tmp_path / "jobs.jsonl", lock_wait_seconds=0.1
    )
    scheduler._lock_paths[0].parent.mkdir(parents=True, exist_ok=True)
    scheduler._lock_paths[0].touch()
    adapter = MappingVisiblePageAdapter(
        {
            build_search_url(search): [
                {
                    "title": "Python Backend Developer",
                    "company": "Acme",
                    "url": "https://www.linkedin.com/jobs/view/stale-lock-test",
                    "description": "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
                }
            ]
        }
    )

    result = scheduler.run(adapter)

    assert result.recommended_exports == 1
    assert result.application_actions == 0


@pytest.mark.skipif(fcntl is None, reason="POSIX-specific external flock contention test")
def test_active_scheduler_lock_times_out_with_a_bounded_wait(tmp_path: Path):
    context = multiprocessing.get_context("spawn")
    state_path = tmp_path / "state.json"
    scheduler = JobDiscoveryScheduler(
        profile(), (), state_path=state_path, export_path=tmp_path / "jobs.jsonl", lock_wait_seconds=0.05
    )
    lock_path = scheduler._lock_paths[0]
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(target=_hold_scheduler_lock, args=(str(lock_path), acquired, release))
    holder.start()
    try:
        assert acquired.wait(timeout=10), "lock-holder process did not acquire the scheduler lock"
        with pytest.raises(TimeoutError, match="Timed out waiting"):
            scheduler.run(MappingVisiblePageAdapter({}))
    finally:
        release.set()
        holder.join(timeout=10)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)

    assert holder.exitcode == 0


def test_cron_wrapper_runs_despite_abandoned_legacy_directory_lock(tmp_path: Path):
    """The scheduler flock, not a stale mkdir lock, controls scheduled runs."""

    from job_profession.intake import activate_candidate_profile, validate_candidate_intake

    output_dir = tmp_path / "scheduled-output"
    output_dir.mkdir()
    (output_dir / ".discovery.lock").mkdir()
    intake_path = tmp_path / "private" / "candidate_intake.json"
    intake_path.parent.mkdir()
    draft = validate_candidate_intake(
        {
            "schema_version": 1,
            "documents": [],
            "approved_facts": {"candidate_confirmed": True},
            "unknown_fields": [],
            "contradictions": [],
            "pending_facts": [],
        }
    )
    intake_path.write_text(
        json.dumps(activate_candidate_profile(draft, actor="user")), encoding="utf-8"
    )
    environment = os.environ.copy()
    environment["JOB_PROFESSION_OUTPUT_DIR"] = str(output_dir)
    environment["JOB_PROFESSION_PYTHON"] = sys.executable
    environment["JOB_PROFESSION_CANDIDATE_INTAKE"] = str(intake_path)

    completed = subprocess.run(
        [str(PROJECT_ROOT / "job_profession" / "scripts" / "cron_wrapper.sh")],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (output_dir / "discovery_runs.jsonl").exists()
    queue_path = output_dir / "Current_Profile_Recommended_Queue.csv"
    assert queue_path.exists()
    assert queue_path.read_text(encoding="utf-8").startswith("profile_revision,fingerprint,platform,score,")
    first_output_line = next(line for line in completed.stdout.splitlines() if line.startswith("{"))
    assert json.loads(first_output_line)["application_actions"] == 0
    assert "application_actions=0" in completed.stdout


@pytest.mark.skipif(os.name == "nt", reason="cron_wrapper.sh requires a POSIX shell and symlinks")
def test_cron_wrapper_resolves_symlink_before_locating_project_files(tmp_path: Path):
    """Launching through a relative symlink must still run the repository script."""

    from job_profession.intake import activate_candidate_profile, validate_candidate_intake

    output_dir = tmp_path / "scheduled-output"
    intake_path = tmp_path / "synthetic-intake.json"
    draft = validate_candidate_intake(
        {
            "schema_version": 1,
            "documents": [],
            "approved_facts": {"candidate_confirmed": True},
            "unknown_fields": [],
            "contradictions": [],
            "pending_facts": [],
        }
    )
    intake_path.write_text(json.dumps(activate_candidate_profile(draft, actor="user")), encoding="utf-8")

    link_path = tmp_path / "launcher dir" / "cron-wrapper.sh"
    link_path.parent.mkdir()
    link_path.symlink_to(os.path.relpath(CRON_WRAPPER, link_path.parent))
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    environment = {
        "PATH": str(_command_path(tmp_path / "tools")),
        "JOB_PROFESSION_OUTPUT_DIR": str(output_dir),
        "JOB_PROFESSION_PYTHON": sys.executable,
        "JOB_PROFESSION_CANDIDATE_INTAKE": str(intake_path),
    }

    completed = subprocess.run(
        [str(link_path)],
        cwd=caller_dir,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (output_dir / "discovery_runs.jsonl").exists()
    assert (output_dir / "Current_Profile_Recommended_Queue.csv").exists()


@pytest.mark.skipif(os.name == "nt", reason="cron_wrapper.sh requires a POSIX shell")
def test_cron_wrapper_rejects_unavailable_python_interpreter_without_running_discovery(tmp_path: Path):
    output_dir = tmp_path / "scheduled-output"
    missing_python = tmp_path / "missing-python"
    environment = {
        "PATH": str(_command_path(tmp_path / "tools")),
        "JOB_PROFESSION_OUTPUT_DIR": str(output_dir),
        "JOB_PROFESSION_PYTHON": str(missing_python),
        "JOB_PROFESSION_CANDIDATE_INTAKE": str(tmp_path / "synthetic-intake.json"),
    }

    completed = subprocess.run(
        [str(CRON_WRAPPER)],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 127
    assert completed.stdout == ""
    assert completed.stderr == (
        "Discovery did not run: no executable Python interpreter found; "
        "set JOB_PROFESSION_PYTHON to Python >= 3.11\n"
    )
    assert not (output_dir / "discovery_runs.jsonl").exists()
    assert not (output_dir / "Current_Profile_Recommended_Queue.csv").exists()


@pytest.mark.skipif(os.name == "nt", reason="cron_wrapper.sh requires a POSIX shell")
def test_cron_wrapper_rejects_unsupported_python_interpreter_without_running_discovery(tmp_path: Path):
    output_dir = tmp_path / "scheduled-output"
    unsupported_python = tmp_path / "unsupported-python"
    unsupported_python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        "  printf '3.10\\n'\n"
        "  exit 1\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    unsupported_python.chmod(0o700)
    environment = {
        "PATH": str(_command_path(tmp_path / "tools")),
        "JOB_PROFESSION_OUTPUT_DIR": str(output_dir),
        "JOB_PROFESSION_PYTHON": str(unsupported_python),
        "JOB_PROFESSION_CANDIDATE_INTAKE": str(tmp_path / "synthetic-intake.json"),
    }

    completed = subprocess.run(
        [str(CRON_WRAPPER)],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == (
        f"Discovery did not run: Python >= 3.11 is required; found Python 3.10 at {unsupported_python}\n"
    )
    assert not (output_dir / "discovery_runs.jsonl").exists()
    assert not (output_dir / "Current_Profile_Recommended_Queue.csv").exists()


def test_generated_launchd_bootstrap_has_no_legacy_directory_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script_path = PROJECT_ROOT / "job_profession" / "scripts" / "install_launch_agent.py"
    spec = importlib.util.spec_from_file_location("install_launch_agent_for_test", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bootstrap_path = tmp_path / "launchd_discover_bootstrap.sh"
    monkeypatch.setattr(module, "RUNTIME_BOOTSTRAP", bootstrap_path)

    module._write_runtime_bootstrap()

    bootstrap = bootstrap_path.read_text(encoding="utf-8")
    assert ".discovery.lock" not in bootstrap
    assert "LOCK_DIR=" not in bootstrap
    assert "discover.py" in bootstrap
    assert "--export-current-recommendations" in bootstrap
    assert "Current_Profile_Recommended_Queue.csv" in bootstrap


def test_scheduler_redacts_adapter_and_payload_exception_details_from_audit_output(tmp_path: Path):
    secret = "candidate-private-token"
    search = SearchProfile("linkedin", "Python Backend Developer")

    class FailingAdapter:
        def read_visible_listings(self, _search_url: str):
            raise RuntimeError(f"adapter exposed {secret}")

    failed = JobDiscoveryScheduler(
        profile(),
        [search],
        state_path=tmp_path / "failed-state.json",
        export_path=tmp_path / "failed-jobs.jsonl",
        run_log_path=tmp_path / "failed-runs.jsonl",
    ).run(FailingAdapter())

    assert failed.errors == ("linkedin adapter read failed: RuntimeError",)
    assert secret not in (tmp_path / "failed-runs.jsonl").read_text(encoding="utf-8")

    invalid_payload = MappingVisiblePageAdapter(
        {
            build_search_url(search): [
                {
                    "title": "Python Backend Developer",
                    "url": f"https://www.linkedin.com/jobs/view/1?access_token={secret}",
                }
            ]
        }
    )
    rejected = JobDiscoveryScheduler(
        profile(),
        [search],
        state_path=tmp_path / "rejected-state.json",
        export_path=tmp_path / "rejected-jobs.jsonl",
        run_log_path=tmp_path / "rejected-runs.jsonl",
    ).run(invalid_payload)

    assert rejected.errors == ("linkedin visible payload rejected: ValueError",)
    assert secret not in (tmp_path / "rejected-runs.jsonl").read_text(encoding="utf-8")


def test_profile_revision_includes_employment_types_and_work_authorizations():
    base = CandidateProfile(
        professional_skills=("python",),
        employment_type_preferences=("full-time",),
        work_authorizations=("india",),
        evidence_by_skill={"python": "professional"},
    )
    changed_employment = CandidateProfile(
        professional_skills=("python",),
        employment_type_preferences=("contract",),
        work_authorizations=("india",),
        evidence_by_skill={"python": "professional"},
    )
    changed_authorization = CandidateProfile(
        professional_skills=("python",),
        employment_type_preferences=("full-time",),
        work_authorizations=("uae",),
        evidence_by_skill={"python": "professional"},
    )

    revision = candidate_profile_revision(base)

    assert revision != candidate_profile_revision(changed_employment)
    assert revision != candidate_profile_revision(changed_authorization)


def test_portable_stale_recovery_never_deletes_a_replaced_fresh_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    lock_path = tmp_path / "scheduler.lock"
    claim_path = scheduler_module._portable_claim_path(lock_path)
    claim_path.write_text(
        json.dumps({"pid": 999_999_999, "token": "stale-token", "acquired_at": 0}),
        encoding="utf-8",
    )
    fresh_claim = {"pid": os.getpid(), "token": "fresh-token", "acquired_at": scheduler_module.time.time()}
    real_stale_check = scheduler_module._portable_claim_is_stale

    def replace_after_stale_check(path: Path, snapshot: object | None = None) -> bool:
        assert real_stale_check(path, snapshot=snapshot) is True
        claim_path.write_text(json.dumps(fresh_claim), encoding="utf-8")
        return True

    monkeypatch.setattr(scheduler_module, "_portable_claim_is_stale", replace_after_stale_check)

    assert scheduler_module._try_portable_claim(lock_path, "contender-token") is False
    assert json.loads(claim_path.read_text(encoding="utf-8"))["token"] == "fresh-token"
