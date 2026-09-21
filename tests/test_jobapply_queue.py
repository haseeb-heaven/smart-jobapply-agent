import importlib.util
import json
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
    monkeypatch.chdir(tmp_path)
    marker = tmp_path / "jobapply_agent" / "src" / "jobapply_agent" / "smart_queue.py"
    marker.parent.mkdir(parents=True)
    marker.write_text("x")
    root = jq.resolve_repository_root(None)
    assert root == tmp_path


def test_resolve_repository_root_prefers_explicit_over_cwd(tmp_path, monkeypatch):
    repo = tmp_path / "repo-checkout"
    marker = repo / "jobapply_agent" / "src" / "jobapply_agent" / "smart_queue.py"
    marker.parent.mkdir(parents=True)
    marker.write_text("x")
    monkeypatch.chdir(tmp_path)
    root = jq.resolve_repository_root(str(repo))
    assert root == repo


def test_resolve_repository_root_falls_back_to_script_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = jq.resolve_repository_root(None)
    assert root == jq.SCRIPT_DIR.parent.parent


# --- capacity authorization --------------------------------------------------

_REVISION = "ab" * 32


def _intake_file(tmp_path):
    path = tmp_path / "candidate_intake.json"
    path.write_text(json.dumps({"approved_facts": {}}), encoding="utf-8")
    return path


def _install_intake(monkeypatch, approved_facts, revision=_REVISION):
    """Replace the intake boundary with a confirmed-revision stand-in."""

    def loader(_root):
        def validate(_payload):
            return {"approved_facts": approved_facts, "revision_hash": revision}

        return validate

    monkeypatch.setattr(jq, "_load_intake_module", loader)


def _install_failing_intake(monkeypatch):
    def loader(_root):
        def validate(_payload):
            raise ValueError("candidate intake revision_hash does not match approved state")

        return validate

    monkeypatch.setattr(jq, "_load_intake_module", loader)


def _write_metadata(path, *, target_size, provenance, revision):
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS smart_queue_metadata (metadata_id INTEGER PRIMARY KEY, "
            "target_size INTEGER, capacity_provenance TEXT, capacity_intake_revision TEXT)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO smart_queue_metadata VALUES (1, ?, ?, ?)",
            (target_size, provenance, revision),
        )
        connection.commit()
    finally:
        connection.close()
    return path


class _CapacityQueue:
    """Record every capacity write, including provenance kwargs."""

    def __init__(self, database, target_size=jq.DEFAULT_CAPACITY, provenance="default"):
        self.database_path = database
        self.target_size = target_size
        self.capacity_provenance = provenance
        self.calls = []

    def set_target_size(
        self, capacity, *, actor, capacity_provenance=None, intake_revision_hash=None
    ):
        self.calls.append(
            {
                "capacity": capacity,
                "actor": actor,
                "capacity_provenance": capacity_provenance,
                "intake_revision_hash": intake_revision_hash,
            }
        )
        self.target_size = capacity
        self.capacity_provenance = capacity_provenance or (
            "default" if capacity == jq.DEFAULT_CAPACITY else "host-configured"
        )
        # The real queue returns the resulting capacity, not the prior one.
        return self.target_size


def test_active_intake_capacity_reads_approved_fact(tmp_path, monkeypatch):
    _install_intake(monkeypatch, {jq.SMART_QUEUE_CAPACITY_FACT: 3})

    assert jq.active_intake_capacity(tmp_path, _intake_file(tmp_path)) == (3, _REVISION)


def test_active_intake_capacity_reads_nested_fact(tmp_path, monkeypatch):
    _install_intake(monkeypatch, {"targets": {"smart_queue_capacity": 7}})

    assert jq.active_intake_capacity(tmp_path, _intake_file(tmp_path)) == (7, _REVISION)


def test_active_intake_capacity_defaults_when_fact_absent(tmp_path, monkeypatch):
    _install_intake(monkeypatch, {"candidate_profile": {"headline": "Backend Engineer"}})

    # The documented default needs no intake binding to stay live.
    assert jq.active_intake_capacity(tmp_path, _intake_file(tmp_path)) == (
        jq.DEFAULT_CAPACITY,
        None,
    )


