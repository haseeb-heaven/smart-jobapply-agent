"""Model-agnostic bridge contracts: generic stdio + external argv bridges.

These tests assert the CONTRACT for the model-agnostic bridge migration and
never modify production behavior. They use injected streams/runners only and
never start a browser, inspect page content, or perform an application action.

The contract symbols (``browser_bridge_adapter.Std​ioBridgeAdapter``,
``smart_queue_daemon`` external ``--adapter`` CLI, and
``smart_queue_daemon_host.mjs``) have landed, so assertions bind them
directly and fail loudly on regression.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


_SCRIPTS_DIR = Path(__file__).parents[1] / "skills" / "easy-apply-tab-monitor" / "scripts"
_REPO_ROOT = Path(__file__).parents[1]
_HOST_PATH = _SCRIPTS_DIR / "smart_queue_daemon_host.mjs"


def _load(name: str, filename: str) -> object:
    path = _SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_BROWSER_ADAPTER = _load("browser_tab_adapter", "browser_tab_adapter.py")
_TAB_ERROR = _BROWSER_ADAPTER.BrowserAdapterError
_EXTERNAL = _BROWSER_ADAPTER.ExternalCommandAdapter

# Load the generic stdio bridge FIRST so the historical alias below resolves
# its try-import from sys.modules instead of path-executing a second copy.
# Two path-executions would create two distinct class objects and break the
# alias identity assertion.
_BRIDGE_PATH = _SCRIPTS_DIR / "browser_bridge_adapter.py"
if _BRIDGE_PATH.is_file():
    _BRIDGE = _load("browser_bridge_adapter", "browser_bridge_adapter.py")
    StdioBridgeAdapter = _BRIDGE.StdioBridgeAdapter
else:  # Pending-on-implementer: contract symbol does not exist yet.
    _BRIDGE = None
    StdioBridgeAdapter = None

_CODEX_MODULE = _load("codex_chrome_extension_adapter", "codex_chrome_extension_adapter.py")
CodexChromeExtensionAdapter = _CODEX_MODULE.CodexChromeExtensionAdapter

_DAEMON = _load("smart_queue_daemon", "smart_queue_daemon.py")
DaemonConfig = _DAEMON.DaemonConfig
DaemonConfigurationError = _DAEMON.DaemonConfigurationError

# F-001 hygiene: this file binds every contract symbol above through direct
# object references, so release the path-loader sys.modules keys. A later
# test module in the same pytest process then path-loads its own
# self-consistent copies instead of inheriting a bridge module whose error
# identity belongs to this file's adapter copy.
for _path_loaded_name in (
    "browser_tab_adapter",
    "browser_bridge_adapter",
    "codex_chrome_extension_adapter",
    "smart_queue_daemon",
):
    sys.modules.pop(_path_loaded_name, None)
del _path_loaded_name


def _needs_bridge() -> Any:
    assert StdioBridgeAdapter is not None
    return StdioBridgeAdapter


LINKEDIN = "https://www.linkedin.com/jobs/view/123456"
INDEED = "https://in.indeed.com/viewjob?jk=abc_123"


class ResponseStream:
    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.read_limits: list[int] = []

    def readline(self, size: int = -1) -> str:
        self.read_limits.append(size)
        return self.frames.pop(0) if self.frames else ""


class RequestStream:
    def __init__(self) -> None:
        self.frames: list[str] = []
        self.flush_count = 0

    def write(self, value: str) -> int:
        self.frames.append(value)
        return len(value)

    def flush(self) -> None:
        self.flush_count += 1


class StalledResponseStream:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.read_limits: list[int] = []

    def readline(self, size: int = -1) -> str:
        self.read_limits.append(size)
        self.started.set()
        self.release.wait(timeout=5)
        return _frame({"id": "request-1", "ok": True, "urls": []})


def _frame(payload: object) -> str:
    return json.dumps(payload, separators=(",", ":")) + "\n"


def _stdio(*responses: object) -> tuple[Any, RequestStream, ResponseStream]:
    bridge = _needs_bridge()
    requests = RequestStream()
    frames = [r if isinstance(r, str) else _frame(r) for r in responses]
    responses_stream = ResponseStream(frames)
    adapter = bridge(response_stream=responses_stream, request_stream=requests)
    return adapter, requests, responses_stream


def _assert_redacted(exception: BaseException, *private_values: str) -> None:
    rendered = str(exception)
    for value in private_values:
        assert value not in rendered


# --- 1. Alias identity -------------------------------------------------------


def test_codex_adapter_is_the_generic_stdio_bridge_alias() -> None:
    _needs_bridge()
    assert StdioBridgeAdapter is CodexChromeExtensionAdapter


def test_stdio_bridge_shares_the_redacted_browser_error() -> None:
    _needs_bridge()
    assert _BRIDGE.BrowserAdapterError is _TAB_ERROR
    assert _CODEX_MODULE.BrowserAdapterError is _TAB_ERROR


# --- 2. Stdio adapter contract -----------------------------------------------


def test_stdio_id_echo_mismatch_is_a_redacted_error() -> None:
    adapter, _requests, _responses = _stdio({"id": "request-2", "ok": True, "urls": []})

    with pytest.raises(_TAB_ERROR) as raised:
        adapter.list_tab_urls()

    _assert_redacted(raised.value, "request-2")


def test_stdio_unsafe_response_terminals_before_a_second_request_write() -> None:
    """An unsafe response cannot be consumed as a later request's reply."""

    private_url = "https://mail.example.test/inbox?private=value"
    adapter, requests, _responses = _stdio(
        {"id": "request-2", "ok": True, "urls": [private_url]},
        {"id": "request-2", "ok": True, "urls": []},
    )

    with pytest.raises(_TAB_ERROR) as raised:
        adapter.list_tab_urls()

    _assert_redacted(raised.value, private_url, "request-2")
    assert adapter.terminal is True
    # The valid-looking queued frame must never become an answer to a second
    # request after the first response loses its request/response boundary.
    with pytest.raises(_TAB_ERROR):
        adapter.list_tab_urls()
    assert len(requests.frames) == 1
    assert requests.flush_count == 1


