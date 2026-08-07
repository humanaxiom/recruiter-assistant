# Roadmap — HR pilot readiness before expansion

**Re-prioritized:** 2026-08-07 after validating the independent review in `CodeX/` against current code.  
**Rule:** security, score validity, HR policy, privacy lifecycle, and operability precede new “wow” features.  
**Non-negotiables:** self-hosted inference only; evidence-backed claims; blind-by-default; PII never embedded; human decision accountability.

This is the current ordered plan. It replaces the earlier instruction to choose a flagship feature next. The former feature ideas are retained at the end, not discarded.

## P0 — blockers before any HR pilot

### 1. Unify human and service authorization

**Validated finding.** Flask attaches `API_KEY_RECRUITER` to every browser-originated API call. FastAPI writer routes authorize that key with `require_role`. The CAS-side `require_role_assigned` gate blocks only `role=None`; any assigned human role passes. Consequently, a hiring-manager or auditor session can inherit recruiter-key write authority through any frontend workflow that reaches a writer route. `scoped_user_id_or_403` correctly scopes hiring-manager reads, but does not intersect permissions for writes.

**Target contract.**

- CAS user identity, assigned role, and job assignment are authoritative for browser traffic.
- Worker/automation credentials are separate service principals with explicit policies.
- If both a human session and service credential are present, effective permission is their intersection.
- A bare service key remains supported only for explicitly documented machine-to-machine routes.
- All sensitive actions retain real actor attribution; development bypass is explicit and cannot be mistaken for deployable mode.

**First implementation slice.**

1. Write an ADR and route/action/principal matrix for every API and Flask route.
2. Add negative integration tests proving hiring managers and auditors cannot perform recruiter/admin writes through Flask.
3. Add tests for missing, role-less, revoked, mismatched-role, service-only, and CAS-disabled callers.
4. Change authorization dependencies/BFF credentials according to the accepted principal contract.
5. Extend CSRF/origin protection from reveal/withdraw to all state-changing browser routes.

**Acceptance criteria.**

- No human gains authority from the BFF’s credential.
- Hiring-manager assignment scoping holds for reads and writes.
- Reveal, export, blind-mode changes, ranking generation, and candidate lifecycle actions identify the responsible human where human-initiated.
- The full route matrix is mutation/negative-test enforced.

**Approval:** security, identity owner, HR operations, privacy.

### 2. Remove the evidence-score cutoff cliff

**Validated finding.** `generate_shortlist` runs evidence only for the top `evidence_k=15` structured candidates, then passes every stage-2 candidate to `stage4_combine`. A candidate without stage-3 output receives zero evidence completeness and zero motivation. Thus candidates outside the top 15 lose 40% of the same headline score due to evaluation placement.

**Decision required from HR/product.**

- **Preferred:** evaluate evidence for every candidate eligible to appear in the retained shortlist; control cost with batching, caching, progress state, and explicit maximum pool rules.
- **Alternative:** split the workflow into a structured-screening score and an evidence-enriched finalist score. Use different names/scales and never sort or compare them as one metric.

Also measure the stage-1 recall gate (`k=50`) against realistic pool sizes. A resume absent from vector recall never receives structured scoring.

**Acceptance criteria.**

- Every value shown under one score label has the same evaluated components.
- Cutoff behavior and incomplete evaluation are visible and cannot silently influence rank.
- False-negative review measures candidates immediately below both recall and shortlist boundaries.
- Ranking evals include pools larger than 15 and 50.

**Approval:** HR policy, product, model owner, privacy/legal.

### 3. Ratify the hiring policy and permitted use

**Validated finding.** Current decimals encode policy: must-have misses are penalties rather than exclusions; optional cover letters can contribute 10%; recency, overqualification, education field/unknown values, ontology-family partial credit, missing skill duration, and batch-normalized vector similarity affect rank. Nice-to-have skills appear in evidence prompts but not the structured skill score. Scores are within-job ordering signals, not probabilities or cross-job measures.

**Plan.**

