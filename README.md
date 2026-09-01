# Smart JobApply Agent — Operating Contract

This repository is an operating contract for an AI coding agent, not a human-facing job-search application. Use it through **Codex**, **Claude Code**, **Grok**, or another capable LLM agent. The LLM is the review brain; this repository supplies deterministic matching, evidence boundaries, local state, and validation.

## Agent mission

Turn candidate-approved facts and already-visible job-listing data into a small, explainable review queue. Recommend roles only when the evidence supports the recommendation. A fit score measures profile overlap, never interview, salary, eligibility, or hiring probability.

## Non-negotiable safety boundary

- Never log in, read credentials/cookies, solve CAPTCHAs, bypass a job board, send messages, upload a resume, fill a form, click Apply/Easy Apply/Next/Submit, or accept an attestation.
- Never fabricate skills, experience, salary, work authorization, location, sponsorship, availability, or screening answers.
- Never expose private profiles, resumes, email contents, browser state, API keys, compensation, visa details, or local job-search notes in a commit, issue, log, prompt, or response.
- Preserve the deterministic matcher result and its explanation. If an LLM interpretation conflicts with the policy, mark it uncertain and defer to the deterministic policy.

The candidate owns every application action and the final submission.

## Required local setup

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp job_profession/private.example/candidate_profile.yaml job_profession/private/candidate_profile.yaml
cp job_profession/private.example/application_answers.yaml job_profession/private/application_answers.yaml
cp job_profession/private.example/documents_manifest.yaml job_profession/private/documents_manifest.yaml
chmod 700 job_profession/private
```

The `job_profession/private/` directory is intentionally ignored. Keep every candidate-specific value there. `resumes/` and `codex-apply-*.*` are also ignored. Before every push, inspect `git status --short` and the staged diff.

## Agent workflow

1. Read the private profile only from the local ignored manifests. Treat fields that require confirmation as unavailable.
2. Accept job data only from an already-visible LinkedIn or Indeed card/listing, via the visible-page adapter. Preserve source URL, date, and description snapshot; do not scrape through login or invent missing details.
3. Run the deterministic matcher. Reject titles or requirements outside the candidate’s approved evidence before ranking. Surface supporting reasons, gaps, and uncertainty for every result.
4. Dedupe listings and write the auditable local queue. Keep tracker history append-only.
5. Open at most five recommended listing tabs for candidate review. A tab may be an external path or Easy Apply, but do not enter either form.
6. If monitoring is requested, run the review-only watcher from a local manifest. It may reopen only the exact missing listing URLs; it must never replace them with fresh search results or interact with page controls.
7. Stop and report when credentials, consent, EEO/screening questions, CAPTCHA, unclear evidence, or a final application action is encountered.

## Local commands for agents

```sh
# Score/refresh a review queue from an explicit visible-data payload.
python job_profession/scripts/discover.py --output-dir job_profession/data

# Export current, evidence-based recommendations.
python job_profession/scripts/discover.py \
  --output-dir job_profession/data \
  --export-current-recommendations job_profession/output/Current_Profile_Recommended_Queue.csv

# Validate and run the bounded review-only tab watcher.
python skills/easy-apply-tab-monitor/scripts/chrome_tab_watcher.py \
  --manifest job_profession/private/easy_apply_tab_monitor.json --watch
```

The watcher requires the candidate’s visible, already-signed-in Chrome session and macOS automation permission. It lists URLs and opens only missing LinkedIn or Indeed listing URLs. It has no form control, upload, or submit capability.

## Repository map

```text
job_profession/src/job_profession/  deterministic matching, provenance, tracking
job_profession/config/              approved scoring and discovery policies
job_profession/scripts/             offline discovery/export and opt-in scheduling
skills/easy-apply-tab-monitor/      bounded review-only Chrome watcher
tests/                              matcher, policy, tracker, scheduler, watcher tests
job_profession/private.example/     redacted local-manifest templates
```

`job_profession` remains the stable internal Python package name. The public repository and distribution name are **smart-jobapply-agent**.

## Required verification before a commit or PR

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

CI enforces the test suite, Ruff, watcher branch/line coverage at 100%, Python compilation, and shell linting. For changes affecting matching, add adversarial fixtures for unsupported skills, seniority, ambiguity, duplicate listings, and missing source evidence. For any safety change, add a regression test proving that no application-form action is possible.

## LLM response requirements

When reporting recommendations, provide the exact listing URL, evidence-backed fit rationale, gaps, certainty level, and whether the tab is open/reopened. Never call a role “100%,” “perfect,” or guaranteed merely from a score. Keep claims short, factual, and challengeable by the candidate.

## Public repository policy

This is public source code; it must contain no candidate documents or runtime data. Do not force-add ignored paths. Report security issues privately with a minimal reproduction; see `SECURITY.md`.
