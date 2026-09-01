#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if ! command -v python >/dev/null 2>&1; then
  echo "validation error: python is required" >&2
  exit 127
fi

if ! command -v ruff >/dev/null 2>&1; then
  echo "validation error: ruff is required; install the dev dependency set" >&2
  exit 127
fi

echo "[1/8] full Python suite"
python -m pytest

echo "[2/8] Python lint"
# Keep the repository gate independent from a runner's user-level Ruff config
# and from newly introduced rule families.  The project can expand this
# baseline deliberately in a separate reviewed change.
ruff check --isolated --select E4,E7,E9,F --line-length 120 --target-version py311 \
  job_profession/src job_profession/scripts tests

echo "[3/8] bounded watcher line and branch coverage"
python -m coverage erase
python -m coverage run --branch \
  --include='*/skills/easy-apply-tab-monitor/scripts/chrome_tab_watcher.py' \
  -m pytest tests/test_chrome_tab_watcher.py
python -m coverage report \
  --include='*/skills/easy-apply-tab-monitor/scripts/chrome_tab_watcher.py' \
  --fail-under=100

echo "[4/8] browser adapter line and branch coverage"
python -m coverage erase
python -m coverage run --branch \
  --include='*/skills/easy-apply-tab-monitor/scripts/browser_tab_adapter.py' \
  -m pytest tests/test_browser_tab_adapter.py
python -m coverage report \
  --include='*/skills/easy-apply-tab-monitor/scripts/browser_tab_adapter.py' \
  --fail-under=100

echo "[5/8] Python bytecode compilation"
python -m compileall -q \
  job_profession/src \
  job_profession/scripts \
  skills/easy-apply-tab-monitor/scripts

echo "[6/8] shell static analysis"
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck scripts/verify.sh job_profession/scripts/cron_wrapper.sh
else
  echo "validation note: shellcheck unavailable; running Bash syntax fallback"
  bash -n scripts/verify.sh job_profession/scripts/cron_wrapper.sh
fi

echo "[7/8] patch whitespace integrity"
git diff --check
git diff --cached --check

echo "[8/8] tracked candidate-data boundary"
tracked_private="$(git ls-files 'job_profession/private/**' 'resumes/**' 'codex-apply-*.*')"
if [[ -n "$tracked_private" ]]; then
  echo "validation error: private candidate paths are tracked" >&2
  echo "$tracked_private" >&2
  exit 1
fi

echo "agent validation gate: PASS"
