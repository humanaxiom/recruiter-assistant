# Roadmap — HR pilot readiness, then next-gen features

> **⚠️ 2026-08-07 — this file now leads with PILOT READINESS.** An HR demo and pilot are imminent. The
> "wow features" menu is intact further down and remains the post-pilot plan, but **none of it should be
> built before the P0 items below.** Every finding here was verified against the working tree with file:line
> evidence; nothing is carried on assertion.

---

# PART A — HR demo and pilot readiness

## A0. Demo guardrails — today, no code required

Four things that make the difference between a demo that lands and one that discredits the product:

1. **Use a curated-vocabulary job description** (the shape of `core/tests/evals/fixtures/jd_backend_data_engineer.json`).
   **Do not demo against a real SFU posting.** See A2 — skill scores collapse toward zero and are then halved,
   and the chips render a wall of red *"— missing · must-have"* for skills the candidates plainly have.
   **Say the constraint out loud:** *"this JD is written in the system's current skill vocabulary; extending
   that vocabulary to real postings is open work."* That is a credible engineering statement. A screen of
   wrong red badges is not.
2. **Sign in as admin or recruiter only.** Do not hand out hiring-manager or auditor accounts during the
   demo or the early pilot — see A1.
3. ~~**Do not circulate `docs/process/ranking-metrics-explainer.html`.**~~ **RESOLVED 2026-08-07** — the
   explainer was rewritten against the working tree and is now cleared for HR circulation. The false safety
   claim is gone (the Hiring Manager reveal claim is replaced with a by-design/as-built split that states A1
   plainly), the senior must-have exemption is disclosed, and A1–A4 are all disclosed to the reader as open
   items with the interim controls named. A Markdown twin lives at `ranking-metrics-explainer.md`. **Every
   change to authz, skill matching, the evals gate or the evidence verifier must update both files** — the
   explainer is now a circulated artifact, not a draft.
4. **Stay in the top ~15 candidates when opening the "Why this rank?" panel.** Below that it renders an
   `Evidence 0%` it never actually measured — see A4.

## A1. P0 · Authorization — RESOLVED in ADR-033 (`fix/session-role-on-writes`, PR #68, open + green)

**Highest-severity finding. This was the one that blocked handing accounts to HR.**

**Status: FIXED.** A `require_session_role(*allowed)` dependency now gates every write route (13 total:
jobs, resumes, shortlist, job_assignees) to admin/recruiter **sessions** only, intersecting the human's
real CAS role with the API key role. A structural test guard (`test_write_route_session_gate.py`) walks
the actual route table and asserts every POST/PATCH/PUT/DELETE route is gated — new write routes added
without the gate fail at test-collection time, not in production.

The human decision recorded in ADR-033 §4: **reveal is recruiter/admin only**. The scoped
hiring-manager reveal (FU-6 slice 6) is retired — un-blinding stays a two-person action (hiring manager
requests, recruiter/admin reveals), not something a hiring manager can trigger for their own assigned
jobs. ADR-020 §9 records the reversal with full context.

**Why step (iii) is deliberately not built:** ROADMAP A1 originally listed four steps: (i) add test axis,
(ii) `require_session_role`, (iii) extend `scoped_user_id_or_403` to writes, (iv) CSRF on all routes.
Once every write route's allowed set is `{admin, recruiter}`, there is no *scoped* role left that can
reach a write route at all — `scoped_user_id_or_403` exists to confine a hiring_manager session to their
own assigned jobs; both admin and recruiter are unscoped by design. Step (iii) would be dead code. This is
recorded explicitly in ADR-033 §5 so a future session does not re-discover it.

**Full detail:** [ADR-033](adr/033-session-role-enforcement-on-writes.md). **Interim pilot control
(enforced in code now, not a convention):** issue only recruiter/admin accounts. **CSRF (step iv):**
remains unscoped as a separate item.

## A2. P0 · Skill matching — domain mismatch, not vocabulary shortage

**Highest product-value finding — this decides whether a pilot produces meaningful output at all.**

**Reframe:** Not "the vocabulary is too small" but **"the ontology is for the wrong domain."**

Measured from **1,802 real SFU canonical JDs** (9,176 qualification statements, 1,222 distinct titles,
449 departments):

