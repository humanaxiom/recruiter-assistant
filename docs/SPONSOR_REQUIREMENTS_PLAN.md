# Sponsor requirements (DTO/CIO, 2026-09-02) — gap analysis and build plan

**Status:** in progress on `feat/sponsor-requirements`. **Source:** requirements
sent by the sponsor (DTO/CIO) on 2026-09-02, with all four open decisions
answered by them the same day (§0). This is **user-sourced work from the person
who owns the pilot**, so under `CLAUDE.md` §Economy 0 it outranks every
self-sourced item in [ROADMAP.md](ROADMAP.md) and it settles the "three framed
feature cards, none chosen" question — none of the three is what was asked for.
See §5.

---

## 0. Decisions — answered 2026-09-02

All four questions in §6 came back the same day. Recorded here verbatim in
substance, with what each changed. **Three of the four went against the plan's
recommended default**, which is why they are worth reading before the sections
below rather than after.

| | Question | Sponsor's answer | Effect |
|---|---|---|---|
| **1** | Where does work authorization come from? | **CSV shape is TBD.** Scaffold the column now; the **recruiter** fills it in, alongside the extra skills/must-haves not in the JD. **"Assign the 10% CL mark to this instead."** | §2.1 option **C promoted to primary**, A deferred until the CSV shape is known. And a *new* instruction the plan did not anticipate: the 10% taken off the cover letter goes to the **manager prompt**, not into a renormalisation. |
| **2** | Which host serves the job postings? | **"Taleo job import was implemented in `C:\repos\hris`. Let us bring it here."** | §2.3 jumps past both options: not "paste the URL" (A) and not "write an allowlisted fetcher" (B), but **port the existing, ADR'd implementation**. See §2.3 as rewritten. |
| **3** | Is there an internal SMTP relay? | **`mailhost.sfu.ca:25`** | §2.5 gets its **option B** — an on-network unauthenticated relay, which maps exactly onto hris's `SMTP_SECURITY=plain` mode. |
| **4** | Auto-reject — hidden or visible? | **"Last but visible. All candidates listed and marked. All other metrics are invalidated though, if candidate has no permit."** | Confirms the plan's recommendation, and **goes further**: the other metrics are not merely deprioritised, they are *invalidated*. That second sentence is what drives the `n/a` rendering and the dropped rank — see §2.1. |

### What has shipped so far

Branch `feat/sponsor-requirements`, off `main` at `ec2f2d2`.

| Slice | State |
|---|---|
| **S2** manager prompt (§I4) | **Done end to end** — field + DDL, the 10% weight move, `manager_prompt_v1` extraction, deterministic scoring, the create-form box, and provenance on the shortlist. **No edit path yet** (see the slice note). |
| **S4/S5** work authorization + cover-letter decoupling | **Done end to end** — column, audited write, API route, read-time band, résumé-page control, shortlist card, CSV export. |
| **S0** `FLASK_SECRET_KEY` | **Done** — generated, sourced from the environment, and refused at boot on a published default. **S6 is unblocked.** The pilot box still needs the quickstart run against it to rotate; see [ROADMAP open item 1](ROADMAP.md). |
| **Taleo import (§I3)** | **Code complete, DISABLED** — [ADR-046](adr/046-taleo-job-source-egress-carveout.md), parsers + fixtures, `TaleoClient` with its own host allowlist, the `jobs` source columns, the idempotent upsert and archive sweep, the daily cron and the admin trigger route. **`TALEO_ENABLED=false`**, and turning it on has three obligations the code cannot discharge — an enumerated firewall rule, counsel + privacy-officer sign-off, and a named owner. See ADR-046 §Consequences. |
| **S1** splitter · **S3** CSV · **S6** links · **S7** notify · **S8** posting URL | Not started. S6 is now unblocked by S0. |

Two findings from building it, both recorded because they change what a future
session should expect rather than because they were interesting:

- **The 10% move nearly broke every existing shortlist page.** `pipeline_meta.weights` is a historical stamp read back verbatim off each ranked row, and the read path validates *uncaught*. Every stamp on the pilot box carries `motivation: 0.1` and no `manager_prompt` key; the new field's 0.10 default made those sum to 1.10 → a 500 on every shortlist page for every job ranked before the change. Handled by a `mode="before"` validator that reads "names `motivation`, does not name `manager_prompt`" as a pre-feature stamp whose manager-prompt weight was genuinely zero. A payload naming both gets no forgiveness.
- **The eval corpus asserted the opposite of §O3.** An ordering control required that the cover-letter twin out-rank its twin. It went red, correctly. It was **inverted, not deleted** — the pair now asserts the two scores are *exactly* equal, which is strictly stronger than the control it replaced (whose rank-only half a prior finding had already shown was satisfiable by tie-break luck).
- **The new weight was declared, validated, and applied by nothing.** `manager_prompt = 0.10` passed its sums-to-1.0 validator and was surfaced on the breakdown, but neither combine site multiplied it in — so the blend actually applied summed to 0.9 and every `score_final` came out uniformly 10% low. **No gate could see it:** the deflation is uniform, so it reorders nobody, and `ranking-evals` is an ordering gate. Fixed, with a guard that drives every sub-score to 1.0 and asserts `score_final == 1.0` — shaped to catch the *next* unapplied weight without anyone remembering to update it.