def test_stdio_exact_request_failure_is_redacted_and_stream_remains_reusable() -> None:
    """A complete fixed failure frame preserves the next request boundary."""

    adapter, requests, _responses = _stdio(
        {"id": "request-1", "ok": False, "error": "request_failed"},
        {"id": "request-2", "ok": True, "urls": [LINKEDIN]},
    )

    with pytest.raises(_TAB_ERROR) as raised:
        adapter.list_tab_urls()

    assert str(raised.value) == "browser bridge rejected the request"
    assert adapter.terminal is False
    assert adapter.list_tab_urls() == (LINKEDIN,)
    assert len(requests.frames) == 2
    assert requests.flush_count == 2


@pytest.mark.parametrize(
    "response",
    (
        "not-json\n",
        {"id": "request-1", "ok": True, "urls": [], "diagnostic": "private-detail"},
        {"id": "request-1", "ok": True, "urls": "https://www.linkedin.com/jobs/view/123456"},
        {"id": "request-1", "ok": True, "urls": [LINKEDIN], "extra": True},
        {"id": "request-1", "ok": True, "urls": ["https://mail.example.test/inbox?private=value"]},
        {"id": "request-1", "ok": True, "urls": ["x" * 9000]},
    ),
    ids=("malformed", "extra-key", "non-list-urls", "extra-envelope-key", "non-listing-url", "overlong-entry"),
)
def test_stdio_list_rejects_untrusted_payloads_without_echo(response: object) -> None:
    private_url = "https://mail.example.test/inbox?private=value"
    adapter, _requests, _responses = _stdio(response)

    with pytest.raises(_TAB_ERROR) as raised:
        adapter.list_tab_urls()

    _assert_redacted(raised.value, private_url, "private-detail", "x" * 100)


def test_stdio_oversize_url_array_is_rejected_without_echo() -> None:
    urls = [f"https://www.linkedin.com/jobs/view/{100000 + i}" for i in range(600)]
    adapter, _requests, _responses = _stdio({"id": "request-1", "ok": True, "urls": urls})

    with pytest.raises(_TAB_ERROR) as raised:
        adapter.list_tab_urls()

    _assert_redacted(raised.value, urls[0])


