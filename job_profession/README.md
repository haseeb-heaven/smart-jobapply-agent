# Job profession review workflow

This folder supports a private, review-gated job-search workflow for mid-level,
implementation-focused backend/software roles. It is not an application bot: it
must never submit an application, accept an attestation, or guess an answer.

## Candidate and evidence boundaries

Use only the facts in `private/candidate_profile.yaml` and the reusable wording
in `private/application_answers.yaml`. A value is eligible for pre-fill only
when its status is `approved`; every `needs_confirmation` or
`do_not_use_in_auto_fill` value stops pre-fill and becomes a candidate question.

The candidate is positioned for maintenance, scoped features, APIs, background
jobs, databases, tests, and integrations. Exclude senior, staff, principal,
lead, architect, manager, head, and director roles. Do not claim architecture
ownership, people management, production AI/ML leadership, or unsupported
experience. GitHub AI work is always `personal_open_source`, never employer
production-AI evidence.

## Manual tracker procedure

1. Open **Recommended Jobs**. Its primary queue contains only evidence-based
   profile-fit jobs scored at **85% or higher**. Lower-fit jobs remain excluded
   or rejected by default; they are not a primary application queue.
2. Treat the fit score as a transparent matching aid, not a promise of an
   interview, offer, compensation, eligibility, or job availability. Read the
   score explanation and gaps before acting.
3. Confirm the title is mid-level and implementation-focused. Reject any role
   that requires seniority, architecture/strategy ownership, people management,
   production GenAI leadership, or other unsupported requirements.
4. Select a truthful resume and cover letter from `private/documents_manifest.yaml`.
   Check every claim and revise job-specific wording from the actual listing.
5. Before applying, manually verify salary, current location, work authorization,
   sponsorship, availability/notice period, and every mandatory screening or EEO
   question. Do not use an answer that would require guessing.
6. Apply manually in the normal site flow. The candidate performs the final
   **Submit** click after reviewing all consent statements and attachments.
7. In **Applications**, record `Submitted`, the timestamp, source URL, materials
   used, and a follow-up date. Keep existing tracker history; do not overwrite or
   silently delete records.

## Private-data handling

`private/` contains supplied contact details, compensation, eligibility, and
answer material. It is ignored by Git. Keep directory permissions limited to the
local user account (for example, `chmod 700 job_profession/private`) and do not
copy its contents into issues, commits, exported workbooks, or shared logs.

## Manual review checklist

Before moving a job to ready-to-apply, confirm all of the following:

- The job has an evidence-based score of at least 85% and no hard exclusion.
- Company, title, source URL, location, and deadline are correct.
- Every resume and cover-letter claim is truthful and sourced.
- Personal/open-source AI work is labelled accurately.
- Compensation, authorization, sponsorship, location, notice period, and all
  screening/EEO answers have been answered by the candidate where required.
- The candidate, not software, will submit the final application.
