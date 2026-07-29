# ADR-026: Résumé lifecycle — candidate withdrawal and stale-résumé tracking

**Status:** Accepted as **FU-8** (2026-07-29) — building the **exclude-and-retain** slice (decisions 1–3, 5).
The §4 **revoke-and-purge** (destructive consent-erasure) path remains **deferred** to a separate follow-up
so the destructive operation is never the accidental default of a routine withdraw; decisions 1–3 ship the
reversible `withdrawn_at` flag, un-project exclusion, reverse-match filter, audit, and per-job status breakdown.
**Date:** 2026-07-28 (scoped) · 2026-07-29 (ratified as FU-8)

## Context

An uploaded résumé's fate is currently untraceable once it enters the system. Two distinct
staleness problems leave rows the recruiter cannot reason about, and a candidate whose résumé is
"stale" for either reason keeps silently affecting — or silently dropping out of — the pool:

- **(a) Parse-failure staleness.** A résumé stranded at `uploaded` when its parse times out (or the
  worker never runs) is indistinguishable from one that was never enqueued. This is the symptom in the
  four-rows-at-`uploaded` screenshot that motivated this ADR, and it is the same defect as the
  2026-07-19/20 incident (16 résumés stuck at `uploaded` for ~18 hours).
- **(b) Candidate withdrawal.** A candidate who withdraws their application has no representation in the
  data model at all. Their résumé stays `parsed`, stays projected in Neo4j, and keeps appearing in every
  newly-generated shortlist with no signal that the person is no longer a candidate.

**Part (a) is NOT in scope for this ADR — it is already scoped as FU-7 / ADR-021 decision 3** ("honest
résumé parse status": claim `uploaded`→`parsing` on task start, transition `parsing`→`failed` when arq
exhausts `max_tries`, populate `failure_reason`). The `'parsing'` and `'failed'` enum values already
exist in `core/src/models/ddl.py:57`; `'parsing'` is currently unreachable and `record_parse_failure` is
never called on a timeout (ADR-021 §2/§3). **This ADR must not introduce a second, parallel parse-state
machine.** It is recorded here only because the two halves share one recruiter-facing symptom — a
silently-shrinking or silently-stale candidate pool — and one cross-cutting fix (the per-job status
breakdown, decision 5 below).

**Part (b) is new — nothing in the repo handles withdrawal.** The evidence:

- `resume_status` (`core/src/models/ddl.py:57`) is `('uploaded', 'parsing', 'parsed', 'failed')` — there
  is no `withdrawn` value, and no `withdrawn_at`/`withdrawal_reason` column.
- The forward shortlist candidate pool is a **Neo4j** query, not a Postgres one:
  `stage1_coarse` (`core/src/pipeline/matching/orchestrator.py:284-288`) runs
  `db.index.vector.queryNodes('resume_summary_idx', …)` filtered only by
  `WHERE r.id IS NOT NULL AND r.job_id = $jid`. The `Resume` node carries **no** status property, so
  Postgres résumé status is invisible to the ranking pool. A withdrawn-but-already-parsed candidate
  would keep ranking unless exclusion is added deliberately — dropping the row's Postgres status to
  `withdrawn` alone changes nothing about what the shortlist returns.
- Reverse-match (`reverse_match_job`, `core/src/worker/matching_tasks.py:121-126`) already gates on
  `status == 'parsed'` in Postgres, so a `withdrawn` status there would exclude the résumé from the
  résumé→jobs direction "for free" — an asymmetry with the forward path worth noting.
- The repo records affirmative consent at upload (`resumes.consent_acknowledged`, no default — a positive
  act). A withdrawal is the symmetric negative act, and under PIPEDA/FIPPA it may be an explicit consent
  **revocation**, which is a stronger obligation than merely hiding the candidate from a shortlist.

## Decision

> All six items below are proposals for a future implementation session, not ratified build steps. The
> load-bearing open choices are called out inline; a human should settle them (or confirm the
> recommendation) before the TDD cycle starts.

### 1. Model withdrawal as a lifecycle event distinct from a parse failure

**Recommendation: a dedicated `withdrawn_at TIMESTAMPTZ` (nullable) + `withdrawal_reason TEXT` (nullable)
column, NOT a new terminal `withdrawn` value on the `resume_status` enum.** Rationale: `resume_status`
describes *processing* state (has the document been parsed?); withdrawal is an orthogonal *application*
state (is this person still a candidate?). A résumé can be withdrawn before it ever parses, or after it
successfully parses — collapsing both axes into one enum loses that, and forces awkward states like
"withdrawn but we never learned whether it would have parsed." A withdrawn row keeps its real
`resume_status` (`uploaded`/`parsed`/`failed`) and additionally carries `withdrawn_at IS NOT NULL`.

