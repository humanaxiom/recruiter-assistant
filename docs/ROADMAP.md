# Roadmap — a live pilot, and what to build for it

**Updated 2026-08-27.** The product is deployed on a dedicated box and **four
people are using it.** The CIO is supportive and HR is satisfied with what was
presented. The build phase is open again.

> **What changed, and why this file is a quarter of its former size.** Everything
> above this line was previously framed as *pilot readiness* — reaching a user was
> the goal, and the file had accumulated eight months of closed work arguing about
> how to get there. That question is answered. The closed Part A chronicle (A0–A7,
> the 21-instance defect taxonomy, the resolved authorization and ranking work)
> now lives in
> [docs/archive/ROADMAP-2026-08-24-part-a-and-history.md](archive/ROADMAP-2026-08-24-part-a-and-history.md),
> whole, with its measurements intact. **Nothing open was dropped** — every live
> item is below.

## The rule that replaces "get a user"

> **The four people on the pilot box can do their real hiring work in it, and
> what they hit gets fixed before anything they haven't hit gets built.**

User-sourced work outranks self-sourced work. That is the whole discipline, and
it is the only thing that has ever reliably stopped this repository from
gold-plating itself — see the archived
[RESET.md](archive/RESET-2026-08-24.md) for the measured version of why, and
`CLAUDE.md` §Economy for the thresholds it is enforced by.

A feature below is worth starting **when a pilot user's experience calls for it,
or when it is the thing you'd show the CIO next.** Not because it is the most
interesting card on the menu.

---

## Where things stand