@pytest.mark.parametrize("capacity", [True, False, 0, 11, -1, "3", 3.0])
def test_active_intake_capacity_rejects_invalid_value(tmp_path, monkeypatch, capacity):
    _install_intake(monkeypatch, {jq.SMART_QUEUE_CAPACITY_FACT: capacity})

    with pytest.raises(jq.CliUsageError):
        jq.active_intake_capacity(tmp_path, _intake_file(tmp_path))


def test_active_intake_capacity_requires_readable_confirmed_intake(tmp_path, monkeypatch):
    with pytest.raises(jq.CliUsageError):
        jq.active_intake_capacity(tmp_path, tmp_path / "absent.json")

    _install_failing_intake(monkeypatch)
    path = _intake_file(tmp_path)
    with pytest.raises(jq.CliUsageError):
        jq.active_intake_capacity(tmp_path, path)

    path.write_text("not json", encoding="utf-8")
    with pytest.raises(jq.CliUsageError):
        jq.active_intake_capacity(tmp_path, path)


def test_capacity_live_authorized_truth_table():
    bound = (3, jq.CAPACITY_PROVENANCE_ACTIVE_INTAKE, _REVISION)
    assert jq.capacity_live_authorized(
        intake_capacity=3, intake_revision=_REVISION, stored=bound
    ) is True
    # A stale digest is not the active intake's proof.
    assert jq.capacity_live_authorized(
        intake_capacity=3, intake_revision=_REVISION, stored=(3, "active-candidate-intake", "cd" * 32)
    ) is False
    # Host-configured non-default capacity is never live-authorized.
    assert jq.capacity_live_authorized(
        intake_capacity=3, intake_revision=_REVISION, stored=(3, "host-configured", None)
    ) is False
    assert jq.capacity_live_authorized(
        intake_capacity=3, intake_revision=_REVISION, stored=(4, "active-candidate-intake", _REVISION)
    ) is False
    # The default carries no binding, so only the size must agree -- even a
    # stale or unrelated intake digest cannot make the default unauthorized.
    assert jq.capacity_live_authorized(
        intake_capacity=5, intake_revision=None, stored=(5, "default", None)
    ) is True
    assert jq.capacity_live_authorized(
        intake_capacity=5, intake_revision=_REVISION, stored=(5, "active-candidate-intake", "cd" * 32)
    ) is True
    assert jq.capacity_live_authorized(
        intake_capacity=5, intake_revision=None, stored=(3, "default", None)
    ) is False
    # A non-default size can never be authorized without the intake's digest.
    assert jq.capacity_live_authorized(
        intake_capacity=3, intake_revision=None, stored=(3, "default", None)
    ) is False


def test_persisted_capacity_metadata_reads_and_fails_closed(tmp_path):
    db = _write_metadata(
        tmp_path / "q.sqlite3",
        target_size=3,
        provenance=jq.CAPACITY_PROVENANCE_ACTIVE_INTAKE,
        revision=_REVISION,
    )
    assert jq.persisted_capacity_metadata(db) == (3, jq.CAPACITY_PROVENANCE_ACTIVE_INTAKE, _REVISION)

    with pytest.raises(jq.CliUsageError):
        jq.persisted_capacity_metadata(tmp_path / "absent.sqlite3")

    empty = tmp_path / "empty.sqlite3"
    sqlite3.connect(empty).close()
    with pytest.raises(jq.CliUsageError):
        jq.persisted_capacity_metadata(empty)


def test_capacity_authorization_reports_unknown_never_authorized(tmp_path, monkeypatch):
    _install_failing_intake(monkeypatch)
    paths = {"queue": _write_metadata(tmp_path / "q.sqlite3", target_size=3, provenance="default", revision=None), "intake": _intake_file(tmp_path)}

    authorized, detail = jq.capacity_authorization(tmp_path, paths)

    assert authorized is None
    assert detail is not None


