# High-Value Enhancement Plan

The seven initiatives below are ordered by risk reduction and value to an HR pilot. Security, policy, and lifecycle work comes before “wow” features.

## 1. Unify identity and authorization

**Outcome:** every action is authorized against the real acting principal, with no privilege inherited from a shared proxy credential.

**Work:**

- Make CAS identity, role, and job assignment authoritative for browser traffic.
- Separate human, worker, and automation principal types.
- Remove the shared recruiter key from browser-proxy authorization or enforce permission intersection.
- Add a route/action/role matrix with negative integration and browser tests.
- Require attributed sessions for reveal, exports, ranking generation, blind-mode changes, and candidate lifecycle actions.
- Harden CSRF/origin protection for all state-changing browser routes.

**Acceptance:** a hiring manager and auditor cannot perform recruiter/admin writes even when traffic passes through Flask; all sensitive actions identify the human actor.

**Owners/approval:** Security, identity team, HR operations, privacy.

## 2. Correct the ranking cutoff and establish policy governance

**Outcome:** displayed scores are comparable within a requisition and every scoring rule is explicitly owned.

**Work:**

- Remove the top-15 evidence cliff by evaluating all retained candidates, or separate structured screening from evidence-enriched ranking with distinct labels.
- Measure vector-recall false negatives and adjust/bypass the 50-candidate gate for appropriate pool sizes.
- Create versioned scoring profiles by job family.
- Build a read-only policy simulation first, then a ratification workflow with owner, rationale, effective date, and audit trail.
- Present must-have, cover-letter, recency, overqualification, education, ontology, and missing-data rules in plain language.
- Ensure score displays state limitations and prohibit cross-job interpretation.

**Acceptance:** no candidate loses 40% of the headline score solely due to cutoff placement; every active policy version has recorded HR/legal approval.

**Owners/approval:** HR policy, employment counsel/privacy, data science, product.

## 3. Build a production-quality and fairness governance program

**Outcome:** model and policy changes cannot reach hiring workflows without representative evidence.

**Work:**

- Expand beyond the single-job, 20-resume regression corpus to multiple job families, formats, languages, noisy/OCR documents, and nontraditional careers.
- Obtain dual HR labels, adjudication, label guidance, and inter-rater agreement.
- Measure recall, false negatives below cutoffs, evidence accuracy, calibration/ordering stability, parse degradation, and subgroup outcomes.
- Add scheduled/on-demand live-model evaluation on an approved runner.
- Pin immutable model digest, prompt versions, code SHA, configuration, dataset version, and report artifact.
- Define change thresholds, exception approval, drift review cadence, and rollback triggers.

**Acceptance:** deployment promotion requires a signed evaluation report; fairness and false-negative reviews cover HR-approved cohorts and job families.

**Owners/approval:** HR analytics, privacy/legal, model owner, engineering.

## 4. Implement complete data lifecycle and recovery

**Outcome:** consent, retention, withdrawal, erasure, backup, and recovery are enforceable across every store.

**Work:**

- Version the consent notice and record timestamp, source, and actor.
- Enforce `retention_days` with preview, legal-hold checks, auditable execution, and reconciliation.
- Define withdrawal versus irreversible erasure semantics.
- Delete consistently from PostgreSQL, Neo4j, Redis/cache, BlobStore, generated exports, and eligible backups.
- Encrypt raw blobs with versioned keys; implement PII/blob key rotation and re-encryption.
- Add encrypted backups, key escrow/recovery, RPO/RTO, restore automation, and regular restore drills.

**Acceptance:** a synthetic candidate lifecycle can be traced from consent through retention or verified erasure, and a tested restore meets approved RPO/RTO.

**Owners/approval:** Privacy, records management, security, infrastructure, HR.

## 5. Establish a hardened production platform and observable control plane

**Outcome:** the system can be operated safely and failures are visible before they affect hiring teams.

**Work:**

- Create separate development and deployment profiles.
- Use immutable, non-root runtime images; pinned dependencies/digests; managed secrets; TLS; secure cookies; private data networks; resource/restart policies; and rollback procedures.
- Enforce an approved inference endpoint allowlist through an inference gateway.
- Add dependency readiness separate from liveness.
- Add structured logs and correlation IDs plus metrics/traces for API, queue age, outbox lag/dead letters, projection freshness, LLM latency/errors, circuit state, parse failures, and ranking retries.
- Create dashboards, alerts, SLOs, incident runbooks, and an operational audit view.
- Add supply-chain scanning, SBOM/provenance, and secret/container/dependency checks.

**Acceptance:** staging passes security review, dependency-failure drills, alert tests, rollback, and restore exercises; production mode refuses insecure startup.

**Owners/approval:** Platform, security, privacy, service owner.

## 6. Add versioned migrations and explicit projection consistency

**Outcome:** schema and graph evolution are auditable, reversible where feasible, and safe across deployments.

**Work:**

- Introduce an ordered PostgreSQL migration ledger and a single deployment migration step.
- Define expand-contract and mixed-version compatibility rules.
- Add schema-version startup guards and backup requirements for destructive changes.
- Version outbox aggregates and Neo4j projections.
- Require eligible resumes to be parsed and projected at the expected version before ranking.
- Expose replay, dead-letter repair, full re-projection, and reconciliation tooling.
- Avoid holding database transactions open across slow external graph operations where practical.

**Acceptance:** upgrades and rollbacks are rehearsed; ranking refuses stale/incomplete projections and records the projection version used.

**Owners/approval:** Data/platform engineering, application engineering.

## 7. Add human decision workflow and defensible audit artifacts

**Outcome:** the tool supports accountable human judgment beyond the ranking screen.

**Work:**

- Record accept/reject/hold decisions separately from algorithmic scores.
- Capture overrides, job-related rationale, reviewer, timestamp, and approvals without altering source evidence.
- Add interview-question support grounded only in weak/missing verified requirements.
- Produce a timestamped decision packet containing policy/model provenance, verified evidence, human actions, reveal history, and limitations.
- Provide a generalized audit-log viewer for authorized auditors, with export and anomaly review.
- Keep identity reveal distinct from merit review and preserve blind-mode chronology.

**Acceptance:** an auditor can reconstruct what the system recommended, what humans decided, why they differed, which identity was revealed when, and which policy/model version applied.

**Owners/approval:** HR operations, audit, privacy/legal, product.

## Suggested sequencing

| Horizon | Initiatives | Exit condition |
|---|---|---|
| Pilot blockers | 1, cutoff portion of 2, policy decisions, privacy boundary | Authorization proven; score cliff removed; HR limitations approved |
| Controlled pilot | 3 and 4 foundations; 5 staging baseline | Representative eval report; lifecycle policy; monitored secure staging |
| Production readiness | Complete 4, 5, and 6 | Restore/rollback/failure drills pass; retention and projection controls live |
| Operational maturity | 7 and policy studio portions of 2 | Human decisions and audits are reconstructable; policy changes governed |

## Measures of success

- Zero unauthorized actions in the role/action negative-test matrix.
- 100% of displayed candidates receive comparable score treatment.
- Measured recall and false-negative review at each ranking cutoff.
- Every model/prompt/policy release has immutable provenance and approval.
- Retention and erasure reconciliation covers every data store.
- Projection lag/dead letters and LLM failure states are observable and alerted.
- Restore and rollback meet approved RPO/RTO.
- Every final hiring decision records accountable human review and any override rationale.

