# Changelog

## Unreleased — 2026-09-08

Release disposition: next minor release (0.2.0); no tag is cut on `develop`.

- Added the bounded existing-session Chrome assistant host loop, which performs
  finite Smart Queue ticks and uses a public-query-only provider when an
  admitted inventory is short.
- Added the optional public provider and URL-only system Chrome bridge. The
  bridge retains a private per-target binding so a supported same-job redirect
  can retain the approved listing identity without changing core
  canonicalization or candidate-memory suppression.
- Added automated host-loop, provider, bridge redirect-identity, and synthetic
  browser-session coverage; the authoritative verification gate includes the
  new Node bridge contract.
- Kept the core package browser-free. The runtime remains listing-only for
  LinkedIn and Indeed, never launches or controls an application flow, and
  leaves every application action and outcome confirmation with the candidate.

## 0.1.0 — 2026-09-01

- Packaged the local mid-level job-discovery workflow as a standalone Python
  project.
- Added reproducible packaging metadata, GitHub CI, license, security policy,
  contribution guidance, and repository-level ignore rules.
- Included the review-safe Smart Test Pipeline and stow skills from
  `firstmate-heaven`'s `smart-test-pipeline` branch.
