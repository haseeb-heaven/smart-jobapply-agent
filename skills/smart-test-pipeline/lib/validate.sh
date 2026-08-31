#!/usr/bin/env bash
# lib/validate.sh — credential-free disposable validation and CI checks
set -euo pipefail

VALIDATE_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$VALIDATE_LIB_DIR/colors.sh"
source "$VALIDATE_LIB_DIR/sandbox.sh"

run_tests() {
  local worktree_dir="$1" test_cmd="$2" data_dir="$3" iteration="$4"
  local output_file="$data_dir/iterations/$iteration/test-output.txt"
  local failure_file="$data_dir/iterations/$iteration/test-failures.txt"
  echo -e "${CYAN}  ${PLAY} Running tests in a credential-free disposable sandbox${NC}"
  local rc=0
  local saved_agent_env="${AGENT_ENV_ALLOWLIST-}"
  local snapshot_dir="$data_dir/iterations/$iteration/validation-worktree-tests"
  local sandbox_home="$data_dir/iterations/$iteration/sandbox-home-tests"
  local sandbox_tmp="$data_dir/iterations/$iteration/sandbox-tmp-tests"
  mkdir -p "$sandbox_home" "$sandbox_tmp"
  if ! prepare_validation_snapshot "$worktree_dir" "$snapshot_dir" 2>"$output_file"; then
    printf 'Tests could not prepare a disposable snapshot.\n' >> "$output_file"
    cp "$output_file" "$failure_file"
    return 1
  fi
  AGENT_ENV_ALLOWLIST=""
  export AGENT_ENV_ALLOWLIST
  run_validation_command "${VALIDATION_TIMEOUT:-3600}" \
    run_sandboxed "${VALIDATION_SANDBOX:-auto}" "$snapshot_dir" "$sandbox_home" "$sandbox_tmp" \
    false bash -c "$test_cmd" >"$output_file" 2>&1 || rc=$?
  AGENT_ENV_ALLOWLIST="$saved_agent_env"
  export AGENT_ENV_ALLOWLIST
  if [[ $rc -ne 0 ]]; then
    cp "$output_file" "$failure_file"
    echo -e "${RED}  ${CROSS} Tests failed (exit $rc)${NC}"
    return 1
  fi
  rm -f "$failure_file"
  echo -e "${GREEN}  ${CHECK} Tests passed${NC}"
}

run_lint() {
  local worktree_dir="$1" lint_cmd="$2" data_dir="$3" iteration="$4"
  [[ -n "$lint_cmd" ]] || { echo -e "${DIM}  ${INFO} Lint command not set — skipping${NC}"; return 0; }
  local output_file="$data_dir/iterations/$iteration/lint-output.txt"
  local failure_file="$data_dir/iterations/$iteration/lint-failures.txt"
  echo -e "${CYAN}  ${PLAY} Running lint in a credential-free disposable sandbox${NC}"
  local rc=0
  local saved_agent_env="${AGENT_ENV_ALLOWLIST-}"
  local snapshot_dir="$data_dir/iterations/$iteration/validation-worktree-lint"
  local sandbox_home="$data_dir/iterations/$iteration/sandbox-home-lint"
  local sandbox_tmp="$data_dir/iterations/$iteration/sandbox-tmp-lint"
  mkdir -p "$sandbox_home" "$sandbox_tmp"
  if ! prepare_validation_snapshot "$worktree_dir" "$snapshot_dir" 2>"$output_file"; then
    printf 'Lint could not prepare a disposable snapshot.\n' >> "$output_file"
    cp "$output_file" "$failure_file"
    return 1
  fi
  AGENT_ENV_ALLOWLIST=""
  export AGENT_ENV_ALLOWLIST
  run_validation_command "${VALIDATION_TIMEOUT:-3600}" \
    run_sandboxed "${VALIDATION_SANDBOX:-auto}" "$snapshot_dir" "$sandbox_home" "$sandbox_tmp" \
    false bash -c "$lint_cmd" >"$output_file" 2>&1 || rc=$?
  AGENT_ENV_ALLOWLIST="$saved_agent_env"
  export AGENT_ENV_ALLOWLIST
  if [[ $rc -ne 0 ]]; then
    cp "$output_file" "$failure_file"
    echo -e "${RED}  ${CROSS} Lint failed (exit $rc)${NC}"
    return 1
  fi
  rm -f "$failure_file"
  echo -e "${GREEN}  ${CHECK} Lint passed${NC}"
}

