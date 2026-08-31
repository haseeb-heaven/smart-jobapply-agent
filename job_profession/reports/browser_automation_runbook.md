# Reliable Browser Automation Runbook

## Chosen routing

1. **LinkedIn primary:** the connected Chrome session. It uses the candidate's existing Chrome login and supports controlled Playwright inspection.
2. **Browser Use fallback:** Browser Use cloud only when a persistent Browser Use profile has LinkedIn login state *and* its account can create a session.
3. **Indeed primary:** the connected Chrome session. Browser Use currently has no Indeed login-ready profile.

## Secret handling

`OAuthENV.txt` is a mixed notes-and-credentials file, not a dotenv file. Never source it in a shell and never print it. Use:

```sh
python3 job_profession/scripts/browser_use_healthcheck.py --key-index 1 --profile-readiness
```

The loader extracts only values from the `Browser Use:` section, keeps the selected key in memory, and reports only a one-way fingerprint plus aggregate readiness. It verifies TLS using `certifi`; TLS verification is never disabled.

## Cloud readiness evidence

- One configured Browser Use key is authenticated and has a persistent LinkedIn-ready profile.
- A bounded read-only cloud session attempt returned HTTP 402. The account needs Browser Use credit/billing before it can create a session.
- A second authenticated Browser Use account has no persistent LinkedIn or Indeed profile.

Therefore, the current reliable live path is connected Chrome. Browser Use becomes a fallback after the account with the LinkedIn profile has credit, or after the other account is funded and a user signs into LinkedIn once in its dedicated persistent profile.

## Read-only Browser Use smoke test

After credit is available, run the bounded discovery test:

```sh
python3 job_profession/scripts/browser_use_linkedin_session.py --start-read-only --max-cost-usd 0.50
```

It selects only a Browser Use profile whose metadata reports a LinkedIn cookie domain. Its cloud task is hard-limited to discovery: no Apply/Easy Apply click, save, message, upload, CAPTCHA, or settings change. Runtime data stays in `job_profession/private/` with mode `0600`.

## No-submit validation evidence

- The installed Codex Job Apply plugin passed its local synthetic verification,
  including the review-only and final-action refusal boundaries.
- The local scheduler wrapper was executed on 2026-08-31. It generated a
  discovery-only record with `application_actions: 0` and declared
  `network_access: none_by_scheduler`; the CSV tracker export also completed.
- Browser Use readiness was rechecked on 2026-08-31: the selected account
  authenticated successfully and reported two persistent profiles, with
  LinkedIn login-ready and Indeed not login-ready. This check sent no candidate
  information and did not create a cloud browser session.
- The currently connected Chrome session exposed one Google tab. A direct,
  read-only LinkedIn search navigation was denied by Chrome's browser security
  policy before the site loaded. Do not work around that denial; a Chrome-side
  live test requires the policy to permit LinkedIn navigation or a user-provided
  LinkedIn tab to be available for inspection.
- In an earlier no-submit Chrome check, Indeed loaded a live Python backend search
  results page and LinkedIn showed an unauthenticated job page. The current
  browser-security decision above is the controlling result for any new Chrome
  navigation.
- The Browser Use credential loader now has a bounded credential-file read timeout
  (`--file-read-timeout`, default 10 seconds). A cloud-backed credential file that
  stalls produces a redacted `TimeoutError` result instead of hanging the workflow.
- `pytest tests -q` passed after this reliability change. These checks are local
  or read-only; they did not create an application, upload a resume, or transmit
  candidate data.

## Live job application safety

- Search and screen listings first; only retain 85%+ direct profile-fit roles.
- Never claim seniority, architecture ownership, or professional production AI leadership.
- Before any form fill/upload/submit, obtain action-time confirmation naming the precise platform, employer, and personal data to be transmitted.
- Stop for any salary, work-authorisation, legal, EEO, photo/PAN, or other unapproved question.
