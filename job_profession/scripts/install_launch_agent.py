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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLIST_NAME = "com.haseeb.job-profession.plist"
SOURCE_PLIST = PROJECT_ROOT / "launchd" / PLIST_NAME
RUNTIME_ROOT = Path.home() / "Library" / "Caches" / "job_profession_runtime"
RUNTIME_BOOTSTRAP = RUNTIME_ROOT / "launchd_discover_bootstrap.sh"
RUNTIME_SRC = RUNTIME_ROOT / "src" / "job_profession"
RUNTIME_DISCOVER_SCRIPT = RUNTIME_ROOT / "discover.py"
RUNTIME_CONFIG = RUNTIME_ROOT / "config"
RUNTIME_PRIVATE = RUNTIME_ROOT / "private"
SOURCE_CANDIDATE_PROFILE = PROJECT_ROOT / "private" / "candidate_profile.yaml"


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
            RUNTIME_ROOT="${HOME}/Library/Caches/job_profession_runtime"
            RUNTIME_SRC="${RUNTIME_ROOT}/src/job_profession"
            RUNTIME_CONFIG="${RUNTIME_ROOT}/config"
            RUNTIME_SCRIPT="${RUNTIME_ROOT}/discover.py"

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
            if [[ -n "${JOB_PROFESSION_VISIBLE_PAYLOADS:-}" ]]; then
              PAYLOAD_ARGS+=(--visible-payloads "${JOB_PROFESSION_VISIBLE_PAYLOADS}")
            fi
            "${PYTHON_BIN}" "${RUNTIME_SCRIPT}" --output-dir "${OUTPUT_DIR}" "${PAYLOAD_ARGS[@]}"
            "${PYTHON_BIN}" "${RUNTIME_SCRIPT}" --output-dir "${OUTPUT_DIR}" --export-current-recommendations "${CURRENT_RECOMMENDATION_QUEUE}"
            """
        ).rstrip()
        + "\n",
        encoding="utf-8",
    )
    RUNTIME_BOOTSTRAP.chmod(0o700)


def _approved_profile_mapping_for_runtime() -> dict[str, object]:
    """Load only the evidence projection exposed by the discovery script."""

    discover_path = PROJECT_ROOT / "scripts" / "discover.py"
    spec = importlib.util.spec_from_file_location("job_profession_discover_profile_projection", discover_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load profile projection helper from {discover_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._approved_profile_mapping(SOURCE_CANDIDATE_PROFILE)


def _write_runtime_candidate_profile() -> None:
    """Write a runtime profile containing only evidence, roles, and exclusions.

    The cached launchd runtime must not contain contacts, compensation,
    confirmation-gated preferences, source status, or autofill values.
    """

    mapping = _approved_profile_mapping_for_runtime()
    roles = mapping["roles"]
    hard_exclusions = mapping["hard_exclusions"]
    skills = mapping["skills"]
    if not isinstance(roles, dict) or not isinstance(hard_exclusions, dict) or not isinstance(skills, dict):
        raise ValueError("Approved candidate profile projection has an invalid shape")
    RUNTIME_PRIVATE.mkdir(parents=True, exist_ok=True)
    RUNTIME_PRIVATE.chmod(0o700)
    runtime_profile = RUNTIME_PRIVATE / "candidate_profile.yaml"
    runtime_profile.write_text(
        "\n".join(
            (
                "roles:",
                f"  include: {json.dumps(roles.get('include', []))}",
                f"  exclude_title_terms: {json.dumps(roles.get('exclude_title_terms', []))}",
                "hard_exclusions:",
                f"  mandatory_requirements: {json.dumps(hard_exclusions.get('mandatory_requirements', []))}",
                "skills:",
                f"  professional: {json.dumps(skills.get('professional', []))}",
                f"  personal_open_source: {json.dumps(skills.get('personal_open_source', []))}",
                f"  learning_or_exposure: {json.dumps(skills.get('learning_or_exposure', []))}",
                "",
            )
        ),
        encoding="utf-8",
    )
    runtime_profile.chmod(0o600)


def _sync_runtime_snapshot() -> None:
    """Copy runtime files while user command has desktop permissions."""

    RUNTIME_SRC.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_CONFIG.mkdir(parents=True, exist_ok=True)
    if RUNTIME_SRC.exists():
        shutil.rmtree(RUNTIME_SRC, ignore_errors=True)
    if (RUNTIME_ROOT / "config").exists():
        shutil.rmtree(RUNTIME_ROOT / "config", ignore_errors=True)
    RUNTIME_CONFIG.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PROJECT_ROOT / "src" / "job_profession", RUNTIME_SRC, dirs_exist_ok=True)
    shutil.copy2(PROJECT_ROOT / "scripts" / "discover.py", RUNTIME_DISCOVER_SCRIPT)
    shutil.copy2(PROJECT_ROOT / "config" / "search_profiles.yaml", RUNTIME_CONFIG / "search_profiles.yaml")
    shutil.copy2(PROJECT_ROOT / "config" / "scoring_rules.yaml", RUNTIME_CONFIG / "scoring_rules.yaml")
    _write_runtime_candidate_profile()
    RUNTIME_DISCOVER_SCRIPT.chmod(0o700)


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
    shutil.copy2(SOURCE_PLIST, target)
    subprocess.run(["launchctl", "bootout", domain, str(target)], check=False)
    subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)
    print(f"Installed {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
