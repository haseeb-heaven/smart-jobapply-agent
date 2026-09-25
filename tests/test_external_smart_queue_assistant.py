"""Adversarial contract tests for the bounded external replenishment host.

All paths and listing facts are synthetic.  Browser/provider processes are inert
fixtures and no candidate-private database or real browser session is used.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import subprocess
import time

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "skills/easy-apply-tab-monitor/scripts/external_smart_queue_assistant.py"
SPEC = importlib.util.spec_from_file_location("external_smart_queue_assistant_test", SCRIPT)
assert SPEC and SPEC.loader
assistant = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = assistant
SPEC.loader.exec_module(assistant)


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    private = tmp_path / "private"
    private.mkdir()
    intake = private / "candidate_intake.json"
    intake.write_text("{}", encoding="utf-8")
    queue = private / "smart-queue.sqlite3"
    memory = private / "candidate-memory.sqlite3"
    monkeypatch.setattr(assistant, "PRIVATE_ROOT", private)
    return intake, queue, memory


def _args(runtime: tuple[Path, Path, Path], *extra: str) -> list[str]:
    intake, queue, memory = runtime
    return [
        "--candidate-intake", str(intake), "--queue-db", str(queue),
        "--memory-db", str(memory), "--bridge-command", "synthetic-bridge",
        "--provider-command", "synthetic-provider", "--max-cycles", "1", *extra,
    ]


def _listing(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "query_id": "q0", "platform": "indeed",
        "url": "https://www.indeed.com/viewjob?jk=synthetic001",
        "title": "Python Backend Developer", "company": "Synthetic Company",
        "description": "Maintain Python APIs and unit tests.",
    }
    value.update(changes)
    return value


def _batch(*listings: dict[str, object], **extra: object) -> str:
    return json.dumps({"schema_version": 1, "listings": list(listings), **extra})


def _daemon_status(*, shortage: int, opened: int = 0) -> dict[str, int]:
    return {
        "ticks_completed": 1, "requested_open_count": opened,
        "opened_count": opened, "open_failed_count": 0,
        "search_needed": shortage, "degraded_tick_count": 0,
    }


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("not-json", "provider_schema_invalid"),
        (_batch(_listing(score=100)), "provider_schema_invalid"),
        (_batch(_listing(candidate_intake="/private/person.json")), "provider_schema_invalid"),
        (_batch(_listing(), _listing()), "provider_duplicate"),
        (_batch(_listing(query_id="q99")), "provider_schema_invalid"),
    ],
)
def test_provider_rejects_malformed_private_scored_unsupported_and_duplicate_batches(
    raw: str, reason: str,
) -> None:
    with pytest.raises(assistant.HostFailure, match=f"^{reason}$"):
        assistant._provider_listings(raw, {"q0": ("https://www.indeed.com/jobs?q=python", "indeed")}, set())


def test_provider_filters_an_already_managed_canonical_url_without_mutating_history() -> None:
    url = "https://www.indeed.com/viewjob?jk=synthetic001"
    pages, accepted = assistant._provider_listings(
        _batch(_listing(url=url)), {"q0": ("https://www.indeed.com/jobs?q=python", "indeed")}, {url}
    )
    assert accepted == 0
    assert pages == {"https://www.indeed.com/jobs?q=python": []}


def test_provider_quarantines_unsupported_hosts_and_invalid_listing_routes_per_row() -> None:
    valid = _listing()
    unsupported_host = _listing(url="https://example.invalid/jobs/one")
    invalid_route = _listing(url="https://www.indeed.com/jobs?q=python")
    invalid_query = _listing(url="https://www.indeed.com/viewjob?jk=synthetic001&apply=1")
    pages, accepted = assistant._provider_listings(
        _batch(valid, unsupported_host, invalid_route, invalid_query),
        {"q0": ("https://www.indeed.com/jobs?q=python", "indeed")}, set(),
    )
    assert accepted == 1
    assert pages == {
        "https://www.indeed.com/jobs?q=python": [
            {**{key: valid.get(key, "") for key in (
                "title", "company", "description", "location", "work_mode",
                "employment_type", "posted_at", "source_job_id", "discovered_at",
            )}, "url": "https://www.indeed.com/viewjob?jk=synthetic001"}
        ]
    }


@pytest.mark.parametrize("bad", [
    _listing(query_id="q9"),
    _listing(platform="linkedin"),
])
def test_provider_schema_query_or_platform_binding_still_rejects_the_whole_batch(
    bad: dict[str, object],
) -> None:
    with pytest.raises(assistant.HostFailure, match="^provider_schema_invalid$"):
        assistant._provider_listings(
            _batch(_listing(), bad),
            {"q0": ("https://www.indeed.com/jobs?q=python", "indeed")}, set(),
        )


def test_provider_schema_version_error_still_rejects_batch_with_valid_row() -> None:
    malformed_schema = json.dumps({"schema_version": 2, "listings": [_listing()]})
    with pytest.raises(assistant.HostFailure, match="^provider_schema_invalid$"):
        assistant._provider_listings(
            malformed_schema,
            {"q0": ("https://www.indeed.com/jobs?q=python", "indeed")}, set(),
        )


def test_provider_duplicate_supported_identity_fails_even_when_managed() -> None:
    url = "https://www.indeed.com/viewjob?jk=synthetic001"
    with pytest.raises(assistant.HostFailure, match="^provider_duplicate$"):
        assistant._provider_listings(
            _batch(_listing(url=url), _listing(url=url)),
            {"q0": ("https://www.indeed.com/jobs?q=python", "indeed")}, {url},
        )


def test_host_admits_only_supported_rows_and_does_not_tick_browser_for_quarantined_rows(
    runtime: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    query_urls = {"q0": ("https://www.indeed.com/jobs?q=python", "indeed")}
    invalid_rows = [
        _listing(url="https://outside.invalid/job/1"),
        _listing(url="https://www.indeed.com/jobs?q=python"),
        _listing(url="https://www.indeed.com/viewjob?jk=synthetic002&application=1"),
    ]
    events: list[str] = []
    monkeypatch.setattr(assistant, "_queries", lambda location: ([{"query_id": "q0", "platform": "indeed", "keywords": "python", "location": location}], query_urls))
    monkeypatch.setattr(assistant, "_managed_urls", lambda queue: set())
    monkeypatch.setattr(assistant, "_daemon", lambda *a, **k: events.append("daemon") or _daemon_status(shortage=1))
    monkeypatch.setattr(assistant, "_run", lambda *a, **k: events.append("provider") or _batch(_listing(), *invalid_rows))

    def admit(intake, queue, memory, pages, timeout):
        events.append("admit")
        rows = pages[query_urls["q0"][0]]
        assert [row["url"] for row in rows] == [_listing()["url"]]
        return {"validated_count": 1, "admitted_count": 0, "suppressed_count": 0}

    monkeypatch.setattr(assistant, "_admit", admit)
    assert assistant.main(_args(runtime)) == 1
    frames = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events == ["daemon", "provider", "admit"]
    assert frames[0]["provider_listing_count"] == 1
    assert frames[0]["admitted_count"] == 0
    assert frames[1]["state"] == "incomplete"
    assert frames[1]["search_needed"] == 1


def test_provider_platform_must_match_the_host_owned_query_profile() -> None:
    with pytest.raises(assistant.HostFailure, match="^provider_schema_invalid$"):
        assistant._provider_listings(
            _batch(_listing(platform="linkedin", url="https://www.linkedin.com/jobs/view/12345")),
            {"q0": ("https://www.indeed.com/jobs?q=python", "indeed")}, set(),
        )


def test_managed_filter_includes_released_historical_urls(tmp_path: Path) -> None:
    database = tmp_path / "queue.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE smart_queue_jobs (source_url TEXT, state TEXT)")
        connection.execute(
            "INSERT INTO smart_queue_jobs VALUES (?, ?)",
            ("https://www.indeed.com/viewjob?jk=historical001", "released"),
        )
    assert assistant._managed_urls(database) == {
        "https://www.indeed.com/viewjob?jk=historical001"
    }


def test_private_runtime_rejects_dotdot_and_symlink_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = tmp_path / "private"
    outside = tmp_path / "outside"
    private.mkdir()
    outside.mkdir()
    (private / "link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(assistant, "PRIVATE_ROOT", private)
    for escaped in (private / ".." / "outside" / "queue.sqlite3", private / "link" / "queue.sqlite3"):
        with pytest.raises(assistant.HostFailure, match="^invalid_private_runtime$"):
            assistant._private_file(escaped, existing=False)


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_process_bounds_reject_nonfinite_numbers(value: float) -> None:
    with pytest.raises(assistant.HostFailure):
        assistant._positive(value, "timeout", allow_zero=True)


@pytest.mark.parametrize(
    "output",
    ["{}", json.dumps({**_daemon_status(shortage=1), "url": "https://private.invalid"}),
     json.dumps({**_daemon_status(shortage=1), "search_needed": True})],
)
def test_daemon_status_is_exact_count_only_json(output: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assistant, "_run", lambda *args, **kwargs: output)
    with pytest.raises(assistant.HostFailure, match="^invalid_child_status$"):
        assistant._daemon(("python", "daemon"), Path("intake"), Path("queue"), ("bridge",), 1)


def test_synthetic_five_to_two_to_five_runs_provider_admission_and_immediate_tick_sequentially(
    runtime: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[str] = []
    statuses = iter((_daemon_status(shortage=3), _daemon_status(shortage=0, opened=3)))
    monkeypatch.setattr(assistant, "_daemon", lambda *a, **k: events.append("daemon") or next(statuses))
    monkeypatch.setattr(assistant, "_queries", lambda location: ([{"query_id": "q0", "platform": "indeed", "keywords": "python", "location": location}], {"q0": ("https://www.indeed.com/jobs?q=python", "indeed")}))
    monkeypatch.setattr(assistant, "_managed_urls", lambda queue: set())
    listings = [_listing(url=f"https://www.indeed.com/viewjob?jk=replacement{i}") for i in range(3)]
    def fake_run(command, **kwargs):
        events.append("provider")
        request = json.loads(kwargs["stdin"])
        assert request["limit"] == 20
        assert set(request) == {"schema_version", "limit", "queries"}
        assert "candidate" not in json.dumps(request).lower()
        return _batch(*listings)
    monkeypatch.setattr(assistant, "_run", fake_run)
    monkeypatch.setattr(assistant, "_admit", lambda *a, **k: events.append("admit") or {"validated_count": 3, "suppressed_count": 0, "admitted_count": 3})

    assert assistant.main(_args(runtime)) == 0
    statuses_out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    status = statuses_out[0]
    assert events == ["daemon", "provider", "admit", "daemon"]
    assert status == {"state": "cycle", "cycle": 1, "rounds_attempted": 1, "provider_listing_count": 3,
                      "admitted_count": 3, "suppressed_count": 0, "opened_count": 3,
                      "search_needed": 0}
    assert statuses_out[1]["state"] == "complete"
    assert "outcome" not in json.dumps(status)


def test_singleton_lock_rejects_duplicate_owner_without_starting_children(
    runtime: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _intake, queue, _memory = runtime
    lock = assistant._acquire_lock(queue)
    monkeypatch.setattr(assistant, "_daemon", lambda *a, **k: pytest.fail("child started while busy"))
    try:
        assert assistant.main(_args(runtime)) == 2
        assert json.loads(capsys.readouterr().out) == {"state": "busy", "reason": "busy"}
    finally:
        os.close(lock)


@pytest.mark.parametrize("child", [
    [sys.executable, "-c", "import sys; sys.exit(7)"],
    [sys.executable, "-c", "print('x' * 10000)"],
])
def test_child_nonzero_and_oversize_are_redacted(child: list[str]) -> None:
    with pytest.raises(assistant.HostFailure, match="^child_(failed|oversize)$"):
        assistant._run(child, stdin=None, timeout=2, maximum_output=64)


def test_child_timeout_terminates_promptly() -> None:
    started = time.monotonic()
    with pytest.raises(assistant.HostFailure, match="^child_timeout$"):
        assistant._run(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            stdin=None, timeout=0.05, maximum_output=64,
        )
    assert time.monotonic() - started < 0.75


def test_empty_provider_rounds_report_no_progress_not_success(
    runtime: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(assistant, "_daemon", lambda *a, **k: _daemon_status(shortage=2))
    monkeypatch.setattr(assistant, "_queries", lambda location: ([], {}))
    monkeypatch.setattr(assistant, "_run", lambda *a, **k: _batch())
    monkeypatch.setattr(assistant, "_managed_urls", lambda queue: set())
    assert assistant.main(_args(runtime, "--max-rounds", "2", "--backoff-seconds", "0")) == 1
    statuses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [status["state"] for status in statuses] == ["no_progress", "incomplete"]
    assert statuses[-1]["search_needed"] == 2




def test_bounded_unresolved_search_need_is_terminal_incomplete_not_complete(
    runtime: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(assistant, "_queries", lambda location: ([], {}))
    monkeypatch.setattr(assistant, "_daemon", lambda *a, **k: _daemon_status(shortage=2))
    monkeypatch.setattr(assistant, "_run", lambda *a, **k: _batch())
    monkeypatch.setattr(assistant, "_managed_urls", lambda queue: set())
    monkeypatch.setattr(assistant, "_cycle_sleep", lambda seconds: None)

    assert assistant.main(_args(runtime, "--max-cycles", "2", "--max-rounds", "1", "--backoff-seconds", "0")) == 1
    frames = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [frame["state"] for frame in frames] == ["no_progress", "no_progress", "incomplete"]
    assert all(frame["search_needed"] == 2 for frame in frames)
    assert all("complete" != frame["state"] for frame in frames)
    assert frames[-1]["provider_listing_count"] == 0
    assert frames[-1]["admitted_count"] == 0


def test_bounded_recovered_search_need_is_terminal_complete_and_zero(
    runtime: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    query_urls = {"q0": ("https://www.indeed.com/jobs?q=python", "indeed")}
    monkeypatch.setattr(assistant, "_queries", lambda location: ([], query_urls))
    monkeypatch.setattr(assistant, "_managed_urls", lambda queue: set())
    statuses = iter([_daemon_status(shortage=1), _daemon_status(shortage=0, opened=1), _daemon_status(shortage=0, opened=0)])
    monkeypatch.setattr(assistant, "_daemon", lambda *a, **k: next(statuses))
    monkeypatch.setattr(assistant, "_run", lambda *a, **k: _batch(_listing()))
    monkeypatch.setattr(assistant, "_admit", lambda *a, **k: {"validated_count": 1, "admitted_count": 1, "suppressed_count": 0})
    monkeypatch.setattr(assistant, "_cycle_sleep", lambda seconds: None)

    assert assistant.main(_args(runtime, "--max-cycles", "2", "--max-rounds", "1", "--backoff-seconds", "0")) == 0
    frames = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert frames[0]["opened_count"] == 1
    assert frames[-1]["state"] == "complete"
    assert frames[-1]["search_needed"] == 0

def test_explicit_shutdown_is_redacted_and_releases_singleton(
    runtime: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _intake, queue, _memory = runtime
    monkeypatch.setattr(assistant, "_daemon", lambda *a, **k: (_ for _ in ()).throw(assistant.HostFailure("shutdown")))
    assert assistant.main(_args(runtime)) == 2
    assert json.loads(capsys.readouterr().out) == {"state": "shutdown", "reason": "shutdown"}
    lock = assistant._acquire_lock(queue)
    os.close(lock)


def test_missing_child_is_redacted() -> None:
    with pytest.raises(assistant.HostFailure, match="^child_failed$"):
        assistant._run(["/nonexistent/synthetic-private-command"], stdin=None, timeout=1, maximum_output=64)


def test_nonreading_child_input_is_inside_deadline() -> None:
    started = time.monotonic()
    with pytest.raises(assistant.HostFailure, match="^child_timeout$"):
        assistant._run([sys.executable, "-c", "import time; time.sleep(10)"],
                       stdin=b"x" * 200000, timeout=0.1, maximum_output=64)
    assert time.monotonic() - started < 0.75


def test_stderr_is_bounded_and_redacted() -> None:
    with pytest.raises(assistant.HostFailure, match="^child_oversize$"):
        assistant._run([sys.executable, "-c", "import sys; sys.stderr.write('private'*10000)"],
                       stdin=None, timeout=1, maximum_output=64)


def test_default_timeout_allows_provider_budget_and_delayed_success() -> None:
    assert assistant._parser().get_default("provider_timeout_seconds") >= 310
    assert assistant._run([sys.executable, "-c", "import time; time.sleep(0.15); print('{}')"],
                          stdin=None, timeout=1, maximum_output=64) == "{}\n"


def test_crashed_lock_owner_does_not_leave_host_busy(runtime: tuple[Path, Path, Path]) -> None:
    _intake, queue, _memory = runtime
    code = "import fcntl,os,sys,time; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); time.sleep(10)"
    lockpath = queue.with_name(f".{queue.name}.external-assistant.lock")
    child = subprocess.Popen([sys.executable, "-c", code, str(lockpath)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(assistant.HostFailure, match="^busy$"):
            assistant._acquire_lock(queue)
        child.kill()
        child.wait(timeout=2)
        lock = assistant._acquire_lock(queue)
        os.close(lock)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()


def test_descendant_cannot_survive_an_exited_leader(tmp_path: Path) -> None:
    marker = tmp_path / "survived"
    descendant = "import signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(0.6); pathlib.Path(" + repr(str(marker)) + ").touch()"
    leader = "import subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(descendant) + "]); sys.exit(0)"
    with pytest.raises(assistant.HostFailure, match="^child_timeout$"):
        assistant._run([sys.executable, "-c", leader], stdin=None, timeout=0.15, maximum_output=64)
    time.sleep(0.65)
    assert not marker.exists()


@pytest.mark.parametrize(("field", "limit"), [
    ("title", 512), ("company", 512), ("description", 12000), ("location", 512),
    ("work_mode", 128), ("employment_type", 128), ("posted_at", 128),
    ("source_job_id", 256), ("discovered_at", 128),
])
def test_host_enforces_published_provider_string_bounds(field: str, limit: int) -> None:
    queries = {"q0": ("https://www.indeed.com/jobs?q=python", "indeed")}
    assert assistant._provider_listings(_batch(_listing(**{field: "x" * limit})), queries, set())[1] == 1
    with pytest.raises(assistant.HostFailure, match="^provider_schema_invalid$"):
        assistant._provider_listings(_batch(_listing(**{field: "x" * (limit + 1)})), queries, set())


@pytest.mark.parametrize("raw", [
    json.dumps({"schema_version": True, "listings": []}),
    _batch(_listing(title="")),
    _batch(_listing(query_id="q" + "1" * 32)),
])
def test_host_rejects_schema_type_and_required_string_violations(raw: str) -> None:
    with pytest.raises(assistant.HostFailure, match="^provider_schema_invalid$"):
        assistant._provider_listings(raw, {"q0": ("https://www.indeed.com/jobs?q=python", "indeed")}, set())


def test_oversize_input_is_rejected_before_spawning() -> None:
    with pytest.raises(assistant.HostFailure, match="^child_input_oversize$"):
        assistant._run(["/nonexistent/synthetic-command"], stdin=b"x" * (assistant.MAX_PROVIDER_BYTES + 1),
                       timeout=1, maximum_output=64)


def test_term_resistant_child_is_killed_and_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    children = []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(assistant.subprocess, "Popen", spawn)
    with pytest.raises(assistant.HostFailure, match="^child_timeout$"):
        assistant._run([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(10)"],
                       stdin=None, timeout=0.15, maximum_output=64)
    assert len(children) == 1
    assert children[0].returncode == -9
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0].pid, os.WNOHANG)


@pytest.mark.parametrize("changes", [
    {"recommended_exports": True}, {"recommended_exports": 1},
    {"errors": ["synthetic failure"]}, {"malformed_payloads": 1},
    {"application_actions": 1}, {"mode": "unexpected"},
])
def test_zero_export_does_not_hide_invalid_discovery_status(
    runtime: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, changes: dict,
) -> None:
    status = {"payloads_seen": 1, "new_listings": 1, "duplicate_listings": 0,
              "recommended_exports": 0, "below_threshold": 1, "malformed_payloads": 0,
              "application_actions": 0, "minimum_profile_fit_score": 85,
              "run_id": "synthetic", "started_at": "synthetic", "finished_at": "synthetic",
              "profile_revision": "synthetic", "matcher_policy_revision": "synthetic",
              "search_urls": [], "errors": [], "mode": "discovery_export_only",
              "network_access": "none_by_scheduler", **changes}
    monkeypatch.setattr(assistant, "_run", lambda *a, **k: json.dumps(status))
    with pytest.raises(assistant.HostFailure, match="^discovery_failed$"):
        assistant._admit(*runtime, {}, 1)


@pytest.mark.parametrize("reason", ["child_timeout", "child_failed", "child_oversize",
                                  "provider_schema_invalid", "provider_duplicate"])
def test_provider_failure_retains_lock_and_reconciles_before_retry(
    runtime: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], reason: str,
) -> None:
    events = []
    monkeypatch.setattr(assistant, "_queries", lambda location: ([], {}))
    monkeypatch.setattr(assistant, "_managed_urls", lambda queue: set())
    monkeypatch.setattr(assistant, "_daemon", lambda *a, **k: events.append("daemon") or _daemon_status(shortage=3))
    monkeypatch.setattr(assistant, "_admit", lambda *a, **k: pytest.fail("invalid batch reached admission"))

    def provider(*args, **kwargs):
        events.append("provider")
        raise assistant.HostFailure(reason)

    def wait(seconds):
        events.append("backoff")
        assert 0 < seconds <= 60
        with pytest.raises(assistant.HostFailure, match="^busy$"):
            assistant._acquire_lock(runtime[1])

    monkeypatch.setattr(assistant, "_run", provider)
    monkeypatch.setattr(assistant, "_cycle_sleep", wait)
    assert assistant.main(_args(runtime, "--max-cycles", "2", "--backoff-seconds", "0")) == 1
    statuses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events == ["daemon", "provider", "backoff", "daemon", "provider"]
    assert [status["state"] for status in statuses] == ["degraded", "degraded", "incomplete"]
    assert all(status["search_needed"] == 3 for status in statuses)
    assert all(status["admitted_count"] == status["provider_listing_count"] == 0 for status in statuses)
    assert not runtime[1].exists() and not runtime[2].exists()


def test_shutdown_during_provider_failure_backoff_is_prompt_and_releases_lock(
    runtime: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(assistant, "_queries", lambda location: ([], {}))
    monkeypatch.setattr(assistant, "_managed_urls", lambda queue: set())
    monkeypatch.setattr(assistant, "_daemon", lambda *a, **k: _daemon_status(shortage=3))
    monkeypatch.setattr(assistant, "_run", lambda *a, **k: "malformed")
    original_sleep = assistant._cycle_sleep

    def stop(seconds):
        assistant._on_shutdown(None, None)
        original_sleep(seconds)

    monkeypatch.setattr(assistant, "_cycle_sleep", stop)
    started = time.monotonic()
    try:
        assert assistant.main(_args(runtime, "--max-cycles", "2", "--backoff-seconds", "60")) == 2
        assert time.monotonic() - started < 0.5
        statuses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert [status["state"] for status in statuses] == ["degraded", "shutdown"]
        lock = assistant._acquire_lock(runtime[1])
        os.close(lock)
    finally:
        assistant._SHUTDOWN = False


def test_queue_failure_is_fatal_not_a_recoverable_provider_error(
    runtime: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(assistant, "_queries", lambda location: ([], {}))
    monkeypatch.setattr(assistant, "_daemon", lambda *a, **k: _daemon_status(shortage=3))
    monkeypatch.setattr(assistant, "_managed_urls", lambda *a: (_ for _ in ()).throw(assistant.HostFailure("queue_read_failed")))
    monkeypatch.setattr(assistant, "_run", lambda *a, **k: pytest.fail("provider started after queue failure"))
    assert assistant.main(_args(runtime, "--max-cycles", "2")) == 2
    assert json.loads(capsys.readouterr().out) == {"state": "failed", "reason": "queue_read_failed"}


@pytest.mark.parametrize("raw", [
    _batch(_listing(platform=[])),
    _batch(_listing(platform={})),
    "[" * 1100 + "0" + "]" * 1100,
    '{"schema_version":' + "9" * 5000 + ',"listings":[]}',
], ids=["platform-array", "platform-object", "deep-json", "oversized-integer"])
def test_malformed_provider_types_and_parser_limits_recover_before_valid_admission(
    runtime: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], raw: str,
) -> None:
    query_urls = {"q0": ("https://www.indeed.com/jobs?q=python", "indeed")}
    assert len(raw.encode()) < assistant.MAX_PROVIDER_BYTES
    with pytest.raises(assistant.HostFailure, match="^provider_schema_invalid$"):
        assistant._provider_listings(raw, query_urls, set())
    events = []
    responses = iter([raw, _batch(_listing())])
    statuses = iter([_daemon_status(shortage=1), _daemon_status(shortage=1),
                     _daemon_status(shortage=0, opened=1)])
    monkeypatch.setattr(assistant, "_queries", lambda location: ([], query_urls))
    monkeypatch.setattr(assistant, "_managed_urls", lambda queue: set())
    monkeypatch.setattr(assistant, "_daemon", lambda *a, **k: events.append("daemon") or next(statuses))
    monkeypatch.setattr(assistant, "_run", lambda *a, **k: events.append("provider") or next(responses))

    def admit(*args):
        events.append("admit")
        assert args[3][query_urls["q0"][0]][0]["url"] == _listing()["url"]
        return {"validated_count": 1, "admitted_count": 1, "suppressed_count": 0}

    def backoff(seconds):
        events.append("backoff")
        assert 0 < seconds <= 60
        with pytest.raises(assistant.HostFailure, match="^busy$"):
            assistant._acquire_lock(runtime[1])

    monkeypatch.setattr(assistant, "_admit", admit)
    monkeypatch.setattr(assistant, "_cycle_sleep", backoff)
    assert assistant.main(_args(runtime, "--max-cycles", "2")) == 0
    output = capsys.readouterr()
    assert output.err == "" and "https://" not in output.out
    frames = [json.loads(line) for line in output.out.splitlines()]
    assert [frame["state"] for frame in frames] == ["degraded", "cycle", "complete"]
    assert frames[0]["search_needed"] == 1
    assert frames[0]["admitted_count"] == frames[0]["provider_listing_count"] == 0
    assert frames[1]["search_needed"] == 0 and frames[1]["admitted_count"] == 1
    assert events == ["daemon", "provider", "backoff", "daemon", "provider", "admit", "daemon"]