def test_stdio_timeout_terminals_the_stream_against_reuse() -> None:
    _needs_bridge()
    requests = RequestStream()
    responses = StalledResponseStream()
    adapter = StdioBridgeAdapter(
        response_stream=responses,
        request_stream=requests,
        response_timeout_seconds=0.025,
    )

    started = time.monotonic()
    try:
        with pytest.raises(_TAB_ERROR):
            adapter.list_tab_urls()
        assert adapter.terminal is True
        with pytest.raises(_TAB_ERROR):
            adapter.list_tab_urls()
        assert time.monotonic() - started < 0.6
        assert requests.flush_count == 1
    finally:
        responses.release.set()


@pytest.mark.parametrize(
    "url",
    (
        "https://example.test/jobs/123",
        "https://www.linkedin.com/jobs/search/?keywords=python",
        "https://in.indeed.com/jobs?q=python",
        "http://www.linkedin.com/jobs/view/123456",
    ),
)
def test_stdio_open_listing_refuses_non_listing_without_emitting(url: str) -> None:
    adapter, requests, responses = _stdio({"id": "request-1", "ok": True})

    with pytest.raises(ValueError):
        adapter.open_listing(url)

    assert requests.frames == []
    assert responses.read_limits == []


# --- 3. Daemon bridge selection ----------------------------------------------


def _private_paths(tmp_path: Path) -> tuple[str, str]:
    runtime = Path(__file__).parents[1] / "jobapply_agent" / "private" / "test-model-agnostic" / tmp_path.name
    runtime.mkdir(parents=True, exist_ok=True)
    intake = runtime / "candidate-intake.json"
    if not intake.is_file():
        intake.write_text(
            json.dumps({
                "schema_version": 1, "documents": [], "approved_facts": {},
                "unknown_fields": [], "contradictions": [], "pending_facts": [],
            }),
            encoding="utf-8",
        )
    return str(intake), str(runtime / "queue.sqlite3")


