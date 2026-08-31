#!/usr/bin/env bash
# lib/loop.sh — Main pipeline loop: review → fix → validate → repeat
set -euo pipefail

source "$(dirname "$0")/colors.sh"
source "$(dirname "$0")/gh.sh"
source "$(dirname "$0")/reviews.sh"
source "$(dirname "$0")/agent.sh"
source "$(dirname "$0")/validate.sh"
source "$(dirname "$0")/report.sh"

PIPELINE_RESULT="clean"
CI_BLOCKED=false

# ── Trigger review bots ────────────────────────────────────────
trigger_reviews() {
  REVIEW_BASELINE_FILE="$DATA_DIR/review-baseline.txt"
  capture_review_baseline "$OWNER" "$REPO" "$PR_NUM" "$REVIEW_BOTS" "$REVIEW_BASELINE_FILE"
  echo -e "${CYAN}┌─ Triggering review bots${NC}"

  for bot in $REVIEW_BOTS; do
    echo -e "${DIM}  ${ARROW} Triggering $bot...${NC}"
    case "$bot" in
      coderabbit) trigger_coderabbit "$OWNER" "$REPO" "$PR_NUM" ;;
      greptile)   trigger_greptile "$OWNER" "$REPO" "$PR_NUM" ;;
      *)          echo -e "${YELLOW}  ${WARN} Unknown bot: $bot${NC}" ;;
    esac
  done

  echo -e "  ${WAIT} Waiting ${PRE_REVIEW_WAIT}s for bots to initialize..."
  sleep "$PRE_REVIEW_WAIT"
}

# ── Wait for reviews to appear ─────────────────────────────────
wait_for_reviews() {
  echo -e "${CYAN}┌─ Waiting for review comments${NC}"
  local start=$SECONDS

  while (( SECONDS - start < REVIEW_TIMEOUT )); do
    local all_done=true
    for bot in $REVIEW_BOTS; do
      if ! is_review_bot_done "$bot" "$OWNER" "$REPO" "$PR_NUM" "$REVIEW_BASELINE_FILE"; then
        all_done=false
      fi
    done

    if [[ "$all_done" == "true" ]]; then
      echo -e "  ${GREEN}${CHECK} All configured reviews complete${NC}"
      return 0
    fi

    local elapsed=$((SECONDS - start))
    echo -e "${DIM}    ${elapsed}s elapsed, waiting for bot reviews...${NC}"
    sleep "$POLL_INTERVAL"
  done

  echo -e "  ${RED}${CROSS} Review timeout — refusing to proceed without a complete review result${NC}"
  return 1
}