| Measure | Value |
|---|---|
| Shipped vocabulary | 231 terms (software-engineering ontology: javascript, react, docker, kafka…) |
| Vocabulary coverage | **15.6%** of real qualification statements |
| **New families derived from corpus** | 13 families, 234 terms (finance, student_affairs, academic_programs, research_admin, human_resources, communications, governance_policy, leadership_management, analysis_reporting, equity_indigenous, facilities_operations, interpersonal_core, health_wellness) |
| **Coverage with new families** | **54.8%** — a +39.2 point gain |
| Remaining gap | 45.2% (genuine long tail: MRI/MEG methods, microfabrication, study-permit requirements, role-specific knowledge) |

**Mechanism:** Real SFU postings are overwhelmingly administrative, academic, professional-services work.
The shipped vocabulary is a software-engineering ontology. This is a **domain mismatch**, not a quantity
problem: new vocabulary work (highest value per hour) lifts coverage from 15.6% to 54.8%; the remaining
45% is a long tail that will need the Phase 3.3 projection-time classifier regardless.

**Why an out-of-vocabulary skill scores 0.0 rather than something partial** — the root cause the classifier
in Phase 3.3 has to address, so do not lose it: ADR-008 hashes any skill outside the curated vocabulary.
`ensure_categories` (`skills_graph.py:353-369`) stamps categories *only* from `categories.yaml`, and for a
hashed key `categories_for()` returns `[]`, so the `if cats:` guard means **no Cypher runs at all** — the
property is never even set to `[]`, it stays absent. Stage 2's family-credit arm requires
`reqSkill.categories IS NOT NULL` (`orchestrator.py:377`), so it is unreachable for a hashed node.
Measured across the live graph: `hashed_total=288, with_categories=0`. A non-vocabulary requirement
therefore scores `1.0` on an identical normalised string and `0.0` otherwise — **no alias resolution, no
family partial credit, nothing in between.** The same dead arm applies to four *cleartext* canonicals with
no family (`c++`, `hudson`, `julia`, `rest api design`).

**The hash is one-way, which decides the classifier's design:** categories cannot be backfilled later over
the graph, because by then only `h:<hex>` remains. Classification must happen at projection time, where the
cleartext `raw_name` is still in hand (`tasks.py:242`/`:293`, `resume_tasks.py:1122`).

It compounds: **every `REQUIRES` edge is written `must=True`** (`tasks.py:264`), so the ×0.5
`must_have_miss_penalty` (`stages.py:185-194`) fires for nearly every candidate. Phase 3.1 addresses this by
scoring the `NICE_TO_HAVE` edges that are already written and never read (`orchestrator.py:426`).

**Unresolved product decision that gates the work:** Many of the derived terms are **competencies**
(communication, leadership, problem-solving), not named tools. The current scorer is `years × recency ×
ontology_weight`. A candidate's "three years of interpersonal skills, last used 2024" is not meaningful
on this model. **Three options:**
1. Score competencies on a different model (proficiency level, recency only, binary present/absent).
2. Exclude competencies from must-have penalties entirely.
3. Make that decision per-competency as new ones are curated.

**Plan:** Grow `aliases.yaml`/`categories.yaml` with the derived families (additive, no scoring-math
change). Once competency handling is decided, the Phase 3.3 projection-time classifier will handle the
45.2% long tail. The `must=True` edge question (ADR-009 residual) is an **HR policy decision**, not an
engineering one, and remains separate from this vocabulary work.

## A3. P0 · The evals harness cannot see what it grades

Every scoring fix above is only as trustworthy as the gate, and the gate is blind in three specific ways:

- **Blind to ADR-008 hashing by construction.** `run_evals.py::_skill_rows_for` reimplements the stage-2
  Cypher in Python and keys via `_basic_normalise`, so it can **never** produce an `h:` key. The only five
  labels the whole corpus emits are `airflow`, `docker`, `postgresql`, `python`, `rest api design`.
  *An attempt to close this was reverted* (ADR-032) because it inverted the bait-below-strong ordering while
  producing no hashed key at all. Doing it properly needs the non-vocab skill in `required_skills`, which
  forces a must-have miss for every honest fixture and re-bands the corpus — margins must be **re-measured**.
- **Assertions that do not run.** `expected_rank_band` is never referenced by `run_evals.py` — and r18
  currently violates its own declared band (tagged `strong`, band `{1,9}`, actual rank 11). *"The bait is
  BELOW EVERY STRONG FIXTURE"* is prose in `thresholds.toml:217`, not a gated key: a change that violated it
  still exited 0. The `skill_missing_must` ordering pair is inert against `weights.skill = 0`.
