> **ARCHIVED 2026-08-27 — the demo happened and it landed.** Written before the
> HR/CIO session; the product was presented, both approved, and four users now
> have it on a dedicated box. Two of its cautions are also out of date: the
> ranking-metrics explainer was rewritten twice and *is* circulatable, and the
> "synthetic candidates only" rule was for the demo, not for the pilot that
> followed. Kept as the record of how the product was framed to HR — the
> "decision-support, not automated decision" framing is still the right one.

---

# HR Demonstration Script

**Suggested duration:** 12–15 minutes  
**Audience:** Recruiters, hiring managers, HR leadership, privacy/legal, and auditors  
**Goal:** Demonstrate the workflow and obtain policy approval—not imply production approval.

## Demo preparation

- Use synthetic candidates only.
- Prepare one realistic job and 6–8 resumes: a strong match, a missing must-have, a nontraditional career path, an apparent keyword-stuffer, two close candidates, and one controlled parse failure/degraded case.
- Add cover letters to only two candidates so the motivation effect is visible.
- Pre-generate a fallback shortlist in case the local model is slow.
- Keep blind review enabled and use a recruiter account. Have a hiring-manager account available to show assignment scope.
- Do not circulate the existing HTML ranking explainer as approved policy; it is marked draft and contains stale access/audit statements.

## Opening: 0:00–0:45

**Say:**

> This is a decision-support system, not an automated hiring decision. It organizes a candidate pool, applies an explicit scoring policy, and shows evidence from each candidate’s own documents. HR and the hiring team remain accountable for reviewing the evidence and making the decision.

> The demo uses synthetic data. Production use would require approval of the scoring policy, access model, retention rules, and fairness validation we will flag today.

## 1. Sign in and access scope: 0:45–1:30

1. Sign in through CAS.
2. Point out the user and role in the header.
3. Briefly show that a hiring manager lands on assigned requisitions, while recruiter/admin roles have broader workflow responsibilities.

**Explain:** new users receive no operational role until an administrator grants one. Sensitive actions and reads are intended to be attributable.

**Flag for approval:** the exact role/action matrix, auditor scope, emergency access, and deprovisioning owner.

## 2. Create and validate a requisition: 1:30–3:00

1. Create a job manually or upload a JD to extract its text.
2. Keep **Blind review** checked.
3. Select a visible shortlist percentage.
4. Open the draft job and review parsed requirements before changing the job to Open.

**Say:**

> The quality of the ranking begins with the job description. HR must validate required skills, minimum experience, education requirements, and must-have labels before candidates are compared. The model’s extraction is a draft interpretation, not policy approval.

**Flag for approval:** who signs off the extracted requirement set, whether inclusive-language review is mandatory, and whether job-family-specific scoring profiles are needed.

## 3. Upload candidates and show lifecycle controls: 3:00–4:30

1. Upload the curated resumes, optionally as a ZIP, with two cover letters.
2. Show the required consent acknowledgement.
3. Watch the resume status area: uploaded, parsing, parsed, degraded, failed, or withdrawn.
4. Open one resume detail page without revealing identity.

**Explain:** original files are stored locally; identified PII is encrypted in PostgreSQL; identity is removed from embeddings and the graph projection; degraded or withdrawn resumes are excluded from ranking.

**Be precise:** inference may run on an approved separate self-hosted GPU host. Do not say “data never leaves this machine” unless the deployment actually uses same-machine Ollama.

**Flag for approval:** consent wording/versioning, retention period, candidate withdrawal versus irreversible erasure, legal holds, and the approved inference boundary.

## 4. Generate the shortlist: 4:30–6:00

1. Click Generate shortlist.
2. While the async job runs, explain the four stages below.
3. If the model is unavailable, show or describe the “Waiting for AI” state.

**Say:**

> Forward ranking fails closed. If the model is unavailable or returns invalid evidence, the application does not quietly turn that failure into low candidate scores. It waits for a healthy run instead.

### Core evaluation process

1. **Recall:** vector search retrieves up to 50 job-scoped candidates for deeper review.
2. **Structured evaluation:** 40% skills, 25% experience, 10% education, 15% seniority/title similarity, and 10% vector similarity.
3. **Evidence evaluation:** for the top structured candidates, the model proposes requirement evidence. Every quote is checked against the cited resume text; unsupported quotes are removed or downgraded.
4. **Final combination:** 60% structured score, 30% evidence completeness, and 10% cover-letter motivation.

