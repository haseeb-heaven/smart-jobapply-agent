"""Synthetic browser-test data only; never reads candidate or browser state.

This module is deliberately a fixture seam rather than a runtime launcher. It
provides canonical approved listing URLs for browser tests and labels all
closures/outcome-like values as simulations.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


@dataclass(frozen=True, slots=True)
class SyntheticQueueCycle:
    capacity: int
    listing_urls: tuple[str, ...]
    simulated_actor: str = "test-fixture"
    inferred_outcome_count: int = 0


def synthetic_queue_cycle() -> SyntheticQueueCycle:
    """Return a deterministic, approved-url-only capacity-two test fixture."""

    return SyntheticQueueCycle(
        capacity=2,
        listing_urls=(
            "https://www.linkedin.com/jobs/view/910001",
            "https://www.linkedin.com/jobs/view/910002",
            "https://www.linkedin.com/jobs/view/910003",
        ),
    )


def _discover():
    root = Path(__file__).parents[2]
    path = root / "jobapply_agent" / "scripts" / "discover.py"
    spec = importlib.util.spec_from_file_location("browser_fixture_discover", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def prepare(runtime: Path) -> dict[str, str]:
    """Build an active synthetic intake and admit three deterministic rows."""

    package_source = Path(__file__).parents[2] / "jobapply_agent" / "src"
    if str(package_source) not in sys.path:
        sys.path.insert(0, str(package_source))
    runtime.mkdir(mode=0o700, parents=True, exist_ok=False)
    intake = runtime / "candidate-intake.json"
    export = runtime / "discovery.jsonl"
    queue = runtime / "smart-queue.sqlite3"
    memory = runtime / "candidate-memory.sqlite3"
    professional = ["Python", "FastAPI", "REST APIs", "PostgreSQL", "unit testing"]
    payload = {
        "schema_version": 1, "documents": [],
        "approved_facts": {
            "experience": {"total_years": 3},
            "roles": {"include": ["Python Backend Developer"], "exclude_title_terms": ["senior"]},
            "skills": {"professional": professional, "personal_open_source": ["Synthetic Open Source"], "learning_or_exposure": ["Synthetic Learning"],
                       "evidence_by_skill": {skill: "professional" for skill in professional}},
            "targets": {"smart_queue_capacity": 2},
        },
        "unknown_fields": [], "contradictions": [], "pending_facts": [],
    }
    from jobapply_agent.intake import activate_candidate_profile, validate_candidate_intake

    intake.write_text(json.dumps(activate_candidate_profile(validate_candidate_intake(payload), actor="user")), encoding="utf-8")
    discover = _discover()
    profile = discover.active_candidate_profile(intake)
    profile_revision = discover.candidate_profile_revision(profile)
    policy_revision = discover.matcher_policy_revision()
    rows = []
    for number in range(1, 4):
        rows.append({
            "schema_version": 2, "record_type": "recommended_job_for_human_review", "discovery_mode": "export_only",
            "application_actions": 0, "fingerprint": hashlib.sha256(f"browser-fixture-{number}".encode()).hexdigest(),
            "profile_revision": profile_revision, "matcher_policy_revision": policy_revision,
            "run_id": "browser-fixture", "discovered_at": "2026-09-08T00:00:00+00:00",
            "search_url": "https://www.linkedin.com/jobs/search/?keywords=python", "platform": "linkedin",
            "title": "Python Backend Developer", "company": f"Synthetic {number}",
            "url": f"https://www.linkedin.com/jobs/view/91000{number}", "location": "", "work_mode": "", "posted_at": None,
            "score": 95, "decision": "recommended", "minimum_profile_fit_score": 85, "threshold_met": True,
            "reasons": ["synthetic fixture"], "gaps": [], "evidence_explanations": ["synthetic fixture"],
            "score_explanation": "synthetic fixture", "human_action_required": "candidate reviews manually",
        })
    export.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    discover.admit_current_recommendations_for_active_queue(intake, export, queue, memory)
    return {"intake": str(intake), "queue": str(queue), "memory": str(memory)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.prepare), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