def test_stdio_timeout_reader_thread_is_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading as threading_module

    captured: dict[str, object] = {}
    real_thread = threading_module.Thread

    def recording_thread(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(_BRIDGE.threading, "Thread", recording_thread)
    adapter, _requests, _responses = _stdio({"id": "request-1", "ok": True, "urls": []})

    assert adapter.list_tab_urls() == ()
    assert captured.get("daemon") is True


def test_daemon_config_accepts_host_provided_and_external_kinds() -> None:
    intake, database = _private_paths(Path("duty"))
    host = DaemonConfig.from_mapping({
        "database_path": database, "active_intake_path": intake, "bridge": {"adapter": "host-provided"},
    })
    assert host.bridge_adapter == "host-provided"

    external = DaemonConfig.from_mapping({
        "database_path": database,
        "active_intake_path": intake,
        "bridge": {"adapter": "external", "command": ["fake-bridge"]},
    })
    assert external.bridge_adapter == "external"


@pytest.mark.parametrize("adapter", ("automatic", "untrusted-command", "chrome-applescript"))
def test_daemon_config_rejects_unknown_bridge_kinds_without_echo(adapter: str) -> None:
    with pytest.raises(DaemonConfigurationError) as raised:
        DaemonConfig.from_mapping({
            "database_path": "jobapply_agent/private/queue.sqlite3",
            "active_intake_path": "jobapply_agent/private/candidate_intake.json",
            "bridge": {"adapter": adapter},
        })

    assert adapter not in str(raised.value)


def _run_main(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    spawned: list[str] = []

    def forbidden_adapter(*args: object, **kwargs: object) -> object:
        spawned.append("adapter")
        raise AssertionError("bridge failure must not spawn a child adapter")

    def forbidden_from_config(*args: object, **kwargs: object) -> object:
        spawned.append("daemon")
        raise AssertionError("bridge failure must not construct the daemon")

    monkeypatch.setattr(_DAEMON, "CodexChromeExtensionAdapter", forbidden_adapter)
    monkeypatch.setattr(_DAEMON.SmartQueueDaemon, "from_config", forbidden_from_config)
    try:
        code = _DAEMON.main(argv)
    except SystemExit as exited:
        assert exited.code == 2
        code = 2
    assert spawned == []
    return code


@pytest.mark.parametrize(
    ("argv_tail", "label", "secrets"),
    (
        ([], "no-bridge-flag", ()),
        (["--bridge-stdio", "--bridge-stdio"], "bridge-stdio-twice", ()),
        (
            ["--bridge-stdio", "--adapter", "external", "--adapter-command", "secret-both-bridges-cmd"],
            "both-bridges",
            ("secret-both-bridges-cmd",),
        ),
        (["--adapter", "external"], "external-without-command", ()),
    ),
)
def test_daemon_rejects_zero_or_two_plus_bridge_selections_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    argv_tail: list[str], label: str, secrets: tuple[str, ...],
) -> None:
    intake, database = _private_paths(tmp_path)
    argv = ["--candidate-intake", intake, "--database", database, *argv_tail, "--max-ticks", "0"]

    assert _run_main(argv, monkeypatch) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    for secret in (*secrets, intake, database):
        assert secret not in captured.err
    assert "https://" not in captured.err


def test_daemon_accepts_valid_external_bridge_without_echo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    intake, database = _private_paths(tmp_path)
    constructed: list[list[str]] = []

    class FakeExternal:
        def __init__(self, command: object, **kwargs: object) -> None:
            constructed.append(list(command))

        def list_tab_urls(self) -> tuple[str, ...]:
            return ()

    states: list[object] = []

    class FakeDaemon:
        @classmethod
        def from_config(cls, config: object, **kwargs: object) -> "FakeDaemon":
            states.append(config)
            return cls()

        def run(self, *, max_ticks: int) -> object:
            return _DAEMON.DaemonStatus(max_ticks, 0, 0, 0, 0, 0)

    monkeypatch.setattr(_DAEMON, "ExternalCommandAdapter", FakeExternal, raising=False)
    monkeypatch.setattr(_DAEMON.SmartQueueDaemon, "from_config", FakeDaemon.from_config)

    code = _DAEMON.main([
        "--candidate-intake", intake, "--database", database,
        "--adapter", "external", "--adapter-command", "fake-bridge", "--browser", "firefox",
        "--max-ticks", "0",
    ])

    assert code == 0
    assert constructed == [["fake-bridge", "--browser", "firefox"]]
    assert len(states) == 1
    assert json.loads(capsys.readouterr().out)["ticks_completed"] == 0


# --- 3b. Bridge argv / daemon flag boundary (F-006, F-007) -------------------
#
# argparse consumes recognized daemon flags anywhere, including after the
# --adapter-command marker, so bridge argv must not reuse daemon flag
# spellings. A collision with a bridge-selection flag is a redacted
# configuration error, never silent bridge misrouting. The extras merge
# scans only post-marker tokens so a daemon value sharing a spelling with
# a bridge token cannot false-reject the bridge command.


def test_bridge_token_reusing_daemon_flag_spelling_is_redacted_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    intake, database = _private_paths(tmp_path)
    argv = [
        "--candidate-intake", intake, "--database", database,
        "--adapter", "external", "--adapter-command", "fake-bridge", "--bridge-stdio",
        "--max-ticks", "0",
    ]

    assert _run_main(argv, monkeypatch) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    for secret in (intake, database, "fake-bridge"):
        assert secret not in captured.err
    assert "https://" not in captured.err


def test_extras_merge_ignores_daemon_value_matching_bridge_token() -> None:
    raw = ["--database", "--browser", "--adapter-command", "fake-bridge", "--browser"]

    merged = _DAEMON._bridge_command_from_extras(raw, ["fake-bridge"], ["--browser"])

    assert merged == ["fake-bridge", "--browser"]


def test_extras_merge_rejects_unknown_token_before_marker() -> None:
    with pytest.raises(DaemonConfigurationError):
        _DAEMON._bridge_command_from_extras(["--mystery", "--adapter-command", "fake-bridge"], ["fake-bridge"], ["--mystery"])


# --- 4. External bridge adversarial ------------------------------------------


def _external_runner(stdout: str, **kwargs: Any) -> Any:
    def runner(argv: list[str], **run_kwargs: Any) -> SimpleNamespace:
        for key in ("shell", "capture_output", "text", "timeout"):
            assert key in run_kwargs, f"runner must receive {key}"
        assert run_kwargs.get("shell") is False
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="", **kwargs)

    return runner


