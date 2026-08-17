# Session Handoff — recruiter-assistant

Read this first if you're resuming cold. It captures state, environment quirks, and the exact next step. The full plan is [docs/EXTRACTION_PLAN.md](docs/EXTRACTION_PLAN.md) — this file is the orientation layer.

### ⚠️⚠️ READ FIRST — 2026-08-17: A2 IS SHIPPED (15.6% → 54.8%), AND A WEEK-LONG BLOCKER WAS MIS-SCOPED

> **Last feature merge: PR #88, squash `a0c3c17`, ADR-042.** Docs commits sit on top, so `origin/main` is at or
> *after* that sha rather than equal to it. **`git fetch` and check `gh pr list` before trusting any of this.**
>
> Supersedes the 2026-08-14 banner below (kept as history). **The four human-only actions from the
> 2026-08-13 banner are STILL ALL OUTSTANDING** — re-run `quickstart.ps1` with `pwsh`, click through the live
> UI, and two recorded decisions (auditor access to withdrawal reasons; unscoped reads for a bare service
> key). Three sessions have now passed them by. **Nobody has run this product end to end.**

#### The lesson that matters more than the feature

**A2 sat "blocked on a human" for a week. The blocker was mis-scoped and nobody checked.**

The blocker is a competency-*scoring* question. The vocabulary work it was thought to gate is additive and
scoring-math-neutral — A2's own Plan section said so all along. And adding a term is **strictly better than
the status quo under every option on the table**: today an out-of-vocabulary competency is hashed by ADR-008,
gets no alias resolution and no family credit, scores `0.0`, **and** trips the ×0.5 must-have penalty.

Three consecutive sessions re-noted the block and picked up something adjacent. Testing whether the blocker
gated the *whole item* took about ten minutes. **Measured cost of that trajectory: 85 commits after the v1
scope completed, 35 `docs`/`chore` against 26 `feat`/`fix`, twelve ADRs — on a product nobody had used.**

`CLAUDE.md` now has an **Economy** section with the four rules that came out of this, and PR #87 put the
matching guidance in `planner.md` (test whether a blocker gates the whole item; write a memo with a
recommended default, not a note), `reviewer.md` (severity is a routing decision — a nit filed as minor costs
a commit) and `docs.md` (proportionality; never overclaim in the circulated explainer). **Read them before
picking anything up.**

#### What shipped — PR #88, ADR-042

234 corpus-derived terms across 13 families merged into the shipped vocabulary. `categories.yaml` 19 → 32
families, `aliases.yaml` 72 → 306 canonicals. **Coverage of real SFU qualification statements 15.6% → 54.8%.**

**A merge, not a derivation** — the terms had existed at `docs/process/skill-vocabulary/derived-families.yaml`
since PR #69, derived and measured, never merged. Worth internalising: check for existing artifacts before
assuming work needs doing.

Terms go in **both** files. `categories.yaml` membership alone confers a cleartext canonical key, but the
résumé text scanner reads *only* the alias table — so a categories-only term is never found in résumé free
text. The nine terms that already "shipped" were categories-only and were never findable.

**Competency scoring is deferred, not answered.** `years × recency` remains semantically odd for "three years
of communication" and should still be revisited — with pilot data, not as a precondition.

#### 🔴 The regression this branch caused, and the shape to remember

`_MAX_SKILLS = 80` mirrored `ResumeParsed.skills` `max_length=80`. At 72 canonicals the deterministic scan
could never reach 80 — **the cap was unreachable by construction, and nothing stated or enforced that
invariant.** At 306 canonicals an ordinary administrative résumé scans to ~106 and a trailing
`TECHNICAL SKILLS` section is truncated away entirely:

> A posting requires Python. A candidate whose résumé lists Python applies. `det` fills all 80 slots with
> admin terms before reaching the technical block → no `HAS_SKILL` edge → `ontology_weight == 0` → `reason ==
> "missing"` → **the must-have penalty fires. The recruiter is told the candidate lacks a skill their résumé
> plainly lists** — on exactly the résumé population A2 exists to serve.

Fixed by merging LLM names first (they carry `years`/`last_used_year`, unrecoverable from a name-only scan)
and raising the cap to 400, above the 306 canonicals. **The eval corpus is blind to it** — max deterministic
scan across all 20 fixtures is 8 — and the existing cap test mocked the scanner and asserted only *that*
truncation happened, never that a real skill survived it.

**Then the fix did not pin its own invariants either.** Reverting the reorder, desyncing the two caps, and
lowering `_MAX_SKILLS` to 300 all passed the full suite. Four guards now kill each, including the
vocabulary-size coupling whose absence caused the original defect. This is the third time this session that a
remediation shipped its own gap.

#### 🆕 A3: the eval corpus is structurally blind to A2

Not a suspicion — measured. The corpus holds **19 distinct skill canonicals, all software/tech**; the 225
newly-categorised canonicals intersect them at **∅**; family credit requires intersecting the JD's fixed
`{backend, data, databases, devops}`, which gained **zero** members. The metric dump is byte-identical to
`main`. In the gate's own words, its green **carries no information about A2's correctness**.

What actually validated A2: the 1,804-JD coverage measurement and the fidelity tests, not the corpus.

**The fix is a corpus addition, not a threshold change** — a JD variant naming at least one derived-family
skill, with a candidate whose credit comes only through a derived family. Corpus owner's call. Note it could
not be done on #88: new fixtures would have destroyed the byte-identity comparison that branch's
non-regression argument rested on.

Also recorded: `thresholds.toml`'s `must_rank_below_every_strong` claims a ~0.19 margin; the real score margin
is **0.0237** (r09 0.592398 vs worst strong r18 0.616104). The 0.19 matches the margin to the k=5 cutoff, a
different quantity. `thresholds.toml` deliberately left untouched.

#### Guardrail A0.1 — weakened, NOT retired

A real SFU posting is far more viable at 54.8% than at 15.6%, but **45.2% remains a genuine long tail**
needing the Phase 3.3 projection-time classifier. The explainer says *"roughly half of what a real posting
asks for is now recognised"*, not *"it works on real postings"* — in both `.md` and `.html`, word-for-word.
Do not upgrade that claim without re-measuring.

#### What is left, in the order I would take it

1. **Get the pilot RUN.** The four human-only actions are the critical path and no agent can do them. This is
   the highest-value item in the project and has been for three sessions.
2. **The three A6 sibling defects** — `score_education`'s `if not ranked: return 0.0` is the strongest: an
   unparsed education section scores *worse* than being below the bar, which at least earns partial credit.
3. **A3's seniority/vector control gap** and the new A2-blindness gap — both measured, both corpus-owner work.
4. **A3's ADR-008 hashing gap** — still the largest; re-bands the corpus.
5. **A2's remaining 45.2%** — the Phase 3.3 projection-time classifier. Classification must happen at
   projection time: the ADR-008 hash is one-way, so categories cannot be backfilled later.
6. **Competency scoring** — now with real pilot data, if the pilot happens.

#### Environment notes

- **Use `pwsh` (PowerShell 7), not `powershell` (5.1), for `scripts/quickstart.ps1`.** 5.1 cannot parse it and
  `powershell.exe` still exits 0 on a parse failure, so breakage looks like a successful boot.
- **There is no `jq` on this host.** `gh pr checks --json … | jq` fails silently in a loop.
- **🆕 The integration suite flakes with `asyncpg` setup errors against testcontainers — now on a THIRD
  distinct, unrelated test** (`test_shortlist_scoping_pg`, on a vocabulary-only branch). An `ERROR` (not
  `FAILED`) at connection setup on a test your diff does not touch is probably this. **Prove it with a clean
  full re-run; never report the first red as "just a flake".** Three unrelated tests failing the same way at
  *setup* points at a port/container ceiling in local Docker — worth diagnosing, because "re-run it" will
  eventually mask something real.
- `./scripts/verify.sh` already handles stale bytecode (`PYTHONDONTWRITEBYTECODE=1` + `__pycache__` purge).
  Hand-rolled mutation probes must do the same and use `python -B`.
- Run mutation-testing gates **sequentially** — reviewer/security/ranking-evals all mutate the shared tree.
- **MSYS path conversion** bites hand-written `docker run` commands (`$(pwd -W)` → `/repo` gets rewritten).
  Another reason to use `verify.sh` rather than rolling your own.
- Standing orders unviolated: unique 29xxx ports · CAS on by default · inference offline-only on `aria-gb10`
  over Tailscale, no cloud call added.

#### (history) READ FIRST — 2026-08-14: A6's TWO SCORING DEFECTS ARE DISCLOSED; A7 IS NOW SIXTEEN

