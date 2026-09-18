import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest
from types import SimpleNamespace

_SCRIPT_PATH = Path(__file__).parents[1] / "jobapply_agent" / "scripts" / "jobapply_queue.py"
_SPEC = importlib.util.spec_from_file_location("jobapply_queue", _SCRIPT_PATH)
assert _SPEC and _SPEC.loader
jq = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = jq
_SPEC.loader.exec_module(jq)

LINKEDIN = "https://www.linkedin.com/jobs/view/4468369583"
INDEED = "https://in.indeed.com/viewjob?jk=4a24e06d0e678c2f"


def test_parse_duration_units():
    assert jq.parse_duration("90") == 90.0
    assert jq.parse_duration("30s") == 30.0
    assert jq.parse_duration("10m") == 600.0
    assert jq.parse_duration("2h") == 7200.0
    assert jq.parse_duration(" 5m ") == 300.0


def test_parse_duration_rejects_bad_input():
    for raw in ("", "-5", "10x", "1.5m", "m", 30, None, True):
        with pytest.raises(jq.CliUsageError):
            jq.parse_duration(raw)


def test_require_capacity_bounds():
    assert jq.require_capacity(1) == 1
    assert jq.require_capacity(10) == 10
    for raw in (0, 11, -1, True, False, "5", 5.0, None):
        with pytest.raises(jq.CliUsageError):
            jq.require_capacity(raw)


def test_queue_state_summary_is_deterministic():
    states = {"a": "open", "b": "open", "c": "recommended", "d": "missing"}
    first = jq.queue_state_summary(states)
    second = jq.queue_state_summary(dict(reversed(list(states.items()))))
    assert first == second == {"open": 2, "missing": 1, "recommended": 1}
    assert jq.queue_state_summary({}) == {}


def test_parse_bridge_urls_valid():
    assert jq.parse_bridge_urls('["https://a.example/", "https://b.example/"]') == (
        "https://a.example/",
        "https://b.example/",
    )
    assert jq.parse_bridge_urls("[]") == ()


def test_parse_bridge_urls_fail_closed():
    with pytest.raises(jq.CliUsageError):
        jq.parse_bridge_urls("not json")
    with pytest.raises(jq.CliUsageError):
        jq.parse_bridge_urls('{"urls": []}')
    with pytest.raises(jq.CliUsageError):
        jq.parse_bridge_urls('["ok", 42]')
    with pytest.raises(jq.CliUsageError):
        jq.parse_bridge_urls('[""]')
    with pytest.raises(jq.CliUsageError):
        jq.parse_bridge_urls("[" + ",".join('"u"' for _ in range(600)) + "]")


def test_count_listing_tabs_only_counts_listings():
    assert jq.count_listing_tabs([LINKEDIN, INDEED]) == 2
    assert jq.count_listing_tabs(["https://github.com/x", "https://x.com/a"]) == 0
    assert jq.count_listing_tabs([LINKEDIN + "?ref=1"]) == 0
    assert jq.count_listing_tabs([123, None]) == 0
    assert jq.count_listing_tabs([]) == 0


def test_format_status_line_exact():
    status = {"opened_count": 2, "requested_open_count": 3, "open_failed_count": 1, "search_needed": 4}
    assert jq.format_status_line(7, status) == "cycle=7 opened=2 requested=3 open_failed=1 search_needed=4"


def test_parse_cycle_status_valid_and_invalid():
    text = 'noise\n{"opened_count": 1, "search_needed": 0}'
    assert jq.parse_cycle_status(text)["opened_count"] == 1
    for bad in ("", "   ", "not json", "[1, 2]", '{"a": 1}\nnot json'):
        with pytest.raises(jq.CliUsageError):
            jq.parse_cycle_status(bad)