### ⚠️ One decision taken by default that the sponsor may want to overturn

**A job with no manager prompt scores 0.0 on that term, so its candidates sit
10% below a theoretical 1.0.** The alternative is to renormalise the surviving
weights back to 1.0.

Renormalising is arguably the better answer, and it was deliberately *not*
taken, for two reasons:

1. **It is a recorded open decision owned by HR**, not by this change. ROADMAP §5: *"Renormalising the remaining sub-weights when a dimension is unmeasurable is open, and needs the same HR decision as item 3."* Settling it inside a change about something else is how a hiring policy gets rewritten by an implementation detail.
2. **Leaving it reproduces exactly what shipped before.** `motivation` held this same 0.10, and a candidate with no cover letter scored 0.0 on it — so a job with no prompt now scores byte-identically to the same job last week. **No live shortlist moves.** The pre-existing weighted-sum test passing unchanged is the evidence.

The mitigation is disclosure rather than adjustment, per ADR-040/041:
`manager_prompt_measured = False` marks the zero as "nobody asked" rather than
"the candidate matched nothing", so the explanation panel can say so. The
deflation is also pinned as ordering-neutral by test.

**If the sponsor wants the numbers to read as percentages of an achievable
maximum**, renormalisation is a small change in one function — but it moves
every displayed score on every job without a prompt, and that is a decision to
take deliberately.

---

## 1. The requirements, verbatim, against what the product actually does

Legend: ✅ shipped · ⚠️ partial or conflicting · ❌ absent.

