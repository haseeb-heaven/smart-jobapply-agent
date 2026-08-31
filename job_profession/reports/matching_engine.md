# Deterministic matching engine report

## Changed files

- `src/job_profession/models.py`: dependency-free listing, minimal candidate-profile, and auditable result models.
- `src/job_profession/normalize.py`: visible-field normalization, canonical URL handling, and SHA-256 listing fingerprints.
- `src/job_profession/matcher.py`: YAML-configured deterministic scoring and hard rejections.
- `config/scoring_rules.yaml`: explicit weights, thresholds, and rejection terms.
- `tests/test_normalize.py` and `tests/test_matcher.py`: fixture-free behavior tests.

## Verification

Command: `pytest tests/test_normalize.py tests/test_matcher.py -v`

Output: `11 passed in 0.19s`.

## Matching assumptions

- The score is profile fit only, never a probability of being hired.
- `recommended` requires a score of at least 85, direct professional evidence, and no hard gaps; scores 70–84 require review; lower scores reject.
- General full-stack listings reject unless backend responsibilities materially dominate, and then remain capped at a review-level title contribution.
- A production GenAI/LLM requirement is rejected when it is mandatory or asks for ownership; personal/open-source evidence never adds professional-experience points.

## Blockers

None. The workspace has no Git repository, so no commit or Git diff check was possible.
