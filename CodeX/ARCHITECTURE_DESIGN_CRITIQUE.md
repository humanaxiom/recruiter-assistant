# Recruiter Assistant: Architecture and Design Critique

**Review date:** 2026-08-07  
**Scope:** Read-only analysis of the current repository. Existing files were not changed.  
**Audience:** Engineering, HR, privacy, security, and product owners.

## Executive assessment

The system is a thoughtfully engineered, offline-first recruiting decision-support application. Its strongest qualities are privacy-by-construction, blind review, independently verified evidence, explicit audit actions, fail-closed forward ranking, and an unusually deep automated test suite. It is substantially more mature than a proof of concept.

It is not yet ready to be treated as a production hiring control. The most urgent issue is authorization composition: browser calls use a shared recruiter API key while a separate CAS session represents the human. The backend does not consistently intersect those two privilege sets, so a lower-privileged authenticated human may exercise recruiter-authorized writes through the frontend proxy. The most important ranking-design concern is the evidence cutoff: only the top 15 structured candidates receive evidence and motivation evaluation, while the remaining recalled candidates receive zero for 40% of the headline score. Both issues should block a real HR pilot until resolved or explicitly constrained.

Other high-value work includes production deployment hardening, enforceable retention and erasure, versioned migrations, graph-projection freshness controls, operational telemetry, a broader HR-labelled evaluation program, and a maintained source of truth for current behavior.

## System purpose and boundaries

The application accepts job descriptions, resumes, and optional cover letters; extracts structured information locally; creates a job-scoped candidate pool; ranks candidates; presents anonymized evidence; supports audited identity reveal; and exports blind results. It is a ranking and review aid, not a system of record for final hiring decisions and not an autonomous decision maker.

The current stack is:

| Layer | Component | Responsibility |
|---|---|---|
| Browser experience | Flask, Jinja, HTMX | Server-rendered workflow and upload proxy |
| Application API | FastAPI | Authorization, validation, CRUD, exports, audit, task enqueueing |
| Async processing | arq worker | Parsing, LLM calls, graph projection, ranking, persistence |
| System of record | PostgreSQL | Jobs, resumes, encrypted PII, results, users, assignments, audit, outbox |
| Search projection | Neo4j | Skill graph, chunks, embeddings, vector recall |
| Queue/cache | Redis | arq broker and embedding cache |
| Original documents | Filesystem BlobStore | Raw resume and cover-letter files |
| Inference | Ollama-compatible endpoint | Generation and embeddings |

PostgreSQL is correctly treated as authoritative; Neo4j is a derived projection. However, that distinction is not yet backed by an explicit projection-version/freshness contract.

## End-to-end data flow

1. A recruiter creates a job manually or imports JD files. FastAPI writes the job and queues parsing.
2. The worker extracts and structures the JD, creates embeddings, and emits an outbox event.
3. A recruiter uploads resumes only after acknowledging consent. Raw files are stored in the BlobStore and metadata is written to PostgreSQL.
4. The worker applies file-size/decompression limits, extracts text, obtains structured LLM output, encrypts identified PII in PostgreSQL, scrubs embedding inputs, and emits a projection event.
5. The outbox drainer projects jobs, resumes, skills, chunks, and vectors to Neo4j.
6. Ranking performs job-scoped vector recall, structured scoring, LLM evidence extraction, quote verification, score combination, and persistence.
7. Read paths redact on the server before DTO construction. The UI displays candidate pseudonyms, component scores, and verified evidence.
8. Identity reveal is an explicit POST action and is audited before decryption. Withdrawal is separately audited and removes the candidate from the graph projection.

## Ranking and evaluation design

### Stage 1: coarse recall

Neo4j vector search recalls up to 50 resumes scoped to the job, querying at three times the requested count before filtering. This is computationally efficient, but it means a qualified resume missed by the embedding search never receives structured evaluation.

### Stage 2: structured score

The structured score is:

`40% skill + 25% experience + 10% education + 15% seniority/title similarity + 10% normalized vector similarity`

Notable policy choices include ontology-family partial credit, recency weighting, a must-have-miss penalty, overqualification dampening, education level/field rules, and title-embedding similarity. These are not neutral implementation details; they are hiring policy encoded as decimals.

### Stage 3: evidence and motivation

Only the top 15 structured candidates receive LLM evidence analysis. Requirement quotes are fuzzy-matched against cited resume chunks at a default 0.85 threshold and invalid quotes are blanked/downgraded. Cover-letter evidence can contribute motivation.

The evidence verification design is excellent anti-fabrication engineering. The cutoff design is not: candidate 16 and below receive no evidence or motivation score, creating a hard discontinuity for 40% of the final score.

### Stage 4: combination and ranking

The final score is:

`60% structured + 30% evidence completeness + 10% motivation`

