import importlib.util
import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace

import pytest


_SCRIPT_PATH = Path(__file__).parents[1] / "skills" / "easy-apply-tab-monitor" / "scripts" / "chrome_tab_watcher.py"
_SPEC = importlib.util.spec_from_file_location("chrome_tab_watcher", _SCRIPT_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
ChromeAppleScript = _MODULE.ChromeAppleScript
ChromeAutomationError = _MODULE.ChromeAutomationError
JobTarget = _MODULE.JobTarget
load_targets = _MODULE.load_targets
main = _MODULE.main
reconcile = _MODULE.reconcile
run_watch = _MODULE.run_watch
status_counts = _MODULE.status_counts


LINKEDIN = "https://www.linkedin.com/jobs/view/1"
INDEED = "https://in.indeed.com/viewjob?jk=2"
INDEED_ACTIVE = "https://smartapply.indeed.com/beta/indeedapply/form/review-module?jk=2"


class FakeChrome:
    def __init__(self, urls: tuple[str, ...], failures: set[str] | None = None):
        self.urls = urls
        self.failures = failures or set()
        self.opened: list[str] = []

    def list_tab_urls(self) -> tuple[str, ...]:
        return self.urls

    def open_listing(self, url: str) -> None:
        if url in self.failures:
            raise ChromeAutomationError("blocked")
        self.opened.append(url)


def _manifest(path: Path, jobs: list[dict[str, object]]) -> Path:
    path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
    return path


def test_load_targets_accepts_active_prefixes(tmp_path: Path):
    targets = load_targets(
        _manifest(
            tmp_path / "monitor.json",
            [{"url": LINKEDIN}, {"url": INDEED, "active_url_prefixes": [INDEED_ACTIVE]}],
        )
    )
    assert targets[1].is_active(INDEED_ACTIVE + "&step=review")
    assert not targets[0].is_active("https://www.linkedin.com/jobs/view/9")


def test_load_targets_canonicalizes_tracking_query_before_reopen(tmp_path: Path):
    targets = load_targets(
        _manifest(
            tmp_path / "tracking.json",
            [{"url": LINKEDIN + "?trk=public_jobs&utm_source=agent"}],
        )
    )

    chrome = FakeChrome(())
    reconcile(chrome, targets)
    assert targets[0].listing_url == LINKEDIN
    assert chrome.opened == [LINKEDIN]


def test_internal_listing_url_validators_fail_closed_for_untrusted_values():
    assert _MODULE._board_url(None) is None
    assert _MODULE._board_url("https://www.linkedin.com:bad/jobs/view/1") is None
    assert _MODULE._board_url("http://www.linkedin.com/jobs/view/1") is None
    assert _MODULE._board_url("https://user@www.linkedin.com/jobs/view/1") is None
    assert _MODULE._board_url("https://www.linkedin.com/jobs/view/1#fragment") is None
    assert _MODULE._board_url("https://example.test/jobs/view/1") is None

    assert _MODULE._is_safe_tracking_key("trk") is True
    assert _MODULE._is_safe_tracking_key("utm_source") is True
    assert _MODULE._is_safe_tracking_key("redirect") is False

    assert _MODULE._indeed_job_id(_MODULE.urlsplit("https://in.indeed.com/jobs?q=python")) is None
    assert _MODULE._indeed_job_id(_MODULE.urlsplit("https://in.indeed.com/viewjob?&")) is None
    assert _MODULE._indeed_job_id(_MODULE.urlsplit("https://in.indeed.com/viewjob?redirect=evil&jk=2")) is None
    assert _MODULE._indeed_job_id(_MODULE.urlsplit("https://in.indeed.com/viewjob?jk=bad%20id")) is None
    assert _MODULE._listing_identity(None) is None
    assert _MODULE._listing_identity("https://www.linkedin.com/jobs/view/1?&") is None


@pytest.mark.parametrize(
    "url",
    (
        "http://www.linkedin.com/jobs/view/1",
        "https://in.indeed.com/viewjob",
        "https://in.indeed.com/viewjob?jk=",
        "https://in.indeed.com/viewjob?jk=invalid%20id",
        "https://in.indeed.com:viewjob/viewjob?jk=2",
        "https://in.indeed.com/viewjob?&",
        "https://in.indeed.com/viewjob?jk=2&redirect=https%3A%2F%2Fevil.example",
        "https://www.linkedin.com/jobs/view/1?&",
        LINKEDIN + "#application",
        INDEED + "#application",
    ),
)
def test_load_targets_rejects_noncanonical_listing_identity_variants(tmp_path: Path, url: str):
    with pytest.raises(ValueError, match="canonical HTTPS"):
        load_targets(_manifest(tmp_path / "noncanonical.json", [{"url": url}]))


def test_canonical_indeed_listing_route_can_be_an_exact_active_prefix(tmp_path: Path):
    target = load_targets(
        _manifest(
            tmp_path / "canonical-prefix.json",
            [{"url": INDEED, "active_url_prefixes": [INDEED]}],
        )
    )[0]

    assert target.is_active(INDEED)


def test_canonical_indeed_active_prefix_must_keep_the_same_job_identity(tmp_path: Path):
    with pytest.raises(ValueError, match="target job"):
        load_targets(
            _manifest(
                tmp_path / "different-indeed-job.json",
                [{"url": INDEED, "active_url_prefixes": ["https://in.indeed.com/viewjob?jk=9"]}],
            )
        )


def test_active_prefix_matches_exact_route_but_not_unrelated_route():
    target = JobTarget(INDEED, (INDEED_ACTIVE,))

    assert target.is_active(INDEED_ACTIVE)
    assert not target.is_active("https://smartapply.indeed.com/beta/indeedapply/form/resume?jk=2")


def test_active_prefix_requires_a_url_boundary_not_a_partial_job_identifier():
    target = JobTarget(INDEED, (INDEED_ACTIVE,))

    assert not target.is_active(INDEED_ACTIVE + "0")


def test_active_prefix_with_trailing_route_separator_matches_a_child_step():
    apply_route = "https://www.linkedin.com/jobs/view/1/apply/"
    target = JobTarget(LINKEDIN, (apply_route,))

    assert target.is_active(apply_route + "review")


@pytest.mark.parametrize(
    "url",
    (
        "https://www.linkedin.com/",
        "https://www.linkedin.com/login",
        "https://www.linkedin.com/jobs/",
        "https://www.linkedin.com/jobs/search/?keywords=python",
        "https://www.linkedin.com/jobs/application/settings",
        "https://in.indeed.com/",
        "https://in.indeed.com/account/login",
        "https://in.indeed.com/jobs?q=python",
        "https://smartapply.indeed.com/beta/indeedapply/form/review-module",
    ),
)
def test_load_targets_rejects_non_listing_board_urls(tmp_path: Path, url: str):
    with pytest.raises(ValueError):
        load_targets(_manifest(tmp_path / "non-listing.json", [{"url": url}]))


@pytest.mark.parametrize(
    ("listing_url", "active_prefix"),
    (
        (LINKEDIN, "https://www.linkedin.com/"),
        (LINKEDIN, "https://www.linkedin.com/jobs/"),
        (INDEED, "https://smartapply.indeed.com/"),
        (LINKEDIN, INDEED_ACTIVE),
        (INDEED, "https://www.linkedin.com/jobs/view/2/apply/"),
        (LINKEDIN, "https://www.linkedin.com/jobs/view/9/apply/"),
        (INDEED, "https://smartapply.indeed.com/beta/indeedapply/form/review-module?jk=9"),
    ),
)
def test_active_prefixes_must_be_same_board_narrow_and_target_specific(
    tmp_path: Path, listing_url: str, active_prefix: str
):
    with pytest.raises(ValueError):
        load_targets(
            _manifest(
                tmp_path / "unsafe-prefix.json",
                [{"url": listing_url, "active_url_prefixes": [active_prefix]}],
            )
        )


@pytest.mark.parametrize(
    "jobs,error",
    [
        ([], "between one"),
        ([{"url": LINKEDIN}] * 6, "between one"),
        (["not-an-object"], "object"),
        ([{"url": None}], "HTTPS"),
        ([{"url": "https://example.test/job"}], "HTTPS"),
        ([{"url": LINKEDIN}, {"url": LINKEDIN}], "unique"),
        ([{"url": LINKEDIN, "active_url_prefixes": "bad"}], "active_url_prefixes"),
        ([{"url": LINKEDIN, "active_url_prefixes": ["https://example.test"]}], "active_url_prefixes"),
    ],
)
def test_load_targets_rejects_invalid_manifests(tmp_path: Path, jobs: list[object], error: str):
    with pytest.raises(ValueError, match=error):
        load_targets(_manifest(tmp_path / "invalid.json", jobs))


def test_apple_script_client_lists_and_opens_supported_urls():
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=f"{LINKEDIN}\n\n")

    chrome = ChromeAppleScript(runner=runner)
    assert chrome.list_tab_urls() == (LINKEDIN,)
    chrome.open_listing(INDEED)
    assert calls[1][-1] == INDEED
    with pytest.raises(ValueError, match="refusing"):
        chrome.open_listing("https://example.test")


