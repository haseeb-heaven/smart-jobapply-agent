"""Offline process-level host -> discovery -> admission -> daemon evidence.

Only the public provider and URL-only browser transport are inert fixtures.
The actual intake, matching policy, durable queue, suppression memory, and
admission CLI run unchanged. No browser or network is accessed.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

import pytest

from jobapply_agent.intake import activate_candidate_profile, validate_candidate_intake
from jobapply_agent.smart_queue import SmartJobQueue


ROOT = Path(__file__).parents[1]
HOST = ROOT / "skills/easy-apply-tab-monitor/scripts/external_smart_queue_assistant.py"
PRIVATE = ROOT / "jobapply_agent/private"


@pytest.mark.parametrize(("eligible", "failure"), [
    (True, "none"), (False, "none"), (True, "malformed"), (True, "timeout"),
    (True, "mixed-invalid-row"),
], ids=["five-refill", "all-ineligible", "malformed-recovery", "timeout-recovery", "mixed-invalid-row"])
def test_actual_host_discovers_admits_and_refills_five_without_inferred_outcomes(eligible: bool, failure: str) -> None:
    PRIVATE.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="synthetic-host-integration-", dir=PRIVATE) as directory:
        runtime = Path(directory)
        intake, queue, memory = (runtime / name for name in ("intake.json", "queue.sqlite3", "memory.sqlite3"))
        professional = ["Python", "FastAPI", "REST APIs", "PostgreSQL", "unit testing"]
        draft = validate_candidate_intake({
            "schema_version": 1, "documents": [], "approved_facts": {
                "experience": {"total_years": 3},
                "roles": {"include": ["Python Backend Developer"], "exclude_title_terms": ["senior"]},
                "skills": {"professional": professional, "personal_open_source": ["Synthetic Open Source"],
                           "learning_or_exposure": ["Synthetic Learning"],
                           "evidence_by_skill": {skill: "professional" for skill in professional}},
                "targets": {"smart_queue_capacity": 5},
            }, "unknown_fields": [], "contradictions": [], "pending_facts": [],
        })
        intake.write_text(json.dumps(activate_candidate_profile(draft, actor="user")))
        tabs = runtime / "synthetic-tabs.json"
        tabs.write_text(json.dumps({"tabs": [], "next_id": 1, "opened": []}))
        bridge = runtime / "bridge.py"
        bridge.write_text('''import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
s = json.loads(p.read_text())
if sys.argv[2] == 'list-tabs':
    print(json.dumps([t['url'] for t in s['tabs']]))
elif sys.argv[2] == 'open-listing':
    url = sys.argv[3]
    assert url.startswith('https://www.linkedin.com/jobs/view/9900')
    s['tabs'].append({'id': s['next_id'], 'url': url})
    s['next_id'] += 1
    s['opened'].append(url)
    p.write_text(json.dumps(s))
else:
    sys.exit(2)
''')
        calls = runtime / "provider-calls.json"
        calls.write_text("[]")
        provider = runtime / "provider.py"
        provider.write_text('''import json, pathlib, sys, time
p = pathlib.Path(sys.argv[1])
calls = json.loads(p.read_text())
request = json.load(sys.stdin)
assert set(request) == {'schema_version', 'limit', 'queries'}
assert request['limit'] == 20
assert all(set(q) == {'query_id','platform','keywords','location'} for q in request['queries'])
query = next(q for q in request['queries'] if q['platform'] == 'linkedin')
calls.append(request)
p.write_text(json.dumps(calls))
if len(calls) == 2 and sys.argv[3] == 'timeout':
    time.sleep(10)
numbers = range(1, 6) if len(calls) == 1 else range(6, 9)
listings = [{'query_id': query['query_id'], 'platform': 'linkedin',
    'url': f'https://www.linkedin.com/jobs/view/9900{n:02d}',
    'title': sys.argv[2], 'company': f'Synthetic Company {n}',
    'description': 'Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL REST APIs.',
    'location': '', 'work_mode': '', 'employment_type': '',
    'posted_at': None, 'source_job_id': str(990000+n)} for n in numbers]
if len(calls) == 2 and sys.argv[3] == 'malformed':
    listings[-1]['unexpected_field'] = 'reject-entire-batch'
if len(calls) == 2 and sys.argv[3] == 'mixed-invalid-row':
    listings[-1]['url'] = 'https://www.linkedin.com/jobs/view/synthetic%20slug-990008'
print(json.dumps({'schema_version': 1, 'listings': listings}))
''')
        command = [sys.executable, str(HOST), "--candidate-intake", str(intake),
                   "--queue-db", str(queue), "--memory-db", str(memory),
                   "--bridge-command", sys.executable, str(bridge), str(tabs),
                   "--provider-command", sys.executable, str(provider), str(calls),
                   "Python Backend Developer" if eligible else "Senior Python Backend Developer", failure,
                   "--provider-timeout-seconds", "1", "--backoff-seconds", "0.01", "--max-cycles", "1"]

        def run_host() -> dict:
            result = subprocess.run(command, capture_output=True, text=True, timeout=20, cwd=ROOT)
            assert result.stderr == ""
            assert "https://" not in result.stdout and str(runtime) not in result.stdout
            statuses = [json.loads(line) for line in result.stdout.splitlines()]
            provider_invocations = len(json.loads(calls.read_text()))
            expected_search_needed = (1 if failure == "mixed-invalid-row" and provider_invocations > 1 else 0) if eligible else 5
            assert statuses[-1]["search_needed"] == expected_search_needed
            expected_state = "complete" if expected_search_needed == 0 else "incomplete"
            assert statuses[-1]["state"] == expected_state
            assert result.returncode == (0 if expected_state == "complete" else 1), (result.stdout, result.stderr)
            if statuses[0]["state"] == "degraded":
                assert statuses[0] == {"state": "degraded", "cycle": 1, "rounds_attempted": 1,
                                       "provider_listing_count": 0, "admitted_count": 0,
                                       "suppressed_count": 0, "opened_count": 0, "search_needed": 3}
                assert statuses[1]["cycle"] == 2
                return statuses[1]
            return statuses[0]

        first = run_host()
        if not eligible:
            assert first["state"] == "no_progress"
            assert first["provider_listing_count"] == 5
            assert first["admitted_count"] == first["opened_count"] == 0
            assert json.loads(tabs.read_text())["tabs"] == []
            with sqlite3.connect(queue) as connection:
                assert connection.execute("SELECT count(*) FROM smart_queue_jobs").fetchone()[0] == 0
            assert not memory.exists()  # No admission or suppression mutation was necessary.
            assert SmartJobQueue(queue).confirmed_outcome_events() == ()
            return
        assert (first["admitted_count"], first["opened_count"]) == (5, 5)
        initial = json.loads(tabs.read_text())
        assert len(initial["tabs"]) == 5
        removed = {tab["id"] for tab in initial["tabs"][:3]}
        released_urls = {tab["url"] for tab in initial["tabs"] if tab["id"] in removed}
        initial["tabs"] = [tab for tab in initial["tabs"] if tab["id"] not in removed]
        retained_ids = {tab["id"] for tab in initial["tabs"]}
        tabs.write_text(json.dumps(initial))  # Synthetic candidate closes three exact owned IDs.
        if failure in {"malformed", "timeout"}:
            command[-1] = "2"  # One persistent process survives the failed first provider attempt.
        second = run_host()
        expected_refill = 2 if failure == "mixed-invalid-row" else 3
        assert (second["admitted_count"], second["opened_count"]) == (expected_refill, expected_refill)
        final = json.loads(tabs.read_text())
        assert len(final["tabs"]) == 2 + expected_refill
        assert retained_ids <= {tab["id"] for tab in final["tabs"]}
        assert not released_urls & {tab["url"] for tab in final["tabs"]}
        assert len(final["opened"]) == len(set(final["opened"])) == 5 + expected_refill
        if failure == "mixed-invalid-row":
            rejected_url = "https://www.linkedin.com/jobs/view/synthetic%20slug-990008"
            assert rejected_url not in final["opened"]
            with sqlite3.connect(queue) as connection:
                staged_urls = {row[0] for row in connection.execute("SELECT source_url FROM smart_queue_jobs")}
            assert rejected_url not in staged_urls
        expected_calls = 2 if failure in {"none", "mixed-invalid-row"} else 3
        assert len(json.loads(calls.read_text())) == expected_calls
        with sqlite3.connect(queue) as connection:
            job_ids = [row[0] for row in connection.execute("SELECT job_id FROM smart_queue_jobs")]
        durable = SmartJobQueue(queue)
        expected_open = 2 + expected_refill
        assert Counter(durable.get(job_id).state for job_id in job_ids) == {"open": expected_open, "released": 3}
        assert durable.confirmed_outcome_events() == ()
        with sqlite3.connect(memory) as connection:
            assert connection.execute("SELECT count(*) FROM candidate_memory_outcomes").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM candidate_memory_queue_scope").fetchone()[0] == 1
