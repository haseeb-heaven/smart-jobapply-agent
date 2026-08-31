#!/usr/bin/env bash
# lib/loop.sh — review → fix → validate → repeat
set -euo pipefail

LOOP_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$LOOP_LIB_DIR/colors.sh"
source "$LOOP_LIB_DIR/gh.sh"
source "$LOOP_LIB_DIR/reviews.sh"
source "$LOOP_LIB_DIR/agent.sh"
source "$LOOP_LIB_DIR/validate.sh"
source "$LOOP_LIB_DIR/report.sh"

PIPELINE_RESULT="failed"
CI_BLOCKED=false
VALIDATION_BLOCKED=false
ITERATION=0

trigger_reviews() {
  require_pr_open "$OWNER" "$REPO" "$PR_NUM"
  REVIEW_BASELINE_FILE="$DATA_DIR/review-baseline.txt"
  capture_review_baseline "$OWNER" "$REPO" "$PR_NUM" "$REVIEW_BOTS" "$REVIEW_BASELINE_FILE"
  local bot
  for bot in $REVIEW_BOTS; do
    case "$bot" in
      coderabbit) trigger_coderabbit "$OWNER" "$REPO" "$PR_NUM" ;;
      greptile) trigger_greptile "$OWNER" "$REPO" "$PR_NUM" ;;
      *) echo "ERROR: unsupported review bot: $bot" >&2; return 1 ;;
    esac
  done
  sleep "$PRE_REVIEW_WAIT"
}

wait_for_reviews() {
  local start=$SECONDS bot all_done
  while (( SECONDS - start < REVIEW_TIMEOUT )); do
    all_done=true
    for bot in $REVIEW_BOTS; do
      is_review_bot_done "$bot" "$OWNER" "$REPO" "$PR_NUM" "$REVIEW_BASELINE_FILE" || all_done=false
    done
    [[ "$all_done" == true ]] && return 0
    sleep "$POLL_INTERVAL"
  done
  echo "ERROR: review wait timed out" >&2
  return 1
}

pr_head_matches_worktree() {
  local remote_head
  remote_head=$(gh api "/repos/$OWNER/$REPO/pulls/$PR_NUM" --jq '.head.sha') || return 1
  [[ "$remote_head" == "$(get_current_sha)" ]]
}

ci_findings() {
  local previous="$DATA_DIR/iterations/$((ITERATION - 1))/ci-failures.md"
  [[ -s "$previous" ]] || { echo '[]'; return 0; }
  jq -n --arg body "$(cat "$previous")" '[{id:"ci-failure",path:"unknown",line:null,severity:"high",body:$body,source:"github-ci"}]'
}

validation_findings() {
  local previous="$DATA_DIR/iterations/$((ITERATION - 1))" body=""
  for failure in test-failures.txt lint-failures.txt; do
    if [[ -s "$previous/$failure" ]]; then
      body+="## $failure\n$(cat "$previous/$failure")\n"
    fi
  done
  if [[ -n "$body" ]]; then
    jq -n --arg body "$body" '[{id:"local-validation",path:"unknown",line:null,severity:"high",body:$body,source:"local-validation"}]'
  else
    echo '[]'
  fi
}

preflight_agent() {
  command -v "$FIX_AGENT" >/dev/null 2>&1 || {
    echo "ERROR: fix agent '$FIX_AGENT' is not installed; refusing to trigger review bots" >&2
    return 1
  }
  case "${AGENT_SANDBOX:-auto}" in
    macos) sandbox_exec_works || { echo "ERROR: macOS agent sandbox unavailable" >&2; return 1; } ;;
    bwrap|docker) echo "ERROR: $AGENT_SANDBOX cannot provide provider-restricted agent networking" >&2; return 1 ;;
    auto) sandbox_exec_works || {
      echo "ERROR: no provider-aware disposable agent sandbox backend is available" >&2; return 1;
    } ;;
    *) echo "ERROR: unsupported agent sandbox mode: $AGENT_SANDBOX" >&2; return 1 ;;
  esac
}