def test_chrome_compatibility_open_script_never_creates_a_window_without_an_existing_session():
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="")

    ChromeAppleScript(runner=runner).open_listing(LINKEDIN)

    script = calls[0][2]
    assert "count of windows" in script
    assert "make new window" not in script.casefold()
    assert "activate" not in script.casefold()


def test_legacy_applescript_adapter_is_not_marked_as_a_live_smart_queue_adapter():
    chrome = ChromeAppleScript(runner=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=""))

    assert getattr(chrome, "smart_queue_adapter", None) is None


def test_apple_script_error_is_redacted():
    def runner(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="")

    with pytest.raises(ChromeAutomationError, match="unavailable"):
        ChromeAppleScript(runner=runner).list_tab_urls()


def test_apple_script_converts_runner_timeout_to_redacted_domain_error():
    secret = "https://www.linkedin.com/jobs/view/private-job-id"

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd=command, timeout=15, output=secret, stderr=secret)

    with pytest.raises(ChromeAutomationError) as raised:
        ChromeAppleScript(runner=runner).open_listing(secret)

    rendered = str(raised.value)
    assert secret not in rendered
    assert "osascript" not in rendered


def test_reconcile_records_open_timeout_as_redacted_cycle_failure():
    calls = 0

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(returncode=0, stdout="")
        raise subprocess.TimeoutExpired(cmd=command, timeout=15, output=LINKEDIN, stderr=LINKEDIN)

    result = reconcile(ChromeAppleScript(runner=runner), (JobTarget(LINKEDIN),))

    assert result.reopened == ()
    assert result.failed == (LINKEDIN,)