**State clearly:** scores are ordering signals within this requisition, not probabilities, pass/fail results, or values comparable across jobs.

## 5. Review blind candidate cards: 6:00–8:00

1. Compare the first two anonymized cards.
2. Show final score, component scores, skill contributions, evidence statuses, verified quotes, and source context.
3. Compare the strong candidate with the missing-must-have candidate.
4. Point out that absent evidence is visible rather than filled with a plausible narrative.

**Say:**

> Blind review aims to make the first comparison about job-related evidence. It does not prove the system is bias-free: career history, education, location, and scoring policy can still act as proxies. That is why validation and human review remain necessary.

## 6. Open “Why this rank?”: 8:00–9:30

1. Open a shortlist entry.
2. Walk through the exact arithmetic: each sub-score, generation-time weight, and contribution.
3. Show the evidence rows and source context.
4. Point out honest states such as “not recorded” and “weights unavailable.”

**Explain:** the explanation uses the weights recorded when the shortlist was generated, not today’s settings. This preserves historical meaning after policy changes.

## 7. Reveal, withdrawal, and export: 9:30–11:00

1. Reveal one candidate only after the blind review discussion.
2. Point out that reveal is an explicit, audited action.
3. Demonstrate withdrawal/reinstatement on synthetic data and explain graph exclusion.
4. Export anonymized evidence CSV or JSON.

**Say:**

> Reveal and withdrawal are accountable actions. The current withdrawal is reversible; a legally required irreversible purge is a separate lifecycle capability that still needs policy and implementation.

## 8. Close and request decisions: 11:00–15:00

### Blocking decisions before real hiring use

- Confirm advisory-only use, required human review, and how overrides/final rationale will be recorded.
- Resolve the top-15 evidence cliff; candidates must not receive materially different headline-score treatment solely because of compute cutoff.
- Approve must-have behavior: penalty, hard exclusion, or human escalation.
- Review recency and overqualification rules for proxy/disparate-impact risk.
- Decide whether an optional cover letter may affect 10% of the result.
- Approve education-field handling, missing-field behavior, ontology partial credit, and missing skill-duration treatment.
- Approve retention, consent notice/version, withdrawal, erasure, audit retention, backup deletion, and legal hold.
- Approve the role matrix, assignment rules, reveal permissions, blind-toggle controls, and audit review process.
- Fix the shared recruiter-key/human-role authorization composition before a pilot.

### Configuration decisions

- Component weights by job family.
- Recall size, evidence pool size, and shortlist percentage.
- Evidence fuzzy threshold, minimum quote length, and confidence semantics.
- Retry ceiling, outage behavior, and manual fallback.
- Whether reverse match may return partial results.
- Whether inference on a separate self-hosted host meets institutional residency policy.

### Pilot evidence HR should require

- Multiple representative job families and document formats.
- Two independent HR reviewers plus adjudication for gold labels.
- False-negative review of candidates below the cutoff.
- Subgroup/adverse-impact analysis approved by privacy/legal.
- Parse failure and degraded-parse rates, including OCR/noisy documents.
- Evidence precision and citation-verification accuracy.
- Comparison with human inter-rater agreement, not only top-k accuracy.
- A model/prompt/config change gate and periodic drift review.

## Suggested Q&A responses

**“Does the AI decide who we hire?”**  
No. It ranks and organizes job-related evidence. Humans must review, override where appropriate, and own the final decision.

**“Is a score of 80% an 80% chance of success?”**  
No. It is a within-job composite ordering signal based on configured policy.

**“Can it hallucinate evidence?”**  
The model can propose text, but the system independently checks every quote against cited source chunks and removes unsupported evidence. This reduces, but does not eliminate, extraction or interpretation error.

**“Is the process bias-free because names are hidden?”**  
No. Blind review reduces direct identity influence, but education, career timing, location, language, and policy weights can still create proxy effects.

**“What happens during an AI outage?”**  
Forward ranking fails closed and does not persist a silently degraded shortlist. Reverse match currently has different partial-failure behavior and needs HR approval.

**“Can candidate data be deleted?”**  
Withdrawal is implemented, but comprehensive retention enforcement and irreversible erasure across all stores and backups remain enhancement work.

**“Can we defend why someone ranked first?”**  
The system records component scores, exact generation-time weights, model/prompt provenance, and verified evidence. A complete human decision/override record is not yet implemented.