| | |
|---|---|
| Deployment | Dedicated box, **4 users**, live |
| Pipeline | Upload → parse → rank → shortlist; JD ingest; blind review, PII encryption, audited reveal — all working end to end |
| Authorization | CAS identity, session role enforcement on writes, CSRF on all 12 browser write routes, auditor viewer — shipped (ADR-019/033/034/035/036) |
| Ranking honesty | Evidence cliff, five fallback sub-scores, and the stage-3 fail-open are all **disclosed on screen** (ADR-037/039/040/041) |
| Ranking quality vs. real postings | Vocabulary recognises **54.8%** of real SFU qualification statements — see [Open item 3](#3-the-remaining-452-of-a-real-posting) |
| Verification | `verify.sh` (code) · `smoke.sh` (screen) · `doctor.sh` (data) · `model-check.sh` (before a model swap) |

---

# Open work

Ordered. Items 1–2 are what a live deployment now demands; 3–5 are carried
engineering residuals that the pilot has made either more or less urgent.

## 1. Operating a real deployment

The pilot box is no longer a dev stack, and three things that were acceptable
without users are not acceptable with them.

- **✅ `FLASK_SECRET_KEY` — CLOSED 2026-09-03.** Was the highest-severity item in
  this file: the literal `dev-only-change-me` was committed in both compose
  files, so anyone who could reach the frontend could forge a session for any
  role. Fixed in three layers, because any one alone leaves a hole —
  `quickstart.ps1` generates it, both compose files now take it from the
  environment (`${FLASK_SECRET_KEY:?…}`, so `.env` actually wins), and
  `validate_startup_session_secret` refuses to boot a CAS-enabled deployment on
  any published default or an empty value.

  **Two things this leaves for an operator, not a session.** The generator
  originally skipped values that were merely *published* rather than blank —
  which would have skipped every `.env` created while compose hard-coded the
  literal, i.e. exactly the deployments needing rotation. That is fixed, but it
  means **the pilot box still needs `scripts/quickstart.ps1` run against it**
  (needs `pwsh` 7; it does not parse under PowerShell 5.1). **Rotating
  invalidates every live session**, so tell the four users before doing it
  rather than letting it look like a fault.
- **`pg.jobs_stuck` is still missing from `doctor.sh`.**
  [core/src/doctor.py:174](../core/src/doctor.py#L174) checks `pg.resumes_stuck` and
  nothing checks `jobs.failure_reason IS NOT NULL` or draft jobs with no
  `parsed_at`. This is precisely why 20 dead JDs sat behind a `parsing…` spinner
  for 24 hours until a human noticed. The display bug is fixed; the *detection*
  gap is not. ~1h.
- **Retention is stored and never enforced** ([ddl.py:132-133](../core/src/models/ddl.py#L132)
  defines and constrains `retention_days`; no purge path reads it). With real
  candidate documents on a real box this moves from hygiene to a FIPPA/PIPEDA
  commitment the product makes and does not keep. Related and still a **recorded
  deferral needing an HR decision**: revoke-and-purge semantics (ADR-026 §4) —
  the repo's first destructive PII operation, and it needs its own security
  review before any code.

**Also now real, previously theoretical:** blobs are permission-gated but
unencrypted ([blob_store.py:116-121](../core/src/storage/blob_store.py#L116)); the
email hash is unsalted ([pii.py:101](../core/src/services/pii.py#L101)) while the
skill hash *refuses to boot* unsalted — the codebase disagrees with itself; audit
immutability is convention only ([ddl.py:317-338](../core/src/models/ddl.py#L317));
`/health` is shallow, with no readiness, metrics, tracing or correlation IDs.
There are no backups and no restore drill.

## 2. Capture what the four users hit

There is currently **no channel from a pilot user's confusion back into this
repo** except someone reporting it in conversation. Every defect the pilot has
produced so far arrived that way, and each was worth more than a week of
inspection: 20 dead jobs, a withdraw form that collected no reason, hashed skill
labels where words belonged.

The cheapest version is a file, not a feature: a `docs/pilot-feedback.md` that
each report lands in verbatim, dated, with who hit it. Promote from there. Do
this before building anything on the menu below — it is what tells you which card
to pick.

## 3. The remaining 45.2% of a real posting

The A2 vocabulary merge took coverage of real SFU qualification statements from
15.6% to **54.8%** (ADR-042). The rest is a genuine long tail — MRI/MEG methods,
microfabrication, study-permit requirements, role-specific knowledge — and the
parse-time skill-family classifier that addresses it **shipped disabled**
(ADR-044). `match_use_classified_families` defaults `False`
([settings.py:213](../core/src/settings.py#L213)): the field is written and the
ranking engine ignores it.

**Two residuals block flipping that flag**, and both are real work:

1. **Shared `Skill` nodes overwrite each other.** Two candidates with the same
   out-of-vocabulary phrase clobber each other's `classified_categories`, and a
   re-parse that declines to classify leaves the prior value in place. Needs the
   field cleared on every write, plus a test for the re-parse case.
2. **No accuracy measurement exists.** The eval corpus is blind to the classifier
   by construction — all 20 fixtures hold only in-vocabulary skills — so a green
   `ranking-evals` run says nothing about whether the classifications are right.

**What the pilot changes here:** an unscripted real posting can still show a wall
of red *"— missing · must-have"* chips for skills a candidate plainly has. Four
users are now seeing that surface directly. If they report it, this item is
item 1.

**Still needing a product decision — and its precondition is now met.** Many
merged terms are *competencies* (communication, leadership, problem-solving), not
named tools, and `years × recency × ontology_weight` is a semantically odd model
for "three years of interpersonal skills, last used 2024". This was deferred with
owner *"corpus owner + HR, with pilot data"*. **There is now pilot data.** Options
are in ADR-042 §2; the deferral was correct, and it has expired.

## 4. The evals gate has four blind spots

Every scoring change is only as trustworthy as `ranking-evals`, and it cannot see
four things. All four are corpus-owner work — new fixtures re-band the corpus, so
none is a drive-by.

| Blind spot | Consequence |
|---|---|
| **ADR-008 hashing, by construction** — `run_evals.py::_skill_rows_for` reimplements the stage-2 Cypher in Python and can never produce an `h:` key | The largest of the four. Closing it forces a must-have miss for every honest fixture and **re-bands the whole corpus** |
| **The A2 vocabulary merge** — the corpus's 19 skill canonicals intersect A2's 225 newly-categorised ones at **∅** | A green run on a vocabulary change is byte-identical to `main` and carries *no information* about its correctness |
| **No ordering control for `seniority` or `vector`** | Measured: wiping seniority moved 8 of 20 fixtures and **exited 0**; flattening vector moved 6 of 20 and **exited 0** |
| **`expected_rank_band` is never enforced** — and enforcement is 🔴 blocked on a three-way contract conflict, not on the r18 violation it was assumed to be | Two fixtures currently violate their own declared bands. See the archived A3 for the three contracts and why retagging fails |

## 5. Carried residuals — recorded, not scheduled

Small, real, and none of them blocking. Fix one when you are already in the file.

**Ranking honesty**
- The evidence cliff is **disclosed, not removed** (ADR-040): candidates past
  `evidence_k=15` still lose 40% of the composite for reasons unrelated to merit,
  and upward mobility is still suppressed. Removing it means evaluating every
  retained candidate, or splitting structured screening from evidence-enriched
  ranking with distinct labels — that is **feature card 1**'s territory (below) and a product decision.
- Reverse match carries the identical fabricated zero (`match_reverse_evidence_k = 10`), untouched.
- Disclosure reaches the **entry-detail panel only** — shortlist card tiles and the CSV export still render a bare `0` / `100`.
- Renormalising the remaining sub-weights when a dimension is unmeasurable is open, and needs the same HR decision as item 3: is "no work history at all" neutral-weighted, or does it genuinely mean no seniority?
- Reverse match fails **open** at stage 3 and is **unwrapped at stage 2** (`orchestrator.py:955-970`), so a stage-3-only fix leaves half the problem.

**Operational**
- **Nothing detects "the fix that never ran."** No check reports job edges missing `display_name`, so the next projection-shaped fix will be inert for exactly as long before someone happens to look at a screen. A startup or health-check count of unlabelled edges would have caught the last one in a day.
- **No way to re-project a job without re-parsing it.** `parse_job` re-runs the LLM and can change the extracted requirements, so it is not a safe "refresh the projection" control. A JD re-parse route exists ([jobs.py:318](../core/src/api/routes/jobs.py#L318)); a re-*project* control does not.
- `shortlist_entries.score_breakdown` caches the rendered label, so a graph backfill stays invisible until each job is regenerated.
- The Regenerate staleness bound is wall-clock, not job-time — a 2+ hour ranking reads stale after 1 hour (`shortlist_service.py:276-279`).
- A second Regenerate during a run is silently dropped by the advisory lock with no user acknowledgement (`matching_tasks.py:80-87`).
- **Job `306c573c` fails extraction on model output, not infrastructure.** The longest real posting (9,523 chars) returns `llm output invalid: title: missing`. `chat_json` already retries once with the validator error appended, and generation is `temperature=0`, so a retry reproduces it. **Measure before guessing:** `jd_extract_v1`'s measured 4096 floor came from a shorter fixture, and the token floor is per-*prompt*, not per-model.
- **No `POST /resumes/{id}/reparse` route** — a degraded résumé cannot be recovered without re-upload. The JD side has one; the résumé side does not.
- `resume_parse_max_tries` has no upper sanity cap.
- **FU-7 decision 1 — LLM provider failover chain.** Genuinely useful now: a second Ollama host would let an `aria-gb10` outage fail *over* rather than fail *closed*.

**Privacy / access**
- **🔴 GitHub Support PII purge — still open, ~15 minutes of someone's time.** Real candidate résumés remain fetchable by SHA on a public repo. Deleting the branch did **not** stop GitHub serving them (tested, not assumed). Both `humanaxiom/` and `sfu-aria/` are public. This is the oldest unactioned item in the file and the only one with a live external exposure.
- The shortlist card's quick withdraw still collects no reason (`shortlist_cards.html:151-158`) — deliberate: a text input on every card is poor UX. Consequence: those withdrawals record `None`, so the audited reveal has nothing to offer for them. **Revisit if pilot users withdraw mostly from cards** — now checkable.
- Reveals are not rate-limited. The audit trail *is* the control (option C records access rather than preventing it), but nothing alerts on the pattern.

---

# The next feature — ANSWERED 2026-09-02, and it is none of these three

> ## 🔴 The sponsor picked, so this menu is no longer the question.
>
> The DTO/CIO sent a requirements set on **2026-09-02** and answered all four of
> its open decisions the same day. The plan of record for what gets built next
> is **[SPONSOR_REQUIREMENTS_PLAN.md](SPONSOR_REQUIREMENTS_PLAN.md)**; work is in
> flight on `feat/sponsor-requirements`.
>
> This is exactly what [open item 2](#2-capture-what-the-four-users-hit) said
> would decide it — *"let the feedback channel pick"* — arriving from the person
> who owns the pilot rather than from a session's own judgement. Under
> `CLAUDE.md` §Economy 0 that outranks everything self-sourced.
>
> **What the sponsor asked for that none of these cards covers:** multi-candidate
> PDF splitting in-app, a candidate CSV roster, JD-by-URL (answered: port the
> Taleo import from `C:\repos\hris`), a hiring-manager requirements prompt,
> Canadian work-authorization screening, links to the source PDFs, and a
> notification when a long run finishes.
>
> Two of the sponsor's answers **changed shipped behaviour**, so they are not
> additive and are recorded here rather than only in the plan:
>
> * **A cover letter must no longer affect ranking** (§O3). `motivation` went
>   0.1 → 0.0 and the 10% moved to the manager's own requirements. The eval
>   corpus's `motivation` ordering control asserted the opposite and has been
>   **inverted** into `[cover_letter_neutrality]` (exact score equality).
> * **Ineligible candidates rank last with their metrics invalidated** (§O2) —
>   not hidden, not deleted, and never inferred from résumé text.

The three cards below stay **framed, not chosen**, as a menu for after the
sponsor's set. Each honours the non-negotiables: **offline-only** (inference on
`aria-gb10` over Tailscale — no cloud, ever), **evidence-backed** (never a number
without a cited source), **privacy-first** (PIPEDA/FIPPA; PII never embedded;
blind-by-default).

> How to read a card: **Pitch → Why it's wow → Fits the thesis → Reuses → Next slice → Risks/decisions.**
> The gate discipline (TDD, three merge-blocking gates, `./scripts/verify.sh all`) applies to all of them.

## ⭐ 1. "Why this rank?" — the per-candidate defense pack

**Slice 1 is SHIPPED** ([ADR-031](adr/031-why-this-rank-defense-pack.md)): the
deterministic score-composition + verified-evidence panel on the shortlist entry
detail page. No LLM, no DDL, no scoring-math change. **Slice 2 is what remains.**

**Pitch.** One click on any shortlisted candidate opens a plain-language
explanation of *why* they sit where they do: each sub-score's contribution to
`score_final` (skill 0.40 · experience 0.25 · education 0.10 · seniority 0.15 ·
vector 0.10, blended structured 0.6 / evidence 0.3 / motivation 0.1), the actual
verified evidence quotes per requirement, what pulled them up vs down, and an
exportable, timestamped **decision-rationale record** for the file.

**Why it's wow.** It turns an opaque 0.78 into a defensible, auditable story —
the literal payoff of "evidence-backed ranking." It is the artifact a hiring
manager shows in a review and a compliance officer shows if a decision is ever
challenged.

**Fits the thesis.** Pure transparency; read-only; adds no new PII surface — it
*reveals* less than a raw reveal. Deterministic core straight from
`ScoreBreakdown` + `evidence`; the optional local-LLM narrative is grounded by
reusing the `verify_evidence` anti-fabrication discipline, and may only reference
verified quotes.

**Reuses.** `ScoreBreakdown`/`EvidenceObject`, `redaction.py`, the
ranking-metrics explainer's math, the reveal-audit sink, CSV/JSON export.

**Next slice.** The optional grounded-LLM narrative + PDF / timestamped
decision-rationale export.

**Risks/decisions.** Reverse-match scores top out at 0.9 (no motivation term), so
the panel must label which direction it is explaining — slice 1 handled this by
scoping forward-only (`shortlist_entry_explanation` accepts only a
`ShortlistEntry`), leaving ADR-009's residual open for reverse match. The
narrative must be **gate-proven** never to cite an unverified quote.

**Lowest risk of the three, and it extends exactly what HR just saw.**

## ⭐ 2. "Ask the pool" — natural-language, evidence-grounded candidate search

**Pitch.** A recruiter types plain English — *"senior backend engineers with
production Kafka who aren't over-qualified and submitted a cover letter"* — and
the local model maps it to a **structured filter/weight spec** over the
already-ranked, evidence-backed pool. The database executes the spec; results
come back as **cited candidates, never invented ones.**

**Why it's wow.** Conversational hiring search that is 100% offline and provably
grounded. No candidate data leaves the tailnet, and the model cannot hallucinate
a candidate because it only ever emits a *query spec*, not results.

**Fits the thesis.** The LLM is a **translator, not an oracle**: it outputs a
strict JSON filter over fields already in `resumes.parsed` / `MatchWeights` /
evidence status, validated by a pydantic schema. The ranking engine and SQL do
the selection. Blind-review redaction still applies to whatever renders.

**Reuses.** `LLMClient` + strict `chat_json`, the schemas, the ranking engine,
blind redaction, existing shortlist read paths.

**First slice.** Single-turn NL → `SearchSpec` (a new strict schema: must-have
skills, min/max years, education level/fields, has-cover-letter, over-qual bound,
sort key) → run as a filter over an existing shortlist, render cited matches.
Multi-turn refinement and "explain this filter" are slice 2.

**Risks/decisions.** Guardrails are the whole game: fail **closed** (ADR-029
pattern) when a valid `SearchSpec` cannot be produced; never free-text a WHERE
clause; a `ranking-evals`-style gate must prove a battery of NL prompts map to
the intended specs. Decide the expressible vocabulary up front. **Note the token
floor is per-prompt** — measure this one's budget with `model-check.sh` rather
than inheriting a literal.

**Highest demo impact, most new surface.**

## ⭐ 3. Policy Studio — ratify the "hiring policy written as decimals," live

**Pitch.** Turn the static policy-decisions register in
`docs/process/ranking-metrics-explainer.md` into an **interactive admin tool**:
adjust the ratifiable `MatchWeights` knobs (sub-score weights, over-qual curve,
recency banding, must-have-miss penalty, education field-relevance bar) and watch
a real requisition's shortlist **re-rank live**, each change annotated with its
register item and its adverse-impact caveat — then **ratify** a weight profile
with an audit trail and an owner.

**Why it's wow.** It makes "hiring policy as decimals" *governable* and tangible
— a leadership and compliance showpiece that closes the loop from the explainer,
which only *describes* the knobs, to actually owning them.

**Fits the thesis.** Extends configurable shortlist size (ADR-024), the
education-field knob (ADR-028) and the register. Admin-gated (ADR-025 role
model). Re-ranking is the existing engine run with a candidate `MatchWeights`, so
no scoring-math change and `ranking-evals` stays the guard.

**Reuses.** `weights_from_settings`/`MatchWeights`, the ranking engine, the
ratification register, the audit sink, the admin session gate.

**First slice.** A read-only "what-if" preview — admin picks a job, tweaks
weights, sees the shortlist re-ordered against a transient `MatchWeights` with
nothing persisted. Persist-a-profile and ratify-with-audit are slice 2.

**Risks/decisions.** Live re-rank calls stage 3 (LLM evidence) — cache it, or
preview structured-only first, to stay fast. Decide whether ratified profiles are
global or per-requisition. Never let the UI write a profile that fails the
`MatchWeights` sums-to-1.0 validator.

**The card that most directly answers "who owns this decision?" — the question
HR and the CIO will ask next.**

## Also on the table (one-liners)

- **Inclusive-JD linter** — pre-create, the local model flags exclusionary language or unrealistic requirements in a JD and suggests rewrites. Improves fairness at the top of the funnel.
- **Interview-question generator** — for a shortlisted candidate, generate targeted questions from the requirements whose evidence was *weak or missing*. Closes ranking → interview.
- **Evidence highlighting** — render the résumé with matched evidence spans highlighted inline; visual, grounded, redaction-aware.
- **Human decision record** — capture accept/reject/hold separately from the algorithmic score, with reviewer, timestamp and job-related rationale, without altering source evidence. Makes "what did the human decide, and why did they differ?" reconstructable. See [CodeX/plan.md](../CodeX/plan.md) §7.
- **Consent-erasure (ADR-026 §4 revoke-and-purge)** — the repo's first destructive PII operation; needs an HR decision on semantics plus its own security review before any code. Cross-listed under [open item 1](#1-operating-a-real-deployment).

---

## What the gates cannot see

Kept from the archived A7 analysis because it is operationally load-bearing, and
because three of these were learned the expensive way. The 21-instance taxonomy
itself is archived and **is not to be extended** — fix defects users hit, do not
audit for defects nobody has hit (`CLAUDE.md` §Economy 0).

The characteristic defect of this repository is **an invariant stated in a
comment, docstring, ADR or threshold file with nothing enforcing it.** Every
instance was invisible to a fully green suite. Four classes the gates are
structurally blind to, and the tool that sees each:

| Blind to | Seen by | Learned from |
|---|---|---|
| The browser → Flask → API seam (every frontend test mocks `api_client`) | `smoke.sh` | Its **first run** found ranking silently dropping every candidate |
| Stale deployment **state** — a fix that is correct, gated green, and has never applied to a row | `doctor.sh` | A correct fix that had not touched a single row 13 days later, because every row predated it |
| The **real model's** behaviour — the suite mocks the LLM everywhere, by necessity | `model-check.sh` | One `max_tokens` literal made the product unable to rank anyone |
| An **absence** — a form input that was never rendered, which no mutation of existing code can make appear | Running the product | *The test that plays both parts*: a test that supplies the input a user was meant to supply can never notice that no user can |

Mutation probing finds unenforced invariants; only running the product finds
uncollected ones. Both are needed — and `CLAUDE.md` §Economy 2 bounds the probe
to **one pass** precisely because the probe is load-bearing and the recursion is
what needed stopping.

---

## History

- [ROADMAP-2026-08-24-part-a-and-history.md](archive/ROADMAP-2026-08-24-part-a-and-history.md) — the closed Part A (A0–A7) with all measurements, mutation results and file anchors intact.
- [RESET-2026-08-24.md](archive/RESET-2026-08-24.md) — why the repo stopped building and went looking for a user. The diagnosis behind `CLAUDE.md` §Economy.
- [HANDOFF-2026-07-10-to-08-23.md](archive/HANDOFF-2026-07-10-to-08-23.md) — the 3,251-line predecessor handoff.
- [HR_DEMO_SCRIPT-2026-08-22.md](archive/HR_DEMO_SCRIPT-2026-08-22.md) — how the product was framed for the HR/CIO session.
- [EXTRACTION_PLAN.md](EXTRACTION_PLAN.md) — the frozen v1 plan of record (Phases 0–7, all shipped). Left in place: 44 ADRs link to it.