def test_reconcile_reopens_only_missing_and_records_failures():
    chrome = FakeChrome((LINKEDIN,), failures={INDEED})
    result = reconcile(chrome, (JobTarget(LINKEDIN), JobTarget(INDEED)))
    assert result.observed_open_tabs == 1
    assert result.missing_before_reopen == (INDEED,)
    assert result.reopened == ()
    assert result.failed == (INDEED,)


def test_status_counts_are_url_free_while_internal_cycle_keeps_urls_for_coordination():
    chrome = FakeChrome(())
    cycle = reconcile(chrome, (JobTarget(LINKEDIN),))

    assert status_counts(cycle) == {
        "failed": 0,
        "missing_before_reopen": 1,
        "observed_open_tabs": 0,
        "reopened": 1,
    }
    assert cycle.missing_before_reopen == (LINKEDIN,)
    assert cycle.reopened == (LINKEDIN,)
    assert LINKEDIN not in json.dumps(status_counts(cycle), sort_keys=True)


def test_reconcile_rejects_over_capacity_and_duplicate_targets_before_browser_access():
    chrome = FakeChrome(())
    six_targets = tuple(JobTarget(f"https://www.linkedin.com/jobs/view/{index}") for index in range(6))

    with pytest.raises(ValueError, match="between one and 5"):
        reconcile(chrome, six_targets)
    with pytest.raises(ValueError, match="unique"):
        reconcile(chrome, (JobTarget(LINKEDIN), JobTarget(LINKEDIN)))
    assert chrome.opened == []


def test_reconcile_and_watch_validate_iterables_and_target_values_before_browser_access():
    chrome = FakeChrome(())
    with pytest.raises(ValueError, match="iterable"):
        reconcile(chrome, object())
    with pytest.raises(ValueError, match="only JobTarget"):
        reconcile(chrome, [object()])
    with pytest.raises(ValueError, match="canonical listing"):
        reconcile(chrome, [JobTarget(LINKEDIN + "?trk=public_jobs")])
    with pytest.raises(ValueError, match="canonical HTTPS"):
        reconcile(chrome, [JobTarget("https://example.test/job")])
    with pytest.raises(ValueError, match="iterable"):
        run_watch(chrome, object(), interval_seconds=1, max_cycles=1)