# ── Main loop ──────────────────────────────────────────────────
run_pipeline() {
  echo ""
  echo -e "${CYAN}╔══════════════════════════════════════════════════╗${NC}"
  echo -e "${CYAN}║${NC}  ${BOLD}Starting Greploop Pipeline${NC}                      ${CYAN}║${NC}"
  echo -e "${CYAN}╚══════════════════════════════════════════════════╝${NC}"
  echo ""

  mkdir -p "$DATA_DIR/iterations"

  if [[ "$DRY_RUN" == "true" ]]; then
    echo -e "  ${YELLOW}${INFO} Dry run — no review triggers, agent, commits, pushes, or CI polling${NC}"
    PIPELINE_RESULT="dry_run"
    write_final_report "$DATA_DIR" 0 "$PIPELINE_RESULT"
    return 0
  fi

  # Step 0: Initial review trigger
  trigger_reviews
  wait_for_reviews || {
    PIPELINE_RESULT="review_blocked"
    return 1
  }

  for (( ITERATION=1; ITERATION<=MAX_ITERATIONS; ITERATION++ )); do
    echo ""
    echo -e "${MAGENTA}╔══════════════════════════════════════════════════╗${NC}"
    echo -e "${MAGENTA}║${NC}  ${BOLD}Iteration $ITERATION / $MAX_ITERATIONS${NC}                   ${MAGENTA}║${NC}"
    echo -e "${MAGENTA}╚══════════════════════════════════════════════════╝${NC}"
    echo ""

    local iter_dir="$DATA_DIR/iterations/$ITERATION"
    mkdir -p "$iter_dir"

    # ── 1. Collect findings ────────────────────────────────────
    local findings
    findings=$(collect_findings "$OWNER" "$REPO" "$PR_NUM" "$iter_dir")
    local findings_file="$iter_dir/findings.json"
    echo "$findings" | jq '.' > "$findings_file"

    local actionable
    actionable=$(count_actionable "$findings")
    local total
    total=$(echo "$findings" | jq 'length')

    echo -e "${CYAN}┌─ Findings: ${BOLD}$total${NC} total, ${BOLD}$actionable${NC} actionable${NC}"

    # Check if clean
    if [[ "$actionable" -eq 0 ]]; then
      if [[ "$CI_BLOCKED" == "true" ]]; then
        echo -e "  ${RED}${CROSS} No actionable findings, but CI is not green${NC}"
        PIPELINE_RESULT="ci_blocked"
        write_iteration_report "$DATA_DIR" "$ITERATION" "$findings_file"
        write_final_report "$DATA_DIR" "$ITERATION" "$PIPELINE_RESULT"
        return 1
      fi

      echo -e "  ${GREEN}${CHECK} No actionable findings — PR is clean!${NC}"
      PIPELINE_RESULT="clean"
      write_iteration_report "$DATA_DIR" "$ITERATION" "$findings_file"
      write_final_report "$DATA_DIR" "$ITERATION" "$PIPELINE_RESULT"
      return 0
    fi

    # ── 2. Generate fix brief ──────────────────────────────────
    echo -e "${CYAN}┌─ Generating fix brief${NC}"
    local brief_file
    brief_file=$(generate_fix_brief "$DATA_DIR" "$ITERATION" "$findings_file")

    # ── 3. Spawn fix agent ─────────────────────────────────────
    echo -e "${CYAN}┌─ Fixing findings${NC}"
    local agent_base_sha
    agent_base_sha=$(get_current_sha)
    if ! spawn_fix_agent "$WORKTREE_DIR" "$brief_file" "$FIX_AGENT"; then
      echo -e "  ${YELLOW}${WARN} Fix agent had issues — checking for partial fixes${NC}"
    fi

    # Check for changes
    local changes
    if ! changes=$(check_agent_changes "$WORKTREE_DIR" "$agent_base_sha"); then
      echo -e "  ${YELLOW}${WARN} No changes from fix agent — skipping push${NC}"
      write_iteration_report "$DATA_DIR" "$ITERATION" "$findings_file"
      append_results "$DATA_DIR" "$ITERATION"
      continue
    fi

    if ! verify_agent_scope "$WORKTREE_DIR" "$agent_base_sha" "$findings_file" "$REVIEW_BASE_SHA"; then
      echo -e "  ${RED}${CROSS} Fix agent changed paths outside the reported findings${NC}"
      PIPELINE_RESULT="scope_blocked"
      write_iteration_report "$DATA_DIR" "$ITERATION" "$findings_file"
      append_results "$DATA_DIR" "$ITERATION"
      write_final_report "$DATA_DIR" "$ITERATION" "$PIPELINE_RESULT"
      return 1
    fi

    echo -e "${DIM}  Changes:${NC}"
    echo "$changes" | while IFS= read -r line; do echo -e "    $line"; done

    # Save git stat
    echo "$changes" > "$iter_dir/git-stat.txt"

    # ── 4. Run local tests before committing or pushing ────────
    echo -e "${CYAN}┌─ Local validation${NC}"
    local validation_failed=false
    if ! run_tests "$WORKTREE_DIR" "$TEST_CMD" "$DATA_DIR" "$ITERATION"; then
      validation_failed=true
      echo -e "  ${RED}${CROSS} Tests failed — fix agent will see this next iteration${NC}"
    fi

    if [[ -n "$LINT_CMD" ]]; then
      if ! run_lint "$WORKTREE_DIR" "$LINT_CMD"; then
        validation_failed=true
        echo -e "  ${RED}${CROSS} Lint failed — fix agent will see this next iteration${NC}"
      fi
    fi

    if [[ "$validation_failed" == "true" ]]; then
      write_iteration_report "$DATA_DIR" "$ITERATION" "$findings_file"
      append_results "$DATA_DIR" "$ITERATION"
      continue
    fi

    # ── 5. Commit only validated fixes ─────────────────────────
    if [[ "$(get_current_sha)" == "$agent_base_sha" ]]; then
      if ! commit_fixes "$WORKTREE_DIR" "$ITERATION" "$agent_base_sha" "$findings_file" "$REVIEW_BASE_SHA"; then
        echo -e "  ${YELLOW}${WARN} Nothing to commit${NC}"
        continue
      fi
    else
      echo -e "  ${GREEN}${CHECK} Fix agent already committed validated changes${NC}"
    fi

    # ── 6. Push to remote ──────────────────────────────────────
    echo -e "${CYAN}┌─ Push to remote${NC}"
    push_changes "$PUSH_REMOTE" "$LOCAL_BRANCH" "$BRANCH" "$FORCE"

    # ── 7. Wait for CI ─────────────────────────────────────────
    if [[ "$WAIT_CI" == "true" && "$DRY_RUN" != "true" ]]; then
      echo -e "${CYAN}┌─ CI checks${NC}"
      local sha
      sha=$(get_current_sha)

      if ! wait_for_ci "$OWNER" "$REPO" "$sha" "$CI_TIMEOUT" "$DATA_DIR" "$ITERATION"; then
        CI_BLOCKED=true
        echo -e "  ${RED}${CROSS} CI has failures — fix agent will address next iteration${NC}"
      else
        CI_BLOCKED=false
        echo -e "  ${GREEN}${CHECK} CI green!${NC}"
      fi
    fi

    # ── 8. Write iteration report ──────────────────────────────
    write_iteration_report "$DATA_DIR" "$ITERATION" "$findings_file"
    append_results "$DATA_DIR" "$ITERATION"

    # ── 9. Re-trigger reviews for next iteration ───────────────
    trigger_reviews

    # ── 10. Wait for new reviews ───────────────────────────────
    wait_for_reviews
  done

  # ── Max iterations reached ───────────────────────────────────
  if [[ "$CI_BLOCKED" == "true" ]]; then
    PIPELINE_RESULT="ci_blocked"
  else
    PIPELINE_RESULT="max_iterations"
  fi
  write_final_report "$DATA_DIR" "$MAX_ITERATIONS" "$PIPELINE_RESULT"

  echo -e "${YELLOW}${WARN} Max iterations ($MAX_ITERATIONS) reached${NC}"
  return 1
}

# Run the pipeline
run_pipeline
