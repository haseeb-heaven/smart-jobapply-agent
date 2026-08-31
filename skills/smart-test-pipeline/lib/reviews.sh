#!/usr/bin/env bash
# lib/reviews.sh — Collect and parse review findings from PR comments
set -euo pipefail

REVIEWS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$REVIEWS_LIB_DIR/colors.sh"

# Collect all unresolved review comments and parse into structured findings
# Output: JSON array of {file, line, severity, description, source}
collect_findings() {
  local owner="$1" repo="$2" pr_num="$3" data_dir="$4"

  echo -e "${DIM}  ${INFO} Collecting unresolved review comments...${NC}" >&2

  local comments
  comments=$(get_unresolved_comments "$owner" "$repo" "$pr_num") || {
    echo -e "${YELLOW}  ${WARN} Could not fetch review threads${NC}" >&2
    return 1
  }

  local findings="[]"
  local count=0

  while IFS= read -r comment; do
    [[ -z "$comment" ]] && continue

    local id path line body author
    id=$(echo "$comment" | jq -r '.id // empty')
    path=$(echo "$comment" | jq -r '.path // "unknown"')
    line=$(echo "$comment" | jq -r '.line // "null"')
    body=$(echo "$comment" | jq -r '.body // ""')
    author=$(echo "$comment" | jq -r '.author // "unknown"')

    # Parse severity from body (CodeRabbit uses 🟡/🟠/🔴, Greptile uses numeric)
    local severity="medium"
    if echo "$body" | grep -q '🔴\|CRITICAL\|HIGH'; then
      severity="high"
    elif echo "$body" | grep -q '🟠\|MAJOR\|Medium'; then
      severity="medium"
    elif echo "$body" | grep -q '🟡\|MINOR\|Low'; then
      severity="low"
    fi

    # Extract file path from body if not in path field
    if [[ "$path" == "unknown" ]]; then
      local extracted_path
      extracted_path=$(echo "$body" | grep -Eo 'backend/[^:[:space:]]+|frontend/[^:[:space:]]+|[^:[:space:]]+\.(py|ts|tsx)' | head -1)
      [[ -n "$extracted_path" ]] && path="$extracted_path"
    fi

    # Build finding object
    local finding
    finding=$(jq -n \
      --arg id "$id" \
      --arg path "$path" \
      --arg line "$line" \
      --arg severity "$severity" \
      --arg body "$body" \
      --arg source "$author" \
      '{
        id: $id,
        path: $path,
        line: ($line | if . == "null" then null else (. | tonumber) end),
        severity: $severity,
        body: $body,
        source: $source
      }')

    findings=$(echo "$findings" | jq ". + [$finding]")
    count=$((count + 1))
  done <<< "$(echo "$comments" | jq -c '.[]')"

  # Save raw findings
  echo "$findings" | jq '.' > "$data_dir/findings.json"

  # Summary
  local high medium low
  high=$(echo "$findings" | jq '[.[] | select(.severity == "high")] | length')
  medium=$(echo "$findings" | jq '[.[] | select(.severity == "medium")] | length')
  low=$(echo "$findings" | jq '[.[] | select(.severity == "low")] | length')

  echo -e "  ${INFO} Found ${BOLD}$count${NC} unresolved findings" >&2
  echo -e "    ${RED}High: $high${NC}  ${YELLOW}Medium: $medium${NC}  ${DIM}Low: $low${NC}" >&2

  echo "$findings"
}

# Check for Greptile score
get_greptile_score() {
  local owner="$1" repo="$2" pr_num="$3"

  local score
  score=$(gh api \
    -H "Accept: application/vnd.github+json" \
    "/repos/$owner/$repo/pulls/$pr_num/comments" \
    --jq '[.[] | select(.user.login == "greptile-apps[bot]") | .body] | last // ""' 2>/dev/null)

  # Extract confidence score from Greptile comment
  local conf
  conf=$(echo "$score" | grep -Eo 'confidence[[:space:]:]+[0-9]+/[0-9]+' | grep -Eo '[0-9]+' | head -1)

  if [[ -n "$conf" ]]; then
    echo "$conf"
  else
    echo "0"
  fi
}