def test_reconcile_preserves_active_redirect_and_reopens_other_listing():
    chrome = FakeChrome(("https://smartapply.indeed.com/beta/review",))
    result = reconcile(
        chrome,
        (JobTarget(LINKEDIN), JobTarget(INDEED, ("https://smartapply.indeed.com/",))),
    )
    assert result.missing_before_reopen == (LINKEDIN,)
    assert result.reopened == (LINKEDIN,)
    assert chrome.opened == [LINKEDIN]


def test_run_watch_honors_cycle_limit_and_validates_arguments():
    chrome = FakeChrome(())
    emitted: list[str] = []
    slept: list[float] = []
    run_watch(chrome, (JobTarget(LINKEDIN),), interval_seconds=2, max_cycles=2, sleep=slept.append, emit=emitted.append)
    assert len(emitted) == 2
    assert json.loads(emitted[0]) == {
        "failed": 0,
        "missing_before_reopen": 1,
        "observed_open_tabs": 0,
        "reopened": 1,
    }
    assert LINKEDIN not in emitted[0]
    assert slept == [2]
    with pytest.raises(ValueError, match="positive"):
        run_watch(chrome, (), interval_seconds=0, max_cycles=1)
    with pytest.raises(ValueError, match="max_cycles"):
        run_watch(chrome, (), interval_seconds=1, max_cycles=0)


