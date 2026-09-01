import importlib.util
import json
from pathlib import Path
import sys

import pytest


_SCRIPT_PATH = Path(__file__).parents[1] / "skills" / "easy-apply-tab-monitor" / "scripts" / "tab_manifest.py"
_SPEC = importlib.util.spec_from_file_location("tab_manifest", _SCRIPT_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
validate_jobs = _MODULE.validate_jobs
missing = _MODULE.missing


def _job(url: str, *, apply_path: str = "easy_apply") -> dict[str, str]:
    return {"title": "Python Backend", "company": "Example", "platform": "linkedin", "url": url, "apply_path": apply_path}


def test_accepts_at_most_five_supported_jobs():
    jobs = validate_jobs([_job(f"https://www.linkedin.com/jobs/view/{index}") for index in range(5)])
    assert len(jobs) == 5


def test_rejects_sixth_job():
    with pytest.raises(ValueError, match="between one and five"):
        validate_jobs([_job(f"https://www.linkedin.com/jobs/view/{index}") for index in range(6)])


def test_rejects_external_host_and_duplicate_url():
    with pytest.raises(ValueError, match="HTTPS LinkedIn or Indeed"):
        validate_jobs([_job("https://example.test/jobs/1")])
    with pytest.raises(ValueError, match="unique"):
        validate_jobs([_job("https://www.linkedin.com/jobs/view/1"), _job("https://www.linkedin.com/jobs/view/1")])


def test_rejects_platform_host_mismatch():
    with pytest.raises(ValueError, match="match the job URL host"):
        validate_jobs([{**_job("https://www.linkedin.com/jobs/view/1"), "platform": "indeed"}])


def test_missing_returns_only_urls_not_observed(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [_job("https://www.linkedin.com/jobs/view/1"), {**_job("https://in.indeed.com/viewjob?jk=2", apply_path="apply_with_indeed"), "platform": "indeed"}]}))
    assert missing(manifest, ["https://www.linkedin.com/jobs/view/1"]) == ["https://in.indeed.com/viewjob?jk=2"]
