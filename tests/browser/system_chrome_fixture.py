"""Prepare only synthetic five-slot Smart Queue data for the live-CDP harness."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "jobapply_agent" / "src"))

URLS = tuple(
    f"https://www.linkedin.com/jobs/view/9900{n:02d}" if n % 2 else f"https://www.indeed.com/viewjob?jk=synthetic{n:08d}"
    for n in range(1, 9)
)

def discover():
    spec = importlib.util.spec_from_file_location("system_chrome_fixture_discover", ROOT / "jobapply_agent/scripts/discover.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def prepare(runtime: Path) -> dict[str, object]:
    if runtime.exists() and any(runtime.iterdir()):
        raise ValueError("synthetic runtime must be empty")
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    d = discover()
    from jobapply_agent.intake import activate_candidate_profile, validate_candidate_intake
    intake = runtime / "candidate-intake.json"
    export = runtime / "discovery.jsonl"
    approved = ["Python", "FastAPI", "REST APIs", "PostgreSQL", "unit testing"]
    raw = {"schema_version": 1, "documents": [], "approved_facts": {"experience":{"total_years":3}, "roles":{"include":["Python Backend Developer"],"exclude_title_terms":["senior"]}, "skills":{"professional":approved,"personal_open_source":["Synthetic Open Source"],"learning_or_exposure":["Synthetic Learning"],"evidence_by_skill":{x:"professional" for x in approved}}, "targets":{"smart_queue_capacity":5}}, "unknown_fields":[],"contradictions":[],"pending_facts":[]}
    intake.write_text(json.dumps(activate_candidate_profile(validate_candidate_intake(raw), actor="user")))
    profile = d.active_candidate_profile(intake)
    pr = d.candidate_profile_revision(profile)
    mr = d.matcher_policy_revision()
    rows = [{"schema_version":2,"record_type":"recommended_job_for_human_review","discovery_mode":"export_only","application_actions":0,"fingerprint":hashlib.sha256(url.encode()).hexdigest(),"profile_revision":pr,"matcher_policy_revision":mr,"run_id":"system-chrome-synthetic","discovered_at":"2026-09-08T00:00:00+00:00","search_url":"https://example.invalid/synthetic","platform":"linkedin" if "linkedin" in url else "indeed","title":"Python Backend Developer","company":f"Synthetic {i}","url":url,"location":"","work_mode":"","posted_at":None,"score":95,"decision":"recommended","minimum_profile_fit_score":85,"threshold_met":True,"reasons":["synthetic"],"gaps":[],"evidence_explanations":["synthetic"],"score_explanation":"synthetic","human_action_required":"candidate reviews manually"} for i,url in enumerate(URLS,1)]
    export.write_text("".join(json.dumps(row)+"\n" for row in rows))
    queue, memory = runtime / "smart-queue.sqlite3", runtime / "candidate-memory.sqlite3"
    d.admit_current_recommendations_for_active_queue(intake, export, queue, memory)
    return {"intake":str(intake),"queue":str(queue),"memory":str(memory),"urls":URLS}

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--prepare", type=Path, required=True)
    print(json.dumps(prepare(p.parse_args().prepare)))
