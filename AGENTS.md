# Smart JobApply Agent — Multi-Agent Contract

This repository is operated by an LLM coding agent. Before changing code, read
`README.md`, `SECURITY.md`, and the nearest applicable `SKILL.md`.

## Delivery lanes

For non-trivial changes, use independent lanes when agent capacity is available:

| Lane | Responsibility | Must not do |
| --- | --- | --- |
| Investigator | Inspect current behavior, constraints, and acceptance criteria | Edit product code |
| Implementer | Own the scoped production files and focused regression tests | Approve its own work |
| Unit-test author | Add boundary, error, and adversarial tests from the contract | Change production behavior to make tests pass |
| Reviewer | Check correctness, security, maintainability, and spec compliance | Rewrite unrelated code |
| Critic/bug finder | Challenge assumptions, uncertainty handling, unsafe matches, and lifecycle failures | Weaken evidence or safety rules |
| Test runner | Execute clean validation commands and capture exact evidence | Modify code under test |

Do not let concurrent implementers edit the same files. Give every worker exact
file ownership, remind it that other agents are active, and require it to
preserve unrelated changes. Run dependent lanes sequentially. When capacity is
unavailable, perform the same lanes one after another and say that they were not
independent; never invent a review or test result.

## Required gates

Every change must preserve these invariants:

- job eligibility is decided before ranking;
- professional, personal/open-source, and learning evidence remain separate;
- missing or ambiguous evidence is displayed as uncertainty, not a verified fit;
- no code logs in, reads credentials/cookies, fills forms, uploads documents,
  solves CAPTCHA, clicks application controls, or submits an application;
- ignored candidate data and runtime artifacts never enter Git.

Before committing, run:

```sh
python -m pytest
ruff check job_profession/src job_profession/scripts tests
python -m coverage run --branch \
  --include='*/skills/easy-apply-tab-monitor/scripts/chrome_tab_watcher.py' \
  -m pytest tests/test_chrome_tab_watcher.py
python -m coverage report \
  --include='*/skills/easy-apply-tab-monitor/scripts/chrome_tab_watcher.py' \
  --fail-under=100
python -m compileall -q job_profession/src job_profession/scripts skills/easy-apply-tab-monitor/scripts
```

The final reviewer must inspect the actual diff and map each acceptance
criterion to code or test evidence. A green suite proves only covered cases.
