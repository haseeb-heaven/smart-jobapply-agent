import importlib.util
import math
from pathlib import Path
import sys

import pytest


_SCRIPT_PATH = Path(__file__).parents[1] / "job_profession" / "scripts" / "browser_use_linkedin_session.py"
_SPEC = importlib.util.spec_from_file_location("browser_use_linkedin_session", _SCRIPT_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
select_linkedin_profile = _MODULE.select_linkedin_profile
BrowserUseCredentials = _MODULE.BrowserUseCredentials
start_read_only_discovery = _MODULE.start_read_only_discovery


def test_selects_only_profile_with_linkedin_cookie_domain():
    profile_id = select_linkedin_profile(
        {"items": [{"id": "indeed-profile", "cookieDomains": ["indeed.com"]}, {"id": "linkedin-profile", "cookieDomains": ["www.linkedin.com"]}]}
    )

    assert profile_id == "linkedin-profile"


def test_does_not_select_profile_without_linkedin_domain():
    assert select_linkedin_profile({"items": [{"id": "other", "cookieDomains": ["example.test"]}]}) is None


def test_does_not_select_lookalike_linkedin_cookie_domain():
    assert select_linkedin_profile({"items": [{"id": "evil", "cookieDomains": ["evil-linkedin.com"]}]}) is None


@pytest.mark.parametrize("maximum_cost_usd", (0.0, -1.0, math.inf, -math.inf, math.nan))
def test_read_only_session_rejects_non_finite_or_non_positive_cost_before_network(
    monkeypatch: pytest.MonkeyPatch, maximum_cost_usd: float
):
    monkeypatch.setattr(_MODULE, "_request", lambda *args, **kwargs: pytest.fail("network request must not run"))

    with pytest.raises(ValueError, match="finite positive"):
        start_read_only_discovery(
            BrowserUseCredentials("https://api.browser-use.com/api/v3", "bu_allowed-key_123"),
            maximum_cost_usd=maximum_cost_usd,
        )