Scores are within-requisition ordering signals. They are not probabilities, qualification certifications, pass/fail decisions, or comparable across jobs. Vector scores are batch-normalized, so the strongest member of a weak pool can receive the maximum normalized vector component.

Forward shortlist generation fails closed if required LLM output is unavailable or invalid. Reverse match uses per-job failure isolation and may return partial results. HR should decide whether this asymmetry is acceptable.

## What is designed well

- PII protection is layered: encrypted database fields, scrubbed embedding inputs, server-side redaction, blind exports, filesystem path controls, and explicit reveal.
- Redaction occurs before response DTO construction rather than relying on templates.
- Evidence is treated as a claim requiring independent verification against candidate-authored text.
- Historical ranking provenance records models, prompts, weights, git SHA, time, and timings.
- Forward ranking fails closed instead of silently treating model failure as candidate weakness.
- Withdrawal is modeled separately from processing status and handles parse/projection races.
- Hiring-manager job scoping and auditor read logging are present in current code.
- Input safety handles file size, PDF page count, archive expansion, path traversal, null bytes, Windows paths, and symlink escape.
- Strict typing, linting, coverage, integration tests, and adversarial ranking fixtures create a strong regression harness.

## Findings and recommendations

### Critical: human and service authorization are not safely composed

The Flask proxy attaches a shared recruiter API key to browser-originated backend calls. Backend write permissions are generally checked against that key-derived role, while the CAS session check establishes that a human has an assigned role but does not consistently require that human role to be allowed for the requested operation. A hiring manager or auditor may therefore inherit recruiter write authority through the proxy.

**Recommendation:** establish one authoritative principal for browser requests. Authorize human traffic using the verified CAS identity, role, and job assignment. Use separate service credentials for worker/automation traffic. If both credentials are present, authorize using the intersection of permissions, never the more privileged identity.

### Critical: evidence cutoff changes the meaning of the final score

Only 15 candidates receive the evidence and motivation components. Candidates below that boundary receive zero for 40% of the displayed score because of compute placement, not evidence quality.

**Recommendation:** either evaluate evidence for every displayed/retained candidate, or use a two-pass design with clearly separate scores and labels. Never compare an evidence-enriched score with a structured-only score under one headline metric.

### High: default runtime is a privileged development environment

Base Compose disables CAS unless an override is used, omits configured role keys, runs reload/debug modes, publishes data-service ports, uses static database credentials, leaves Redis unauthenticated, and uses a fixed development Flask secret.

**Recommendation:** separate dev and deployment profiles. Deployment must fail startup unless CAS, secure cookies, trusted hosts, TLS termination, unique secrets, and explicit role policy are configured. Publish only the intended ingress and isolate the data tier.

### High: the inference privacy boundary is ambiguous

Documentation says candidate data never leaves the machine, while environment guidance supports a separate GPU host over Tailscale. The configurable inference URL has no approved-host allowlist.

**Recommendation:** replace the claim with an accurate “approved self-hosted inference boundary,” document every network hop, have privacy/HR approve the GPU host, and enforce an endpoint allowlist at startup.

### High: retention and erasure are not enforced

`retention_days` is stored but no lifecycle worker enforces it. Withdrawal is reversible and does not perform consent-revocation erasure. Raw source files are protected by filesystem permissions but not application-level encryption.

**Recommendation:** implement an auditable lifecycle covering PostgreSQL, Neo4j, Redis/cache, blobs, exports, audit retention, and backups. Define legal hold and irreversible erasure semantics with HR/privacy. Encrypt raw blobs with versioned keys and a rotation process.

### High: graph freshness can silently affect who is considered

The outbox makes PostgreSQL-to-Neo4j delivery reliable, but ranking has no explicit requirement that every eligible resume be projected at a known version. A delayed or dead-lettered event can remove a candidate from coarse recall.

**Recommendation:** expose projection state, require a specific projection version before ranking, record it in ranking provenance, alert on outbox age/dead letters, and provide safe replay/rebuild tools.

### High: no versioned migration or rollback discipline

API and worker run idempotent DDL at startup. This works for additive early development but cannot safely represent ordered transformations, mixed versions, rollback, or many constraint/enum changes.

**Recommendation:** add versioned SQL migrations and a separate deployment migration step, plus expand-contract rules and Neo4j re-projection/version procedures.

### High: model validation is strong as regression testing, narrow as hiring evidence

The fixed synthetic corpus and mutation-style checks are excellent for detecting algorithm regressions. They do not establish performance or fairness across job families, document formats, languages, career paths, protected groups, or live model versions. Live-model evaluation is manual rather than a promotion gate.

**Recommendation:** create representative, permitted, HR-labelled datasets across job families; use two reviewers and adjudication; measure false negatives below the cutoff, subgroup outcomes, parse degradation, evidence accuracy, and inter-rater agreement; run scheduled live-model evaluation with immutable model/prompt/config provenance.