run_validation_command() {
  local seconds="$1"; shift
  local child watcher rc
  "$@" &
  child=$!
  (
    trap 'exit 0' TERM INT
    sleep "$seconds"
    kill -TERM "$child" 2>/dev/null || true
    pkill -TERM -P "$child" 2>/dev/null || true
    sleep 1
    kill -KILL "$child" 2>/dev/null || true
    pkill -KILL -P "$child" 2>/dev/null || true
  ) &
  watcher=$!
  if wait "$child"; then rc=0; else rc=$?; fi
  kill -TERM "$watcher" 2>/dev/null || true
  pkill -TERM -P "$watcher" 2>/dev/null || true
  wait "$watcher" 2>/dev/null || true
  return "$rc"
}

prepare_validation_snapshot() {
  local source_dir="$1" snapshot_dir="$2" path lower_path parent target
  rm -rf "$snapshot_dir"
  mkdir -p "$snapshot_dir"
  while IFS= read -r -d '' path; do
    [[ "$path" != /* && "$path" != .git && "$path" != .git/* ]] || {
      echo "ERROR: Git control path in validation snapshot: $path" >&2
      return 1
    }
    lower_path=$(printf '%s' "$path" | tr '[:upper:]' '[:lower:]')
    case "$lower_path" in
      .env|*.key|*.pem|*.p12|*.pfx)
        if [[ "$path" == .env.example || "$path" == .env.*.example || "$path" == docs/*.md || "$path" == docs/**/*.md ]]; then
          :
        else
          echo "ERROR: secret-like path refused in validation snapshot: $path" >&2
          return 1
        fi
        ;;
      *credentials*|*credential*)
        if [[ "$path" == docs/*.md || "$path" == docs/**/*.md ]]; then :; else
        echo "ERROR: secret-like path refused in validation snapshot: $path" >&2
        return 1
        fi
        ;;
    esac
    if [[ "$(git -C "$source_dir" ls-files --stage -- "$path" | awk 'NR == 1 { print $1 }')" == 160000 ]]; then
      [[ -d "$source_dir/$path" ]] || {
        echo "ERROR: checked-out submodule missing from validation snapshot: $path" >&2
        return 1
      }
      parent="$snapshot_dir/$path"
      mkdir -p "$parent"
      git -C "$source_dir/$path" archive --format=tar HEAD | tar -xf - -C "$parent" || {
        echo "ERROR: unable to archive submodule in validation snapshot: $path" >&2
        return 1
      }
      continue
    fi
    if [[ -L "$source_dir/$path" ]]; then
      target=$(readlink "$source_dir/$path")
      [[ "$target" != /* && "$target" != ../* && "$target" != */../* ]] || {
        echo "ERROR: unsafe symlink in validation snapshot: $path" >&2
        return 1
      }
      parent="$snapshot_dir/$(dirname "$path")"
      mkdir -p "$parent"
      ln -s "$target" "$snapshot_dir/$path"
      continue
    fi
    [[ -e "$source_dir/$path" || -L "$source_dir/$path" ]] || continue
    [[ -f "$source_dir/$path" ]] || {
      echo "ERROR: unsupported file type in validation snapshot: $path" >&2
      return 1
    }
    parent="$snapshot_dir/$(dirname "$path")"
    mkdir -p "$parent"
    cp -p "$source_dir/$path" "$snapshot_dir/$path"
  done < <(git -C "$source_dir" ls-files --cached --others --exclude-standard -z)
  git -C "$snapshot_dir" init -q
  git -C "$snapshot_dir" add -A
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null \
    GIT_AUTHOR_NAME=validation GIT_AUTHOR_EMAIL=validation@localhost \
    GIT_COMMITTER_NAME=validation GIT_COMMITTER_EMAIL=validation@localhost \
    git -c core.hooksPath=/dev/null -c commit.gpgSign=false \
      -C "$snapshot_dir" commit --no-verify -qm "credential-free validation snapshot"
}

push_changes() {
  local remote="$1" worktree_dir="$2" remote_branch="$3" force="$4"
  local expected_sha="${5:-}" current_sha
  current_sha=$(git -C "$worktree_dir" rev-parse HEAD)
  [[ -z "$expected_sha" || "$current_sha" == "$expected_sha" ]] || {
    echo "ERROR: local HEAD changed unexpectedly before push" >&2
    return 1
  }
  git -C "$worktree_dir" diff --quiet || { echo "ERROR: unstaged changes before push" >&2; return 1; }
  git -C "$worktree_dir" diff --cached --quiet || { echo "ERROR: staged changes before push" >&2; return 1; }
  local -a args=(push "$remote" "HEAD:$remote_branch")
  [[ "$force" == true ]] && args+=(--force-with-lease)
  if ! git -C "$worktree_dir" "${args[@]}"; then
    echo "ERROR: git push failed" >&2
    return 1
  fi
  echo -e "${GREEN}  ${CHECK} Pushed${NC}"
}

wait_for_ci() {
  local owner="$1" repo="$2" sha="$3" timeout="$4" data_dir="$5" iteration="$6"
  local start=$SECONDS
  local conclusions_file="$data_dir/iterations/$iteration/ci-conclusions.json"
  local failures_file="$data_dir/iterations/$iteration/ci-failures.md"
  while (( SECONDS - start < timeout )); do
    local check_runs statuses
    check_runs=$(gh api --paginate --slurp "/repos/$owner/$repo/commits/$sha/check-runs" 2>/dev/null) || {
      echo "ERROR: unable to read CI checks" >&2
      return 1
    }
    statuses=$(gh api --paginate --slurp "/repos/$owner/$repo/commits/$sha/statuses" 2>/dev/null) || {
      echo "ERROR: unable to read commit statuses" >&2
      return 1
    }
    local total pending
    total=$(jq '[.[].check_runs[]] | length' <<<"$check_runs")
    pending=$(jq '[.[].check_runs[] | select(.status != "completed")] | length' <<<"$check_runs")
    local latest_statuses status_total status_pending
    latest_statuses=$(jq '[.[][]] | sort_by(.context, .created_at) | group_by(.context) | map(last)' <<<"$statuses")
    status_total=$(jq 'length' <<<"$latest_statuses")
    status_pending=$(jq '[.[] | select(.state == "pending")] | length' <<<"$latest_statuses")
    total=$((total + status_total)); pending=$((pending + status_pending))
    if [[ "$total" -eq 0 ]]; then sleep 5; continue; fi
    if [[ "$pending" -gt 0 ]]; then sleep 30; continue; fi
    jq '[.[].check_runs[] | {name, conclusion, details_url: .html_url}]' <<<"$check_runs" > "$conclusions_file"
    jq --argjson latest "$latest_statuses" '[ $latest[] | {name: .context, conclusion: (if .state == "success" then "success" else .state end), details_url: .target_url} ]' <<<"{}" > "$conclusions_file.statuses"
    jq -s '.[0] + .[1]' "$conclusions_file" "$conclusions_file.statuses" > "$conclusions_file.tmp"
    mv "$conclusions_file.tmp" "$conclusions_file"
    rm -f "$conclusions_file.statuses"
    local failed
    failed=$(jq '[.[] | select(.conclusion != "success" and .conclusion != "neutral" and .conclusion != "skipped")] | length' "$conclusions_file")
    local successful
    successful=$(jq '[.[] | select(.conclusion == "success")] | length' "$conclusions_file")
    if [[ "$failed" -eq 0 && "$successful" -gt 0 ]]; then
      rm -f "$failures_file"
      return 0
    fi
    {
      echo "## CI Failures"
      jq -r '.[] | select(.conclusion != "success" and .conclusion != "neutral" and .conclusion != "skipped") | "- **\(.name)**: \(.conclusion) (\(.details_url))"' "$conclusions_file"
    } > "$failures_file"
    return 1
  done
  echo "ERROR: CI timeout after ${timeout}s" >&2
  printf '## CI Failures\n\n- CI wait timed out after %ss\n' "$timeout" > "$failures_file"
  return 1
}

get_current_sha() { git -C "$WORKTREE_DIR" rev-parse HEAD; }