- Adopt an HR-approved statement that the system is advisory only and cannot autonomously reject candidates.
- Define mandatory human review, below-cutoff review, override reasons, and prohibited score uses.
- Ratify each scoring rule and threshold by job family, including adverse-impact caveats.
- Decide reverse-match failure semantics: partial results versus the forward path’s fail-closed behavior.
- Version every active policy with owner, rationale, approval date, effective date, and change history.
- Correct the draft HR explainer before circulation; current access/audit statements are stale.

**Acceptance criteria.** no unratified scoring profile is usable in a real requisition; UI/export language states score limitations.

> **Added 2026-08-07 by evidence-check.** Ratification must also cover **M3: every JD skill is written
> `must=True`** (`tasks.py:264`), so the `is_must_have=False` branch is dead code and the ×0.5
> `must_have_miss_penalty` fires on *any* unmatched extracted skill. Combined with the skill-vocabulary gap
> (Item 4a) this is the **dominant real-world scoring effect** and has never been reviewed by anyone. HR
> cannot ratify a policy register that omits it.

### 3a. Close the two ranking defects the review missed *(added 2026-08-07)*

Both are S1 and neither appears in the original critique. They are separated from Item 2 because Item 2's
harm is *systematic and predictable* while these two are not.

**M1 — stage 3 fails OPEN on a non-LLM exception.** `orchestrator.py:637-639` catches bare `Exception`
per candidate and sets `results[id] = None`. For a **top-15** candidate that silently zeroes 40% of
`score_final` and persists it, with no marker. Unlike the evidence cliff — which provably cannot reorder the
displayed list, because `final_i = 0.6·s_i + (≥0) ≥ 0.6·s_j` for any in-batch *i* and out-of-batch *j* — this
one **displaces real candidates inside the displayed top ranks**, and does so only when a transient
Neo4j/Postgres hiccup happens to hit that candidate. It contradicts the fail-closed posture ADR-029 claims.
*Plan:* narrow the `except`, or record an explicit non-evaluation marker and fail closed. **Gated by
`ranking-evals`.**

**M2 — stage-1 recall is a global vector query.** `resume_summary_idx` (`neo4j_bootstrap.py:105`) is not
job-partitioned, and `orchestrator.py:303-320` applies `WHERE r.job_id` *after* the index returns its global
top ~150. So the real ceiling is not "this job's 50 best" but "whichever of this job's résumés happen to land
in the global 150 nearest this JD." Past ~150 résumés corpus-wide, a job's own candidates are crowded out
**even when that job's pool is well under `coarse_k`**. *Plan:* job-partitioned recall, or a Postgres-sourced
exhaustive fallback for small pools. **Raising `coarse_k` does not fix this** and would mask it.
**Gated by `ranking-evals`.**

**M6 — the negative authorization tests give false assurance.** `test_route_jobs.py:276` and
`test_api_resumes_withdraw_pg.py:147` parametrize the **API-key** role. **No test anywhere exercises
recruiter-key + hiring_manager/auditor-session — the only combination that occurs in production.** This is
*why* a merge-blocking security gate passed Item 1. *Plan:* add the missing test axis **before** fixing
Item 1, so the fix is demonstrably Red-first. This is the highest-leverage single test in the plan.

**Acceptance criteria.** M1 and M2 each have a failing test before their fix; the M6 axis exists and fails
against today's code.

## P1 — controlled-pilot foundations

### 4a. Make skill matching work on real job descriptions *(added 2026-08-07 — highest product value in this plan)*

**This is the item that decides whether the product works at all on real postings, and the original review
did not identify it.** Measured across the live database:

| job title | hashed reqs | total | avg skill sub-score |
|---|---|---|---|
| 20251023 00101827 JDFN APSA 20260106 | 16 | 19 | 0.0033 |
| Application Administrator | 15 | 20 | 0.0375 |
| Program Director, SFU Morris J. Wosk Centre | 5 | 6 | 0.0000 |
| **Backend Data Engineer** *(corpus fixture)* | **0** | 5 | **0.6425** |
| **Senior Backend Data Engineer** *(corpus fixture)* | **0** | 6 | **0.5000** |