@pytest.mark.parametrize(
    ("stdout", "label"),
    (
        ('{"urls": []}', "dict-payload"),
        ('["https://example.test", 3]', "non-string-entry"),
        ("null", "null-payload"),
    ),
)
def test_external_structural_violations_are_redacted_errors(stdout: str, label: str) -> None:
    secret = f"secret-{label}"
    runner = _external_runner(stdout)

    with pytest.raises(_TAB_ERROR) as raised:
        _EXTERNAL((secret,), runner=runner).list_tab_urls()

    rendered = str(raised.value)
    assert secret not in rendered
    assert stdout not in rendered


def test_external_oversize_array_is_a_redacted_error() -> None:
    urls = [f"https://www.linkedin.com/jobs/view/{200000 + i}" for i in range(600)]
    runner = _external_runner(json.dumps(urls))

    with pytest.raises(_TAB_ERROR) as raised:
        _EXTERNAL(("fake-bridge",), runner=runner).list_tab_urls()

    assert urls[0] not in str(raised.value)


def test_external_overlong_entry_is_a_redacted_error() -> None:
    long_url = "https://www.linkedin.com/jobs/view/" + "1" * 9000
    runner = _external_runner(json.dumps([long_url]))

    with pytest.raises(_TAB_ERROR) as raised:
        _EXTERNAL(("fake-bridge",), runner=runner).list_tab_urls()

    assert long_url not in str(raised.value)
    assert long_url[:100] not in str(raised.value)


@pytest.mark.parametrize(
    "bad_url",
    (
        "https://mail.example.test/inbox?private=value",
        "https://user:pass@www.linkedin.com/jobs/view/123456",
        "http://www.linkedin.com/jobs/view/123456",
        "https://www.linkedin.com/jobs/view/123456?jk=abc_123",
        "https://in.indeed.com/jobs?q=python",
    ),
    ids=("non-listing", "credentialed", "non-https", "linkedin-jk-param", "search-page"),
)
def test_external_list_ignores_rather_than_returns_non_listing_urls(bad_url: str) -> None:
    runner = _external_runner(json.dumps([bad_url, LINKEDIN]))

    assert _EXTERNAL(("fake-bridge",), runner=runner).list_tab_urls() == (LINKEDIN,)


def test_external_oversize_stdout_is_a_redacted_error() -> None:
    oversized = "[" + json.dumps(LINKEDIN) + "," + " " * 1_048_576 + "]"
    runner = _external_runner(oversized)

    with pytest.raises(_TAB_ERROR) as raised:
        _EXTERNAL(("fake-bridge",), runner=runner).list_tab_urls()

    assert LINKEDIN not in str(raised.value)
    assert oversized[:100] not in str(raised.value)


def test_external_list_ignores_mixed_batch_without_raising() -> None:
    payload = json.dumps([
        "https://mail.example.test/inbox?private=value",
        "https://user:pass@www.linkedin.com/jobs/view/123456",
        LINKEDIN,
        INDEED,
    ])
    runner = _external_runner(payload)

    assert _EXTERNAL(("fake-bridge",), runner=runner).list_tab_urls() == (LINKEDIN, INDEED)


# --- 4b. Layered list contract parity (F-003) ---------------------------------
#
# The stdio adapter speaks to an already-filtering host, so a non-canonical
# URL is a protocol violation and fails the whole batch closed. The external
# argv adapter speaks to raw bridge output, so non-listing tabs are skipped
# as not-managed. Both adapters agree on all-canonical batches and on
# structural violations (non-list / oversize payloads).


def test_layered_adapters_agree_on_all_canonical_batch() -> None:
    _needs_bridge()
    stdio, _requests, _responses = _stdio({"id": "request-1", "ok": True, "urls": [LINKEDIN, INDEED]})
    external = _EXTERNAL(("fake-bridge",), runner=_external_runner(json.dumps([LINKEDIN, INDEED])))

    assert stdio.list_tab_urls() == (LINKEDIN, INDEED)
    assert external.list_tab_urls() == (LINKEDIN, INDEED)