### Medium: operational controls are incomplete

`/health` reports only process liveness. There is no comprehensive metrics/tracing layer, correlation ID, queue/dead-letter dashboard, dependency readiness, backup/restore procedure, RPO/RTO, key escrow, or recovery drill.

**Recommendation:** add liveness/readiness separation, structured logs, metrics, alerts, backup automation, key recovery, and tested restoration. Monitor queue age, outbox lag, model latency/errors, circuit state, failed/degraded parses, ranking retries, and audit anomalies.

### Medium: human decision capture is intentionally absent

The application ranks and exports but does not capture shortlist accept/reject decisions, overrides, rationale, approvals, interview progression, or a final decision record.

**Recommendation:** add a human decision layer that never changes the evidence score, records overrides and reasons, timestamps approvals, and exports a defensible decision packet.

### Medium: policy semantics need explicit HR ownership

Must-have requirements are penalized rather than hard exclusions; nice-to-have skills do not affect the structured score; cover letters contribute 10%; recency, overqualification, education field, unknown duration, ontology similarity, and foreign-location redaction are policy choices with fairness implications.

**Recommendation:** expose these in a versioned policy register owned by HR, privacy, and legal. Require ratification and change control by job family.

### Medium: security hardening opportunities

- Most state-changing Flask routes do not use the one-shot CSRF pattern used for reveal/withdraw.
- PII key rotation lacks ciphertext key versioning and a re-encryption workflow.
- Candidate email lookup uses an unsalted deterministic hash.
- Audit immutability is an application convention rather than a database-enforced rule.
- Raw asyncpg and large services make uniform authorization/audit application harder to maintain.

## Documentation assessment

The ADR collection is detailed and valuable, but operational/current-state documentation is fragmented:

- `DEVELOPER_GUIDE.md` retains template-era credentials, routes, components, and ports.
- `HANDOFF.md` is a long chronological record with obsolete states rather than a concise source of truth.
- `README.md` mixes current design with phase-history language.
- The HR ranking explainer is marked draft/not for circulation and contains obsolete claims about assignment scoping, reveal attribution, auditor capabilities, and blind-toggle audit.

Do not use the current explainer as an approved HR artifact without reconciliation. Create a short current-state runbook, archive historical state, and assign owners/review dates to policy documents.

## Recommended target architecture

1. Browser traffic reaches a hardened BFF/API boundary authenticated by CAS.
2. Human authorization uses CAS user role plus job assignment; service credentials are separate principal types.
3. PostgreSQL remains the authoritative ledger with versioned migrations.
4. Outbox events carry aggregate/projection versions; Neo4j is visible, rebuildable, and freshness-gated.
5. An inference gateway allowlists self-hosted endpoints, pins model versions, applies request limits, and emits PII-safe telemetry.
6. Ranking runs only when all eligible candidates are parsed and projected at the required version.
7. A policy registry stores versioned, ratified scoring configurations by job family.
8. A retention service deletes data consistently across stores and records completion evidence.
9. A decision workflow records human judgment separately from algorithmic evidence.
10. Production deployment uses immutable non-root images, private data services, managed secrets, TLS, readiness checks, observability, backups, and rollback.

## HR/privacy/security decisions required

- Confirm the system is advisory only and define mandatory human review and override documentation.
- Approve the role/action matrix and require real human identity for reveals and sensitive writes.
- Decide whether a separate Tailscale inference host meets the institution’s privacy/residency definition.
- Decide whether evidence should be computed for all candidates and how shortlist cutoffs may be used.
- Ratify must-have, cover-letter, recency, overqualification, education, ontology, and missing-data rules.
- Define retention, withdrawal, erasure, legal hold, audit retention, and backup deletion requirements.
- Decide whether auditors have global logged visibility or job-scoped visibility.
- Decide whether partial reverse-match results during model failure are permissible.
- Approve the evaluation corpus, fairness methodology, false-negative tolerance, and model-change review cadence.
- Define the evidence retained when a final human decision differs from the ranking.

## Evidence map

- Architecture and workflow: `README.md`, `docker-compose.yml`, `core/src/api/main.py`, `core/src/worker/main.py`
- Ranking: `core/src/pipeline/matching/orchestrator.py`, `stages.py`, `core/src/schemas/matching.py`
- Authorization: `core/src/api/deps.py`, route modules, `core/frontend/api_client.py`, `core/frontend/app.py`
- Privacy: `core/src/services/redaction.py`, `pii.py`, `reveal_service.py`, `core/src/storage/blob_store.py`
- Reliability: `core/src/worker/graph_tasks.py`, `matching_tasks.py`, `core/src/settings.py`
- Quality: `.github/workflows/ci.yml`, `Makefile`, `scripts/verify.sh`, `core/tests/evals/`
- Policy/history: `docs/adr/`, `docs/process/ranking-metrics-explainer.html`