**Mechanism, verified.** ADR-008 hashes any skill outside the curated vocabulary. `ensure_categories`
(`skills_graph.py:358-369`) stamps categories *only* from `categories.yaml` (~19 families), and its own
docstring says "no LLM backfill in v1" — so a hashed node gets **no `categories` property**. Stage 2's
family-credit branch requires `reqSkill.categories IS NOT NULL` (`orchestrator.py:377`). Measured:
`hashed_total=288, with_categories=0` across the entire graph. So for a non-vocabulary requirement the
ontology weight is exactly `1.0` on an identical normalised string, else `0.0` — **no alias resolution, no
family partial credit, nothing in between.** Then M3 (`must=True` on every edge) compounds it: the ×0.5
must-have-miss penalty fires for nearly every candidate.

**Every real SFU posting is 47-84% outside the vocabulary. Both corpus fixture JDs are 0%.** That is exactly
why the evals gate never saw this. The `h:` hashes recently visible in the UI were the *marker*; non-vocabulary
-ness is the cause.

**Plan.** Grow `aliases.yaml`/`categories.yaml` toward the job families actually being posted; and/or enable
LLM category backfill for hashed nodes (`worker.skill_category_task`, currently deferred); and/or reinstate a
scoped fuzzy/vector path for JD↔résumé non-vocabulary skills. **All change scoring math → `ranking-evals`
gated.** Also needs an HR decision, because it changes who ranks where.

**Hard prerequisite: Item 4b.** The current corpus *cannot measure any of this.*

### 4b. Make the evals harness able to see what it grades *(added 2026-08-07)*

Sequence this **before or alongside 4a and M2** — every scoring fix above is only as trustworthy as the gate.

- **The corpus is blind to ADR-008 hashing by construction.** `run_evals.py::_skill_rows_for` reimplements
  the stage-2 Cypher in Python and keys via `_basic_normalise`, so it can **never** produce an `h:` key. The
  only five labels the whole corpus produces are `airflow`, `docker`, `postgresql`, `python`,
  `rest api design`. A prior attempt to close this was **reverted** (see ADR-032) because it inverted the
  bait-below-strong ordering while producing no hashed key at all — closing it properly needs the non-vocab
  skill in `required_skills`, which forces a must-have miss for every honest fixture and re-bands the corpus,
  so the documented margins must be **re-measured, not assumed**.
- **Unenforced assertions (M8).** `expected_rank_band` is never referenced by `run_evals.py` — and r18
  currently violates its own declared band (tagged `strong`, band `{1,9}`, actual rank 11). "The bait is
  BELOW EVERY STRONG FIXTURE" is prose in `thresholds.toml:217`, not a gated key; a change that violated it
  still exited 0. The `skill_missing_must` ordering pair is inert against `weights.skill = 0`.
- **Recommended first move**, because it needs no new fixtures and no measured constants, passes today, and
  would have caught the reverted change: add `[adversarial] must_rank_below_every_strong = true`, enforced as
  an order relation over tags. **Do not** instead enforce `expected_rank_band` wholesale — it would go red
  immediately on r18, which needs its own reconciliation (widen the band, or revisit the `strong` tag).

### 4c. The defense pack currently asserts a score it did not measure *(added 2026-08-07)*

ADR-031's thesis is *never present a number as authoritative when you don't know it*. The evidence cliff
(Item 2) breaks that, one layer above where ADR-031 looks:

`stage4_combine` calls `_evidence_completeness(None) → 0.0` and `_motivation_score(None) → 0.0`
(`stages.py:564-565,587`) — **real floats, not `None`**. `persist_shortlist` folds them into the jsonb and
coerces `evidence=None` to `{}`. On read, `_folded_subscore` returns `0.0`, `EvidenceObject.model_validate({})`
yields `requirements=[]`, and `explanation.py:180-182` therefore sets **`scores_available=True`**. So the
panel renders, affirmatively:

> Evidence · 30% · **0%** · **0.00**  Motivation · 10% · **0%** · **0.00**

