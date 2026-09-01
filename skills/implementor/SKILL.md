---
name: implementor
description: >-
  Structured implementation skill that breaks down feature requests into actionable
  steps, writes clean code following project conventions, validates with tests,
  and ensures completion before marking done. Use when building features, fixing
  bugs, implementing requirements, or any task requiring systematic development.
---

# Implementor Skill

## Smart JobApply Agent override

Follow the assigned lane and file ownership from `AGENTS.md`. Make safe,
in-scope assumptions when repository evidence resolves ambiguity. Never approve
your own work; return the diff scope and validation evidence to an independent
reviewer and test runner.

Systematic approach to implementing software changes that produces correct, maintainable code with proper test coverage — nothing skipped, nothing assumed.

## Workflow

### Phase 1: Understand
1. **Read the requirement** — Restate it in your own words to confirm understanding. If ambiguous, ask clarifying questions before proceeding.
2. **Map the codebase** — Identify existing files, functions, and patterns relevant to the change. Don't assume — read the actual code.
3. **Identify integration points** — Where does this fit? What needs to change to support the new behavior?

### Phase 2: Plan
1. **Break into subtasks** — List each logical step needed. Each subtask should be small enough to verify independently.
2. **Order by dependency** — Some things must exist before others can work. Respect those constraints.
3. **Consider edge cases** — What could go wrong? Error paths? Invalid input? Concurrency? Note them before writing code.

### Phase 3: Implement
1. **Write one subtask at a time** — Complete and verify each before moving to the next. Never leave an intermediate state broken.
2. **Follow project conventions** — Match existing naming, structure, error handling style, and import patterns. Read similar code for reference.
3. **No shortcuts on errors** — Every external call, parse operation, and user input must have proper error handling. No try/catch without meaningful recovery.
4. **Self-document where complexity exists** — Brief inline comments explaining why, not what (the code shows what).

### Phase 4: Validate
1. **Run relevant checks** — Lint, typecheck, build, and tests. All must pass.
2. **Verify behavior matches requirement** — Go through the original requirement line by line. Is every part implemented?
3. **Check for regressions** — Did something that worked before now break? Especially check related areas you didn't directly modify.

### Phase 5: Complete
1. **Ensure nothing is left half-done** — No TODOs unless explicitly requested. No commented-out code. No debug statements.
2. **Summarize changes** — Briefly list what was modified, created, and deleted. Include file paths.
3. **Flag any assumptions or limitations** — Be transparent about anything you couldn't fully resolve.

## Rules

- **Never assume** — If you're unsure how something works, read the code. Don't guess.
- **One breaking change at a time** — If multiple things need changing, do them sequentially with verification between steps.
- **Tests are non-negotiable** — New code gets tests. Changed code gets updated tests. Broken tests don't get suppressed — they get fixed.
- **Don't refactor unless asked** — Focus on the stated requirement. Cleaning up unrelated code creates scope creep and risk.
- **Report failures honestly** — If something can't be done as specified, explain why and suggest alternatives. Don't paper over issues.

## Output Format

After completing an implementation:

```
## Implementation Complete

### Changes Made
| File | Action | Summary |
|------|--------|---------|
| `src/foo.ts` | Modified | [What changed] |
| `src/bar.test.ts` | Created | [Test coverage added] |

### Verification
- [x] Linting passes
- [x] Type checking passes
- [x] Tests pass
- [x] Build succeeds

### Notes
- [Any assumptions, limitations, or follow-ups]
```