- **Recommended first move** — needs no new fixtures and no measured constants, passes today, and would have
  caught the reverted change: add `[adversarial] must_rank_below_every_strong = true`, enforced as an order
  relation over tags. **Do not** enforce `expected_rank_band` wholesale; it goes red immediately on r18,
  which needs its own reconciliation.

## A4. P0 · Two ranking defects that affect what HR will see

**M1 — stage 3 fails OPEN on a non-LLM exception.** `orchestrator.py:637-639` catches bare `Exception`
per candidate and sets `results[id] = None`. For a **top-15** candidate that silently zeroes 40% of
`score_final` and persists it, unmarked. The systematic evidence cliff provably *cannot* reorder the
displayed list; **this one displaces real candidates inside the visible top ranks**, only when a transient
Neo4j/Postgres hiccup happens to hit them. It contradicts the fail-closed posture ADR-029 claims.
**`ranking-evals` gated.**

**M2 — stage-1 recall is a global vector query.** `resume_summary_idx` (`neo4j_bootstrap.py:105`) is not
job-partitioned; `orchestrator.py:303-320` applies `WHERE r.job_id` *after* the global index returns its top
~150. Past ~150 résumés corpus-wide, a job's own candidates get crowded out **even when that job's pool is
well under `coarse_k`**. A pilot that loads several hundred résumés will hit this. **Raising `coarse_k` does
not fix it** and would mask it. **`ranking-evals` gated.**

**The evidence cliff and the defense pack (ADR-031).** `evidence_k=15`, but all of `candidates_s2` goes to
`stage4_combine`, so candidates past 15 get `0.0` evidence *and* motivation — 40% of the score — from compute
placement, not merit. `shortlist_top_percent` defaults to **100**, so this is live on any job with >15
recalled candidates. Rank order is provably unaffected (`0.6·s_i + (≥0) ≥ 0.6·s_j`), but **the number is not
comparable across the boundary and upward mobility is suppressed** — a rank-16 candidate with strong evidence
can never demonstrate it.

Worse for HR-facing honesty: `stage4_combine` produces **real `0.0` floats**, so `explanation.py:180-182`
sets `scores_available=True` and the panel renders `Evidence · 30% · 0% · 0.00` **affirmatively** —
indistinguishable from a candidate evaluated and found lacking. **ADR-031's "not recorded" guard protects an
*unreadable* row, not a *never-computed* one.** The honest fix needs a persisted `evidence_evaluated` marker
(write-path → `ranking-evals` gated). **Do not** infer it from `requirements == []` in the template — that is
inferring pipeline state from a display artifact.

## A5. P0 · Documents that currently state the opposite of the code

Each is a claim someone could rely on:

- ~~**`docs/process/ranking-metrics-explainer.html:401`**~~ **FIXED 2026-08-07.** The bold "cannot reveal a
  candidate's identity" claim is gone; each persona now carries an explicit *by design* / *as built* split,
  and A1 is disclosed as pilot gap 1 with "issue only Recruiter and Admin accounts" as the interim control.
  The four obsolete passages the review identified (`:418` no-scoping, `:407` auditor, `:723-728` register
  items 13/14, `:636-637` unaudited un-blind) are now marked **Closed since July** rather than deleted, so a
  reader comparing versions can see what moved. The register is rebuilt at 23 items (13 ratify / 10 gap) and
  now carries A1–A4, retention non-enforcement, unencrypted blobs and the unsalted email hash. A Markdown
  twin was added: `docs/process/ranking-metrics-explainer.md`.
- **`README.md:3,14`** — "data never leaves the machine" is false for the Tailscale peer inference setup.
- **`CLAUDE.md:10,79`** — specify "SQLAlchemy async"; there is no SQLAlchemy dependency anywhere. The harness
  contract misleads every session.
- **`compose.cas.yml:31`** ships `FLASK_SECRET_KEY: dev-only-change-me` — the *authenticated* boot signs
  sessions with a committed secret. `make up` also diverges from `quickstart.ps1` (which correctly adds CAS).

## A6. P1 · Before the pilot widens

Retention is stored but never enforced (`ddl.py:76-77`); revoke-and-purge is a **recorded deferral**
(ADR-026 §4) needing an HR decision, not a rediscovery; reverse match fails *open* at stage 3 and is
**unwrapped at stage 2** (`orchestrator.py:847-854`), so a stage-3-only fix leaves half the problem; blobs
are permission-gated but unencrypted (`blob_store.py:116-121`); the email hash is unsalted (`pii.py:101`)
while the skill hash *refuses to boot* unsalted (`skills_graph.py:276-282`) — the codebase disagrees with
itself; audit immutability is convention only (`ddl.py:317-338`); `/health` is shallow with no readiness,
metrics, tracing or correlation IDs.

