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


LINKEDIN = "https://www.linkedin.com/jobs/view/1"
INDEED = "https://in.indeed.com/viewjob?jk=2"


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
            [{"url": LINKEDIN}, {"url": INDEED, "active_url_prefixes": ["https://smartapply.indeed.com/"]}],
        )
    )
    assert targets[1].is_active("https://smartapply.indeed.com/beta/review")
    assert not targets[0].is_active("https://www.linkedin.com/jobs/view/9")


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


def test_apple_script_error_is_redacted():
    def runner(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout="")

    with pytest.raises(ChromeAutomationError, match="unavailable"):
        ChromeAppleScript(runner=runner).list_tab_urls()


def test_apple_script_preserves_runner_timeout():
    def runner(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd="osascript", timeout=15)

    with pytest.raises(subprocess.TimeoutExpired):
        ChromeAppleScript(runner=runner).list_tab_urls()


def test_reconcile_reopens_only_missing_and_records_failures():
    chrome = FakeChrome((LINKEDIN,), failures={INDEED})
    result = reconcile(chrome, (JobTarget(LINKEDIN), JobTarget(INDEED)))
    assert result.observed_open_tabs == 1
    assert result.missing_before_reopen == (INDEED,)
    assert result.reopened == ()
    assert result.failed == (INDEED,)


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
    assert json.loads(capsys.readouterr().out)["reopened"] == [LINKEDIN]
    assert main(["--manifest", str(tmp_path / "missing.json"), "--once"]) == 2
    assert json.loads(capsys.readouterr().err)["ok"] is False


def test_main_watch_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    manifest = _manifest(tmp_path / "valid.json", [{"url": LINKEDIN}])
    called: dict[str, object] = {}
    monkeypatch.setattr(_MODULE, "ChromeAppleScript", lambda: FakeChrome(()))
    monkeypatch.setattr(_MODULE, "run_watch", lambda _chrome, _targets, **kwargs: called.update(kwargs))
    assert main(["--manifest", str(manifest), "--watch", "--interval-seconds", "3", "--max-cycles", "1"]) == 0
    assert called == {"interval_seconds": 3.0, "max_cycles": 1}


def test_script_entrypoint_exits_cleanly_for_help(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT_PATH), "--help"])
    with pytest.raises(SystemExit) as exit_status:
        runpy.run_path(str(_SCRIPT_PATH), run_name="__main__")
    assert exit_status.value.code == 0