# Check if CodeRabbit review is complete
is_coderabbit_done() {
  local owner="$1" repo="$2" pr_num="$3"

  local last_comment
  last_comment=$(gh api \
    -H "Accept: application/vnd.github+json" \
    "/repos/$owner/$repo/pulls/$pr_num/comments" \
    --jq '[.[] | select(.user.login == "coderabbitai[bot]") | .body] | last // ""' 2>/dev/null)

  if echo "$last_comment" | grep -q "All comments resolved\|No issues found\|review complete"; then
    echo "true"
  else
    echo "false"
  fi
}

capture_review_baseline() {
  local owner="$1" repo="$2" pr_num="$3" bots="$4" baseline_file="$5"
  : > "$baseline_file"

  for bot in $bots; do
    local login review_id issue_id
    case "$bot" in
      coderabbit) login="coderabbitai[bot]" ;;
      greptile) login="greptile-apps[bot]" ;;
      *) continue ;;
    esac

    local review_comments issue_comments
    review_comments=$(gh api \
      -H "Accept: application/vnd.github+json" \
      "/repos/$owner/$repo/pulls/$pr_num/comments" \
      --paginate --slurp 2>/dev/null) || return 1
    issue_comments=$(gh api \
      -H "Accept: application/vnd.github+json" \
      "/repos/$owner/$repo/issues/$pr_num/comments" \
      --paginate --slurp 2>/dev/null) || return 1
    review_id=$(echo "$review_comments" | jq --arg login "$login" '[.[][] | select(.user.login == $login) | .id] | max // 0')
    issue_id=$(echo "$issue_comments" | jq --arg login "$login" '[.[][] | select(.user.login == $login) | .id] | max // 0')
    echo "$bot $review_id $issue_id" >> "$baseline_file"
  done
}

# Check if a configured review bot has completed its review.
is_review_bot_done() {
  local bot="$1" owner="$2" repo="$3" pr_num="$4" baseline_file="$5"
  local login
  case "$bot" in
    coderabbit) login="coderabbitai[bot]" ;;
    greptile) login="greptile-apps[bot]" ;;
    *) return 1 ;;
  esac

  local review_baseline issue_baseline
  review_baseline=$(awk -v bot="$bot" '$1 == bot { print $2 }' "$baseline_file")
  issue_baseline=$(awk -v bot="$bot" '$1 == bot { print $3 }' "$baseline_file")
  [[ -n "$review_baseline" && -n "$issue_baseline" ]] || return 1

  local review_comments issue_comments
  review_comments=$(gh api \
    -H "Accept: application/vnd.github+json" \
    "/repos/$owner/$repo/pulls/$pr_num/comments" \
    --paginate --slurp 2>/dev/null) || return 1
  issue_comments=$(gh api \
    -H "Accept: application/vnd.github+json" \
    "/repos/$owner/$repo/issues/$pr_num/comments" \
    --paginate --slurp 2>/dev/null) || return 1

  local review_bodies issue_bodies
  review_bodies=$(echo "$review_comments" | jq -r --arg login "$login" --argjson baseline "$review_baseline" '[.[][] | select(.user.login == $login and .id > $baseline) | .body] | .[]')
  issue_bodies=$(echo "$issue_comments" | jq -r --arg login "$login" --argjson baseline "$issue_baseline" '[.[][] | select(.user.login == $login and .id > $baseline) | .body] | .[]')

  printf '%s\n%s\n' "$review_bodies" "$issue_bodies" |
    grep -Eiq "all comments resolved|no issues found|review complete|no findings|lgtm"
}

count_actionable() {
  local findings="$1"
  echo "$findings" | jq 'length'
}