def test_daemon_cycle_command_validation(tmp_path):
    root = tmp_path
    daemon_dir = root / "skills" / "easy-apply-tab-monitor" / "scripts"
    daemon_dir.mkdir(parents=True)
    (daemon_dir / "smart_queue_daemon.py").write_text("x")
    command = jq.daemon_cycle_command(
        root,
        intake=tmp_path / "i.json",
        database=tmp_path / "q.sqlite3",
        bridge_command=["node", "bridge.mjs"],
        ticks=2,
    )
    assert "--max-ticks" in command and "2" in command
    assert "--adapter-command" in command
    for bad_ticks in (0, -1, True, "2"):
        with pytest.raises(jq.CliUsageError):
            jq.daemon_cycle_command(root, intake=tmp_path, database=tmp_path, bridge_command=["x"], ticks=bad_ticks)
    with pytest.raises(jq.CliUsageError):
        jq.daemon_cycle_command(root, intake=tmp_path, database=tmp_path, bridge_command=[], ticks=1)
    with pytest.raises(jq.CliUsageError):
        jq.daemon_cycle_command(tmp_path / "missing", intake=tmp_path, database=tmp_path, bridge_command=["x"])



class _FakeQueue:
    def __init__(self, database, states):
        self.database_path = database
        self._states = dict(states)
        self.target_size = 5
        self.capacity_provenance = "default"
        self.queue_id = "q" * 32
        self.active_revisions = ("p", "m")
        self.set_sizes = []

    def get(self, job_id):
        return SimpleNamespace(state=self._states[job_id])

    def confirmed_submitted_count(self):
        return sum(1 for state in self._states.values() if state == "submitted")

    def set_target_size(self, capacity, *, actor):
        self.set_sizes.append((capacity, actor))
        previous = self.target_size
        self.target_size = capacity
        return previous