Ships as an idempotent `ALTER TABLE resumes ADD COLUMN IF NOT EXISTS …` on boot, matching the repo's
no-migration-framework convention (cf. ADR-017's `description_sha256`, ADR-025's `role` relax).

**Open alternative to weigh:** if the enum route is chosen instead, `withdrawn` must still be layered so
it does not erase the parse outcome — e.g. keep `resume_status` and add a separate boolean. The dedicated
timestamp column is preferred precisely because it sidesteps this.

### 2. A withdrawal action (API + Workflow UI), audited

- **API:** `POST /resumes/{id}/withdraw` (mutating, admin/recruiter only per FU-4 RBAC), optional
  `{ "reason": "…" }` body (capped length). Idempotent — withdrawing an already-withdrawn résumé is a
  no-op success, not a 409. A symmetric `POST /resumes/{id}/reinstate` (clears `withdrawn_at`) is
  in scope so a mistaken withdrawal is reversible; reinstatement must re-run exclusion in reverse
  (decision 3) — i.e. re-project or re-enable the candidate.
- **Workflow UI:** a "Withdraw candidate" control on the résumé detail page and/or each shortlist card,
  mirroring the audited-reveal button pattern (ADR-016). Blind-only posture is unchanged — withdrawal
  needs no identity reveal.
- **Audit:** every withdraw/reinstate writes an `audit_log` row (reuse FU-5's generalized sink, ADR-019 —
  the same table that already records `blind_review` flips), capturing actor, `resume_id`, action, and
  reason. Do **not** invent a second audit table.

### 3. Exclude withdrawn résumés from ranking — and pick the exclusion point deliberately

Because the forward pool is a Neo4j query blind to Postgres status (see Context), a Postgres-only status
flip is insufficient. Three candidate exclusion points, to be chosen in the implementation ADR/PR:

1. **Un-project on withdrawal (recommended for the forward path).** On withdraw, delete the résumé's
   `Resume` node + `summary_embedding` (and its `ResumeChunk`s) from Neo4j, so it simply is not in the
   `resume_summary_idx` recall set. On reinstate, re-run `project_to_graph`. Pro: the ranking pipeline
   stays byte-unchanged (no scoring-code churn → ranking-evals gate stays green trivially); cheapest to
   reason about. Con: reinstatement must re-embed.
2. **Post-stage-1 Postgres filter.** After `stage1_coarse` returns candidate `resume_id`s, drop any whose
   Postgres row has `withdrawn_at IS NOT NULL`. Pro: no Neo4j mutation; reinstatement is free. Con: adds
   a scoring-path code change (touches `orchestrator.py`) → must clear the ranking-evals gate.
3. **Cypher-level filter.** Add a `withdrawn` property to the `Resume` node and extend the stage-1
   `WHERE`. Con: introduces a Postgres↔Neo4j consistency surface (the property can drift from the source
   of truth); rejected unless option 1/2 prove inadequate.

**Reverse-match** already filters `status == 'parsed'` in Postgres
(`matching_tasks.py:121-126`); extend that predicate to also require `withdrawn_at IS NULL`. This is the
one place a pure-Postgres check is sufficient because the reverse path reads status from Postgres directly.

Whichever forward option is chosen, `persist_shortlist`'s DELETE-first-per-run behavior (ADR-010) means a
regenerate after a withdrawal correctly drops the withdrawn candidate from the persisted `shortlist_entries`
— no stale row survives, *provided* exclusion happens before persist.

### 4. Consent revocation vs. exclusion — the PIPEDA/FIPPA decision (needs a human)

A withdrawal may be a plain "not interested anymore" (exclude from ranking, retain data) or an explicit
**consent revocation** (the candidate asks that their data be removed). These carry different obligations
and this ADR does **not** presume which. Two sub-options to ratify:

- **Exclude-and-retain (default proposal):** `withdrawn_at` set, row + PII + blob retained, excluded from
  ranking. Reversible. Adequate when withdrawal ≠ erasure request.