— byte-identical to a candidate evaluated against every requirement and found to meet none. **ADR-031's
"not recorded" mechanism guards an *unreadable* row, not a *never-computed* one.** Reachable on any entry at
structured-rank ≥ 16 whenever `ceil(N·p/100) > 15` — and `shortlist_top_percent` defaults to **100**, so
always, on any job with more than 15 recalled candidates.

**Plan.** The honest minimum requires persisting an explicit `evidence_evaluated` marker — a **write-path
change**, therefore `ranking-evals`-gated, with a back-compat story for existing rows. **Do not** infer it
from `requirements == []` in the template: that is inferring pipeline state from a display artifact, the same
class of error. Amend ADR-031 to record that its guard covers unreadable rows only.

### 4. Production-quality and fairness governance

**Validated with qualification.** The fixed 20-resume corpus, adversarial fixtures, ordering twins, determinism, PII gates, and real-service integration tests are unusually strong regression controls. They do not establish generalization or fairness across job families, formats, languages, career paths, protected cohorts, or changing live models. The production-faithful model run remains manual.

**Plan.**

- Build permitted, representative datasets across job families, volumes, document formats, OCR/noise, languages, and nontraditional careers.
- Use two independent HR reviewers, written label guidance, adjudication, and inter-rater agreement.
- Measure recall, false negatives below cutoffs, evidence precision, citation verification, parse degradation, rank stability, and HR-approved subgroup outcomes.
- Add scheduled/on-demand live-model evaluation on an approved trusted runner.
- Record immutable model digest, prompt/config/dataset versions, code SHA, cache state, and report artifact.
- Define promotion thresholds, exception approval, drift cadence, and rollback triggers.
- Repair the known inert `skill_missing_must` corpus mutation before relying on it as proof of that dimension.

**Acceptance criteria.** every model/prompt/scoring release has a signed report and rollback decision; evaluation limitations are explicit.

### 5. Enforce consent, retention, erasure, and recovery

**Validated finding.** `retention_days` is stored but not enforced. Withdrawal is reversible and consent-revocation purge remains deferred. Raw blobs rely on filesystem permissions, while database PII encryption does not cover original documents. Backup, restore, key escrow/rotation, and cross-store erasure evidence are not defined.

**Plan.**

- Version the consent notice and record version, timestamp, source, and actor.
- Define withdrawal, consent revocation, irreversible erasure, legal hold, audit retention, and backup expiry with privacy/legal.
- Implement previewable, auditable retention across PostgreSQL, Neo4j, Redis/cache, BlobStore, exports, and eligible backups.
- Encrypt raw blobs with versioned keys; add ciphertext key IDs, rotation, and re-encryption.
- Define encrypted backups, key escrow/recovery, RPO/RTO, restore automation, and quarterly restore drills.
- Reconcile deletion results across stores and retain non-PII completion evidence.

**Acceptance criteria.** a synthetic lifecycle is traceable from consent to verified expiry/erasure; restore and key-recovery drills meet approved RPO/RTO.

### 6. Harden deployment and add an operational control plane

**Validated with qualification.** Base Compose is a development profile, so reload/debug, published ports, static credentials, insecure cookie defaults, and warnings are not defects in isolation. They become critical if that profile is treated as deployable. `/health` is shallow; no complete metrics/tracing, projection/dead-letter view, backup runbook, or production deployment profile is present.

**Plan.**

- Create distinct development and deployment profiles; production mode refuses insecure startup.
- Use immutable pinned images, non-root/minimal runtime, managed secrets, TLS, secure cookies, trusted hosts, private data networks, resource/restart policies, and rollback instructions.
- Publish only intended ingress; authenticate/segment PostgreSQL, Redis, and Neo4j.
- Replace “never leaves the machine” with the accurate approved self-hosted inference boundary. Allowlist inference endpoints and document the Tailscale GPU hop.
- Separate liveness from dependency readiness for API, frontend, worker, databases, queue, graph, and inference.
- Add structured logs, correlation IDs, metrics/traces, dashboards, and alerts for queue age, outbox lag/dead letters, projection freshness, model latency/errors/circuit state, parse degradation, retries, and audit anomalies.
- Add SBOM/provenance and dependency, secret, container, and license scanning.
- Add browser E2E with a CAS stub, accessibility checks, load tests, and dependency-failure/recovery scenarios.

