#!/usr/bin/env bash
# lib/gh.sh — GitHub API helpers
set -euo pipefail

require_pr_open() {
  local owner="$1" repo="$2" pr_num="$3" state
  state=$(gh api "/repos/$owner/$repo/pulls/$pr_num" --jq '.state' 2>/dev/null) || return 1
  [[ "$state" == open ]] || { echo "ERROR: PR #$pr_num is $state; mutation refused" >&2; return 1; }
}

get_unresolved_comments() {
  local owner="$1" repo="$2" pr_num="$3" cursor="null" response nodes='[]'
  while :; do
    response=$(gh api graphql \
      -f query='query($owner:String!, $repo:String!, $number:Int!, $after:String) {
        repository(owner:$owner, name:$repo) {
          pullRequest(number:$number) {
            reviewThreads(first:100, after:$after) {
              pageInfo { hasNextPage endCursor }
              nodes { id isResolved isOutdated comments(first:100) {
                nodes { databaseId body path line originalLine author { login } }
              } }
            }
          }
        }
      }' -f owner="$owner" -f repo="$repo" -F number="$pr_num" -F after="$cursor" 2>/dev/null) || {
        echo "ERROR: unable to query GitHub review threads" >&2
        return 1
      }
    local page
    page=$(jq '.data.repository.pullRequest.reviewThreads.nodes // []' <<<"$response") || return 1
    nodes=$(jq -s '.[0] + .[1]' <(printf '%s\n' "$nodes") <(printf '%s\n' "$page"))
    if [[ "$(jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage' <<<"$response")" != true ]]; then break; fi
    cursor=$(jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.endCursor' <<<"$response")
    [[ -n "$cursor" && "$cursor" != null ]] || return 1
  done
  jq '[.[] | select(.isResolved == false and .isOutdated == false) | . as $thread
    | ($thread.comments.nodes // []) | select(length > 0)
    | {id: ($thread.id // "thread"),
       path: (.[0].path // "unknown"),
       line: (.[0].line // .[0].originalLine),
       body: (map((.author.login // "unknown") + ":\n" + (.body // "")) | join("\n\n---\n\n")),
       author: (.[0].author.login // "unknown")} ]' <<<"$nodes"
}

capture_review_baseline() {
  local owner="$1" repo="$2" pr_num="$3" bots="$4" baseline_file="$5"
  : > "$baseline_file"
  local bot login review_comments issue_comments review_id issue_id
  for bot in $bots; do
    case "$bot" in
      coderabbit) login="coderabbitai[bot]" ;;
      greptile) login="greptile-apps[bot]" ;;
      *) echo "ERROR: unsupported review bot: $bot" >&2; return 1 ;;
    esac
    review_comments=$(gh api --paginate --slurp "/repos/$owner/$repo/pulls/$pr_num/comments") || return 1
    issue_comments=$(gh api --paginate --slurp "/repos/$owner/$repo/issues/$pr_num/comments") || return 1
    review_id=$(jq --arg login "$login" '[.[][] | select(.user.login == $login) | .id] | max // 0' <<<"$review_comments")
    issue_id=$(jq --arg login "$login" '[.[][] | select(.user.login == $login) | .id] | max // 0' <<<"$issue_comments")
    printf '%s %s %s\n' "$bot" "$review_id" "$issue_id" >> "$baseline_file"
  done
}

is_review_bot_done() {
  local bot="$1" owner="$2" repo="$3" pr_num="$4" baseline_file="$5" login review_baseline issue_baseline
  case "$bot" in
    coderabbit) login="coderabbitai[bot]" ;;
    greptile) login="greptile-apps[bot]" ;;
    *) return 1 ;;
  esac
  review_baseline=$(awk -v bot="$bot" '$1 == bot { print $2 }' "$baseline_file")
  issue_baseline=$(awk -v bot="$bot" '$1 == bot { print $3 }' "$baseline_file")
  [[ -n "$review_baseline" && -n "$issue_baseline" ]] || return 1
  local review_comments issue_comments
  review_comments=$(gh api --paginate --slurp "/repos/$owner/$repo/pulls/$pr_num/comments") || return 1
  issue_comments=$(gh api --paginate --slurp "/repos/$owner/$repo/issues/$pr_num/comments") || return 1
  local bodies
  bodies=$(jq -r --arg login "$login" --argjson baseline "$review_baseline" '[.[][] | select(.user.login == $login and .id > $baseline) | .body] | .[]' <<<"$review_comments")$'\n'
  bodies+=$(jq -r --arg login "$login" --argjson baseline "$issue_baseline" '[.[][] | select(.user.login == $login and .id > $baseline) | .body] | .[]' <<<"$issue_comments")
  grep -Eiq 'all comments resolved|no issues found|review complete|no findings|lgtm' <<<"$bodies"
}

trigger_coderabbit() { require_pr_open "$1" "$2" "$3" && gh api --method POST -f body='@coderabbitai review' "/repos/$1/$2/issues/$3/comments" >/dev/null; }
trigger_greptile() { require_pr_open "$1" "$2" "$3" && gh api --method POST -f body='@greptile review' "/repos/$1/$2/issues/$3/comments" >/dev/null; }
get_pr_state() { gh api "/repos/$1/$2/pulls/$3" --jq '.state'; }
