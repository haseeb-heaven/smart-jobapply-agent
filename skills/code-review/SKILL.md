---
name: code-review
description: >-
  Review the changes since the last commit (or a specified diff). Analyze diffs for bugs,
  style violations, logic errors, missing tests, security issues, and performance problems.
  Provides constructive feedback with severity levels. Use when asked to review code changes.
---

# Code Review

## Smart JobApply Agent override

Always review an explicit merge-base-to-head range or PR diff. Feature reviews
target `develop`; release reviews target `main`. The reviewer must be distinct
from implementers/fixers and map acceptance criteria to code or test evidence.

Systematic approach to reviewing pull request changes that catches real problems without wasting time on noise.

## Workflow

### Phase 1: Context
1. **Understand intent** — Read PR description, linked issues, and discussion to understand what this change is trying to achieve
2. **Read changed files** — Focus only on modified lines. Ignore unmodified boilerplate and auto-generated code unless suspicious
3. **Check existing patterns** — Look at similar code in the repo to understand conventions being followed

### Phase 2: Analysis
1. **Correctness** — Logic flow, edge cases, error handling paths, null/undefined safety
2. **Security** — Injection vectors, auth bypasses, data exposure, dependency vulnerabilities
3. **Performance** — N+1 queries, O(n²) loops, unnecessary allocations, blocking operations
4. **Tests** — Are new features tested? Do tests cover edge cases or just happy path? Are they testing public interfaces?
5. **Style & Consistency** — Naming conventions, error handling patterns, file organization matching the codebase

### Phase 3: Feedback
Categorize findings by severity:

| Level | Meaning | Expectation |
|-------|---------|-------------|
| 🔴 Blocker | Bug, security issue, data loss risk | Must fix before merge |
| 🟡 Concern | Suboptimal pattern, missing edge case | Should address, can merge after discussion |
| 💡 Suggestion | Style, naming, minor improvement | Author may accept or decline |

## What to Skip

- Personal style preferences already established in the project
- Nitpicks on unchanged code
- "It would be nice if" without concrete reasoning

## Output Format

```
## Review Summary
- **Overall**: [Approve / Request Changes / Comment]

## Issues Found
### 🔴 Critical
- [File:line]: Issue → Suggested fix

### 🟡 Concerns
- [File:line]: Issue → Suggestion

### 💡 Suggestions
- Point: Improvement idea

## Positive Notes
- What was done well
```

**Source:** [mattpocock/skills](https://github.com/mattpocock/skills) — Skills for Real Engineers
