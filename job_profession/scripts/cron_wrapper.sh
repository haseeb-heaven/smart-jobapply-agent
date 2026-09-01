#!/bin/zsh
# Example crontab: 0 8,17 * * * /absolute/path/to/job_profession/scripts/cron_wrapper.sh
# The wrapper only invokes local discovery/export: no browser, fetching, login,
# CAPTCHA workaround, application click, form answer, or submission is possible.
set -eu
set -o pipefail
umask 077
SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${SCRIPT_DIR:h}"
OUTPUT_DIR="${JOB_PROFESSION_OUTPUT_DIR:-${PROJECT_ROOT}/data}"
CURRENT_RECOMMENDATION_QUEUE="${OUTPUT_DIR}/Current_Profile_Recommended_Queue.csv"
mkdir -p "${OUTPUT_DIR}"
# JobDiscoveryScheduler owns bounded, crash-recoverable cross-process locking
# for its state, export, and audit artifacts.  Do not add a mkdir lock here:
# an abandoned directory would suppress every future scheduled run.
PAYLOAD_ARGS=()
if [[ -n "${JOB_PROFESSION_VISIBLE_PAYLOADS:-}" ]]; then
  PAYLOAD_ARGS=(--visible-payloads "${JOB_PROFESSION_VISIBLE_PAYLOADS}")
fi
if [[ -z "${JOB_PROFESSION_CANDIDATE_INTAKE:-}" ]]; then
  print -u2 "Discovery did not run: JOB_PROFESSION_CANDIDATE_INTAKE is required"
  exit 2
fi
CANDIDATE_ARGS=(--candidate-intake "${JOB_PROFESSION_CANDIDATE_INTAKE}")
PYTHON_BIN="${JOB_PROFESSION_PYTHON:-/Library/Frameworks/Python.framework/Versions/3.12/bin/python3}"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi
"${PYTHON_BIN}" "${SCRIPT_DIR}/discover.py" --output-dir "${OUTPUT_DIR}" "${CANDIDATE_ARGS[@]}" "${PAYLOAD_ARGS[@]}"
"${PYTHON_BIN}" "${SCRIPT_DIR}/discover.py" --output-dir "${OUTPUT_DIR}" "${CANDIDATE_ARGS[@]}" --export-current-recommendations "${CURRENT_RECOMMENDATION_QUEUE}"
