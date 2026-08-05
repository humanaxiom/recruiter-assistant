# Roadmap — next-gen "wow" features

Seed menu for the **next session**. v1 (Phases 0–7) + FU-1..FU-8 + FU-7 §2/§3/§4 are all shipped and
merged; the dev-boot is reproducible (see the HANDOFF banner). These are **flagship candidates** — pick
**one** to build first; they are framed, not committed, and each honours the project's non-negotiables:
**offline-only** (inference on `aria-gb10` over Tailscale — no cloud, ever), **evidence-backed** (never a
number without a cited source), **privacy-first** (PIPEDA/FIPPA; PII never embedded; blind-by-default).

> How to read a card: **Pitch → Why it's wow → Fits the thesis → Reuses → First slice → Risks/decisions.**
> The gate discipline (TDD, three merge-blocking gates, `./scripts/verify.sh all`) applies to all of them.

---

## ⭐ 1. "Why this rank?" — the per-candidate defense pack

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
direction it's explaining (ADR-009 residual). The optional narrative must be gate-proven to never cite an
unverified quote.

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
