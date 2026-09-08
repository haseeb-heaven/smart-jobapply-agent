"""Portable-runtime durability contracts using synthetic queue data only.

The tests cover restart-visible state and durable candidate-memory suppression;
they do not start a browser or treat an absent tab as an application outcome.
"""

from __future__ import annotations

from pathlib import Path

from jobapply_agent.candidate_memory import CandidateMemory
from jobapply_agent.smart_queue import QueueCandidate, SmartJobQueue


PROFILE_REVISION = "portable-recovery-profile-v1"
POLICY_REVISION = "portable-recovery-policy-v1"


def _candidate(number: int) -> QueueCandidate:
    return QueueCandidate(
        job_id=f"portable-job-{number}",
        source_url=f"https://www.linkedin.com/jobs/view/9300{number}",
        fit_score=95,
        eligible=True,
        decision="recommended",
        evidence=("synthetic approved evidence",),
        profile_revision=PROFILE_REVISION,
        matcher_policy_revision=POLICY_REVISION,
    )


def _queue(path: Path) -> tuple[SmartJobQueue, tuple[QueueCandidate, ...]]:
    queue = SmartJobQueue(path, target_size=2)
    candidates = (_candidate(1), _candidate(2), _candidate(3))
    queue.add_recommendations(candidates)
    return queue, candidates


def test_restart_preserves_visible_open_jobs_and_only_refills_a_released_slot(tmp_path: Path):
    database = tmp_path / "smart-queue.sqlite3"
    queue, candidates = _queue(database)
    initial = queue.plan_refill(open_urls=())
    queue.record_visible_snapshot(initial.urls_to_open, actor="synthetic-browser-bridge")

    restarted = SmartJobQueue(database)
    assert restarted.plan_refill(open_urls=initial.urls_to_open).job_ids == ()

    remaining = (candidates[1].source_url,)
    restarted.record_visible_snapshot(remaining, actor="synthetic-browser-bridge")
    assert restarted.get(candidates[0].job_id).state == "released"
    assert restarted.confirmed_outcome_events() == ()

    refill = restarted.plan_refill(open_urls=remaining)
    assert refill.job_ids == (candidates[2].job_id,)
    assert refill.urls_to_open == (candidates[2].source_url,)


def test_explicit_outcome_survives_restart_and_suppresses_exact_canonical_url(tmp_path: Path):
    database = tmp_path / "smart-queue.sqlite3"
    memory_path = tmp_path / "private" / "candidate-memory.sqlite3"
    queue, candidates = _queue(database)
    initial = queue.plan_refill(open_urls=())
    queue.record_visible_snapshot(initial.urls_to_open, actor="synthetic-browser-bridge")
    memory = CandidateMemory(memory_path, private_root=memory_path.parent)
    memory.filter_unsuppressed_candidates(candidates, queue=queue)

    queue.record_visible_snapshot((candidates[1].source_url,), actor="synthetic-browser-bridge")
    queue.confirm_outcome(
        candidates[0].job_id,
        "skipped",
        actor="user",
        vacated=True,
        candidate_memory=memory,
    )

    reloaded_memory = CandidateMemory(memory_path, private_root=memory_path.parent)
    reloaded_queue = SmartJobQueue(database)
    assert reloaded_memory.is_suppressed(candidates[0].source_url) is True
    assert reloaded_memory.filter_unsuppressed_candidates(candidates, queue=reloaded_queue) == candidates[1:]
    assert reloaded_queue.get(candidates[0].job_id).state == "skipped"