- **Revoke-and-purge:** on a withdrawal flagged as consent revocation, hard-delete the blob
  (`BlobStore.delete`) and null/scrub the pgcrypto PII columns + `resumes.parsed`, keeping only a
  non-PII tombstone row for audit/statistics. This is the symmetric inverse of `consent_acknowledged`
  and satisfies a subject erasure request. Irreversible — no reinstatement.

**Recommendation:** ship exclude-and-retain first (decisions 1–3), and add a separate, clearly-labelled
"revoke consent & erase" path as a follow-up so the destructive operation is never the accidental default
of a routine "withdraw" click. The withdraw UI should make the two visually distinct.

### 5. Surface a per-job résumé-status breakdown (the shared cross-cut with FU-7)

Both staleness halves want the same recruiter-facing fix: on the job detail page, show candidate counts
broken down by lifecycle state — `uploaded` / `parsing` / `parsed` / `failed` / `withdrawn` — instead of a
flat résumé table where a stuck or withdrawn row silently shrinks the effective pool. ADR-021 decision 3
already promises exactly this ("candidate counts by status") for the parse-failure half; this ADR extends
the same breakdown to include the withdrawal dimension. **This is the concrete meaning of the original
request's word "tractable."** Build it once, covering both axes.

## Consequences

- A withdrawn candidate stops appearing in newly-generated shortlists and reverse-matches, and the reason
  is visible (audited) rather than a silent disappearance.
- Choosing the un-project exclusion point (decision 3, option 1) keeps `stages.py`/`orchestrator.py`
  byte-unchanged, so the ranking-evals merge-blocking gate stays green without re-baselining the corpus —
  the cheapest path to a passing build.
- The `withdrawn_at` column route (decision 1) means existing read/list/export paths must learn to display
  a withdrawn marker; the redaction boundary (ADR-006 §4 / ADR-011) is unaffected — withdrawal state is
  not PII.
- The revoke-and-purge path (decision 4) is the repo's first *destructive* PII operation (every prior PII
  control was encrypt/redact, never delete). It needs its own security review before it ships.

## Accepted residuals (to record when built, not fixed here)

- **Historical shortlists are not retroactively scrubbed.** A `shortlist_entries` row persisted *before* a
  withdrawal keeps the (redacted) candidate until the next regenerate. Decision: leave persisted history
  as-is (it is a point-in-time snapshot) and rely on regenerate, OR add a withdrawal→shortlist-cleanup
  sweep. Recommend the former for v1.
- **Neo4j/Postgres consistency window (only if exclusion option 3 is chosen).** A `withdrawn` property on
  the graph node can drift from the Postgres source of truth; options 1 and 2 avoid this surface entirely.
- **No concurrent-withdraw lock.** Symmetric with ADR-010 §1's no-advisory-lock-on-regenerate residual;
  last-writer-wins is fine for an idempotent flag flip.

## Alternatives Considered

- **Fold withdrawal into the `resume_status` enum as a terminal `withdrawn` value** — rejected as the
  primary model (see decision 1): it conflates processing state with application state and erases the
  parse outcome. Kept available as a fallback only if the dedicated-column route proves awkward.
- **Do nothing for withdrawal; rely on the recruiter to ignore withdrawn candidates manually** — rejected.
  It is exactly the silent-stale-pool problem the request is about; a withdrawn candidate still ranks and
  still consumes a shortlist slot.
- **Reuse FU-7's `failed` status for withdrawals** — rejected. A withdrawal is not a processing failure;
  overloading `failed` would corrupt the parse-health signal FU-7 exists to make honest, and would make
  "how many parses actually failed?" unanswerable.
- **Delete the résumé row outright on withdrawal** — rejected as the default. It destroys audit history
  and is irreversible for a mistaken click; the erasure case is handled deliberately and separately in
  decision 4's revoke-and-purge path.

## Notes

- This ADR is a scoping document filed from the backlog; it ships no code and no tests. When built, it
  follows the repo's mandatory TDD order (failing tests first) and all five merge-blocking gates, per
  CLAUDE.md. **Ratified 2026-07-29 as FU-8** (next free after FU-7); the exclude-and-retain slice is being
  built, the revoke-and-purge path stays deferred (see Status).
- Cross-references: FU-7 / ADR-021 (the parse-failure half of "stale résumés"); ADR-019 (FU-5 `audit_log`,
  reused here); ADR-016 (audited-action UI pattern); ADR-010 (DELETE-first persist; concurrent-run
  residual); ADR-007 §6/§7 (at-rest cleartext PII posture the revoke-and-purge path would partially
  reverse).