Two smaller scoring defects worth folding into any A2/A4 work: `normalise_vector_scores` returns `1.0` for
**everyone** on a degenerate pool (`stages.py:300-311`), and `seniority = 0.0` on an unparseable title
(`orchestrator.py:454-462`) — a candidate loses the full 15% sub-weight for a *parsing* failure.

## A7. The pattern worth naming

Across the external review and this session's gate work the same defect shape appears **nine times**: an
invariant stated in a comment, docstring, ADR, threshold file or HR document, **with nothing enforcing it**.
The evidence cliff, `must=True`, the unenforced corpus assertions, the authz test axis that was never
exercised, the explainer's reveal claim, and the `Skill.display_name` cross-job leak are all instances. Every
one was invisible to a fully green 4,100-test suite and was found only by mutating the code and watching what
*failed to complain*.

**Planning consequence:** for each item above, the deliverable is the fix **plus the assertion that would
have caught it** — Red first.

---

# PART B — next-gen "wow" features (post-pilot)

Seed menu for **after** Part A. v1 (Phases 0–7) + FU-1..FU-8 + FU-7 §2/§3/§4 are all shipped and
merged; the dev-boot is reproducible (see the HANDOFF banner). These are **flagship candidates** — pick
**one** to build first; they are framed, not committed, and each honours the project's non-negotiables:
**offline-only** (inference on `aria-gb10` over Tailscale — no cloud, ever), **evidence-backed** (never a
number without a cited source), **privacy-first** (PIPEDA/FIPPA; PII never embedded; blind-by-default).

> How to read a card: **Pitch → Why it's wow → Fits the thesis → Reuses → First slice → Risks/decisions.**
> The gate discipline (TDD, three merge-blocking gates, `./scripts/verify.sh all`) applies to all of them.

---

## ⭐ 1. "Why this rank?" — the per-candidate defense pack

**Slice 1 SHIPPED** (branch `feat/why-this-rank-defense-pack`, PR pending; [ADR-031](adr/031-why-this-rank-defense-pack.md)):
the deterministic score-composition + verified-evidence panel on the shortlist entry detail page, per the
"First slice" scope below — no LLM, no DDL, no scoring-math change. Slice 2 (the optional grounded-LLM
narrative + PDF/timestamped decision-rationale export) is still open; see ADR-031's accepted residuals.

**Pitch.** One click on any shortlisted candidate opens a plain-language explanation of *why* they sit
where they do: each sub-score's contribution to `score_final` (skill 0.40 · experience 0.25 · education
0.10 · seniority 0.15 · vector 0.10, blended structured 0.6 / evidence 0.3 / motivation 0.1), the **actual
verified evidence quotes** per requirement, what pulled them up vs down, and an exportable, timestamped
**decision-rationale record** for the file.

**Why it's wow.** It turns an opaque 0.78 into a defensible, auditable story — the literal payoff of
"evidence-backed ranking." It's the artifact a hiring manager shows in a review and a compliance officer
shows if a decision is ever challenged.

**Fits the thesis.** Pure transparency; read-only; adds no new PII surface (it *reveals* less than a raw
reveal — it explains the score, redaction-aware). Deterministic core straight from `score_breakdown` +
`evidence`; an *optional* local-LLM narrative that is grounded (reuse the `verify_evidence` anti-fabrication
discipline — the narrative may only reference verified quotes, never invent).

**Reuses.** `ScoreBreakdown`/`EvidenceObject`, `redaction.py`, the ranking-metrics explainer's math, the
reveal-audit sink, CSV/JSON export.

**First slice.** A deterministic "score composition + verified evidence" panel on the shortlist entry page
(no LLM) — table of sub-scores × weights → contribution, with each requirement's quote and met/partial/
missing status. Ship that, *then* add the optional grounded narrative + PDF/record export as slice 2.

**Risks/decisions.** Reverse-match scores top out at 0.9 (no motivation term) — the panel must label which
direction it's explaining (ADR-009 residual). **Slice 1 addressed this by scoping forward-only**
(`shortlist_entry_explanation` only accepts a `ShortlistEntry`, never a `JobMatchEntry`); a reverse-match
panel is unscoped future work, so ADR-009's residual stays open there. The optional narrative must be
gate-proven to never cite an unverified quote.