def test_main_once_and_error_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    manifest = _manifest(tmp_path / "valid.json", [{"url": LINKEDIN}])
    fake = FakeChrome(())
    monkeypatch.setattr(_MODULE, "ChromeAppleScript", lambda: fake)
    assert main(["--manifest", str(manifest), "--once"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "failed": 0,
        "missing_before_reopen": 1,
        "observed_open_tabs": 0,
        "reopened": 1,
    }
    assert LINKEDIN not in output
    assert main(["--manifest", str(tmp_path / "missing.json"), "--once"]) == 2
    assert json.loads(capsys.readouterr().err)["ok"] is False


def test_main_watch_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    manifest = _manifest(tmp_path / "valid.json", [{"url": LINKEDIN}])
    called: dict[str, object] = {}
    monkeypatch.setattr(_MODULE, "ChromeAppleScript", lambda: FakeChrome(()))
    monkeypatch.setattr(_MODULE, "run_watch", lambda _chrome, _targets, **kwargs: called.update(kwargs))
    assert main(["--manifest", str(manifest), "--watch", "--interval-seconds", "3", "--max-cycles", "1"]) == 0
    assert called == {"interval_seconds": 3.0, "max_cycles": 1}


def test_main_external_adapter_preserves_explicit_bridge_argv_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    manifest = _manifest(tmp_path / "valid.json", [{"url": LINKEDIN}])
    fake = FakeChrome(())
    selected: dict[str, object] = {}

    def fake_create_adapter(name: str, **kwargs: object) -> FakeChrome:
        selected.update({"name": name, **kwargs})
        return fake

    monkeypatch.setattr(_MODULE, "create_adapter", fake_create_adapter)

    assert (
        main(
            [
                "--manifest",
                str(manifest),
                "--once",
                "--adapter",
                "external",
                "--adapter-timeout-seconds",
                "4.5",
                "--adapter-command",
                "browser-agent-bridge",
                "--browser",
                "firefox",
            ]
        )
        == 0
    )
    assert selected == {
        "name": "external",
        "command": ["browser-agent-bridge", "--browser", "firefox"],
        "timeout_seconds": 4.5,
    }
    output = capsys.readouterr().out
    assert json.loads(output)["reopened"] == 1
    assert LINKEDIN not in output


def test_main_rejects_external_command_for_chrome_compatibility_adapter(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    manifest = _manifest(tmp_path / "valid.json", [{"url": LINKEDIN}])

    assert (
        main(
            [
                "--manifest",
                str(manifest),
                "--once",
                "--adapter",
                "chrome-applescript",
                "--adapter-command",
                "must-not-run",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().err) == {"error": "ValueError", "ok": False}


def test_module_import_fails_closed_when_adapter_source_cannot_be_loaded(monkeypatch: pytest.MonkeyPatch):
    isolated_spec = importlib.util.spec_from_file_location("isolated_chrome_tab_watcher", _SCRIPT_PATH)
    assert isolated_spec and isolated_spec.loader
    isolated_module = importlib.util.module_from_spec(isolated_spec)
    monkeypatch.delitem(sys.modules, "browser_tab_adapter", raising=False)
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *_args, **_kwargs: None)

    with pytest.raises(ModuleNotFoundError, match="browser_tab_adapter"):
        isolated_spec.loader.exec_module(isolated_module)


def test_script_entrypoint_exits_cleanly_for_help(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT_PATH), "--help"])
    with pytest.raises(SystemExit) as exit_status:
        runpy.run_path(str(_SCRIPT_PATH), run_name="__main__")
    assert exit_status.value.code == 0


class _FlakyListChrome(FakeChrome):
    """Fails the first failing_cycles list_tab_urls calls, then succeeds."""

    def __init__(self, failing_cycles: int):
        super().__init__(())
        self._failing_cycles = failing_cycles

    def list_tab_urls(self) -> tuple[str, ...]:
        if self._failing_cycles > 0:
            self._failing_cycles -= 1
            raise ChromeAutomationError("Chrome tab automation is unavailable")
        return self.urls


_STATUS_COUNTS_ZERO = {
    "failed": 0,
    "missing_before_reopen": 1,
    "observed_open_tabs": 0,
    "reopened": 1,
}


def _failure_lines(emitted: list[str]) -> list[str]:
    return [line for line in emitted if json.loads(line).get("ok") is False]


def test_run_watch_recovers_after_transient_failure_and_redacts_the_error_line():
    emitted: list[str] = []
    run_watch(
        _FlakyListChrome(failing_cycles=1),
        (JobTarget(LINKEDIN),),
        interval_seconds=1,
        max_cycles=2,
        sleep=lambda _seconds: None,
        emit=emitted.append,
    )
    assert len(emitted) == 2
    assert json.loads(emitted[0]) == {"ok": False, "error": "ChromeAutomationError"}
    assert json.loads(emitted[1]) == _STATUS_COUNTS_ZERO
    failures = _failure_lines(emitted)
    assert len(failures) == 1
    assert LINKEDIN not in failures[0]
    assert LINKEDIN not in json.dumps(json.loads(failures[0]))


def test_run_watch_reraises_after_three_consecutive_failures():
    emitted: list[str] = []
    with pytest.raises(ChromeAutomationError):
        run_watch(
            _FlakyListChrome(failing_cycles=3),
            (JobTarget(LINKEDIN),),
            interval_seconds=1,
            max_cycles=3,
            sleep=lambda _seconds: None,
            emit=emitted.append,
        )
    assert len(emitted) == 3
    assert all(
        json.loads(line) == {"ok": False, "error": "ChromeAutomationError"} for line in emitted
    )


def test_run_watch_unlimited_mode_reraises_after_three_consecutive_failures():
    emitted: list[str] = []
    with pytest.raises(ChromeAutomationError):
        run_watch(
            _FlakyListChrome(failing_cycles=3),
            (JobTarget(LINKEDIN),),
            interval_seconds=1,
            max_cycles=None,
            sleep=lambda _seconds: None,
            emit=emitted.append,
        )
    assert len(emitted) == 3


def test_run_watch_resets_failure_counter_after_a_successful_cycle():
    emitted: list[str] = []
    run_watch(
        _FlakyListChrome(failing_cycles=2),
        (JobTarget(LINKEDIN),),
        interval_seconds=1,
        max_cycles=3,
        sleep=lambda _seconds: None,
        emit=emitted.append,
    )
    assert [json.loads(line).get("ok") is False for line in emitted] == [True, True, False]
    assert json.loads(emitted[-1]) == _STATUS_COUNTS_ZERO
    assert all(LINKEDIN not in line for line in _failure_lines(emitted))


def test_main_watch_returns_error_after_three_consecutive_failed_cycles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    manifest = _manifest(tmp_path / "valid.json", [{"url": LINKEDIN}])
    monkeypatch.setattr(_MODULE, "ChromeAppleScript", lambda: _FlakyListChrome(failing_cycles=3))
    assert main(["--manifest", str(manifest), "--watch", "--max-cycles", "3"]) == 2
    assert json.loads(capsys.readouterr().err) == {
        "ok": False,
        "error": "ChromeAutomationError",
    }
