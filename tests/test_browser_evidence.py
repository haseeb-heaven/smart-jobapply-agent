"""Contract tests for redacted browser-validation evidence records.

Evidence records deliberately contain only bounded, opaque identifiers and
counts.  These tests validate the checked-in schema without loading a live
browser session, an artifact, or a candidate record.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError


SCHEMA_PATH = Path(__file__).parents[1] / "docs" / "testing" / "browser-evidence.schema.json"


def _schema() -> dict[str, object]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _evidence(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "case_id": "synthetic-refill-01",
        "agent": "playwright",
        "transport": "playwright-context",
        "environment": "ci",
        "browser": "firefox",
        "runtime_versions": {"agent": "test-agent-1", "browser": "test-browser-1"},
        "automated": "passed",
        "live": "not_run",
        "human_manual": "not_run",
        "human_signoff_ref": None,
        "opened_count": 2,
        "inferred_outcome_count": 0,
        "artifact_ref": "synthetic-artifact-01",
    }
    value.update(overrides)
    return value


def _validator() -> Draft202012Validator:
    validator = Draft202012Validator(_schema())
    validator.check_schema(_schema())
    return validator


def _assert_schema_accepts(payload: dict[str, object]) -> None:
    _validator().validate(payload)


def _assert_schema_rejects(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _validator().validate(payload)


def test_schema_is_closed_and_accepts_a_redacted_synthetic_record():
    schema = _schema()
    assert schema["additionalProperties"] is False
    _assert_schema_accepts(_evidence())


@pytest.mark.parametrize(
    "field,value",
    (
        ("transport", "firefox-profile"),
        ("automated", "unknown"),
        ("browser", "safari"),
        ("inferred_outcome_count", 1),
        ("opened_count", 11),
    ),
)
def test_schema_rejects_unsupported_statuses_and_counts(field: str, value: object):
    _assert_schema_rejects(_evidence(**{field: value}))


def test_schema_rejects_extra_url_or_token_bearing_fields():
    record = _evidence()
    record["listing_url"] = "https://www.linkedin.com/jobs/view/123456?token=private"

    _assert_schema_rejects(record)


def test_schema_rejects_fabricated_human_pass_without_an_opaque_signoff():
    _assert_schema_rejects(_evidence(human_manual="passed", human_signoff_ref=None))


def test_schema_accepts_human_pass_only_with_an_opaque_signoff_reference():
    _assert_schema_accepts(
        _evidence(
            automated="passed",
            live="passed",
            human_manual="passed",
            human_signoff_ref="human-check-01",
        )
    )


def test_schema_rejects_url_bearing_runtime_version_values():
    _assert_schema_rejects(_evidence(runtime_versions={"agent": "test-agent", "browser": "https://private.example.test"}))


@pytest.mark.parametrize(
    "payload",
    (
        _evidence(human_manual="passed", human_signoff_ref="human-check-01", live="not_run"),
        _evidence(human_manual="passed", human_signoff_ref="human-check-01", automated="not_run"),
    ),
)
def test_human_pass_requires_prior_automated_and_live_passes(payload: dict[str, object]):
    _assert_schema_rejects(payload)