def _queue_db(tmp_path, job_ids):
    path = tmp_path / "q.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE smart_queue_jobs (job_id TEXT, created_at TEXT)")
        for index, job_id in enumerate(job_ids):
            connection.execute(
                "INSERT INTO smart_queue_jobs (job_id, created_at) VALUES (?, ?)",
                (job_id, f"2026-09-18T00:00:{index:02d}"),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def test_collect_job_states_uses_queue_get(tmp_path):
    db = _queue_db(tmp_path, ["job-a", "job-b", "job-unknown"])
    queue = _FakeQueue(db, {"job-a": "open", "job-b": "recommended"})
    states = jq.collect_job_states(queue)
    assert states == {"job-a": "open", "job-b": "recommended"}


def test_status_payload_is_redacted(tmp_path):
    db = _queue_db(tmp_path, ["job-a"])
    queue = _FakeQueue(db, {"job-a": "open"})
    payload = jq.status_payload(queue, bridge_command=None)
    assert payload["jobs_total"] == 1
    assert payload["states"] == {"open": 1}
    assert payload["confirmed_submitted"] == 0
    assert payload["profile_revision_bound"] is True
    dumped = str(payload)
    assert "linkedin" not in dumped and "indeed" not in dumped


def test_watch_rejects_bad_interval():
    with pytest.raises(jq.CliUsageError):
        jq.watch(None, paths={}, bridge_command=[], interval_seconds=0, duration_seconds=None, search_command=None)
    with pytest.raises(jq.CliUsageError):
        jq.watch(None, paths={}, bridge_command=[], interval_seconds=True, duration_seconds=None, search_command=None)


def test_watch_stops_at_duration(monkeypatch):
    calls = {"cycles": 0}
    statuses = [{"opened_count": 0, "requested_open_count": 0, "open_failed_count": 0, "search_needed": 0}]

    def fake_cycle(*args, **kwargs):
        calls["cycles"] += 1
        return statuses[0]

    monkeypatch.setattr(jq, "run_cycle", fake_cycle)
    monkeypatch.setattr(jq, "daemon_cycle_command", lambda *a, **k: ["cycle"])
    emitted = []
    clock = [0.0]
    code = jq.watch(
        Path("/nonexistent"),
        paths={"intake": Path("/i"), "queue": Path("/q")},
        bridge_command=["bridge"],
        interval_seconds=60.0,
        duration_seconds=150.0,
        search_command=None,
        emit=emitted.append,
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        clock=lambda: clock[0],
    )
    assert code == 0
    assert calls["cycles"] == 4
    assert emitted[-1] == "watch window elapsed; stopping cleanly"


def test_watch_refills_from_search_handoff(monkeypatch):
    emitted = []
    handed_off = []

    def fake_cycle(*args, **kwargs):
        if not handed_off:
            return {"opened_count": 0, "requested_open_count": 0, "open_failed_count": 0, "search_needed": 2}
        return {"opened_count": 2, "requested_open_count": 2, "open_failed_count": 0, "search_needed": 0}

    def fake_handoff(**kwargs):
        handed_off.append(True)
        return {"admitted_count": 2, "suppressed_count": 0, "validated_count": 2}

    monkeypatch.setattr(jq, "run_cycle", fake_cycle)
    monkeypatch.setattr(jq, "daemon_cycle_command", lambda *a, **k: ["cycle"])
    monkeypatch.setattr(jq, "search_and_admit", lambda *a, **k: fake_handoff())
    clock = [0.0]
    code = jq.watch(
        Path("/nonexistent"),
        paths={"intake": Path("/i"), "queue": Path("/q")},
        bridge_command=["bridge"],
        interval_seconds=60.0,
        duration_seconds=61.0,
        search_command=["search"],
        emit=emitted.append,
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        clock=lambda: clock[0],
    )
    assert code == 0
    assert any("search handoff admitted=2" in line for line in emitted)
    assert any(line.startswith("refill cycle=1 opened=2") for line in emitted)


def test_watch_reports_failed_cycle(monkeypatch):
    def boom(*args, **kwargs):
        raise jq.CliUsageError("bridge down")

    monkeypatch.setattr(jq, "run_cycle", boom)
    monkeypatch.setattr(jq, "daemon_cycle_command", lambda *a, **k: ["cycle"])
    emitted = []
    code = jq.watch(
        Path("/nonexistent"),
        paths={"intake": Path("/i"), "queue": Path("/q")},
        bridge_command=["bridge"],
        interval_seconds=60.0,
        duration_seconds=None,
        search_command=None,
        emit=emitted.append,
        sleep=lambda seconds: None,
    )
    assert code == 2
    assert "bridge down" in emitted[0]


def test_outcome_recorded_rejects_bad_outcome():
    with pytest.raises(jq.CliUsageError):
        jq.outcome_recorded(Path("/nonexistent"), paths={}, job_id="job-a", outcome="applied")
    with pytest.raises(jq.CliUsageError):
        jq.outcome_recorded(Path("/nonexistent"), paths={}, job_id="", outcome="submitted")


def test_resolve_repository_root_defaults_to_cwd(tmp_path, monkeypatch):
    import os

    monkeypatch.chdir(tmp_path)
    marker = tmp_path / "jobapply_agent" / "src" / "jobapply_agent" / "smart_queue.py"
    marker.parent.mkdir(parents=True)
    marker.write_text("x")
    root = jq.resolve_repository_root(None)
    assert root == tmp_path


def test_resolve_repository_root_prefers_explicit_over_cwd(tmp_path, monkeypatch):
    import os

    repo = tmp_path / "repo-checkout"
    marker = repo / "jobapply_agent" / "src" / "jobapply_agent" / "smart_queue.py"
    marker.parent.mkdir(parents=True)
    marker.write_text("x")
    monkeypatch.chdir(tmp_path)
    root = jq.resolve_repository_root(str(repo))
    assert root == repo


def test_resolve_repository_root_falls_back_to_script_checkout(tmp_path, monkeypatch):
    import os

    monkeypatch.chdir(tmp_path)
    root = jq.resolve_repository_root(None)
    assert root == jq.SCRIPT_DIR.parent.parent