def test_capacity_authorization_detects_stale_metadata(tmp_path, monkeypatch):
    _install_intake(monkeypatch, {jq.SMART_QUEUE_CAPACITY_FACT: 3})
    queue_db = _write_metadata(
        tmp_path / "q.sqlite3", target_size=3, provenance="host-configured", revision=None
    )
    paths = {"queue": queue_db, "intake": _intake_file(tmp_path)}

    assert jq.capacity_authorization(tmp_path, paths) == (False, None)

    _write_metadata(
        queue_db,
        target_size=3,
        provenance=jq.CAPACITY_PROVENANCE_ACTIVE_INTAKE,
        revision=_REVISION,
    )
    assert jq.capacity_authorization(tmp_path, paths) == (True, None)


def test_apply_managed_capacity_binds_approved_capacity(tmp_path):
    queue = _CapacityQueue(tmp_path / "q.sqlite3")

    resulting, warning = jq.apply_managed_capacity(
        queue, requested=3, intake_capacity=3, intake_revision=_REVISION
    )

    assert (resulting, warning) == (3, None)
    assert queue.calls == [
        {
            "capacity": 3,
            "actor": "user",
            "capacity_provenance": jq.CAPACITY_PROVENANCE_ACTIVE_INTAKE,
            "intake_revision_hash": _REVISION,
        }
    ]


def test_apply_managed_capacity_binds_default_without_proof(tmp_path):
    queue = _CapacityQueue(tmp_path / "q.sqlite3", target_size=3, provenance="host-configured")

    resulting, warning = jq.apply_managed_capacity(
        queue, requested=5, intake_capacity=5, intake_revision=None
    )

    assert (resulting, warning) == (5, None)
    assert queue.calls[-1]["capacity_provenance"] is None


def test_apply_managed_capacity_never_binds_proof_at_default_size(tmp_path):
    """An explicitly approved default size is live without any binding.

    Binding intake proof at the default size is rejected by the queue, so the
    default must always resolve to plain default provenance.
    """

    queue = _CapacityQueue(tmp_path / "q.sqlite3")

    resulting, warning = jq.apply_managed_capacity(
        queue, requested=5, intake_capacity=5, intake_revision=_REVISION
    )

    assert (resulting, warning) == (5, None)
    assert queue.calls == [
        {
            "capacity": 5,
            "actor": "user",
            "capacity_provenance": None,
            "intake_revision_hash": None,
        }
    ]
    assert queue.calls[-1]["intake_revision_hash"] is None


def _real_queue(tmp_path):
    """Build the repository's real queue so policy failures surface here."""

    queue_class, _, _, _ = jq._load_queue_module(jq.SCRIPT_DIR.parent.parent)
    return queue_class(tmp_path / "q.sqlite3")


def test_apply_managed_capacity_default_size_is_accepted_by_the_real_queue(tmp_path):
    """An approved default size must never be bound with intake proof.

    The queue rejects non-default provenance at the default size, so binding
    proof here would raise QueuePolicyError instead of authorizing capacity.
    """

    queue = _real_queue(tmp_path)

    resulting, warning = jq.apply_managed_capacity(
        queue, requested=5, intake_capacity=5, intake_revision=_REVISION
    )

    assert (resulting, warning) == (5, None)
    assert queue.target_size == 5
    assert queue.capacity_provenance == "default"


def test_apply_managed_capacity_non_default_reaches_the_live_construction_seam(tmp_path):
    """An approved non-default size must survive the daemon's own constructor."""

    queue = _real_queue(tmp_path)

    resulting, warning = jq.apply_managed_capacity(
        queue, requested=3, intake_capacity=3, intake_revision=_REVISION
    )

    assert (resulting, warning) == (3, None)
    assert queue.target_size == 3
    assert queue.capacity_provenance == jq.CAPACITY_PROVENANCE_ACTIVE_INTAKE
    assert queue.has_active_intake_capacity_provenance is True
    # The daemon constructs through this exact active-intake seam on every run.
    queue_class, _, _, _ = jq._load_queue_module(jq.SCRIPT_DIR.parent.parent)
    built = queue_class.for_active_candidate_intake(
        tmp_path / "q.sqlite3", target_size=3, intake_revision_hash=_REVISION
    )
    assert built.target_size == 3
    assert jq.capacity_live_authorized(
        intake_capacity=3,
        intake_revision=_REVISION,
        stored=jq.persisted_capacity_metadata(tmp_path / "q.sqlite3"),
    ) is True