**Acceptance criteria.** staging passes security review, readiness/failure drills, alert tests, rollback, restore, and role-based browser journeys.

### 7. Version schema changes and gate projection freshness

**Validated finding.** Startup idempotent DDL is effective for early additive development but lacks ordered history, transformation/rollback control, and mixed-version safeguards. PostgreSQL-to-Neo4j uses a transactional outbox, but ranking has no explicit proof that every eligible resume reached the expected graph projection; dead-lettered/stale events can affect recall.

**Plan.**

- Introduce ordered PostgreSQL migrations and a single deployment migration step.
- Define expand-contract rules, schema-version startup guards, and backup requirements.
- Version outbox aggregates and Neo4j projections.
- Require every eligible resume/job to be parsed and projected at the expected version before ranking.
- Record projection version/freshness in ranking provenance.
- Expose replay, dead-letter repair, full re-projection, and reconciliation tools.
- Avoid holding a PostgreSQL transaction across slow external graph calls where feasible.

**Acceptance criteria.** upgrade/rollback is rehearsed; ranking refuses stale/incomplete projections and explains the blocked state.

## P2 — accountable workflow and maintainability

### 8. Add human decisions and a defensible audit packet

- Record accept/reject/hold separately from algorithmic scores.
- Capture reviewer, timestamp, job-related rationale, override, and approval without rewriting source evidence.
- Build a generalized audit-log viewer with authorized export and anomaly review.
- Produce a timestamped decision packet with policy/model/projection provenance, verified evidence, human actions, reveal history, and limitations.
- Preserve blind-mode chronology and keep identity reveal distinct from merit review.

### 9. Reconcile documentation and operational ownership

**Validated finding.** ADRs are strong, but `DEVELOPER_GUIDE.md` contains template-era facts, `HANDOFF.md` is valuable history rather than concise current state, `README.md` mixes shipped behavior with phase history, and the HR explainer is draft/stale.

- Create a concise current-state operator runbook and archive superseded chronology.
- Repair credentials/routes/ports/components in the developer guide.
- Separate approved HR policy documentation from engineering history.
- Assign owner and review date to runbooks, privacy boundaries, and policy registers.
- Add lightweight documentation/config consistency checks.

### 10. Targeted defense-in-depth backlog

These findings are valid but follow the P0/P1 controls above:

- Add PII key versioning and re-encryption workflow.
- Replace or harden deterministic unsalted email lookup if the subject-access use case permits keyed hashing.
- Enforce audit immutability at the database/privilege layer, not only by application convention.
- Make API read-path validation of corrupt stored score metadata degrade safely and observably.
- Reduce repeated `pipeline_meta` payload or introduce a detail DTO if operational evidence shows value.
- Decide whether pseudonyms should be stable across re-ranking rather than rank-derived.
- Add a dedicated degraded-resume reparse route.
- Decide and implement local LLM failover without weakening fail-closed behavior.
- Add a sanity cap for resume parse retries.

## Deferred product expansion

These remain valuable, but none outranks the controls above:

1. **Policy Studio:** first a read-only simulation over versioned policies; persistence/ratification only after P0 policy governance exists.
2. **Why this rank? slice 2:** timestamped decision-rationale export; any narrative must be strictly grounded in verified evidence. Prefer the human decision packet in item 8 over decorative prose.
3. **Inclusive-JD review:** identify exclusionary or ambiguous language before requirements are ratified.
4. **Evidence highlighting:** show verified spans in redacted source context.
5. **Grounded interview questions:** derive questions only from weak/missing verified requirements.
6. **Ask the pool:** natural-language to a strict validated query spec, never generated SQL or model-invented candidates.

## Finding disposition summary

