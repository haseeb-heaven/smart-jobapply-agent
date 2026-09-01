---
name: security-and-hardening
description: Use when software code handles authentication, authorization, user input, file uploads, sensitive data, payments, webhooks, external integrations, multi-tenant access, or production security review.
---

# Security and Hardening

## Smart JobApply Agent boundary

Treat candidate profiles, resumes, email, job-board sessions, browser state,
API keys, screening answers, compensation, and eligibility as sensitive. This
skill reviews code and trust boundaries only; `SECURITY.md`, `AGENTS.md`, and
the job-board monitor skill remain authoritative for prohibited actions.

> Upstream: `zineyu/skills` — `skills/engineering/security-and-hardening/SKILL.md`

Review software trust boundaries and harden applications against realistic abuse. Prioritize exploitable defects over generic checklist output.

**Software-development scope only:** use for application source code, APIs, infrastructure and deployment configuration, databases, tests, dependencies, CI/CD, and runtime security. Do not use for physical security, personal safety, policy writing, generic compliance prose, resumes, documents, or non-code content.

## Threat model first

Identify:

- protected assets and sensitive data;
- attackers and untrusted inputs;
- authentication and authorization boundaries;
- tenant ownership rules;
- external services, files, queues, webhooks, and callbacks;
- operations capable of data loss, privilege escalation, or financial impact.

## Required checks

### Authentication and sessions

- Secure password/token handling and expiry.
- Session rotation, revocation, logout, and cookie attributes.
- Brute-force and credential-stuffing controls.
- No credentials, access tokens, or reset links in logs.

### Authorization and tenant isolation

- Enforce authorization server-side for every object operation.
- Derive ownership from the authenticated identity, not request fields.
- Test cross-user access to reads, updates, deletes, files, exports, and background jobs.
- Prevent insecure direct object references and role escalation.

### Input and output

- Validate type, shape, size, ranges, and allowed values at boundaries.
- Use parameterized database access.
- Encode output for its destination and avoid unsafe HTML execution.
- Restrict redirects, URLs, paths, and network destinations.
- Use explicit allowlists where practical.

### File uploads and parsing

- Enforce size, count, extension, MIME, and content-signature policies.
- Generate server-side filenames and prevent path traversal.
- Store uploads outside executable/public paths unless intentionally published.
- Treat parsers as untrusted; apply time, memory, page-count, archive, and decompression limits.
- Scan or quarantine files where the risk model requires it.
- Never expose another user's object-storage key or signed URL.

### Data privacy

- Minimize collection and retention of personal data.
- Encrypt sensitive data in transit and at rest where appropriate.
- Redact PII, resumes, tokens, and secrets from logs, traces, analytics, and error reports.
- Verify deletion and export flows include derived data, files, caches, and indexes.

### APIs, webhooks, and payments

- Verify signatures before processing webhooks.
- Apply replay protection and idempotency.
- Use strict timeouts, bounded retries, and safe failure handling for external calls.
- Never trust prices, plans, roles, or entitlement state supplied by the client.

### Operations and dependencies

- Keep secrets out of source control and build artifacts.
- Run dependency, static-analysis, and secret scans.
- Use least-privilege credentials and scoped integration permissions.
- Review security headers, CORS, rate limits, error disclosure, and production debug settings.

## Evidence

For every finding include severity, affected asset, attack path, file/line, proof, required correction, and a regression/security test.

Classify as:

- **Critical:** immediately exploitable compromise, cross-tenant breach, secret exposure, payment/auth bypass, or destructive action;
- **High:** realistic vulnerability with material impact;
- **Medium:** defense weakness requiring pre-release correction;
- **Low:** hardening improvement with limited immediate risk.

Do not declare an application secure. State what was tested, what evidence was collected, and what remains unverified.