def test_apply_managed_capacity_warns_for_unapproved_capacity(tmp_path):
    """An intake that approves the default still refuses any other size."""

    queue = _CapacityQueue(tmp_path / "q.sqlite3")

    resulting, warning = jq.apply_managed_capacity(
        queue, requested=3, intake_capacity=5, intake_revision=None
    )

    assert resulting == 3
    assert warning is not None
    assert jq.SMART_QUEUE_CAPACITY_FACT in warning
    assert "approves 5" in warning
    assert queue.calls[-1]["capacity_provenance"] is None
    assert queue.calls[-1]["intake_revision_hash"] is None


def test_apply_managed_capacity_warns_when_intake_unconfirmed(tmp_path):
    queue = _CapacityQueue(tmp_path / "q.sqlite3")

    resulting, warning = jq.apply_managed_capacity(
        queue, requested=3, intake_capacity=None, intake_revision=None
    )

    assert resulting == 3
    assert warning is not None
    assert "could not be confirmed" in warning
    assert jq.SMART_QUEUE_CAPACITY_FACT not in warning


def test_set_managed_capacity_prints_authorized_write(tmp_path, monkeypatch, capsys):
    intake = _intake_file(tmp_path)
    _install_intake(monkeypatch, {jq.SMART_QUEUE_CAPACITY_FACT: 3})
    queue = _CapacityQueue(tmp_path / "q.sqlite3")

    previous = jq.set_managed_capacity(tmp_path, {"intake": intake}, queue, 3)
    out = capsys.readouterr().out

    assert previous == jq.DEFAULT_CAPACITY
    assert "capacity 5 -> 3" in out
    assert f"capacity_provenance: {jq.CAPACITY_PROVENANCE_ACTIVE_INTAKE}" in out
    assert "warning" not in out.lower()


def test_set_managed_capacity_warns_for_unapproved_write(tmp_path, monkeypatch, capsys):
    intake = _intake_file(tmp_path)
    _install_intake(monkeypatch, {jq.SMART_QUEUE_CAPACITY_FACT: 3})
    queue = _CapacityQueue(tmp_path / "q.sqlite3")

    jq.set_managed_capacity(tmp_path, {"intake": intake}, queue, 4)
    out = capsys.readouterr().out

    assert "capacity 5 -> 4" in out
    assert "capacity_provenance: host-configured" in out
    assert "warning:" in out and jq.SMART_QUEUE_CAPACITY_FACT in out


def test_status_payload_reports_capacity_authorization(tmp_path):
    db = _queue_db(tmp_path, ["job-a"])
    queue = _FakeQueue(db, {"job-a": "open"})

    assert jq.status_payload(queue, bridge_command=None)["capacity_live_authorized"] is None
    payload = jq.status_payload(queue, bridge_command=None, capacity_live_authorized=False)
    assert payload["capacity_live_authorized"] is False


def test_doctor_report_surfaces_capacity_authorization(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "skills" / "easy-apply-tab-monitor" / "scripts").mkdir(parents=True)
    (repo / "skills" / "easy-apply-tab-monitor" / "scripts" / "smart_queue_daemon.py").write_text("x")
    (repo / "jobapply_agent" / "scripts").mkdir(parents=True)
    for name in ("discover.py", "record_candidate_outcome.py"):
        (repo / "jobapply_agent" / "scripts" / name).write_text("x")

    private = tmp_path / "private"
    private.mkdir()
    intake = private / "candidate_intake.json"
    intake.write_text(json.dumps({"approved_facts": {}}), encoding="utf-8")
    memory = private / "candidate-memory.sqlite3"
    memory.write_text("x")
    bridge = private / "bridge.mjs"
    bridge.write_text("x")

    paths = {
        "queue": _write_metadata(
            private / "q.sqlite3", target_size=3, provenance="host-configured", revision=None
        ),
        "memory": memory,
        "intake": intake,
        "bridge": bridge,
    }
    _install_intake(monkeypatch, {jq.SMART_QUEUE_CAPACITY_FACT: 3})
    monkeypatch.setattr(jq, "listing_tab_urls", lambda *_args, **_kwargs: ())

    report = jq.doctor_report(repo, paths, ["node", "bridge.mjs"])

    # Every prerequisite is present, yet live cycles would still refuse capacity.
    assert report["ready"] is True
    assert report["capacity_live_authorized"] is False
    assert report["capacity_detail"] is None