@pytest.mark.parametrize(
    "payload",
    (
        ["https://example.test/jobs/1", 3],
        "null",
    ),
    ids=("non-string-entry", "null-payload"),
)
def test_layered_adapters_agree_on_structural_violations(payload: object) -> None:
    _needs_bridge()
    raw = json.dumps(payload)
    stdio, _requests, _responses = _stdio(raw + "\n")
    external = _EXTERNAL(("fake-bridge",), runner=_external_runner(raw))

    with pytest.raises(_TAB_ERROR):
        stdio.list_tab_urls()
    with pytest.raises(_TAB_ERROR):
        external.list_tab_urls()


def test_layered_adapters_agree_on_oversize_violation() -> None:
    _needs_bridge()
    urls = [f"https://www.linkedin.com/jobs/view/{300000 + i}" for i in range(600)]
    raw = json.dumps(urls)
    stdio, _requests, _responses = _stdio({"id": "request-1", "ok": True, "urls": urls})
    external = _EXTERNAL(("fake-bridge",), runner=_external_runner(raw))

    with pytest.raises(_TAB_ERROR) as stdio_raised:
        stdio.list_tab_urls()
    with pytest.raises(_TAB_ERROR) as external_raised:
        external.list_tab_urls()
    _assert_redacted(stdio_raised.value, urls[0])
    assert urls[0] not in str(external_raised.value)


def test_layered_mixed_batch_stdio_fails_while_external_skips() -> None:
    _needs_bridge()
    bad_url = "https://mail.example.test/inbox?private=value"
    stdio, _requests, _responses = _stdio({"id": "request-1", "ok": True, "urls": [bad_url, LINKEDIN]})
    external = _EXTERNAL(("fake-bridge",), runner=_external_runner(json.dumps([bad_url, LINKEDIN])))

    with pytest.raises(_TAB_ERROR) as raised:
        stdio.list_tab_urls()
    _assert_redacted(raised.value, bad_url)
    assert external.list_tab_urls() == (LINKEDIN,)


def test_external_open_listing_passes_canonical_argv_without_shell() -> None:
    observed: list[tuple[list[str], dict[str, Any]]] = []

    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        observed.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    _EXTERNAL(
        ("browser-agent-bridge", "--browser", "firefox"), runner=runner,
    ).open_listing("https://WWW.LinkedIn.com/jobs/view/123456/?trk=public_jobs&utm_source=agent")

    assert observed[0][0] == ["browser-agent-bridge", "--browser", "firefox", "open-listing", LINKEDIN]
    assert observed[0][1].get("shell") is False
    assert not any(isinstance(part, str) and ("&&" in part or ";" in part) for part in observed[0][0])


def test_external_open_rejects_non_listing_before_runner_use() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(ValueError, match="listing"):
        _EXTERNAL(("fake-bridge",), runner=runner).open_listing("https://user:pass@www.linkedin.com/jobs/view/1")

    assert calls == []


def test_external_runner_timeout_is_redacted() -> None:
    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"], output="private-stdout")

    with pytest.raises(_TAB_ERROR) as raised:
        _EXTERNAL(("private-bridge",), runner=runner).list_tab_urls()

    assert "private-bridge" not in str(raised.value)
    assert "private-stdout" not in str(raised.value)


# --- 5. Generic host binding -------------------------------------------------


def test_generic_daemon_host_exports_model_agnostic_starters() -> None:
    """Behaviorally assert the generic host module loads and exports both starters.

    This runs the real Node module (no browser needed) instead of grepping
    source text, so it proves the importable contract the docs promise.
    """

    completed = subprocess.run(
        [
            "node", "-e",
            "import('./skills/easy-apply-tab-monitor/scripts/smart_queue_daemon_host.mjs')"
            ".then(m=>console.log(typeof m.startSmartQueueDaemonHost,"
            " typeof m.startOrGetSmartQueueDaemonHost))",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "function function"
