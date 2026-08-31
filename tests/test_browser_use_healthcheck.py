import importlib.util
from pathlib import Path
import sys
import time

import pytest


_SCRIPT_PATH = Path(__file__).parents[1] / "job_profession" / "scripts" / "browser_use_healthcheck.py"
_SPEC = importlib.util.spec_from_file_location("browser_use_healthcheck", _SCRIPT_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
load_browser_use_credentials = _MODULE.load_browser_use_credentials
profile_readiness = _MODULE.profile_readiness


def test_loader_uses_only_keys_in_browser_use_section(tmp_path: Path):
    credentials_file = tmp_path / "mixed.txt"
    credentials_file.write_text(
        "Other Service:\nsecret_elsewhere\nBrowser Use:\nhttps://api.browser-use.com/api/v3/sessions\nbu_allowed-key_123\nbu_second-key_456\nbu-max\nAnother Service:\nsecret_elsewhere\n",
        encoding="utf-8",
    )

    credentials = load_browser_use_credentials(credentials_file)

    assert credentials.base_url == "https://api.browser-use.com/api/v3"
    assert credentials.api_key == "bu_allowed-key_123"
    assert credentials.fingerprint != credentials.api_key


def test_loader_rejects_key_index_outside_browser_use_section(tmp_path: Path):
    credentials_file = tmp_path / "mixed.txt"
    credentials_file.write_text("Browser Use:\nbu_only-key\nOther:\nbu_not_selected\n", encoding="utf-8")

    try:
        load_browser_use_credentials(credentials_file, key_index=1)
    except ValueError as error:
        assert "found 1 key" in str(error)
    else:
        raise AssertionError("Expected missing Browser Use key index to be rejected")


@pytest.mark.parametrize(
    "base_url",
    (
        "http://api.browser-use.com/api/v3",
        "https://api.browser-use.com.evil.test/api/v3",
        "https://evil-browser-use.com/api/v3",
    ),
)
def test_loader_rejects_non_https_or_untrusted_browser_use_hosts(tmp_path: Path, base_url: str):
    credentials_file = tmp_path / "mixed.txt"
    credentials_file.write_text(f"Browser Use:\n{base_url}\nbu_allowed-key_123\n", encoding="utf-8")

    with pytest.raises(ValueError, match="trusted HTTPS Browser Use host"):
        load_browser_use_credentials(credentials_file)


def test_loader_times_out_when_credential_file_read_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    credentials_file = tmp_path / "mixed.txt"

    def slow_read_text(self: Path, *, encoding: str) -> str:
        time.sleep(0.2)
        return "Browser Use:\nbu_allowed-key_123\n"

    monkeypatch.setattr(Path, "read_text", slow_read_text)

    with pytest.raises(TimeoutError):
        load_browser_use_credentials(credentials_file, file_read_timeout_seconds=0.01)


def test_profile_readiness_returns_only_aggregate_login_signals():
    readiness = profile_readiness(
        {
            "items": [
                {"id": "do-not-return", "name": "private", "cookieDomains": ["linkedin.com", "example.test"]},
                {"id": "also-private", "cookieDomains": ["in.indeed.com"]},
            ]
        }
    )

    assert readiness == {"profile_count": 2, "linkedin_login_ready": True, "indeed_login_ready": True}


def test_profile_readiness_rejects_lookalike_cookie_domains():
    readiness = profile_readiness(
        {
            "items": [
                {"id": "do-not-return", "cookieDomains": ["evil-linkedin.com", "indeed.com.evil.test"]},
            ]
        }
    )

    assert readiness == {"profile_count": 1, "linkedin_login_ready": False, "indeed_login_ready": False}