def test_doctor_report_keeps_ready_true_when_capacity_is_unknown(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "skills" / "easy-apply-tab-monitor" / "scripts").mkdir(parents=True)
    (repo / "skills" / "easy-apply-tab-monitor" / "scripts" / "smart_queue_daemon.py").write_text("x")
    (repo / "jobapply_agent" / "scripts").mkdir(parents=True)
    for name in ("discover.py", "record_candidate_outcome.py"):
        (repo / "jobapply_agent" / "scripts" / name).write_text("x")

    private = tmp_path / "private"
    private.mkdir()
    (private / "candidate-memory.sqlite3").write_text("x")
    (private / "candidate_intake.json").write_text(json.dumps({"approved_facts": {}}), encoding="utf-8")
    (private / "bridge.mjs").write_text("x")
    (private / "q.sqlite3").write_text("x")

    paths = {key: private / name for key, name in (
        ("queue", "q.sqlite3"),
        ("memory", "candidate-memory.sqlite3"),
        ("intake", "candidate_intake.json"),
        ("bridge", "bridge.mjs"),
    )}
    _install_failing_intake(monkeypatch)
    monkeypatch.setattr(jq, "listing_tab_urls", lambda *_args, **_kwargs: ())

    report = jq.doctor_report(repo, paths, ["node", "bridge.mjs"])

    assert report["ready"] is True
    assert report["capacity_live_authorized"] is None
    assert report["capacity_detail"] is not None


def test_active_intake_capacity_missing_error_string_sanitized(tmp_path):
    absent = tmp_path / "secret_candidate_profile" / "candidate_intake.json"
    with pytest.raises(jq.CliUsageError) as exc_info:
        jq.active_intake_capacity(tmp_path, absent)
    message = str(exc_info.value)
    assert message == "the active candidate intake is missing"
    assert str(absent) not in message
    assert "secret_candidate_profile" not in message


def test_require_authorized_capacity_unauthorized_raises_helpful_error(tmp_path, monkeypatch):
    intake = _intake_file(tmp_path)
    _install_intake(monkeypatch, {jq.SMART_QUEUE_CAPACITY_FACT: 3})
    queue_db = _write_metadata(
        tmp_path / "q.sqlite3", target_size=4, provenance="host-configured", revision=None
    )
    paths = {"intake": intake, "queue": queue_db}

    with pytest.raises(jq.CliUsageError) as exc_info:
        jq.require_authorized_capacity(tmp_path, paths)

    message = str(exc_info.value)
    assert "capacity is not authorized for live reconciliation cycles" in message
    assert "active candidate intake approves 3" in message
    assert "Run 'jobapply_queue.py tabs 3' to use the approved capacity" in message
    assert "or approve targets.smart_queue_capacity in candidate_intake.json" in message