---

## ⭐ 2. "Ask the pool" — natural-language, evidence-grounded candidate search

**Pitch.** A recruiter types plain English — *"senior backend engineers with production Kafka who aren't
over-qualified and submitted a cover letter"* — and the local model on `aria-gb10` maps it to a **structured
filter/weight spec** over the already-ranked, evidence-backed pool. The DB executes the spec; results come
back as **cited candidates**, never invented ones.

**Why it's wow.** Conversational hiring search that is 100% offline and provably grounded — no candidate
data leaves the tailnet, and the model can't hallucinate a candidate because it only ever emits a *query
spec*, not results.

**Fits the thesis.** The LLM is a **translator, not an oracle**: it outputs a strict JSON filter (fields
already in `resumes.parsed` / `MatchWeights` / evidence status) validated by a pydantic schema; the ranking
engine + SQL do the actual selection. Blind-review redaction still applies to whatever renders.

**Reuses.** `LLMClient` + strict `chat_json`, the schemas, the ranking engine, blind redaction, the
existing shortlist read paths.

**First slice.** A single-turn NL → `SearchSpec` (a new strict schema: must-have skills, min/max years,
education level/fields, has-cover-letter, over-qual bound, sort key) → run it as a filter over an existing
shortlist and render cited matches. Add multi-turn refinement + "explain this filter" as slice 2.

**Risks/decisions.** Guardrails are the whole game: the model **must** fail closed (ADR-029 pattern) if it
can't produce a valid `SearchSpec`; never free-texts a WHERE clause (injection); a ranking-evals-style gate
should prove a battery of NL prompts map to the intended specs. Decide the vocabulary the filter can express
up front.

---

## ⭐ 3. Policy Studio — ratify the "hiring policy written as decimals," live

**Pitch.** Turn the static *fifteen policy decisions* register (`docs/process/ranking-metrics-explainer.html`)
into an **interactive admin tool**: adjust the ratifiable `MatchWeights` knobs (sub-score weights, over-qual
curve, recency banding, must-have-miss penalty, education field-relevance bar…) and watch a real
requisition's shortlist **re-rank live**, each change annotated with its register item and its adverse-impact
caveat — then **"ratify"** a weight profile with an audit trail and an owner.

**Why it's wow.** It makes "hiring policy as decimals" *governable* and tangible — a leadership/compliance
showpiece that closes the loop from the explainer (which only *describes* the knobs) to actually owning them.

**Fits the thesis.** Directly extends configurable shortlist size (ADR-024) + the education-field knob
(ADR-028) + the register. Admin-gated (ADR-025 role model). Re-ranking is the existing engine run with a
candidate `MatchWeights`; no scoring-math change, so ranking-evals stays the guard.

**Reuses.** `weights_from_settings`/`MatchWeights`, the ranking engine, the ratification register, the
audit sink, the admin session gate.

**First slice.** A read-only "what-if" preview: admin picks a job, tweaks weights in the UI, sees the
shortlist re-ordered (computed against a transient `MatchWeights`, nothing persisted). Add persist-a-profile
+ ratify-with-audit as slice 2.

**Risks/decisions.** Live re-rank calls stage 3 (LLM evidence) — cache/scope it or preview structured-only
first to stay fast. Decide whether ratified profiles are global or per-requisition. Never let the UI write a
weight profile that fails the `MatchWeights` sums-to-1.0 validator.

---

## Also on the table (one-liners)

- **Inclusive-JD linter** — pre-create, the local model flags exclusionary language / unrealistic
  requirements in a JD and suggests rewrites. Improves fairness at the top of the funnel.
- **Interview-question generator** — for a shortlisted candidate, generate targeted questions from the
  requirements whose evidence was *weak/missing* — closes ranking → interview.
- **Evidence highlighting** — render the résumé with matched evidence spans highlighted inline (visual,
  grounded), redaction-aware.
- **Consent-erasure (ADR-026 §4 revoke-and-purge)** — the repo's first destructive PII op; needs a human
  decision on semantics + its own security review before any code. (Carried from the Next-session plan.)

## Still-open smaller items (from the prior plan)

- **FU-7 decision 1** — LLM provider failover chain (now genuinely useful: a *second* Ollama host would let
  `aria-gb10` outages fail over instead of fail closed).
- `resume_parse_max_tries` upper sanity cap; extend fail-closed to the reverse-match path; a
  `POST /resumes/{id}/reparse` route (makes degraded résumés recoverable without re-upload).
