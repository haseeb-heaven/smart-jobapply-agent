#!/usr/bin/env bash
# lib/validate.sh — Run local tests, lint, and CI checks
set -euo pipefail

source "$(dirname "$0")/colors.sh"

# Run local tests
run_tests() {
  local worktree_dir="$1" test_cmd="$2" data_dir="$3" iteration="$4"

  echo -e "${CYAN}  ${PLAY} Running local tests...${NC}"
  cd "$worktree_dir"

  local test_output
  local test_rc=0
  test_output=$(eval "$test_cmd" 2>&1) || test_rc=$?

  # Save test output
  echo "$test_output" > "$data_dir/iterations/$iteration/test-output.txt"

  # Extract failures
  local failures
  failures=$(echo "$test_output" | grep -E "FAILED|ERROR|AssertionError|ModuleNotFoundError" | head -50) || true

  if [[ -n "$failures" ]]; then
    echo "$failures" > "$data_dir/iterations/$iteration/test-failures.txt"
  fi

  if [[ $test_rc -eq 0 ]]; then
    echo -e "  ${GREEN}${CHECK} Tests passed${NC}"
    return 0
  else
    local passed failed
    passed=$(echo "$test_output" | grep -Eo '[0-9]+ passed' | grep -Eo '[0-9]+' | head -1) || passed="?"
    failed=$(echo "$test_output" | grep -Eo '[0-9]+ failed' | grep -Eo '[0-9]+' | head -1) || failed="?"
    echo -e "  ${RED}${CROSS} Tests failed (passed: $passed, failed: $failed)${NC}"
    return 1
  fi
}

# Run local lint
run_lint() {
  local worktree_dir="$1" lint_cmd="$2"

  if [[ -z "$lint_cmd" ]]; then
    echo -e "${DIM}  ${INFO} Lint command not set — skipping${NC}"
    return 0
  fi

  echo -e "${CYAN}  ${PLAY} Running lint...${NC}"
  cd "$worktree_dir"

  local lint_output
  local lint_rc=0
  lint_output=$(eval "$lint_cmd" 2>&1) || lint_rc=$?

  if [[ $lint_rc -eq 0 ]]; then
    echo -e "  ${GREEN}${CHECK} Lint passed${NC}"
    return 0
  else
    echo -e "  ${RED}${CROSS} Lint failed${NC}"
    echo "$lint_output" | tail -20
    return 1
  fi
}

# Push to remote
push_changes() {
  local remote="$1" local_branch="$2" remote_branch="$3" force="$4"

  echo -e "${CYAN}  ${ARROW} Pushing to $remote/$remote_branch...${NC}"

  if [[ "$force" == "true" ]]; then
    git push "$remote" "$local_branch:$remote_branch" --force-with-lease 2>&1
  else
    git push "$remote" "$local_branch:$remote_branch" 2>&1
  fi

  echo -e "  ${GREEN}${CHECK} Pushed${NC}"
}

# Wait for CI to complete
wait_for_ci() {
  local owner="$1" repo="$2" sha="$3" timeout="$4" data_dir="$5" iteration="$6"

  echo -e "${CYAN}  ${WAIT} Waiting for CI (timeout: ${DIM}${timeout}s${NC})...${NC}"

  local start=$SECONDS
  while (( SECONDS - start < timeout )); do
    local check_runs
    check_runs=$(gh api \
      -H "Accept: application/vnd.github+json" \
      "/repos/$owner/$repo/commits/$sha/check-runs" \
      --paginate --slurp 2>/dev/null) || {
      echo -e "${YELLOW}  ${WARN} Could not fetch check runs${NC}"
      check_runs=""
    }

    if [[ -z "$check_runs" ]]; then
      sleep 30
      continue
    fi

    local pending
    pending=$(echo "$check_runs" | jq '[.[].check_runs[] | select(.status == "in_progress" or .status == "queued")] | length')

    if [[ "$pending" -eq 0 ]]; then
      echo -e "  ${GREEN}${CHECK} CI checks complete${NC}"

      # Get conclusions
      local conclusions
      conclusions=$(echo "$check_runs" | jq '[.[].check_runs[] | select(.conclusion != null) | {name: .name, conclusion: .conclusion}]')

      echo "$conclusions" > "$data_dir/iterations/$iteration/ci-conclusions.json"

      # Check for failures
      local total non_success success
      total=$(echo "$conclusions" | jq 'length')
      non_success=$(echo "$conclusions" | jq '[.[] | select(.conclusion != "success")] | length')
      success=$(echo "$conclusions" | jq '[.[] | select(.conclusion == "success")] | length')

      if [[ "$total" -eq 0 ]]; then
        echo -e "${DIM}    No CI checks registered yet; continuing to poll...${NC}"
        sleep 30
        continue
      fi

      echo -e "  ${INFO} CI results: ${GREEN}passed: $success${NC}  ${RED}non-success: $non_success${NC}"

      # Show failed checks
      if [[ "$non_success" -gt 0 ]]; then
        echo "$conclusions" | jq -r '.[] | select(.conclusion != "success") | "    ${RED}✗${NC} \(.name): \(.conclusion)"'
      fi

      if [[ "$total" -gt 0 && "$non_success" -eq 0 ]]; then
        return 0
      fi

      # Save CI failure details for fix agent
      echo "## CI Failures" > "$data_dir/iterations/$iteration/ci-failures.md"
      echo "" >> "$data_dir/iterations/$iteration/ci-failures.md"
      echo "$conclusions" | jq -r '.[] | select(.conclusion != "success") | "- **\(.name)**: \(.conclusion)"' \
        >> "$data_dir/iterations/$iteration/ci-failures.md" 2>/dev/null || true

      return 1
    fi

    local elapsed=$((SECONDS - start))
    echo -e "${DIM}    ${elapsed}s elapsed, $pending checks still running...${NC}"
    sleep 30
  done

  echo -e "  ${RED}${CROSS} CI timeout after ${timeout}s${NC}"
  return 1
}

# Get current SHA
get_current_sha() {
  git rev-parse HEAD 2>/dev/null
}
