#!/usr/bin/env bash
# Example crontab: 0 8,17 * * * /absolute/path/to/jobapply_agent/scripts/cron_wrapper.sh
# The wrapper only invokes local discovery/export: no browser, fetching, login,
# CAPTCHA workaround, application click, form answer, or submission is possible.
set -eu
set -o pipefail
umask 077
SOURCE="${BASH_SOURCE[0]}"
while [[ -h "${SOURCE}" ]]; do
  LINK_DIR="$(CDPATH="" cd -P -- "$(dirname -- "${SOURCE}")" && pwd -P)"
  SOURCE="$(readlink "${SOURCE}")"
  if [[ "${SOURCE}" != /* ]]; then
    SOURCE="${LINK_DIR}/${SOURCE}"
  fi
done
SCRIPT_DIR="$(CDPATH="" cd -P -- "$(dirname -- "${SOURCE}")" && pwd -P)"
PROJECT_ROOT="$(dirname -- "${SCRIPT_DIR}")"
OUTPUT_DIR="${JOBAPPLY_AGENT_OUTPUT_DIR:-${PROJECT_ROOT}/data}"
CURRENT_RECOMMENDATION_QUEUE="${OUTPUT_DIR}/Current_Profile_Recommended_Queue.csv"
mkdir -p "${OUTPUT_DIR}"
# JobDiscoveryScheduler owns bounded, crash-recoverable cross-process locking
# for its state, export, and audit artifacts.  Do not add a mkdir lock here:
# an abandoned directory would suppress every future scheduled run.
DISCOVERY_ARGS=(--output-dir "${OUTPUT_DIR}")
if [[ -n "${JOBAPPLY_AGENT_VISIBLE_PAYLOADS:-}" ]]; then
  DISCOVERY_ARGS+=(--visible-payloads "${JOBAPPLY_AGENT_VISIBLE_PAYLOADS}")
fi
if [[ -z "${JOBAPPLY_AGENT_CANDIDATE_INTAKE:-}" ]]; then
  printf '%s\n' "Discovery did not run: JOBAPPLY_AGENT_CANDIDATE_INTAKE is required" >&2
  exit 2
fi
CANDIDATE_ARGS=(--candidate-intake "${JOBAPPLY_AGENT_CANDIDATE_INTAKE}")
PYTHON_BIN="${JOBAPPLY_AGENT_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.12/bin/python3}"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [[ -z "${PYTHON_BIN}" || ! -x "${PYTHON_BIN}" ]]; then
  printf '%s\n' "Discovery did not run: no executable Python interpreter found; set JOBAPPLY_AGENT_PYTHON to Python >= 3.11" >&2
  exit 127
fi
check_python_version() {
  local python_version=""
  if python_version="$("${PYTHON_BIN}" -c 'import sys; print("%d.%d" % sys.version_info[:2]); raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null)"; then
    return 0
  fi
  if [[ -n "${python_version}" ]]; then
    printf '%s\n' "Discovery did not run: Python >= 3.11 is required; found Python ${python_version} at ${PYTHON_BIN}" >&2
    return 1
  fi
  printf '%s\n' "Discovery did not run: Python interpreter could not be executed: ${PYTHON_BIN}" >&2
  return 127
}
if check_python_version; then
  :
else
  exit $?
fi
"${PYTHON_BIN}" "${SCRIPT_DIR}/discover.py" "${DISCOVERY_ARGS[@]}" "${CANDIDATE_ARGS[@]}"
"${PYTHON_BIN}" "${SCRIPT_DIR}/discover.py" --output-dir "${OUTPUT_DIR}" "${CANDIDATE_ARGS[@]}" --export-current-recommendations "${CURRENT_RECOMMENDATION_QUEUE}"