> **Revised 2026-08-07 after evidence-checking every finding against the working tree.** The first pass of
> this table marked **21 of 21 findings "Confirmed" with no file:line citations**. A 100% acceptance rate on
> an external review is itself a warning sign, and under this repo's own rule — an assertion is not evidence
> — that table was unverified. Re-checking agreed with most of it, but **five dispositions were wrong or
> imprecise** (they would have funded the wrong work) and **four defects were missed that are more severe
> than several findings that were accepted** (see "Missed by the review" below).

Severity: **S1** blocks an HR pilot · **S2** blocks production · **S3** should fix · **S4** backlog.

| Review finding | Disposition | Sev | Evidence | Response |
|---|---|---|---|---|
| Shared recruiter key can exceed human CAS role | **Confirmed — worse than stated** | S1 | `frontend/api_client.py:118-119`; `api/deps.py:309-313` | Item 1 |
| Writes are not assignment-scoped at all | **Confirmed (sharper)** | S1 | `scoped_user_id_or_403` appears on reads only — `jobs.py:179,230`, `resumes.py:238,268,356`, `shortlist.py:90,138,163,186` | Item 1 |
| Top-15 evidence cliff | **Confirmed** | S1 | `orchestrator.py:714,723-731`; `stages.py:564-565,587` | Item 2 |
| Vector recall can omit qualified candidates | **Confirmed — worse than stated** | S1 | Recall is a **global** vector query: `orchestrator.py:303-320`, index at `neo4j_bootstrap.py:105` is not job-partitioned | Items 2, 4 |
| Ranking decimals encode HR policy | Confirmed — HR decision, not engineering | S1 | `schemas/matching.py:198-228` | Item 3 |
| Base Compose is insecure | **PARTLY RIGHT — narrower and worse** | S2 | `quickstart.ps1:107-112` already adds CAS **by default**. Real defect: `make up` diverges from it, and `compose.cas.yml:31` ships `FLASK_SECRET_KEY: dev-only-change-me`, so the *authenticated* boot signs sessions with a committed secret | Item 6 |
| "No data leaves machine" claim | Confirmed | S2 | `README.md:3,14`; `settings.py:37` has no allowlist validator | Item 6 |
| Retention not enforced; purge deferred | Confirmed / **already a recorded decision** | S2 | `ddl.py:76-77`; ADR-026 §4 states verbatim that revoke-and-purge is deferred | Item 5 |
| Raw blobs lack application encryption | Confirmed | S3 | `blob_store.py:116-121` — `0o600`, no cipher | Item 5 |
| Graph freshness can affect recall | Confirmed | S2 | `matching_tasks.py:66-67`; `graph_tasks.py:45-52` | Item 7 |
| No versioned migrations | Confirmed (deliberate) | S2 | `models/ddl.py:390-397` | Item 7 |
| Eval corpus is narrow | Partly right | S3 | Regression value is genuine; the generalization claim is fair | Item 4 |
| Live model eval is manual | Confirmed | S3 | — | Item 4 |
| Health/observability shallow | Confirmed | S3 | `api/main.py:105-107`; no middleware repo-wide | Items 5, 6 |
| Human decision workflow absent | Out of scope — intentional | S4 | — | Item 8 |
| Documentation drift | **Confirmed — and one instance is a false safety claim** | S2 | See "False assurances" below | Item 9 |
| CSRF coverage uneven | Confirmed | S3 | 3 of 12 state-changing Flask routes | Items 1, 10 |
| PII key rotation absent | Confirmed | S3 | `pii.py:45-47`; `ddl.py:145-147` | Items 5, 10 |
| Deterministic email hash enumerable | **Confirmed — sharper** | S3 | `pii.py:101` is unsalted, while `skills_graph.py:276-282` *refuses to boot* on an unsalted skill hash. The codebase disagrees with itself | Item 10 |
| Audit immutability application-only | Confirmed | S3 | `ddl.py:317-338` — no trigger/REVOKE | Item 10 |
| Reverse match failure differs from forward | **Confirmed — incomplete** | S2 | Reverse **stage 2** (`orchestrator.py:847-854`) is *unwrapped*, so an embedder outage raises a bare error that ADR-027 says will not trigger an arq retry. A stage-3-only fix leaves half the problem | Item 3 |
| Rank-derived pseudonyms can change | Confirmed | S3 | — | Item 10 |
| "A compose override pins the Tailscale peer IP" | **WRONG** | — | `docker-compose.yml:67` defaults to `host.docker.internal`; `compose.cas.yml` sets no `LLM_BASE_URL`. The IP lives in `.env.example:50` / `quickstart.ps1:98`. Fixing the named file would leave the boundary undocumented | — |
| "Nice-to-have skills do not affect the structured score" | **PARTLY RIGHT — do not generalize** | — | Structurally true (`orchestrator.py:367`), but they feed the evidence prompt (`orchestrator.py:564`) and so drive 30% of `score_final` | — |
| "Raw asyncpg makes uniform authz harder" | Premise confirmed, conclusion a judgment call | S4 | Deliberate and documented. Do **not** fund an ORM migration | Item 9 |
| Positive claims (redaction before DTO, input safety, provenance, fail-closed forward ranking) | **All confirmed true** | — | `resume_service.py:581-609`; `extract.py:42-43,104-106`; `zip_upload.py:82-124`; `orchestrator.py:585-595,705-718` | Say so in HR material |

