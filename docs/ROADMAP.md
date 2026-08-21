# Roadmap — HR pilot readiness, then next-gen features

> **⚠️ 2026-08-07 — this file now leads with PILOT READINESS.** An HR demo and pilot are imminent. The
> "wow features" menu is intact further down and remains the post-pilot plan, but **none of it should be
> built before the P0 items below.** Every finding here was verified against the working tree with file:line
> evidence; nothing is carried on assertion.

---

## Status at a glance — updated 2026-08-17 (A2's vocabulary merge LANDED as PR #88; `origin/main` is at or after `a0c3c17`)

> This heading pins no equality on purpose. Its previous version said A2 was *"on
> `feat/a2-vocabulary-families`, not yet merged"* while the A2 row two lines below already read **SHIPPED** —
> the branch updated the row and left the heading behind, which is the same stale-pin mistake PR #82 fixed in
> `HANDOFF.md`. **`git fetch` and check `gh pr list` before trusting any line here.**

| Item | State |
|---|---|
| **A0** Demo guardrails | **2 of 4 retired.** Guardrail 2 (accounts) and 4 (top-15 panel) are closed. 1 (curated JD) is **substantially weakened, not retired** — see A0 item 1. 3 (explainer circulation) stands. |
| **A1** Authorization | ✅ **FULLY CLOSED** — ADR-033 · **034** · **035**. All four original steps accounted for. |
| **A2** Skill matching | ✅ **Vocabulary merge SHIPPED — [ADR-042](adr/042-skill-vocabulary-domain-families.md).** 234 derived terms / 13 families merged into both vocabulary files; coverage of real SFU qualification statements 15.6% → 54.8%. **Competency-scoring model remains DEFERRED** (owner: corpus owner + HR, with pilot data) — deferring it did not block this merge; see ADR-042 §2. |
| **A3** The evals harness is blind | 🟡 **Two gates added** (ADR-038 bait ordering; the shipped `must_have_miss_penalty`, which could be switched **off** with the corpus green). **Structurally blind to the A2 vocabulary merge** — the corpus's 19 skill canonicals intersect the 225 newly-categorised ones at ∅; a green run on this branch is byte-identical to `main` and carries no information about A2's correctness. **Band enforcement is 🔴 blocked on a corpus-contract decision**, not on r18 as A3 assumed. |
| **A4** Two ranking defects + the cliff | ✅ **FULLY CLOSED** — ADR-**037** (M1) · **039** (M2) · **040** (the cliff, disclosed not removed). |
| **A5** Docs contradicting the code | Largely addressed as each item landed; the explainer was updated four times this session. |
| **A6** Before the pilot widens | 🟡 **Five scoring defects disclosed** — two in [ADR-041](adr/041-sub-score-measurement-markers.md) and three in the [ADR-041 addendum](adr/041-sub-score-measurement-markers.md#addendum--three-siblings-now-marked-and-disclosed-2026-08-18). Retention unenforced, unsalted email hash, audit immutability by convention, shallow `/health` still stand. |
| **A7** The pattern worth naming | **Twenty-one instances.** **(21), 2026-08-21, was found by the smoke suite's FIRST run and had left the product unable to rank anyone** — a measured lesson from ADR-044 recorded against one call site while four others kept smaller literals, including the one the whole product depends on.** **(20), 2026-08-20, is the longest-fuse variant yet — "the fix that never ran": ADR-032 shipped 2026-08-07 and had never applied to a single row, because every job predated it and the write only fires on projection. Recruiters saw salted hashes where skill names belonged for thirteen days, behind a green suite.** **(18) and (19) added 2026-08-20 by the D1=C branch, and (18) is the first found by neither mutation nor review but by RUNNING THE PRODUCT** — a form field that was never rendered, structurally invisible to the suite because the only test exercising it supplied the input itself. Three added A1b/A3, **three more added A6** — same escalation: (14), (15) and (16) all sat inside code whose purpose was to prevent them, and (16) was found by the reviewer *after* (14) and (15) were closed. **(17) came from the branch that disclosed the A6 siblings** — see A7 below.  |

**Read this before picking anything up — rewritten 2026-08-14 after a regroup.**

The previous version of this paragraph said *"A2 is the highest-value item and cannot start without a human
decision"*, and routed three consecutive sessions into adjacent hardening work. **It was wrong.** A2's
blocker is a competency-*scoring* question; the vocabulary work it was thought to gate is **additive,
scoring-math-neutral, and strictly better than the status quo** — an out-of-vocabulary competency today
scores `0.0` *and* trips the ×0.5 must-have penalty, so matching it can only improve matters. Nobody
checked whether the blocker gated the whole item. It cost a week on the highest-value P0.

**The measured cost of that trajectory:** since the v1 scope completed, 85 commits — **35 `docs`/`chore`
against 26 `feat`/`fix`** — and twelve ADRs, on a product **nobody has yet used end to end**.

**Priority order now:**

1. **A2 vocabulary merge shipped 2026-08-14 ([ADR-042](adr/042-skill-vocabulary-domain-families.md)).** The
   competency-scoring model stays deferred, deliberately, with the reasoning recorded in A2 below. The A3
   evals corpus cannot see this merge at all (its own entry below) — that is the next dependency, not more
   vocabulary work.
2. **Anything that makes the pilot actually run.** **Both decisions are now answered AND shipped** — D2 =
   option B (PR #95, 2026-08-19) and D1 = option C (2026-08-20, [ADR-036](adr/036-auditor-audit-log-viewer.md)
   amendment). What remains is purely physical: run `quickstart.ps1` under `pwsh`, and click through the
   live UI. Nothing is blocked on a human decision any more.

   **D1=C also produced the strongest evidence yet for why the physical half matters.** The reveal shipped
   green across 5,448 unit and 541 integration tests — and a live click found that the withdraw form never
   collected a reason, so the control would have had nothing to reveal, ever. Fifth defect in two sessions
   found only by running the product.
3. Everything else, and **only if it changes what the product can do**. Hardening a feature nobody has run
   is speculative. See CLAUDE.md's "Economy" section for the finding-disposition and stopping rules that
   now govern this — in particular: nits get **recorded, not fixed**, and mutation probing is **one pass**.

A3 remains the item that makes every scoring change trustworthy, and its seniority/vector control gap is
now measured — but it is a gate on work, not work that ships value, and it should not outrank A2 again.

---

# PART A — HR demo and pilot readiness

## A0. Demo guardrails — today, no code required

Four things that make the difference between a demo that lands and one that discredits the product:

1. ~~**Use a curated-vocabulary job description... do not demo against a real SFU posting.**~~ **WEAKENED,
   NOT RETIRED, 2026-08-14 — [ADR-042](adr/042-skill-vocabulary-domain-families.md).** Vocabulary coverage of
   real SFU qualification statements went 15.6% → 54.8%, so a real posting is far more viable than it was:
   roughly half of what a real posting asks for is now recognised, not one statement in six. **A real posting
   is still not a safe unscripted demo.** 45.2% remains a genuine long tail (the Phase 3.3 classifier's job; its
   résumé half shipped 2026-08-19 as ADR-044, but that half is credit-DISABLED by default and the
   45.2% is job-side work still to come), so an unscripted real posting can still surface a wall of red *"— missing · must-have"*
   chips for skills the candidate plainly has. If demoing against a real posting, pre-check its extracted
   requirements against the shipped vocabulary first, or say the constraint out loud: *"about half of a real
   posting's requirements are recognised today; the rest needs the classifier that's still open work."*
2. ~~**Sign in as admin or recruiter only.**~~ ✅ **RETIRED 2026-08-13 — all four accounts can now be
   issued.** Every reason this guardrail existed has been closed, in order, and each is recorded rather
   than assumed:
   - **Role escalation on writes** — ADR-033 (#68).
   - **The auth boundary shipped OFF**, so anyone reachable on the network had the whole API including the
     audit log — ADR-034 (#72, `299b529`). It can no longer be shipped off:
     `validate_startup_auth_config` refuses to boot CAS-enabled with zero role keys, every write requires a
     real CAS session, and `users.active` is enforced in all four session gates.
   - **CSRF covered 3 of 12 browser state-changing routes** — ADR-035 (#74, `b12ec84`). Now all 12,
     fail-closed, so route 13 is protected by default.
   - **An auditor account could not do its job** — **ADR-036**. This was worse than "the screen is
     missing": `audit_log` had **no read path anywhere in the application**, so producing an access record
     meant an engineer running SQL against production by hand. There is now a viewer, at `/audit`.

   **Operationally:** the stack refuses to boot until `./scripts/quickstart.ps1` is re-run to generate the
   keys — that is the fix working, not a break.

   **Before widening, one honest caveat** (not an authorization gap): nobody has clicked through the full
   live UI for ADR-035/036 end to end as a signed-in auditor. The **withdrawal-reason question is CLOSED** —
   D1 = option C shipped 2026-08-20. Reasons stay withheld by default and are revealable on request via
   `POST /audit/log/{id}/reveal`, each reveal separately audited (ADR-036's D1 amendment). That reveal path
   *was* exercised live, end to end, including the withdraw → withhold → reveal → re-mask cycle.

   **One residual worth knowing before issuing auditor accounts:** with CAS disabled, a reveal is recorded
   as `service:dev-anonymous` rather than a named person (ADR-019 §10b — the sentinel is not a `users` row).
   The attributability that justifies option C only materialises **with CAS on**, which is another reason
   the pilot must run authenticated.
3. ~~**Do not circulate `docs/process/ranking-metrics-explainer.html`.**~~ **RESOLVED — rewritten twice,
   2026-08-07 then 2026-08-09 (PR #70).** The first pass made it accurate; the second made it *useful*, after
   the reader's own verdict that it "reads like a chronicle of what is not working and technical build
   details, none of which really helps." It is now organised as *what it does · how to use it well · what you
   must decide*, with build detail (ADR numbers, fuzz ratios, mutation testing, corpus fixtures) removed
   entirely and the register reframed as **15 decisions** rather than a 23-item defect ledger. The second
   pass also added the **entire job side** — JD authoring, AI extraction of requirements, per-job settings,
   the one-way lifecycle, and reverse match — which the first pass had omitted, starting at "recruiter
   uploads résumés". **The load-bearing point for users:** candidates are scored against the requirements the
   system *extracted*, not the prose that was written, so reviewing the extraction is the highest-leverage
   habit available to them. A Markdown twin lives at `ranking-metrics-explainer.md`. **Every change to
   authz, skill matching, the evals gate or the evidence verifier must update both files** — the explainer is
   a circulated artifact, not a draft. Circulation is still gated on the remaining pilot-readiness phases by
   the human's own decision, not on the document's accuracy.
4. ~~**Stay in the top ~15 candidates when opening the "Why this rank?" panel.**~~ **RESOLVED 2026-08-13 —
   [ADR-040](adr/040-evidence-cliff-disclosure.md).** The panel no longer renders an `Evidence 0%` it never
   measured: a candidate below the cut-off shows **"not assessed"** and the page states that their headline
   score is not comparable with those above it. **The cliff itself still exists** — they really do lose 40%
   of the composite by where the work stops — so still read a below-cut-off score as *unassessed*, not as
   *weak*. That is now what the screen says, rather than something the demo-runner had to say for it.

## A1. P0 · Authorization — ✅ FULLY RESOLVED (ADR-033/#68, ADR-034/#72, ADR-035 — CSRF, step (iv))

> **All four original A1 steps are now closed.** (i) the test axis and (ii) `require_session_role` landed
> in ADR-033/#68; the fail-open that ADR-033 did not close landed in ADR-034/#72; (iii) is deliberately not
> built (dead code under the current role model — ADR-033 §5); and **(iv) CSRF is closed by
> [ADR-035](adr/035-csrf-on-every-browser-write-route.md)** — all 12 browser state-changing routes, up
> from 3, enforced by a fail-closed `before_request` hook so route 13 is protected by default.

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

**Full detail:** [ADR-033](adr/033-session-role-enforcement-on-writes.md). **CSRF (step iv):** remains
unscoped as a separate item (Phase 1.3).

### A1b. P0 · The auth boundary was OFF in the shipped config — ✅ CLOSED (ADR-034, PR #72, `299b529`)

**Status: FIXED 2026-08-13.** Found by the retrospective `reviewer` pass on `ab6c278`, independently
reproduced against the live stack, and closed by [ADR-034](adr/034-auth-boundary-fails-open.md). The
defect as it stood is kept below because it is the clearest instance of the A7 pattern in the repo, and
because ADR-033 had claimed to close exactly this.

**What shipped.** (i) **F1b, primary** — `validate_startup_auth_config` now **raises** on CAS-enabled with
zero role keys, and the channel it needed was built alongside it (`&app_env` forwards all four
`API_KEY_*`, `.env.example` defines them, `quickstart.ps1` generates them beside
`PII_KEY`/`SKILL_HASH_SALT`), so the boundary can no longer be shipped off. (ii) **F1a** —
`require_session_role` 403s on `user is None`, reversing ADR-033 §1; **human decision: a valid API key
alone is never sufficient for a write.** This meant rewriting 13 tests that *pinned the fail-open*, plus 7
more the tester found, including one asserting the exploit itself succeeded. (iii) **F5** — `users.active`
enforced in all four session gates, which none of them had consulted while `refresh_if_needed` slid
expiry forward on every request. (iv) **F4** — the 403→500 Flask regression ADR-033 introduced, fixed on
all six routes plus `resume_reveal`.

**Decided and implemented (2026-08-19, D2 = option B):** `require_role_assigned` now 403s on `user is None`,
closing the last unscoped-read case symmetrically with writes. See the amendment in
[ADR-034](adr/034-auth-boundary-fails-open.md#amendment--d2--option-b-closes-the-carried-question-dated-2026-08-19) —
the decision measured that no legitimate keyed tooling depends on bare-key unscoped reads, and both shipped entry points
(CAS-off dev boot, Flask viewer) are untouched. Also deliberately out of scope: F3 (three flaky reveal tests), F7
(dead `_EXISTS_SCOPED_SQL`).

<details>
<summary><strong>The defect as it stood</strong> (retained — the clearest A7 instance we have)</summary>

> **Tense warning:** everything below is written in the present tense and describes the code **before**
> PR #72. None of it is true of `main` today. It is kept verbatim as the record of what was found.

`auth_enabled` is `False` iff all four role keys are empty (`settings.py:253-263`), and no `API_KEY_*` is
written by `quickstart.ps1`, shipped in `.env.example`, or present in the running container. `resolve_role`
then returns `Role.ADMIN` for every request (`deps.py:102-103`), and both `require_role_assigned`
(`deps.py:299-301`) and the new `require_session_role` pass on `user is None`. **The two gates are ANDed, and
in this configuration both are vacuous.**

Reproduced with no cookie and no key against `:29800`, the address `CAS_SERVICE_BASE_URL` advertises to the
browser: `PATCH /jobs/{id}` → 404 (handler reached; a real id flips `blind_review`), `POST /jobs` → 422
(validation reached), `GET /auth/cas/user` → `authenticated: false`. Only three routes fail closed —
`reveal`, the two `assignees` routes, and the exempted `PATCH /users/{id}/role` — and all of them do so via
their **own** `user is None` → 403 gate, not via anything ADR-033 added.

**Why nothing caught it.** Config-dependent and every unit test mocks `resolve_user`, so the suite
structurally cannot see it. `validate_startup_auth_config` (`settings.py:281`) exists precisely to *"refuse
to boot on an auth configuration that would silently fail open"* — it checks stale legacy keys and key
collisions, but **not CAS-enabled-with-zero-role-keys**, which is the shipped default. The invariant is in
that docstring with nothing enforcing it: the same pattern as A7, third occurrence this session.

**Fix, Red first** *(as planned — all three shipped in PR #72; see the status block above)*: (i) extend
`validate_startup_auth_config` to refuse boot when CAS is on and no role key
is configured — the assertion that would have caught it; (ii) `quickstart.ps1` + `.env.example` generate and
write the role keys, mirroring how they already generate `PII_KEY`/`SKILL_HASH_SALT`, so the boot stays
one-command; (iii) decide whether human-only write routes should also 403 on `user is None`, mirroring
`reveal`/`assignees` — defence in depth, since the BFF's shared recruiter key would otherwise still permit a
sessionless write. **(iii) needed a human decision** on whether any legitimate non-browser caller exists —
**decided: no, for writes.** A valid key alone is never sufficient (ADR-034 §2). The same question for
*reads* (`require_role_assigned`) is the carried item above.

</details>

## A2. P0 · Skill matching — domain mismatch, not vocabulary shortage — ✅ VOCABULARY MERGE + PARSE-TIME CLASSIFIER SHIPPED (ADR-042 · ADR-044)

**Reframe, confirmed by measurement:** not "the vocabulary is too small" but **"the ontology was for the
wrong domain."** Measured from **1,802 real SFU canonical JDs** (9,176 qualification statements, 1,222
distinct titles, 449 departments): the shipped 72-canonical, software-engineering vocabulary recognised
**15.6%** of real qualification statements. `docs/process/skill-vocabulary/derived-families.yaml` (13
families, 234 corpus-derived terms — finance, student_affairs, academic_programs, research_admin,
human_resources, communications, governance_policy, leadership_management, analysis_reporting,
equity_indigenous, facilities_operations, interpersonal_core, health_wellness) had existed unmerged since
PR #69. **Merged 2026-08-14 — [ADR-042](adr/042-skill-vocabulary-domain-families.md).**
`categories.yaml` 19 → 32 families, `aliases.yaml` 72 → 306 canonicals, coverage **15.6% → 54.8%** (+39.2
points). Remaining gap: **45.2%**, a genuine long tail (MRI/MEG methods, microfabrication, study-permit
requirements, other role-specific knowledge) that needs the Phase 3.3 classifier, not more
curation. NOTE the shipped design is PARSE-time, not projection-time (ADR-044): the ADR-008 hash is
one-way, so classification must happen while the cleartext name is in hand and can never be run over
the graph afterwards. Earlier text here said "projection-time"; that design is impossible. Full mechanism (why an out-of-vocabulary skill scores `0.0`, the cap regression the merge
triggered, why `rest api design` must never get a family) is in ADR-042 — not repeated here.

**Parse-time skill-family classifier now shipped 2026-08-19 ([ADR-044](adr/044-skill-family-classifier.md)):** out-of-vocabulary skill names are now classified at parse time (where the LLM is already being called) into one of the 32 curated families, and their assignments are written to the `Skill.classified_categories` graph property. This stops the irreversible loss: previously, a hashed skill never earned family credit and could not be recovered later. The feature is **opt-in** — the default `match_use_classified_families=False` means the field is read but its value is ignored by the ranking engine's family-credit arm. Two live defects invisible to the full test suite were found and fixed:
1. `_CLASSIFY_MAX_TOKENS=1024` was a silent no-op (reasoning trace consumed the budget, 0 of 6 skills classified); raised to 4096.
2. The prompt did not signal non-matchable families as preferred for doubtful names, letting two domain-expert phrases misfire into matchable families.

**Two residuals block enabling the flag:** (1) Shared Skill nodes — two candidates with the same out-of-vocab phrase overwrite each other's `classified_categories`, and a re-parse that declines still leaves the prior value; needs to clear the field on every write and test the re-parse case. (2) A real accuracy measurement must run before flag flip — the eval corpus is blind to the classifier by construction (all 20 fixtures hold only in-vocab skills).

**Three corrections to anchors this section previously cited, found while writing ADR-042 (a fourth,
ADR-008's own residual #12, is corrected in that ADR directly):**
- Stage 2's family-credit arm (`reqSkill.categories IS NOT NULL`) is at **`orchestrator.py:428`**, not
  `:377`.
- The NICE_TO_HAVE-written-never-read fact (blocker #5) is documented at **`orchestrator.py:28-29`**, not
  `:426`.
- `hashed_total=288` (measured pre-merge) is now **`hashed_total=318`** against the current graph state —
  re-measure again before citing either number as current.

**Product decision — still DEFERRED, owner: corpus owner + HR, with pilot data.** Many of the merged terms
are **competencies** (communication, leadership, problem-solving), not named tools, and the scorer's
`years × recency × ontology_weight` model remains semantically odd for "three years of interpersonal
skills, last used 2024." This was not answered by the merge — it turned out not to need to be, because
adding a term is strictly better than the pre-merge status quo under every option on the table (score
competencies on a different model; exclude them from must-have penalties; decide per-competency as
curated) — see ADR-042 §2 for the full reasoning. **Still needs a real decision before the model is trusted
on postings that lean heavily on competencies, which at SFU is most of them.**

**Not addressed by this merge, unchanged:** the `must=True` edge question (ADR-009 residual) is an HR
policy decision, not engineering, and stays separate from the vocabulary work.

## A3. P0 · The evals harness cannot see what it grades

Every scoring fix above is only as trustworthy as the gate, and the gate is blind in three specific ways:

- **Blind to ADR-008 hashing by construction.** `run_evals.py::_skill_rows_for` reimplements the stage-2
  Cypher in Python and keys via `_basic_normalise`, so it can **never** produce an `h:` key. The only five
  labels the whole corpus emits are `airflow`, `docker`, `postgresql`, `python`, `rest api design`.
  *An attempt to close this was reverted* (ADR-032) because it inverted the bait-below-strong ordering while
  producing no hashed key at all. Doing it properly needs the non-vocab skill in `required_skills`, which
  forces a must-have miss for every honest fixture and re-bands the corpus — margins must be **re-measured**.
- **Assertions that do not run.** `expected_rank_band` is never referenced by `run_evals.py` — and r18
  currently violates its own declared band (tagged `strong`, band `{1,9}`, actual rank 11). The
  `skill_missing_must` ordering pair is inert against `weights.skill = 0`.

  > **Measured 2026-08-13, and two of these three claims need correcting.**
  >
  > **(a) There are TWO band violations, not one.** `r18` at rank 11 vs `{1,9}` (known), and
  > **`r19_jamie_okafor_recency_twin` at rank 9 vs `{10,15}` — undocumented.** Both invisible for the same
  > reason: nothing reads the key.
  >
  > **(b) 🔴 Enforcing bands is BLOCKED by a three-way contract conflict — a corpus-owner decision.**
  > A3 assumed reconciling r18 would unblock enforcement. It does not, because three existing contracts
  > cannot all hold:
  > 1. `test_evals_corpus` asserts `band == canonical band for the fixture's TAG` (bands are derived, not
  >    per-fixture data);
  > 2. `test_ordering_control_pair_members_share_the_same_tag` requires both halves of a matched pair to
  >    carry the **same** tag;
  > 3. the enforcement A3 wants requires each fixture to rank **inside** its band.
  >
  > Both offenders are matched-pair twins built with a **designed 0.144 gap** — large enough to cross a
  > tier boundary by construction. (1)+(2) forbid the twins from differing in tag or band; (3) requires
  > them to sit in different tiers. **Pick one contract to change.** Retagging (r18→borderline,
  > r19→strong) satisfies (1) and (3) and was tried — it breaks (2). Relaxing (2) for twins is probably
  > the smallest change, since a pair can isolate one dimension while still crossing a tier, but it
  > touches the mechanism every ordering control rests on. Not decided here.
  >
  > **(c) `weights.skill = 0` DOES fail the corpus** — via the *recency* pair (r19/r10), which is airtight
  > by byte-identical embeddings. The `skill_missing_must` pair is individually inert against it (the N-1
  > vector residual), but the corpus as a whole is not blind to it. The original wording implied otherwise.
  >
  > **(d) ✅ A different inert gate was found and CLOSED** — see the `must_have_miss_penalty` note below.
  ~~*"The bait is BELOW EVERY STRONG FIXTURE"* is prose, not a gated key.~~ ✅ **CLOSED 2026-08-13 —
  [ADR-038](adr/038-gate-the-bait-below-strong-ordering.md).**
- ~~**Recommended first move**~~ ✅ **DONE.** `[adversarial] must_rank_below_every_strong = true`, enforced
  as an order relation over tags. **Measured arming** (sweeping `weights.evidence` against the real corpus,
  recording which assertion fires first):

  | `weights.evidence` | What fires |
  |---|---|
  | 0.30 (default), 0.28 | GREEN |
  | **0.25 → 0.10** | **only this gate** — bait rank 11→7 vs worst strong 12→13 |
  | 0.05, 0.00 | `precision@k` (the bait reaches the top-5) |

  A real detection band of ~0.25–0.10 in which the bait outranks strong fixtures while **every pre-existing
  gate stays green** — halving the evidence weight used to pass the whole harness. `precision@k` and
  `must_not_surface_in_topk` only notice the bait once it reaches the top-5. **`expected_rank_band` is
  still deliberately NOT enforced wholesale** — it goes red immediately on r18, which needs its own
  reconciliation.

  🆕 **Measured drift in the threshold's own comment, 2026-08-14.** `thresholds.toml`'s prose for
  `must_rank_below_every_strong` states "~0.19 of margin either way." That figure is real but is a
  **different quantity**: it measures r09's margin to the k=5 5th-place score cutoff. The margin the
  gate actually enforces — r09 (the bait) vs r18 (the worst-scoring `strong`-tagged fixture, per the r18
  band violation noted above) — is **0.0237** (r09 `0.592398` vs r18 `0.616104`). Recorded here, not
  edited into `thresholds.toml`, since that file's number is not wrong for what it is measuring, only
  mislabelled relative to what a reader would assume it means next to this gate's name.

- ✅ **CLOSED 2026-08-13 — the shipped `must_have_miss_penalty` was ungated.** Not on A3's original list;
  found by mutating the corpus while investigating the above. Setting
  `DEFAULT_WEIGHTS.must_have_miss_penalty = 1.0` — switching the penalty **off**, so a candidate missing a
  required skill scores the same as one who has it — left the **entire corpus green**.
  `_assert_must_have_penalty_fires_on_r18` was meant to be the guard, but it builds its *own*
  `MatchWeights(0.5 / 1.0)` and never reads the shipped value: it proved the mechanism existed, not that it
  was in force. **The ADR-035 shape, one layer in.** Now reads the shipped weights. Armed against both the
  blunt mutation (`1.0` → fails) and the subtle one (`0.95` → fails); both were green before.

- 🆕 **Measured 2026-08-14 (A6/ADR-041): there is no ordering control for `seniority` or `vector` at all,
  and a total knockout of either passes the gate.** `[ordering_controls]` has matched pairs for education,
  overqual, motivation, `skill_missing_must` and recency — **none for the other two sub-scores**. Measured
  by mutation against the real corpus:

  | mutation | fixtures moved | max rank Δ | max score Δ | gate |
  |---|---|---|---|---|
  | `_most_recent_title` → always `None` (seniority wiped) | **8 of 20** | 3 | 0.09 | **exit 0** |
  | `vector_pool_is_degenerate` → always `True` (vector flattened to 1.0) | **6 of 20** | 1 | 0.06 | **exit 0** |
  | `_DEGENERATE_POOL_EPS` `1e-9` → `1e9` | — | — | — | **exit 0** |

  Two independent causes: the corpus pool has spread `0.4547`, so the degenerate branch is **never entered**
  (a permanently-disarmed predicate is *exactly* `0.0` corpus-neutral), and no threshold gates either
  dimension even when it is wiped entirely. **Closing it needs a seniority matched pair** (twins differing
  only in current-role title readability) **and a degenerate-pool control.** Deliberately not added on the
  A6 branch: new fixtures would have broken the byte-identity comparison that branch's non-regression proof
  rested on. Corpus owner's pickup.

- 🆕 **Measured 2026-08-14 — the corpus is structurally blind to the entire A2 vocabulary merge
  ([ADR-042](adr/042-skill-vocabulary-domain-families.md)).** The eval corpus's fixtures name 19 distinct
  skill canonicals, all software/tech, all pre-existing. The 225 canonicals A2 newly categorised (234
  derived terms minus the 9 that already shipped) intersect the corpus's 19 at **∅**. Family credit
  additionally requires intersecting the JD fixture's fixed skill-family set (`{backend, data, databases,
  devops}`), which gained zero members from the merge. Consequence, stated in the gate's own words: running
  `./scripts/verify.sh all`/`ranking-evals` against this branch produces a metric dump **byte-identical to
  `main`**, and that green **carries no information about A2's correctness** — it proves the merge did not
  break the pre-existing 19-canonical surface, nothing about whether the new 225 score sensibly. The fix is
  a corpus addition, not a code change: a JD fixture variant naming at least one derived-family skill, and a
  candidate fixture whose credit comes only through a derived family (not an exact match). **Corpus owner's
  call** — new fixtures re-band the corpus, same caution as every other corpus-owner item above.

**Still open in A3:** the ADR-008 hashing blindness (first bullet, and the largest — it re-bands the corpus
so every margin must be re-measured); **`expected_rank_band` enforcement, now blocked on the three-way
contract decision above rather than on r18 alone**; the seniority/vector control gap; and the **new A2
vocabulary blindness immediately above** — four separate blind spots now, not one.

## A4. P0 · Two ranking defects that affect what HR will see — ✅ FULLY CLOSED (M1 ADR-037 · M2 ADR-039 · evidence cliff ADR-040)

~~**M1 — stage 3 fails OPEN on a non-LLM exception.**~~ ✅ **FIXED 2026-08-13 —
[ADR-037](adr/037-stage3-fails-closed-on-non-llm-error.md).** `orchestrator.py` caught bare `Exception`
per candidate and set `results[id] = None`. For a **top-15** candidate that silently zeroed 40% of
`score_final` and persisted it, unmarked. The systematic evidence cliff provably *cannot* reorder the
displayed list; **this one displaced real candidates inside the visible top ranks**, only when a transient
Neo4j/Postgres hiccup happened to hit them — unreproducible afterwards, and on screen indistinguishable
from a candidate evaluated and found lacking.

Now fails closed into the existing `RankingUnavailableError` path (withhold → visible state → bounded
`arq.Retry`), which is what a transient cause needs anyway. Four functional lines, all in `except`
branches, so **scoring math is byte-unchanged by construction** — a successful run enters none of them.
The exception type is carried into `shortlist_state_reason` because `shortlist_state` is CHECK-constrained
to `awaiting_llm` and cannot distinguish an outage from a database blip from a bug.

**Side effect worth knowing:** `None` in `evidence_by_id` now has exactly one meaning — "nothing to
evaluate" or "past the cliff" — never "we tried and it broke". The two were previously indistinguishable.

**M2 — stage-1 recall is a global vector query.** `resume_summary_idx` (`neo4j_bootstrap.py:105`) is not
job-partitioned; `orchestrator.py:303-320` applies `WHERE r.job_id` *after* the global index returns its top
~150. Past ~150 résumés corpus-wide, a job's own candidates get crowded out **even when that job's pool is
well under `coarse_k`**. A pilot that loads several hundred résumés will hit this. **Raising `coarse_k` does
not fix it** and would mask it. **`ranking-evals` gated.**

> ✅ **FIXED 2026-08-13 — [ADR-039](adr/039-stage1-recall-is-job-scoped.md).** The index is not
> job-partitioned and *cannot* be — `db.index.vector.queryNodes` takes no pre-filter — so `WHERE r.job_id`
> ran **after** the global top-150 had already been chosen from the whole corpus.
>
> **Measured against a real Neo4j, and worse than this entry predicted:** a job with **5 applicants** — a
> pool one tenth of `coarse_k` — recalled **zero** of them once 300 résumés belonging to another job
> existed. Not crowding; total starvation.
>
> Now scores the job's own pool directly (`vector.similarity.cosine` over `MATCH (r:Resume {job_id})`,
> indexed by the existing `resume_job_id_idx`) — **exact** rather than approximate, and independent of what
> else is in the database. The `[0,1]` normalisation was verified against a real server to match what the
> index reported, since a raw cosine would have silently rescaled every `vec_score` into `score_final` with
> nothing failing. Eval-corpus ranking is structurally unaffected: `run_evals.py` never calls
> `stage1_coarse`.

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

> ✅ **DISCLOSED 2026-08-13 — [ADR-040](adr/040-evidence-cliff-disclosure.md). With this, A4 is fully
> closed.** An `evidence_evaluated` marker is now set from `top_k` membership on the write path (not
> re-derived from `rank` or `requirements == []`), folded into the `score_breakdown` jsonb like its two
> sibling sub-scores so no DDL was needed, and rendered in **three** states: assessed → `0%` stands,
> past-the-cliff → **"not assessed"**, legacy row → no claim either way. Both the evidence *and* motivation
> rows are marked, since motivation derives from the same stage-3 object, and the panel now states the
> consequence outright: the headline score is **not comparable** across the cut-off.
>
> **What this does NOT do — the cliff itself is still there.** Candidates past `evidence_k` still lose 40%
> of the composite for reasons unrelated to merit; upward mobility is still suppressed. This makes it
> *visible*, not *gone*. Removing it means evaluating every retained candidate (an LLM call each) or
> splitting structured screening from evidence-enriched ranking with distinct labels — ROADMAP item 2's
> territory and a product decision. **Reverse match still has the identical fabricated zero**
> (`match_reverse_evidence_k = 10`), untouched here per ADR-031's forward-only boundary.

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

~~**Two smaller scoring defects.**~~ ✅ **DISCLOSED 2026-08-13 —
[ADR-041](adr/041-sub-score-measurement-markers.md).** `normalise_vector_scores` returns `1.0` for
**everyone** on a degenerate pool (`stages.py:300-311`) — every single-candidate pool included, which
reverse match hits routinely — and `seniority = 0.0` whenever `_most_recent_title` comes back falsy
(`orchestrator.py:503-512`). Both are fallbacks byte-identical to a real measurement, and neither was
disclosed on screen. Now marked on the write path and disclosed on the read path, the same strategy
ADR-040 used for the evidence cliff, in **both** directions — there is no forward-only boundary here,
since reverse match calls the identical `_stage2_per_candidate`.

**Arithmetic unchanged: the defects are visible, not removed.** A candidate with no readable title
still loses the full 15%. The eval corpus cannot exercise either branch — all 20 fixtures have
distinct summaries and a titled current role — so a value change here would be unverifiable by the
gate, which is how ADR-032 earned its revert. One exception: `_most_recent_title` now falls back to
the first *titled* role (whitespace-only titles count as unreadable) instead of returning `None` when
the current role's title is blank. That raises **that candidate's own seniority sub-score** — not the
run, since a raised structured score can displace a *different* candidate below the stage-3 evidence
cut-off and lower *their* final score — and it is not even monotonic for that candidate: a title of
`"   "` previously embedded as literal whitespace and scored a garbage non-zero, and now scores `0.0`
marked unmeasured. That is the intent, not a regression. Provably corpus-neutral: all 20 fixtures
yield an identical title on `main`, before the remediation, and after.

**Disclosure reaches the entry-detail panel only.** The shortlist card tiles and the CSV export still
render the bare `0` / `100`. Recorded in ADR-041's residuals, and the explainer's register decisions 10
and 11 were narrowed to say "Why this rank?" page rather than "the screen" for exactly that reason.

**Still open, and owner-assigned:** renormalising the remaining sub-weights when a dimension is
unmeasurable. Needs new fixtures, corpus re-banding, and a product decision — should "no work history
at all" be neutral-weighted, or does it genuinely mean no seniority? Owner: corpus owner + HR.
Related and untouched: the reverse-match vector scale is unverified (`stage1_coarse_jobs` uses
`job_summary_idx` with no analogue of the forward path's index-normalisation integration test).

**✅ Three more of the same shape, found while fixing these two and NOW FIXED — [ADR-041 addendum](adr/041-sub-score-measurement-markers.md#addendum--three-siblings-now-marked-and-disclosed-2026-08-18).**
1. **`score_education`'s `if not ranked: return 0.0` (`stages.py:327`)** — D2 one dimension over, and
   the strongest of the three. Unparsed education scores `0.0`, which is *worse* than being below the
   bar (that earns partial credit via `education_partial`). Now marked with `education_readable` and
   disclosed on the "Why this rank?" panel.
2. **`score_experience`'s `if not jd_min_years: return 1.0` (`stages.py:222-223`)** — D1 one dimension
   over. A JD stating no minimum gives *every* candidate full marks on 25% of the score. Now marked
   with `experience_bar_stated` and disclosed when the bar is not stated.
3. **`score_education`'s `if not jd_min_level: return 1.0` (`stages.py:315-316`)** — the same, on 10%.
   Now marked with `education_bar_stated` and disclosed when the bar is not stated.

All three reuse `ScoreBreakdown`'s marker pattern directly. Implementation mirrors ADR-041's own two
markers, with one structural difference (same field names, not renamed) and one precedence rule (bar-not-stated
wins for education when both markers are false). See the addendum for full detail.

**🆕 Two findings from the branch that marked them, recorded not fixed.**

1. **The corpus already sits on defect 1, and the bands embed it.** `r07_alex_nguyen`
   (`"Certificate, Full-Stack Web Development"`) and `r08_riley_chen` (`"Diploma, Business
   Administration"`) match none of `_DEGREE_KEYWORDS` (`orchestrator.py:589-595`), so both score education
   `0.0` through the unreadable branch against a JD that *does* state a bachelors bar. The corpus's current
   bands were measured with those two at `0.0`. This is why changing that fallback is corpus-owner work:
   the gate **can** see the change (contrary to what a first draft of the ADR-041 addendum claimed), so the
   cost is re-banding, not unverifiability. Owner: corpus owner, alongside the sub-weight renormalisation
   already open above.

2. **Doc-anchor drift, five instances, in the ADR-041 addendum and this file.** Recorded as nits per
   CLAUDE.md Economy rule 1, none of them wrong about substance: the addendum and ROADMAP give different
   line ranges for the same statement (`stages.py:220-223` vs `:222-223`, both stopping one line short of
   the `return 1.0` at `:224`); two anchors start one line after the construct they name
   (`stages.py:284-296` for a `def` at `:283`, `explanation.py:143-150` for a comment block at `:142`);
   the addendum's "each scorer now **calls** the predicate" cites the predicate *definitions*
   (`stages.py:202-212, 280, 296`) rather than the call sites (`:223`, `:315`, `:326`); the docs quote
   `if not ranked:` / `if not jd_min_years:` / `if not jd_min_level:` as current source when the branch
   replaced those literal strings with predicate calls; and ADR-041's `## Gate state` numbers (4466 unit
   tests, 94.41%) are the original branch's, now sitting immediately after the addendum. Also nit:
   `test_education_levels_readable_true_for_whitespace_and_unrecognised_strings`'s
   `assert isinstance(pairs_call, float)` is vacuous — the predicate assertion above it is the one with
   teeth. Anchored here so a future session can sweep them in one pass rather than one per branch.

🆕 **2026-08-18 — First end-to-end product run surfaced a defect the suite could not see** — 
[ADR-043](adr/043-shortlist-ranking-state.md) (regenerate shortlist not updating UI). The API worked, the 
worker ran, the database persisted correctly, but the frontend's polling gate was broken (inferred "run in 
flight" from `not entries`, which fails for Regenerate). The entire unit + integration suite was green, because
it proves the API returns 202, the worker runs and the rows land -- none of which is the same claim as
"the screen updates". This is direct evidence for "get the pilot
running" being the highest-value item: the suite cannot see what only the product can show.

🆕 **Pre-existing boot-race defect found in review (ADR-043 §1b).** Postgres's native `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` clauses are not atomic against concurrent callers — on a fresh volume, `docker compose up` with `api` and `worker` booting together can leave one dead with a duplicate-object error, silently breaking the stack. Measured 100/100 failures in trials; measured 0/50 on existing relations (Postgres locks them). Fix: `init_schema` retries any statement raising a duplicate-object sqlstate (5 attempts, bounded loop). **Consequence:** first boot against a clean volume is now safe; operationally, this closes a silent failure mode on deployment. Severity is major because it leaves the stack exited with no log explaining why; the defect existed since the DDL was written but was only observable with concurrent schema initializers (both services boot together).

**Recorded residuals from the regenerate-shortlist review (ADR-043):** (1) the staleness bound is wall-clock, not job-time — a 2+ hour ranking would read stale after 1 hour (`shortlist_service.py:276-279`); (2) a second Regenerate during a run is silently dropped by the advisory lock with no user acknowledgement (`matching_tasks.py:80-87`).

Remaining open in A6: retention is stored but never enforced (`ddl.py:76-77`); revoke-and-purge is a
**recorded deferral** (ADR-026 §4) needing an HR decision; reverse match fails *open* at stage 3 and
is **unwrapped at stage 2** (reverse match starts at `orchestrator.py:883`, its stage-2 loop at
`:955-970`), so a stage-3-only fix leaves half the problem; blobs are permission-gated but unencrypted
(`blob_store.py:116-121`); the email hash is unsalted (`pii.py:101`) while the skill hash *refuses to
boot* unsalted (`skills_graph.py:276-282`) — the codebase disagrees with itself; audit immutability is
convention only (`ddl.py:317-338`); `/health` is shallow with no readiness, metrics, tracing or
correlation IDs.

## A7. The pattern worth naming

Across the external review and this session's gate work the same defect shape appears **twenty-one times**: an
invariant stated in a comment, docstring, ADR, threshold file or HR document, **with nothing enforcing it**.
The evidence cliff, `must=True`, the unenforced corpus assertions, the authz test axis that was never
exercised, the explainer's reveal claim, and the `Skill.display_name` cross-job leak are all instances. Every
one was invisible to a fully green 4,100-test suite and was found only by mutating the code and watching what
*failed to complain*.

**Two more, added 2026-08-13 by A1b/[ADR-034](adr/034-auth-boundary-fails-open.md)** — and the first of
them is the purest specimen yet: (10) `validate_startup_auth_config`'s docstring promised it refuses any
auth configuration that would *"silently fail open"*, while the shipped default — CAS on, zero role keys —
was exactly such a configuration and went unchecked; (11) `users.active` was a column the four session
gates all documented as meaningful and **none of them read**, while `refresh_if_needed` slid session
expiry forward on every request. Note the escalation: A7 instance (10) sat inside the very function whose
job was to prevent A7 instances.

**A twelfth, added the same day by [ADR-035](adr/035-csrf-on-every-browser-write-route.md)** — and this
one is the mirror image of the others, worth recording as its own variant: `frontend/csrf.py`'s module
docstring reads as *the* CSRF story for the application, describing the threat and the fix in full. The
fix was real and correct. It was wired to **3 of 12** state-changing routes. **Nothing was false; the
documentation simply described a control more general than the wiring.** The lesson generalises: an
invariant needs enforcement, and a *mechanism* needs a check that it is actually applied everywhere it
claims to be. ADR-035's structural test is behavioural for exactly this reason — an inert hook would pass
any introspective check.

**A thirteenth, found 2026-08-13 while investigating A3 — and it is the same variant as the twelfth,
which is the point.** `_assert_must_have_penalty_fires_on_r18` describes itself as the review obligation
covering the must-have-miss penalty. It compares `score_final` at penalty `0.5` versus `1.0` using
`MatchWeights` **it constructs itself** — so it proved the mechanism was wired while the *shipped*
`DEFAULT_WEIGHTS.must_have_miss_penalty` could be set to `1.0`, switching the penalty off entirely, with
the whole corpus staying green. Real control, correct implementation, checked somewhere that could not see
whether it was in force.

**Two of this session's three new instances were inside the gate itself** — instance (10) in the function
whose job is refusing unsafe configurations, and this one in the harness whose job is catching unsafe
scoring. That is worth stating plainly: *the assertions are subject to the pattern they exist to detect*,
and neither was found by reading the code. Both were found by mutating a value and watching what failed to
complain.

**A seventeenth, 2026-08-18, from the branch that disclosed the A6 siblings — and it is the fourth
consecutive branch whose own remediation shipped an unenforced invariant.** `score_education` materialises
its levels once (`levels = list(candidate_levels)`, `stages.py:320-325`) and the comment above it states
exactly why: the predicate and the `ranked` build must read the **same** sequence, because consuming a
one-shot iterable twice would silently desync them. The parameter is typed `Iterable[str | None]`, so the
hazard is real and reachable. Nothing enforced it. Deleting the `list(` survived all 349 tests in the
branch's own new A6 files — a green suite, on the line whose comment describes the failure it permits.

Found by the merge-blocking reviewer's mutation pass, not by reading the code, and now killed by
`test_a6_education_levels_materialised_before_readability_consumes_it` (verified by re-applying the mutant
and watching exactly that test, and only that test, fail).

**The pattern within the pattern is now the finding.** Instances (14), (15), (16) and (17) were all
introduced by branches whose stated purpose was to close instances of this shape. A remediation is not
exempt from the defect it remediates, and the only thing that has ever caught these is mutating the new
invariant and watching what fails to complain. That is why `CLAUDE.md`'s Economy rule 2 bounds the probe to
**one pass** rather than dropping it: the probe is load-bearing, and the recursion is what needed stopping.

**Two more, added 2026-08-13 by A6/[ADR-041](adr/041-sub-score-measurement-markers.md)** — the same
escalation. (14) The `seniority_measured` marker was computed from the branch taken, never re-derived from
the score; the re-derivation mutant (`seniority_measured = seniority != 0.0`) survived **4457 unit tests**
because every pre-existing test paired measured=True with 1.0 and measured=False with 0.0. Only a title that
*was* read and *honestly* scored 0.0 (orthogonal embeddings, clamped by rescale) separates them — the rarest
case in practice. Killed by `test_seniority_measured_is_not_re_derived_from_a_zero_score`. (15) The
`vec_discriminating` parameter defaulted to `None` ("unknown"), never `True` ("yes, this pool discriminated"),
because an unsupplied opinion should not manufacture an affirmative claim; the affirmative-default mutant
survived **4457 tests** because no call site omitted the kwarg. Killed by
`test_an_unsupplied_pool_opinion_is_unknown_never_an_affirmative_claim`. **Again, both sat inside code whose
explicit purpose was to prevent this pattern.**

**A sixteenth, and the sharpest — found by the merge-blocking reviewer *after* (14) and (15) were closed.**
Dropping the `not` from the **forward** call site's
`vec_discriminating = not vector_pool_is_degenerate(raw_vec_scores)` passed **4459 unit and 56 integration
tests**. A job with one parsed résumé would then have rendered `vector | 10% | 100% | 10%` as a measured
semantic match — the exact fabrication ADR-041 exists to close, reintroduced by deleting three characters.
The branch *had* pinned the marker wiring: for **reverse** match, which by ADR-041's own residual has no
rendering surface at all. **It pinned the invisible direction and left the visible one open**, while a comment
asserted that "both real call sites always pass it explicitly" as though that settled it. So *"both call sites
are wired"* was itself an unenforced invariant — the pattern one level above the markers, inside the branch
whose whole subject is the pattern. Closed by two forward-path integration tests, verified by kill-and-restore.

**The lesson that generalises past this instance:** two of the three were found only because someone
adversarially mutated code that was already green *and already reviewed for this exact defect class*. A
green suite plus an author who is actively hunting the pattern is still not enough; the reviewer found
what the author's own probe missed, because the author probed the invariants he had thought to write down.

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

**Pitch.** Turn the static *seventeen policy decisions* register (`docs/process/ranking-metrics-explainer.html`)
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

**An eighteenth and a nineteenth, 2026-08-20, from the D1 = option C branch — and the eighteenth is the
first one in this list found by *neither* mutation nor review, but by running the product.**

**(18) The withdraw form never collected a reason** (`frontend/templates/resume_detail.html:102`, before
this branch). `resume_withdraw` has always read `request.form.get("reason")`; the backend has always
accepted one (`WithdrawRequest.reason`, `schemas/resumes.py:512`); and
`test_resume_withdraw_route_forwards_the_reason_field` has always proved the forwarding worked — **by
POSTing the field itself**. No rendered form contained the input, so every withdrawal in the live database
recorded `reason = None`. The whole of D1 = option C would have shipped as a control to disclose a value
the product could not record.

This is a **new variant** worth naming separately from the twelfth's "mechanism wired to 3 of 12 routes".
Here the mechanism was wired end to end and correct at every layer — route, client, schema, service,
column. The only missing link was the **input a human types into**, and the one test that exercises the
field supplies it itself, which makes the gap structurally invisible to the suite. Call it *the test that
plays both parts*: a test that provides the input a user was supposed to provide can never notice that no
user can.

**(19) Both audit reads could 500 on one malformed row** (`services/audit_service.py`, before this branch).
`redact_audit_details`'s docstring has promised since Phase 1.4 that "an audit read must degrade, never
500" — and it does, for anything handed to it. But both reads called `json.loads` on the raw `jsonb`
column *before* handing it over, and a row whose text is not valid JSON raised first. One such row would
have taken down the whole access-record page. The classic shape: the invariant was written one layer below
where it needed to hold. Closed by `_decode_details`, which degrades fail-closed, plus the matching
`is mapping` guard in the template, which called `.items()` unconditionally on the same column.

**What (18) changes about how to hunt these.** Every previous instance was reachable by mutating code and
watching what failed to complain. (18) is not: there was nothing to mutate, because the defect was an
*absence* in a template, and no mutation of existing code makes an absent input appear. Mutation probing
finds unenforced invariants; only running the product finds uncollected ones. Both are needed, and the
second has now produced five defects in two sessions.

### Recorded, not fixed (D1 = C branch, 2026-08-20)

- **The shortlist card's quick withdraw still collects no reason**
  (`frontend/templates/shortlist_cards.html:151-158`). Same defect shape as A7 instance (18), left in
  place deliberately: a text input on every card in a shortlist grid is poor UX, and the résumé-detail
  form is where a considered note belongs. Consequence to accept: withdrawals performed from the shortlist
  still record `None`, so the audited reveal has nothing to offer for them. Revisit if pilot users turn out
  to withdraw mostly from cards.
- **Reveals are not rate-limited.** An auditor can reveal every withheld row one click at a time and reach
  option B's disclosure with a trail behind it. The trail *is* the control — C records access rather than
  preventing it — but nothing alerts on the pattern. Recorded in ADR-036's D1 residuals.

**A twentieth, 2026-08-20 — and it is the variant with the longest fuse: a fix
that shipped, passed every gate, and then never ran.**

[ADR-032](adr/032-skill-display-names.md) / PR #66 (`8bf7a16`, 2026-08-07) is
titled *"render JD-authored skill display names, not ADR-008 hashes"*. It writes
the JD's own wording onto each job's `REQUIRES`/`NICE_TO_HAVE` edge
(`worker/tasks.py::_job_projection_tx`), and Stage 2 renders
`coalesce(req.display_name, reqSkill.canonical_key)`. The code is correct. The
tests are correct. It has been correct for thirteen days.

**It had never applied to a single row.** That write only runs during *job
projection*, and the newest job in the live database was parsed **2026-08-02** —
five days before the fix landed. So all 160 `REQUIRES` edges carried
`display_name = NULL`, every shortlist fell through to the fallback, and a
recruiter saw `h:2431ff17cb58a88d057650c93758977d — missing · must-have` where
the word *Facilitation* belonged. Reported from the live product, with the
entirely reasonable follow-up "…although I rerun the generate shortlist":
**Regenerate re-ranks against the same edges, so the one control that looks like
it should fix this provably cannot.**

**Why this variant is worth naming separately.** (18) and (19) were defects in
code. This is not: the code is right, the tests pass, and the defect is that
*existing data was never migrated and nothing said so*. It is invisible to every
gate in this repo by construction — a test fixture is always freshly projected,
so it always has the property that production lacks. Call it **the fix that never
ran**: a change whose correctness depends on a write path that only fires on
new data, shipped without a backfill and without a check that would notice.

**Fixed in the data, 2026-08-20**, by a two-pass backfill that writes only the
label and never deletes or recreates an edge: pass 1 recomputed each job's
canonical keys from its own stored extraction (146 edges); pass 2 recovered the
remainder, whose keys had shifted under the [ADR-042](adr/042-skill-vocabulary-domain-families.md)
vocabulary merge, by matching `Skill.display_name` against **that job's own** JD
wording (26 edges). All 172 job skill edges across 15 jobs are now labelled.

Pass 2 stays inside orchestrator **S2**'s boundary deliberately: S2 forbids
*rendering* `reqSkill.display_name` because it is global/last-writer-wins and
could show job B's wording on job A's shortlist. The backfill uses it only as a
**join key** — an edge is labelled only when the node property matches a skill
name in that job's own extraction, and the value written is that job's own raw
wording, so a cross-job value can never be written.

### Recorded, not fixed (2026-08-20, second report)

- **Nothing detects "the fix that never ran".** No check reports job edges
  missing `display_name`, so the next projection-shaped fix will be inert for
  exactly as long before someone happens to look at a screen. A startup or
  health-check count of unlabelled edges would have surfaced this in a day.
- **There is no way to re-project a job without re-parsing it.** The remedy
  above had to be run by hand against the graph. `parse_job` re-runs the LLM and
  can change the extracted requirements, so it is not a safe "refresh the
  projection" control — which is why one is needed.
- **`shortlist_entries.score_breakdown` caches the rendered label**, so a graph
  backfill is invisible until each job is regenerated (55 entries still held
  hashed labels immediately after the backfill). Any future label change carries
  the same two-step.

**A twenty-first, 2026-08-21 — found by the smoke suite's FIRST real run, and it
had made the product unable to rank anybody.**

Three résumés uploaded through the UI; none of them got LLM skill extraction.
Two fell back to the keyword scan (`degraded`) and one failed outright, every
one logging `parse_resume.skills_llm_failed ... response content was empty
(possibly reasoning model exhausted token budget); reasoning_present=True`.
Degraded résumés are excluded from shortlisting (ADR-030), so the shortlist came
back **empty**. The most visible function of the product was inert.

The cause was one number: `resume_skills_v2` was called with `max_tokens=1536`,
and on `gpt-oss:20b` the discarded reasoning trace is charged against that budget
before a single byte of JSON is emitted (ADR-021 §6).

**What makes this an A7 instance rather than a plain bug is that it was already
known.** [ADR-044](adr/044-skill-family-classifier.md) / PR #94 hit the identical
failure on the skill classifier, proved live that 1024 classified 0 of 6 skills
while 4096 classified 6 of 6, and left a comment reading *"do not optimise this
back toward 1024 — that value was proven live to zero out the feature"*. The
lesson was recorded against **one call site** while four others kept their own
hand-picked literals (1536, 1024, 2048, 3072, 128) — and the one the entire
product depends on was among them. A hard-won, measured invariant, written in a
comment, enforced nowhere.

Closed by `REASONING_JSON_MIN_TOKENS` in `pipeline/llm/client.py` — one shared
floor, used by every extraction call — and by
`tests/unit/test_reasoning_token_floor.py`, which **scans the source** rather
than asserting per call. The failure mode is a new call site added later with a
hand-picked literal, and nobody writes a test for the call they forgot to think
about; a scan sees the ones that do not exist yet.

**The tool-building point.** This was the first thing `scripts/smoke.sh` ever
did, and no amount of further unit testing would have found it: the suite mocks
the LLM everywhere, by necessity. It is the third distinct defect class this
session has shown the gates are structurally blind to — the untested seam
(smoke), stale deployment state (doctor), and now **the real model's actual
behaviour**, which only a live call can show.

### Recorded, not fixed (2026-08-21)

- **`skills_graph.py:443`'s vocabulary tiebreaker still calls the model with
  `max_tokens=128`** — far below the proven floor, so on a reasoning model it
  almost certainly returns empty content too. NOT fixed here on purpose: it asks
  for a single token, it HAS a deterministic fallback (so it degrades resolution
  rather than emptying a shortlist), and raising it changes canonical-key
  resolution, which is a scoring-path change that does not belong in an incident
  fix. It is listed in `_RECORDED_EXCEPTIONS` in the enforcing test, so it is a
  decision with a reason attached rather than an oversight the scan tolerates
  silently. Worth measuring: if the tiebreaker has been silently inert, some
  share of skill resolutions have been falling back for a long time.
