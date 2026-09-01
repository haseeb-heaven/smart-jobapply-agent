#!/usr/bin/env python3
"""Install the opt-in local launchd configuration for discovery/export only."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
import textwrap
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Mapping
from xml.sax.saxutils import escape as xml_escape

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLIST_NAME = "com.haseeb.smart-jobapply-agent.plist"
SOURCE_PLIST = PROJECT_ROOT / "launchd" / PLIST_NAME
RUNTIME_ROOT = Path.home() / "Library" / "Caches" / "smart_jobapply_agent_runtime"
RUNTIME_BOOTSTRAP = RUNTIME_ROOT / "launchd_discover_bootstrap.sh"
RUNTIME_SRC = RUNTIME_ROOT / "src" / "jobapply_agent"
RUNTIME_DISCOVER_SCRIPT = RUNTIME_ROOT / "discover.py"
RUNTIME_CONFIG = RUNTIME_ROOT / "config"
RUNTIME_PRIVATE = RUNTIME_ROOT / "private"
RUNTIME_CANDIDATE_INTAKE = RUNTIME_PRIVATE / "candidate_intake.json"
SOURCE_CANDIDATE_INTAKE = PROJECT_ROOT / "private" / "candidate_intake.json"


def _write_runtime_bootstrap() -> None:
    RUNTIME_BOOTSTRAP.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_BOOTSTRAP.write_text(
        textwrap.dedent(
            """\
            #!/bin/zsh
            set -eu
            set -o pipefail
            umask 077

            if [[ $# -ne 2 ]]; then
              echo "Usage: $0 <repo_root> <output_dir>" >&2
              exit 2
            fi

            OUTPUT_DIR=$2
            CURRENT_RECOMMENDATION_QUEUE="${OUTPUT_DIR}/Current_Profile_Recommended_Queue.csv"
            RUNTIME_ROOT="${HOME}/Library/Caches/smart_jobapply_agent_runtime"
            RUNTIME_SRC="${RUNTIME_ROOT}/src/jobapply_agent"
            RUNTIME_CONFIG="${RUNTIME_ROOT}/config"
            RUNTIME_SCRIPT="${RUNTIME_ROOT}/discover.py"
            RUNTIME_CANDIDATE_INTAKE="${RUNTIME_ROOT}/private/candidate_intake.json"

            mkdir -p "${RUNTIME_ROOT}" "${RUNTIME_CONFIG}" "${RUNTIME_ROOT}/src" "$(dirname "${OUTPUT_DIR}")"
            mkdir -p "${OUTPUT_DIR}"
            # JobDiscoveryScheduler owns bounded, crash-recoverable locks for
            # all state/export/audit artifacts.  An outer mkdir lock can become
            # stale and suppress future scheduled discovery runs.

            PYTHON_BIN="/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
            if [[ ! -x "${PYTHON_BIN}" ]]; then
              PYTHON_BIN="$(command -v python3)"
            fi

            PAYLOAD_ARGS=()
            if [[ -n "${JOBAPPLY_AGENT_VISIBLE_PAYLOADS:-}" ]]; then
              PAYLOAD_ARGS+=(--visible-payloads "${JOBAPPLY_AGENT_VISIBLE_PAYLOADS}")
            fi
            "${PYTHON_BIN}" "${RUNTIME_SCRIPT}" --candidate-intake "${RUNTIME_CANDIDATE_INTAKE}" --output-dir "${OUTPUT_DIR}" "${PAYLOAD_ARGS[@]}"
            "${PYTHON_BIN}" "${RUNTIME_SCRIPT}" --candidate-intake "${RUNTIME_CANDIDATE_INTAKE}" --output-dir "${OUTPUT_DIR}" --export-current-recommendations "${CURRENT_RECOMMENDATION_QUEUE}"
            """
        ).rstrip()
        + "\n",
        encoding="utf-8",
    )
    RUNTIME_BOOTSTRAP.chmod(0o700)


def _discover_module_for_runtime() -> Any:
    """Load the local discovery validation/projection boundary."""

    discover_path = PROJECT_ROOT / "scripts" / "discover.py"
    spec = importlib.util.spec_from_file_location("jobapply_agent_discover_runtime_projection", discover_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load discovery runtime helpers from {discover_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prune_empty_matching_facts(value: object) -> object | None:
    """Remove absent matcher fields without substituting candidate facts."""

    if isinstance(value, Mapping):
        projected = {
            key: nested
            for key, raw_nested in value.items()
            if isinstance(key, str)
            and (nested := _prune_empty_matching_facts(raw_nested)) is not None
        }
        return projected or None
    if isinstance(value, list):
        return list(value) or None
    return value


def _project_active_intake_for_runtime() -> dict[str, object]:
    """Create a verified, matcher-only active-intake cache projection."""

    if not SOURCE_CANDIDATE_INTAKE.exists():
        raise ValueError(f"Active candidate intake is missing: {SOURCE_CANDIDATE_INTAKE}")
    raw_intake = json.loads(SOURCE_CANDIDATE_INTAKE.read_text(encoding="utf-8"))
    if not isinstance(raw_intake, Mapping):
        raise ValueError("Active candidate intake JSON must be an object")

    discover = _discover_module_for_runtime()
    from jobapply_agent.intake import _activation_revision

    active_intake = discover.validate_active_candidate_profile(raw_intake)
    matching_facts = _prune_empty_matching_facts(
        discover._active_intake_profile_mapping(active_intake)
    )
    if not isinstance(matching_facts, Mapping):
        raise ValueError("Active candidate intake has no matcher-approved facts")
    projection_draft = discover.validate_candidate_intake(
        {
            "schema_version": 1,
            "documents": [],
            "approved_facts": dict(matching_facts),
            "unknown_fields": [],
            "contradictions": [],
            "pending_facts": [],
        }
    )
    projection = {
        **projection_draft,
        "state": "active",
        "activated_by": active_intake["activated_by"],
        "confirmed_at": active_intake["confirmed_at"],
        "revision_hash": _activation_revision(projection_draft),
    }
    return discover.validate_active_candidate_profile(projection)


def _write_runtime_candidate_intake() -> None:
    """Write the validated matcher-only active intake needed by discovery.

    The cached launchd runtime must not contain contacts, compensation,
    source status, documents, or autofill values.
    """

    projection = _project_active_intake_for_runtime()
    RUNTIME_PRIVATE.mkdir(parents=True, exist_ok=True)
    RUNTIME_PRIVATE.chmod(0o700)
    RUNTIME_CANDIDATE_INTAKE.write_text(
        json.dumps(projection, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    RUNTIME_CANDIDATE_INTAKE.chmod(0o600)


def _sync_runtime_snapshot() -> None:
    """Copy runtime files while user command has desktop permissions."""

    RUNTIME_SRC.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_CONFIG.mkdir(parents=True, exist_ok=True)
    if RUNTIME_SRC.exists():
        shutil.rmtree(RUNTIME_SRC, ignore_errors=True)
    if (RUNTIME_ROOT / "config").exists():
        shutil.rmtree(RUNTIME_ROOT / "config", ignore_errors=True)
    if RUNTIME_PRIVATE.exists():
        shutil.rmtree(RUNTIME_PRIVATE, ignore_errors=True)
    RUNTIME_CONFIG.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PROJECT_ROOT / "src" / "jobapply_agent", RUNTIME_SRC, dirs_exist_ok=True)
    shutil.copy2(PROJECT_ROOT / "scripts" / "discover.py", RUNTIME_DISCOVER_SCRIPT)
    shutil.copy2(PROJECT_ROOT / "config" / "search_profiles.yaml", RUNTIME_CONFIG / "search_profiles.yaml")
    shutil.copy2(PROJECT_ROOT / "config" / "scoring_rules.yaml", RUNTIME_CONFIG / "scoring_rules.yaml")
    _write_runtime_candidate_intake()
    RUNTIME_DISCOVER_SCRIPT.chmod(0o700)


def _render_plist() -> str:
    """Render a portable launchd plist without publishing local machine paths."""

    template = SOURCE_PLIST.read_text(encoding="utf-8")
    substitutions = {
        "__RUNTIME_BOOTSTRAP__": str(RUNTIME_BOOTSTRAP),
        "__PROJECT_ROOT__": str(PROJECT_ROOT),
        "__OUTPUT_DIR__": str(RUNTIME_ROOT / "data"),
        "__STDOUT_PATH__": str(RUNTIME_ROOT / "launchd.stdout.log"),
        "__STDERR_PATH__": str(RUNTIME_ROOT / "launchd.stderr.log"),
    }
    for placeholder, value in substitutions.items():
        template = template.replace(placeholder, xml_escape(value))
    if "__" in template:
        raise ValueError("launchd plist contains an unresolved placeholder")
    return template


def main() -> int:
    parser = argparse.ArgumentParser(description="Install the opt-in 08:00/17:00 local discovery launch agent.")
    parser.add_argument("--install", action="store_true", help="Copy the reviewed plist into ~/Library/LaunchAgents and load it.")
    parser.add_argument("--uninstall", action="store_true", help="Unload and remove this agent only.")
    args = parser.parse_args()
    if args.install and args.uninstall:
        parser.error("Choose exactly one of --install or --uninstall")
    target = Path.home() / "Library" / "LaunchAgents" / PLIST_NAME
    domain = f"gui/{os.getuid()}"
    if not args.install and not args.uninstall:
        print(f"Reviewed plist: {SOURCE_PLIST}")
        print("Run with --install to opt in. It schedules discovery/export only at 08:00 and 17:00 local time.")
        return 0
    if args.uninstall:
        subprocess.run(["launchctl", "bootout", domain, str(target)], check=False)
        if target.exists():
            target.unlink()
        if RUNTIME_BOOTSTRAP.exists():
            RUNTIME_BOOTSTRAP.unlink()
        if RUNTIME_ROOT.exists():
            shutil.rmtree(RUNTIME_ROOT, ignore_errors=True)
        print(f"Removed {target}")
        return 0
    if not SOURCE_PLIST.exists():
        print(f"Missing source plist: {SOURCE_PLIST}", file=sys.stderr)
        return 2
    # Keep source/runtime synchronization serialized with an in-flight launchd run.
    install_lock = RUNTIME_ROOT.parent / f".{RUNTIME_ROOT.name}.install-lock"
    try:
        install_lock.mkdir()
    except FileExistsError:
        print(f"Another runtime installation is in progress: {install_lock}", file=sys.stderr)
        return 3
    try:
        _sync_runtime_snapshot()
        _write_runtime_bootstrap()
    finally:
        install_lock.rmdir()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render_plist(), encoding="utf-8")
    subprocess.run(["launchctl", "bootout", domain, str(target)], check=False)
    subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    print(f"Installed {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