### Missed by the review — found while validating, and two are S1

| # | Finding | Sev | Evidence |
|---|---|---|---|
| M1 | **Stage 3 fails OPEN on a non-LLM exception.** `except Exception: results[id] = None` for a **top-15** candidate → 0.0 evidence, persisted, no marker. Unlike the systematic cliff, this displaces real ranks *within* the displayed top 15, unpredictably. The review's "fails closed" praise has this hole in it | **S1** | `orchestrator.py:637-639` |
| M2 | **Stage-1 recall is a global vector query.** `WHERE r.job_id` filters *after* the global index returns its top ~150. Past ~150 résumés corpus-wide, a job's own candidates get crowded out **even when its pool is under 50**. Raising `coarse_k` will not fix this | **S1** | `orchestrator.py:303-320`; `neo4j_bootstrap.py:105` |
| M3 | **Every JD skill is a must-have.** `must=True` is hardcoded, so the `is_must_have=False` branch is dead code and `must_have_miss_penalty` (×0.5) fires on *any* unmatched extracted skill. Combined with the vocabulary gap this is the dominant real-world scoring effect — and it is unratified policy nobody has looked at | **S1** | `tasks.py:264`; `stages.py:185-194` |
| M4 | **`normalise_vector_scores` returns 1.0 for everyone on a degenerate pool** — a single-candidate or uniform pool awards the full 10% vector component to all | S3 | `stages.py:300-311` |
| M5 | **`seniority = 0.0` on an unparseable title** — a candidate loses the entire 15% sub-weight for a *parsing* failure, not a fit failure. Same class as the evidence cliff | S3 | `orchestrator.py:454-462` |
| M6 | **The negative authz tests give false assurance.** They parametrize the **key** role via `_build_app(conn, role=role)`; **no test anywhere exercises recruiter-key + hiring_manager/auditor-session** — the only combination that occurs in production. They read like coverage and are not. This is *how* a security gate passed an S1 defect | **S1** | `test_route_jobs.py:276`; `test_api_resumes_withdraw_pg.py:147` |
| M7 | **`CLAUDE.md:10` and `:79` are factually wrong** — they specify "SQLAlchemy async"; there is no SQLAlchemy dependency anywhere. The harness contract misleads every session | S2 | `requirements.txt:10` |
| M8 | **Only ~3 of ~20 ADR-referenced invariants are gated.** `expected_rank_band` is never read by `run_evals.py` (and r18 violates its own declared band); "bait below every strong fixture" is prose in `thresholds.toml`; the `skill_missing_must` pair is inert against `weights.skill = 0`. The review praised the harness without checking whether its assertions run | S2 | `run_evals.py`; `thresholds.toml:217` |

### False assurances — documents that currently state the opposite of the code

These matter more than ordinary doc drift, because each is a claim someone could rely on.

