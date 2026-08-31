#!/usr/bin/env bash
# lib/report.sh — Generate iteration and final reports
set -euo pipefail

REPORT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$REPORT_LIB_DIR/colors.sh"

# Generate iteration report
write_iteration_report() {
  local data_dir="$1" iteration="$2" findings_file="$3"
  local report_file="$data_dir/iterations/$iteration/report.md"

  local findings_count actionable_count
  findings_count=$(jq 'length' "$findings_file")
  actionable_count=$(jq '[.[] | select(.severity == "high" or .severity == "medium")] | length' "$findings_file")

  local high medium low
  high=$(jq '[.[] | select(.severity == "high")] | length' "$findings_file")
  medium=$(jq '[.[] | select(.severity == "medium")] | length' "$findings_file")
  low=$(jq '[.[] | select(.severity == "low")] | length' "$findings_file")

  cat > "$report_file" << EOF
# Iteration $iteration Report

## Summary
- **Findings collected:** $findings_count
- **Actionable (high + medium):** $actionable_count
- **Severity breakdown:** ${RED}High: $high${NC} ${YELLOW}Medium: $medium${NC} ${DIM}Low: $low${NC}

## Findings Detail
EOF

  jq -r '.[] | "### \(.severity | ascii_upcase) — \(.path):\(.line // 0)\n\n\(.body | gsub("\n"; "\n"))\n"' \
    "$findings_file" >> "$report_file" 2>/dev/null || true

  echo -e "${DIM}  Report: $report_file${NC}"
}

# Append results to iteration report
append_results() {
  local data_dir="$1" iteration="$2"
  local report_file="$data_dir/iterations/$iteration/report.md"

  local test_output="$data_dir/iterations/$iteration/test-output.txt"
  local lint_output="$data_dir/iterations/$iteration/lint-output.txt"
  local ci_conclusions="$data_dir/iterations/$iteration/ci-conclusions.json"
  local git_stat="$data_dir/iterations/$iteration/git-stat.txt"

  cat >> "$report_file" << 'EOF'

## Results

### Local Tests
EOF

  if [[ -f "$test_output" ]]; then
    echo '```' >> "$report_file"
    tail -30 "$test_output" >> "$report_file"
    echo '```' >> "$report_file"
  else
    echo "_Not run_" >> "$report_file"
  fi

  cat >> "$report_file" << 'EOF'

### Lint
EOF
  if [[ -f "$lint_output" ]]; then
    echo '```' >> "$report_file"
    tail -30 "$lint_output" >> "$report_file"
    echo '```' >> "$report_file"
  else
    echo "_Not run_" >> "$report_file"
  fi

  cat >> "$report_file" << 'EOF'

### CI Status
EOF

  if [[ -f "$ci_conclusions" ]]; then
    echo '```json' >> "$report_file"
    cat "$ci_conclusions" >> "$report_file"
    echo '```' >> "$report_file"
  else
    echo "_Not run_" >> "$report_file"
  fi

  cat >> "$report_file" << 'EOF'

### Changes
EOF

  if [[ -f "$git_stat" ]]; then
    echo '```' >> "$report_file"
    cat "$git_stat" >> "$report_file"
    echo '```' >> "$report_file"
  else
    echo "_No changes_" >> "$report_file"
  fi
}

# Generate final report
write_final_report() {
  local data_dir="$1" total_iterations="$2" result="$3"
  local report_file="$data_dir/report.md"

  cat > "$report_file" << EOF
# Greploop Pipeline — Final Report

**Date:** $(date -u +"%Y-%m-%dT%H:%M:%SZ")
**Total iterations:** $total_iterations
**Result:** $result

## Iteration Summary

| Iteration | Findings | Actionable | Tests | CI |
|-----------|----------|------------|-------|----|
EOF

  for i in $(seq 1 "$total_iterations"); do
    local iter_dir="$data_dir/iterations/$i"
    [[ ! -d "$iter_dir" ]] && continue

    local findings actionable tests ci
    findings=$(jq 'length' "$iter_dir/findings.json" 2>/dev/null || echo "?")
    actionable=$(jq '[.[] | select(.severity == "high" or .severity == "medium")] | length' "$iter_dir/findings.json" 2>/dev/null || echo "?")

    if [[ -f "$iter_dir/test-failures.txt" ]]; then
      tests="failed"
    elif [[ -f "$iter_dir/test-output.txt" ]]; then
      tests=$(grep -Eo '[0-9]+ passed' "$iter_dir/test-output.txt" | tail -1 || echo "ran")
    else
      tests="skipped"
    fi

    if [[ -f "$iter_dir/ci-conclusions.json" ]]; then
      local passed failed
      passed=$(jq '[.[] | select(.conclusion == "success")] | length' "$iter_dir/ci-conclusions.json" 2>/dev/null || echo "?")
      failed=$(jq '[.[] | select(.conclusion != "success" and .conclusion != "neutral" and .conclusion != "skipped")] | length' "$iter_dir/ci-conclusions.json" 2>/dev/null || echo "?")
      ci="passed: $passed, failed: $failed"
    else
      ci="pending"
    fi

    echo "| $i | $findings | $actionable | $tests | $ci |" >> "$report_file"
  done

  cat >> "$report_file" << EOF

## Conclusion

EOF

  case "$result" in
    clean)
      echo "🎉 **Review findings are clean.** Validation results above reflect only stages that actually ran." >> "$report_file"
      echo "" >> "$report_file"
      echo "The PR is ready for captain review and merge." >> "$report_file"
      ;;
    max_iterations)
      echo "⚠️ **Max iterations reached** after $total_iterations cycles." >> "$report_file"
      echo "" >> "$report_file"
      echo "Remaining findings require manual attention. Review the iteration reports above." >> "$report_file"
      ;;
    ci_blocked)
      echo "🚫 **CI blocked** — pipeline could not achieve green CI." >> "$report_file"
      echo "" >> "$report_file"
      echo "Check the CI failure details in the iteration reports." >> "$report_file"
      ;;
    *)
      echo "⏹ **Pipeline stopped:** $result" >> "$report_file"
      ;;
  esac

  echo -e "${GREEN}  ${CHECK} Final report: ${BOLD}$report_file${NC}"
}
