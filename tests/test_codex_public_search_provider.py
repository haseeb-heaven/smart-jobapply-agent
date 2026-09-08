import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

P = Path(__file__).parents[1] / "skills/easy-apply-tab-monitor/scripts/codex_public_search_provider.py"
s = importlib.util.spec_from_file_location("provider", P)
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)
REQUEST = {
    "schema_version": 1,
    "limit": 1,
    "queries": [{"query_id": "q1", "platform": "linkedin", "keywords": "Python backend", "location": ""}],
}
OUTPUT = {
    "schema_version": 1,
    "listings": [
        {
            "query_id": "q1",
            "platform": "linkedin",
            "url": "https://www.linkedin.com/jobs/view/1",
            "title": "Python",
            "company": "Example",
            "description": "Python required; Go preferred; Rust or Java acceptable.",
        }
    ],
}


def events(value):
    # Codex exec --json emits item.completed with nested agent_message/text.
    return "\n".join(
        json.dumps(event)
        for event in [
            {"type": "thread.started", "thread_id": "fixture-thread"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "Searching."}},
            {"type": "item.completed", "item": {"id": "item_1", "type": "web_search", "query": "public jobs"}},
            {"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": json.dumps(value)}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}},
        ]
    )


def test_real_cli_event_contract_and_hardened_command():
    def runner(args):
        assert args[:3] == ["codex", "exec", "--json"]
        for flag in [
            "--ignore-user-config",
            "--ephemeral",
            "read-only",
            'approval_policy="never"',
            'web_search="live"',
        ]:
            assert flag in args
        disabled = [args[i + 1] for i, value in enumerate(args) if value == "--disable"]
        assert set(disabled) == {
            "shell_tool",
            "unified_exec",
            "apps",
            "plugins",
            "browser_use",
            "browser_use_external",
            "computer_use",
            "multi_agent",
            "view_image",
        }
        assert "code_mode_host" not in disabled
        assert args[args.index("-C") + 1] == "/private/tmp"
        assert "United Arab Emirates" in args[-1] and "worldwide remote" in args[-1]
        assert "preferred qualifications and alternatives" in args[-1]
        assert "multiple distinct listings per query" in args[-1]
        assert "in person means on-site" in args[-1]
        assert "otherwise use an empty string" in args[-1]
        assert "never infer an unseen location component" in args[-1]
        return events(OUTPUT)

    assert m.run(json.dumps(REQUEST), runner) == OUTPUT


def test_actual_host_queries_accepted():
    spec = importlib.util.spec_from_file_location("provider_host", P.with_name("external_smart_queue_assistant.py"))
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    queries, _ = host._queries("")
    result = m.request(json.dumps({"schema_version": 1, "limit": 20, "queries": queries}))
    assert {q["platform"] for q in result["queries"]} == {"linkedin", "indeed"}


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"limit": True},
        {"limit": 21},
        {"queue": "private"},
        {"queries": []},
        {"queries": [{"query_id": "q1", "query": "legacy"}]},
        {"queries": REQUEST["queries"] * 2},
        {"queries": [{**REQUEST["queries"][0], "platform": []}]},
        {"queries": [{**REQUEST["queries"][0], "query_id": "q/private"}]},
    ],
)
def test_invalid_requests_fail_before_worker(change):
    with pytest.raises(ValueError):
        m.run(json.dumps({**REQUEST, **change}), lambda _: pytest.fail("worker started"))


@pytest.mark.parametrize(
    "change",
    [
        {"title": ""},
        {"title": "x" * 513},
        {"description": "x" * 12001},
        {"location": []},
        {"work_mode": None},
        {"posted_at": False},
        {"fit_score": 99},
        {"query_id": "q2"},
        {"platform": "indeed"},
    ],
)
def test_invalid_listing_schema_or_binding_rejected(change):
    value = {**OUTPUT, "listings": [{**OUTPUT["listings"][0], **change}]}
    with pytest.raises(ValueError):
        m.run(json.dumps(REQUEST), lambda _: events(value))


def test_empty_results_and_nullable_unknowns():
    assert m.run(json.dumps(REQUEST), lambda _: events({"schema_version": 1, "listings": []}))["listings"] == []
    value = {**OUTPUT, "listings": [{**OUTPUT["listings"][0], "posted_at": None}]}
    assert m.run(json.dumps(REQUEST), lambda _: events(value)) == value


def test_legacy_event_and_oversized_final_fail():
    for stream in [
        json.dumps({"type": "agent_message", "message": json.dumps(OUTPUT)}),
        events("x" * (m.MAX_OUTPUT + 1)),
    ]:
        with pytest.raises(ValueError):
            m.final_json(stream)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_real_subprocess_output_cap(stream, monkeypatch):
    monkeypatch.setattr(m, "MAX_STREAM", 1024)
    with pytest.raises(ValueError, match="public search provider failed"):
        m.bounded_worker([sys.executable, "-c", f"import sys; sys.{stream}.write('x'*10000)"])


def test_real_subprocess_timeout_and_nonzero(monkeypatch):
    monkeypatch.setattr(m, "TIMEOUT_SECONDS", 0.1)
    started = time.monotonic()
    with pytest.raises(ValueError):
        m.bounded_worker([sys.executable, "-c", "import time; time.sleep(30)"])
    assert time.monotonic() - started < 3
    with pytest.raises(ValueError):
        m.bounded_worker([sys.executable, "-c", "raise SystemExit(3)"])


def test_real_subprocess_drains_stderr_and_reads_stdout():
    assert m.bounded_worker([sys.executable, "-c", "import sys; sys.stderr.write('diagnostic'); print('ok')"]) == "ok\n"


def test_cli_caps_input_and_emits_no_private_error():
    result = subprocess.run([sys.executable, str(P)], input=b"private" * m.MAX_INPUT, capture_output=True, timeout=5)
    assert result.returncode == 2
    assert result.stdout == result.stderr == b""