def test_require_authorized_capacity_unknown_due_to_missing_or_unreadable_intake(tmp_path):
    queue_db = _write_metadata(
        tmp_path / "q.sqlite3", target_size=5, provenance="default", revision=None
    )
    # Missing intake
    paths_missing = {"intake": tmp_path / "missing.json", "queue": queue_db}
    with pytest.raises(jq.CliUsageError) as exc_missing:
        jq.require_authorized_capacity(tmp_path, paths_missing)
    assert "cannot verify capacity authorization for live cycles:" in str(exc_missing.value)
    assert "the active candidate intake is missing" in str(exc_missing.value)

    # Unreadable intake
    unreadable = tmp_path / "corrupt.json"
    unreadable.write_text("{corrupt json", encoding="utf-8")
    paths_unreadable = {"intake": unreadable, "queue": queue_db}
    with pytest.raises(jq.CliUsageError) as exc_unreadable:
        jq.require_authorized_capacity(tmp_path, paths_unreadable)
    assert "cannot verify capacity authorization for live cycles:" in str(exc_unreadable.value)
    assert "the active candidate intake is unreadable" in str(exc_unreadable.value)


def test_require_authorized_capacity_authorized_succeeds(tmp_path, monkeypatch):
    # Approved non-default capacity (3)
    intake = _intake_file(tmp_path)
    _install_intake(monkeypatch, {jq.SMART_QUEUE_CAPACITY_FACT: 3}, revision=_REVISION)
    queue_db_bound = _write_metadata(
        tmp_path / "q_bound.sqlite3",
        target_size=3,
        provenance=jq.CAPACITY_PROVENANCE_ACTIVE_INTAKE,
        revision=_REVISION,
    )
    paths_bound = {"intake": intake, "queue": queue_db_bound}
    assert jq.require_authorized_capacity(tmp_path, paths_bound) is None

    # Approved default capacity (5)
    _install_intake(monkeypatch, {})
    queue_db_default = _write_metadata(
        tmp_path / "q_default.sqlite3",
        target_size=5,
        provenance="default",
        revision=None,
    )
    paths_default = {"intake": intake, "queue": queue_db_default}
    assert jq.require_authorized_capacity(tmp_path, paths_default) is None


def test_dispatch_open_guards_unauthorized_capacity_before_run_cycle(tmp_path, monkeypatch):
    repo = jq.SCRIPT_DIR.parent.parent
    intake = _intake_file(tmp_path)
    _install_intake(monkeypatch, {jq.SMART_QUEUE_CAPACITY_FACT: 3})
    queue_db = _write_metadata(
        tmp_path / "q.sqlite3", target_size=4, provenance="host-configured", revision=None
    )

    cycle_called = False

    def fake_run_cycle(*args, **kwargs):
        nonlocal cycle_called
        cycle_called = True
        return {"opened_count": 0}

    monkeypatch.setattr(jq, "run_cycle", fake_run_cycle)

    parser = jq.build_parser()
    args = parser.parse_args([
        "--repo", str(repo),
        "--intake", str(intake),
        "--queue-db", str(queue_db),
        "open",
    ])

    with pytest.raises(jq.CliUsageError) as exc_info:
        jq.dispatch(args)

    assert "capacity is not authorized for live reconciliation cycles" in str(exc_info.value)
    assert not cycle_called


def test_dispatch_watch_guards_unapproved_tabs_before_watch_starts(tmp_path, monkeypatch):
    repo = jq.SCRIPT_DIR.parent.parent
    intake = _intake_file(tmp_path)
    _install_intake(monkeypatch, {jq.SMART_QUEUE_CAPACITY_FACT: 3})
    queue = _real_queue(tmp_path)
    jq.apply_managed_capacity(
        queue, requested=3, intake_capacity=3, intake_revision=_REVISION
    )
    queue_db = queue.database_path

    watch_called = False

    def fake_watch(*args, **kwargs):
        nonlocal watch_called
        watch_called = True
        return 0

    monkeypatch.setattr(jq, "watch", fake_watch)

    parser = jq.build_parser()
    args = parser.parse_args([
        "--repo", str(repo),
        "--intake", str(intake),
        "--queue-db", str(queue_db),
        "watch",
        "--tabs", "4",
    ])

    with pytest.raises(jq.CliUsageError) as exc_info:
        jq.dispatch(args)

    message = str(exc_info.value)
    assert "capacity is not authorized for live reconciliation cycles" in message
    assert "active candidate intake approves 3" in message
    assert "Run 'jobapply_queue.py tabs 3'" in message
    assert not watch_called