- **`docs/process/ranking-metrics-explainer.html:401`** states a Hiring Manager **"cannot reveal a candidate's identity."** Item 1 falsifies this. It is a bolded safety claim in an HR-facing document about the exact control HR would rely on. The file is marked draft/not-for-circulation at `:326` — keep it that way until Item 1 lands.
- The same file is **partially** updated (`:418`, `:407`, `:723-728`, `:636-637` are obsolete; `:711-715` is current), which is worse than uniformly stale — a reader cannot tell which half to trust.
- **`README.md:3,14`** — "data never leaves the machine" is false for the Tailscale peer inference setup.
- **`CLAUDE.md:10,79`** — see M7.

### The recurring failure mode, named

Across this review, the prior session's triage, and this session's gate work, the same defect shape appears
at least nine times: **an invariant stated in a comment, docstring, ADR, threshold file or HR document, with
nothing enforcing it.** The evidence cliff, `must=True`, the unenforced corpus assertions, the authz test
axis that was never exercised, the explainer's reveal claim, and the `Skill.display_name` cross-job leak are
all instances. Every one was invisible to a fully green gate suite and was found only by mutating the code
and observing what *failed to complain*.

**Planning consequence:** for each P0/P1 item below, the deliverable is not just the fix — it is the fix plus
the assertion that would have caught it. Prefer adding the missing test axis *before* the fix (Red first).

## Demo readiness — `CodeX/HR_DEMO_SCRIPT.md` *(assessed 2026-08-07)*

**Verdict: safe in posture, unreliable in specifics. Do not run it unmodified.** Its framing is genuinely
good — advisory-only, "blind ≠ bias-free", "not a probability", explicit limitation flags, and it correctly
tells the presenter *not* to circulate the HTML explainer and *not* to say "data never leaves this machine."
Several of its factual claims are verified accurate: fail-closed forward ranking, degraded/withdrawn résumés
excluded from ranking, quote verification and blanking, audited reveal ordering, no-role-until-granted,
hiring managers landing on assigned requisitions.

Three things make it unrunnable as written:

1. **§6 tells the presenter to point out the honest states "not recorded" and "weights unavailable". Those
   cannot appear in a fresh demo** — they fire only for legacy or malformed rows, and every newly generated
   shortlist writes `pipeline_meta` and both folded sub-scores. The presenter will be pointing at empty
   space. Worse, **the one dishonest state that *will* be on screen — the manufactured `Evidence 0%` of Item
   4c — the script does not mention.** If anyone opens candidate #16, the demo's own honesty claim breaks
   live, in the room.
2. **§1's access-scope story is falsified by Item 1.** With a hiring-manager account signed in, the reveal
   and blind-review-off controls are visible and functional. If anyone clicks one while role separation is
   being explained, the demo disproves itself.
3. **§3's "original files are stored locally" is true but incomplete** — they are unencrypted on disk
   (`blob_store.py:116-121`). Say "protected by filesystem permissions."

**Would a demo against a real SFU JD work? No.** Per Item 4a, skill scores collapse toward zero and are then
halved by the M3 must-have penalty. The chips render a wall of red *"— missing · must-have"* for terms the
candidates plainly have. That is worse than a low number: it is a screen full of visibly wrong red badges in
front of HR. **Only curated-vocabulary fixture JDs demo well — which is precisely why the evals gate never
caught this.** If a demo must happen before Item 4a, use a JD authored against `aliases.yaml`/`categories.yaml`
and **say so out loud**: "this JD is written in the system's current skill vocabulary; extending that
vocabulary to real postings is open work." Anything else misrepresents the product.

## Sequence and exit gates

| Horizon | Required work | Exit gate |
|---|---|---|
| Before pilot | 1–3 | Authorization negative tests green; score comparability fixed; HR policy signed |
| Controlled pilot | 4–6 foundations | Representative evaluation accepted; lifecycle policy approved; hardened monitored staging |
| Production readiness | Complete 4–7 | Promotion, retention, restore, rollback, and projection-freshness gates pass |
| Operational maturity | 8–10 | Human decisions reconstructable; audit and documentation owned |
| Product expansion | Deferred feature list | Each feature passes the same security, privacy, evidence, and policy gates |