| # | Sponsor requirement | Today | Evidence |
|---|---|---|---|
| **I1** | CSV of all candidates | ❌ | No candidate-roster ingest exists. The only CSV path is `parse_csv_manifest` ([bulk_ingest_service.py:357](../core/src/services/bulk_ingest_service.py#L357)) and it carries **job** metadata for bulk-JD upload, not candidates. |
| **I2** | Multiple PDFs, each with up to 20 résumés + cover letters | ⚠️ | `scripts/split_taleo_pdf.py` does exactly this (LLM segmentation → per-applicant PDFs + a `manifest.json` in the shape `parse_pairing_manifest` already consumes). **But it is untracked, it is not wired into the app, and as pushed it does not import in this repo** — see §2.0. One PDF per invocation. |
| **I3** | Link to the job posting req (JD + description) | ❌ | JD arrives by paste or file upload only ([jd_import_service.py](../core/src/services/jd_import_service.py), txt/json/pdf/docx). There is **no outbound HTTP fetch anywhere in `core/src`**. **ANSWERED:** port the existing Taleo import from the `hris` repo rather than writing one — see §2.3. |
| **I4** | Additional requirements as a free-text prompt | ❌ | `JobCreate` ([jobs.py:35](../core/src/schemas/jobs.py#L35)) has no such field; `JDExtracted` ([jobs.py:250](../core/src/schemas/jobs.py#L250)) derives requirements from the JD text alone. The only workaround today is editing `description_raw`, which pollutes the JD of record. |
| **S1** | Extract résumés from a multi-candidate PDF | ⚠️ | Same as I2. The hard part (LLM segmentation that does not split on "Professional Summary") is solved and validated; the delivery is an operator CLI, not a product feature. |
| **S2** | Long-running; let the user leave; notify when done | ⚠️ | The *work* is already durable and async (arq + Redis; `jobs.shortlist_state` is a recorded fact). What is missing is the notify half: **no SMTP, no webhook, no SSE, no in-app inbox** — grep for `notif\|smtp\|webhook\|EventSource` over `core/` returns nothing. The page must stay open and the poll gives up at ~20 min ([shortlist_cards.html:12](../core/frontend/templates/shortlist_cards.html#L12)). |
| **O1** | Candidates ordered by relevance to the prompt **and** the posting | ⚠️ | The 4-stage evidence-backed engine ranks against the posting. The "prompt" half does not exist (I4). |
| **O2** | Auto-reject those not eligible to work in Canada — rank them last | ❌ | **Nothing anywhere.** No work-authorization field in the DDL, the parse schema, the graph, or the ranking engine. `_ELIGIBLE_SQL` in [matching_tasks.py:73](../core/src/worker/matching_tasks.py#L73) is about *ranking* eligibility (is the row projected yet), not work eligibility. Ranking is a pure score sort ([orchestrator.py:1151](../core/src/pipeline/matching/orchestrator.py#L1151)). |
| **O3** | Identify whether a cover letter was included — **must not affect ranking** | ⚠️ **conflict** | Presence is stored (`resumes.cover_letter_blob_key`, [ddl.py:265](../core/src/models/ddl.py#L265)) and surfaced on the résumé list as `has_cover_letter` ([resume_service.py:443](../core/src/services/resume_service.py#L443)) — but **not on the shortlist card or in the CSV export** ([_CSV_FIELDS, shortlist_service.py:1195](../core/src/services/shortlist_service.py#L1195)). Worse: it **does** affect the ranking. `motivation` is **10% of `score_final`** and is computed from cover-letter evidence ([_motivation_score, stages.py:664](../core/src/pipeline/matching/stages.py#L664)). The product currently does the opposite of what was asked. |
| **O4** | The list should link to the PDF résumé and cover letter | ❌ | **There is no blob download route in the entire API.** Blobs are written to `BlobStore` and never served — grep for `StreamingResponse\|FileResponse\|send_file` over routes returns nothing. This is also a new PII-egress surface that interacts with blind review (§2.4). |
| **B1** | Blind review (optional) | ✅ **exceeds** | Blind-by-default per job (`JobCreate.blind_review = True`), rank-based pseudonyms, PII encrypted at rest, and an *audited* reveal (ADR-016/ADR-036). Nothing to build. Worth demoing back — this is stronger than "optional". |
| **N1** | Next version: highlight / long-list candidates | ❌ | No flag/promote concept. `withdraw` removes a candidate; there is no positive counterpart. |
| **N2** | Next version: notes (sponsor unsure — FIPPA) | ❌ | Nothing, and the sponsor's own hesitation is correct. Not planned. See §4. |

**Net:** five of the eleven live requirements are outright absent (I1, I3, I4,
O2, O4), one is implemented backwards (O3), two are solved but not delivered
(I2/S1), one is half-solved (S2), and two are already done (O1's engine, B1).

---

## 2. Decisions that must be made before or during the build

Each carries a **recommended default** so none of these becomes a bare "blocked"
line (`CLAUDE.md` §Economy 3). Work proceeds on the default unless the sponsor
says otherwise; the "if not" column is what changes.

### 2.0 The splitter as pushed does not run here — fix it first (no decision needed)

`scripts/split_taleo_pdf.py` is **untracked** (`git status` shows `??`) and was
written against the sibling `hris` layout:

- `from pipeline.config import get_settings` → this repo has no `pipeline/config.py`; settings live at `src.settings.get_settings` ([settings.py:387](../core/src/settings.py#L387)).
- `from pipeline.llm import LLMClient` → this repo's path is `src.pipeline.llm`.
- The docstring's `infra/docker-compose.yml` and `scripts/split-taleo.{ps1,sh}` do not exist here (`docker-compose.yml` is at the root; the wrappers are absent).

Everything else lines up: the `LLMClient.__init__` signature matches exactly, and
every `Settings` field it reads (`llm_base_url`, `llm_timeout_s`,
`llm_model_embedding`, `llm_max_retries`, `llm_breaker_threshold`,
`llm_breaker_cooldown_s`, `debug_llm`, `llm_ollama_native`) exists verbatim. So
this is a **three-line import fix plus a docstring correction**, not a port. Do
it in S1 and commit the script so it stops being untracked shell-state.

### 2.1 🔴 Where does "eligible to work in Canada" come from?

This is the highest-stakes decision in the set. It is an automated adverse
decision about a real person, on an attribute adjacent to national origin and
immigration status — protected grounds under the BC Human Rights Code. Screening
on work authorization is a lawful bona fide requirement; **inferring** it from a
résumé is not defensible and will be wrong often, because the signal simply is
not on most résumés.

| | Option | Assessment |
|---|---|---|
| **A** ⭐ | **Read the candidate's own declaration from the Taleo CSV** (Taleo asks this as a prescreen question). Never infer. | **Recommended.** The source is the candidate's self-attestation, it is auditable, it is what a human reviewer would use, and it is free once I1 lands. |
| B | LLM-infer from résumé / cover-letter text | Rejected. Fabricates a protected-ground decision out of absence of evidence, and violates the repo's own "never a number without a cited source" rule. |
| C | Recruiter marks it manually per candidate | Fine as a **fallback for rows the CSV does not cover**; useless as the primary at 200+ applicants. |

**ANSWERED — C is primary for now, A when the CSV shape is known.** The sponsor
deferred the CSV ("TBD") and made this a **recruiter-entered field**, alongside
the extra skills/must-haves. A stays the right eventual source and the column is
shaped for it, so adopting the candidate's own declaration later is a change of
*writer*, not of schema.

**The three-state field survives intact and is the load-bearing part:**
`eligible` / `not_eligible` / `unknown`, defaulting to `unknown`, which never
bands. Absence of a declaration must not be read as a negative; that is the
failure mode that turns this from a screen into discrimination. It is enforced
in three places — a `Literal`, a Postgres `CHECK`, and a `NOT NULL DEFAULT
'unknown'` that back-fills every pre-existing row — because a single unenforced
statement of it is this repo's characteristic defect.

**Rank last, or rank last AND hidden? — ANSWERED: visible.** *"Last but visible.
All candidates listed and marked. All other metrics are invalidated though, if
candidate has no permit."*

The second sentence went further than the plan proposed and is what drives the
rendering. It is not enough to sort the candidate last: the rank and the
sub-scores must stop being asserted. So an ineligible candidate keeps their card
and their evidence, and shows `—` for rank and `n/a` for every sub-score, with
the reason and a pointer to where it can be corrected. Showing "#3" and "78"
beside "no work permit" would state a merit position the product is
simultaneously disclaiming.

**`n/a` is deliberately distinct from the existing `—`**, which already means
"never recorded". Collapsing "we did not measure this" into "this does not
apply" is the same conflation the three-state field exists to prevent, one layer
up.

### 2.2 🔴 Cover letter: presence must stop affecting the score

The sponsor is explicit — identify it, do not let it rank. Today `motivation`
contributes 10% of `score_final`.

| | Option | Assessment |
|---|---|---|
| **A** ⭐ | Set `MatchWeights.motivation = 0.0` **per job**, renormalising structured/evidence to 0.667/0.333, and surface presence as a non-scoring badge | **Recommended.** Honours the requirement exactly, is a config change to an already-validated weights model (the `sums-to-1.0` validator, [matching.py:249](../core/src/schemas/matching.py#L249)), and is reversible per requisition. |
| B | Rip the motivation term out globally | Destroys a shipped, ADR'd capability for every future requisition on one requisition's requirement. Rejected. |
| C | Leave it and just add the badge | Ships a list the sponsor explicitly said not to ship. Rejected. |

**ANSWERED — A, and the sponsor said where the 10% goes:** *"Assign the 10% CL
mark to this instead"*, "this" being the manager's additional-requirements
prompt. That is better than the renormalisation the plan proposed, because it
keeps the blend at four terms summing to 1.0 with no sub-weight arithmetic, and
it spends the freed weight on something the hiring manager actually asked for.

**Shipped:** `MatchWeights.manager_prompt = 0.10`, `motivation = 0.0`, both
inside the sum validator. `motivation` is **kept, not deleted** — every
persisted `score_breakdown` on the pilot box carries a real motivation score,
and a requisition can opt the old behaviour back in per job.

**⚠️ The re-band happened, and the corpus told the truth.** `ranking-evals` is
merge-blocking, and its `motivation` ordering control asserted that the
cover-letter twin r04 must out-rank r16 by ≥ `min_score_gap`. Under the new
blend that gap is exactly 0.000e+00 and the gate went red — correctly, because
the control asserts the *opposite* of what the sponsor now requires.

It was **inverted, not deleted.** The pair moved to a new
`[cover_letter_neutrality]` gate asserting the two `score_final` values are
**exactly equal**. Three reasons that is the right shape:

- Deleting it would have left the new requirement enforced by nothing — this repo's characteristic defect, reintroduced by the change meant to honour the requirement.
- Exact equality is honest rather than conservative here: round-6 finding F5 already measured these twins' embedding input as **byte-identical** (`_build_summary_text` reads no `cover_letter_chunks`) and a motivation-blind engine on this pair at exactly `+0.000e+00`. There is no residual to tolerate.
- It is **strictly stronger** than the control it replaces, whose `rank(hi) < rank(lo)` half F5 had shown was satisfiable by tie-break luck. No tie-break can satisfy exact equality.

The threshold key set is a **three-way contract**, so `thresholds.toml`,
`run_evals.py` and `.claude/agents/ranking-evals.md` moved together, plus
`labels.json` (which carries the rationale) and `_THRESHOLD_KEYS`. A test pins
that the pair was inverted rather than dropped.

### 2.3 Fetching the job posting from a URL — ANSWERED: port it from hris

The plan offered "paste the URL for provenance" (A) or "write an allowlisted
fetcher" (B). The sponsor's answer supersedes both: **the Taleo job import
already exists in `C:\repos\hris` and should be brought here.**

That is a materially better answer than either option, because the hard part of
B is not the HTTP call — it is the egress carve-out, and that argument has
already been had and written down. What exists there:

- **`docs/adr/0012-taleo-egress-carveout.md`** — the decision itself, with three sync models weighed (external fetcher + file drop; whitelisted egress; an SFU-internal feed) and four alternatives recorded with reasons. It permits outbound HTTPS to **`tre.tbe.taleo.net` and only that host**, gated by `TALEO_ENABLED` **defaulting to `false`**.
- **`packages/pipeline/src/pipeline/sources/taleo.py`** — `TaleoClient` plus pure `parse_listing_page` / `parse_requisition_page` parsers, tested against **vendored HTML fixtures** (11 parser tests).
- **`apps/api/src/api/services/job_source_service.py`** (283 lines) — `upsert_external_job` on `(source, external_id)`, an archive sweep for postings pulled upstream, audit + cron-run rows.
- **`apps/worker/src/worker/taleo_sync_task.py`** (154 lines) — the daily cron, short-circuiting when disabled.
- Migration `0006_job_sources.py`, 13 helper tests, 6 testcontainer upsert tests.

**What the port must carry over, not just the code.** The carve-out is a
compliance-posture change: hris's own ADR records that production go-live with
`TALEO_ENABLED=true` needs **counsel + privacy-officer sign-off**, and that the
firewall must enumerate `tre.tbe.taleo.net:443` explicitly. This repo's
`CLAUDE.md` says *"NEVER add cloud API calls"* and ADR-012 §2 recorded the Taleo
scraper as **deferred**. Bringing it here therefore needs **an ADR of its own
that supersedes that deferral** — not a silent import. It passes all three of
`CLAUDE.md` §0a's tests comfortably.

**Two adaptations the port needs**, both already visible from the source:

1. hris scrapes **paginated HTML** and pins to landmarks (`rid=NNNN`, `dt`/`dd`) rather than positional XPaths. Its own ADR flags this as fragile and names the mitigation — vendored fixture tests plus a `taleo.sync.empty_listing` signal. Both must come across; a scraper with no empty-listing alarm fails silently the day the template changes.
2. hris deliberately **does not fetch the "Full Job Description" PDF** (Alternative D), capturing the URL in the description footer instead. That limitation matters more here than there, because §O1 ranks against the JD — a thin inline summary is a thin requirement set. Port as-is, then measure whether the summaries are usable before deciding to lift it.

**Still worth doing regardless:** `posting_url` on the job as recorded
provenance (the old option A, S8 below). It is ~2 hours, it is useful for
manually-created jobs the sync never touches, and it does not depend on the
carve-out landing.

### 2.4 Serving the résumé / cover-letter PDFs

O4 needs a download route that does not exist. Three things collide: blind review
(the blob **is** the identity), FIPPA, and the fact that the pilot box's session
key is the published default.

**Recommended default:**

- A single `GET /api/v1/resumes/{id}/document?kind=resume|cover_letter` streaming from `BlobStore`, whose path safety is already enforced ([blob_store.py](../core/src/storage/blob_store.py)).
- **Authorization reuses the existing per-job assignment scoping** (ADR-020) and the writer-role gate — not a new model.
- **Under blind review, a download *is* a reveal.** It goes through the same audited path as `resume_reveal` (ADR-016) and writes an audit row. On a blind job the shortlist links are *reveal* buttons, not bare hrefs. This is the only way the feature does not silently punch a hole in the blind boundary.
- `Content-Disposition: attachment`, correct content-type, no inline HTML rendering.

**This slice is gated on the `FLASK_SECRET_KEY` fix** (ROADMAP open item 1).
Serving candidate PDFs from a deployment where any visitor can forge a session for
any role is not a defensible thing to ship. That item is ~30 minutes and is
already the highest-severity line in the roadmap; it simply becomes a
prerequisite rather than a standalone chore.

### 2.5 What "notification" means offline

No SMTP client exists, and email is egress.

| | Option | Assessment |
|---|---|---|
| **A** ⭐ | **In-app notification centre**: a durable `notifications` table, a header badge, a "Recent runs" page. The user closes the tab and finds the result on return. | **Recommended first slice.** Zero egress, satisfies "leave the app and come back", reuses the outbox pattern already in the repo. |
| B | A + **institutional SMTP relay** (SFU's internal relay, not a cloud provider), subject line only, **no candidate PII in the body** | The literal "sends notification". Needs the relay host and credentials from the sponsor, plus a PII-in-transit review. Good slice 2. |
| C | Browser push / Web Notifications API | Needs HTTPS plus a service worker; the pilot box would need a cert. Nice-to-have, not first. |

**ANSWERED — B is available: `mailhost.sfu.ca:25`.** An on-network,
unauthenticated relay, which maps exactly onto hris's existing
`SMTP_SECURITY=plain` mode, so the transport needs no new design.

Build **A first regardless** — the durable `notifications` table is what makes
"leave the app and come back" work at all, and email is a *mirror* of it, not a
replacement. That is also hris's shape ([ADR-0019](../../hris/docs/adr/0019-email-egress-carveout.md)):
in-app rows first, a worker cron mirroring them to email second, delivery tracked
on the notification itself (`emailed_at`, `email_attempts`) rather than in a
separate outbox.

**Port hris's PII discipline verbatim, because it is the whole safety argument.**
Its `email_service.render(kind, config)` takes the notification *kind* and config
only — never the notification's context — so a body **cannot** embed candidate or
job data even by mistake. Every body is a static lead sentence plus a link into
the app. Recipients derive from the CAS username (`{cas_username}@{EMAIL_DOMAIN}`),
so no new PII column is needed, and an `EMAIL_REDIRECT_TO` valve lets
deliverability be tested before real addresses are wired.

Two differences from hris to settle at build time: it used an authenticated
external relay (IONOS, implicit TLS) and needed SMTP AUTH; `mailhost.sfu.ca:25`
is `plain` with no credential, so the secret-handling half of its design is
unnecessary here. And `EMAIL_ENABLED` must default **false**, exactly as there.

---

## 3. The build, sequenced

Nine slices. Ordering rule: make the sponsor's described workflow possible
end-to-end with the fewest manual steps, cheapest-first, and put the things they
have no workaround for ahead of the things they do. They can already split PDFs
by hand, so S1 sits mid-list; they have **no** way to express extra requirements
or see eligibility, so those lead.

Every slice: **TDD red→green→refactor**, `./scripts/verify.sh all` for anything
touching `models/ api/ services/ worker/` or Neo4j, plus `smoke.sh` for anything
that renders and `ranking-evals` for anything that scores. Per `CLAUDE.md` §0b
these batch into roughly **four PRs**, not nine.

### PR 1 — "Rank against what the manager actually wants"

**S0 · Prerequisite: generate `FLASK_SECRET_KEY`** *(~30 min)*
ROADMAP open item 1. Generate it in `quickstart.ps1` beside
`PII_KEY`/`SKILL_HASH_SALT`; refuse to boot on the literal `dev-only-change-me`
the way `validate_startup_auth_config` already refuses a CAS-on/zero-key config.
Gates S6.

**S2 · Additional requirements prompt (I4, O1)** *(~1 day)* — **SHIPPED**
The single highest value-per-hour item in the set.

> ⓘ **What building it changed.** Four things the framing below did not
> anticipate, and one it got wrong:
>
> * **The requirements are NOT merged into the job's requirement set.** The
>   bullet below says "merged into the job's requirement set and tagged with
>   provenance". That would have double-counted them inside the 40% skill
>   sub-score while the sponsor's separate 10% sat beside it, and it would have
>   put the manager's wording into the same list as the posting's. They stay
>   separate all the way to the screen: `manager_prompt_contributions` beside
>   `skill_contributions`, and a distinctly-styled "Added by you" chip group.
> * **Scoring is deterministic, not an LLM call.** A name comparison, not a
>   judgement — which keeps it explainable without re-running a model and,
>   load-bearing, keeps it outside stage 3's fail-closed path so a model outage
>   cannot silently blank 10% of every candidate's score.
> * **Matching leans on the vocabulary's FALLBACK, not its coverage.** This
>   field exists for what the posting missed, so terms typed here are
>   disproportionately out-of-vocabulary (open item 3: 54.8% coverage).
>   `_basic_normalise` returns unresolved names unchanged, so non-vocab terms
>   match on their normalised form. Exact after normalisation, never fuzzy.
> * **The wiring got its own test file.** Four links, each silent if broken —
>   which is exactly how the weight itself came to be applied by nothing.
>
> **Still open on this slice:** an **edit path**. `additional_requirements` can
> be set at create time; changing it afterwards — and re-extracting *without*
> re-running the JD parse — is not built. Also **unmeasured**: the prompt's real
> token budget. Run `model-check.sh` rather than trusting the 2048 literal; the
> floor is per-PROMPT, not per-model.

- `JobCreate`/`JobUpdate`/`JobOut` gain `additional_requirements: str | None` (max ~4000). Idempotent `ALTER TABLE` in `ddl.py`, matching the `withdrawn_at` precedent.
- A **second extraction pass** turns that free text into the same `Skill`/requirement shapes `JDExtracted` produces, merged into the job's requirement set and **tagged with provenance** (`source: "jd" | "manager_prompt"`) so the shortlist can show *why* a requirement is being scored — the repo's "never a number without a cited source" rule applied to the requirement side.
- Manager-prompt requirements are **must-have by default**, overridable. That is what "special skills I am looking for" means.
- Re-parsing `additional_requirements` alone must not re-run the JD parse — the JD side's `reparse` route re-runs the LLM and can change extracted requirements ([ROADMAP §5](ROADMAP.md)).
- **Tests:** prompt-only requirements appear in the breakdown with provenance; a job with an empty prompt is byte-identical to today; a 4000-char prompt does not blow the token budget — **measure it with `model-check.sh`, do not inherit the 8192 literal** (the floor is per-*prompt*, not per-model).
- **Gate:** `ranking-evals` with one new fixture pair proving a prompt requirement reorders the corpus in the intended direction.

### PR 2 — "Get the real Taleo batch in"

**S1 · Land and wire the splitter (I2, S1)** *(~2 days)*

- **Fix and commit the script** (§2.0): three imports, one docstring. It becomes tracked, gated code — `ruff`/`black`/`mypy --strict` apply, so budget for type work around the untyped `fitz` calls.
- Add the `scripts/split-taleo.ps1` / `.sh` wrappers the docstring already promises.
- **Then wire it into the app**: `POST /api/v1/jobs/{id}/resumes/split-upload` accepting **multiple** combined PDFs, running segmentation on the worker (it is an LLM call — minutes, not milliseconds), and feeding the emitted `manifest.json` straight into the existing `parse_pairing_manifest` → `pair_applicants` → `upload_resumes` path. **No new ingest path**; this is a producer for ADR-017's existing consumer.
- **A confirmation screen is mandatory.** LLM segmentation is good, not exact, and a mis-split silently attributes one person's experience to another. Render the proposal (applicant → résumé pages / cover pages / guessed name / `LOW TEXT` flag) and require the manager to accept before anything is ingested. The CLI's `--ranges` escape hatch becomes an on-screen page-range correction.
- **Tests:** a fixture combined PDF splits to the expected manifest; a `LOW TEXT` (scanned) segment is flagged and not silently ingested; segmentation failure falls back to the heuristic and says so; the confirmation step is *required* — an accept-less request ingests nothing.
- **Amend ADR-017** — do not write a sibling (`CLAUDE.md` §0a).

**S3 · Candidate CSV roster (I1)** *(~1.5 days)*

- `parse_candidate_csv` beside `parse_csv_manifest`, with the same hardening (size cap, case/space-insensitive headers, per-row `ManifestError` carrying a line number).
- Columns: candidate identifier, name, email, **work-authorization declaration**, cover-letter flag, and the Taleo attachment filename(s) for reconciliation.
- **Reconciliation is the real work.** The CSV row and the split PDF have to find each other. Match on attachment filename first (deterministic), fall back to email, then to normalised name. **Every unmatched row on either side is surfaced, never silently dropped** — that is ADR-017's existing "nothing is silently dropped" invariant and it must hold here.
- The CSV carries PII. It goes through the same encryption boundary as `candidate_name`/`candidate_email`, and is **never** embedded (ADR-008).
- **Tests:** all three match strategies; unmatched-in-both-directions surfaces; a CSV with no auth column yields `unknown` for every row (never `not_eligible`); formula-injection neutralisation on any cell that round-trips to the export.

### PR 3 — "The list the sponsor described"

> **S4 and S5 are DONE** and landed ahead of PRs 1–2, because the sponsor's
> answers made them the two slices with no remaining unknowns. What follows is
> the original framing, kept so the reasoning stays legible; the deltas from
> actually building it are in §0 and in the two ⓘ notes below.

**S4 · Work-eligibility band (O2)** *(~1.5 days, ADR)* — **SHIPPED**

> ⓘ **What building it changed.** The recruiter-entered path (option C) became
> primary rather than the fallback, per answer 1. The ADR is still owed — it
> passes all three of §0a's tests and must record *why inference was rejected*.
> Two things the design gained on contact with the code:
>
> * **The band is read-time, not persisted.** A correction re-bands the list on
>   the next page load rather than after a regenerate, and the declaration keeps
>   exactly one home. It is the primary sort key **in the query**, because
>   `shortlist_top_percent` caps the list — a Python post-sort would let the cap
>   evict eligible candidates to make room for ineligible ones.
> * **Do not JOIN `resumes` into the shortlist read to get it.** `_ENTRY_COLS`
>   selects bare `id`/`job_id`, which `resumes` also has → `AmbiguousColumnError`
>   on every non-blind read; and `WHERE job_id = $1` is the `.replace` anchor
>   `list_for_job` uses for FU-6 row scoping, so qualifying the columns would
>   have broken the scoping silently. A correlated subquery adds nothing to the
>   FROM clause and leaves every anchor byte-identical. **The unit suite could
>   not see this** — it asserts on the SQL as a string; `verify.sh all` caught it.

- `resumes.work_authorization` — three-state, default `unknown`, sourced from S3, manually overridable (audited).
- The engine's final sort becomes **band-then-score**: `not_eligible` rows sort into a trailing band with their scores intact and still visible. Not a score penalty — a penalty is a magic number that silently mixes with merit and is impossible to explain in a review.
- An on-screen reason on every banded card, and a matching column in the CSV export.
- **Mutation probe (one pass, `CLAUDE.md` §Economy 2):** the invariant "an `unknown` row is never banded" is exactly the kind this repo ships unenforced. Mutate it and confirm a test dies.
- **ADR required** — it passes all three of §0a's tests: live alternatives (§2.1), expensive to reverse (a persisted enum plus a ranking invariant), and reasoning that is unrecoverable from the code. It must record *why inference was rejected*.

**S5 · Cover-letter presence, decoupled (O3)** *(~1 day + corpus re-band)* — **SHIPPED**

> ⓘ **What building it changed.** The 10% went to the manager prompt rather than
> being renormalised away (answer 1). The corpus re-band happened as predicted
> and is described in §2.2 — the `motivation` ordering control was **inverted**
> into `[cover_letter_neutrality]`, not deleted. The ADR-009 amendment is still
> owed.
>
> **The one thing that nearly shipped broken:** `pipeline_meta.weights` is a
> historical stamp read back verbatim off every ranked row, and the API read
> path validates it **uncaught**. Every stamp on the pilot box reads
> `{structured 0.6, evidence 0.3, motivation 0.1}` with no `manager_prompt` key;
> the new field's 0.10 default made those sum to 1.10 → a 500 on every shortlist
> page for every job ranked before this change. A `mode="before"` validator now
> reads "names `motivation`, does not name `manager_prompt`" as a pre-feature
> stamp whose manager-prompt weight was genuinely zero — a statement of fact
> about the stamp, not a fudge. A payload naming both gets no forgiveness, so
> the shim cannot absorb a real misconfiguration. Both halves are pinned.

- Per-job `motivation` weight, defaulting to 0 (§2.2).
- `has_cover_letter` plumbed onto shortlist entries, onto the card (a neutral badge, not a chip that reads as a score), and into `_CSV_FIELDS`.
- **Re-band the eval corpus** under the new blend and record the movement. Do not weaken a fixture to make the gate green.
- **Amend ADR-009** (matching engine) rather than adding a sibling.

**S6 · Document links (O4)** *(~1.5 days, ADR, gated on S0)*

- The download route and the blind-review-as-reveal boundary described in §2.4.
- Links on the shortlist card, on the entry detail, and as URL columns in the CSV export.
- **Runs the `security` subagent as a merge-blocking gate** — this is a new PII-egress surface on a live box with four real users.
- **ADR required**: it is the first route that serves raw candidate documents, and the blind/reveal interaction is a cross-cutting invariant.

### PR 4 — "Leave the app and come back"

**S7 · In-app notification centre (S2)** *(~2 days)*

- A `notifications` table (recipient, job, kind, payload, `read_at`), written by the worker **in the same transaction as the terminal state change** — the outbox discipline already in `outbox_service.py`, and the reason a completed run can never fail to notify.
- Header badge plus a "Recent runs" page. Terminal states only: ranking complete, ranking failed closed, bulk ingest complete with its accepted/duplicate/rejected summary, split ready for confirmation.
- **No candidate PII in a notification** — job title and counts only. It is a surface a future email slice would forward verbatim, so the constraint has to hold from the first line.
- `doctor.sh` gains a check for notifications whose job reached a terminal state but which were never written. *This repo has shipped four state-only defects; a "notify" feature that silently does not fire is the fifth waiting to happen.*

**S8 · `posting_url` provenance (I3, §2.3 option A)** *(~2 h)*
Store and render the posting link on the job. Rides along in this PR. The
allowlisted server-side fetch (option B) is a **follow-on**, opened only when the
sponsor names the host.

---

## 4. Explicitly not in this plan

- **N2 · Candidate notes.** The sponsor's own instinct is right: free-text notes about an identifiable candidate are FIPPA-disclosable records, and they would be the first place a reviewer writes something that cannot be defended in a review. If it is wanted, it needs its own decision on retention, disclosure, and who can read it — not a text box. Raise it back to the sponsor as a question; do not build it.
- **N1 · Highlight / long-list.** Genuinely straightforward once S4–S6 land (it is `withdraw`'s positive twin, plus a filtered export for the hiring committee), and it is what the sponsor's closing paragraph — "forward long list candidates to the hiring committee" — actually needs. Deferred only because it should be built on the finished list rather than beside it. **Expect it to be the next thing asked for.**
- **Anything in [ROADMAP §5](ROADMAP.md) "carried residuals"** not named above. This plan does not license a pass over them.
- **The three framed feature cards.** See §5.

---

## 5. What this displaces

[ROADMAP.md](ROADMAP.md) ends with three framed-but-uncommitted cards — "Why this
rank?" slice 2, "Ask the pool", and Policy Studio — waiting on open item 2 ("let
the feedback channel pick"). **The sponsor has now picked, and it is none of
them.** The cards stay framed; this plan takes precedence.

Two roadmap items are absorbed rather than displaced:

- **Open item 1's `FLASK_SECRET_KEY`** becomes S0, because S6 cannot ship without it.
- **Open item 2 (capture what the users hit)** is partly answered: this document is the first user-sourced requirement set to reach the repo in writing. A standing `docs/pilot-feedback.md` is still worth ten minutes, because the four users' day-to-day friction is a *different* channel from the sponsor's feature list.

One item is made more urgent: **ROADMAP open item 3** (the 45.2% of a real
posting the vocabulary does not recognise). S2's manager prompt is, in practice,
the hiring manager routing around exactly that gap by hand. Watch whether prompt
requirements cluster on out-of-vocabulary terms — if they do, that is the
measurement the skill-family classifier has never had.

---

## 6. Questions for the sponsor — all four answered 2026-09-02

The answers are in [§0](#0-decisions--answered-2026-09-02). What they left open,
and what is now worth asking next:

1. **A sample Taleo CSV export.** Answer 1 deferred the CSV ("TBD") and made the
   recruiter the interim source, which is shipped. S3 stays blocked on knowing
   the real column names — in particular *whether the export carries the
   candidate's own work-authorization prescreen answer*, which is what would let
   the declaration come from the candidate rather than from a recruiter
   re-keying it. **One real export file settles it.**
2. **One real combined PDF**, to build S1's splitter confirmation screen against
   real segmentation behaviour rather than a synthetic fixture.
3. **Who signs off on the Taleo egress carve-out?** hris's ADR-0012 records that
   production go-live with `TALEO_ENABLED=true` needs **counsel + privacy-officer
   sign-off**, and that the firewall must enumerate `tre.tbe.taleo.net:443`. The
   port can be built and tested with the flag off; turning it on is not an
   engineering decision.
4. **May a notification email name the requisition in its subject line?** The
   relay (`mailhost.sfu.ca:25`) is settled. hris's ADR-0019 makes every body
   PII-free by construction — a static lead sentence plus a link, never candidate
   or job details — and the same discipline should hold here. Whether a *job
   title* may appear in a subject is the sponsor's call, not ours.

Both files are real PII → **`fixtures/`, which is gitignored by design; never
`git add -A` in this repo.**
