"""Cross-platform and URL-boundary security regressions.

The scheduler portability check runs in an isolated child interpreter so the
suite never mutates this process's import machinery. No browser, network, or
candidate-private file is used.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from job_profession.smart_queue import QueueCandidate, QueuePolicyError
from job_profession.sources import _validate_listing_url


PROJECT_ROOT = Path(__file__).parents[1]
PACKAGE_ROOT = PROJECT_ROOT / "job_profession" / "src"


@pytest.mark.parametrize(
    ("platform", "url"),
    (
        ("linkedin", "https://user@www.linkedin.com/jobs/view/123"),
        ("linkedin", "https://user:password@www.linkedin.com/jobs/view/123"),
        ("linkedin", "https://www.linkedin.com/jobs/view/123#application"),
        ("linkedin", "https://www.linkedin.com/jobs/view/123?access_token=secret"),
        ("linkedin", "https://www.linkedin.com/jobs/view/123?session=secret"),
        ("indeed", "https://in.indeed.com/viewjob?jk=abc_123&password=secret"),
        ("linkedin", "https://www.linkedin.com/login"),
        ("linkedin", "https://www.linkedin.com/jobs/search/?keywords=python"),
        ("linkedin", "https://www.linkedin.com/jobs/view/123/apply/"),
        ("indeed", "https://in.indeed.com/account/login"),
        ("indeed", "https://in.indeed.com/jobs?q=python"),
        ("indeed", "https://in.indeed.com/viewjob"),
        ("indeed", "https://in.indeed.com/viewjob?jk=abc_123#apply"),
        ("indeed", "https://smartapply.indeed.com/beta/indeedapply/form?jk=abc_123"),
    ),
)
def test_visible_listing_boundary_rejects_credentials_and_noncanonical_routes(platform: str, url: str):
    with pytest.raises(ValueError):
        _validate_listing_url(url, platform)


@pytest.mark.parametrize(
    ("platform", "url"),
    (
        (
            "linkedin",
            "https://www.linkedin.com/jobs/view/123?trk=public_jobs_topcard-title&utm_source=agent",
        ),
        (
            "indeed",
            "https://in.indeed.com/viewjob?jk=abc_123&from=shareddesktop&utm_source=agent",
        ),
    ),
)
def test_visible_listing_boundary_accepts_canonical_listings_with_safe_tracking(platform: str, url: str):
    _validate_listing_url(url, platform)


def _queue_candidate(url: str) -> QueueCandidate:
    return QueueCandidate(
        job_id="synthetic-job",
        source_url=url,
        fit_score=90,
        eligible=True,
        decision="recommended",
        evidence=("synthetic verified professional evidence",),
        profile_revision="portability-profile-v1",
        matcher_policy_revision="portability-policy-v1",
    )


@pytest.mark.parametrize(
    "url",
    (
        "https://user@www.linkedin.com/jobs/view/123",
        "https://user:password@www.linkedin.com/jobs/view/123",
        "https://www.linkedin.com/jobs/view/123#application",
        "https://www.linkedin.com/jobs/view/123?access_token=secret",
        "https://www.linkedin.com/jobs/view/123?session=secret",
        "https://www.linkedin.com/login",
        "https://www.linkedin.com/jobs/search/?keywords=python",
        "https://www.linkedin.com/jobs/view/123/apply/",
        "https://in.indeed.com/viewjob?jk=abc_123&password=secret",
        "https://in.indeed.com/account/login",
        "https://in.indeed.com/jobs?q=python",
        "https://in.indeed.com/viewjob",
        "https://in.indeed.com/viewjob?jk=abc_123#apply",
        "https://smartapply.indeed.com/beta/indeedapply/form?jk=abc_123",
    ),
)
def test_smart_queue_rejects_credentials_and_noncanonical_listing_routes(url: str):
    with pytest.raises(QueuePolicyError):
        _queue_candidate(url)


@pytest.mark.parametrize(
    "url",
    (
        "https://www.linkedin.com/jobs/view/123?trk=public_jobs_topcard-title&utm_source=agent",
        "https://in.indeed.com/viewjob?jk=abc_123&from=shareddesktop&utm_source=agent",
    ),
)
def test_smart_queue_accepts_canonical_listings_with_safe_tracking(url: str):
    candidate = _queue_candidate(url)

    assert candidate.source_url.startswith("https://")
    assert "utm_source" not in candidate.source_url


def test_scheduler_imports_and_runs_with_a_portable_lock_when_fcntl_is_unavailable(tmp_path: Path):
    child_code = r"""
import builtins
import json
from pathlib import Path
import sys

package_root = Path(sys.argv[1])
output_root = Path(sys.argv[2])
sys.path.insert(0, str(package_root))
real_import = builtins.__import__

def without_fcntl(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "fcntl":
        raise ModuleNotFoundError("simulated Windows: fcntl unavailable", name="fcntl")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = without_fcntl

from job_profession.models import CandidateProfile
from job_profession.scheduler import JobDiscoveryScheduler
from job_profession.sources import MappingVisiblePageAdapter

scheduler = JobDiscoveryScheduler(
    CandidateProfile(),
    (),
    state_path=output_root / "state.json",
    export_path=output_root / "jobs.jsonl",
    run_log_path=output_root / "runs.jsonl",
)
result = scheduler.run(MappingVisiblePageAdapter({}))
lock_files = sorted(path.name for path in output_root.glob("*.lock") if path.is_file())
print(json.dumps({"actions": result.application_actions, "locks": lock_files}))
"""

    completed = subprocess.run(
        [sys.executable, "-I", "-c", child_code, str(PACKAGE_ROOT), str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["actions"] == 0
    assert result["locks"]