run_pipeline() {
  mkdir -p "$DATA_DIR/iterations" "$RUN_ROOT/agent-home" "$RUN_ROOT/agent-tmp" \
    "$DATA_DIR/sandbox-home" "$DATA_DIR/sandbox-tmp"
  if [[ "$DRY_RUN" == true ]]; then
    PIPELINE_RESULT="dry_run"
    write_final_report "$DATA_DIR" 0 "$PIPELINE_RESULT"
    return 0
  fi

  require_pr_open "$OWNER" "$REPO" "$PR_NUM"
  preflight_agent || { PIPELINE_RESULT="agent_preflight_failed"; write_final_report "$DATA_DIR" 0 "$PIPELINE_RESULT"; return 1; }
  trigger_reviews || { PIPELINE_RESULT="review_blocked"; write_final_report "$DATA_DIR" 0 "$PIPELINE_RESULT"; return 1; }
  wait_for_reviews || { PIPELINE_RESULT="review_blocked"; write_final_report "$DATA_DIR" 0 "$PIPELINE_RESULT"; return 1; }

  for (( ITERATION=1; ITERATION<=MAX_ITERATIONS; ITERATION++ )); do
    local iter_dir="$DATA_DIR/iterations/$ITERATION"
    rm -rf "$iter_dir"
    mkdir -p "$iter_dir"
    local findings findings_file
    findings=$(collect_findings "$OWNER" "$REPO" "$PR_NUM" "$iter_dir") || {
      PIPELINE_RESULT="review_blocked"; write_final_report "$DATA_DIR" "$ITERATION" "$PIPELINE_RESULT"; return 1;
    }
    if [[ "$(jq 'length' <<<"$findings")" -eq 0 ]]; then
      if [[ "$CI_BLOCKED" == true ]]; then
        findings=$(jq -s '.[0] + .[1]' <(printf '%s\n' "$findings") <(ci_findings))
      fi
      if [[ "$VALIDATION_BLOCKED" == true ]]; then
        findings=$(jq -s '.[0] + .[1]' <(printf '%s\n' "$findings") <(validation_findings))
      fi
    fi
    findings_file="$iter_dir/findings.json"
    jq '.' <<<"$findings" > "$findings_file"
    local actionable
    actionable=$(count_actionable "$findings")
    if [[ "$actionable" -eq 0 ]]; then
      if [[ "$CI_BLOCKED" != true ]] && ! pr_head_matches_worktree; then
        echo "ERROR: PR head changed after validation; refusing to report a stale clean result" >&2
        PIPELINE_RESULT="head_changed"
        write_iteration_report "$DATA_DIR" "$ITERATION" "$findings_file"
        write_final_report "$DATA_DIR" "$ITERATION" "$PIPELINE_RESULT"
        return 1
      fi
      if [[ "$WAIT_CI" == true && "$CI_BLOCKED" != true ]]; then
        if wait_for_ci "$OWNER" "$REPO" "$(get_current_sha)" "$CI_TIMEOUT" "$DATA_DIR" "$ITERATION"; then
          CI_BLOCKED=false
        else
          CI_BLOCKED=true
        fi
      fi
      if [[ "$CI_BLOCKED" == true ]]; then
        PIPELINE_RESULT="ci_blocked"
      else
        PIPELINE_RESULT="clean"
      fi
      write_iteration_report "$DATA_DIR" "$ITERATION" "$findings_file"
      write_final_report "$DATA_DIR" "$ITERATION" "$PIPELINE_RESULT"
      [[ "$PIPELINE_RESULT" == clean ]] && return 0 || return 1
    fi

    local brief_file agent_base_sha allowed_file
    brief_file=$(generate_fix_brief "$DATA_DIR" "$ITERATION" "$findings_file")
    agent_base_sha=$(get_current_sha)
    allowed_file="$iter_dir/allowed-paths.txt"
    if ! spawn_fix_agent "$WORKTREE_DIR" "$brief_file" "$FIX_AGENT" "$RUN_ROOT/agent-home" "$RUN_ROOT/agent-tmp"; then
      echo "ERROR: fix agent failed" >&2
      PIPELINE_RESULT="agent_failed"
      write_iteration_report "$DATA_DIR" "$ITERATION" "$findings_file"
      write_final_report "$DATA_DIR" "$ITERATION" "$PIPELINE_RESULT"
      return 1
    fi
    if ! check_agent_changes "$WORKTREE_DIR" "$agent_base_sha" "$findings_file" "$allowed_file"; then
      echo "ERROR: agent changes violated the review scope or were absent" >&2
      PIPELINE_RESULT="scope_blocked"
      write_iteration_report "$DATA_DIR" "$ITERATION" "$findings_file"
      write_final_report "$DATA_DIR" "$ITERATION" "$PIPELINE_RESULT"
      return 1
    fi
    git -C "$WORKTREE_DIR" diff --stat "$agent_base_sha" -- > "$iter_dir/git-stat.txt"

    local validation_failed=false
    run_tests "$WORKTREE_DIR" "$TEST_CMD" "$DATA_DIR" "$ITERATION" || validation_failed=true
    run_lint "$WORKTREE_DIR" "$LINT_CMD" "$DATA_DIR" "$ITERATION" || validation_failed=true
    if [[ "$validation_failed" == true ]]; then
      write_iteration_report "$DATA_DIR" "$ITERATION" "$findings_file"
      append_results "$DATA_DIR" "$ITERATION"
      VALIDATION_BLOCKED=true
      continue
    fi

    if [[ "$(get_current_sha)" == "$agent_base_sha" ]]; then
      commit_fixes "$WORKTREE_DIR" "$ITERATION" "$allowed_file" || {
        PIPELINE_RESULT="commit_failed"; write_final_report "$DATA_DIR" "$ITERATION" "$PIPELINE_RESULT"; return 1;
      }
    else
      # Agents are not allowed to commit. If one did, validate every committed path.
      if ! validate_scope "$WORKTREE_DIR" "$agent_base_sha" "$findings_file" "$allowed_file"; then
        PIPELINE_RESULT="scope_blocked"; write_final_report "$DATA_DIR" "$ITERATION" "$PIPELINE_RESULT"; return 1
      fi
      if ! git -C "$WORKTREE_DIR" diff --cached --quiet; then
        echo "ERROR: agent-created commit left staged changes" >&2
        PIPELINE_RESULT="scope_blocked"; write_final_report "$DATA_DIR" "$ITERATION" "$PIPELINE_RESULT"; return 1
      fi
    fi

    if ! validate_scope "$WORKTREE_DIR" "$agent_base_sha" "$findings_file" "$allowed_file"; then
      PIPELINE_RESULT="scope_blocked"; write_final_report "$DATA_DIR" "$ITERATION" "$PIPELINE_RESULT"; return 1
    fi

    require_pr_open "$OWNER" "$REPO" "$PR_NUM"
    push_changes "$PUSH_REMOTE" "$WORKTREE_DIR" "$BRANCH" "$FORCE_PUSH" || {
      PIPELINE_RESULT="push_failed"; write_final_report "$DATA_DIR" "$ITERATION" "$PIPELINE_RESULT"; return 1;
    }
    WORKTREE_LANDED=true
    if [[ "$WAIT_CI" == true ]]; then
      if wait_for_ci "$OWNER" "$REPO" "$(get_current_sha)" "$CI_TIMEOUT" "$DATA_DIR" "$ITERATION"; then
        CI_BLOCKED=false
      else
        CI_BLOCKED=true
      fi
    fi
    VALIDATION_BLOCKED=false
    write_iteration_report "$DATA_DIR" "$ITERATION" "$findings_file"
    append_results "$DATA_DIR" "$ITERATION"
    trigger_reviews || { PIPELINE_RESULT="review_blocked"; write_final_report "$DATA_DIR" "$ITERATION" "$PIPELINE_RESULT"; return 1; }
    wait_for_reviews || { PIPELINE_RESULT="review_blocked"; write_final_report "$DATA_DIR" "$ITERATION" "$PIPELINE_RESULT"; return 1; }
  done

  # The final review is collected after the last fix pass. A clean response
  # here is a valid success, not an iteration-limit failure.
  local final_dir="$DATA_DIR/iterations/$MAX_ITERATIONS" final_findings final_findings_file
  mkdir -p "$final_dir"
  final_findings=$(collect_findings "$OWNER" "$REPO" "$PR_NUM" "$final_dir") || {
    PIPELINE_RESULT="review_blocked"
    write_final_report "$DATA_DIR" "$MAX_ITERATIONS" "$PIPELINE_RESULT"
    return 1
  }
  final_findings_file="$final_dir/final-findings.json"
  jq '.' <<<"$final_findings" > "$final_findings_file"
  if [[ "$CI_BLOCKED" != true ]] && [[ "$(count_actionable "$final_findings")" -eq 0 ]] && pr_head_matches_worktree; then
    write_iteration_report "$DATA_DIR" "$MAX_ITERATIONS" "$final_findings_file"
    PIPELINE_RESULT="clean"
    write_final_report "$DATA_DIR" "$MAX_ITERATIONS" "$PIPELINE_RESULT"
    return 0
  fi

  PIPELINE_RESULT="$([[ "$CI_BLOCKED" == true ]] && echo ci_blocked || echo max_iterations)"
  write_final_report "$DATA_DIR" "$MAX_ITERATIONS" "$PIPELINE_RESULT"
  return 1
}

run_pipeline