> **Last feature merge: PR #85, squash `14ba59f`, ADR-041.** Docs-only commits (including this banner's
> own) sit on top, so `origin/main` is at or *after* `14ba59f` rather than equal to it — stated that way
> deliberately, since a banner cannot name the sha of the commit that introduces it (the mistake #82 fixed).
> **Always `git fetch` and check `gh pr list` before trusting any of this.**
>
> Supersedes the 2026-08-13 banner below (kept as history). **The four human-only actions in that banner
> are ALL still outstanding** — re-run `quickstart.ps1` with `pwsh`, click through the live UI, and the two
> recorded decisions (auditor access to withdrawal reasons; whether a bare service key gets unscoped reads).
> Nothing this session touched any of them.

#### What shipped: PR #85 — ROADMAP A6's two smaller scoring defects

Two ranking sub-scores fell back to a number byte-identical to a real measurement — [ADR-040](docs/adr/040-evidence-cliff-disclosure.md)'s
evidence cliff one layer down:

- `normalise_vector_scores` returned `1.0` for **everyone** on a degenerate pool — every single-candidate
  pool included, which **reverse match hits routinely**. The panel showed `vector | 10% | 100% | 10%`: a
  perfect semantic match, when no comparison was possible at all.
- `seniority = 0.0` whenever no readable job title was found — indistinguishable from a genuinely poor
  title match, on 15% of the score.

Fixed the way ADR-040 fixed the cliff: **marked on the write path, disclosed on the read path, arithmetic
left alone.** Both directions — reverse match shares `_stage2_per_candidate`, and ADR-031's forward-only
boundary is about the display panel, not scoring.

**The defects are visible, not gone.** A candidate with no readable title still loses the full 15%. The eval
corpus provably cannot exercise either branch, so a value change would have been unverifiable by the gate —
the failure mode that got ADR-032 reverted. Renormalising unmeasurable dimensions is the deeper fix and is
an owner-assigned residual. One sanctioned value change: `_most_recent_title` now falls back to the first
*titled* role and treats a whitespace-only title as unreadable; corpus-neutral, verified fixture-by-fixture
three ways.

**Gates: reviewer APPROVE (2 rounds, 27 mutants) · security PASS (0 crit/high/med) · ranking-evals PASS with
the metric dump byte-identical to `main` (sha256 `f883529a…`).** `./scripts/verify.sh all` green, exit code
captured directly: **4466 unit @ 94.41%, 499 integration.**

#### 🔴 The finding worth carrying forward: A7 is now SIXTEEN, and three came from this one branch

All three sat **inside code written to prevent A7**, and they escalate:

| # | The unenforced invariant | How it survived |
|---|---|---|
| 14 | "marker set from the branch taken, never re-derived from the score" | Every test paired `measured=True` with a `1.0` score and `measured=False` with `0.0`, so the two were never separable |
| 15 | a parameter default that could not express "unknown" | No test called the function without the kwarg |
| **16** | **"both real call sites always pass it explicitly"** | Only the **reverse** site was pinned — the direction with *no rendering surface at all*. The visible one was wide open. |

**Instance 16 is the one to internalise.** 14 and 15 were found by a self-run mutation probe. 16 was found
by the merge-blocking reviewer *afterwards*, because the author had probed the invariants **he had thought
to write down**. A green suite plus an author actively hunting this exact defect class was still not
sufficient. **Keep the adversarial reviewer merge-blocking; a clean self-probe is not equivalent.**

The technique is cheap and worth repeating every branch: after green and before review, mutate each
invariant the branch itself introduces, run the full unit suite per mutant, revert. Then re-run the
survivors with **your own new tests deselected** — that last step is what separates "I added tests" from
"I closed a real gap".

#### 🆕 Two new measured findings, recorded not fixed

1. **A3: there is no ordering control for `seniority` or `vector` at all, and a knockout of either passes.**
   Measured against the real corpus: `_most_recent_title → always None` moves **8 of 20** fixtures and the
   gate exits 0; flattening vector to `1.0` moves **6 of 20** and exits 0; `_DEGENERATE_POOL_EPS 1e-9 → 1e9`
   exits 0. Two causes — the corpus pool has spread `0.4547` so the degenerate branch is never entered, and
   nothing gates either dimension even when wiped. Needs a seniority matched pair plus a degenerate-pool
   control. **Deliberately not added on #85**: new fixtures would have broken the byte-identity proof that
   branch's non-regression argument rested on. Corpus owner's pickup.
2. **Three sibling defects of the same family**, found by grepping the same two files:
   `score_education`'s `if not ranked: return 0.0` — **the strongest**, since an unparsed education section
   scores *worse* than being below the bar, which at least earns partial credit; `score_experience`'s
   `if not jd_min_years: return 1.0` (full marks for everyone on 25% of the score); and `score_education`'s
   `if not jd_min_level: return 1.0` (the same, on 10%). Marking all three is mechanically identical to
   ADR-041 and reuses the same pattern — a small, well-understood follow-up.

#### Disclosure reaches the entry-detail panel only

The shortlist **card tiles** and the **CSV export** still render the bare `0` / `100`. Recorded in ADR-041's
residuals, and the explainer's register decisions 10 and 11 were narrowed from "the screen" to the "Why this
rank?" page for exactly that reason — that document is circulated to non-engineers, so an overclaim there is
worse than one in an ADR.

#### What is left, in the order I would take it

1. **A2 — skill matching (P0, BLOCKED ON A HUMAN).** 231-term software-engineering ontology against an
   administrative/academic corpus: 15.6% coverage, 54.8% achievable. Blocked on a product decision, because
   many derived terms are **competencies** and `years × recency × ontology_weight` is meaningless for
   "three years of interpersonal skills".
2. **🔴 A3 band enforcement — BLOCKED ON A CORPUS-OWNER DECISION.** Three contracts cannot all hold; both
   offenders are twins with a designed 0.144 gap. Pick one contract to change.
3. **🆕 A3's seniority/vector control gap** (above) — smaller than the hashing gap, and now quantified.
4. **A3's ADR-008 hashing gap** — still the largest; re-bands the corpus, every margin re-measured.
5. **The three A6 siblings** (above), then the rest of A5/A6: retention unenforced, unsalted email hash,
   audit immutability by convention, shallow `/health`.
6. **Named follow-ups:** access-record export; reverse match's identical fabricated evidence zero; a
   fail-closed stage 3 leaves orphaned LLM calls (`asyncio.gather` does not cancel siblings); and ADR-040's
   own `evidence_evaluated: bool = False`, which still cannot express "unknown" — the same shape as A7
   instance 15, and now the obvious companion fix.

#### Environment notes

- **Use `pwsh` (PowerShell 7), not `powershell` (5.1), for `scripts/quickstart.ps1`.** 5.1 cannot parse it,
  and `powershell.exe` still exits 0 on a parse failure, so breakage looks like a successful boot.
- **🆕 There is no `jq` on this host.** `gh pr checks --json … | jq` fails silently inside a polling loop and
  looks like "no output" rather than an error. Use plain `gh pr checks` and grep.
- `./scripts/verify.sh` already handles the stale-bytecode hazard (`PYTHONDONTWRITEBYTECODE=1` plus a
  `__pycache__` purge). Hand-rolled mutation probes must do the same and use `python -B`.
- Run mutation-testing gates **sequentially** — the reviewer mutates the shared tree, so a concurrent gate
  reads a mutated tree and reports nonsense.
- Standing orders unviolated: unique 29xxx ports · CAS on by default · inference offline-only on
  `aria-gb10` over Tailscale, no cloud call added.

#### (history) READ FIRST — 2026-08-13: PILOT BLOCKERS A1 AND A4 ARE CLOSED; GUARDRAILS 2 AND 4 RETIRED

> **Last FEATURE merge: `7257c20` (PR #83). Working tree clean. Zero open PRs. Nothing is mid-flight.**
> Docs-only commits sit on top of that — including this banner's own refresh — so `origin/main` is at or
> after `7257c20` rather than equal to it. Stated that way deliberately: a banner cannot name the sha of the
> commit that introduces it, and every previous banner here pinned an `==` that was false the moment it
> merged. **Always `git fetch` and check `gh pr list` before trusting any of this.**
>
> Supersedes the 2026-08-07 banner below (kept as history, and itself stale — it describes #68 as
> "waiting merge" and A1 as partly open; both are false).
>
> This banner is a **state document**, not a changelog. The thirteen PRs that landed this session are
> summarised once, in the table, rather than narrated in sequence.

#### 🔴 Four things a human must do — none of them are agent-doable

*(A fifth decision — a corpus-design contract conflict — blocks A3's band enforcement. It is not a pilot
blocker, so it sits in the work queue at item 2 of "What is left" rather than here.)*

1. **Re-run `./scripts/quickstart.ps1`** (with `pwsh`, see below) before the stack will boot. The API now
   refuses to start without the four `API_KEY_*` values. **That is the fix working, not a break** —
   `.env` is permission-protected, so only you can do it.
2. **Click through the live UI.** ADR-035 (CSRF), ADR-036 (audit viewer) and ADR-040 (evidence-cliff
   panel) are proven by the full suite and, for the auth boundary, by live probes — but **nobody has seen
   the UI work**. The stack does not boot in the agent's environment without step 1. Particularly worth
   checking: the htmx controls (their CSRF token is inherited from an `hx-headers` attribute on `<body>`,
   exercised in tests through the header path rather than a real htmx runtime), the **Access record** nav
   link, and a below-cut-off candidate's "Why this rank?" panel.
3. **Decide: should an auditor be able to read résumé withdrawal reasons?** They are operator-typed free
   text about a named candidate, so they are **withheld** today behind a fail-closed allowlist (ADR-036
   §1). Plausibly within an auditor's remit; plausibly a PIPEDA/FIPPA problem. Recorded rather than
   answered by implementation.
4. **Decide: should a bare service key get unscoped READS?** `require_role_assigned` still passes on
   `user is None`, so it does today — **verified live**: with an admin key and no session,
   `GET /jobs` and `GET /audit/reveals-legacy` both return 200. ADR-034 §"carried" left this open
   deliberately. It is now observable rather than theoretical.

#### What shipped this session — ten PRs, all CI-green before merge

| # | What | ADR |
|---|---|---|
| 71 | Docs accuracy pass, retiring what #68–#70 made false | — |
| 72 | **The auth boundary was OFF in the shipped config** | [034](docs/adr/034-auth-boundary-fails-open.md) |
| 73 | The ADR + doc pass #72 shipped without | — |
| 74 | **CSRF on all 12 browser write routes** (was 3) — A1 step (iv) | [035](docs/adr/035-csrf-on-every-browser-write-route.md) |
| 75 | **The auditor's access-record viewer** — Phase 1.4 | [036](docs/adr/036-auditor-audit-log-viewer.md) |
| 76 | `quickstart.ps1` could not be parsed by the PowerShell it claims to support | — |
| 77 | **Stage 3 fails closed on a non-LLM error** — A4 M1 | [037](docs/adr/037-stage3-fails-closed-on-non-llm-error.md) |
| 78 | **Gate the bait-below-strong ordering** the corpus only asserted in prose — A3's first move | [038](docs/adr/038-gate-the-bait-below-strong-ordering.md) |
| 79 | **Stage-1 recall searches the job's pool, not the whole DB** — A4 M2 | [039](docs/adr/039-stage1-recall-is-job-scoped.md) |
| 80 | **Disclose the evidence cliff** instead of a fabricated 0% — closes A4 | [040](docs/adr/040-evidence-cliff-disclosure.md) |
| 81 | Session wrap — this banner, plus the ROADMAP status table | — |
| 82 | Stop this banner pinning a sha it cannot know | — |
| 83 | **Gate the shipped `must_have_miss_penalty`** (it could be switched off with the corpus green); record the band-enforcement blocker | — |

**Suite: 4401 unit @ 94.00% · 493 integration.** Every merge ran `./scripts/verify.sh all` with the exit
code captured directly rather than piped.

#### The four findings worth remembering

- **The auth boundary was open to unauthenticated callers.** No `API_KEY_*` existed in *any* channel, so
  `auth_enabled` was `False`, `resolve_role` returned `ADMIN` for every request, and both session gates
  passed on `user is None` — two gates ANDed, both vacuous. Proven live: `PATCH /jobs/{id}
  {blind_review:false}` → 200 with the column really flipped, then candidate PII read un-redacted, audited
  as `actor_service='api'` — unattributable. **Re-probed after the fix: every one now refuses.**
- **`audit_log` had no read path at all.** Nine call sites write it; `grep -rn "FROM audit_log" core/src/`
  returned nothing. Producing an access record meant an engineer running SQL against production — itself
  an unaudited read of the audit log.
- **Stage-1 recall was searching the whole database.** A job with **5 applicants** — a pool one tenth of
  `coarse_k` — recalled **zero** of them once 300 résumés belonging to another job existed. Not crowding;
  an empty shortlist.
- **Two ranking defects were invisible because a real `0.0` looks like a measurement.** A transient DB
  blip silently cost a top-15 candidate 40% of their score (A4 M1), and every below-cut-off candidate was
  shown an `Evidence 0%` nobody ever computed (the cliff).

#### Guardrails: 2 and 4 retired, 1 and 3 stand

- ~~**2. Sign in as admin/recruiter only.**~~ **Retired** — all four account types can be issued. Role
  escalation (ADR-033), the auth boundary (034), CSRF (035) and the missing auditor viewer (036) are all
  closed.
- ~~**4. Stay in the top ~15 when opening the panel.**~~ **Retired** — the panel no longer claims a
  measured `0%` for candidates it never assessed (040).
- **1. Use a curated-vocabulary JD — still stands.** A2 is untouched; real SFU postings still collapse
  skill scores. Unchanged.
- **3. The explainer is cleared for circulation — still true**, and it was updated four times this
  session. Every authz/scoring change must keep updating both `.md` and `.html`.

#### What is left, in the order I would take it

1. **A2 — skill matching (P0, BLOCKED ON YOU).** Not a vocabulary shortage: the shipped 231-term ontology
   is a software-engineering one against an overwhelmingly administrative/academic corpus, covering
   **15.6%** of real qualification statements. 13 corpus-derived families would lift it to **54.8%**. The
   blocker is a product decision: many derived terms are **competencies** (communication, leadership), and
   `years × recency × ontology_weight` is meaningless for "three years of interpersonal skills". Whether
   competencies are scored differently, or excluded from must-have penalties, is unresolved.
2. **🔴 A CORPUS-OWNER DECISION now blocks A3's band enforcement.** Measuring the bands (2026-08-13) found
   **two** violations, not the one A3 documented — r18 at rank 11 vs `{1,9}`, and **r19 at rank 9 vs
   `{10,15}`, undocumented**. But enforcing them is blocked by three existing contracts that cannot all
   hold: (1) `band == canonical band for the TAG`, (2) matched-pair members must share a tag, (3) each
   fixture must rank inside its band. Both offenders are twins with a **designed 0.144 gap**, large enough
   to cross a tier boundary by construction — so (1)+(2) forbid what (3) requires. Retagging was tried and
   breaks (2). Relaxing (2) for twins is probably smallest, but it touches the mechanism every ordering
   control rests on. **Pick a contract to change.** See ROADMAP A3's second bullet.
   *Also corrected there:* `weights.skill = 0` **does** fail the corpus (via the recency pair) — only the
   `skill_missing_must` pair is individually inert, not the corpus.
   *Also closed there:* the shipped `must_have_miss_penalty` was ungated — it could be switched off with
   the whole corpus staying green. Now armed.
3. **A3's largest remaining blindness — the ADR-008 hashing gap.** `_skill_rows_for` reimplements the
   stage-2 Cypher in Python and can never produce an `h:` key, so the corpus is blind to hashed skills by
   construction. Closing it needs a non-vocab skill in `required_skills`, which forces a must-have miss for
   every honest fixture and **re-bands the corpus — every margin must be re-measured**. A previous attempt
   (ADR-032) was reverted. This is the one A3 item that is genuinely large; the two smaller ones are done
   or blocked (see 2 above).
4. **A5/A6** — retention stored but never enforced, unsalted email hash while the skill hash refuses to
   boot unsalted, audit immutability by convention only, shallow `/health`. Plus two smaller scoring
   defects: `normalise_vector_scores` returns `1.0` for **everyone** on a degenerate pool, and
   `seniority = 0.0` on an unparseable title (a candidate loses the full 15% sub-weight to a *parsing*
   failure).
5. **Named follow-ups from this session:** an **export** for the access record (an auditor can read it on
   screen but cannot hand it to anyone); **reverse match carries the identical fabricated evidence zero**
   (`match_reverse_evidence_k = 10`, untouched per ADR-031's forward-only boundary); `asyncio.gather` does
   not cancel siblings, so a fail-closed stage 3 leaves orphaned LLM calls running.

#### Environment notes

- **Use `pwsh` (PowerShell 7), not `powershell` (5.1), for `scripts/quickstart.ps1`.** It was unparseable
  under 5.1 until #76 — and `powershell.exe` **still exits 0** on a parse failure, so the breakage looks
  like a successful boot. Fixed with a UTF-8 BOM plus a guard test; keep the BOM.
- **Two branch-name gate catches this session** from starting work on `main`. The gate did its job both
  times; branch first.
- Standing orders unviolated: unique 29xxx ports · CAS on by default · inference offline-only on
  `aria-gb10` over Tailscale, no cloud call added.

#### (history) READ FIRST — 2026-08-07: HR DEMO + PILOT STATE UPDATE

> **ROADMAP A1 — PARTLY fixed (PR #68 `ab6c278`, ADR-033). A SECOND, WORSE door is open: the auth boundary is OFF in the shipped config.**
> *(Superseded — see the 2026-08-13 banner above. #68 is merged and A1 is closed.)* A `require_session_role` dependency now gates every write route to
> admin/recruiter sessions only; a structural test guard prevents future write routes from reaching
> production without it. The human decision recorded: reveal is recruiter/admin only; the scoped
> hiring-manager reveal (FU-6 slice 6) is retired. **Demo guardrail 2 ("sign in as admin or recruiter
> only") is now ENFORCED IN CODE rather than a convention** — once #68 merges, issuing hiring-manager
> accounts during the pilot will automatically fail on every write attempt. See
> [ADR-033](docs/adr/033-session-role-enforcement-on-writes.md) for full detail, including why ROADMAP
> A1 step (iii) is deliberately not built (§5).
>
> **ROADMAP A2 (Skill matching) — REFRAMED by measured corpus findings.** Not "the vocabulary is too
> small" but "the ontology is for the wrong domain." Measured from 1,802 real SFU JDs (9,176 qualification
> statements, 1,222 distinct titles, 449 departments):
>
> - Shipped 231-term vocabulary is a **software-engineering ontology** (javascript, react, docker, kafka…)
>   against a corpus that is overwhelmingly administrative, academic, professional-services work.
> - Current coverage: **15.6%** of real qualification statements.
> - 13 new families derived from the corpus (finance, student_affairs, academic_programs, research_admin,
>   human_resources, communications, governance_policy, leadership_management, analysis_reporting,
>   equity_indigenous, facilities_operations, interpersonal_core, health_wellness): 234 terms.
> - Adding them lifts coverage **to 54.8%, a +39.2 point gain**.
> - Remaining 45.2% is a genuinely long tail of role-specific knowledge (MRI/MEG methods, microfabrication,
>   study-permit requirements).
>
> **Unresolved product decision that now gates the work:** many derived terms are **competencies**
> (communication, leadership, problem-solving), not named tools. The current scorer is `years × recency ×
> ontology_weight` — "three years of interpersonal skills, last used 2024" is not meaningful. Whether
> competencies are scored on that model, on a different one, or excluded from must-have penalties is
> unresolved. See [docs/ROADMAP.md](docs/ROADMAP.md) §A2 for the measured data and the revised plan.
>
> **Pilot readiness (unchanged).** The four demo guardrails remain; A1 defect is now code-fixed and merged (PR #68,
> green, waiting merge); A2 plan is revised to reflect domain-match findings; A3–A6 remain active and
> unchanged. Use [docs/ROADMAP.md](docs/ROADMAP.md) PART A for the full current state.

### (history) READ FIRST — SESSION 2026-08-04/05: "Why this rank?" defense pack, slice 1 (ADR-031) — gates green, PR pending

> **Branch `feat/why-this-rank-defense-pack`, HEAD `637c6bd`, off `main` @ `6d452e5`. Tree clean. All three
> merge-blocking gates GREEN locally (reviewer APPROVE, security PASS, ranking-evals PASS). PR pending — not
> yet opened; the human opens it next. `gh pr merge` is classifier-blocked in this environment (standing
> finding, unrelated to this branch) — drive the PR to green and hand the merge command to the human, don't
> attempt it.** This banner supersedes the 2026-08-02/04 "FU-7 §2/§4 + reproducible dev-boot" banner below
> (kept as history) and all older stale banners.
>
> **What shipped this session (`docs/ROADMAP.md` card #1, slice 1 of 2):** a **deterministic**
> score-composition + verified-evidence panel on the shortlist entry detail page
> (`GET /shortlist/<uuid:entry_id>`, both the API route and the Flask workflow-UI page). **No LLM, no DDL, no
> scoring-math change** — every number rendered was already persisted in `shortlist_entries.score_breakdown`
> / `evidence` / `pipeline_meta` jsonb; this slice only stopped throwing two of those three away on read.
> `ShortlistEntry` gained `score_structured`/`score_evidence`/`pipeline_meta`; a new pure module,
> `core/src/services/explanation.py::shortlist_entry_explanation`, is the **single source** of the
> `weight × score = contribution` arithmetic (no DB/LLM/clock/randomness, no `src.pipeline` import) — the
> Flask template renders its output and computes nothing itself. Full detail: **[ADR-031](docs/adr/031-why-this-rank-defense-pack.md)**.
>
> **The honesty decisions (the actual point of this slice — read ADR-031 in full before touching this
> surface again):** (1) weights come from `entry.pipeline_meta.weights` — the weights **in force when that
> row was generated** — never current settings/`DEFAULT_WEIGHTS`; this matters more once ROADMAP card #3
> (Policy Studio) potentially makes weights tunable. (2) no `pipeline_meta` → `weights_available=False`,
> every weight/contribution `None`, UI says "weights unavailable", never a silently substituted default.
> (3) a malformed stamp is treated as unavailable, never resurrected as `DEFAULT_WEIGHTS` — this was a
> **surviving mutant** the reviewer found (the invariant lived only in a docstring); now test-enforced.
> (4) an unrecorded sub-score renders "not recorded", never an affirmative "0%" — **and, symmetrically, a
> genuine `0.0` renders as "0", never "not recorded"**; the second direction was itself a surviving mutant
> introduced by the fix for the first (`_motivation_score` returns `0.0` for every candidate with no cover
> letter, so real zeros are the common case, not an edge case). (5) anti-fabrication verdicts (met/missing,
> scrubbed-quote demotion) are copied **faithfully** off `entry.evidence.requirements`, never re-derived.
> **Direction boundary (ADR-009 residual, unchanged):** forward-shortlist only — reverse-match's
> `score_final` tops out at 0.9 (no motivation term), so a shared panel would mislabel scales; the helper is
> named forward-only and reverse-match extension stays unscoped future work.
>
> **Privacy finding, recorded honestly:** the *original* black-box HTML PII scan for this panel was
> **inert** — it planted PII only in extra top-level keys the redaction whitelist drops and monkeypatched
> the API client, so it never drove a request through the real server-side redaction boundary
> (`shortlist_service._row_to_blind_entry`) at all, and it still passed with that boundary hypothetically
> removed. Replaced with `test_entry_detail_real_blind_read_renders_no_pii`, which drives a raw-PII row
> through the *real* redaction function, round-trips through `model_dump_json()`, and renders — killing both
> known redaction mutants (M1, M2). **No live PII leak ever existed**; the server-side boundary was
> mutation-proven throughout — the gap was only in one test's ability to prove it.
>
> **Gate verdicts, final, HEAD `637c6bd`:** reviewer APPROVE, security PASS (0 critical/high/unresolved
> -medium), ranking-evals PASS. `./scripts/verify.sh all` green: **4153 unit tests @ 94.03% coverage, 470
> integration tests**. ranking-evals proved ranking **byte-identical to `main`** — ran the corpus against a
> `main` worktree and diffed full-precision metric dumps: **identical md5sums**
> (`ada3e283774cc642cfccba9d3ff9994f`), precision@5 = 1.0, evidence verification rate = 1.0, r09 held at rank
> 12, all 5 ordering pairs enforced, determinism `max_score_delta` 0.0.
>
> **Accepted residuals (recorded, not fixed — see ADR-031 for full rationale):**
> - **PipelineMeta disclosure widened (security finding 2, Low, accepted).** The full `PipelineMeta`
>   (`model_gen`/`model_emb`/`prompt_versions`/`git_sha`/`timings_ms`/`weights`) now serializes to **all four
>   roles** on both the detail route and every entry of `GET /jobs/{job_id}/shortlist` — previously gated to
>   admin/recruiter, others saw only `git_sha[:12]` via the export. No PII, `extra="forbid"`, nothing
>   rendered into HTML, offline app, and the reproducibility trail is the point of an auditor's defense pack.
>   Optional later hardening (not done): truncate to `git_sha[:12]` for parity, or gate
>   `prompt_versions`/`timings_ms` specifically.
> - **Payload growth**: `pipeline_meta` on every shortlist list entry (~200-400 bytes/entry); no detail-only
>   response model split out for slice 1.
> - **Backend read path validates uncaught (security N-3, unreachable today)**: the new `ge=0, le=1` bound on
>   `score_evidence`/`score_structured` is a genuinely new rejection surface, proven unreachable for every
>   `MatchWeights` the model accepts across all 20 corpus fixtures — but a hand-corrupted row would still
>   raise uncaught on the **API** read path (the Flask frontend route degrades gracefully; the backend API
>   route does not). Joins a pre-existing family of uncaught read-path validates.
> - **Slice 2 deferred**: the optional grounded-LLM narrative and the PDF/timestamped decision-rationale
>   export named in the ROADMAP card are **not** in this slice.
>
> **Carried-forward corpus finding (NOT from this branch, present identically on `main`) — now recorded in
> `core/tests/evals/README_4c_twins.md` §6, owned by the corpus owner as a follow-up:** **N-1** — the
> `skill_missing_must` ordering pair is **inert** against `weights.skill = 0` (measured `+4.895691e-03` in
> `score_final` units on both input orders, ~4900× above `min_score_gap`); root cause is a vector-embedding
> residual from the `Skills:` line differing between `r18`/`r01`, not the intended arithmetic gap — same
> shape as the round-5/round-7 vector confounds already documented there. That README's §4 had explicitly
> flagged this as unmeasured; it is now measured. **N-2** (doc nit) — §4's stated `must_have_miss_penalty`
> gap for `r18` was written as ≈0.048; measured value is **0.096** (`0.6*0.40*0.40`); the obligation itself
> (`_assert_must_have_penalty_fires_on_r18`) was already green, this only corrects the prose.
>
> **Environment observation for the next session (not a regression):** the integration suite **flaked
> twice** on this box this session, on two unrelated tests —
> `test_auditor_read_logging_pg::test_get_resume_nonexistent_auditor_404_writes_zero_audit_rows`, then
> `test_matching_orchestrator::test_load_job_view_missing_fields_key_defaults_to_empty_tuple` — each passed
> clean on an identical immediate re-run. Worth knowing before treating a single unrelated integration
> `ERROR` as a real regression on this box.
>
> **Standing orders — verified unviolated this session:** unique 29xxx host ports (untouched, no compose
> change this session); CAS on by default (untouched); inference is offline-only on `aria-gb10` over
> Tailscale, no cloud call added (this slice added zero LLM calls — the whole point of "no LLM" scope).
>
> **Next session:** open the PR, get it merged (human-driven, `gh pr merge` is classifier-blocked — see the
> memory note), then either slice 2 of this card (optional grounded-LLM narrative + PDF/decision-rationale
> export, gate-proven to only cite verified quotes) or one of the other two ROADMAP flagship cards ("Ask the
> pool" NL search, Policy Studio). The N-1/N-2 corpus findings are the corpus owner's pickup, not blocking.

#### (history) READ FIRST — SESSION 2026-08-02/04: FU-7 §2/§4 + reproducible dev-boot (unique ports · CAS · peer LLM); everything merged

> **Current tip: `humanaxiom/main` == `8d664c3`. Both repos are PUBLIC. Zero open PRs. Working tree clean.
> Nothing is mid-flight.** This banner supersedes the 2026-08-01 "education field relevance (PR #49)" banner
> below (kept as history) and all older stale banners.
>
> **⚠️ Dev-boot is now REPRODUCIBLE on a fresh box (PR #60) — `scripts/quickstart.ps1` is the way in.** It
> writes the ENTIRE `.env` (secrets + unique 29xxx ports + inference config), verifies both models at
> `LLM_BASE_URL`, port-preflights, and boots with CAS on. `.env` is permission-protected (agent can't
> read/write it — the user/script does). **Inference endpoint is `.env`-driven** (`docker-compose.yml` reads
> `${LLM_BASE_URL:-http://host.docker.internal:11434/v1}` + `${LLM_TIMEOUT_S:-120}`). **Inference runs on the
> GPU host `aria-gb10` over Tailscale — there is NO local Ollama in this setup.** `.env.example` ships
> `LLM_BASE_URL=http://100.88.247.106:11434/v1` (aria-gb10's tailnet IP — the container resolves the IP, NOT
> the `aria-gb10` hostname; box must be on the tailnet) + `LLM_TIMEOUT_S=300`; the `host.docker.internal`
> default is only for someone running their own Ollama on the app box. **`compose.live-eval.yml` and the
> `-LiveEval` flag are GONE** — the endpoint is just `LLM_BASE_URL` now (one mechanism).
> **`compose.cas.yml` IS TRACKED (PR #58) — the old "untracked stray, leave it" guidance is DEAD;** CAS is
> ON BY DEFAULT (dev-anonymous-admin only via `-NoCas`/plain `docker compose up`, a BOOT MODE not a missing
> feature). **Standing orders (do not violate):** (1) UNIQUE host ports (29xxx), never stock
> 5432/6379/7474/7687/8000/5000; (2) never silently drop a feature (e.g. CAS) in tooling/config without asking;
> (3) inference runs on local/tailnet Ollama only — never a cloud endpoint.
>
> **What landed on `origin/main` this session (CI-verified green, then squash-merged):**
> - **Reproducible fresh-box boot (PR #60, `e6e35d9`).** `.env.example` is a complete copy-and-go template
>   (unique ports + peer LLM + timeout 300 + local alternative); `docker-compose.yml` parameterizes
>   `LLM_BASE_URL`/`LLM_TIMEOUT_S`/`LLM_MODEL_*`; `quickstart.ps1` writes the full `.env` and verifies both
>   models at the configured endpoint; removed the buggy untracked `compose.live-eval.yml` + `-LiveEval`.
>   README quick-start is now the definitive fresh-box guide. **Diagnosed live this session:** a shortlist
>   sat at `awaiting_llm` (FU-7 §2 fail-closed working AS DESIGNED) because LOCAL Ollama had **no models**;
>   repointing api/worker at the peer (which has them) → shortlist generated in ~60s. No feature code changed.
> - **Dev-boot fix — unique host ports + CAS on by default (PR #58, `f7dadc5`).** A stock `docker compose up`
>   hit `Bind for 0.0.0.0:8000 failed` (held by `bccb-api-1`) and, separately, booted CAS-off so RBAC/
>   user-management *looked* gone. Fix: `docker-compose.yml` host ports parameterized `${X_PORT:-<stock>}`
>   (only host side; in-network DSNs unchanged); `.env.example` ships a unique 29xxx block; `compose.cas.yml`
>   TRACKED + CAS URLs port-parameterized; `scripts/quickstart.ps1` writes the ports to `.env`, PORT-PREFLIGHTS
>   with a clear "port N held by <container>" message, and applies CAS by default (`-NoCas` to skip). Verified
>   live: stack up on 29xxx, frontend `:29500` 302→CAS login, API `:29800` `/auth/cas/login` → `cas.sfu.ca`.
>   No feature code changed. Ports (unique): API 29800, frontend 29500, pg 29432, redis 29379, neo4j 29474/29687.
> - **FU-7 §4 degraded-parse visibility (ADR-030)** — PR **#55**, squash `3df47de`. Closes the 2026-07-19
>   silent-partial-parse gap: when résumé SKILLS extraction hits `LLMOutputInvalidError`,
>   `_extract_skills_merged` falls back to the deterministic keyword scan; the parse is now flagged
>   `ResumeParsed.degraded`/`degradation_reason` (PII-free literal; persisted in the `resumes.parsed` jsonb —
>   NO DDL), and **excluded from ranking by SKIPPING the `resume.parsed` Neo4j projection enqueue** (mirrors
>   the ADR-026 withdrawn-during-parse skip → no node → absent from stage-1 recall), until a successful
>   re-parse. Made VISIBLE where it was blind: `ResumeListItem.degraded`, a `degraded` sub-count on
>   `ResumeStatusBreakdown` (⊆ `parsed`), `get_one` under blind+reveal (non-PII), UI badges on the résumé
>   detail/list/status-breakdown. Scoring math + outbox payload BYTE-UNCHANGED. **Residual:** no in-place
>   re-parse route (re-parse via re-upload today; dedicated `POST /resumes/{id}/reparse` deferred, ADR-030).
>   Degraded covers skills extraction only. TDD (red→green→test→docs); reviewer APPROVE (exclusion
>   mutation-proven), security PASS, ranking-evals PASS, `./scripts/verify.sh all` + CI green.
> - **AI-usage one-pager** — PR **#54**, squash `30c1c7f`. `docs/ai-usage-overview.md`: every LLM/embedding
>   call site, the 4-stage AI-vs-deterministic split, resilience, and the privacy boundaries, with 5 mermaid
>   diagrams. **Windows quickstart** — PR **#56**, squash `d336cd8`: `scripts/quickstart.ps1` boots the whole
>   stack (generates `PII_KEY`/`SKILL_HASH_SALT`, checks Ollama, `docker compose up -d`, waits for health).
> - **FU-7 §2 fail-closed ranking + §6 empty-content (ADR-029)** — PR **#52**, squash `79d69ac`. Closes the
>   silent zero-score penalty (ADR-009 residual / explainer register item 11): when the LLM fails during
>   ranking — **BOTH** Mode A (`LLMUnavailableError`, timeout/conn/5xx/429) **and** Mode B
>   (`LLMOutputInvalidError`, invalid/empty output), per the human decision — `generate_shortlist` now raises
>   `RankingUnavailableError` and `shortlist_job` WITHHOLDS the shortlist (does not persist silently-degraded
>   rows), sets a job-level `awaiting_llm` state (dedicated nullable `jobs.shortlist_state`/`_reason`/`_at`
>   columns, NOT a `job_status` enum value), and `raise arq.Retry` until `shortlist_max_tries` (bounded
>   1..1000), clearing state on success. New `GET /jobs/{id}/shortlist/status` (job-assignment-scoped) +
>   frontend "Waiting for AI to rank candidates…". §6: empty LLM content → `LLMOutputInvalidError` with a
>   reasoning-present diagnostic (both compat + native), inert `think:false` comment corrected. Scoring math
>   BYTE-UNCHANGED. **Security bug caught mid-build & fixed:** the new status route lacked FU-6/ADR-020
>   job-assignment scoping (IDOR + 404-vs-200 existence oracle) — fixed to mirror `get_job` (scoped-unassigned
>   == nonexistent → 404), security re-verified CLOSED. TDD (red→green→test→red→green→docs); reviewer APPROVE
>   (7 mutations killed), security PASS, ranking-evals PASS (corpus identical), `./scripts/verify.sh all` +
>   CI green. **Residual:** reverse-match keeps per-candidate isolation — fail-closed NOT extended there
>   (out of scope, ADR-029; follow-up mirrors the withdrawn-read split #43→#46). Same-provider retry until
>   FU-7 decision 1 (failover) lands.
>
> **Previous session (2026-08-01), now history:**
> - **Education field-of-study relevance (ADR-028)** — PR #49, squash `9229d61`; plus the explainer follow-up
>   PR #51 (`107e6bb`).
>
> **Older banner (2026-07-31), kept below as history:** This banner supersedes the "reverse-match read consistency (PR #46)" banner
> below (kept as history) and all older stale banners.
>
> **What landed on `origin/main` this session (CI-verified green, then squash-merged):**
> - **Education field-of-study relevance (resolve ADR-009 §7 / ADR-028)** — PR **#49**, squash `9229d61`.
>   `score_education` read degree LEVEL only and ignored `jd.education.fields` (open since Phase 4c). Human
>   decision: EXTEND the scorer. Now a candidate who MEETS the level bar but whose qualifying degree is in a
>   NON-allowed field is capped at `education_partial` (0.5) instead of 1.0; fuzzy field match
>   (`rapidfuzz.token_set_ratio ≥ weights.education_field_fuzz`, new knob default 0.85, settings-wired as
>   `match_education_field_fuzz`). A JD with no `fields` stays level-only (unchanged); below-level candidates
>   unaffected; unknown/blank candidate field counts as no-match (penalized — decision + counter-risk in
>   ADR-028). Corpus impact measured: only r06/r12/r17 demoted (all already `must_not_surface`), precision@5
>   stays 1.0, r14/r11 twin gap byte-identical; new mutation-killed `_assert_education_field_relevance`
>   control. TDD (red→green→lint-chore→evals-control→docs); reviewer APPROVE (live mutation proof), security
>   PASS, ranking-evals PASS, `./scripts/verify.sh all` + CI green. **ADR-028 added; ADR-009 §7 RESOLVED.**
>   *Small follow-up left open:* `docs/process/ranking-metrics-explainer.html`'s policy-decision register is
>   now stale by one (the `education_field_fuzz` / unknown-field decision).
>
> **Previous session (2026-07-31), now history:**
> - **Reverse-match read hides withdrawn** — PR **#46**, squash `6d8d33f`. The mirror of PR #43, one read
>   path over: the reverse-match (candidate → jobs) persisted read `get_reverse_match_result` /
>   `_REVERSE_MATCH_QUERY` in `shortlist_service.py` still returned a withdrawn candidate's rows written
>   *before* withdrawal (the write path `reverse_match_job` was already withdrawal-aware). Added a correlated
>   `NOT EXISTS (… r.id = rm.resume_id AND r.withdrawn_at IS NOT NULL)` guard (`_REVERSE_NOT_WITHDRAWN_SQL`,
>   mirroring the non-blind-shortlist `_NOT_WITHDRAWN_SQL`): a withdrawn candidate's whole reverse-match read
>   collapses to the empty shape, reinstate restores it from the same rows, correlation on `rm.resume_id`
>   means A's withdrawal never empties B's read. Scoring byte-unchanged. **With this, all five persisted read
>   paths (four shortlist + reverse-match) + export hide withdrawn consistently** — the ADR-026 read-side
>   residual is fully closed. TDD (3 integration tests vs real Postgres, RED → GREEN); all three
>   merge-blocking gates green (reviewer APPROVE w/ mutation evidence, security PASS, ranking-evals PASS);
>   `./scripts/verify.sh all` + CI green. ADR-026 amendment 2026-07-31.
>
> **`sfu-aria` mirror note:** NOT re-synced to `6d8d33f` this session (last synced at `69e6ac0`). It's the
> redundant billing workaround (option 7 below); sync or retire it only if you still want it live.
>
> **What landed on `origin/main` this session (all CI-verified green, then squash-merged):**
> - **Cover-letter zip pairing fix** — PR **#42**, squash `1b7c40d`. `bulk_ingest_service._classify` only
>   matched underscore suffixes, so a zip whose cover letters used spaces/dashes (`Jane Smith Cover
>   Letter.pdf`) parsed the résumés but silently ingested the covers AS résumés. Now separator-agnostic
>   (space/dash/underscore) + a ReDoS stem-length cap (security). ADR-017 amendment.
> - **Shortlist read hides withdrawn** — PR **#43**, squash `0412eea`. FU-8 un-projects a withdrawn candidate
>   from Neo4j (new shortlists exclude them) but the persisted `shortlist_entries` read had no filter → a
>   withdrawn candidate kept showing. All four shortlist reads + the export now filter `withdrawn_at IS NULL`;
>   withdraw hides immediately, reinstate restores from the same row. ADR-026 amendment (closes the "rely on
>   regenerate" residual).
> - **FU-7 honest parse status (ADR-021 §3 / ADR-027)** — PR **#44**, squash `69e6ac0`. `uploaded→parsing`
>   claim at task start + `parsing→failed` on retries-exhausted, fixing the "16 résumés stuck at `uploaded`"
>   incident. **Key correction (verified against `arq==0.28.0`): a bare uncaught exception does NOT retry in
>   arq — only `raise arq.Retry` does**, so the old code failed on the first outage and stranded the row;
>   ADR-021 §3's premise was wrong (recorded in ADR-027). Remaining FU-7 (decisions 1 failover / 2 fail-closed
>   ranking UI / 4 empty-content) still DEFERRED.
>
> **CI billing (unchanged from last session, still true):** GitHub Actions on `humanaxiom` (`team` plan) had a
> **payment failure** that blocked all runs at *Set-up-job*. The fix was making the repos **PUBLIC** —
> public-repo Actions run free regardless. CI runs green in the cloud (integration ~5 min). **Billing only
> needs funding if you want the repos PRIVATE again.**
>
> **`sfu-aria/recruiter-assistant`** is a **detached standalone PUBLIC mirror** (kept synced to `69e6ac0`),
> the billing workaround — redundant now that `humanaxiom` CI works, but harmless. Local safety backups
> `backup/main-pre-reconcile` + `backup/chore-pre-reconcile` still exist (from the 2026-07-29 reconciliation).
>
> **Live exposure while public (accepted, temporary):** `recruiter@sfu.ca` in `test_schemas_{jobs,resumes}.py`
> and the owner email in git history are publicly visible. When finance funds `humanaxiom` billing,
> re-privatize (`gh repo edit <repo> --visibility private --accept-visibility-change-consequences`; CI then
> needs that org's Actions quota funded) and/or run the `recruiter@sfu.ca → @example.test` scrub.
>
> **Next session: pick from "## Next session" below.** Natural follow-ons: **ADR-026 §4 revoke-and-purge**
> (destructive consent-erasure — needs a human decision + its own security review); **remaining FU-7**
> (decisions 1/2/4, ADR-021); **reverse-match read consistency** (extend PR #43's withdrawn read-filter to the
> reverse-match persisted read — small); `jd.education.fields` (ADR-009 §7); re-privatize + PII scrub. None
> auto-starts — a human picks.

## What we're doing

Building a **local-first recruiter assistant**: evidence-backed resume ranking → shortlists, fully offline. We are **porting the resume-ranking feature out of `C:\repos\hris`** onto a golden template, stripping the review workflow and JD-Harmonizer, and replacing MinIO with filesystem storage. See the plan for the keep/cut boundary and the 4-stage ranking algorithm.

## Repos & locations

| Thing | Location |
|---|---|
| **This project** (the real thing) | `github.com/humanaxiom/recruiter-assistant` (private) — local `origin` points here |
| Working copy | `C:\repos\recruiter-assistant` — on `main` |
| **Golden template** (frozen, don't build here) | `github.com/adamsalah13/agent-harness-template` (private, is-template) |
| **Source to port FROM** | `C:\repos\hris` (Python 3.12 uv monorepo; the ranking feature lives in `packages/` + `apps/api` + `apps/worker`) |

The working copy now holds the **ranking-domain foundation** (Phases 0–2: infra + storage + schemas) on the template chassis, all merged to `main`. The template demo app is gone.

## Current state

**Done:** repo created + `origin` repointed + pushed; 4 decisions locked; plan-of-record and the `data-pipeline` + `ranking-evals` subagents committed. **Phases 0, 1, 2, and 3 are all complete and merged to `main`, CI green:** Phase 0 (seed & infra) via PR #1 (merge `8b2b47c`), Phase 1 (storage) via PR #2 (merge `f7e7cbe`), Phase 2 (schemas) via PR #3 (merge `cefd545`), Phase 3 (ingest + parse) via PR #6 (merge `49196d7`). Phases 0–2 merged 2026-07-11; Phase 3 merged 2026-07-12. **Phase 4 (Ranking engine) is ✅ complete — all 4 gated sub-phases merged to `main`.** Sub-phase **4a (evals corpus) is MERGED to `main` via PR #8** (merge `875eac2`), CI green, 2026-07-12, and its **falsifiability hardening is also MERGED via PR #10** (merge `464a479`), CI green. **Sub-phase 4b (graph projection) MERGED to `main` via PR #11** (merge `68fe821`), CI green. **Sub-phase 4c (matching engine) is MERGED to `main` via PR #12** (merge `fd12d1a`), CI green. **Sub-phase 4d (shortlist + reverse-match write path) is MERGED to `main` via PR #13** (merge `5945320`) this session, CI green. **Phase 5 (persist + anonymize + export — read/list/get/export + display redaction) is MERGED to `main` via PR #14** (merge `6deade3`), CI green. **Phase 6 (API routes — job create/read/list/status, résumé upload/read/list, shortlist generate/list/get/export, reverse-match, configurable auth) is COMPLETE and MERGED to `main` via PR #15** (squash merge `e910669`, CI `gates-all` fully green, merged 2026-07-17), tip `837de9e` — all three merge-blocking gates were green (reviewer APPROVE, security PASS, ranking-evals PASS) AND CI's `gates-all` (offline `run_evals.py` running inside the gated unit suite — CI never calls a model endpoint; inference is host-only by design) went fully green before merge. **Phase 7 (evals + minimal Flask viewer) is now MERGED to `main` via PR #16** (squash merge `1039e5c`, 2026-07-17), off `main` @ `e910669`, pre-merge tip `92ca4ae` — all three merge-blocking gates were green (reviewer APPROVE, security PASS, ranking-evals PASS). Branch `feat/phase-7-evals-viewer` is deleted (local + remote). **Post-review addition (2026-07-17): the live end-to-end eval against the real stack — previously recorded as deferred — was reversed, built, run, and PASSED (reproduced identically twice), and was a prerequisite for merging PR #16.** **All seven phases of the locked v1 extraction-plan scope (0–7) are now merged to `main`, CI green — there is no Phase 8.** See "Phase 4a status", "Phase 4b status", "Phase 4c status", "4d status", "Phase 5 status", "Phase 6 status", and "Phase 7 status" below.

### Phase 4a status — corpus + hardening both MERGED (read this before starting 4c)

**Corpus.** `core/tests/evals/` (JD fixture + labelled synthetic résumés + `labels.json` + `thresholds.toml`
+ RED-pending-4c harness stub `run_evals.py`) is **MERGED to `main` via PR #8** (merge `875eac2`), CI
green, 2026-07-12. Zero product code.

**Falsifiability hardening.** Branch `fix/phase-4a-corpus-falsifiability`, opened as **PR #10**
(https://github.com/humanaxiom/recruiter-assistant/pull/10), off `main` @ `463cbaa`, tip `583427f`, 18
commits. **MERGED to `main` via merge commit `464a479`, CI green.** Why this
branch exists: the three merge-blocking gates audited the *merged* corpus and proved by mutation that it
**could not fail a bad Phase-4c engine** — the artifact whose whole purpose is to make 4c's first green
build falsifiable. Nine cumulative rounds of findings-and-fix closed every hole found (rounds 1–2 pre-merge
on the original 4a branch; rounds 3–9 on this branch). Full history:
[docs/activity/phase-4a-ranking-evals-corpus.md](docs/activity/phase-4a-ranking-evals-corpus.md); contract
detail: [docs/EXTRACTION_PLAN.md](docs/EXTRACTION_PLAN.md) ("4a hardening" subsections and "Current status
& next step").

**Final gate verdicts, HEAD `583427f`:**
- reviewer: **APPROVE** (31 of 32 mutations killed across the branch's history; the one survivor is **R1**
  below — a consciously-carried residual, not an open defect)
- security: **PASS** (empty findings table)
- ranking-evals: **PASS**
- Offline: ruff · black · `mypy --strict` clean · **1040 unit tests @ 96.63% coverage** (up from **955** on
  `main` before this branch — zero `core/src/` changes, so the whole delta is new eval-corpus tests) ·
  **65 integration tests** vs real Postgres+Neo4j · `run_evals.py` still exits 1 (correct pre-4c RED state)
- **Zero `core/src/` changes** across the whole branch

**The six-arm baseline battery** (verified against an engine replica ported from hris
`packages/pipeline/src/pipeline/matching/{stages,orchestrator}.py`, real `nomic-embed-text` on a cold
cache, real `rapidfuzz`, both input orders):

| engine | precision@5 | r09 rank | ordering pairs | verdict |
|---|---|---|---|---|
| keyword-overlap | 0.80 | 1 | 0/3 | FAIL |
| lexical tf-idf | 1.00 | 8 | 0/3 | FAIL |
| embedding pure-vector | 0.80 | 4 | 0/3 | FAIL |
| faithful + no-op evidence verifier | 0.80 | 1 | 3/3 | FAIL (adversarial arm) |
| faithful + hris `_fuzz_substring` | 0.80 | 1 | 3/3 | FAIL |
| faithful + correct verifier | 1.00 | 8 | 3/3 | **PASS** |

**The single most important finding for 4c.** hris's `_fuzz_substring` — the evidence verifier 4c is
slated to port verbatim — **verifies all four of the corpus's fabricated quotes** (0.928 / 0.943 / 0.988 /
0.935, all ≥ the 0.85 bar) and puts the keyword-stuffer at **rank 1**. It is a character-**set** overlap
ratio, not a sequence ratio. **It must be REPLACED, not ported** — use `rapidfuzz.partial_ratio` or
`token_set_ratio` (both measured safe: negatives score 0.36–0.46, golds 1.000). Other measured landmines:
`fuzz.WRatio` scores a fabricated anchor at **0.855** (leaks); `partial_token_set_ratio` returns **1.000**
on 2 of 4 negatives; `fuzz.ratio` scores the corpus's own **gold** anchors at 0.648/0.796 (would reject
valid evidence).

**The recurring lesson, worth stating once, plainly.** The corpus was wrong three separate times in the
**same way**: it asserted what a sub-score *should* mean rather than what the code *does*. `seniority` is
**not** a years check — it is `cosine(jd.title, most-recent job title)` rescaled from `seniority_floor`.
`score_education` reads only the degree **level** and never `jd.education.fields`. Each time, a control
that looked rigorous was **inert**. The gates only caught it once they stopped reasoning from the spec and
**ported the actual `stages.py`**. **4c should read `stages.py` first, not third.**

**Open decisions / accepted residuals carried into 4c:**
- **R1 — the corpus is blind to the skill sub-score's internals.** `weights.skill = 0.0` **passes**;
  recency decay disabled passes (even though r10's `decision_point` is literally
  `recency_decay_stale_skills` — that label is decorative); `must_have_miss_penalty 0.5 → 1.0` passes; the
  implied-experience relief path and the ontology junk-bucket are never exercised decisively.
  **Deliberately CARRIED INTO 4c, not closed on this branch** — closing it needs skill-dimension twin
  fixtures, which churns the rank bands. 4c must add: a recency twin for r10, a must-have-miss twin, and
  confirm `weights.skill = 0` **FAILS**.
- **R2 — the corpus gates the evidence *verifier*, never the *extractor*.** An LLM that simply fails to
  *find* real evidence is caught only in the limit (`min_completeness_in_topk` catches "no quote at all").
- **Open decision needing a human:** `score_education` ignores `jd.education.fields`, so JD field-relevance
  is **decorative** today. Either extend the scorer, or drop `fields` from the JD contract. The r14/r11
  ordering pair is deliberately built to survive **either** resolution.
- **Ported engine helpers are trusted, not verified, until 4c lands.**
  `test_ported_engine_helpers_agree_with_the_real_ones` **skips** today and wakes up when
  `src.pipeline.matching.{stages,orchestrator}` exists. **If it fails then, re-derive every corpus claim
  that depends on the ports** (r09's potency, r11/r14's partial credit, the education twins, the
  ordering-pair gaps) — do NOT relax the comparison.
- **The three blind-engine mutations are a documented review OBLIGATION, not a gate**
  (`weights.education = 0`, `overqual_ratio = 99`, `weights.motivation = 0` must each FAIL on **both**
  input orders). No gate in this repo can run them — they need the engine with *mutated* `MatchWeights`,
  which is a property of 4c's own test suite. **The 4c reviewer is the last line of defence on the
  ordering pairs.**

**Documented process deviation (flag for the human, not silently absorbed).** Three commits on the
hardening branch are labelled `test(...)` rather than `red:`/`green:` — a deliberate, reviewer-verified
deviation from CLAUDE.md's mandatory TDD order, because the guards they add pass against the unmutated
tree and can only be shown red by *mutation*, not by an honest failing-test-first commit. Detail in
`docs/EXTRACTION_PLAN.md`'s round-numbering note.

**Suggested chores flagged by security, OUTSIDE this branch's scope (not fixed, just flagged):**
- `recruiter@sfu.ca` appears in `core/tests/unit/test_schemas_jobs.py` and `test_schemas_resumes.py`
  (Phase-2 code, already on `main`) — a real institutional domain used as test data, in a repo whose own
  PII invariant (this corpus) bans exactly that shape. Suggested chore branch: replace with `@example.test`.
- The repo owner's real email is also in **this file's own git-identity recipe** below (see "Environment
  quirks") — kept here only as a placeholder now; the real value lives in the owner's git config.
- Pre-existing: **18 `mypy --strict` errors in `core/tests/unit/`** — the gate is `mypy src --strict`
  only, so repo *tests* are not type-gated at all. Suggested chore, not a blocker.

### Phase 3 is MERGED to `main` (PR #6, merge `49196d7`) — DONE

Phase 3 (ingest + parse) landed on `main` on 2026-07-12 via PR #6 after **four rounds of gate findings-and-fix** on branch `feat/phase-3-ingest-parse` (now deleted). All three merge-blocking gates were green on final HEAD `c7b497e` (reviewer APPROVE, security PASS, ranking-evals PASS) and **CI (`gates-all`) went fully green before merge**. The gate history below is retained because its findings (especially the PII-at-rest / outbox-embedding boundary) are load-bearing context for Phase 4. **The next action is Phase 4 — see "Phase 4 resume" below.**

**What landed on the branch (TDD, red→green throughout):** the full ingest/parse pipeline ported from `C:\repos\hris`:
- `core/src/pipeline/parsing/{extract,chunk}.py` — PyMuPDF/python-docx/striprtf extraction + section-aware chunker (chunk ids **one-based**, `c_001`/`cl_001`; `_sanitize` NUL-strip preserved).
- `core/src/pipeline/llm/{client,cache}.py` — hris's hand-rolled **httpx** OpenAI-compatible client (retry + circuit breaker, chat/JSON-mode/embeddings) + Redis read-through `CachedEmbedder`. **`openai` dep was REMOVED** (locked decision: port httpx verbatim).
- `core/src/pipeline/skills.py` + `skill_data/aliases.yaml` — Neo4j-free slice of `skill_normalize` (match/canonicalize/`build_summary_text`); the Neo4j skill-graph half is deferred to Phase 4.
- `core/src/prompts/` — Jinja loader + **4** template pairs (`jd_extract_v1`, `resume_core_v1`, `resume_skills_v2`, `cover_letter_v1`). NOTE: the handoff's old "4 pairs" list was wrong — it named `shortlist_evidence_v1` (Phase 4) and omitted `jd_extract_v1` (which `parse_job` calls). Corrected.
- `core/src/services/{pii,job_service,resume_service,outbox_service}.py` — **this dir did not exist before**; it's a hard prerequisite the plan under-scoped. `pii.py` uses the STRICT `current_setting('app.pii_key')` (no `missing_ok`) sourced from `settings.pii_key` (env), not hris's secrets-file ladder. `record_parsed` has the optimistic-concurrency race guard (0 rows → `False` → task `"stale"`, no outbox row).
- `core/src/worker/{tasks,resume_tasks}.py` + `main.py` wiring — `parse_job` / `parse_resume`; `WorkerSettings.functions = [parse_job, parse_resume]`. **Graph projection (`project_to_graph`/`normalize_skill`) deliberately CUT to Phase 4** — Phase 3 stops at parse → Postgres → outbox row (undelivered rows are the outbox pattern working).
- `core/src/schemas/{resumes,jobs}.py` — added `max_length` caps on LLM-output fields (carried-forward Phase-2 security item) **and fixed a real Phase-2 data-loss bug**: the `mode="before"` row filters dropped already-validated sub-model instances (`ResumeParsed(skills=[ResumeSkill(...)])` yielded `skills == []`); dict path is byte-identical.
- `core/src/settings.py` (+6 LLM/cache knobs), `core/requirements.txt` (added PyMuPDF/python-docx/striprtf/jinja2/pyyaml; removed openai; `redis>=5.0.1`).
- `docs/adr/007-phase3-ingest-parse-hardening.md` — records all Phase 3 decisions + the PII-at-rest boundary.

Full write-up: [docs/activity/phase-3-ingest-parse.md](docs/activity/phase-3-ingest-parse.md); rationale: [docs/adr/007-*.md](docs/adr/007-phase3-ingest-parse-hardening.md).

**Gate history on this branch (this is the important part):** first full pass was reviewer=CHANGES-REQUIRED, security=FAIL, ranking-evals=PASS. All blocking gates **mutation-tested every guard** and found real defects across four rounds:
- **Round 1** (`e24f9dc` red → `c8485b9` green): PII-in-`ValidationError` redaction (pydantic embeds `input_value` → leaked into `failure_reason`/logs), unbounded-chunks → uncaught `ValidationError`, decompression-bomb caps, LLM-emitted NUL, `embed()` dim validation, dropped `candidate` from outbox payload.
- **Round 2** (`86f66d1` red → `c57a1c1` green, findings F1–F6): the re-audit **defeated round 1 by mutation** — DOCX bomb guard trusted the zip's self-declared central-directory sizes (forged CD → 198 MB inflation), a corrupt PDF raised a bare `RuntimeError` that still escaped uncaught, `_strip_nuls` could hit `RecursionError`, embedding-call failures escaped `parse_resume`, and dropping only the `candidate` field while still shipping raw chunk text (résumé header PII) into the outbox was "theatre." All six fixed: DOCX guard now streams members with a real 50 MB decompression ceiling; `_extract_pdf` page-count + loop wrapped → `UnsupportedMimeError`; `chat_json` catches `RecursionError` (+ depth-bounded `_strip_nuls`); permanent embedding `LLMOutputInvalidError` → `record_parse_failure` (transient `LLMUnavailableError` deliberately still escapes so arq retries an outage); outbox payload now excludes chunk `text` too (Phase 4 reads text from `resumes.parsed`, the system of record). ADR-007 was written at this point.
- **Round 3** (`d7afe53` red → `13c74d8` green, findings F1/F2/F3/F5): the re-audit found the round-2 outbox fix still incomplete — **F1 (HIGH): `chunk_embs`/`summary_emb` in the outbox payload encode candidate identity inside the embedding vectors themselves** (a `nomic-embed-text` vector of a header chunk, or of a summary a small model opened with the candidate's name, is PII-equivalent under PIPEDA/FIPPA); **F2: the outbox `summary` field** was still cleartext and could open with the candidate's name; **F3: an empty `PII_KEY`** did not fail loud, so a misconfigured deploy would silently `pgp_sym_encrypt` PII with an empty passphrase; **F5: `_extract_pdf`'s `doc.needs_pass` read** was unwrapped and could raise an untyped exception on a corrupt (not merely password-protected) PDF, escaping `extract_text` uncaught. All four fixed: a deterministic `_redact_candidate_pii` scrub (whitespace-flexible identifier match) applied to every string handed to the embedder; `summary` dropped from the outbox payload; `worker/main.py` startup now raises `RuntimeError` on an empty `PII_KEY` before opening any pool/driver/store; `needs_pass` wrapped the same way as the page-count/page-loop reads. ADR-007 §7/§7a were extended.
- **Round 4** (`6e1d35e` red → `c7b497e` green, finding F1-R): the re-audit found a **MEDIUM residual under-redaction** in the round-3 embed scrub — identifiers are matched as the LLM's *normalized* values against the *un-normalized* résumé body, so whitespace/format divergence (a line-broken name, a reflowed phone number, a bare email local-part) could still leave identity in the embedded text. Fixed with a whitespace-flexible redaction pattern (tokens joined by `\s+`) plus a separate email-local-part scrub. Two deliberate, accepted, documentation-only residuals were recorded alongside the fix: **N1** (structured experience/education/skills fields ride the outbox unscrubbed — non-contact, symmetric with the §6/§7 at-rest cleartext decision) and **N2** (the scrub errs toward over-redaction of embedded text, e.g. a common-word `location` substring). Same commit also **pinned `black==26.5.1`** in `requirements-dev.txt` for gate reproducibility (CI and local containers had been resolving different `black` versions).

**All three merge-blocking gates are green on final HEAD `c7b497e`** — reviewer APPROVE, security PASS, ranking-evals PASS. No further gate rounds are outstanding.

**Human decision made this session (record, don't re-ask):** for PII-at-rest, we **drop `candidate` (and now chunk text) from the outbox payload only**; `resumes.parsed` jsonb retains cleartext candidate — accepted for v1, documented in ADR-007 §6, revisit before any multi-tenant deploy. Phase 5 redaction is display-only and must not be mistaken for at-rest protection.

**Current gate status:** offline (ruff/black/mypy --strict/**729 unit @ ~96.6% coverage**) and integration (vs real Postgres+Neo4j) all GREEN as of final HEAD `c7b497e`, host write-back verified. `.claude/settings.json` was reverted to `main` (`f12faf6`) after parallel agents polluted it — do not let it back into the diff.

**Phase 2 landed** (two commits — red `1645178` → green `5bbf7c2`): the pydantic **v2** contract layer in `core/src/schemas/` — three modules + an `__init__` re-export (`from src.schemas import JobCreate, ResumeParsed, MatchWeights, …`), the contracts Phases 3–6 code against (API DTOs, strict LLM `chat_json` schemas, jsonb shapes, ranking weights). `jobs.py` = job DTOs + `Skill`/`Education`/`JDExtracted`; `resumes.py` = parse shapes + resume DTOs + the `_coerce_year`/`_drop_invalid_rows`/`_coerce_*` lossy validators; `matching.py` = `MatchWeights` (+ `DEFAULT_WEIGHTS`) + score/evidence/shortlist shapes. Pure data models — no I/O. **Review workflow + Taleo/JD-comments CUT and not importable** (`PipelineStage`/`DispositionReason`/`ShortlistDecision*`/`StageTransition*` deleted; `ShortlistEntry` drops `current_decision`/`current_stage`, keeps blind-review `blinded`/`display_label`; `JobListItem` drops `comment_count`/`source`/`external_last_seen_at`; no `approval_required_2nd_review`) — a merge-blocking cut guard enforces it. **Three DDL-alignment deviations**: `created_by`/`uploaded_by` are `str | None` (nullable TEXT actor labels), `JobCreate.blind_review` defaults `True` (blind-by-default), no `approval_required_2nd_review`. **`MatchWeights` is the ranking-weight contract** (0.6/0.3/0.1 top; 0.40/0.25/0.10/0.15/0.10 sub; `evidence_verify_fuzz=0.85`; frozen; sums-to-1.0 validator). Gates: offline green — ruff (no `--fix`), black, mypy --strict, **486 unit tests, 97.52% coverage**; reviewer APPROVE, security PASS, ranking-evals PASS (incl. a weight-validator mutation test). The GREEN step was completed by the coordinator directly after a coder subagent hit a session limit mid-port (`matching.py` + `__init__.py` hand-authored, re-verified by reviewer + evals). Security flagged a **redaction-boundary contract for Phase 5**: `ResumeOut`/`ResumeListItem` can serialize decrypted PII with `blinded=True`, so Phase 5 redaction MUST mask `candidate.*`/`candidate_name`/`cover_letter_text` before DTO construction (the schema can't enforce it). Details: [docs/activity/phase-2-schemas.md](docs/activity/phase-2-schemas.md); rationale: [docs/adr/006-*.md](docs/adr/006-schema-port-trim-ddl-alignment.md).

**Phase 1 landed** (four commits — red → green → red-harden → green-harden): the filesystem `BlobStore` (`core/src/storage/blob_store.py`) exists — async `put`/`get`/`delete`/`exists`/`list_keys` over `settings.storage_dir`, stdlib-only (`pathlib`/`asyncio`/`os`, IO via `asyncio.to_thread`), replacing MinIO. `BlobNotFound` / `InvalidBlobKey` exceptions. Security core: the `_resolve` guard rejects `..` segments, absolute/Windows-drive/backslash keys, empty/root/null-byte keys, and symlink escapes (realpath + `is_relative_to`); blobs are `0o600` and store-created dirs `0o700` (PIPEDA/FIPPA — blobs-at-rest are permission-gated, distinct from the pgcrypto-encrypted PII *columns*); `list_keys` realpath-filters escaping symlinks out of listings. Wired onto `app.state.blob_store` (with a `get_blob_store` dependency) and worker `ctx["blob_store"]`; **no call site invokes it yet** — the upload/fetch/flush sites are ported in Phases 3–6. Gates: offline green — ruff (no `--fix`), black, mypy --strict, **240 unit tests, 99.46% coverage**; all three merge-blocking gates passed (reviewer APPROVED, security PASS, ranking-evals PASS with a guard-mutation test). Details: [docs/activity/phase-1-storage.md](docs/activity/phase-1-storage.md); rationale: [docs/adr/005-*.md](docs/adr/005-filesystem-blobstore-interface-path-safety.md).

**Phase 0 landed** (seven commits + a merge commit, red → green → 3 review fixes → docs → ruff-pin fix):
- Template demo app removed (`core/src/agents|memory|gates`, `models/db.py`) and replaced with the ranking-domain foundation. Rebrand to `recruiter-assistant`.
- Compose: pg/neo4j/redis/ollama, **no MinIO**, `./data` bind mount. Settings: `llm_embedding_dim = 768` (contract source), `storage_dir`, LLM/Neo4j config.
- **asyncpg idempotent startup DDL** for 5 tables (`jobs`, `resumes` +PII BYTEA, `shortlist_entries`, `reverse_match_entries`, `outbox`; SQLAlchemy dropped). **Neo4j bootstrap**: 4× 768-d cosine vector indexes + skill-graph constraints, dim derived from settings. Schema deviations recorded in **ADR-004**.
- **Gates:** offline green (ruff / black / mypy --strict, 172 unit, coverage 88.79%); integration green (39 tests vs real Postgres + Neo4j). **CI (GitHub Actions) went fully green before merge** — branch-name, `ruff·black·mypy`, `unit·coverage ≥ 80%`, `integration (pg + neo4j + redis)`.
- **Ruff-pin fix (7th commit, `22abcb9`):** CI's ruff (0.15.21) and the local container had resolved different ruff versions (`requirements-dev.txt` only floor-pinned `ruff>=0.6.0`), which disagreed on first-party import grouping and failed the static gate with I001. Fixed by pinning `ruff==0.15.21` and adding `known-first-party = ["src"]` to `core/pyproject.toml`.

**Note on `core/src/gates/`:** the deleted `gates/` was the template demo's *product-code* gate-runner, not the build harness. `make gates`, CI, `.claude/`, and pre-commit are all intact. The Phase 0 checklist's "keep gates" meant the build suite.

**Not started:** Phase 4 onward — see below. (Phase 3 is merged to `main`; see "Current state" above.)

**Decisions locked:** template-first port · filesystem storage (MinIO dropped — community edition archived 2026-04-25) · keep Neo4j (load-bearing) · v1 includes cover-letter/motivation, reverse-match, a minimal Flask viewer, and blind-review redaction ON by default.

## Environment quirks (IMPORTANT — a fresh session won't know these)

- **No real Python on this host** — only the WindowsApps stub. You **cannot** run `make gates` natively. Verify the offline gate suite in a container:
  ```bash
  docker run --rm -v "C:\repos\recruiter-assistant:/w" -w /w/core python:3.11-slim bash -lc \
    "pip install -q -r requirements.txt -r requirements-dev.txt && \
     ruff check --fix src frontend tests && black src frontend tests && \
     ruff check src frontend tests && black --check src frontend tests && mypy src frontend --strict && \
     pytest tests/unit --cov=src --cov=frontend --cov-fail-under=80 -q"
  ```
  (The `--fix` + `black` write pass auto-formats; the following `check`/`--check` then verify. **This must
  match `Makefile:27`'s `mypy src frontend --strict` exactly** — an earlier version of this snippet only
  ran `mypy src --strict`, which let a `core/frontend/` type error slip past a subagent's self-check
  during the FU-4 session; the real gate has always covered both trees.)
- **Docker is available.** Integration/e2e that need live Postgres/Neo4j/Redis run via Docker/testcontainers (CI does `gates-all`). For testcontainers in the container, mount the docker socket + install `docker.io` + set `TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal`.
- **Two container gotchas:** (1) prefix `docker run` with `MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'` or Git Bash mangles `/w/core`→`W:/core`. (2) stale `__pycache__` on the Windows bind mount can mask source edits (coarse mtime → reused bytecode); when re-running pytest after editing source, add `PYTHONDONTWRITEBYTECODE=1` or clear `__pycache__`.
- **No git identity configured** — commit with inline `git -c user.name='Adam Salah' -c user.email=<owner-email> commit …` (the real address is in the owner's git config / the owner knows it — kept as a placeholder here per the security-flagged chore above: this repo's own PII invariant bans real personal emails in committed text, and this file is committed text).
- **Windows 11**, PowerShell primary; Bash tool available (Git Bash). `.claude/settings.json` hooks shell to `bash`, so Git Bash must be on PATH.
- **`gh` CLI** authed as `adamsalah13`, **admin on the `humanaxiom` org**. Pushing to `humanaxiom/recruiter-assistant` is authorized.
- Template Python is **3.11**; hris is **3.12**. Keep 3.11 (the template's) and port hris code to it — nothing in the ranking core needs 3.12.
- Model note: this is Claude Opus 4.8 (1M context); the latest models are the Claude 5 family / Opus 4.8 / Haiku 4.5.
- **`core/requeue.py` is untracked stray operational scratch work**, found in the working tree during the
  FU-4 session — a hardcoded job UUID, blocking `urllib` inside an async function, `localhost:8000`
  instead of going through `settings`, and no tests. It is not part of any branch and was deliberately
  left untracked. A resuming session should not mistake it for product code, and should not `git add` or
  commit it without first rewriting it to the codebase's actual conventions.
- **A NUL escape written into file content becomes a REAL 0x00 byte.** In the ADR-023 session this broke
  two tester subagents, a coder, and the coordinator's own `git commit -m`: pytest dies at collection with
  `SyntaxError: source code string cannot contain null bytes`, git classifies the file as binary and
  commits a zero-line diff, and the Bash tool refuses the command outright. ADR-022 records the same thing
  happening to its own first draft. In code build control characters with `chr()`; in prose (ADRs, commit
  messages) spell the escape out in words; write commit messages that discuss control characters to a
  scratchpad file and use `git commit -F`. Scan before committing: `grep -rlP '\x00' core/src core/tests`.
- **Stale `.pyc` gives FALSE GREENS when mutation-testing.** `default=32` and `default=16` are the same
  byte length, so bytecode cache validation (mtime + size) accepted a stale `.pyc` and validated the
  *restored* source — a mutant looked like a survivor without ever executing. Bit the `ranking-evals` gate
  in the ADR-023 session. Clear `__pycache__` **and** pass `-B` between every mutation run
  (`PYTHONDONTWRITEBYTECODE=1` stops writing, not reading), or run each mutant on a throwaway tree copy.
  Treat any single-token numeric/boolean mutation as especially suspect.
- **`minio` and `web` containers may show in `docker compose ps`.** This project dropped MinIO in Phase 0
  (filesystem `BlobStore`). They are almost certainly stale containers from `C:\repos\hris` sharing a
  compose project name — not this stack, and not something to wire back in.

## Subagent roster (`.claude/agents/`)

Build harness (from the template): `planner`, `tester`, `coder`, `reviewer`, `security`, `docs`.
Domain additions (this project): **`data-pipeline`** (ranking coder with the invariants baked in) and **`ranking-evals`** (merge-blocking quality gate: precision@k, evidence-verification rate = 1.0, PII-leak check).

Per-phase flow: planner → tester (+ evals fixture) → data-pipeline coder (ReviewLoop, ≤5 iters) → reviewer + security + ranking-evals (all merge-blocking) → docs. `make gates` green before the next phase.

**Run the mutation-testing gates SEQUENTIALLY, never concurrently.** `reviewer`, `security` and
`ranking-evals` all prove findings by editing source and re-running the suite. Two of them on the shared
tree at once and each reads the other's mutations as its own — a survivor that only survived because the
other agent reverted mid-run. The ADR-023 session launched `reviewer` and `security` together and had to
kill one (caught before either wrote). Parallel *producers* are fine, but only with exclusive file
ownership and explicit-pathspec commits.

**Subagent model tiering ([docs/SUBAGENT_MODEL_POLICY.md](docs/SUBAGENT_MODEL_POLICY.md)):** cheap producers + strong verifiers. The three merge-blocking gates (`reviewer`, `security`, `ranking-evals`) run on **opus** and are never downgraded; producers (`data-pipeline`, `planner`, `tester`, `coder`) default to **sonnet**; `docs` runs on **haiku**. Defaults are in each `.claude/agents/*.md` frontmatter. The coordinator overrides per-call: `data-pipeline` UP to `opus` for diffs touching the 4-stage ranking algorithm / evidence verifier / PII crypto / Neo4j scoring; `docs` UP to `sonnet` for load-bearing handoff/plan refreshes; `coder`/`Explore` DOWN to `haiku` for mechanical fixes / lookups. Quality holds because every producer's diff passes the opus-tier gates + CI before merge.

## Non-negotiables (from CLAUDE.md)

Never commit to `main` for feature work (branch `agent|feat|fix|chore/<slug>`); TDD (failing tests first); offline only (no cloud endpoints — local Ollama/OpenAI-compatible client); config via settings; a single red gate = not done. Privacy: PII never enters embeddings; anonymization non-destructive; PIPEDA/FIPPA.

## v1 status — all phases merged, plan complete (read this first)

**Phase 6 is MERGED** (PR #15, squash merge `e910669`, CI `gates-all` fully green, 2026-07-17). **Phase 7
(a minimal read-only Flask viewer over the Phase 6 API, the gate-scope fix, and the live end-to-end eval)
is MERGED to `main` via PR #16** (squash merge `1039e5c`, 2026-07-17) — all three merge-blocking gates
were green (reviewer APPROVE, security PASS, ranking-evals PASS) on pre-merge tip `92ca4ae`. Branch
`feat/phase-7-evals-viewer` is deleted (local + remote). **The live end-to-end eval — recorded below as
originally deferred — was reversed, built, run, and PASSED (reproduced identically twice against a real
stack), and was a prerequisite for merging PR #16.** See "Phase 7 status" below for the full write-up (the
viewer's blind-only posture, the gate-scope widening to `core/frontend/`, the confirmation that the
evals-fixtures line item was already satisfied in 4a/4c, and the live end-to-end eval's build-run-PASS).

**This is the last phase.** `docs/EXTRACTION_PLAN.md`'s phase table ends at Phase 7 — the locked v1 scope
(the plan's four decisions) is now fully delivered, all seven phases merged, CI green. There is **no Phase
8**. See "Next session" below for what a human might scope next — it is a list of options, not a
to-do list to auto-start. The full historical resume trail is retained below for context.

Phases 0–3 are **merged to `main`, CI green** (Phase 3 via PR #6, merge `49196d7`, 2026-07-12). Phase 4
(Ranking engine) was split into 4 gated sub-phases (4a→4b→4c→4d, each its own branch/PR — see the plan
table) and **all four are now MERGED to `main`, CI green**: 4a (evals corpus) via PR #8 (merge
`875eac2`) plus falsifiability hardening via PR #10 (merge `464a479`); 4b (graph projection) via PR #11
(merge `68fe821`); 4c (matching engine) via PR #12 (merge `fd12d1a`); 4d (shortlist + reverse-match
write path) via PR #13 (merge `5945320`) — see "4d status" below for the closed Requirement 1, the
persistence asymmetry, and the PII residual. **Phase 5 (persist + anonymize + export —
`list_for_job`/`get_one`/`export_rows` + display redaction) is MERGED to `main` via PR #14** (merge
`6deade3`), CI green — see "Phase 5 status" below for the redaction-boundary enforcement, the
`ScoreBreakdown` fold-read guard, the `cover_letter_chunks` security fix, and the (now resolved)
`original_filename` residual. **Phase 6 (API routes — job create/read/list/status, résumé
upload/read/list, shortlist generate/list/get/export, reverse-match, configurable auth) is COMPLETE and
MERGED to `main` via PR #15** (squash merge `e910669`, off `main` @ `6deade3`, tip `837de9e`) — all three
merge-blocking gates green (reviewer APPROVE, security PASS, ranking-evals PASS) AND CI's `gates-all`
went fully green before merge. See "Phase 6 status" below for the auth switch, the upload/zip scope, the
status-transition route, the reverse-match-no-redaction decision, the security hardening (SEC-1/2/4), and
the `pool.py` latent-bug fix. **Phase 7 (evals + minimal Flask viewer) is MERGED to `main` via PR #16**
(squash merge `1039e5c`, 2026-07-17) — the live end-to-end eval (previously deferred, then built, run, and
PASSED — see "Phase 7 status" below) was a prerequisite for that merge and passed.
`docs/EXTRACTION_PLAN.md`'s phase table ends at Phase 7 —
**all seven phases are now merged and the extraction plan's locked v1 scope is complete.** See "Phase 7
status" below for the full write-up, and "Next session" below for what, if anything, a human might scope
next as a follow-up chore rather than a new phase.

### 4a recap (see "Phase 4a status" above for the full write-up)
`core/tests/evals/` holds the labelled corpus (JD fixture + synthetic résumés, `labels.json`,
`thresholds.toml`, harness stub `run_evals.py`) and is now gate-hardened by 9 cumulative rounds of
findings-and-fix (1040 unit tests, 65 integration tests, reviewer/security/ranking-evals all green on
PR #10's tip `583427f`, since merged). **Do not start 4c without first reading the 4c warnings in
"Phase 4a status" above** — several of them (the `_fuzz_substring` replacement, reading `stages.py`
first, the skill-sub-score residual R1) are exactly the surface 4c itself lands on.

### 4b status — DONE, MERGED via PR #11 (merge `68fe821`) — read this before starting 4c/4d

`core/src/worker/graph_tasks.py` (the outbox drainer) + the job/résumé Neo4j projection
(`worker/{tasks,resume_tasks}.py`) + the Neo4j skill-graph half of `skill_normalize`
(`pipeline/skills_graph.py`, `skill_data/categories.yaml`) were built and gate-green on branch
`feat/phase-4b-graph-projection`, tip `429adc7`, 20 commits, off `main` @ `464a479`, opened as
**PR #11** (https://github.com/humanaxiom/recruiter-assistant/pull/11) and **MERGED to `main` on
2026-07-15 (merge `68fe821`), CI green.** Full write-up:
[docs/activity/phase-4b-graph-projection.md](docs/activity/phase-4b-graph-projection.md); PII
architecture rationale: [docs/adr/008-skill-graph-pii-by-construction.md](docs/adr/008-skill-graph-pii-by-construction.md).

**The headline.** `ResumeSkill.name` is untrusted free text; a small model that fumbles a résumé
header into `skills[]` can put a candidate's name into a Neo4j `Skill` node and its embedding.
**Five rounds of heuristic pattern-matching (shape rejection → offline name lexicon → quantifier
tuning → vendor veto) each closed one hole and opened another, hitting CLAUDE.md's 5-iteration cap
with a critical still open.** The human then chose an architectural fix that eliminates the class by
construction (**ADR-008**), on the insight that a job description carries no candidate PII:
`Skill.canonical_key` is either a closed-vocab cleartext term or a salted hash — never free text; the
résumé side never embeds, vector-searches, or writes cleartext; every name-detection heuristic was
**deleted outright**, not tuned again. Security PASSED after mutation-killing all four
`_resolve_one` branches and verifying the canonical-key constraint on a real Neo4j.

**Ranking-evals then did something the 4a hardening rounds, working against an idealized engine
replica, couldn't:** it projected the 4a corpus through 4b's real code into a real Neo4j and measured
the actual cost. Spelling-recall was **37.5%** — one variant (`REST APIs` vs `REST API design`) cost a
strong candidate **−0.144 on `score_final`**, more than education + overqual + motivation *combined*,
enough to drop them out of the shortlist. Fixed in-branch (`_basic_normalise` trailing-version/
parenthetical stripping, symmetric both sides): recall on the measured divergence class went
**40% → 100%**. A follow-up commit deduped `_basic_normalise` (it had drifted between two "byte-
identical by convention" copies) and gated the new parenthetical-split against skill inflation
(`Casey Rivera (Python)` must not extract `python`).

**Final gate state, HEAD `429adc7`:** ruff/black/`mypy --strict` clean; **1739 unit tests @ 97.04%
coverage** (up from 1040 pre-branch); **82 integration tests** vs real Postgres+Neo4j (up from 65);
`run_evals.py` still exits 1 (correct pre-4c RED). **All three merge-blocking gates green:** security
PASS, reviewer APPROVE, ranking-evals PASS.

**Read ADR-008's residuals before building on the graph** — 14 accepted residuals, not restated here.
The two that matter most going forward: **the vocabulary (147 concepts / ~229 spellings) is now the
single ranking bottleneck** — growing it is the only lever that improves non-vocab recall, since
auto-merge no longer affects scoring at all; and a candidate whose name collides with a vocab term
(`julia`, `hudson`, `kafka`, …) still gets a deniable-but-cleartext `canonical_key`.

### 4c status — DONE, MERGED to `main` via PR #12 (merge `fd12d1a`) — read this before starting 4d/5

`core/src/pipeline/matching/{stages,orchestrator}.py` (the 4-stage ranking engine) + `MatchWeights`
settings wiring (`src/settings.py::weights_from_settings`) + the live orchestrator wired into
`run_evals.py::main()` were built and gate-green on branch `feat/phase-4c-matching-engine`, off `main`
@ `68fe821` (PR #11's merge), opened as **PR #12**
(https://github.com/humanaxiom/recruiter-assistant/pull/12) on 2026-07-15. **All three merge-blocking
gates were green (security PASS, reviewer APPROVE, ranking-evals PASS) AND CI (`gates-all`) was fully
green — PR #12 was MERGED to `main` (merge `fd12d1a`).** Full write-up:
[docs/activity/phase-4c-matching-engine.md](docs/activity/phase-4c-matching-engine.md); decisions +
residuals: [docs/adr/009-matching-engine-port.md](docs/adr/009-matching-engine-port.md).

**All four 4b→4c blockers are closed** (full detail in ADR-009 and the activity report; the
`docs/EXTRACTION_PLAN.md` "4b → 4c BLOCKERS" section is now marked CLOSED, not restated here):
1. `missing_must` now keys off `reason == "missing"` (row `ontology_weight == 0`), never the built
   contribution's `score == 0.0` — verified single-candidate on r18 (the must-have-miss penalty is
   provably uncatchable by any pairwise rank+gap check; ADR-009 §2 has the algebra).
2. Two new skill-dimension twins landed — a must-have-miss twin (r18) and a recency twin (r19 vs
   r10) — both independently prove `weights.skill = 0`, `must_have_miss_penalty 0.5→1.0`, and
   disabled recency decay each FAIL.
3. The spelling-divergence twin was **not** needed as a separate fixture — the −0.144 swing that
   motivated it was a 4b graph-projection normalisation issue, already fixed in 4b (`_basic_normalise`);
   4c's own r18/r19 twins cover the skill sub-score's internals for 4c's own scoring bugs.
4. `canonical_name` → `canonical_key` renamed in `_stage2_skill_rows`' Cypher on day one of the port,
   verified against a real Neo4j.

**Also closed from Phase 4a's carry-forward list:** `_fuzz_substring` REPLACED with
`rapidfuzz.fuzz.partial_ratio` (re-measured against the full corpus: rejects all 4 fabrications at
0.41–0.46, survives all 4 gold anchors at 1.000); an evidence-recall assertion against `gold_evidence`
now runs (`gold_recall_min = 1.0`, R2 closed). **Still open, carried to a human, NOT resolved by this
port:** `score_education` ignores `jd.education.fields` — extend the scorer or drop `fields` from the
JD contract (ADR-009 §7).

**New in 4c, not anticipated by 4a/4b:** a reviewer finding that `orchestrator.py` read
`os.environ.get("GIT_SHA")` directly (CLAUDE.md violation) — fixed by routing `git_sha` through
`Settings`/`MatchingContext`, and backed by a new AST meta-test
(`test_no_scattered_os_environ.py`) that fails the gate if any module other than `settings.py` reads
`os.environ`/`os.getenv` again.

**Final gate state, HEAD `ed4a142`:** ruff/black/`mypy --strict` clean; **1916 unit tests @ 90.71%
coverage**; **87 integration tests** vs real Postgres+Neo4j; **`run_evals.py` exits 0** — the corpus's
first real live-engine run, not a scaffold. `precision@5 = 1.0`; r09 (adversarial bait) ranks 12th
(outside top-5); `gold_recall = 4/4`; 0 PII leaks / 116 inputs scanned; determinism exact. All six
mutation obligations (`weights.education=0`, `overqual_ratio=99`, `weights.motivation=0`,
`weights.skill=0`, `must_have_miss_penalty 0.5→1.0`, disabled recency) FAIL the corpus as required,
plus the optional WRatio-swap.

**Carried forward into 4d** (see the "4d status" section immediately below — the settings-wiring item is
now CLOSED there; the `jd.education.fields` decision is still open): wire `MatchingContext`/`weights`
from `Settings` at the real worker call sites (4c only proves the bridge in isolation); the open
`jd.education.fields` human decision; 4d ships the write path only.

### 4d status — DONE, MERGED to `main` via PR #13 (merge `5945320`) — read this before starting Phase 6

`core/src/services/shortlist_service.py` (`persist_shortlist`/`persist_reverse_match`) +
`core/src/worker/matching_tasks.py` (`shortlist_job`/`reverse_match_job` arq tasks) +
`matching_context_from_settings`/`non_matchable_families_from_settings` (the ADR-009 "Requirement 1"
settings-wiring closure) were built on branch `feat/phase-4d-shortlist-writepath`, tip `6c2bf43`, 2
commits (RED `24419b0` → GREEN `6c2bf43`), off `main` @ `fd12d1a` (PR #12's merge). **All three
merge-blocking gates were green (reviewer APPROVE, security PASS, ranking-evals PASS) AND CI's
`gates-all` (offline `run_evals.py` running inside the gated unit suite — CI never calls a model
endpoint) went fully green — PR #13 was MERGED to `main` (merge `5945320`) this session.** The scoring
code itself, `stages.py`/
`orchestrator.py`, is byte-unchanged by 4d — only the new additive `matching_context_from_settings`
factory touches `orchestrator.py`. Full write-up:
[docs/activity/phase-4d-shortlist-writepath.md](docs/activity/phase-4d-shortlist-writepath.md);
decisions + residuals: [ADR-010](docs/adr/010-shortlist-reverse-match-write-path.md).

**Requirement 1 (ADR-009, carried from 4c) is now CLOSED.** `matching_context_from_settings(settings,
*, db, neo4j, llm, embedder)` is the single call site populating every non-weight `MatchingContext`
tunable from `Settings`; `shortlist_job`/`reverse_match_job` call it with `get_settings()` and pass
`weights=weights_from_settings(get_settings())` into the orchestrator — never `DEFAULT_WEIGHTS`. The
load-bearing test class here is a settings-wiring unit test built around **non-default** `Settings`
values, because `run_evals.py` structurally cannot catch a silent fallback to `DEFAULT_WEIGHTS`:
`Settings()`'s own `match_*` defaults equal `MatchWeights`' defaults by construction, so the corpus
(which only ever runs against default `Settings`) would pass either way. See ADR-010 §3.

**The mirror-image persistence asymmetry (ADR-010 §2).** `shortlist_entries` has no dedicated
`score_structured`/`score_evidence` columns and `evidence JSONB NOT NULL` → `persist_shortlist` folds
those two scores into the `score_breakdown` jsonb and coerces `evidence=None` to `{}`.
`reverse_match_entries` has dedicated columns and a nullable `evidence JSONB` →
`persist_reverse_match` writes those as their own SQL args and passes `evidence=None` through as SQL
`NULL`. Residual: at the raw-SQL level, shortlist's `{}` cannot distinguish "never evidence-scored"
from "scored, found nothing" — a minor info-loss Phase 5's read layer should be aware of.

**PII-at-rest residual, security-flagged and recorded (not just in a test docstring, per instruction) —
ADR-010 §6.** Evidence quotes (verbatim résumé/cover-letter chunk text) are written unredacted into
both tables. This is symmetric with, not a new instance beyond, ADR-007 §6/§7's already-accepted
cleartext-at-rest posture for `resumes.parsed` — same Postgres instance, same DB-access boundary,
derivative of already-accepted-cleartext chunk text, and it never rides the outbox or an embedding.
Accepted for v1; revisit before multi-tenant.

**Both DELETE-first persist functions are per-run idempotent, keyed on the DDL's real unique
constraints** (`shortlist_entries (job_id, resume_id)`, `reverse_match_entries (resume_id, job_id)`),
proven against a real Postgres including the `NOT NULL`/`UniqueViolationError` cases a mocked
connection can't exercise. **No advisory lock exists for concurrent duplicate enqueues** — accepted
(last-committer-wins; nothing currently enqueues duplicates by design), revisit once Phase 6 ships a
user-facing regenerate route (ADR-010 §1).

**`reverse_match_job`'s `allowed_job_ids` filter is `description_parsed IS NOT NULL`, not
`status = 'open'`** — `jobs.status` is never transitioned by any code path through 4d (no Phase-6 route
yet), so filtering on it would filter to zero or an arbitrary default; ADR-010 §4 has the full
reasoning and the note to revisit once Phase 6 starts transitioning `status`.

**Open human decision, carried forward AGAIN, still unresolved:** `score_education` ignores
`jd.education.fields` (ADR-009 §7) — 4d touches none of `stages.py`/`orchestrator.py`'s scoring code
(byte-unchanged), so this is untouched, not newly relevant. Either extend the scorer to read `fields`,
or drop `fields` from the JD contract.

**Final gate state, HEAD `6c2bf43`:** ruff/black/`mypy --strict` clean; **1947 unit tests @ 91.98%
coverage**; **93 integration tests** vs real Postgres+Neo4j (87 carried forward + 6 new, all in
`test_shortlist_persistence_pg.py`). Reviewer's sign-off rests on 8 behavioral guards (rerun-replaces
×2, `NOT NULL` coercion, weights-from-settings ×2, `matching_context_from_settings` tunable coverage,
the `allowed_job_ids` scoping, and the no-silent-redaction guard) — full table in the activity report.

**Carried forward into Phase 5:** `list_for_job`/`get_one`/`export_rows` + display redaction (deferred
by 4d's own scope, per the plan-of-record); the shortlist-side `evidence = {}` ambiguity (above); the
still-open `jd.education.fields` decision; no advisory lock on concurrent runs.

### Phase 5 status — DONE, MERGED to `main` via PR #14 (merge `6deade3`) — read this before starting Phase 7

`core/src/services/redaction.py` (new — `redact_text`/`pseudonym`/`blind_label_map`/
`is_foreign_location` + `redacted_filename`, ported near-verbatim from hris) + `core/src/errors.py` (new —
`AppError`/`NotFoundError`) + the read/export extensions to `shortlist_service.py`
(`list_for_job`/`get_one`/`export_rows` + pure `shortlist_csv`/`shortlist_evidence_csv`/`shortlist_json`
formatters) and `resume_service.py` (`list_for_job`/`get_one(reveal=...)`) were built and gate-green on
branch `feat/phase-5-persist-anonymize-export`, off `main` @ `5945320` (PR #13's merge), tip `02af27c`,
6 commits across three RED→GREEN cycles (RED `3e383ff` → GREEN `33512c2` initial build; RED `8b1597e` →
GREEN `b6b1ec7` cover-letter-chunks security fix; RED `c1e4e04` → GREEN `02af27c` filename
de-anonymization fix). **All three merge-blocking gates are green (reviewer APPROVE, security PASS,
ranking-evals PASS) — re-verified after each post-first-green fix.** **MERGED to `main` via PR #14
(merge `6deade3`), CI green.** The scoring code itself, `stages.py`/`orchestrator.py`, is byte-unchanged
by Phase 5 (this phase is entirely read/export/redaction, no ranking logic). Full write-up:
[docs/activity/phase-5-persist-anonymize-export.md](docs/activity/phase-5-persist-anonymize-export.md);
decisions + residuals: [ADR-011](docs/adr/011-display-redaction-read-export-boundary.md).

**ADR-006 §4's redaction-boundary contract is now enforced in code, not just recorded.** Every blind
read path builds the redacted value first and only then constructs the DTO — `resume_service.get_one`'s
blind branch builds `_blind_parsed(...)` before `ResumeOut(parsed=...)`;
`shortlist_service._row_to_blind_entry` builds `_redact_evidence(...)` before
`ShortlistEntry.model_validate(raw)`; `export_rows`/`_apply_reveal` builds `_redact_evidence_dict(...)`
before the export dict is finalized. Proven by three black-box byte-scan tests (assert the candidate's
real name/email/phone byte-sequence is absent anywhere in the serialized blind output, not just in
specific fields) plus reviewer mutation testing on every redaction call site. **This is display-only
redaction, not at-rest protection** — ADR-007 §6/§7's cleartext-at-rest posture and ADR-010 §6's
extension of it to `shortlist_entries`/`reverse_match_entries` are both unchanged.

**The `ScoreBreakdown` fold read guard (ADR-011 §2) is required to read ANY 4d-written shortlist row.**
`persist_shortlist` (4d, ADR-010 §2) folds `score_structured`/`score_evidence` into the
`score_breakdown` jsonb; `ScoreBreakdown` is `extra="forbid"`, so the read layer
(`_parse_entry_jsonb`) pops those two keys out before `.model_validate()` — without this pop, every row
4d ever wrote raises `ValidationError` on read. Proven against the real jsonb codec in
`test_shortlist_read_export_pg.py`.

**Two post-first-green security/residual fixes (ADR-011 §4/§1):** (1) HIGH: the first GREEN
(`33512c2`) redacted `resumes.parsed.chunks[].text` but not `cover_letter_chunks[].text` — raw letterhead
PII still reachable under blind — written as a failing regression test first (`8b1597e`), fixed by
extending `_blind_parsed` (`b6b1ec7`), mutation-proven. (2) RESIDUAL FIX: the human decided preemptively
to close the `original_filename` de-anonymization vector (a `First_Last_Resume.pdf` identifying a candidate
under blind review) rather than accept it as v1. Fixed by adding `redacted_filename()` helper, wired at
three blind surfaces (`resume_service.get_one`/`list_for_job`, `shortlist_service._apply_reveal` for
csv/json export), returning generic `resume<ext>` under blind; real filename under reveal/non-blind
(RED `c1e4e04` → GREEN `02af27c`, mutation-proven).

**Two hris gaps closed beyond a verbatim port (ADR-011 §3/§5):** `_redact_evidence` now redacts
`cover_letter_evidence[].evidence`/`overall_motivation` in BOTH the read and export paths (hris's
version never did); the name/term redaction regex is now grouped
(`(?<![\w])(?:{alt})(?![\w])`) so a middle name-part can't match inside a longer unrelated word — a
latent hris bug, not a Phase-5-introduced one.

**A LOW residual from the filename fix (not a blocker):** `redacted_filename()` trusts `os.path.splitext`,
so a pathological filename like `cover.Jane_Smith` (no true extension) yields `resume.jane_smith`,
leaking the lowercased suffix. Accepted for v1 (low risk: requires dot-containing name component AND
upload under that exact name). Recommend an extension allowlist + length cap when Phase 6's upload
validation lands.

**One pre-existing CI flake (not introduced by Phase 5):** `test_evals_corpus.py::test_every_threshold_key_is_enumerated_by_both_consumers`
is order-dependent (passes in isolation, can fail under `pytest-randomly`). Flagged by the reviewer as a
separate follow-up chore, not a gate blocker.

**Open human decision, carried forward AGAIN, still unresolved:** `score_education` ignores
`jd.education.fields` (ADR-009 §7, restated ADR-010 §5) — Phase 5 touches no scoring code, so this is
untouched.

**Final gate state, HEAD `02af27c`:** ruff/black/`mypy --strict` clean; **2039 unit tests @ 91.86%
coverage**; **18 integration tests** green vs real Postgres (`test_shortlist_read_export_pg.py`,
`test_resume_read_pg.py`). Reviewer APPROVE (5 mutation obligations fired — full table in the activity
report), security PASS (after the `cover_letter_chunks` fix), ranking-evals PASS (scoring code
byte-unchanged).

**Carried forward into Phase 6:** the `original_filename` open decision (resolved by Phase 5's own
post-first-green fix — see above); the still-open `jd.education.fields` decision; the shortlist-side
`evidence = {}` ambiguity (ADR-010 §2, still unresolved, first touched by this phase's read code but not
fixed); no advisory lock on concurrent shortlist/reverse-match runs (ADR-010 §1) — revisit once Phase 6
ships a user-facing regenerate route; CSV formula/injection in `shortlist_csv`/`shortlist_evidence_csv` —
accepted for v1, one-line fix noted.

### Phase 6 status — DONE, MERGED to `main` via PR #15 (merge `e910669`) — read this before starting Phase 7

`core/src/api/deps.py` (new — `require_api_key`/`resolve_actor`/`get_arq`/`log_auth_mode`),
`core/src/api/routes/{jobs,resumes,shortlist}.py` (new — 11 routes), `core/src/services/zip_upload.py`
(new — `expand_zip_entries`/`ZipRejected`), `core/src/services/jd_import_service.py` (new —
`extract_jd_text`) were built and gate-green on branch `feat/phase-6-api-routes`, off `main` @ `6deade3`
(PR #14's merge), tip `837de9e`, commit chain: RED `209bff7` → GREEN `bc9a3d6` (initial routes, resumed
mid-build after a session-limit interruption) → RED `1f2b161` → GREEN `344f6bf` (SEC-1/SEC-2/SEC-4
security hardening + exact `fastapi`/`starlette`/`python-multipart` pins) → RED `c75f4a7` → GREEN
`837de9e` (non-ASCII `X-API-Key` 401 generalization + upload file-count-ordering regression pin). **All
three merge-blocking gates green (reviewer APPROVE, security PASS, ranking-evals PASS) — re-verified
after the security-hardening round.** Opened as PR #15
(https://github.com/humanaxiom/recruiter-assistant/pull/15) on 2026-07-17 after the human check-in;
**CI's `gates-all` (offline `run_evals.py` running inside the gated unit suite — CI never calls a model
endpoint; inference is host-only by design) went fully green, and PR #15 was squash-merged to `main`
(merge `e910669`) on 2026-07-17.** Full
write-up: [docs/activity/phase-6-api-routes.md](docs/activity/phase-6-api-routes.md); decisions +
residuals: [ADR-012](docs/adr/012-api-routes-auth-upload-scope.md).

**Route map:** `POST/GET /jobs`, `GET/PATCH /jobs/{id}`, `PATCH /jobs/{id}/status` (draft→open, the only
status-mutating route, forward-only, 409 on invalid transition), `POST /jobs/jd-extract` (pre-fill
helper, no DB write), `POST/GET /jobs/{id}/resumes`, `GET/PATCH /jobs/{id}/shortlist`, `GET
/jobs/{id}/shortlist/export`, `GET /resumes/{id}`, `POST /resumes/{id}/match-jobs`, `GET
/resumes/{id}/match-results`, `GET /shortlist/{id}`.

**Locked human decisions this phase (ADR-012):** (1) one settings flag `api_key` — empty disables auth
(loud startup warning), non-empty enables fail-closed 401 with constant-time UTF-8-byte comparison;
optional `X-Actor-Name` (128-char cap) populates `created_by`/`uploaded_by`. (2) Upload accepts local
multi-file + zip only — Taleo/CSV-manifest connector pairing explicitly CUT and deferred to a future
"sources/connectors" feature (the user's framing: "Taleo was a shortcut to get sample data … will add
more connectors in the future"); zip expansion mirrors the Phase-3 DOCX-bomb defense (never trusts
`ZipInfo.file_size`, streams real decompressed bytes, path-traversal/extension-allowlist/entry-count/
per-entry/total-size guards, writes nothing on reject). (3) `PATCH /jobs/{id}/status` is the only
status-mutating route. (4) Reverse-match is a subresource of `routes/resumes.py`; **explicitly NO
redaction** on the reverse-match read (the caller owns the résumé they matched — no third party to
protect, unlike every other blind-review-aware read path). (5) `POST /jobs/jd-extract` included.

**Carry-forwards now CLOSED:** `JobOut.blind_review` fail-open (ADR-006 §4 note) — `_row_to_jobout` now
sets it explicitly from the row on every path, reviewer mutation-proved both directions. Redaction
boundary at the HTTP layer (ADR-006 §4 / ADR-011) — read/export routes route straight through to the
already-redacting service functions; security byte-scanned actual serialized HTTP responses.

**Security hardening + accepted residuals (ADR-012):** Fixed SEC-1 (non-ASCII API-key compare crashing
to 500 instead of 401), SEC-2 (upload file-count cap now checked before any file body is read —
regression-pinned), SEC-4 (`X-Actor-Name` 128-char cap); `fastapi`/`starlette`/`python-multipart` pinned
`==` exactly (the route-walker test depends on a FastAPI-internal structure). Accepted-for-v1: SEC-3 (no
LIMIT/OFFSET on shortlist/reverse-match reads, bounded by shortlist size in practice), SEC-5
(`detect_mime`'s `txt` catch-all, intentional), blob-write-inside-transaction (a rollback leaves a
harmless uuid-keyed orphan blob, no orphaned enqueue). Also fixed: a latent `pool.py` bug —
`PoolConnectionProxy[Record]` isn't subscriptable at runtime; under `from __future__ import annotations` +
FastAPI's `eval_str` signature introspection it crashed at route registration the first time any route
actually used `Db` (never true before Phase 6) — fixed with a `TYPE_CHECKING`-gated alias.

**Final gate state, HEAD `837de9e`:** ruff/black/`mypy --strict` clean; **2156 unit tests @ 91.68%
coverage**; **123 integration tests** vs real Postgres+Neo4j+Redis, incl. 12 new Phase-6 ASGI integration
tests (real HTTP through the FastAPI app). Reviewer APPROVE (6 mutation obligations fired), security PASS
(SEC-1/SEC-2 closed on re-audit), ranking-evals PASS (scoring byte-unchanged; CI's `gates-all` runs the
offline `run_evals.py` stand-in inside the gated unit suite — no live Ollama call, by design).

**Carried forward into Phase 7:** `score_education` ignores `jd.education.fields` (still open,
untouched); `reverse_match_job`'s `allowed_job_ids` filter still `description_parsed IS NOT NULL`, not
`status='open'`, even though a status route now exists (ADR-012 §3 revisits but does not resolve this);
the `redacted_filename` `os.path.splitext` truncation LOW residual (not addressed by Phase 6's upload
validation); no advisory lock on concurrent shortlist/reverse-match runs — a user-facing regenerate route
now exists (`POST /jobs/{id}/shortlist`, `POST /resumes/{id}/match-jobs`), so this question (ADR-010 §1)
is now live, not hypothetical.

### Phase 7 status — MERGED via PR #16 (squash `1039e5c`), CI green — v1 extraction plan complete

`core/frontend/api_client.py` (new — sync `httpx` wrapper: `build_client` + one fn per Phase-6 route +
`BackendError`/`NotFound`/`BackendUnavailable`), `core/frontend/app.py` (extended from a `/health`-only
stub with routes `/`, `/jobs/<uuid>`, `/jobs/<uuid>/shortlist`, `/shortlist/<uuid>`, `/resumes/<uuid>`,
`/resumes/<uuid>/match-results`, `/jobs/<uuid>/shortlist/export`), `core/frontend/templates/*.html` (new,
server-side Jinja2, autoescaped) were built and gate-green on branch `feat/phase-7-evals-viewer`, off
`main` @ `e910669` (PR #15's merge), pre-merge tip `92ca4ae`, commit chain: `55ee0a0` docs (interim
HANDOFF/plan stamp) → `942e8f5` red → `f28c22e` green (the viewer + client + gate-scope fix) → `92ca4ae`
refactor/fix (post-review security findings closed). **All three merge-blocking gates were green (reviewer
APPROVE, security PASS, ranking-evals PASS).** **MERGED to `main` via PR #16, squash commit `1039e5c`,
2026-07-17, CI green.** Branch `feat/phase-7-evals-viewer` is deleted (local + remote). Full write-up:
[docs/activity/phase-7-evals-viewer.md](docs/activity/phase-7-evals-viewer.md);
decisions + residuals: [ADR-013](docs/adr/013-phase7-evals-viewer.md).

**Gate-scope fix, the other half of this phase.** `Makefile` (`gates`/`gates-fast`) and
`.github/workflows/ci.yml` (`static`/`unit` jobs) are widened so ruff/black/mypy/coverage now cover
`core/frontend/` alongside `core/src`/`core/tests` — previously the frontend directory was invisible to
every quality gate (it is a sibling of `core/src/`, not nested under it). A meta-test
(`core/tests/unit/test_gates_cover_frontend.py`) pins this so it can't silently regress.

**Locked human decisions this phase (ADR-013):** (1) blind-only viewer for v1 — no reveal control
anywhere; shortlist list/detail reads are unconditionally blind (`api_client.list_shortlist`/
`get_shortlist_entry` take no `reveal` param at all) and the résumé route hardcodes `reveal=False`,
ignoring any browser-supplied `?reveal=`. (2) the blind résumé page is structurally PII-incapable —
`resume_detail.html` has no branch that renders `candidate.name/email/phone/location` at all, closing a
latent path that had been gated only on the backend's `blinded` flag (which ADR-012 notes had a fail-open
history). (3) gate scope widened to `core/frontend/`, pinned by a meta-test, rather than a second gate
suite. (4) no new evals fixtures this phase — the plan's Phase 7 evals line item (precision@k,
evidence-verification rate) was already satisfied by 4a (corpus) + 4c (live orchestrator wiring);
`run_evals.py::main()` already runs inside the gated unit suite. (5) a live end-to-end eval (the 4a/4c
corpus run through the real pipeline, re-checking thresholds against persisted rows) was originally
recorded as deferred — it needs a reachable host Ollama + `docker compose up`, which CI does not provide by
design — but that decision was **reversed on 2026-07-17**: the human un-deferred it and made it a
prerequisite for merging PR #16. It has since been **built, run, and PASSED**, reproduced identically
twice against a real stack (real `nomic-embed-text` embeddings, real Neo4j, real `shortlist_job`, real
Postgres persistence). See "Live end-to-end eval — built, run, PASS (post-review addition)" immediately
below.

**Live end-to-end eval — built, run, PASS (post-review addition, 2026-07-17).**
`core/tests/evals/run_evals_live.py` (new, 812 lines) + `core/tests/unit/test_evals_live_metrics.py` (new,
16 offline tests) were built after PR #16 was opened. The corpus is pre-parsed by design (4a fixed the
parsed representation to isolate ranking from non-deterministic LLM parsing — no raw docs exist in the
corpus), so the harness seeds the pre-parsed corpus at the **post-parse boundary** (a `jobs` row + 20
`resumes` rows with `parsed` jsonb, PII encrypted via the real `pii.py` path, and `job.parsed`/
`resume.parsed` outbox events carrying real `nomic-embed-text` embeddings through the production embed
boundary with PII redaction), then drives the real `project_to_graph` (Neo4j) → real `shortlist_job` →
reads the persisted `shortlist_entries` → evaluates every `thresholds.toml` gate, reusing
`run_evals.load_corpus`/`load_thresholds`/`_labels` and the real `stages.verify_evidence` + real redaction
functions. Ran against a remote Ollama with the calibrated models (`nomic-embed-text` + `gpt-oss:20b`); the
local metal host lacked them. Run via `docker compose ... exec -T api python tests/evals/run_evals_live.py`
against a stack pointed at that Ollama. **Verified results, reproduced exactly on two independent runs,
exit 0 both times:** `precision@5 = 1.000`; adversarial bait (r09) ranked 14th, outside k=5, no
`must_not_surface` offenders; `evidence.verification_rate = 78/78 = 1.000`;
`evidence.min_completeness_in_topk = 5/5 = 1.000`; `evidence.gold_recall = 4/4 = 1.000`;
`evidence.negative_evidence_must_fail`: 4 fabrications, all scrubbed; `ordering_controls` all pass
(education +0.0411, overqual +0.0120, motivation +0.0900, skill_missing_must +0.1460, recency +0.1440);
`pii.embedding_input_pii_free`: 0/20; `pii.exported_output_pii_free`: 0/top-5; determinism: order
identical, `max_rank_delta=0`, `max_score_delta=0`. The pure metric layer (`eval_*`) is offline-unit-tested
(16 tests, bad rankings FAIL); the live orchestration script lives under `tests/evals` (not collected by
`pytest tests/unit`), so CI stays green with no Ollama. Offline suite: **2245 unit tests @ 91.67%**
(was 2229; +16), ruff/black/mypy clean. **Deviations, recorded honestly:** ADR-013 §5's literal "HTTP
upload" wording is intentionally not followed (seeding at the post-parse boundary is what keeps thresholds
meaningful); `project_to_graph`/`shortlist_job` ran with a direct `ctx`, not enqueued on the worker; the
second determinism run used a warm Redis embed cache (embed half compares cache to itself); the
`jd.education.fields` open decision remains unresolved and untouched. Full detail:
[ADR-013 §5](docs/adr/013-phase7-evals-viewer.md) and
[docs/activity/phase-7-evals-viewer.md](docs/activity/phase-7-evals-viewer.md)'s "Live end-to-end eval
(post-review addition)" section.

**Accepted residual (ADR-013):** `_unavailable(exc: BackendUnavailable)` in `app.py` has an unused `exc`
parameter (its value is no longer rendered after the security fix below made the error page fully
static). Kept because the signature documents the handler's intent; ruff's unused-arg rules aren't
enabled in this repo. Security finding #2 (an earlier draft rendered the raised exception's message,
risking a backend-URL leak to the browser) is CLOSED, not a residual.

**Final gate state, HEAD `92ca4ae`:** ruff/black/`mypy src frontend --strict` clean; **2229 unit tests @
91.67% coverage** (frontend now format/type/coverage-gated for the first time). Reviewer APPROVE, security
PASS (both hardening findings closed — the structurally-PII-incapable résumé template and the fully
static error page), ranking-evals PASS (scoring code byte-unchanged; offline corpus 352 tests green,
`run_evals.py::main()` exits 0). **Post-review (2026-07-17):** `test_evals_live_metrics.py`'s 16 new
offline tests bring the count to **2245 unit tests @ 91.67% coverage**, ruff/black/mypy still clean — see
"Live end-to-end eval — built, run, PASS (post-review addition)" above.

**Carried forward, still unresolved:** `score_education` ignores `jd.education.fields` (ADR-009 §7,
restated through ADR-012 — untouched, scoring byte-unchanged); `reverse_match_job`'s `allowed_job_ids`
filter still `description_parsed IS NOT NULL`, not `status='open'`; no advisory lock on concurrent
shortlist/reverse-match runs (the viewer is read-only, so this is unaffected). **Resolved post-review
(2026-07-17):** the live end-to-end eval — the one genuine verification gap Phase 7 originally left open —
was built, run, and PASSED against the real stack (reproduced twice); see "Live end-to-end eval — built,
run, PASS (post-review addition)" above and ADR-013 §5. It does not exercise the Phase 6 HTTP upload/parse
routes themselves or the arq/Redis queue hop — those remain covered only by Phase 3/4/6's own tests.

**Documentation correction made this phase:** prior HANDOFF/plan text repeatedly said CI runs "a live
`run_evals.py` re-measurement against Ollama" for Phases 4d/5/6. That was inaccurate — CI's `gates-all`
runs the offline deterministic stand-in harness (`run_evals.py::main()`) inside the gated unit suite; it
never calls Ollama (`.github/workflows/ci.yml`'s own comment: "CI never calls a model endpoint; inference
is host-only by design"). Corrected everywhere it appeared in this file and in
`docs/EXTRACTION_PLAN.md`.

**Merge status:** PR #16 was gated in CI (`gates-all` fully green) and **squash-merged to `main` as
`1039e5c`** on 2026-07-17 — the live end-to-end eval (above) was the merge prerequisite and had already
passed, reproduced twice. `docs/EXTRACTION_PLAN.md`'s phase table ends at Phase 7, and it is now fully
merged: **the extraction plan's v1 scope (as locked in the plan's four decisions) is complete.**

### Workflow UI status — DONE, gates green — a post-v1 feature, NOT "Phase 8"

`core/frontend/app.py` (extended with 9 new write-capable routes: résumé upload, job status transitions,
blind-review toggle, shortlist generation, and three 3-second HTMX poll fragments — `parse-status`,
`resumes-table`, `shortlist-cards`), `core/frontend/templates/*.html` (rewritten as a full recruiter
workflow — create job → upload → generate shortlist → review → export — replacing Phase 7's read-only
pages), `core/frontend/static/app.css` (new, hand-authored) + `core/frontend/static/vendor/htmx.min.js`
(new, vendored htmx 2.0.4 + `htmx.LICENSE`), `core/src/services/job_service.py`
(`update_job`, new) + `core/src/api/routes/jobs.py` (`PATCH /jobs/{id}`, new) were built on branch
`feat/workflow-ui`. It reproduces the recruiter workflow that exists in the source `hris` Next.js
frontend, scoped strictly to **job → résumé → shortlist** — the review/decision workflow, JD-Harmonizer,
comment threads, admin console, and CAS auth all stay cut, per the plan's original keep/cut boundary.
Full detail: [ADR-014](docs/adr/014-workflow-ui.md).

**This is a new post-v1 feature, not a numbered phase.** `docs/EXTRACTION_PLAN.md`'s phase table still
ends at Phase 7 and stays closed — do not call this "Phase 8" anywhere.

**Stack decision (ADR-014 §1):** Flask + HTMX (vendored, served locally, no CDN) + a hand-authored
`app.css` utility stylesheet. Deliberately **not** a Tailwind/Node build — there is no Node toolchain in
the container, and CLAUDE.md locks the frontend stack at Flask. No `tailwind.config.js` exists; a future
contributor should not look for one. This keeps the app offline/air-gapped and keeps every redacted
response assembled server-side in Python — HTMX only ever swaps in Jinja2-rendered fragments, never raw
JSON assembled client-side.

**Blind-only, by construction, carried forward from ADR-013 (§2):** the Flask layer never forwards
`reveal` to the backend, even though this is now a write-enabled surface. `get_resume` stays hardcoded
`reveal=False`; `list_shortlist`/`get_shortlist_entry` take no `reveal` parameter at all; the three export
formats (csv/evidence-csv/json) proxy the backend's `reveal=False` default without exposing a browser-side
way to flip it. `resume_detail.html` still has no template branch capable of rendering candidate name/
email/phone/location — proven by structural byte-scan tests, not merely gated on the backend's `blinded`
flag.

**One backend addition — the only `core/src/` change in this feature:** `PATCH /jobs/{id}`
(`job_service.update_job`), needed for the blind-review toggle. Allowlist-guarded partial update built
from `payload.model_dump(exclude_unset=True)` (an omitted field means "unchanged," not "set to null" —
matters for `blind_review: bool | None`, since `False` is a legitimate deliberate value); `status` remains
unwritable through this route (`JobUpdate` carries no `status` field, `extra="forbid"` 422s a client that
tries) — every status change still goes through the Phase 6 state-machine-guarded
`PATCH /jobs/{id}/status`. `stages.py`/`orchestrator.py` and every other Phase 6 route are byte-unchanged.

**Screens:** jobs list (create-job form with JD-file auto-extract + blind-review checkbox +
status-filter pills) → job detail (3s-polled "parsing…" badge, status-transition buttons with draft→open
disabled until parsed, blind-review toggle, consent-gated résumé upload + 3s-polled status-pill résumé
table) → résumé detail (blind banner, recency-coloured skill chips, experience/education/cover letter, no
PII code path) → shortlist (Generate/Regenerate button that polls until ranked, per-candidate cards with
rank/`score_final × 100`/five sub-score tiles/matched-missing skill chips/evidence panel with cited
quotes, three anonymized export formats).

**Gate outcome:** GREEN — ruff/black/`mypy --strict` clean; **2364 unit tests @ 91.30% coverage**; all
screens live-verified end-to-end against the real running stack (create job → LLM parse → upload →
shortlist → ranked cards, confirmed blind throughout). Reviewer **APPROVE** (after fixing one Major: the
export route had silently dropped the `?format=` query parameter, always exporting csv — now reads and
validates it against the allowed set); security **PASS** after two fixes: `MAX_CONTENT_LENGTH` (210 MiB,
sized off the backend's 10 MB/file × 20-file caps) added so an oversized multipart request 413s before
Flask buffers it into process memory, and an explicit `httpx.Timeout` (30s/5s connect) added to the
`api_client` build so outbound calls never rely on `httpx`'s implicit no-timeout default.

**Accepted LOW residual (ADR-014, documented not fixed):** the create/upload error paths render the
backend's 4xx `detail` verbatim (Jinja2-autoescaped). Today the backend only ever puts field-level
validation text there — no PII, no raw upload content — accepted for v1. If a future backend change ever
surfaces something PII-bearing or attacker-controlled in `detail`, map it to fixed friendly messages
instead of rendering verbatim.

**Deferred, not built — the reverse-match UI is now a concrete follow-up.** A "find matching jobs"
trigger button on the résumé-detail screen was scoped as an optional slice (S9) and cut for time. The
backend endpoints already exist and are unchanged (`POST /resumes/{id}/match-jobs`,
`GET /resumes/{id}/match-results`, both Phase 6), and the old `match_results.html` view remains, already
wired to `app.py::resume_match_results`. Wiring a trigger button that calls the existing
`api_client.get_match_results`/a thin new POST wrapper is a clean, low-risk follow-up needing no backend
change.

**Pre-existing, out of scope:** weak/empty `flask_secret_key`/`api_key` defaults (env-overridable) —
hardening backlog, inherited from Phase 6/7, not introduced or worsened by this feature.

## Next session

> **STATE AS OF 2026-08-04 (read the top READ-FIRST banner first).** `origin/main` == **`8d664c3`**, both
> repos PUBLIC, **zero open PRs**, working tree clean. Everything below in this section is
> the older historical log. **Shipped through this session:** all of v1 (phases 0–7), the Workflow UI,
> FU-1..FU-6, user-admin roles (ADR-025), configurable shortlist size (ADR-024), `/my/jobs`, CAS
> live-integration, **FU-8 résumé withdrawal (ADR-026, PR #37)**, the **cover-letter zip pairing fix (PR
> #42)**, the **shortlist-hides-withdrawn read fix (PR #43)**, **FU-7 §3 honest parse status (ADR-027, PR
> #44)**, the **reverse-match-hides-withdrawn read fix (ADR-026 amendment, PR #46, `6d8d33f`)**,
> **education field-of-study relevance (ADR-028, PR #49, `9229d61`)**, the **explainer follow-up (PR #51,
> `107e6bb`)**, **FU-7 §2 fail-closed ranking + §6 empty-content (ADR-029, PR #52, `79d69ac`)**, the
> **AI-usage one-pager (PR #54)**, the **Windows quickstart `scripts/quickstart.ps1` (PR #56)**,
> **FU-7 §4 degraded-parse visibility (ADR-030, PR #55, `3df47de`)**, the **dev-boot fix — unique host
> ports + CAS-on-by-default + tracked `compose.cas.yml` (PR #58, `f7dadc5`)**, and the **reproducible
> fresh-box boot — complete `.env` + `.env`-driven peer LLM + `compose.live-eval.yml` removed (PR #60,
> `e6e35d9`)** — all on `main`, CI green. **Nothing is mid-flight.**
>
> **Plan — options for the next session (a human picks; none auto-starts):**
>
> **🌟 NEXT-GEN "WOW" FEATURE MENU → [`docs/ROADMAP.md`](docs/ROADMAP.md).** Session wrapped 2026-08-04 for a
> fresh start on greater features. The roadmap details three flagship candidates — **(1) "Why this rank?"
> per-candidate defense pack, (2) "Ask the pool" NL evidence-grounded search, (3) Policy Studio live
> weight-ratification** — each framed with pitch / why-wow / thesis-fit / reuses / first-slice / risks. **Pick
> ONE to build first.** All honour offline-only (aria-gb10), evidence-backed, privacy-first. The smaller
> carried-over items (below + roadmap's tail) remain valid.
>
> 1. **ADR-026 §4 — revoke-and-purge (consent-erasure).** The natural FU-8 follow-on: a destructive PII-erase
>    path (hard-delete blob + null pgcrypto columns + `resumes.parsed`, keep a non-PII tombstone), clearly
>    separated from the routine reversible withdraw. The repo's FIRST destructive PII op — needs a human
>    decision on consent-revocation semantics + its own security review before any code.
> 2. **Remaining FU-7 (ADR-021 decision 1 only)** — decision 3 (honest parse status, ADR-027, PR #44),
>    **decision 2 (fail-closed ranking) + §6 (empty-content), ADR-029, PR #52**, and **decision 4
>    (degraded-parse visibility), ADR-030, PR #55** are all shipped. Left: **1** (LLM provider failover
>    chain — most infra-heavy; offline-only local Ollama peers, so limited payoff until there's a second
>    endpoint to fail over to). Two small FU-7 residuals: `resume_parse_max_tries`
>    has no upper sanity cap (`shortlist_max_tries` got one — mirror it), and fail-closed is NOT extended to
>    reverse-match (ADR-029 residual; mirror the withdrawn-read split #43→#46).
> 3. ~~**Reverse-match read consistency (small).**~~ ✅ **DONE — PR #46 (`6d8d33f`), 2026-07-31.** The
>    reverse-match persisted read (`_REVERSE_MATCH_QUERY` in `shortlist_service.py`) now carries the
>    correlated `_REVERSE_NOT_WITHDRAWN_SQL` guard, mirroring PR #43's shortlist filter. All five persisted
>    read paths + export now hide withdrawn consistently (ADR-026 amendment 2026-07-31).
> 4. ~~**`jd.education.fields`** (ADR-009 §7, open since 4c).~~ ✅ **DONE — PR #49 (`9229d61`), 2026-08-01,
>    ADR-028.** Extended `score_education` to read `jd.education.fields`: a qualifying-level degree in a
>    non-allowed field caps education at `education_partial` (fuzzy `token_set_ratio` ≥
>    `education_field_fuzz`). ADR-009 §7 RESOLVED. *Residual follow-up:* `docs/process/
>    ranking-metrics-explainer.html`'s policy-decision register is stale by one (the `education_field_fuzz` /
>    unknown-field decision).
> 5. **Re-privatize + PII hygiene** once finance funds `humanaxiom` billing: flip both repos back to private
>    (CI then needs the org's Actions quota funded) and run the `recruiter@sfu.ca → @example.test` scrub.
> 6. **Connectors feature** (Taleo/CSV-manifest ingest) — explicitly cut in Phase 6 (ADR-012 §2), deferred by
>    the user ("Taleo was a shortcut … more connectors in the future").
> 7. **Retire the `sfu-aria` mirror** if `humanaxiom` is the settled home — it was the billing workaround and
>    is now redundant (detached standalone public copy at `69e6ac0`).

**The v1 extraction plan is fully delivered, and FIVE post-v1 features have shipped on top of it: the
Workflow UI, then FU-1/FU-2/FU-3.** All seven plan phases (0–7) are merged to `main`, CI green: Phase 0
(PR #1, `8b2b47c`), Phase 1 (PR #2, `f7e7cbe`), Phase 2 (PR #3, `cefd545`), Phase 3 (PR #6, `49196d7`),
Phase 4a–4d (PR #8/#10/#11/#12/#13), Phase 5 (PR #14, `6deade3`), Phase 6 (PR #15, `e910669`), Phase 7
(PR #16, `1039e5c`). **There is no Phase 8** — `docs/EXTRACTION_PLAN.md`'s phase table intentionally ends
at Phase 7. **Post-v1, all merged to `main`, CI green:** the **Workflow UI** (PR #18, `3eba9cf`, ADR-014),
**FU-2** evidence chunk expansion (PR #19, `8d7ce0b`, ADR-015), **FU-1** audited reveal + cover-letter file
upload (PR #20, `bc055f4`, ADR-016), and **FU-3** bulk ingest (PR #21, `e033d31`, ADR-017). Also merged
this session: **PR #22** (`chore/fu3-merged-docs`, squash merge `2fc3d4f`) — the docs-only PR marking
FU-1/FU-2/FU-3 merged. **FU-4 — RBAC is MERGED to `main` via PR #23 (merge `961caab`, 2026-07-21),
CI green on all five gates.** It was the last item planned as of
2026-07-19; **FU-5/FU-6/FU-7 were scoped on 2026-07-20** (see "Queued next work"). Each post-v1 feature
is a named feature, not a numbered phase.

**Merged 2026-07-21, after FU-4 — `main` is now at `6db83b6`:**
- **PR #24** (`chore/fu5-7-plan`, squash merge `abb5d67`) — the docs-only FU-5/6/7 plan: ADR-019/020/021,
  9 gaps filed into their owning ADRs, the HR explainer, and the HANDOFF queued-work section.
- **PR #25** (`fix/uncited-evidence-quotes`, squash merge `6db83b6`) — **ADR-022**, an evidence-integrity
  fix in the anti-fabrication verifier. All three merge-blocking gates green. See "ADR-022 status" below,
  and read it before touching `verify_evidence` — it leaves a **HIGH** finding deliberately open.

**OPEN AS PR #27, CI GREEN, AWAITING THE HUMAN'S MERGE (2026-07-22) — branch
`fix/adr022-evidence-verifier-hardening` (HEAD `85d995c`, off `main` @ `1f526f6`):** **ADR-023**, the
evidence-verifier hardening follow-up to ADR-022 (closes three of the four items PR #25 left open and
**narrows, does not close,** the HIGH one). All three merge-blocking gates green (reviewer APPROVE,
security PASS, ranking-evals PASS); 3125 unit tests, 123 integration tests, **CI 10/10**. Merge with
`gh pr merge 27 --squash --delete-branch`. See "ADR-023 status" below. **FU-5 is the next work item after
this merges.**

### Workflow-UI enhancements — FU-1, FU-2, FU-3, FU-4 ✅ ALL MERGED

The three user-requested enhancements (built order FU-2 → FU-1 → FU-3) are **all merged to `main`, CI
green**: **FU-2** evidence chunk-id expansion (PR #19, merge `8d7ce0b`, ADR-015), **FU-1** audited reveal +
cover-letter file upload + reveal-on-shortlist-card (PR #20, merge `bc055f4`, ADR-016), **FU-3** bulk ingest
(PR #21, merge `e033d31`, ADR-017), **FU-4** RBAC (PR #23, merge `961caab`, ADR-018). FU-4 is no longer
the last planned item: **FU-5/FU-6/FU-7 were scoped on 2026-07-20** — see "Queued next work" further
down. The original per-FU detail is retained below for history; each
of FU-1/FU-2/FU-3 is DONE, FU-4 is pending merge.

**Blind-review model (user-confirmed 2026-07-17, matches hris) — now LIVE:** blind is ON at every step by
default; identity is exposed only through an explicit, **audited** reveal (FU-1, shipped: `reveal_audit`
sink + `POST /resumes/{id}/reveal` + a "Reveal identity (audited)" button on the résumé page AND each
shortlist card). **RBAC is a SEPARATE task** (FU-4 below) — mandated in the early design, still not
implemented; FU-1's reveal shipped audited-first, and RBAC (who is *permitted* to reveal, closing FU-1
residuals R1/R2/R5 in ADR-016) layers on top.

- **FU-1 — Audited reveal — ✅ MERGED (PR #20, merge `bc055f4`, ADR-016).** Shipped: `reveal_audit`
  append-only sink + `POST /resumes/{id}/reveal` (records actor/resume/timestamp, returns the un-blinded
  résumé) + a "Reveal identity (audited)" button on the résumé detail AND each shortlist card (`context`
  distinguishes origin). Also folded into #20: cover-letter **file** upload (blob-stored, worker-parsed).
  Residuals R1 (no RBAC), R2 (unaudited `GET ?reveal=true` still exists), R5 (no CSRF token) are closed by
  FU-4. Original spec below (now delivered):
  Clicking the candidate label
  ("Candidate A") on a shortlist card reveals the full, un-blinded résumé (name/email/phone/employers/
  schools/grad years). **This deliberately reverses the blind-only frontend posture** locked in
  ADR-013/014 — the user reversed that decision on 2026-07-17. Backend already supports it:
  `GET /resumes/{id}?reveal=true` decrypts PII (the frontend currently hardcodes `reveal=False` and
  never forwards reveal). Build: a reveal action on the card/entry (shortlist entry → `resume_id` is the
  link) that calls `get_resume(reveal=True)` and renders the un-blinded record; **blind stays the
  default, reveal is opt-in.** MUST be **audited** (log actor + `resume_id` + timestamp on every reveal —
  hris did this; recruiter-assistant has no reveal-audit sink yet, so add an append-only audit table/log).
  Record the reversal + the audit control in a new ADR (015). Keep the blind byte-scan tests on the
  default (non-reveal) paths.

- **FU-2 — Evidence chunk expansion — ✅ MERGED (PR #19, merge `8d7ce0b`, ADR-015).** Shipped: a pure
  `_resolve_chunk_context` resolver + an `evidence_context` CSV column + a source-text collapsible in the
  shortlist cards, redacted under blind/anon (resolve-before-pseudonym ordering) and full under reveal.
  Original spec below (now delivered):
  The evidence export (`shortlist_evidence_csv`)
  and the UI evidence `<details>` panel show opaque `evidence_chunk_ids` (`c_001`). Resolve each id → its
  real chunk text from `resumes.parsed.chunks[]` (`id → {section, text}`) and show that instead of / next
  to the id. **Redaction-aware**: under anonymized export / blind view the expanded chunk text runs
  through the same display redaction as everything else; under reveal it's full text. Backend: the export
  path (`shortlist_service.export_rows` / `shortlist_evidence_csv`) needs the résumé chunks joined in to
  resolve ids — today it likely doesn't; add a chunk-id→text resolver with redaction applied.

- **FU-3 — Bulk ingest — ✅ MERGED (PR #21, merge `e033d31`, ADR-017).** All gates green (reviewer APPROVE,
  security PASS, ranking-evals PASS), live-verified against the `hris/fixtures/llm_split` sample PDFs.
  Five slices shipped: (1) shortlist "Generating… forever" fix + Generate-gated-until-parsed + parse hint;
  (2) per-résumé cover-letter pairing by filename convention (new pure `bulk_ingest_service.py`) + results
  summary; (3) `manifest.json` pairing (precedence over convention); (4) bulk JD upload (`POST /jobs/bulk`,
  `create_jobs_bulk`, CSV manifest, `description_sha256` dedup via the repo's first idempotent `ALTER
  TABLE`); (5) reverse-match UI (candidate→jobs, POST-only trigger + bounded poll + rows link to job).
  Gates: reviewer APPROVE, security PASS (file-count-cap parity fixed), ranking-evals PASS (scoring
  byte-unchanged); ~2528 unit @ 91.37%. Live-verified against the `hris/fixtures/llm_split` sample PDFs.
  Decisions + accepted residuals: **ADR-017**. Everything below is the original plan detail, now delivered.

- **FU-3 (original plan) — Bulk ingest (local, offline): many résumés + per-résumé cover letters + bulk JDs.** The
  clarified shape of the "connectors" ask. Model: **candidates apply to a job** (résumé tied to a job);
  the **cover letter is optional** and counts as bonus intention/motivation (feeds the motivation
  sub-score). Three parts:
  1. **Bulk résumé upload** — many résumés in one action, loose files OR a `.zip` (backend already
     multi-file + zip-expands per Phase 6). NEW: **per-résumé cover-letter pairing** — match each résumé
     to its own cover letter via a `manifest.json` or a filename convention (`<base>_resume` ↔
     `<base>_cover_letter`); not all résumés have one; unmatched cover files demote to standalone/ignore.
     `upload_resumes` today takes a single `cover_letter_text` → extend to a pairing map. **hris prior
     art:** `C:\repos\hris\apps\api\src\api\services\bulk_ingest_service.py`
     (`pair_applicants`/`parse_pairing_manifest`).
     - **Cover letter must be uploadable as a FILE, not just pasted text** (user request 2026-07-18).
       Today only the pasted `cover_letter_text` textarea is wired end-to-end. IMPORTANT: the service
       layer `resume_service.upload_resumes` **already accepts a `cover_letter_file: tuple[str, bytes]
       | None` param** (currently unwired) — so the SINGLE-résumé cover-letter-file case is a small
       wire-through: add a `cover_letter_file: UploadFile` Form field to the API route
       (`routes/resumes.py::upload_resumes`) + a file input to the job-detail upload form +
       `api_client.upload_resumes` passthrough. This is the natural FIRST slice of FU-3 and can ship
       ahead of the full bulk/pairing work.
  2. **Bulk JD upload** — multiple JD files (individual OR a `.zip`) → parse each into its own job;
     optional CSV manifest mapping filename → job metadata (title/dept/…). Backend has single
     `jd-extract` + `POST /jobs`; NEW: a bulk endpoint that expands files/zip, extracts JD text per file,
     and creates + enqueues a `parse_job` per file. **hris prior art:** bulk-JD create in its `jobs.py` +
     `bulk_ingest_service.parse_csv_manifest`.
  3. **Many-to-many views** — a candidate can be shortlisted across multiple jobs; navigate candidate↔job
     both ways ("which jobs is this candidate matched to" = reverse-match; "candidates for this job" =
     shortlist). Ties to the reverse-match UI (FU adjacent).
  4. **Shortlist-generation UX fixes (from live testing 2026-07-18, folded into FU-3):**
     - **The "Generating… forever" bug** — `shortlist_cards.html` polls every 3s and shows "Generating…"
       whenever `entries` is empty, with NO stop condition. Clicking **Generate before any résumé has
       finished parsing** makes `shortlist_job` return `empty`, and the page then polls indefinitely — it
       LOOKS stuck (this is exactly what the 009_adejoke test hit). FIX: bound the poll (e.g. stop after
       ~2–3 min / N attempts) with a real empty-state ("No ranked candidates yet — make sure résumés show
       'parsed', then Generate again"), and/or disable/​warn the **Generate** button until ≥1 résumé is
       `parsed`. hris used a 20-min safety valve on this poll.
     - **Parse speed is accepted as inherent** to the offline local LLM (real PDFs take ~60–116s each to
       parse on the remote 20B model; shortlist_job adds ~30–57s) — NOT a bug. Just **surface a UI hint**
       ("large PDFs take ~1–2 min to parse on the local model") so the wait isn't mistaken for a hang.
  - **Sample data for testing:** `C:\repos\hris\fixtures\llm_split\*.pdf` (21 real résumé/cover-letter
    PDFs, e.g. `009_adejoke_adeyemi_resume.pdf`, some `NNN_name_cover_letter.pdf` pairs) — ideal for
    exercising FU-3's bulk upload + per-résumé cover-letter pairing against realistic inputs.
  Security: forward `.zip` bytes verbatim (never client-expand — preserves the backend zip-bomb/
  path-traversal guards); consent gate per résumé; blind posture unchanged. This is the **offline** half
  of the old "connectors" concept; the Taleo *job-source scraper* remains a separate, still-deferred
  thing (see the connectors bullet below).

- **FU-4 — RBAC — ✅ MERGED (PR #23, merge `961caab`, 2026-07-21, ADR-018).** Branch `feat/fu4-rbac`, off
  `main` @ `2fc3d4f`, 13 commits, merged after the org billing block was cleared and CI ran green on all
  five gates (its first real execution on this branch — see the billing bullet below). Decisions +
  full detail: **ADR-018** (`docs/adr/018-rbac-keyed-roles.md`).
  - **CI billing block — RESOLVED 2026-07-21. CI IS NOW GREEN.** For history: `Gate: branch-name` on
    PR #23 showed FAILURE with every downstream gate SKIPPED, but the job never ran — the annotation
    read *"The job was not started because recent account payments have failed or your spending limit
    needs to be increased."* Actions was disabled for the `humanaxiom` org (a private org repo meters
    Actions minutes to the org). `feat/fu4-rbac` always matched the branch-name regex fine (verified
    against `Makefile:18-22` and `.github/workflows/ci.yml:20-27`); PR #22 ran green earlier the same
    day, so this lapsed mid-session. **The human fixed org billing on 2026-07-21 and both runs were
    re-run to full green** — run `29701584800` (pull_request) and `29701583639` (push):
    `Gate: branch-name` ✅ · `Gates: ruff · black · mypy` ✅ · `Gates: unit · coverage ≥ 80%` ✅ ·
    `Gate: integration (pg + neo4j + redis)` ✅ · `✅ ALL GATES GREEN` ✅. This was the **first time CI
    ever executed on this branch** — everything before it was the never-started billing failure.
    **Diagnostic note for a future block:** the real signal is `steps` on the job, not `conclusion`. A
    billing-refused job reports `conclusion: failure` with `steps: 0`; a job that genuinely ran and
    failed has `steps > 0`. Check with
    `gh api repos/humanaxiom/recruiter-assistant/actions/runs/<id>/jobs --jq '.jobs[]|{name,conclusion,steps:(.steps|length)}'`.
    PR #23 is now MERGEABLE with gates green; the merge decision itself was left to the human.
  - **The model:** four `Role(StrEnum)` values (`admin`, `recruiter`, `hiring_manager`, `auditor`) and
    four flat settings fields (`api_key_admin`, `api_key_recruiter`, `api_key_hiring_manager`,
    `api_key_auditor`) replace the old single `api_key` switch. `resolve_role` (reads `X-API-Key`,
    401s on no/unmatched key) and a new `require_role(*allowed)` dependency factory (403s an
    authenticated-but-not-allowed role) split Phase 6's old single-boolean `require_api_key` into real
    authentication-vs-authorization. Auth-disabled (all four keys empty) still resolves every caller to
    `Role.ADMIN`, unchanged fail-open-by-explicit-configuration posture from ADR-012. Two fail-closed
    startup refusals in `validate_startup_auth_config`: a stale legacy `API_KEY` env var hard-fails boot
    (a WARNING was rejected — indistinguishable in the log stream from the legitimate disabled-auth
    WARNING), and two configured role keys being byte-identical also hard-fails boot (silent role
    collapse otherwise).
  - **The `PATCH /jobs/{id}` finding (§7 of ADR-018) — the widest blast-radius item found, not recorded
    in ADR-016.** `JobUpdate` can flip `blind_review: false`, and every redaction key in the service
    layer gates off `jobs.blind_review`, not off any per-request `reveal` flag — before this feature,
    any authenticated caller could PATCH one job and permanently un-blind every résumé and shortlist
    entry under it, for every future reader, with **no audit row written anywhere**. Now restricted to
    admin/recruiter (`_JOB_WRITERS`). The authorization gap is closed; an audit row on a `blind_review`
    flip is still not added (deferred, see ADR-018 Consequences).
  - **R2 (ADR-016) closed further than ADR-016 described.** `reveal` is removed entirely from both
    `GET /resumes/{id}` and `GET /jobs/{id}/shortlist/export` — the export case was an unaudited **bulk**
    de-anonymization (every résumé on a shortlist in one response) never recorded in ADR-016 at all.
    `POST /resumes/{id}/reveal` (admin/recruiter only) is now the only un-blinding path in the system.
  - **R5/CSRF — closed, with two load-bearing amendments worth remembering if this area is touched
    again.** (a) The first cut stored one bare token per Flask session; since the FU-1 reveal button
    appears on every shortlist card posting to the same route, minting a token for one card invalidated
    every other card's token — only the first reveal click on a page worked. Fixed by scoping the token
    map per résumé id. (b) That fix then overflowed the ~4093-byte browser cookie ceiling at the
    `MAX_TOKENS_PER_SESSION = 64` cap (~5.2 KB measured) — browsers **silently drop** an oversized cookie
    rather than error, which re-triggered the exact same regression at full shortlist size (a 50-row
    shortlist was precisely the scenario that overflowed). Fixed with `secrets.token_urlsafe(16)` +
    a 12-hex-char SHA-256 mapping key (~2,440 B measured at cap), now pinned by a regression test that
    measures the real signed cookie, not a re-derived estimate. **The lesson stated plainly in ADR-018:
    the original 64-token cap was reasoned about entropy, never measured against the serialized cookie
    — measure, don't re-derive an estimate, if this size or cap changes again.**
  - **Two honest limitations a resuming session must not miss (both accepted residuals, ADR-018).** (1)
    The Flask viewer attaches one fixed `recruiter` role key outbound for every browser it serves —
    backend RBAC is largely decorative against frontend-originated traffic (every browser gets the same
    role regardless of who's sitting at it), and every browser-originated reveal audits as the same
    actor (`reveal_audit.actor = "api"`); RBAC's real enforcement is against direct API callers. (2)
    Roles are role-level, not row-level — there is no owner/company scoping, so a single
    `hiring_manager` or `auditor` key reads every job, résumé, and shortlist company-wide.
  - **Gate state at handoff:** both merge-blocking gates green — reviewer **APPROVE** (0 critical, 0
    major; 5 minor findings, all closed in `6da32ee`), security **PASS** (16 mutations, 15 killed; the
    one survivor — an unpinned no-short-circuit invariant on `resolve_role`'s comparison loop — closed
    in `a826d97`). `ranking-evals` is **not** a required gate for this branch (no scoring code touched:
    `pipeline/matching/*`, `stages.py`, `orchestrator.py`, `matching_tasks.py` byte-unchanged). Offline:
    ruff/black/`mypy src frontend --strict` clean; **2703 unit tests @ 91.57% coverage**; **123
    integration tests** passed live against real Postgres+Neo4j.
  - **`a826d97` is a `test:`-prefixed commit, a declared TDD-order deviation, not sloppiness** — it pins
    already-correct behavior (the no-short-circuit comparison loop) that could only be shown RED by
    mutation testing, not by a normal failing-test-first cycle; same precedent as Phase 4a.

### ADR-022 status — MERGED via PR #25 (squash `6db83b6`) — READ BEFORE TOUCHING `verify_evidence`

An evidence-integrity fix in the anti-fabrication verifier
(`core/src/pipeline/matching/stages.py::verify_evidence`), merged 2026-07-21. Full detail:
[ADR-022](docs/adr/022-uncited-evidence-quote-scrub.md).

**What was wrong.** The verifier had two asymmetric arms. A quote whose text failed to match a valid cited
chunk was scrubbed (evidence blanked, `met`→`missing`, confidence capped at 0.3). A quote with **no
surviving citation** was only confidence-capped — the quote text and the `"met"` status both survived, and
it was **never text-matched against anything**. Since `good_ids` is empty whenever *every* cited id is
hallucinated, **a fabricated CITATION took the lenient arm while a fabricated QUOTE took the strict one**:
the verifier was weakest exactly where the model was least trustworthy. The cover-letter loop had the same
shape. Both arms now scrub identically.

**Three things about this worth carrying forward.**

1. **It was a considered decision, not an oversight.** `test_matching_stages.py` carried a test whose
   docstring defended the branch as "distinct from the fabrication-scrub branch." The asymmetry was
   reasoned about, written down, tested, and never revisited — **the test locked in the intent and hid the
   consequence.** It was provably wrong and was updated in place and renamed, per CLAUDE.md.
2. **The gate's own scrubbing step was manufacturing the input that made the gate blind.**
   `run_evals.py`'s verification-rate loop conditioned on `req.evidence and req.evidence_chunk_ids`, and
   `verify_evidence` sets `evidence_chunk_ids` to `good_ids` *before* the branch fires — so a
   fully-hallucinated citation reached the harness with an empty id list, precisely the shape the conjunct
   filtered out. The `ranking-evals` gate proved the hole was reachable by running the 2×2: **old verifier
   + old harness PASSED with a fabrication riding through the entire corpus gate undetected.**
3. **The harness loop is now a backstop, not a live assertion.** On the unmutated corpus the new condition
   is extensionally inert (`surfaced == 81` either way, `uncited_surfaced == 0`), because the deterministic
   stand-in only cites real ids and the fixed verifier makes uncited quotes impossible by construction. Its
   falsifiability rests entirely on `core/tests/unit/test_evals_uncited_quote_gate.py`. **Delete that file
   and reverting the harness line becomes silently undetectable.**

**It was found by fact-checking a document, not by reading code** — the `reviewer` pass on
`docs/process/ranking-metrics-explainer.html`, which tells HR that "every quote shown to a human must
survive a match against the real document." The claim was false, and chasing it found the defect.

**Scope: display/integrity, not scoring.** The 0.3 cap already held these below `_evidence_completeness`'s
`met AND confidence >= 0.7` bar. The corpus confirms it — the full 20-fixture ranking is **byte-identical**
to `main` @ `961caab` (every `score_final` delta `+0.000e+00`, r09 still 12th, all five ordering pairs
unchanged). The invariance is conditional on `evidence_met_confidence > 0.3`; `match_evidence_met_confidence`
is env-settable, and below the cap the fix *does* move completeness (toward correctness). Pinned explicitly.

**Gate state (HEAD `1e1776c`):** reviewer **APPROVE**, security **PASS** (7/7 mutations killed, zero
findings introduced by the diff), ranking-evals **PASS** (six standing mutation obligations still FAIL as
required on both input orders). 2730 unit tests @ 91.64%; CI green on all five gates including integration.

### ADR-023 status — evidence-verifier hardening — PR #27 OPEN, CI green, AWAITING HUMAN MERGE

> **Resume here.** Branch `fix/adr022-evidence-verifier-hardening`, HEAD `85d995c`, pushed to origin and
> opened as **PR #27** (https://github.com/humanaxiom/recruiter-assistant/pull/27) on 2026-07-22.
> **CI is fully green — 10/10 checks, zero pending, zero failures**, including `✅ ALL GATES GREEN` and
> `Gate: integration (pg + neo4j + redis)`. `gh pr view 27` reports `MERGEABLE` / `CLEAN`.
> **Nothing is left to build.** The only outstanding action is the human running:
>
> ```
> gh pr merge 27 --squash --delete-branch
> ```
>
> (`gh pr merge` is classifier-blocked for the agent — see the standing note in this file; drive to green
> and hand over the command.) If you are resuming and PR #27 is already merged, this whole section is
> history: go to "Queued next work" and start **FU-5**. **Verify against origin before trusting either
> state** — `git fetch && gh pr view 27`.

Branch `fix/adr022-evidence-verifier-hardening` (HEAD `85d995c`, off `main` @ `1f526f6`). The `security`
gate had passed PR #25's diff but found a **HIGH pre-existing defect in the same function that PR #25
deliberately did not close**; the human agreed on 2026-07-21 to merge #25 as-is and take these as one
focused follow-up branch. **That branch is done: all four items below are closed or narrowed, all three
merge-blocking gates are green, and the "next work item" flag on this section is cleared — FU-5 is next**
(see "Queued next work" below). Full detail: [ADR-023](docs/adr/023-evidence-verifier-hardening.md); ADR-022
itself now carries a superseded-by note pointing here, and its follow-up item #2 had the wrong fix site —
corrected in place in ADR-022, restated below.

1. **`partial_ratio` superset bypass — NARROWED, not closed.** A length guard
   (`len(collapsed needle) > len(collapsed haystack) → 0.0`) turns the old unbounded-append bypass into a
   bounded one: a quote at or under its cited chunk's length can still replace roughly a quarter of that
   chunk's content with invention and verify — `chunk[:130] + " ALSO CTO"` on a 148-char chunk scores
   **0.982**. The guard is fully closed (no `k` multiplier), not ratio-bounded: a +1-character append on a
   148-char chunk scores 1.007, so no `k` separates that from a genuine re-quote. Framed as a structural
   invariant ("a quote is a span of exactly one cited chunk"), not a `MatchWeights` setting.
2. **NUL-byte / C0 availability bug — CLOSED, at the correct site.** (bug was in
   `shortlist_service.py:109-123`.) A NUL byte in a quote survives
   the verifier, `json.dumps` emits a `\u0000` escape, and Postgres rejects it outright — **the whole
   `persist_shortlist` transaction dies, so one malformed quote loses the entire shortlist.** The fix-site
   instruction originally recorded here — strip C0 controls in `verify_evidence` — was **wrong; do not
   follow it.** `verify_evidence` never rewrites `requirement` / `overall_summary` / `overall_motivation`,
   so scrubbing there would leave those three fields reaching `json.dumps` unscrubbed. ADR-023 scrubs at
   the schema boundary instead (`schemas/matching.py`'s `CleanText` annotation), covering every free-text
   evidence field, including future non-`verify_evidence` producers.
3. **Unbounded evidence fields — CLOSED at ingest, deliberately left OPEN on read.** (was
   `schemas/matching.py:127,147,157`, no `max_length`). Strict `*Ingest` models cap size at the LLM
   boundary (`chat_json` in `orchestrator.py`); read/DTO models stay uncapped by design — capping the read
   model turns any pre-existing over-cap row into a 500 for the whole shortlist endpoint, and this project
   has no migration framework to fix that after the fact. The requirements-list bound is a `mode="before"`
   validator, since bare `max_length` let pydantic validate all 100,000 items before raising — the original
   cap never prevented the DoS it targeted.
4. **No minimum quote length — CLOSED, recalibrated by measurement.** Floor set to 16 chars, lowered from
   an initial 32 after measuring that 32 also scrubbed genuine short evidence indistinguishably from
   fabrication (`"PhD in Computer Science"` = 23 chars, `"AWS Solutions Architect"` = 23,
   `"Postgres schema migrations"` = 26 — all blanked at 32).
5. **LOW, accepted residuals — UNCHANGED, still open.** Homoglyph substitution scores 0.879 and passes the
   0.85 bar; chunk ids are not globally unique (`c_001` exists in every résumé), so cross-résumé isolation
   rests on `chunks_by_id` being built per-candidate — correct at both call sites today, undefended by any
   assertion, and a future batching refactor would silently verify quotes against the wrong person's
   document.

**Deferred, recorded as open work, not silently dropped:**

- **Fix A — span-quoting the eval-corpus stand-in.** `_extract_evidence` quotes each cited chunk *in
  full*, so all 81 corpus-surfaced quotes are byte-identical to their chunk: the length floor never binds,
  the length guard sits exactly on its own boundary, and whitespace/control scrubbing are no-ops on it.
  **This branch found the ranking-evals corpus could falsify none of its own six new guards** — every
  guard mutation survived unmutated on both input orders — the third recurrence in this repo's history of
  a control asserting what code *should* do rather than exercising what it *does* (see the Phase 4a
  "recurring lesson"). Closed for this branch with additive probe fixtures and a dedicated
  `core/tests/unit/test_evals_verifier_guard_gate.py` (20/20 mutation kills across 10 mutations × 2 input
  orders), but the stand-in itself is unchanged and needs its own re-baselining against the labelled
  corpus — deferred as the recommended next hardening step, not done here because it would move every
  evidence-derived score the stand-in produces.
- **The 26% bounded-replacement residual** on item #1 above — not closed, see ADR-023's
  Consequences/residuals section.
- **Ellipsis-joined quotes score 0.792 and are scrubbed as fabrication** — `"...start ... end..."` is a
  common genuine LLM quoting idiom for a non-contiguous span; pre-existing (a property of `partial_ratio`
  at the 0.85 bar, not introduced this branch) but newly documented here.

**HR explainer banner stays.** `docs/process/ranking-metrics-explainer.html`'s DRAFT/NOT-FOR-CIRCULATION
banner was scoped by ADR-022 to come off once item #1 closed; #1 is only partially closed (the 26%
residual above), so the explainer's claim that "every quote shown to a human must survive a match against
the real document" is still not literally true. The banner's stated removal trigger now points at the
bounded-replacement residual rather than at "#1 closed."

**Gate state (HEAD `85d995c`):** reviewer **APPROVE**, security **PASS**, ranking-evals **PASS** — ranking
byte-identical to `main` (expected, and per the Fix A finding above not on its own evidence that the new
guards work; see the dedicated guard-gate test for that). **3125 unit tests**; **123 integration tests**
passed live against real Postgres + Neo4j. **CI on PR #27: 10/10 green.**

The reviewer's APPROVE is a **re-review**. Its first pass on `3fdae95` was CHANGES-REQUIRED (2 major, 4
minor, 3 nits); the re-run independently rebuilt the guard-mutation battery rather than trusting the
branch's own numbers, and killed **20/20 across 10 mutations × 2 input orders**. It also confirmed
branch-wide test integrity: `git diff 1f526f6..HEAD -- core/tests/` removes **exactly one line**, an unused
import. Every intra-branch test edit has a stronger replacement — details in ADR-023.

**Commit shape (13 commits, TDD-ordered).** `red:` 91 failing tests → `green:` src-only → drift pin →
`red:`/`green:` reviewer findings → `red:`/`green:` security findings → `red:`/`green:` corpus probes →
`docs:`. Two rounds fixed defects that this branch's own fixes introduced (the read-path 500s from the new
caps; a symmetric-scrub defect the coder caught and closed itself, where scrubbing the needle but not the
haystack made a chunk fail to match *itself* at 0.838). Severity fell monotonically across rounds — HIGH →
2 major → 5 medium/low → self-caught — which is why it was allowed to run five rounds rather than being
split.

### ⚠️ LOCAL-INTEGRATION STATE (2026-07-28) — billing CLEARED, origin caught up to FU-6, four features remain local-only

> **Read this FIRST — supersedes the 2026-07-25 version of this banner below (kept as history, not
> deleted).** The org's GitHub-Actions **billing block is now CLEARED** (2026-07-28) — CI runs again.
> `origin/main` has advanced to **`c2f6a57`**, which now contains, via merged GitHub PRs: **FU-5** (#29,
> `ae18687`), the **live-app UX bug fixes** (#30, origin squash `22db93f`), and **FU-6 core** (#31, origin
> squash `c2f6a57`). CI on all three is green.
>
> **Local `main` is at `2b2f291`, 24 commits AHEAD of `origin/main` and 2 BEHIND it.** This is a SHA-only
> divergence, not a content divergence: origin's #30/#31 landed as squash commits with different SHAs than
> local's own merge commits for the same content (`git diff origin/main main` on the FU-6 files is EMPTY).
> **Consequence: local `main` cannot fast-forward push** — a plain `git push` will be rejected.
>
> **Four finished, fully-gated features are STILL local-only** — none of them ever had their own PR, so
> they did not ride in with #29/#30/#31:
> 1. **Configurable shortlist size** — ADR-024, commit `2e2da05` (per-job `shortlist_top_percent`, 1–100%,
>    default 100).
> 2. **`/my/jobs` hiring-manager viewer default** — commit `f3b2998` (ADR-020 §7 "viewer half"; it landed
>    after the local FU-6 merge, so PR #31 does not contain it).
> 3. **CAS live integration** — commits `adb55fd` (split-origin post-login redirect fix) + `d54a6be`
>    (header auth widget: user · role · Logout/Login) + the `compose.cas.yml` override that enables real
>    SFU CAS. **(Updated PR #58, 2026-08-02: `compose.cas.yml` is now TRACKED, its CAS URLs are
>    port-parameterized `${API_PORT}`/`${FRONTEND_PORT}` — no longer the stale `:18000`/`:5000` — and CAS is
>    ON BY DEFAULT in `scripts/quickstart.ps1` (`-NoCas` opts out). The earlier "untracked operational
>    scratch" description no longer applies.)**
> 4. **User-admin roles** — ADR-025, slices 1–8 up to `45eba6d` (no-role-by-default first login, reversing
>    ADR-019 §10a; the fail-closed `require_role_assigned` gate; admin-session-gated `GET /users` +
>    `PATCH /users/{id}/role` with atomic `role_changed` audit + last-admin lockout; the Flask
>    `/admin/users` page). Confirmed absent from `origin/main`: `docs/adr/025-*.md`,
>    `core/frontend/templates/pending_access.html`, `core/src/api/routes/users.py`.
>
> **Verification:** `./scripts/verify.sh all` re-run GREEN on the local tip — **3815 unit tests @ 92.6%
> coverage, 375 integration tests**. All four features are applied to the running stack (the `users.role`
> DDL reversal is live in the running Postgres; `asalah` is the sole active admin).
>
> **Reconciliation path (exact next step — human/next session runs it; the push/PR-merge itself is
> classifier-blocked for the agent, same as every prior PR in this repo):**
> - **Preferred:** rebase just the four local-only commits onto `origin/main` — `git rebase --onto
>   origin/main <local-FU-6-tip> main` (drops local's now-redundant #30/#31 merge commits, since that
>   content is already on origin under different SHAs) — then `git push origin main`.
> - **Alternative:** open one fresh PR per remaining feature (shortlist-config, my-jobs, CAS-integration,
>   user-admin) off `origin/main` and let CI gate each individually — cleaner for review, more branches.
> - Either way: do **not** double-merge — #29/#30/#31 are already on origin, do not re-open or re-merge
>   them.
>
> **CAS status, precisely:** the FU-5 CAS *backend* (identity, session store, ticket dance) is merged to
> `origin/main` (#29); the CAS *live-integration polish* — the split-origin redirect fix and the header auth
> widget (item 3 above) — is still local-only. Both are applied to the running stack (`compose.cas.yml` is
> live), and `asalah` is the §10a default-admin turned sole active admin under the ADR-025 role reversal.
>
> **No open follow-ups from this session remain** beyond the reconciliation path above.

<details>
<summary>Superseded 2026-07-25 version of this banner (billing-blocked state) — kept for history</summary>

> **Read this FIRST.** GitHub Actions is **billing-blocked** for the `humanaxiom` org (jobs fail at "Set up
> job" with `BlobNotFound`; the failure annotation reads "recent account payments have failed or your
> spending limit needs to be increased"). Fix in the org's **Settings → Billing & plans**. Until then, CI
> cannot run — but `./scripts/verify.sh all` runs the **same Makefile targets** locally against real
> Postgres/Neo4j/Redis and is the source of truth.
>
> Because of that block, **four finished, fully-gated features were squash-integrated into LOCAL `main`**
> (not pushed), each verified `./scripts/verify.sh all` green:
> - `202c9ac` — UX bugfixes (was **PR #30**): zip-upload friendly error, upload-missing-job 404, slow-job
>   progress honesty + elapsed, advisory-lock dedupe of concurrent runs. reviewer APPROVE, security PASS.
> - `1dfa590` — **FU-6** (was **PR #31**): per-job assignment + row-level scoping (ADR-020). reviewer
>   APPROVE (12 mutations), security PASS.
> - `2e2da05` — configurable shortlist size (ADR-024): per-job `shortlist_top_percent` (1-100, default 100).
>   reviewer APPROVE, security PASS, **ranking-evals PASS** (default byte-identical).
> - `f3b2998` — hiring_manager `/my/jobs` default view (ADR-020 §7 viewer half). reviewer APPROVE.
>
> **`origin/main` is still at `ae18687` (FU-5).** PRs #30 and #31 are OPEN on origin but their work is now in
> local `main`. **GitHub reconciliation, once billing clears:** push local `main` (`git push origin main`)
> and CLOSE #30/#31 as superseded (their commits are integrated), OR if you prefer the PR flow, reset local
> `main` to origin and merge the PRs on GitHub instead — but do NOT do both (double-merge). The feature
> BRANCHES (`fix/upload-and-progress-ux`, `feat/fu6-job-assignment-scoping`, `feat/configurable-shortlist-size`,
> `feat/hiring-manager-my-jobs-view`) still exist locally/on origin for reference.
>
> **CAS is built + merged (FU-5) but NEITHER deployed NOR enabled** — running containers predate FU-5;
> `cas_enabled` defaults False. Rebuild off `main` + `CAS_ENABLED=true` to activate; `asalah` is the §10a
> default-admin.
>
> **No open follow-ups from this session remain** — the two ADR-020/shortlist follow-ups were both built.

</details>

### FU-8 status — MERGED to `main` via PR #37 (squash `0162302`), CI green

FU-8 (résumé withdrawal, ADR-026 exclude-and-retain slice) is **MERGED to `origin/main`** — PR #37, squash
`0162302`, 2026-07-29. All five gates green (reviewer APPROVE with 8/8 load-bearing mutations killed,
security PASS, ranking-evals PASS, coordinator-independent `./scripts/verify.sh all` = 3958 unit @ 92.63% +
422 integration), and **CI re-ran the full suite green in the cloud** on the now-public repo before merge.
Branch `feat/fu8-resume-withdrawal` was deleted on merge (local pruned too). The §4 revoke-and-purge
(destructive consent-erasure) path is **still DEFERRED, not built** — the natural next follow-on.

**What shipped (ADR-026 decisions 1–3, 5 — decision 4's revoke-and-purge path is still DEFERRED, not
built):**
- Nullable `withdrawn_at TIMESTAMPTZ` + `withdrawal_reason TEXT` on `resumes` (idempotent
  `ALTER … ADD COLUMN IF NOT EXISTS` + partial index `resumes_withdrawn_idx`) — not a new `resume_status`
  enum value.
- `POST /resumes/{id}/withdraw` + `/reinstate` (`admin`/`recruiter`, `require_role`), audited via FU-5's
  `audit_log` (ADR-019) — audit + outbox written atomically inside one `conn.transaction()` with the flag
  flip. Withdraw is idempotent (repeat is a no-op, zero extra audit/outbox rows).
- Exclusion via ADR-026 decision 3's **option 1**: on withdraw, `unproject_resume` DETACH-DELETEs the
  `Resume` + `ResumeChunk` nodes from Neo4j (`resume_summary_idx` recall left in place) via a
  `resume.withdrawn` outbox event + drainer branch. **`stages.py`/`orchestrator.py` are byte-unchanged**
  (ranking-evals confirmed an empty diff). Reverse-match now returns `"withdrawn"` and writes zero entries.
- **Reinstate = replay (human decision this session, not in the original scoping doc):** re-enqueues the
  last delivered `resume.parsed` outbox payload — no re-embed/LLM call — restoring byte-identical recall.
- **Withdrawn-during-parse race fixed in-scope (human decision this session):** `parse_resume` now skips
  the `resume.parsed` outbox enqueue when `withdrawn_at` is already set, while still reaching
  `status='parsed'`.
- Per-job status breakdown `GET /jobs/{id}/resume-status` (all roles, integer counts only) + a frontend
  HTMX widget.
- Frontend: withdraw/reinstate buttons on the résumé detail page and shortlist cards (blind posture
  unchanged); **CSRF token is now action-keyed** `(resume_id, action)` so the reveal button and the
  withdraw button coexist without invalidating each other's one-shot token.

**Gate verdicts, all green:** reviewer APPROVE (8/8 load-bearing mutations killed: idempotency no-op,
atomic-transaction rollback, Neo4j un-projection exclusion, reinstate recall restoration, reverse-match
zero-rows, parse-race skip, CSRF action-keying, RBAC 403); security PASS; ranking-evals PASS (scoring
byte-unchanged, corpus exits 0). Coordinator-independent `./scripts/verify.sh all`: **3958 unit tests @
92.63% coverage, 422 integration tests**, exit 0.

**Commit chain:** `cd540ef` (ADR scope) → `fc46cea` (ratify FU-8) → `9ea8a27` red backend → `a2e5437`
green backend → `0c51b8a` red frontend → `ed7701c` green frontend.

**Accepted residuals (from security, record them):**
- **R-1 (low)** — `withdrawal_reason` at-rest cleartext in `resumes.withdrawal_reason` +
  `audit_log.details`, same accepted boundary as `failure_reason`/ADR-007 §6, never embedded or written to
  Neo4j.
- **R-2 (low)** — `GET /jobs/{id}/resume-status` is an all-role aggregate count oracle (integers only, no
  PII), per ADR-026 decision 5.
- **R-3 (info)** — `withdrawal_reason` does not flow into CSV export (a pre-existing CSV residual, not
  extended by FU-8).
- Ranking-evals recommendation (non-blocking): a withdrawal-aware end-to-end check belongs in the live
  eval, not the offline corpus.
- Still-open, unchanged by FU-8: `jd.education.fields` remains decorative (ADR-009 §7).

Full decision record: [ADR-026](docs/adr/026-resume-withdrawal-lifecycle.md) (see its "Built (FU-8,
2026-07-29)" section). **Next action:** human pushes the branch and opens the PR; FU-7 (ADR-021, LLM
failover + fail-closed ranking) remains the next unbuilt, scoped item after FU-8 merges.

### FU-6 status — BUILT, gates green, ON A PR (read this before starting FU-7)

> **SUPERSEDED 2026-07-28 — FU-6 merged.** PR #31 merged to `origin/main` (squash `c2f6a57`), CI green.
> This section's "Resume here" pointer and PR-open framing are historical; see the "⚠️ LOCAL-INTEGRATION
> STATE (2026-07-28)" banner near the top of this file for the current state and next step. Body kept below
> for the FU-6 architecture/decision record.

> **Resume here** *(historical — see supersession note above)*. FU-6 (per-job assignment + row-level scoping, ADR-020) is **code-complete and
> gate-green** on branch `feat/fu6-job-assignment-scoping` (10 TDD slices + a note-cap hardening, each a
> `red:`→`green:` pair, off `main` @ `ae18687`). Both merge-blocking gates passed: **reviewer APPROVE**
> (12 mutations caught across the scoping predicates / helper / reveal ordering / `/my/jobs` / auditor
> logging), **security PASS** (no critical/high — IDOR, fail-open, existence-oracle, assignment-privilege
> all cleared). `./scripts/verify.sh all` green — ~3523 unit @ ~92%, 308 integration vs real
> Postgres+Neo4j. Next action: **open/verify the PR and hand the merge to the human** (`gh pr merge` is
> classifier-blocked). If FU-6 is already merged, skip to FU-7. Verify against origin: `git fetch && gh pr list`.

**FU-6 keys off the CAS SESSION role, not the API key** (ADR-020 §8, the crux reconciliation). ADR-020
predates FU-5's CAS pivot and assumed the key carried identity; the Flask viewer actually sends one shared
`recruiter` key for every browser user, so scoping keys off `resolve_user().role` via
`scoped_user_id_or_403`. A real hiring_manager session scopes to `user.id` even under the shared recruiter
key; a hiring_manager *key* with no/mismatched session **403s** (fail-closed). admin/recruiter/auditor +
dev-anonymous are unscoped (auditor unscoped per §4, with read-logging as the compensating control).

**What shipped (10 slices):** `job_assignees` table; `job_assignee_service`; assign/unassign routes
(admin/recruiter only, real-assigner gate, atomic audit); `scoped_user_id_or_403`; row-scoping on the jobs/
résumé(+reveal)/shortlist reads (an `EXISTS` on `job_assignees`, present only when `user_id` set, SQL
byte-identical when None; a blocked reveal 404s with ZERO audit rows and no decrypt); auditor read-logging
on the 4 deliberate reads (not the polled lists); `GET /my/jobs` (scopes to the caller directly); `role`
on `GET /auth/cas/user`. Unassigned resources 404 (not 403 — §5, no existence oracle). Full detail +
residuals: **ADR-020 §8**.

**Deferred follow-ups (post-merge, flagged for the human):**
1. **Flask viewer default-view switch** — the API side (`role` on `/auth/cas/user`) shipped; the Flask
   change making a hiring_manager land on `/my/jobs` needs PR #30's session-cookie forwarding + `get_cas_user()`
   helper, so it lands as a small follow-up on `main` once **both PR #30 and FU-6 merge** (building it on
   either branch alone duplicates the Flask plumbing). Suggested merge order: #30, then FU-6, then the switch.
2. **Per-job configurable shortlist size (top P%, 1-100%)** — user-requested 2026-07-24. Today the shortlist
   persists ALL ranked candidates up to `match_coarse_k=50` (top `match_evidence_k=15` get LLM evidence);
   there is NO fixed cap. Decision: **per-job** `shortlist_top_percent` column (default 100% = today), set at
   job creation + editable, capping the persisted shortlist to the top P% of the ranked pool. Its own branch,
   after FU-6.
3. **Assign-route session-role check** — when the assignment UI/proxy is added, `_require_real_assigner` must
   also verify `user.role in {admin, recruiter}` (ADR-020 §8; latent-low today, no viewer proxy exists).

**CAS is built + merged (FU-5) but NEITHER deployed NOR enabled.** The running containers were built
2026-07-17 (pre-FU-5), and `cas_enabled` defaults False (no `CAS_*` env set) → dev-anonymous admin mode.
To activate: rebuild off `main` (`docker compose build`), set `CAS_ENABLED=true` (+ `CAS_SERVER_URL`
defaults to `https://cas.sfu.ca/cas`); first login by the §10a default-admin (`asalah`) lands as admin.

### FU-5 status — BUILT, gates green, ON A PR (read this before starting FU-6)

> **SUPERSEDED 2026-07-28 — FU-5 merged.** PR #29 merged to `origin/main` (`ae18687`), CI green. This
> section's "Resume here" pointer and PR-open framing are historical; see the "⚠️ LOCAL-INTEGRATION STATE
> (2026-07-28)" banner near the top of this file for the current state and next step. Body kept below for
> the FU-5 architecture/decision record.

> **Resume here** *(historical — see supersession note above)*. FU-5 (CAS identity + attributable audit, ADR-019) is **code-complete and gate-green**
> on branch `feat/fu5-cas-identity` (13 TDD slices, each a `red:`→`green:` pair, off `main` @ `1f526f6`).
> Both merge-blocking gates passed: **reviewer APPROVE** (8 security-critical guards mutation-verified),
> **security PASS** (no critical/high). `./scripts/verify.sh all` green — 3358 unit @ ~91.9%, 202
> integration vs real Postgres+Neo4j. `ranking-evals` n/a (no scoring code touched). **The next action is
> to open the PR and hand the merge command to the human** (`gh pr merge` is classifier-blocked for the
> agent — drive to green, hand over). **If you are resuming and FU-5 is already merged, skip to FU-6.**
> Verify against origin first: `git fetch && gh pr list`.

**Architecture pivot (human decision, 2026-07-22).** The build did NOT follow ADR-019 §8's original
Flask+HMAC-assertion design. Reconnaissance found a production-proven CAS implementation in `C:\repos\hris`
(FastAPI + Postgres-backed sessions + opaque httpOnly cookie), and the human directed FU-5 to **port it**.
ADR-019 **§10/§10a/§10b** record the supersession and are the source of truth; §8 is marked superseded.
Two ratified sub-decisions: **§10a** a settings-driven default-admin allowlist (`default_admin_cas_username`,
default `"asalah"`) grants admin on that user's *first* login only (reverses §9's no-bootstrap-admin
stance); **§10b** when `cas_enabled=False` (the shipped-compose default) a synthetic dev-admin is resolved
and reveal's human gate is skipped (audited as `actor_kind='service'`) so dev/CI stay usable.

**What shipped (13 slices):** `users`/`audit_log`/`sessions` DDL; CAS+session settings; the `cas_service`
CAS 2.0 ticket-validation client (stdlib `ElementTree`, XXE-safe); `session_service`/`user_service`
(provisioning + the default-admin grant, role-sticky `ON CONFLICT`); FastAPI CAS routes
(`/auth/cas/{login,validate,logout,user}`) + `resolve_user` + dev-anonymous; **`X-Actor-Name` retired**
(§8.3 — `created_by`/`uploaded_by`/reveal actor now come from the session, NULL for services);
**reveal → `audit_log`** with the §7 human gate (403 for non-human, audit-row-before-decrypt so it
survives a crash); **`blind_review` flips → `audit_log`** (atomic with the flip — opposite ordering to
reveal); `GET /audit/reveals-legacy` (admin+auditor, §9.4 — the auditor's first capability); Flask
forwards the `ra_session` cookie + a fail-closed session gate; sliding-window session refresh wired into
the request path. Full detail: **ADR-019 §10/§10a/§10b/§10c** (§10c lists the build, the closed security
findings, and the accepted residuals).

**Security findings closed in-branch (slice 13):** a CONFIRMED-low open redirect (`next` now sanitized to
safe relative paths at all six sinks) and an insecure-cookie startup warning. **Accepted residuals** (in
ADR-019 §10c, hand to a hardener): `session_cookie_secure=False` default (now warns), `X-Forwarded-Host`
trust behind `cas_service_from_request` (default off), the `httpx` log mute, the pre-existing weak
`flask_secret_key` default, no role-provisioning path beyond §10a, and the dormant unwired
`cas_dev_fake_user` setting.

**Stacking note.** This branch is stacked on the two harness-hardening commits (`08ff27d`/`51f383e`) that
are **also** open as **PR #28** (`chore/agent-harness-hardening`). If #28 merges first, the FU-5 diff
against `main` cleans up; otherwise the FU-5 PR's diff will include them. Decide merge order with the human.

### Queued next work — FU-5, FU-6, FU-7 (user-scoped 2026-07-20)

> **Status of this planning work: MERGED to `main` via PR #24 (squash `abb5d67`), 2026-07-21.** It was
> uncommitted at the 2026-07-20 session end (working-tree files on `feat/fu4-rbac`, no branch), then
> committed to its own docs-only branch `chore/fu5-7-plan` — the separate-branch option that had been left
> on the table, rather than growing PR #23 — and merged with CI green on all five gates.
> `docs/adr/{019,020,021}-*.md` and `docs/process/` are tracked files on `main` now, not working-tree
> scratch. **Two earlier versions of this block said the opposite**; if you are looking for uncommitted
> planning work, there is none.
>
> **Build order changed on 2026-07-21, then completed the same day:** the ADR-022 evidence-verifier
> hardening branch (ADR-023, see the section immediately above) was inserted **before** FU-5, and is now
> **DONE** — gates green, deferred items recorded. ~~FU-5 is the next work item.~~ **FU-5 is now also
> BUILT and gate-green on a PR (2026-07-24) — see the "FU-5 status" section immediately above; FU-6 is
> the next work item once FU-5 merges.** FU-5 → FU-6 → FU-7 is otherwise unchanged.
>
> Still uncommitted-by-design: `compose.live-eval.yml` is gitignored and now carries
> `LLM_TIMEOUT_S: "300"` for the worker. A fresh clone will not have it and will hit the 120s parse
> failure described in the incident below.

**This is a queued plan, not an options list.** The user scoped it on 2026-07-20 after an operational
incident (below) exposed the silent-failure class. Build order matters: **FU-5 → FU-6 → FU-7**, because
FU-6's scoping predicates and FU-7's attributable failure states both key off FU-5's `users` table.
Each is a named feature, not a numbered phase — `docs/EXTRACTION_PLAN.md`'s table stays closed at Phase 7.

**The full current build order is: ~~ADR-022/ADR-023 hardening~~ (DONE) → FU-5 → FU-6 → FU-7.** The
hardening branch was inserted ahead of FU-5 on 2026-07-21 (human decision) because it carried a HIGH
security finding that was live on `main`, in the ranking path, and ungated; it is complete (see "ADR-023
status" above) and **FU-5 (CAS identity + attributable audit, ADR-019) is now the immediate next work
item.**

- **FU-5 — CAS identity, user records, attributable audit (ADR-019).** Adds the first real `users` table
  (there is none today), authenticates humans via **CAS** rather than an API key, moves `role` onto the
  user row so roles become data instead of a hardcoded `StrEnum`, and generalizes `reveal_audit` into an
  `audit_log` that also captures **`blind_review` flips** (today's widest-blast-radius unaudited action).
  Closes ADR-018's actor-attribution residual: a reveal will name a person instead of `"api"`.
  **On CAS and offline-first:** CAS is **SFU-hosted internal infrastructure**, not a cloud API — it does
  not breach CLAUDE.md's "NEVER add cloud API calls" constraint, and no data leaves the institution.
  What it adds is a runtime dependency *outside the compose stack*: unlike Postgres and Neo4j, it is not
  a container this project starts or can restart. ADR-019 §3 records that honestly — CAS unreachable =
  fail closed on new logins only, existing sessions keep working, and local model inference is entirely
  unaffected. Service-to-service API keys survive as a *separate* mechanism that can never satisfy an
  action requiring an attributable human.
- **FU-6 — Per-job assignment and row-level scoping (ADR-020).** A `job_assignees` table; hiring managers
  and auditors see only assigned jobs. **Scoping is enforced in SQL, not in the handler** — a Python-side
  filter after fetching is a leak waiting to happen. Unassigned reads return **404, not 403**, so the
  existence of a requisition is not leaked. Closes ADR-018's role-level-not-row-level residual. One item
  is deliberately left for ratification rather than silently decided: whether `auditor` should be scoped
  at all, since scoping an auditor may defeat the role's purpose.
- **FU-7 — LLM failover and fail-closed ranking (ADR-021).** An ordered provider chain (A → B) with
  per-provider breakers, failover only on availability errors and never on schema-validation failures.
  **The pipeline refuses to emit a ranking containing a silently-zeroed component** — a job blocks in an
  `awaiting_llm` state and retries rather than publishing a degraded shortlist. Also makes parse status
  honest: claim `uploaded → parsing` on start, write `failed` + `failure_reason` on retry exhaustion, and
  surface partial-parse degradation instead of marking it `parsed`.
- **HR-facing explainer — REVIEWED 2026-07-21, verdict CHANGES-REQUIRED. Banner added; do NOT send to
  HR. CRITICAL-2 is FIXED IN CODE (ADR-022, PR #25) but the document is still wrong and still blocked.
  UPDATE (ADR-023, same day): the hardening branch has now landed and narrows the `partial_ratio` superset
  bypass (ADR-022 follow-up #1) from unbounded to a bounded ~26%-of-chunk replacement — this is NOT a
  closure.** The document's anti-fabrication claim remains false, CRITICAL-1 plus all five MAJORs below are
  still untouched, and the banner's stated removal trigger has been rewritten in the document itself to
  point at the bounded-replacement residual (ADR-023) rather than at "the hardening branch lands." Do not
  remove the banner on the strength of ADR-023 landing alone. `docs/process/ranking-metrics-explainer.html`
  is a plain-language explainer of the scoring model
  for HR/compliance, with Mermaid diagrams and a **ratification register** of 15 policy decisions
  currently encoded as config defaults. It has now had the `reviewer` pass it was flagged as needing.
  **All fourteen weight values fact-checked MATCH** the code (`schemas/matching.py`, `settings.py`), as do
  the recency bands, the over-qual constants, the 0.85 fuzz bar, the k values, and — creditably — the
  education field-blindness, the motivation denominator, and the reverse-match incomparability. The
  narrative around them is where it fails. A `⛔ DRAFT — NOT FOR CIRCULATION` banner is now rendered at
  the top of the file; remove it only when the CRITICAL/MAJOR items are closed.
  - **CRITICAL-1 — the senior-candidate must-have exemption is undocumented.** The doc (and register items
    2 and 7) say a must-have miss halves the skills score. `stages.py:168-175`: if
    `is_senior_candidate` (≥`implied_seniority_factor=1.5`× the JD's min years) AND ≥`implied_min_coverage=0.5`
    of must-haves matched, the penalty is `implied_experience_relief=0.75` instead. **More years buys a
    lighter penalty for lacking a mandatory skill** — a years-correlated advantage, while the doc flags
    only a years-correlated *penalty*. Those three constants are also absent from the register.
  - **CRITICAL-2 — "every quote shown to a human must survive a match against the real document" is
    false, and this is a CODE gap, not only a doc gap.** `stages.py:291-295`: a requirement with a quote
    but **no surviving citation id** caps confidence at 0.3 and **keeps the quote text and the `"met"`
    status without ever text-matching it**. The doc's own diagram shows the fabrication check running on
    that path; it does not. The evals gate cannot catch it either — `run_evals.py:436` guards on
    `if req.evidence and req.evidence_chunk_ids`, so uncited quotes are **excluded from the 100%
    verification-rate figure**. No display-path confidence filter exists
    (`shortlist_service.py:880-890` passes confidence through untouched). Net: an uncited, unverified,
    model-authored quote renders to a reviewer labelled "met". **Treat this as a product defect to
    triage, not a wording fix.**
  - **MAJOR-3 — relevant to FU-7's scope.** The doc claims a mid-run LLM outage "fails the whole run
    loudly." `orchestrator.py:548-550` catches bare `Exception` per candidate and sets evidence to
    `None` — so a stage-3 **timeout or connection reset produces the same silent 0.4 zeroing as malformed
    JSON**. Given the 2026-07-20 incident, this fires in practice. This is more evidence for FU-7's
    fail-closed requirement, and FU-7 should close it in code.
  - **MAJOR-4** — "all of them are configurable without code changes" is false for ≥3 register items
    (unstated-duration full credit is hardcoded `stages.py:129`; batch-relative normalisation
    `stages.py:227-238`; consent granularity is a schema change).
  - **MAJOR-5 — the 50-candidate coarse cutoff is a harder exclusion than the registered top-15 cliff and
    is unregistered.** Worse, `stage1_coarse` (`orchestrator.py:264-280`) oversamples the **global** vector
    index 3× and only then filters to the job — so on a busy instance a résumé can be squeezed out of its
    own job's pool by similar résumés on unrelated requisitions. Non-merit exclusion, zero doc coverage.
  - **MAJOR-6 — "Repeatability · 0 rank change" is presented under the "you do not need to police this"
    chip, but `thresholds.toml:403-411` says the opposite in its own words:** no `seed` is passed to
    Ollama, greedy decode is not bit-stable, and a warm-Redis rerun **passes vacuously** (testing the
    cache, not the model). Pinning `seed` is still outstanding.
  - **MAJOR-7 — ordering controls overstated.** `min_score_gap = 1e-6` is "a float-noise epsilon, not a
    separation the fixtures have to earn" (`thresholds.toml:358`), and the three mutations that prove
    those pairs gate anything are **human review obligations, not gates** (`thresholds.toml:265-274`) —
    so the row marks a human process as machine-enforced.
  - **MINORs (8–15), ride the same revision:** a "met" requirement under `confidence ≥ 0.7` contributes
    **zero**; seniority's floor rescale means any title cosine ≤0.50 scores exactly 0 (adverse-impact
    relevant, unregistered); the register is miscounted ("eleven" vs nine `Ratify`); the "~40% random
    ranker" figure is stale (17-fixture corpus, not recomputed for 20); `education` returns **1.0 for
    everyone** when the JD omits `min_level`; ~8 further policy-laden defaults are unregistered
    (`match_family_weight`, `education_partial`, `evidence_met_confidence`, `motivation_min_confidence`,
    `match_non_matchable_families`, plus CRITICAL-1's three) — the register is closer to 23 than 15;
    "fully deterministic" at stage 2 contradicts the doc's own item 9; the 0.9 reverse-match cap holds
    only while `evidence_k > 0`.
- **Chore — config plumbing and fail-closed auth.** No `MATCH_*` tunable and none of the four `API_KEY_*`
  vars appear in `docker-compose.yml` or `compose.live-eval.yml`. Two consequences: the documented ranking
  knobs are unreachable in the running containers, and since auth is disabled iff all four keys are empty,
  **the shipped compose runs auth-disabled with every caller resolving to `admin`.** Fail-open is the
  *shipped* default, not merely a possible misconfiguration. Also raise the `LLM_TIMEOUT_S` default off
  120 (see incident below).

**The 2026-07-20 incident that motivated FU-7 — read this before touching the LLM or parse path.**
16 résumés sat at `uploaded` for ~18 hours. `gpt-oss:20b` on the calibrated peer generates at **~23.5
tok/s** (measured: 1338 completion tokens in 56.8s from inside the worker container); `parse_resume`
calls `chat_json(max_tokens=3072)`, so a full-length core extraction needs **~131s** against a
`LLM_TIMEOUT_S` of **120**. The failure was **deterministic, not transient** — short generations
succeeded, which disguised it as flaky infrastructure for two diagnostic rounds. Raising the timeout to
300 in `compose.live-eval.yml` cleared all 16 (real parses measured 150–205s). **But 10 of the 16 then
logged `parse_resume.skills_llm_failed` and were still marked `parsed`** — skills silently fell back to
the deterministic vocabulary scan. Root cause of the empty content: `gpt-oss:20b` is a reasoning model
that returns its chain in a separate `reasoning` field and can exhaust `max_tokens` before emitting any
`content`. `reasoning_effort: "low"` is a large latency lever (~7x on a toy prompt) but changes
extraction quality — **it must go through the `ranking-evals` gate, never be set unilaterally.**

**Diagnostic lesson worth keeping:** do not exonerate the LLM endpoint with a small curl. A 10-token
probe returns in ~4s and proves nothing. Measure `completion_tokens / elapsed` at realistic `max_tokens`
from **inside the worker container**, then compare `max_tokens / tok_s` against `LLM_TIMEOUT_S`.

The remaining backlog below is still **options, not a queued to-do list**:

- **Résumé lifecycle — candidate withdrawal + stale-résumé tracking (user request 2026-07-28, scoped in
  [ADR-026](docs/adr/026-resume-withdrawal-lifecycle.md)).** Two
  related staleness problems make an uploaded résumé's fate untraceable: **(a) parse-failure staleness** —
  a résumé stranded at `uploaded` when its parse times out (or the worker never runs), indistinguishable
  from one that was never enqueued; **(b) candidate withdrawal** — no way to mark a candidate as withdrawn,
  so a withdrawn résumé keeps appearing in newly-generated shortlists with no signal. Both leave "stale"
  rows the recruiter can't reason about.
  - **Part (a) is ALREADY SCOPED as FU-7 / ADR-021 decision 3** ("honest résumé parse status": claim
    `uploaded`→`parsing` on task start, transition `parsing`→`failed` when arq exhausts `max_tries`). The
    2026-07-19/20 incident (16 résumés stuck at `uploaded` for ~18h — see the incident note above) is
    exactly this defect. **Build it in FU-7, not as a second state machine** — don't duplicate. The
    `'parsing'`/`'failed'` enum values already exist in `core/src/models/ddl.py:57`; `'parsing'` is
    currently unreachable and `record_parse_failure` is never called on a timeout (ADR-021 §2/§3).
  - **Part (b) is NEW — nothing in the repo handles withdrawal.** `resume_status`
    (`core/src/models/ddl.py:57`) is `('uploaded','parsing','parsed','failed')` — no `withdrawn`. Scope to
    decide: a new terminal **`withdrawn`** status vs. a separate `withdrawn_at`/`withdrawal_reason` column
    kept distinct from a parse `failed` (a withdrawal is a lifecycle event, not a processing error, and
    conflating them loses that). Then: an API action + a Workflow-UI control to withdraw a candidate;
    **exclusion of withdrawn résumés from `shortlist_job`/`reverse_match_job`** (they filter on parse
    status/`description_parsed`, not lifecycle, so a withdrawn candidate would otherwise still rank); and an
    audit row (reuse FU-5's `audit_log`, ADR-019). **PIPEDA/FIPPA angle to settle in the ADR:** a
    withdrawal may be an explicit consent revocation, which is stronger than mere shortlist exclusion —
    decide whether withdrawn PII is purged or retained (the repo already tracks `consent_acknowledged`; this
    is its symmetric un-consent).
  - **Cross-cut for both halves:** surface a per-job résumé-status breakdown in the UI (ADR-021 decision 3
    already promises "candidate counts by status") so a recruiter sees stuck/failed/withdrawn résumés
    instead of a silently-shrinking pool. This is the "tractable" the request is really asking for.

- **Wire the reverse-match UI** — ✅ **CLOSED (FU-3 slice 5, PR #21, merge `e033d31`).** Shipped as the
  POST-only trigger + bounded poll + rows linking to the job. Listed here as an option long after it was
  delivered; retained per the repo's record-closure-forward convention rather than deleted.
- **The open `jd.education.fields` decision** (ADR-009 §7, restated through ADR-013) — `score_education`
  ignores `jd.education.fields` entirely, so JD field-relevance is decorative. Either extend the scorer to
  read `fields`, or drop `fields` from the JD contract. Still unresolved after 4c/4d/5/6/7/Workflow UI all
  touched no scoring code.
- **The deferred connectors feature** (Taleo/CSV-manifest upload) — explicitly cut in Phase 6 (ADR-012 §2),
  the user's own framing was "Taleo was a shortcut to get sample data … will add more connectors in the
  future." Upload today only accepts local multi-file or `.zip`, from the browser or the API directly.
- **No advisory lock on concurrent shortlist/reverse-match regenerate** (ADR-010 §1) — now live and
  reachable from the browser (the Workflow UI's Generate/Regenerate button calls
  `POST /jobs/{id}/shortlist` directly). Last-committer-wins today.
- **`reverse_match_job`'s `allowed_job_ids` filter** is still `description_parsed IS NOT NULL`, not
  `status = 'open'`, even though Phase 6 added the first code path that ever transitions `jobs.status`
  (ADR-012 §3 revisits, does not resolve).
- **CSV formula/injection** in `shortlist_csv`/`shortlist_evidence_csv` — accepted for v1 (ADR-011), a
  one-line fix (leading-character escaping) was noted but not applied.
- **The `redacted_filename` `os.path.splitext` LOW residual** — a pathological filename with no true
  extension can leak a lowercased name-derived suffix under blind review (ADR-011). Accepted for v1.
- **The live-eval harness's synthetic-only skill-name-scrub shortcut** — `run_evals_live.py` was built and
  verified only against the synthetic 4a/4c corpus; its embed-boundary PII handling should not be reused
  as-is on a path carrying real candidate PII without a fresh security review (security note, not a defect
  in the merged code).
- **At-rest cleartext PII posture** (ADR-007 §6/§7, ADR-010 §6) — `resumes.parsed`,
  `shortlist_entries`/`reverse_match_entries`'s evidence quotes, and structured experience/education/skills
  fields are all cleartext at rest in Postgres (protected by pgcrypto only on the four dedicated PII
  columns). Accepted for v1; revisit before any multi-tenant deploy.
- **Weak/empty `flask_secret_key`/`api_key` defaults** — env-overridable, but weak-by-default; harden
  before any non-local deployment (Workflow UI status, above).
- **Reverse-match ranking quality is entirely ungated** (ADR-013) — the `[reverse_match]` section of
  `core/tests/evals/thresholds.toml` is a commented-out placeholder, so no precision, evidence-verification
  or ordering bar applies to the résumé→jobs direction, while the forward direction is gated at 100%
  precision@5. Revisit before reverse match informs any decision.
- **Reverse-match scores are not comparable to forward-match scores** (ADR-009) — `rank_job_matches` omits
  the motivation term, so reverse `score_final` maxes at 0.9 under default weights while forward maxes at
  1.0. Nothing in the API, the export or the UI signals this. Must be documented wherever both numbers can
  reach the same reader.
- **Two ranking numbers are unreachable from settings** (ADR-009) — `_STRUCTURED_ONLY_WEIGHTS` and the
  stage-1 3x oversample factor are in-code literals, while all 26 `MatchWeights` values and both k values
  are env-configurable. Also: reverse match reuses `match_coarse_k`; there is no `match_reverse_coarse_k`.
  Minor, but an inconsistency in an otherwise fully-tunable engine.
- **The circuit breaker's half-open docstring contradicts its code** — `core/src/pipeline/llm/client.py`
  claims a failing half-open trial "will re-open immediately", but the failure counter was reset on
  cooldown expiry, so it takes another full `breaker_threshold` (10) consecutive failures to trip again.
  Either the doc or the behaviour is wrong; decide which when FU-7 touches this file.
- **ADR-016's R3 and R4 remain open** — R3 (no reveal-audit viewer: the `auditor` role added by FU-4 still
  has nothing to view, so retrieval is a manual SQL query) and R4 (unredacted `source_context` on reveal).
  FU-5's `audit_log` makes R3 actionable but does not itself ship a viewer.

**Re-running the live eval, if a future session needs to:**

```bash
docker compose -f docker-compose.yml -f compose.live-eval.yml up -d postgres neo4j redis api worker
docker compose -f docker-compose.yml -f compose.live-eval.yml exec -T worker \
  python tests/evals/run_evals_live.py
```

Two files this depends on are git-ignored and not in the repo: `.env` and `compose.live-eval.yml`. The
calibrated models (`nomic-embed-text` + `gpt-oss:20b`) live on a **remote** Ollama — the local metal host
does not have them pulled. A fresh clone must recreate both `.env` and the compose override before this
will run; see ADR-013 §5 / §"Live end-to-end eval" for the harness's design and verified results.

## Historical: original Phase 3 plan (for reference)

Port the ingest/parse pipeline: `parsing/{extract,chunk}` (PyMuPDF/python-docx), the LLM client + Redis embed cache, `parse_resume`/`parse_job`, cover-letter parse, and **PII encryption on parse** (`pii.py`, pgcrypto). hris source paths are in **Appendix A** of the plan. These schemas are the parse targets: `JDExtracted` (job parse), `ResumeParsed`/`ResumeCore`/`ResumeSkill*` (resume parse), `CoverLetterParsed` (cover-letter parse). Then Phases 4–7 per the plan table.

**Phases 1 and 2 are done** (see Current state). Carried-forward criteria to apply in Phase 3:
1. **Path-traversal rejection — DONE in Phase 1.** `BlobStore._resolve` rejects `..`, absolute paths, null-byte keys, and symlink escapes before any IO. Nothing further needed.
2. **STRICT PII-key GUC read — a Phase 3 acceptance criterion.** It concerns `pii.py` (the PII read path). Wire `settings.pii_key` into `app.pii_key` with `current_setting('app.pii_key')` **without** `missing_ok=true` — a missing_ok read of an unset key yields NULL → NULL ciphertext → silent data loss. Fail loud.
3. **Per-field `max_length` on LLM string fields — a Phase 3 acceptance criterion** (Phase 2 security low). Add belt-and-braces caps on the free-text LLM-output fields at the ingest boundary.

Carried further: the **Phase 5 redaction-boundary contract** (Phase 2 security, ADR-006 §4) — `ResumeOut`/`ResumeListItem` can serialize decrypted PII with `blinded=True`, so Phase 5 redaction MUST mask `candidate.*`/`candidate_name`/`cover_letter_text` before DTO construction (the schema can't enforce it). And the **Phase 6 `JobOut.blind_review` fail-open** note (Phase 2 security low) — the DTO defaults `blind_review` to `False`, so a route must set it explicitly from the row.

hris source paths for every phase are in **Appendix A** of the plan; architecture rationale: Phase 0 in **ADR-004**, Phase 1 in **ADR-005**, Phase 2 in **ADR-006**.

### Phase 3 starting map (verified)

Two read-only audits confirmed the following against `C:\repos\hris` — orientation for a cold start, not a spec:

- **Dependency gap.** `core/requirements.txt` is missing `PyMuPDF` (import `fitz`), `python-docx` (import `docx`), and `striprtf` (lazy-imported for RTF) — add all three. Already present and sufficient: `redis` (ships `redis.asyncio`), `httpx`, `openai`, `tenacity`.
- **LLM client decision.** hris's `LLMClient` (`packages/pipeline/src/pipeline/llm/client.py`) hand-rolls the OpenAI-compatible REST calls over `httpx` (chat / JSON-mode / embeddings) with its own retry + circuit breaker — it does **not** use the `openai` SDK. `cache.py` (`CachedEmbedder`) is a Redis read-through cache over `LLMClient.embed` via `redis.asyncio`. Phase 3 decision to make: port the httpx client verbatim (recommended — matches source) vs. rewrite on the `openai` SDK already in requirements.
- **Both carried-forward PII criteria are already satisfied in the hris source — port verbatim, don't re-invent:**
  - `_build_summary_text` (`apps/worker/src/worker/resume_tasks.py`) excludes PII structurally — it only reads `parsed.summary`/`skills`/`experience`/`education`, never `parsed.candidate` (the `CandidateInfo` holding name/email/phone). Preserve this "never touch `.candidate`" discipline when building embedding/summary text.
  - `pii.py` (`apps/api/src/api/services/pii.py`) already uses the strict GUC read — `current_setting('app.pii_key')` single-arg, no `missing_ok` — so an unset key raises rather than silently yielding NULL. This is exactly the Phase 3 acceptance criterion; port the SQL verbatim (`set_pii_key` = `SELECT set_config('app.pii_key', $1, true)`; `encrypt`/`decrypt` via `pgp_sym_encrypt/decrypt(..., current_setting('app.pii_key'))`).
- **Source + target schemas confirmed ready.** hris side: `parsing/{extract,chunk}.py`, `llm/{client,cache}.py`, `pipeline/config.py` (scope down its many `match_*`/`jd_*` knobs), `worker/resume_tasks.py` (`parse_resume`, `project_to_graph`, `_build_summary_text`, `_parse_cover_letter`), `worker/tasks.py` (`parse_job`), `services/pii.py`. Target side: `core/src/schemas/` already has `JDExtracted`, `ResumeParsed`/`ResumeCore`/`ResumeSkill`/`ResumeSkillDetails`, `CoverLetterParsed`, `ResumeChunk`.
- Prompt templates live at `packages/prompts/src/prompts/templates/` (Appendix A corrected); the four pairs needed (`.system.j2` + `.user.j2`) all exist: `resume_core_v1`, `resume_skills_v2`, `shortlist_evidence_v1`, `cover_letter_v1`.

## Trigger prompt (paste into a new session)

```
Resume the recruiter-assistant build. Working dir C:\repos\recruiter-assistant
(origin github.com/humanaxiom/recruiter-assistant). Read HANDOFF.md and
docs/EXTRACTION_PLAN.md first — they are the source of truth for state,
decisions, environment quirks, and the hris source-file map (Appendix A).
Architecture rationale: Phase 0 in docs/adr/004-*.md, Phase 1 in docs/adr/005-*.md,
Phase 2 in docs/adr/006-*.md, Phase 3 in docs/adr/007-*.md, the Phase 4b PII
rearchitecture in docs/adr/008-skill-graph-pii-by-construction.md, the Phase 4c
matching-engine port in docs/adr/009-matching-engine-port.md, the Phase 4d
shortlist/reverse-match write path in docs/adr/010-shortlist-reverse-match-write-path.md,
the Phase 5 display-redaction read/export boundary in
docs/adr/011-display-redaction-read-export-boundary.md, the Phase 6 API-routes
auth/upload scope in docs/adr/012-api-routes-auth-upload-scope.md, and the
Phase 7 read-only Flask viewer in docs/adr/013-phase7-evals-viewer.md.

We are porting the resume-ranking feature from C:\repos\hris onto this template
(template-first, filesystem storage instead of MinIO, keep Neo4j, v1 includes
cover-letter/reverse-match/minimal viewer/blind-default). Phases 0, 1, 2, and 3 are
ALL complete and merged to main, CI green: Phase 0 (seed & infra) PR #1, Phase 1
(storage — filesystem BlobStore) PR #2, Phase 2 (schemas) PR #3, Phase 3 (ingest +
parse) PR #6 (merge 49196d7). Phase 4 (Ranking engine) was split into 4 gated
sub-phases and ALL FOUR are now MERGED to main, CI green: 4a (evals corpus)
MERGED (PR #8, merge 875eac2), falsifiability hardening MERGED via PR #10
(merge 464a479); 4b (graph projection) MERGED via PR #11 (merge 68fe821);
4c (matching engine) MERGED via PR #12 (merge fd12d1a); 4d (shortlist +
reverse-match write path) MERGED via PR #13 (merge 5945320) — all three
merge-blocking gates were green (security PASS, reviewer APPROVE, ranking-evals
PASS) AND CI was fully green before each merge.

**4d closed ADR-009's carried "Requirement 1"** — matching_context_from_settings
(src/pipeline/matching/orchestrator.py) is the single call site that builds
MatchingContext from Settings (family_weight, non_matchable_families,
llm_concurrency, evidence_max_tokens, model_gen/emb, git_sha); shortlist_job/
reverse_match_job (src/worker/matching_tasks.py) call it with get_settings()
and pass weights=weights_from_settings(get_settings()) — never DEFAULT_WEIGHTS.
It also shipped src/services/shortlist_service.py's persist_shortlist/
persist_reverse_match (DELETE-first per-run idempotency, mirror-image handling
of score_structured/score_evidence/evidence dictated by the two tables'
different DDL shapes). Full detail: docs/activity/phase-4d-shortlist-writepath.md
and docs/adr/010-shortlist-reverse-match-write-path.md.

**Phase 5 (persist + anonymize + export) is MERGED to main via PR #14
(merge 6deade3), CI green.** ADR-006 §4's redaction-boundary contract is now
ENFORCED IN CODE, not just recorded: every blind read path
(shortlist_service.list_for_job/get_one/export_rows,
resume_service.list_for_job/get_one(reveal=...)) redacts BEFORE building the
DTO, proven by black-box byte-scan tests plus reviewer mutation testing on
every redaction call site. This is display-only redaction, NOT at-rest
protection — ADR-007 §6/§7 and ADR-010 §6's cleartext-at-rest postures are
UNCHANGED. The ScoreBreakdown fold-read guard (ADR-011 §2) pops
score_structured/score_evidence back out of score_breakdown before
model_validate — required to read ANY 4d-written shortlist row. Two
post-first-green fixes landed before merge: a HIGH cover-letter-chunks PII leak
(blind ResumeOut.parsed.cover_letter_chunks[].text still carried raw letterhead
PII) and the original_filename de-anonymization vector (redacted_filename()
now returns generic resume<ext> under blind at three surfaces; real filename
under reveal/non-blind) — both mutation-proven, both merge-blocking gates
re-verified. Full detail: docs/activity/phase-5-persist-anonymize-export.md and
docs/adr/011-display-redaction-read-export-boundary.md.

**Phase 6 (API routes) is COMPLETE and MERGED to main via PR #15** (squash
merge e910669, off main @ 6deade3, tip 837de9e), commit chain:
red 209bff7 -> green bc9a3d6 (initial routes, resumed mid-build after a
session-limit interruption) -> red 1f2b161 -> green 344f6bf (SEC-1/SEC-2/SEC-4
security hardening + exact fastapi/starlette/python-multipart pins) -> red
c75f4a7 -> green 837de9e (non-ASCII X-API-Key 401 generalization + upload
file-count-ordering regression pin). **All three merge-blocking gates were
green (reviewer APPROVE, security PASS, ranking-evals PASS) AND CI's
gates-all went fully green before merge (2026-07-17).** Note: CI's gates-all
runs the offline run_evals.py stand-in inside the gated unit suite — it never
calls a live Ollama endpoint, by design (see the Phase 7 correction below).
Full detail: docs/activity/phase-6-api-routes.md and
docs/adr/012-api-routes-auth-upload-scope.md.

**Phase 6 shipped src/api/deps.py (new — require_api_key/resolve_actor/
get_arq/log_auth_mode), src/api/routes/{jobs,resumes,shortlist}.py (new — 11
routes), src/services/zip_upload.py (new — expand_zip_entries/ZipRejected),
src/services/jd_import_service.py (new — extract_jd_text).** The configurable
auth switch is ONE settings flag (settings.api_key): empty disables auth (loud
startup WARNING), non-empty enables fail-closed 401 with constant-time
UTF-8-byte comparison. Upload accepts local multi-file + zip ONLY — the
Taleo/CSV-manifest connector is explicitly CUT and deferred to a future
connectors feature; zip expansion mirrors the Phase-3 DOCX-bomb defense
(streams real decompressed bytes, never trusts ZipInfo.file_size).
PATCH /jobs/{id}/status is the only status-mutating route (forward-only, 409
on an invalid transition) — the first code path in the whole repo that
transitions jobs.status. Reverse-match (POST /resumes/{id}/match-jobs, GET
/resumes/{id}/match-results) is a subresource of routes/resumes.py with
EXPLICITLY NO redaction on the read (the caller owns the résumé they matched).
ADR-006 §4's JobOut.blind_review fail-open note is now CLOSED —
_row_to_jobout sets it explicitly from the row on every path, reviewer
mutation-proved both directions. A latent pool.py bug was also fixed:
PoolConnectionProxy[Record] isn't subscriptable at runtime; under
`from __future__ import annotations` + FastAPI's eval_str signature
introspection it crashed at route registration the first time any route
actually used Db (never true before Phase 6) — fixed with a
TYPE_CHECKING-gated alias.

**Phase 6 final state (HEAD 837de9e):** 2156 unit tests @ 91.68% coverage;
123 integration tests vs real Postgres+Neo4j+Redis, incl. 12 new Phase-6 ASGI
integration tests. Reviewer APPROVE (6 mutation obligations fired), security
PASS (SEC-1/SEC-2 closed on re-audit), ranking-evals PASS (scoring
byte-unchanged).

**Phase 7 (a minimal read-only Flask viewer over the Phase 6 API) is MERGED to
main via PR #16** (squash merge 1039e5c, 2026-07-17), built on branch
feat/phase-7-evals-viewer (off main @ e910669, pre-merge tip 92ca4ae, now
deleted local + remote), commit chain: docs 55ee0a0 (interim HANDOFF/plan
stamp) -> red 942e8f5 -> green f28c22e (core/frontend/api_client.py new,
core/frontend/app.py extended with 7 new routes + templates, Makefile/ci.yml
gate-scope widening) -> refactor/fix 92ca4ae (two post-review security
findings closed: the résumé-detail template rewritten so it is structurally
incapable of rendering candidate.name/email/phone/location rather than merely
flag-gated on the backend's blinded flag, and the error page made fully
static so no backend-supplied text reaches the browser). **All three
merge-blocking gates were green (reviewer APPROVE, security PASS,
ranking-evals PASS), CI's gates-all went fully green, and PR #16 was
squash-merged.** Full detail:
docs/activity/phase-7-evals-viewer.md and docs/adr/013-phase7-evals-viewer.md.

**Phase 7 shipped a blind-only viewer, by construction, not by default.**
Shortlist list/detail reads (api_client.list_shortlist/get_shortlist_entry)
take no reveal parameter at all; the résumé route hardcodes reveal=False and
ignores any browser-supplied ?reveal=. Reveal/reveal-export remains an
audited, non-viewer backend surface (ADR-011/012) — the viewer can never
de-anonymize from the browser. **Phase 7 also fixed a real, previously
un-gated hole:** core/frontend/ (api_client.py, app.py, the new tests) was
invisible to every quality gate before this phase — it is a sibling of
core/src/, and every gate command named "src tests" explicitly. Makefile and
.github/workflows/ci.yml now run ruff/black/mypy/coverage over frontend too,
pinned by a new meta-test (test_gates_cover_frontend.py).

**Phase 7 shipped NO new evals fixtures as part of the viewer build — that line
item was already done.** The plan's Phase 7 row said "ranking-quality fixtures
(precision@k, evidence-verification rate)"; those shipped in 4a (corpus) + 4c
(live orchestrator wiring, run_evals.py::main() already running inside the
gated unit suite). A live end-to-end eval of the corpus through the real
pipeline (post-parse boundary -> project_to_graph -> shortlist_job -> persisted
rows) had never run (4c only proved it against the orchestrator directly) and
was originally deferred this session — needing a reachable host Ollama +
docker compose up, which CI cannot provide by design. **Reversed later the
same day (2026-07-17): the human un-deferred it and made it a prerequisite for
merging PR #16. It is now BUILT + RUN + PASS, reproduced identically twice**
(core/tests/evals/run_evals_live.py, new, 812 lines +
core/tests/unit/test_evals_live_metrics.py, new, 16 tests). Full detail:
ADR-013 §5 and docs/activity/phase-7-evals-viewer.md's "Live end-to-end eval
(post-review addition)" section.

**Documentation correction made this session, apply it wherever you see the
old phrasing:** prior HANDOFF/plan text said CI runs "a live run_evals.py
re-measurement against Ollama" for Phases 4d/5/6. That is inaccurate — CI's
gates-all runs the OFFLINE run_evals.py stand-in inside the gated unit suite;
it never calls Ollama (.github/workflows/ci.yml's own comment: "CI never
calls a model endpoint; inference is host-only by design"). CI itself still
never calls Ollama — the live measurement against a real Ollama endpoint
(above) runs as a separate script outside CI, via
`docker compose ... exec -T api python tests/evals/run_evals_live.py`
against a stack pointed at an Ollama with nomic-embed-text + gpt-oss:20b, not
inside the gated unit suite.

**Phase 7 final state (tip 92ca4ae, pre-live-eval):** 2229 unit tests @ 91.67%
coverage (frontend now format/type/coverage-gated for the first time).
Reviewer APPROVE, security PASS (both findings closed), ranking-evals PASS
(scoring code byte-unchanged; offline corpus 352 tests green;
run_evals.py::main() exits 0). **Post-review (2026-07-17): the 16 new
test_evals_live_metrics.py tests bring the offline suite to 2245 unit tests @
91.67% coverage**, ruff/black/mypy still clean.

**PR #16 was gated in CI (gates-all fully green) and squash-merged to main as
1039e5c on 2026-07-17** — the live end-to-end eval (above) was the merge
prerequisite and had already passed, reproduced twice.
docs/EXTRACTION_PLAN.md's phase table ends at Phase 7, and it is now fully
merged: **the extraction plan's locked v1 scope (the four decisions at the top
of the plan) is complete. All seven phases (0-7) are merged to main, CI
green. There is NO Phase 8.** Do not invent a new numbered phase on your own
initiative; any further work (the still-open jd.education.fields decision,
the accepted residuals catalogued across ADR-009 through ADR-013, the
deferred connectors feature, the no-advisory-lock gap, at-rest cleartext PII
posture before multi-tenant — see HANDOFF.md's "Next session" section for the
full list) is a follow-up chore that needs a human to scope it, not an
automatic Phase 8.

Subagent model tiering is in effect (docs/SUBAGENT_MODEL_POLICY.md): the three
merge-blocking gates (reviewer/security/ranking-evals) run on opus; producers
(data-pipeline/planner/tester/coder) default to sonnet; docs on haiku. Defaults
live in .claude/agents/*.md frontmatter.

**Open human decision, carried forward across 4c/4d/5/6/7, still UNRESOLVED:**
score_education ignores jd.education.fields (ADR-009 §7, restated ADR-010 §5,
ADR-011, ADR-013) — either extend the scorer or drop fields from the JD
contract. Neither 4c, 4d, 5, 6, nor 7 touched stages.py's scoring code, so
this remains exactly as open as it was after 4c. Do not resolve this silently.
Also carried: reverse_match_job's allowed_job_ids filter is still
description_parsed IS NOT NULL, not status='open', even though Phase 6 added a
status route (ADR-012 §3 revisits but does not resolve this); no advisory
lock on concurrent shortlist/reverse-match runs (ADR-010 §1, still open, the
viewer is read-only so Phase 7 didn't touch this either).

Note: no local Python — verify gates in the python:3.11-slim Docker container per
HANDOFF.md. Phase 7's PR #16 is merged; v1 is complete. Post-v1, the Workflow
UI and FU-1/2/3/4 are merged, and so are PR #24 (the FU-5/6/7 plan) and PR #25
(ADR-022). The ADR-022/ADR-023 hardening branch (fix/adr022-evidence-verifier-
hardening, HEAD 85d995c) is COMPLETE and open as PR #27 with CI 10/10 green —
it needs the human to run `gh pr merge 27 --squash --delete-branch`. Then
start FU-5.

CURRENT STATE AS OF 2026-07-22 — read HANDOFF.md's "ADR-022 status" and
"ADR-023 status" sections before doing anything:
- `main` is at 1f526f6. Everything below is merged with CI green, EXCEPT the
  ADR-023 hardening branch, which is open as PR #27 — CI fully green (10/10),
  MERGEABLE/CLEAN, awaiting the human's merge command. FETCH AND CHECK FIRST:
  `git fetch && gh pr view 27` — if it is already merged, skip to FU-5. This
  file has lagged origin by a whole sub-phase before; do not trust it blind.
- FU-4 (RBAC) MERGED via PR #23 (961caab). The org billing block was fixed that
  morning and CI ran green on all five gates — its first real execution on that
  branch. Diagnostic note if it recurs: a billing-refused job reports
  conclusion=failure with steps=0; a job that genuinely ran and failed has
  steps>0.
- PR #24 (abb5d67) MERGED — the FU-5/6/7 plan (ADR-019/020/021), 9 gaps filed,
  the HR explainer. THE PLANNING WORK IS COMMITTED; earlier handoff text
  claiming it is uncommitted is stale and has been corrected twice.
- PR #25 (6db83b6) MERGED — ADR-022, an evidence-integrity fix to
  verify_evidence: an uncited quote is now scrubbed like a fabricated one, in
  both the requirement and cover-letter arms. Found by fact-checking the HR
  explainer, not by reading code. Ranking is byte-identical; it is a
  display/integrity fix, not a scoring one.
- THE ADR-022 HARDENING BRANCH IS DONE (ADR-023, HEAD 85d995c, PR #27, CI
  10/10 green, awaiting merge): the security HIGH finding PR #25 deliberately
  left open — rapidfuzz partial_ratio returning 1.000 when a quote contains
  the whole cited chunk verbatim PLUS arbitrary appended fabrication — is
  narrowed (unbounded append -> bounded ~26%-of-chunk replacement, NOT fully
  closed), the NUL-byte transaction killer is closed at the schema boundary,
  evidence fields are capped at ingest, and the missing minimum-quote-length
  floor is closed at 16 chars. Deferred: Fix A (span-quoting the eval-corpus
  stand-in), the 26% residual, the ellipsis-quote false-negative class. Full
  detail in ADR-023 and HANDOFF's "ADR-023 status" section.
- TWO PIECES OF WRITTEN GUIDANCE WERE WRONG AND ARE CORRECTED IN PLACE — if
  you have older text in context, distrust it. (a) ADR-022 follow-up #2 named
  verify_evidence as the fix site for the NUL bug; it never rewrites
  requirement / overall_summary / overall_motivation, so a spec-literal fix
  leaves three fields killing the transaction. (b) ADR-022 recommended a
  length-ratio guard at k~1.2; measurement rejects ANY usable k, because the
  bypass returns 1.000 at +1 appended character (~1.008 ratio), not only at
  the +1120 the ADR records.
- THE MOST TRANSFERABLE FINDING, and the reason a green ranking-evals run is
  not by itself evidence: the corpus could falsify NONE of the six guards
  ADR-023 added — all seven guard mutations survived on both input orders,
  because the stand-in extractor quotes each chunk IN FULL, so every surfaced
  quote is byte-identical to its cited chunk and no guard ever binds. A
  byte-identical ranking was the ONLY possible outcome; it would have been
  identical for a materially broken implementation. Third recurrence of this
  pattern (Phase 4a's corpus, ADR-022's own harness loop, now this). Fixed
  for these guards by additive probes (20/20 kills), but the ROOT CAUSE
  STANDS: the corpus is blind to any future quote-shape guard by
  construction. Do Fix A before the next change to _fuzz_ratio.
- CLAUDE.md's "Before implementing anything new" step 1 used to say to curl
  localhost:8000/memory/similar. That route was a golden-template demo route
  DELETED IN PHASE 0 (test_api.py's DEMO_ROUTES asserts its absence), so a
  mandated step was silently unfollowable for this project's entire life.
  Fixed, and .claude/commands/memory-query.md was REMOVED rather than
  corrected — it also documented BaseAgent._memory_context(), a retrieval it
  claimed ran inside every subagent, and core/src/agents/ was deleted in
  Phase 0 too. Do not re-add either without building the feature first.
- THE IMMEDIATE NEXT WORK ITEM IS FU-5 (CAS identity + attributable audit,
  ADR-019), THEN FU-6 (per-job assignment + row-level scoping, ADR-020), THEN
  FU-7 (LLM failover + fail-closed ranking, ADR-021), in that order; each
  depends on the previous.
- The HR explainer (docs/process/) carries a DRAFT/NOT-FOR-CIRCULATION banner
  and must not go to HR. Fixing CRITICAL-2 in code did not fix the prose, and
  ADR-023 narrowing (not closing) the superset bypass does not fix it either
  — the banner's stated removal trigger now points at the bounded-replacement
  residual, not at "the hardening branch lands."
- Two open decisions needing a human: ADR-020's auditor scoping (global-read-
  but-every-read-logged, flagged for ratification, FU-6 needs it), and the
  long-carried jd.education.fields question (ADR-009 §7).
- Operational: `compose.live-eval.yml` (gitignored) now sets LLM_TIMEOUT_S=300
  for the worker. The 120s default is BELOW the real parse time on the
  calibrated peer (~23.5 tok/s vs max_tokens=3072 => ~131s), so a fresh clone
  will see resumes hang at status 'uploaded' forever with no error. This is
  deterministic, not flaky. See the incident writeup in HANDOFF.md.

See the "Phase 3 starting map (verified)" subsection above (historical) and
docs/adr/007 for how the ingest/parse layer Phase 4 builds on was ported.
```

### user-admin-roles status — built + integrated to local main, not yet pushed to origin

**2026-07-28 update.** `feat/user-admin-roles` is integrated to local `main` (fast-forward, at `45eba6d`,
full `red:`→`green:`→`docs:` history preserved). `./scripts/verify.sh all` was **re-verified GREEN on the
local tip**: **3815 unit tests @ 92.6% coverage, 375 integration tests** against real Postgres/Neo4j.
**Reviewer APPROVE ×2** (backend slices 5+6 and the frontend slice 7 each reviewed separately) + **security
PASS** — the accepted concurrent-double-demote residual described below is recorded in ADR-025. Applied to
the running stack: the nullable-role DDL is live in Postgres and `asalah` is the sole active admin. It is
**not yet on `origin/main`** — see the "⚠️ LOCAL-INTEGRATION STATE (2026-07-28)" banner near the top of
this file for the four local-only features (this is one of them) and the reconciliation path.

Built on branch `feat/user-admin-roles` (HEAD `63ae662`, integrated to local `main` at `45eba6d`), off
`feat/fu5-cas-identity`'s completed FU-5 work, across eight TDD slices (DDL nullable-role reversal →
provisioning default reversal → `require_role_assigned` fail-closed gate → the Flask `pending_access.html`
gate → `GET /users` → `PATCH /users/{id}/role` (+ `Role` enum move to `schemas.auth` + last-admin lockout)
→ the Flask admin UI → a security-hardening fix round). **All offline gates green** and **both
merge-blocking reviews recorded APPROVE** (a first-round APPROVE followed by a second confirming APPROVE
after the security round's fixes were folded in); **security PASS**. Full decision record:
[ADR-025](docs/adr/025-user-admin-roles.md); the superseded ADR-019 §10a default (`role='recruiter'` on
first login) is annotated in place, not rewritten — see ADR-019's inline supersession note and its §10c
residuals list.

**The headline change: no-role-by-default first login.** ADR-019 §10a's "every non-default-admin CAS
login lands as `recruiter`" is reversed — `users.role` is now nullable with no DB default
(`core/src/models/ddl.py`, two idempotent `ALTER ... DROP DEFAULT` / `DROP NOT NULL` statements, same
already-migrated-volume discipline as every other schema change in this repo), and
`user_service.provision_or_get` writes `role = NULL` for anyone except the configured
`default_admin_cas_username` (`asalah`, unchanged). A brand-new user now gets **no access at all** until
an admin grants a role, closing the fail-open gap ADR-019 §9 recorded and deferred.

**The fail-closed gate that makes the reversal actually bite.** `require_role_assigned`
(`core/src/api/deps.py`) is wired on all 5 business routers (`jobs`, `resumes`, `shortlist`,
`job_assignees`, `audit` — `core/src/api/main.py`) and 403s a real, resolved session with `role is None`
before any route body runs — closing the hole where a no-role user could otherwise ride the Flask
viewer's one shared `recruiter` API key to full access, since `require_role`/`resolve_role` only ever
judge the *key*, never the session. The Flask frontend mirrors this one hop up: `_cas_auth_gate`
intercepts an authenticated no-role status and renders `pending_access.html` (200) instead of the
requested page.

**The admin surface.** `GET /users` (list) and `PATCH /users/{id}/role` (assign) —
`core/src/api/routes/users.py` — are gated by their own `_require_admin_session`, keyed off the **CAS
session's** `role == "admin"`, deliberately NOT the API-key role the Flask viewer's shared `recruiter`
key would otherwise present for every browser user. The `role_changed` audit row is written in the SAME
transaction as the role `UPDATE` (atomic — a failed audit write rolls back the role change), and demoting
the last active admin is refused with a 409 `ConflictError` before any write (the last-admin lockout
guard). A Flask admin UI (`/admin/users`, `core/frontend/templates/admin_users.html`, an admin-only
"Users" nav link) gives an admin a role-assignment surface without SQL.

**Accepted residual, carried forward, not fixed (both reviewer and security flagged it
independently).** The last-admin lockout guard's `count_active_admins()` read and the role `UPDATE` run
under asyncpg's default READ COMMITTED with no row lock — two concurrent demote requests against two
*different* admin rows, from exactly 2 active admins, can each observe `count == 2`, each pass the guard,
and both commit, leaving 0 admins. Deliberately deferred (an offline, effectively-single-admin tool
today); ADR-025's Accepted residuals section has the exact remediation (`SELECT ... FOR UPDATE` or
SERIALIZABLE) for when multi-admin becomes real.

**Exact next step (updated 2026-07-28 — integration to local `main` is DONE, this is what's left):**
this feature is already integrated to local `main` and applied to the running stack (see the 2026-07-28
update above); what remains is getting it onto `origin/main`. The billing block that used to gate CI is
CLEARED, but local `main` has diverged from `origin/main` on SHAs (not content — see the current-state
banner), so a plain `git push` is rejected. Follow the reconciliation path in the "⚠️ LOCAL-INTEGRATION
STATE (2026-07-28)" banner near the top of this file: either rebase the four local-only commits (this one
included) onto `origin/main` and push, or open a fresh PR for this feature off `origin/main` and let CI
gate it. Either way the push/PR-merge is a human-driven command (`gh pr merge` remains classifier-blocked
for the agent) — do not attempt it unattended.
