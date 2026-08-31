#!/usr/bin/env bash
# lib/gh.sh — GitHub API helpers
set -euo pipefail

# Get unresolved review threads for a PR.
# GitHub's REST review-comment endpoint does not expose thread resolution state;
# use GraphQL and fail closed if pagination would hide additional threads.
get_unresolved_comments() {
  local owner="$1" repo="$2" pr_num="$3"
  local response
  response=$(gh api graphql \
    -f query='query($owner:String!, $repo:String!, $number:Int!) {
      repository(owner:$owner, name:$repo) {
        pullRequest(number:$number) {
          reviewThreads(first:100) {
            pageInfo { hasNextPage }
            nodes {
              id isResolved isOutdated
              comments(first:1) {
                nodes { databaseId body path line originalLine author { login } }
              }
            }
          }
        }
      }
    }' \
    -f owner="$owner" -f repo="$repo" -F number="$pr_num" 2>/dev/null) || {
      echo "Unable to query GitHub review threads" >&2
      return 1
    }

  if [[ "$(echo "$response" | jq -r '.data.repository.pullRequest.reviewThreads.pageInfo.hasNextPage')" == "true" ]]; then
    echo "More than 100 review threads exist; refusing to declare the PR clean" >&2
    return 1
  fi

  echo "$response" | jq '[.data.repository.pullRequest.reviewThreads.nodes[]
    | select(.isResolved == false and .isOutdated == false)
    | . as $thread
    | $thread.comments.nodes[0]
    | select(. != null)
    | {id: ($thread.id // "thread"), path: (.path // "unknown"), line: (.line // .originalLine), body, author: (.author.login // "unknown")} ]'
}

# Get review comments with more detail (path, line, body, author)
get_review_details() {
  local owner="$1" repo="$2" pr_num="$3"
  gh api \
    -H "Accept: application/vnd.github+json" \
    "/repos/$owner/$repo/pulls/$pr_num/comments" \
    --jq '[.[] | {id: .id, path: .path, line: .line, body: .body, author: .user.login, resolved: .resolved, created: .created_at}]' 2>/dev/null || echo "[]"
}

# Resolve a review thread by GraphQL node ID.
resolve_comment() {
  local thread_id="$3"
  gh api graphql \
    -f query='mutation($threadId:ID!) {
      resolveReviewThread(input:{threadId:$threadId}) { thread { isResolved } }
    }' \
    -f threadId="$thread_id" --jq '.data.resolveReviewThread.thread.isResolved'
}

# Post a comment on a PR
post_pr_comment() {
  local owner="$1" repo="$2" pr_num="$3" body="$4"
  gh api \
    --method POST \
    -H "Accept: application/vnd.github+json" \
    -f body="$body" \
    "/repos/$owner/$repo/issues/$pr_num/comments" 2>/dev/null
}

# Trigger CodeRabbit review
trigger_coderabbit() {
  local owner="$1" repo="$2" pr_num="$3"
  gh api \
    --method POST \
    -H "Accept: application/vnd.github+json" \
    -f body="@coderabbitai review" \
    "/repos/$owner/$repo/issues/$pr_num/comments" 2>/dev/null
}

# Trigger Greptile review
trigger_greptile() {
  local owner="$1" repo="$2" pr_num="$3"
  gh api \
    --method POST \
    -H "Accept: application/vnd.github+json" \
    -f body="@greptile review" \
    "/repos/$owner/$repo/issues/$pr_num/comments" 2>/dev/null
}

# Get PR merge state
get_merge_state() {
  local owner="$1" repo="$2" pr_num="$3"
  gh api \
    -H "Accept: application/vnd.github+json" \
    "/repos/$owner/$repo/pulls/$pr_num" \
    --jq '.mergeable_state // "unknown"' 2>/dev/null
}

# Get PR state
get_pr_state() {
  local owner="$1" repo="$2" pr_num="$3"
  gh api \
    -H "Accept: application/vnd.github+json" \
    "/repos/$owner/$repo/pulls/$pr_num" \
    --jq '.state // "unknown"' 2>/dev/null
}

# Get latest CI check runs
get_check_runs() {
  local owner="$1" repo="$2" sha="$3"
  gh api \
    -H "Accept: application/vnd.github+json" \
    "/repos/$owner/$repo/commits/$sha/check-runs" \
    --jq '.check_runs[] | {name: .name, status: .status, conclusion: .conclusion, details_url: .html_url}' 2>/dev/null
}

# Wait for all check runs to complete
wait_for_checks() {
  local owner="$1" repo="$2" sha="$3" timeout="${4:-3600}"
  local start=$SECONDS

  while (( SECONDS - start < timeout )); do
    local runs
    runs=$(gh api \
      -H "Accept: application/vnd.github+json" \
      "/repos/$owner/$repo/commits/$sha/check-runs" \
      --jq '.check_runs[] | select(.status == "in_progress" or .status == "queued") | .status' 2>/dev/null)

    if [[ -z "$runs" ]]; then
      echo "all_checks_complete"
      return 0
    fi

    sleep 30
  done

  echo "timeout"
  return 1
}

# Get check run conclusions
get_check_conclusions() {
  local owner="$1" repo="$2" sha="$3"
  gh api \
    -H "Accept: application/vnd.github+json" \
    "/repos/$owner/$repo/commits/$sha/check-runs" \
    --jq '[.check_runs[] | select(.conclusion != null) | {name: .name, conclusion: .conclusion}]' 2>/dev/null
}
