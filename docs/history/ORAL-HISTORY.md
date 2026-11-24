# Oral history of recruiter-assistant

**Written 2026-11-24, on branch `chore/handover-2026-11`, for a team that has never seen this project.**

This is a narrative, not a changelog. It says how the project got here and why, what was decided and what
was turned down, what went wrong and what each failure taught. It is also written for the product owner's
software-engineering practice, because this repository is the seed for how a human runs a Claude agent fleet.

How to read the citations: short SHAs are commits on `main` (most are squash merges, so one SHA is often a
whole branch), `#NN` is a GitHub PR number, `ADR-NNN` is `docs/adr/NNN-*.md`, paths are relative to the repo
root. Dates are commit dates (the `gh` merge dates in the appendix are UTC and sometimes a day later). Where a
claim could not be verified from the repository it is marked **unverified** or left out. Roles stand in for
people: "the product owner" (PO), "the sponsor" (the DTO/CIO side that asked for features), "HR", "the four
pilot users".

---

## 1. What this is

recruiter-assistant ranks résumés against a job description for SFU recruiters, offline, and shows its work.
A recruiter creates a job (or imports one from Taleo), uploads résumés, and gets a shortlist. Each candidate
carries a score, per-requirement evidence quotes that are fuzzy-checked against the source text so the model
cannot fabricate them, a disclosed breakdown, and, on jobs where blind review is on, an anonymized card whose
identity is reachable only through an audited reveal. Recruiters, hiring managers and auditors see different
things (RBAC, CAS single sign-on, per-job assignment).

The stack is Python 3.11, FastAPI, a Flask+HTMX frontend, Postgres, Neo4j (graph and vector recall), Redis/arq,
and a 20B reasoning model (`gpt-oss:20b`) served by Ollama on a GPU host reached over the tailnet
(`CLAUDE.md` Stack section, ADR-003, ADR-045). Inference never leaves the tailnet. That sentence is the most
load-bearing constraint in the project, and one documented exception to the broader no-egress rule exists
(Taleo, ADR-046, section 3.4 below).

It was built almost entirely by Claude Code sessions coordinated by one human. The first commit is
2026-07-10 (`1891210`); this history covers 272 commits on `main` to 2026-11-24 and 115 PRs (appendix B).

---

## 2. Timeline in eras

### Era 0: the golden template (2026-07-10)

The repo began as a clone of an "agent harness" template: `CLAUDE.md`, `.claude/agents/*.md` (planner,
tester, coder, reviewer, security, docs), a Makefile gate suite, and a demo FastAPI app. The first commits are
`1891210` "import agent-harness v2", `f24a605` "make harness green out-of-the-box", `167072b` the
`USING_THIS_TEMPLATE` guide (now `docs/archive/USING_THIS_TEMPLATE-template-era.md`).

Two things from this era still shape the project:

- **The agent pipeline.** Red (tester) then green (coder) then three merge-blocking gates (reviewer,
  security, and a domain gate) then docs. The repo added two domain agents of its own, `data-pipeline` and
  `ranking-evals` (`.claude/agents/`).
- **The template's flaw, found much later.** Its Definition of Done judged *changes*, never *products*.
  `docs/process/HARNESS_FIELD_NOTES.md` (written 2026-08-24) makes this its first finding: the harness "can
  tell a *change* is done nine different ways, and cannot tell a *product* is done at all."

The template also left residue. Its own guide kept claiming to supersede the README and described inference
as running on `host.docker.internal`, which stopped being true. It was archived on 2026-08-27 with an
explanation that three documents had drifted apart.

### Era 1: the extraction, Phases 0 to 7 (2026-07-10 to 2026-07-17)

The product was **extracted from an existing Next.js HRIS** (`C:\repos\hris`, an unrelated repo): take its
résumé-ranking feature and rebuild it as a small, local-first app. `docs/EXTRACTION_PLAN.md` is the plan of
record, and four decisions are "locked" at the top of it:

| # | Decision | Alternative turned down |
|---|---|---|
| 1 | Template-first port: golden template as chassis, port clean pipeline modules near-verbatim | rewrite from scratch |
| 2 | Local filesystem `BlobStore` | MinIO. Its community edition was archived 2026-04-25 with no CVE fixes (ADR-005) |
| 3 | Keep Neo4j for vector recall and skill-graph scoring | drop the graph, use Postgres only |
| 4 | v1 includes cover letter, reverse-match, a minimal viewer and blind review ON by default | cut cover letters / reverse match |

What was cut on purpose: the HRIS review workflow, the JD-Harmonizer, the Taleo connector, the Next.js app,
and CAS. Three of those cuts were later reversed (CAS in July as FU-5, the decision workflow in November,
Taleo in September), which is a theme: **a cut is a deferral with a paper trail**.

The phases and their PRs, all merged in eight days:

| Phase | PR | What |
|---|---|---|
| 0 Seed & infra | #1 | asyncpg DDL, Neo4j bootstrap, settings, rebrand (ADR-004) |
| 1 Storage | #2 | filesystem `BlobStore`, path safety, 0600/0700 perms (ADR-005) |
| 2 Schemas | #3 | jobs/résumés/matching, review types cut (ADR-006) |
| 3 Ingest + parse | #6 | extract/chunk, LLM client, embed cache, PII encryption (ADR-007) |
| 4a Evals corpus | #8, #10 | labelled fixtures + `thresholds.toml` |
| 4b Graph projection | #11 | outbox drainer, PII-safe skill graph (ADR-008) |
| 4c Matching engine | #12 | four-stage scoring, evidence verifier (ADR-009) |
| 4d Shortlist write path | #13 | persist shortlist + reverse match (ADR-010) |
| 5 Persist/anonymize/export | #14 | display redaction boundary, blind export (ADR-011) |
| 6 API | #15 | routes, configurable auth, multi-file and zip upload (ADR-012) |
| 7 Viewer | #16 | read-only blind-only Flask viewer (ADR-013) |

`c4ea6bf` (#17, 2026-07-17) declares "v1 extraction plan complete". **That is the day the project was
feature-complete.** Hold that date; section 2.3 is about what happened next.

Two observations from the log, because they are visible in it:

1. The ranking algorithm was ported "verbatim" (`EXTRACTION_PLAN.md`: 0.40 skill, 0.25 experience, 0.10
   education, 0.15 seniority, 0.10 vector for the structured score, then 0.6 structured + 0.3 evidence
   completeness + 0.1 motivation). The port was fast. The *hardening* of the port was not.
2. The gold-plating habit was already present inside v1. Phase 4a, the eval corpus, took nine hardening rounds
   in two days (`98196b8` "4a-hard" through `583427f` "4a-hard-9", PR #10), each a red/green pair about whether
   the corpus "could fail a bad engine". Phase 4b took five security rounds (`6f5f617` through `398a8ba`)
   before the architectural answer arrived: **ADR-008, PII eliminated by construction** (`2a08322`), meaning
   skill names are hashed to canonical keys so no filter has to guess whether a string is a person's name.
   The heuristic filters kept being patched for four rounds first. The lesson is in the ADR's own status line:
   the construction "supersedes the heuristic PII filter".

### Era 2: "v1 complete" and the gold-plating problem (2026-07-18 to 2026-08-23)

No one declared the product finished, and no one used it. What followed was a run of eight numbered follow-ups
and an authorization and ranking-honesty programme. The pace was real and each step was individually
defensible.

**Follow-ups (FU-n).** #18 the Flask+HTMX workflow UI (ADR-014); #19 FU-2 evidence chunk expansion (ADR-015);
#20 FU-1 the audited candidate reveal (ADR-016); #21 FU-3 bulk ingest and cover-letter pairing (ADR-017); #23
FU-4 RBAC with keyed roles (ADR-018); #29 FU-5 CAS identity, user records and an attributable audit log
(ADR-019); #31 FU-6 per-job assignment and row-level scoping (ADR-020); #37 FU-8 résumé withdrawal,
"exclude-and-retain" (ADR-026); #44, #52, #55 FU-7 in three slices (ADR-027, 029, 030).

**An incident that created FU-7.** On 2026-07-19/20 sixteen résumés sat at status `uploaded` for about 18 hours
because LLM calls timed out. ADR-021 recorded two distinct failure modes (timeout versus a reasoning model
returning empty `content`) and six gaps, including that the `parsing` enum value was unreachable in code, so
`uploaded` meant "never enqueued", "running now" and "dead" at once. Ten of the sixteen were quietly marked
`parsed` with no skills and later ranked with zeroed evidence. **The fix principle that came out of it, fail
closed and say so, became the spine of the next month** (ADR-029, 037, 039, 040, 041).

**The authorization rebuild.** The auth story is where this era's best and worst moments sit:

- ADR-033 (#68, 2026-08-09) put a session-role check on every write route and declared the matter closed.
- A retrospective reviewer-plus-security pass found it had not. ADR-034 (#72, 2026-08-13, `299b529`): no
  `API_KEY_*` existed anywhere, the `.env` file could not reach the container, so `auth_enabled` was false,
  every request resolved to `Role.ADMIN`, and both new gates passed on "no user". Proven against the live
  stack: no cookie and no key returned real job data, the audit log, and un-redacted candidate names.
  **Two gates, ANDed, both vacuous.** The PR notes the human had authorised merging #68 without the
  merge-blocking gates, and that the retrospective pass found what they would have.
- It was followed by CSRF on every browser write route (ADR-035, #74), an auditor's access-record viewer
  (ADR-036, #75), and in the same week the three stage-3/stage-1 fail-closed fixes (ADR-037, 039, 040) and
  disclosure of fabricated-looking sub-scores (ADR-041, #85). Fourteen commits landed on 2026-08-13 alone.
- #95 (D2) closed unscoped keyed reads: every read now needs a real principal.

**The defect taxonomy ("A7").** A pattern was named: *an invariant asserted in prose with nothing enforcing
it* (memory note `gates-only-catch-enforced-invariants`). The technique that found instances was mutation
probing: after going green, mutate each invariant the branch introduces, run the suite, and see what fails to
complain. It found real things (a `seniority_measured = seniority != 0.0` mutant survived 4,457 unit tests).
But the count rose to **21 instances** (ROADMAP A7, `docs/archive/ROADMAP-2026-08-24-part-a-and-history.md`)
and the taxonomy turned into a scoreboard. The reset document says it bluntly: "naming the pattern made finding
more instances feel like progress."

**The quiet useful work.** The skill vocabulary was derived from a real JD corpus and merged, raising
recognition of real SFU qualification statements from 15.6% to 54.8% (ADR-042, #88). A first guardrail
appeared on 2026-08-14 (#87 "regroup — unblock A2, and add the stopping rules this repo lacked"). Two
decisions that had sat as bare "blocked" lines for five sessions (D1 withdrawal-reason access, D2 keyed
reads) were answered in a day once options and a recommended default were written down, both as the default
(`docs/OPEN_DECISIONS.md`). They are the origin of Economy rule 3.

**The three tools (2026-08-22, #97).** The pipeline "worked end to end" for the first time, and three
verification tools were built that week and each found something on its first run:

| Tool | Proves | First-run finding |
|---|---|---|
| `scripts/doctor.sh` | the data in a live deployment | 20 stranded résumés; an auth-disabled stack serving `/audit` |
| `scripts/smoke.sh` | the browser to Flask to API seam | ranking silently dropped an unprojected candidate |
| `scripts/model-check.sh` | the model dependency (ADR-045) | `resume_skills_v2` needed 8192 tokens, not 4096 |

**PII exposure (2026-08-22).** A `git add -A` swept `fixtures/` (117 files, 99 MiB of real résumés and JDs)
into a branch that was pushed to a *public* repository. Contained the same session (branch deleted, history
rewritten with `filter-branch`, the ignore rule actually committed), but **deleting a ref did not stop GitHub
serving the blobs**; it was tested by fetching by SHA. Only a GitHub Support purge removes them, and
`HANDOFF.md` section 6 still lists it as open. See incident 4.5.

### Era 3: the reset (2026-08-24)

`ec2f2d2` (#100) "Reset: the product has been finished for 38 days — stop building, get a user." Measured on
2026-08-23 in `docs/archive/RESET-2026-08-24.md`:

| Measure | Value |
|---|---|
| ADRs for Phases 0-7 (the whole product) | 12 |
| ADRs after it was finished | 32 |
| Source lines / test lines | 18,983 / 97,517 (5.1x) |
| Documentation lines | about 16,700 (`HANDOFF.md` alone was 3,251) |
| Commits since scope complete, docs vs feat | 32 vs 14 |
| Recruiters who had used it | 0 |

Its diagnosis was careful: **not a quality problem, a stopping problem.** "Fixing a defect creates a new
invariant, a new invariant needs a guard, and a guard is a new surface to probe." Without a user the only
feedback is introspection, and "two days of real usage produced better signal than the four weeks before it":
the smoke suite's first run found silent drops, and the first human to open a job page found 20 dead jobs
that the suite was content with.

The repo had known. On 2026-08-14 `docs/ROADMAP.md` recorded the ratios and `CLAUDE.md` gained its Economy
section. Ten days later every ratio was worse. So the reset set a **destination** rather than another rule:

> A real recruiter, signed in as themselves, ranks a real requisition against real applicants and says
> whether the shortlist is sensible.

What stopped: ADRs became an exceptional act (all three of live alternatives, expensive to reverse, and
unrecoverable from code and commit message), PRs are batched, `HANDOFF.md` is capped at eight *items* (not
lines, because a line cap is satisfied by compression), A7 hunting ceased, and docs-only commits skip the
full gate. What did not change: the gates themselves. (`CLAUDE.md` Economy 0 to 0b.)

### Era 4: demo, approval, pilot (2026-08-24 to 2026-09-15)

The product was demoed to the CIO and HR; both approved. A dedicated box went live with four users. The HR
demo script (`docs/archive/HR_DEMO_SCRIPT-2026-08-22.md`) framed it as decision support, "not automated
decision", a framing that the later decision-record feature (ADR-014 amendment) respects in code. On
2026-08-27 `CLAUDE.md` and ROADMAP were updated: the "get a user" destination was met and replaced with the
standing objective *"the four people on the pilot box can do their real hiring work in it, and what they hit
gets fixed before anything they haven't hit gets built."* User-sourced work outranks self-sourced work.

A dated, newest-first channel for user reports was created (`docs/pilot-feedback.md`) and earned its keep
immediately: its first real entry produced the roster feature.

### Era 5: sponsor requirements, Taleo, and the public deployment (2026-09-02 to 2026-09-26)

On 2026-09-02 the sponsor sent a requirements set and answered four open questions the same day. Three of
four went against the plan's recommended default (`docs/SPONSOR_REQUIREMENTS_PLAN.md` section 0):

| Question | Answer | Effect |
|---|---|---|
| Where does work authorization come from? | Recruiter-entered; "assign the 10% cover-letter mark to this instead" | Declared screening fact, not inferred (ADR-047); the 10% weight moved to the manager's prompt |
| Which host serves postings? | "Taleo import was implemented in the hris repo. Bring it here." | Port the ADR'd implementation behind an egress carve-out (ADR-046) |
| Is there an internal SMTP relay? | Yes, an on-network relay | notification option B (S7, not started) |
| Auto-reject, hidden or visible? | "Last but visible... all other metrics are invalidated" | Ineligible candidates keep their card but show `—` rank and `n/a` sub-scores |

Delivered as #101, #102, #103 and #104 (merged 2026-09-09): work authorization, the manager's additional
requirements as a second extraction pass (`manager_prompt_v1`, 10% weight), the Taleo importer (default off),
document links, department and campus extraction, blind review made opt-in per job, and a candidate roster CSV
(`core/src/services/candidate_roster_service.py`) built against the sponsor's real 315-row export, where every
parsed résumé matched its roster row. Other milestones:

- **#103 (2026-09-03)** also closed a defect that had been in the repo since the start: `FLASK_SECRET_KEY` was
  committed as a literal in both compose files, so anyone who could reach the frontend could forge a session
  for any role. Fix in three layers (generate it, take it from env, refuse to boot on a published default).
- **2026-09-09 hardware pause.** Shown that a 315-candidate requisition needs about 11 hours of parsing (about
  29 résumés/hour, within about 1.4x of the arithmetic floor for one GPU), the PO paused feature work "until
  the data center hardware is ready" (`docs/ROADMAP.md` item 0). The arithmetic favoured the tool on human
  hours, since 11 machine-hours cost about zero attention and a recruiter spends about 10.5 hours screening 315
  résumés; the damage was iteration latency.
- **#105 (2026-09-15):** refuse to rank a job whose JD yielded no requirements, and say so on screen.
- **sfuai.ca (2026-09-15).** The box went onto the public internet at `https://sfuai.ca:8000` with CAS on,
  behind an nginx in a separate, non-git compose project (`docs/deploy/sfuai-ca.md`; #106). The router forwards
  only TCP 8000-8009, hence the port. The same day a Werkzeug-debugger exposure through a forged `Host` header
  was found and closed (`--no-debugger` plus a default-deny nginx catch-all), and the Flask secret and four API
  keys were rotated. Both facts come from the archived handoff (`docs/archive/HANDOFF-2026-08-24-to-11-21.md`
  section 2).
- **#107, #108 (2026-09-26):** a manager's guide, an end-to-end run of it against an *isolated* stack
  (`scripts/e2e.sh`, `scripts/stress.sh`, compose project `recruiter-stress`), and the five gaps that run
  found. **#109:** an unsplit Taleo export (all applicants' pages in one PDF) is refused with guidance and a
  cover letter is never parsed as a résumé. This came from a sponsor-reported requirement on 2026-09-18.

**A gap.** The log has no commits between 2026-09-26 and 2026-11-20. The reason is **unverified** from the
repository (the ROADMAP pause on feature work, dated 2026-09-09, is the only recorded context).

### Era 6: November features (2026-11-20 to 2026-11-24)

| PR | Date | What |
|---|---|---|
| #111 | 11-20 | `sfuai.ca` and `localhost` both log in; app containers survive a reboot |
| #110 | 11-21 | Re-parse a failed or degraded résumé from its own page; `verify.sh` works from a git worktree |
| #112 | 11-21 | In-app Taleo split (`taleo_split_service.py`, with a mandatory confirmation screen) and an anonymized résumé export |
| #113 | 11-23 | Interview-question generator for shortlist entries (`interview_question_service.py`) |
| #114 | 11-24 | Human decision record: accept, reject or hold with a rationale (`decision_service.py`) |
| #115 | 11-24 | The worker loads `.env`, so `TALEO_ENABLED` reaches the Taleo import |

Details worth keeping:

- **#113 took three review rounds**: `6c971e5` round 1 (blind flip, LLM retry, stalled-pending state, scrub
  gaps), `e24cef6` round 2 (claim-scoped settles, honest stall window, common-word names, dash phones), and
  `870a431` round 3 (title-case weak names, reversed order, more dashes, superseded skip). Each round is a
  red/green pair, and the tests found in round 1 required correcting two "provably wrong" earlier tests
  (`bf0ca50`). The recurring theme was the identifier scrubber: partial quotes on non-blind jobs could leak a
  name or phone.
- **#114 had one review round, and a major in it** (`333be7f` red, `ff14a2e` green): multi-line rationales
  were rejected because browsers submit `<textarea>` newlines as CRLF and the validator saw a control
  character. The round also added an advisory lock for concurrent repeats and zero-width/bidi rejection. The
  design decision is recorded as an **amendment to ADR-014 (2026-11-24), not a new ADR**: the "no
  approve/reject" scope exclusion is reversed for one narrow surface, the decision is append-only, never
  feeds rank or score, and its audit `details` never hold the rationale. This follows the Economy rule that
  amends rather than adds.
- **The pilot box was put on `main`** by a tree-identical commit (`4716a36`, then `a8f5e0d`): the live
  checkout's branch had diverged from `main`, and `main`'s tree was what CI had gated on #114. See incident 4.9.
- **A clean slate, 2026-11-24.** At the PO's request, and after the PO chose the scope ("everything except
  accounts"), the coordinator wiped the pilot box: 12 Postgres tables truncated, the audit log included; the
  Neo4j graph and the Redis queue cleared; the stored résumé and cover-letter files deleted. The two user
  accounts and their roles were kept, and so was the PO's source material in `data/`. No backup was kept,
  because it was real candidate data. A wipe changes state, not code, so git has no record of it; this
  paragraph is the record. After that, the PO believed résumé upload had been deleted. The
  form only renders on an *open* job, and every job was an unparsed Taleo draft. That led directly to
  `docs/plans/2026-11-job-page-redesign/` and the fleet playbook (section 5.5). The repo's own record of that
  incident is `docs/plans/2026-11-job-page-redesign/README.md` section 1 and `docs/practice/fleet-project-playbook.md`
  section 7.
- The PO is handing the project to a Teams account; this document is part of that handover.

---

## 3. The decisions that shaped it

### 3.1 Offline inference, without exception (ADR-003, `CLAUDE.md`)

Every model call goes to an OpenAI-compatible endpoint on the tailnet. ADR-003 originally put Ollama on the
app host's own metal (`host.docker.internal:11434`) and rejected "Ollama in Docker" and a native Ollama client.
In practice the model lives on a shared GPU peer (`aria-gb10`), a correction made in #63 (2026-08-05). The
rule that came out of it: no candidate data, résumé, prompt or shortlist ever crosses a boundary. Cloud APIs
were never an option. A consequence the project paid for repeatedly: **the model is slow, shared and
reasoning-heavy**, so token budgets and timeouts became first-class (incident 4.3).

### 3.2 Neo4j and Postgres, each for what it is good at (ADR-002, ADR-004)

Postgres is the transactional ledger (tables created at startup; there is no migration framework, which later
mattered, see 4.2). Neo4j holds the skill graph and the 768-dimension cosine vector indexes (`nomic-embed-text`).
Redis exists only as the arq broker. ADR-002 is "partially superseded" by ADR-004, and embeddings deliberately
exclude name, email and phone.

### 3.3 Blind review and the audited reveal (ADR-011, 013, 016)

Blind review was a day-one product value. The route there: the v1 viewer was blind *by construction* (the
template had no code path capable of rendering `candidate.*`, pinned by a source-scan test, ADR-013); the
shortlist and export read paths redact at the boundary (ADR-011); and the PO asked on 2026-07-18 for "the
hris model": blind on by default, identity only through an explicit, POST-only, audited reveal (ADR-016).
Two leaks were found and closed during Phase 5 alone: cover-letter chunks (`8b1597e`) and the original
filename (`c1e4e04`), the second a reminder that identity hides in unlikely columns.

Then the sponsor reversed the default on 2026-09-09: *"reverse the blind review to be off by default, keep the
on switch button."* ADR-016 got an amendment rather than a successor: `jobs.blind_review` now defaults
`FALSE`, and the audited reveal "remains the only path to identity on a job that has blind review turned on".
Two follow-on lessons are in `HANDOFF.md`: reversing a default reaches no existing row (hence a backfill
script) and exposes every path the old default hid. The anonymized résumé export (#112) rebuilds a copy with
identity stripped, rather than redacting the original.

### 3.4 CAS and identity (ADR-018, ADR-019, ADR-025, ADR-033, ADR-034)

Roles started as four API keys (ADR-018). The weakness (ADR-019's Context): the actor in audit rows was an
optional, unverified header or `"api"`, so no audit row could name a person. ADR-019 added a `users` table,
SFU CAS login and a general `audit_log`. It reversed an explicit v1 cut ("CAS authentication tables were
deliberately excluded"). The design changed mid-build: ADR-019 section 10 (2026-07-22) supersedes the first
architecture (Flask runs the CAS dance, HMAC-signed assertion header to the API) with a port of the proven
implementation from the `hris` repo (FastAPI owns the CAS ticket, session in a Postgres `sessions` table
behind an opaque httpOnly cookie, Flask forwards the cookie). **The lesson: when a working implementation
exists in a sibling repo, port it; do not redesign.** ADR-025 then made first-login no-role-by-default and
added admin screens. `users.role` as `NOT NULL` with no default was the case that produced the verify.sh
rule (see 4.1).

Two ADRs read as a pair: ADR-033's claim of "closed" and ADR-034's "it failed open" (section 2.2). Two rules
survive from that pair: **a security fix that is not exercised against the real configuration is a claim, not a
fix,** and **an auth boundary must default closed**; the startup now refuses to boot an auth config that would
fail open (`validate_startup_auth_config`).

### 3.5 Evidence-backed ranking and "fail closed, say so" (ADR-009, 021, 022, 023, 029, 037, 039-041)

The four-stage engine (vector recall, structured score, LLM evidence with an anti-fabrication verifier, then
combine) was ported verbatim (`EXTRACTION_PLAN.md`). Everything after is about *honesty of the output*:
an uncited quote is scrubbed like a fabricated one (ADR-022); unfinished evidence fails closed rather than
rendering a zero; stage 1 recall is job-scoped, not database-wide (ADR-039); the "evidence cliff" past
`evidence_k=15` (where candidates lose 40% of the composite for reasons unrelated to merit) is *disclosed, not
removed* (ADR-040); and sub-scores that "read as measurements when nothing was measured" are marked
(ADR-041). The principle is stated in the repo's own words: a ranking tool for hiring must never present a
guess as a measurement. The honest remainder is in `docs/ROADMAP.md` section 5 (the cliff remains).

### 3.6 Screening facts are declared, never inferred (ADR-047)

Work authorization and SFU-internal status (APSA/CUPE) both move a person on a ranked list, and a résumé
contains text from which a model could guess either at no extra cost. **ADR-047 forbids it:** both are
entered by a human or read from the sponsor's roster, never from résumé text, a name, an email domain or model
output. The internal-status uplift is a bounded, disclosed `+0.05` inside `score_final`. The memorable
real-data case: one candidate was both `not_eligible` and CUPE-internal, "two mechanisms in tension on a real
person; the band wins, the uplift cannot resurrect them, both facts disclosed". No fixture would have
produced that.

### 3.7 The Taleo egress carve-out (ADR-046)

The sponsor asked to import job postings by link. The repo's one hard rule is no egress. ADR-046 does not hide
that: a daily worker fetch of `tre.tbe.taleo.net`, and *only that host*, is an outbound call. It argues the
direction of the data (nothing of ours crosses the boundary; the worker reads a public page SFU publishes)
and bounds the blast radius: default off (`TALEO_ENABLED=false`), its own host allowlist inside `TaleoClient`,
and unmet obligations before production use (an enumerated firewall rule, counsel and privacy-officer
sign-off, a named owner). `CLAUDE.md` now says **a second carve-out needs an ADR, a default-off flag and a
line in `CLAUDE.md`, "never a quiet import".** The ADR "ports `hris` ADR-0012": again, an argument already
had and accepted in a sibling repo was reused rather than re-argued.

### 3.8 The model is a dependency with an acceptance test (ADR-045)

Four reasoning-model incidents in a row each got a per-call-site fix (token budgets of 1536, 1024, 2048,
3072, 128 in five places, each measured once). ADR-045 states the real problem: *"model behaviour is encoded
as constants, each measured once, and nothing re-measures them when the model changes."* The decision:
`scripts/model-check.sh` builds the real prompts from `fixtures/` at the worker's real concurrency, finds the
smallest budget that yields valid JSON, writes `docs/model-profiles/<model>.json`, and `doctor.sh` fails when
the configured model has no profile or when `LLM_TIMEOUT_S` is below the measured latency. The timeout is
*derived* from measurement, not declared. The ADR also records an honest open gap: `model-check.sh` probes
Ollama's native schema-constrained path, while the app runs the OpenAI-compat path.

### 3.9 Decision record: human decisions alongside the ranking, never inside it (ADR-014 amendment)

The PO wanted recruiters to record accept/reject/hold with a rationale. The live alternative was a full
review workflow (stages, approvals, queues), which v1 had deliberately cut. The choice made: an append-only
`decision_records` table, a badge on the card, rank snapshotted at decision time, **no feedback into rank or
score**, auditors read-only, hiring managers only on their assigned jobs, the rationale never in audit
`details`. It was recorded as an ADR-014 amendment because the Economy bar says to amend rather than add.

---

## 4. Incidents, and what each one taught

Format: what happened, how it was found, what it taught. The ones that changed the repo's rules are marked
(rule).

### 4.1 A green suite is not a working product (rule)

The recurring finding, in `CLAUDE.md` as "the hand-over rule" (standing order 2026-09-09). Instances:

- **`users.role NOT NULL` with no `DEFAULT`** passed 2,764 unit tests and would have failed the first real
  `INSERT`, because ADR-019 requires first login to omit `role`. Only an integration run against real
  Postgres could see it. This is the origin of `scripts/verify.sh` (#28, `8c29ff2`) and the path-to-required-mode
  table.
- **`{{ job.updated_at.strftime(...) }}`** returned HTTP 500 for the whole jobs list (2026-09-03) after a green
  gate and green CI, because the frontend is a BFF where every timestamp is a *string*. New tests
  string-matched the template source; older ones used hand-written fixture dicts. Fix: render
  `DTO.model_dump(mode="json")` in tests, use the `| day` filter (memory note `frontend-sees-json-not-dtos`).
- **A fix that was correct, gated green, and had never applied to a single row** thirteen days later (A7 (20)):
  the write fired only on new data, and every row predated it. No fixture is ever old, so tests cannot see
  this class. That is why `doctor.sh` exists.
- **The roster upload 503'd on first real use** (`pgp_sym_decrypt` ran with no transaction, so no PII key). 6,067
  unit tests passed because they mock `pii_service` wholesale, and `reconcile_candidate_roster` had no
  integration test. "A mock agrees with any transaction state you ask it about."
- **The sponsor's first click on the work-authorization control returned 403** (one-shot CSRF tokens were not
  minted by the re-render). Fixed the same day; reported with "the declaration is a critical eval parameter".
- **A mutant flipping `if resolved_wa != "unknown":` to `if True:` survived all 6,067 unit tests**: a blank CSV
  cell would overwrite a recruiter's audited decision. Pinned afterwards.

What it taught: the unit suite measures what someone wrote a test for. Smoke, doctor and a by-hand drive are
separate instruments for separate classes of defect. `CLAUDE.md`: "A green suite is evidence about the code.
It is not evidence about the product."

### 4.2 State defects that no test can see: `doctor.sh`

Four state-versus-code defects had shipped by the time `doctor.sh` was written (ROADMAP A7, `CLAUDE.md`):
20 résumés stranded since July at a status nothing wrote to, an auth-disabled stack serving the audit log,
20 dead JDs behind a `parsing...` spinner for 24 hours, and a reconcile cron that was "completely inert" because
it read `ctx["arq"]` while the worker sets `ctx["redis"]` (found by making its own guard loud). The design
rule: *"could not check" is never a clean bill of health*; the tool exits non-zero on a failed invariant **or an
unreachable datastore**. Tables created at startup with no migrations also bite: an idempotent `ALTER` and a
backfill script were needed whenever a default changed. `pg.jobs_stuck` was recorded as still missing from
`doctor.sh` (ROADMAP section 1); that is detection, not display, so the same blind spot remains.

### 4.3 Model budgets must be measured at real concurrency (rule, ADR-045)

The measurement history, from `docs/model-profiles/gpt-oss-20b.json` and memory notes:

1. 2026-07-19/20: `LLM_TIMEOUT_S=120` was below the peer's real parse time (about 23.5 tok/s, a 3,072-token
   ceiling needs about 131 s). Failures were deterministic, but short probes succeeded, which looked like
   flaky infrastructure. A 10-token curl probe "falsely exonerates the peer".
2. 2026-08-21: raising one number to 4096 turned "returns nothing" into "exceeds `LLM_TIMEOUT_S`" and the
   circuit breaker opened (every call timed out at 366 s). The budget lived in a worker file, the timeout in
   `.env`, "nobody owned the relationship between them".
3. 2026-09-09: the shortlist produced nothing for hours. `match_evidence_max_tokens` was 2048; the model spent it
   on reasoning and returned empty content; ADR-029 fails closed, so one starved candidate withheld the whole
   run. **Two probes, one prompt at a time, both passed**; at the real concurrency of 4, two of four failed
   and not the largest inputs. The wrong diagnosis was reported twice in public before fanning out.
4. The token floor is a property of the *prompt*, not the model: the `skills_graph` tiebreaker at 128 tokens
   gave identical answers to 8192 (measured 2026-08-22), so "fix small budgets on sight" is wrong too.
5. Never diagnose the model on a contended peer. A foreign model landing on the shared GPU twice destroyed a
   measurement mid-flight and once produced a confidently wrong retraction of a correct fix.

Taught: probes at concurrency 1 are a different measurement, not a smaller one. Every budget, timeout and
concurrency number in this repo was measured against `gpt-oss:20b`; swapping the model makes all of them wrong
at once with no signal, hence `model-check.sh` before any swap and a committed profile per model.

### 4.4 Config that never reached the process that needed it (rule)

Three separate instances, one shape:

- **ADR-034 (2026-08-13):** no `API_KEY_*` and no `.env` channel into the container, so the auth boundary was
  off in the shipped configuration.
- **2026-08-22:** `.env` never reached containers because compose named `CAS_ENABLED` only in a comment and
  `quickstart.ps1` never wrote it. Also `LLM_TIMEOUT_S=900` lived in a shell override that would not survive
  `docker compose up`.
- **2026-11-24 (#115):** `TALEO_ENABLED` reached the API and the frontend but not the worker, because only
  those two services had `env_file: [.env]`. The scheduled Taleo import therefore never ran with the flag on.
  The red commit `ac32930` is titled "every app service loads .env — the worker missed TALEO_ENABLED".

Taught (the playbook's words): *a config flag is only on if the process that reads it sees it.* Check the flag
inside the process that acts on it.

### 4.5 PII hygiene is the project's characteristic risk

- **2026-08-22:** `git add -A` pushed 99 MiB of real résumés to a public repo (above). Process lesson recorded
  in the handoff: the guidance (commit with explicit pathspecs) already existed in a memory note and was not
  followed. The `.gitignore` edit that the previous commit claimed had been made sat unstaged in no commit:
  "this repo's signature defect, an invariant in prose with nothing enforcing it, landing on the remediation
  for a disclosure". The `fixtures/` harnesses now *hard-fail* when the directory is missing.
- **2026-09-09:** real candidate details were committed to `docs/pilot-feedback.md` and three other files
  *while documenting PII hygiene*. Caught by the security gate before push, history rewritten. The only
  scanning method that worked was extracting every name and email token from the real roster and scanning
  every file and commit message; three earlier passes from remembered patterns each missed something. The
  standing warning at the top of `pilot-feedback.md` explains how writing about data makes quoting a real
  example feel like precision.
- **Phase 3 and 4b:** PII scrubbing on embeddings and skill names went through repeated adversarial rounds
  (intra-token breaks, format-divergent phones, email local parts) until ADR-008 made the problem structurally
  impossible. The interview-question work (#113) fought the same fight on partial quotes. Where a heuristic
  scrubber needs endless patching, look for the by-construction design.
- Still open: the GitHub Support purge, and whether the repos should be public at all (`HANDOFF.md` section 6).
  Both `humanaxiom/` and `sfu-aria/` remote repositories were public as of the recorded notes.

### 4.6 Process bloat measured, and the reset (rule)

Section 2.3 above. The sharpest finding is in `HARNESS_FIELD_NOTES.md`: three of the four root causes were in
the template, not the project (a change-level DoD, a retrospective that can only add rules, a stakeholder demo
that exists in prose and nowhere in enforcement). The sub-lesson: "A rule that says 'do less of this' loses to
a process that generates work faster than the rule can veto it. A destination does not."

### 4.7 Hardware as the real ceiling

The 2026-09-09 arithmetic (section 2.5): one parse is two or three sequential LLM calls; `max_jobs=4` puts up
to twelve concurrent requests on one GPU; "Generate shortlist" queues behind every parse in a FIFO queue, so a
request can return 200 and then sit for hours while the page says "several minutes". It reproduced the exact
complaint of the day from an unrelated cause. Taught: do not raise `max_jobs`; surface real queue depth; the
step change is more or faster GPUs, a smaller model, or fewer calls per résumé.

### 4.8 One working tree, two sessions, many branches

- **The compose stack bind-mounts `./core`** into api, worker and frontend with `--reload`/`--debug`. A
  `git checkout` therefore changes what the running box executes within seconds. On 2026-09-15 a branch switch
  during a `smoke.sh` run caused a `ScoreBreakdown extra_forbidden` 500 and a 24-minute failed run, and a
  reviewer blamed the wrong branch (memory note `compose-bind-mounts-working-tree`). Rule: each work branch
  lives in its own `git worktree`; never switch branches while smoke, doctor, a hand-drive or a user is on the
  stack. `verify.sh` had to be taught to run from a worktree (#110/#112; `c6e6532`, `66f3f98`), because a
  worktree's `.git` is a file pointing to a host path that does not exist in the container, so the branch-name
  gate died before any test ran. "Every 'gated from the worktree' claim before today went through something
  narrower."
- **Two Claude sessions drive this repo at once** (memory note `concurrent-sessions-on-this-repo`): on
  2026-08-17 two sessions resumed the same branch within two minutes and one committed, pushed, opened and
  merged #89 before the other acted. Git metadata cannot tell which session acted. Rule: re-read git and PR
  state in the same call that commits, pushes or merges.
- **CI was payment-blocked** for a stretch in late July: GitHub Actions on the `humanaxiom` org was
  billing-blocked, so four features were squash-integrated into local `main` and reconciled with origin later
  (`ccb5ffe`, `5cab283`, `6903691`). This is why #32 to #35 show no GitHub merge date in the appendix.
  (The org move itself is recorded in `6903691`; the commercial detail is unverified.)

### 4.9 Putting the live box on `main` (2026-11-24)

The pilot box's checkout had drifted to a feature branch. The fix was `4716a36` and `a8f5e0d`: "takes main's
tree verbatim, the tree CI gated on PR #114", with the argument written in the message that every live-only
change was already in main or superseded. This is a reasonable template for any deploy that is not an image
(because the stack serves the working tree): the pilot box should be on a commit that CI actually gated.

### 4.10 Review rounds as signal, and the CRLF major

The three #113 rounds and the one #114 major (CRLF in `<textarea>`) show what reviewers catch that authors
miss. The CRLF defect is a classic seam bug: the validator was unit-tested with `\n`, the browser sends
`\r\n`. A related tooling trap recorded in the playbook: a by-hand drive "accepted" a zero-width character
because Git Bash had converted it to `?`, so the drive tested the shell and not the product. Before calling
a surprising result a product bug, confirm the input arrived intact.

### 4.11 Smaller lessons that cost something

- **A fix that is declared, validated and applied by nothing:** `manager_prompt = 0.10` passed its
  sums-to-1.0 validator but neither combine site multiplied it in, so every `score_final` was uniformly 10%
  low. No gate could see it because uniform deflation reorders nobody. Now pinned by
  `test_top_blend_is_fully_applied.py` ("a perfect candidate scores exactly 1.0").
- **A historical stamp read back with an uncaught validator:** adding a weight with a non-zero default would
  have made every existing `pipeline_meta.weights` fail its sum check and 500 every shortlist page. A
  `mode="before"` shim and a regression test exist; do not remove either.
- **Memos that reason about a field should check it is ever populated:** the D1 memo priced "reveal of a
  withheld withdrawal reason", but the withdraw form had never collected a reason. All five live withdrawals
  had NULL details. Found by running the product.
- **One-shot CSRF tokens have a budget** (64 per session, FIFO-evicted), and a shortlist can exceed it.
- **Mis-scoped blockers:** A2 sat blocked on a competency-scoring decision while the vocabulary work it was
  thought to gate was additive and strictly better than the status quo (`d73a0a0`, #89). Hence Economy
  rule 3: check what a blocker actually gates, and write a memo with a recommended default.
- **Cover letters, Taleo and the real bundle:** the splitter's LLM segmentation was intermittent on a real
  export (20 applicants with one page assigned to nobody, or 18 with two people merged). Page accounting
  caught the first, nothing caught the second until invariants were added.

---

## 5. How the human-fleet working relationship evolved

### 5.1 Roles and tiering

From day one: the main session coordinates and subagents produce. The PO's standing instruction on
2026-07-10 was "always use subagents" for substantive work (memory note `always-use-subagents`). On
2026-07-11 the PO asked that simpler tasks run on cheaper models "without compromising quality", which became
`docs/SUBAGENT_MODEL_POLICY.md` (#4): **cheap producers, strong verifiers.** Opus for the three merge-blocking
gates (reviewer, security, ranking-evals; "never downgrade"), Sonnet for producers (tester, coder, planner,
data-pipeline), Haiku for docs and mechanical edits, with per-call overrides *up* to Opus for the ranking
algorithm, the evidence verifier, PII crypto and graph scoring. The policy says it should be tuned by observed
gate-pass rates, not guessed once. Commit trailers show the coordinator model changed over the project's life
(Opus 4.8, Opus 5, Fable 5.1, Opus 5.5 all appear in `Co-Authored-By` lines), and the process was not
re-architected per model.

### 5.2 The evidence contract

Born from a thin report. The coordinator's own prompt said "report back with the diff summary", and a diff
summary is what came back. The lesson recorded in `docs/process/HARNESS_LESSONS.md` section 3: *check your
prompt before blaming the agent.* The fix is written into every producer's agent definition (the exact
command, the last 15 lines of real output with pass/fail counts, one line on what changed from red to green)
and into the coordinator's rule: **a subagent's claim of green is not evidence of green; re-run the gate
yourself.** It generalizes beyond subagents: the field notes record the identical failure driving a background
task whose exit code was masked by a trailing `echo`.

### 5.3 One verification path, and path-to-depth

The host has no usable Python, so every agent improvised a `docker run`, each narrower than the real gate
(`mypy src` for `mypy src frontend`, `black --check frontend` only, unit-only on a schema change).
`scripts/verify.sh` runs the real Makefile targets in a container so that "narrowing becomes impossible
(there is nothing left to narrow)". A table in `CLAUDE.md` maps diffs to required depth: anything touching
`models/`, `api/`, `services/`, `worker/` or graph code needs `verify.sh all`. Later, three more instruments
were added for the three things unit tests cannot see (4.2).

### 5.4 Consent points (standing orders)

Each of these exists because of an incident, and the playbook (`docs/practice/fleet-project-playbook.md`
section 4) lists them as the points where the human is always in the loop:

- **Push, merge, deploy to a box with users, anything destructive, turning on egress.** Corrected on
  2026-08-07: `gh pr merge` *works*; the constraint is consent, not capability. When the PO says merge, merge.
- **No cloud artifacts / local Markdown only** (memory note `no-cloud-artifacts`), because this is an HR
  product with a PII disclosure in its history. The playbook says the same: "A visual goes beside it as a
  local file. Nothing is published." (This document is no exception.)
- **CAS stays on** (2026-08-02): the PO reacted sharply when a convenience script's default dropped CAS ("you
  can't just drop entire features ... without asking"). A tooling default is a product decision. Never turn
  CAS off on the public box.
- **Unique host ports (29xxx)**, never stock 5432/8000/5000, because many other apps' containers collide on
  the host.
- **Wipes are scoped by asking** (the playbook: "ask for the scope as a question with options, never assume
  it"). The 2026-09-09 pilot wipe kept `users` and `audit_log`; the archived handoff records the deliberate
  asymmetry (the audit log still referenced vanished candidates) and that the PO "did not ask for it to be
  purged".

### 5.5 From hand-over rule to playbook

Three documents mark the arc of how the human runs the fleet:

1. `CLAUDE.md` (the contract: gates, hand-over rule, Economy).
2. `docs/process/HARNESS_LESSONS.md` and `HARNESS_FIELD_NOTES.md` (portable lessons for the template).
3. `docs/practice/fleet-project-playbook.md` (v0.1, seeded 2026-11-24): roles, lifecycle (Intake, Discovery,
   Options, Plan, Build, Review, Hand-over, PR, Deploy, UAT, Retro), consent points, estimating in *slice
   cycles*, a 12-section plan template, and coordinator rules that "cost something to learn". Its two arrows
   that are not allowed: Build to PR without hand-over; Discovery to Build without Options.

The playbook's baselines are measured, not guessed (section 5 of the playbook): a full unit gate about 39 min,
integration about 12 to 14 min, CI about 10 min, a red step about 1 h and a green step 1 h 47 m to 1 h 51 m,
so one slice cycle is 2.5 to 3 h wall clock. Rules of thumb: elapsed time is cycles x 3 h plus human
turnaround, which "usually dominates"; cycles run one at a time because they share one Docker host and parallel
gates worsen the testcontainers flake; budget one to two review rounds.

The first project run under it is the job-page redesign proposal (`docs/plans/2026-11-job-page-redesign/`,
three options with Option A, "workspace tabs", recommended). The PO decided checkpoint H1 the same day, Option A
with all five recommended defaults: "Assuming above 1-5 are approved Go A plan." H1 took hours, not days. That
was the playbook's claim that checkpoint turnaround dominates delivery, demonstrated in its first run. The proposal's evidence is a plain user quote ("so cluttered and confusing") plus the feature
that seemed to vanish after the clean slate: "A layout that hides a feature without saying why reads as a
regression." It also records the biggest non-visual risk: 27 CSRF tokens on the page, so moving a form
silently 400s if its token is no longer minted where it renders.

### 5.6 What the evolution amounts to

| Phase | The human's role | What failed | What was added |
|---|---|---|---|
| July (build) | Delegates everything; wants cheap producers | Thin reports; improvised gates | Evidence contract, `verify.sh`, tiering policy |
| Late Jul - Aug (follow-ups) | Chooses the next FU; approves | No exit condition; a merge without gates | A7 taxonomy; the gates stay merge-blocking |
| Aug 24 (reset) | Sets the destination | Rules lost to a process that generates work | Economy 0-4, the 8-item cap, ADR bar |
| Sep (sponsor/pilot) | Relays a sponsor's requirements, answers decisions | Defaults that went the other way | Declared-not-inferred, carve-out template |
| Nov (practice) | Names the project's method | Hidden features, flags not seen | Playbook, plan template, estimating baselines |

---

## 6. Where it stands on 2026-11-24, and what is open

### State

- **Shipped on `main`:** everything in section 2 through #115. The interview-question generator and the human
  decision record are merged. The pilot box was put onto `main` (`a8f5e0d`).
- **The deployment:** `https://sfuai.ca:8000`, CAS on, nginx in a separate non-git project (`C:\repos\web`).
  Taleo import is code-complete and **default off**; turning it on in production has unmet obligations (ADR-046).
  `smoke.sh` cannot run on this box (it needs CAS off); the obligation is `doctor.sh` plus a signed-in by-hand
  drive from off the LAN.
- **This branch** (`chore/handover-2026-11`) carries the job-page redesign proposal and the fleet playbook
  (`eae4377`) as the most recent commit on the log, plus this document.

### Open (from `HANDOFF.md`, `docs/ROADMAP.md`, `docs/OPEN_DECISIONS.md`)

1. **The job-page redesign** is approved (Option A, H1 decided 2026-11-24) and ready for S0; see the plan's
   §7 and §12. It is a template and handler change only, with every POST route and the API unchanged, behind
   `JOB_PAGE_V2` (default off).
2. **The GitHub Support purge** of the 99 MiB of résumés, and the decision whether the repos should be public.
   Still open at the time `HANDOFF.md` section 6 was written.
3. **Operating a real deployment** (ROADMAP item 1): retention is stored and never enforced; no `pg.jobs_stuck`
   in `doctor.sh`; blobs are permission-gated but unencrypted; the email hash is unsalted while the skill hash
   refuses to boot unsalted; audit immutability is convention only; `/health` is shallow; no backups and no
   restore drill. **A revoke-and-purge semantic (ADR-026 section 4) needs an HR decision and its own security
   review before any code.**
4. **Ranking quality versus real postings:** the vocabulary recognises 54.8% of real SFU qualification
   statements (ROADMAP item 3). The competency-scoring model (`years x recency x ontology_weight`) is flagged
   as semantically odd; its deferral "has expired" because pilot data now exists.
5. **The evidence cliff remains** (ADR-040): disclosed, not removed. The same fabricated zero appears in
   reverse match (`match_reverse_evidence_k = 10`), and disclosure reaches the entry-detail panel only.
6. **Model acceptance:** `interview_questions_v1` has no `model-check` profile yet (HANDOFF item 1, "S4");
   `taleo_segment_v1` is not in `model_probe_live.py` (ROADMAP section 5). ADR-045's transport gap (native
   versus OpenAI-compat) is chosen but unmeasured: "Do not treat the decision as data."
7. **Hardware:** the 2026-09-09 pause on feature work pending datacenter hardware has been **lifted in
   practice**: in November the PO ordered five features and three of them shipped (#112, #113, #114). The production hardware itself (ADA6000 boxes with vLLM) is still not in place, so
   inference still runs on the shared lab GPUs. `ROADMAP.md` §0 has not been updated yet (HANDOFF item 2).
8. **Owed by humans, not sessions:** an off-LAN signed-in drive of the live site (first-time-colleague lands
   on pending-access; one real write does not 403 behind the proxy; assign a hiring manager and confirm
   scoping). Rotating `FLASK_SECRET_KEY` logs everyone out; warn the four users first.
9. **Roadmap "wow" features** (ROADMAP "next feature" section): "Why this rank?" shipped as slice 1 (ADR-031);
   "Ask the pool" and "Policy Studio" are framed but not built.

### What the next team should do first

Read, in order: `CLAUDE.md` (the contract), `HANDOFF.md` (eight items), `docs/practice/fleet-project-playbook.md`
and `docs/plans/2026-11-job-page-redesign/README.md`. Then run `./scripts/verify.sh` once on a clean checkout
to prove the toolchain works for you (it needs Docker and nothing else; there is no usable Python on the
original host). The rest of the lessons in section 4 are in `HANDOFF.md` section 4 and `docs/process/`.

---

## Appendix A: ADR index

Dates are first-commit dates for the file (`git log --diff-filter=A`); an ADR's own header date is sometimes
earlier (for example ADR-003 is dated 2026-07-09, ADR-019 and ADR-021 2026-07-20, ADR-034 2026-08-11). There
is no ADR-001 in `docs/adr/`. Titles are shortened from each file's heading.

| ADR | Date | Title |
|---|---|---|
| 002 | 07-10 | Neo4j as agent memory, Postgres as transaction ledger (partly superseded by 004) |
| 003 | 07-10 | Offline inference: Ollama, OpenAI-compatible client |
| 004 | 07-10 | Phase 0: filesystem storage, asyncpg startup DDL, 768-d embedding contract |
| 005 | 07-10 | Phase 1: filesystem BlobStore interface and path safety |
| 006 | 07-11 | Phase 2: schema port, trim and DDL alignment |
| 007 | 07-11 | Phase 3: ingest and parse hardening, PII-at-rest posture |
| 008 | 07-14 | Skill-graph projection: PII elimination by construction |
| 009 | 07-15 | Matching engine port: stages, orchestrator, 4b to 4c blocker closures |
| 010 | 07-16 | Shortlist and reverse-match write path |
| 011 | 07-16 | Display redaction: read/export boundary enforcement |
| 012 | 07-17 | API routes: configurable auth, upload scope, first status transition |
| 013 | 07-17 | Phase 7: read-only Flask viewer, blind-only display |
| 014 | 07-17 | Workflow UI: Flask and HTMX (amended 2026-11-24, human decision record) |
| 015 | 07-17 | Evidence chunk-id expansion (FU-2) |
| 016 | 07-18 | Audited candidate reveal (FU-1; amended 2026-09-09, blind is opt-in per job) |
| 017 | 07-19 | Bulk ingest: per-résumé cover-letter pairing, bulk JD upload (FU-3) |
| 018 | 07-21 | RBAC with keyed roles (FU-4) |
| 019 | 07-21 | CAS identity, user records, attributable audit (FU-5; section 10 supersedes the first design) |
| 020 | 07-21 | Per-job assignment and row-level scoping (FU-6) |
| 021 | 07-21 | LLM failover and fail-closed ranking (FU-7) |
| 022 | 07-21 | An uncited evidence quote is scrubbed like a fabricated one |
| 023 | 07-22 | Evidence-verifier hardening |
| 024 | 07-24 | Per-job configurable shortlist size (top P%) |
| 025 | 07-25 | User administration and no-role-by-default access control |
| 026 | 07-29 | Résumé lifecycle: withdrawal, exclude-and-retain (FU-8) |
| 027 | 07-30 | Honest résumé parse status (FU-7) |
| 028 | 08-01 | Education field-of-study relevance |
| 029 | 08-02 | Fail-closed ranking (FU-7) |
| 030 | 08-02 | Degraded-parse visibility (FU-7) |
| 031 | 08-07 | "Why this rank?" defense pack, slice 1 |
| 032 | 08-07 | Render JD-authored skill display names, not hashes |
| 033 | 08-09 | Session-role enforcement on write routes (amended by 034) |
| 034 | 08-13 | The auth boundary failed open in the shipped configuration |
| 035 | 08-13 | CSRF on every browser state-changing route |
| 036 | 08-13 | The auditor's access-record viewer and the `audit_log` read path |
| 037 | 08-13 | Stage 3 fails closed on a non-LLM error too |
| 038 | 08-13 | Gate the bait-below-strong ordering the corpus only asserted in prose |
| 039 | 08-13 | Stage-1 recall searches the job's pool, not the whole database |
| 040 | 08-13 | Disclose the evidence cliff instead of a fabricated 0% |
| 041 | 08-14 | Sub-scores that read as measurements when nothing was measured |
| 042 | 08-17 | Skill vocabulary: merge the derived domain families |
| 043 | 08-18 | Shortlist regeneration polling gate: record "ranking" state server-side |
| 044 | 08-20 | Parse-time skill-family classifier for out-of-vocabulary names |
| 045 | 08-22 | The model is a dependency with an acceptance test |
| 046 | 09-03 | Taleo job source: an egress carve-out for `tre.tbe.taleo.net` |
| 047 | 09-26 | Screening facts are declared, never inferred (header dated 2026-09-09) |

Note on the count: the reset document says 12 ADRs carried Phases 0-7 and 32 followed. From this directory,
ADR-002 to ADR-013 are the first twelve and ADR-014 onward are the rest, with ADR-047 added after the reset
(the Economy bar's "roughly monthly" has held since: ADR-045 to 047 in about five weeks, and none in November).

## Appendix B: PR index

Merge dates are GitHub's (`gh pr list`), which are UTC and can lag the commit date by a day. A dash means
GitHub reports no merge date (closed, or integrated locally while CI was blocked; see 4.8).

| PR | Merged | Title |
|---|---|---|
| 1 | 07-11 | Phase 0: seed and infra |
| 2 | 07-11 | Phase 1: storage (filesystem BlobStore) |
| 3 | 07-11 | Phase 2: schemas |
| 4 | 07-11 | chore: subagent model tiering and handoff refresh |
| 5 | 07-11 | docs: Phase 3 starting map |
| 6 | 07-12 | Phase 3: ingest and parse pipeline |
| 7 | 07-12 | docs: Phase 3 merged, resume point to Phase 4 |
| 8 | 07-12 | Phase 4a: ranking-evals corpus |
| 9 | 07-12 | docs: Phase 4a merged |
| 10 | 07-14 | Phase 4a hardening: make the corpus able to fail a bad engine |
| 11 | 07-15 | Phase 4b: graph projection (PII-safe skill graph by construction) |
| 12 | 07-16 | Phase 4c: matching engine |
| 13 | 07-16 | Phase 4d: shortlist and reverse-match write path |
| 14 | 07-17 | Phase 5: persist, anonymize, export |
| 15 | 07-17 | Phase 6: API routes |
| 16 | 07-17 | Phase 7: read-only Flask viewer, gate-scope fix |
| 17 | 07-17 | docs: Phase 7 merged, v1 extraction plan complete |
| 18 | 07-18 | Workflow UI: Flask and HTMX |
| 19 | 07-18 | FU-2: expand evidence chunk ids to source text |
| 20 | 07-18 | FU-1: audited candidate reveal |
| 21 | 07-19 | FU-3: bulk ingest, reverse-match UI, shortlist-poll fix |
| 22 | 07-19 | docs: FU-1/2/3 merged |
| 23 | 07-21 | FU-4: RBAC |
| 24 | 07-21 | docs: FU-5/6/7 plan (ADR-019/020/021), 9 gaps filed |
| 25 | 07-21 | fix: uncited evidence quote scrubbed (ADR-022) |
| 26 | 07-21 | docs: PR 24/25 merged |
| 27 | 07-22 | fix: evidence-verifier hardening |
| 28 | 07-23 | chore: one verification path and evidence contract for subagents |
| 29 | 07-24 | FU-5: CAS identity, user records, attributable audit |
| 30 | 07-29 | fix: zip-upload error, upload 404, slow-job progress, dedupe concurrent runs |
| 31 | 07-29 | FU-6: per-job assignment and row-level scoping |
| 32 | - | Configurable shortlist size (ADR-024) |
| 33 | - | hiring_manager `/my/jobs` default view |
| 34 | - | CAS live integration: split-origin redirect fix, header auth widget |
| 35 | - | User admin: granular roles, no-role-by-default (ADR-025) |
| 37 | 07-29 | FU-8: résumé withdrawal (ADR-026) |
| 38 | 07-29 | chore: workflow_dispatch trigger for CI |
| 39 | - | docs: org move and CI reconcile in HANDOFF |
| 40 | 07-29 | docs: org move and CI reconcile in HANDOFF |
| 41 | 07-29 | docs: session wrap 2026-07-29 |
| 42 | 07-30 | fix: space/dash separators in zip cover-letter pairing |
| 43 | 07-30 | fix: hide withdrawn candidates from the shortlist read |
| 44 | 07-31 | FU-7: honest résumé parse status (ADR-027) |
| 45 | 07-31 | docs: refresh HANDOFF |
| 46 | 07-31 | fix: reverse-match read hides withdrawn candidates |
| 47 | - | docs: refresh HANDOFF |
| 48 | 07-31 | docs: refresh HANDOFF |
| 49 | 08-02 | feat: education field-of-study relevance (ADR-028) |
| 50 | 08-02 | docs: refresh HANDOFF |
| 51 | 08-02 | docs: ranking-metrics explainer for education relevance |
| 52 | 08-02 | feat: FU-7 fail-closed ranking (ADR-029) |
| 53 | 08-02 | docs: refresh HANDOFF |
| 54 | 08-02 | docs: AI-usage one-pager |
| 55 | 08-02 | feat: FU-7 degraded-parse visibility (ADR-030) |
| 56 | 08-02 | chore: PowerShell quickstart |
| 57 | 08-02 | docs: refresh HANDOFF |
| 58 | 08-02 | chore: unique host ports, CAS on by default |
| 59 | 08-02 | docs: refresh HANDOFF banner |
| 60 | 08-05 | chore: reproducible fresh-box boot |
| 61 | 08-05 | docs: refresh HANDOFF banner |
| 62 | - | docs: inference is aria-gb10 over Tailscale |
| 63 | 08-05 | chore: inference is aria-gb10 over Tailscale |
| 64 | 08-05 | docs: next-gen roadmap and session wrap |
| 65 | 08-07 | feat: "Why this rank?" defense pack (ADR-031) |
| 66 | 08-07 | fix: JD-authored skill display names (ADR-032) |
| 67 | 08-08 | docs: HR ranking-metrics explainer refreshed |
| 68 | 08-10 | fix: session role on every write route (ADR-033) |
| 69 | 08-10 | docs: sync state docs; reframe ROADMAP A2 |
| 70 | 08-10 | docs: rewrite HR explainer for its audience |
| 71 | 08-13 | docs: retire claims that PRs 68-70 made false |
| 72 | 08-13 | fix: close the auth boundary (ADR-034) |
| 73 | 08-13 | docs: record the auth boundary fix |
| 74 | 08-13 | fix: CSRF on every browser write route (ADR-035) |
| 75 | 08-13 | feat: auditor access-record viewer (ADR-036) |
| 76 | 08-13 | fix: quickstart.ps1 could not be parsed |
| 77 | 08-13 | fix: stage 3 fails closed on non-LLM error (ADR-037) |
| 78 | 08-13 | feat: gate bait-below-strong ordering (ADR-038) |
| 79 | 08-13 | fix: stage-1 recall job-scoped (ADR-039) |
| 80 | 08-13 | fix: disclose the evidence cliff (ADR-040) |
| 81 | 08-13 | docs: session wrap, plan status table |
| 82 | 08-13 | docs: handoff banner stops pinning a sha |
| 83 | 08-14 | fix: gate the shipped must_have_miss_penalty |
| 84 | 08-14 | docs: refresh; count A7 honestly |
| 85 | 08-14 | fix: mark two sub-scores that read as measurements (ADR-041) |
| 86 | 08-14 | docs: handoff for ADR-041 and the A7 lesson |
| 87 | 08-14 | chore: regroup, unblock A2, add stopping rules |
| 88 | 08-17 | feat: corpus-derived skill vocabulary, 15.6% to 54.8% (ADR-042) |
| 89 | 08-17 | docs: handoff for A2, mis-scoped-blocker lesson |
| 90 | 08-19 | chore: memos for the two blocked decisions |
| 91 | 08-19 | docs: two documents described the skill pipeline wrongly |
| 92 | 08-19 | feat: disclose A6's three sibling fallback sub-scores |
| 93 | 08-19 | fix: Regenerate shortlist shows what it is doing; schema bootstrap race |
| 94 | 08-20 | feat: skill-family classifier at parse time (ADR-044) |
| 95 | 08-20 | feat: every read needs a real principal (D2 = B) |
| 96 | 08-21 | feat: audited reveal of a withheld withdrawal reason (D1 = C) |
| 97 | 08-23 | D1 reveal, pipeline end to end, three verification tools, PII containment |
| 98 | 08-23 | Re-parse a failed JD |
| 99 | - | docs: refresh HANDOFF for session 3 |
| 100 | 08-25 | Reset: the product has been finished for 38 days |
| 101 | 09-02 | Sponsor requirements: work authorization, 10% moved off the cover letter |
| 102 | 09-03 | Sponsor requirements: the manager's additional requirements |
| 103 | 09-03 | Close the published session key; permit the Taleo carve-out (ADR-046) |
| 104 | 09-09 | Sponsor requirements: Taleo import, links, manager prompt, dept/campus, blind opt-in |
| 105 | 09-15 | Refuse to rank a job whose JD yielded no requirements |
| 106 | - | Serve the app at sfuai.ca:8000 behind nginx, CAS on |
| 107 | 09-26 | Manager's guide, end-to-end run, isolated stress build |
| 108 | 09-26 | Close the five gaps the end-to-end run found |
| 109 | 09-26 | Refuse an unsplit Taleo export; never parse a cover letter as a résumé |
| 110 | 11-22 | Re-parse a failed or degraded résumé; verify.sh from a worktree |
| 111 | 11-20 | sfuai.ca and localhost both log in; containers survive a reboot |
| 112 | 11-22 | In-app Taleo split and anonymized résumé export |
| 113 | 11-23 | Interview-question generator |
| 114 | 11-24 | Human decision record |
| 115 | 11-24 | The worker loads `.env` so `TALEO_ENABLED` reaches the Taleo import |

Note: PR #36 does not appear in the `gh` listing. PR #110's merge date (11-22) is later than #111 (11-20) even
though its commit `da8dc44` is dated 11-21 in the log; the log order and merge order differ.

## Appendix C: where to look

| For | Read |
|---|---|
| The contract and standing orders | `CLAUDE.md` |
| Current state, eight items | `HANDOFF.md`; history in `docs/archive/HANDOFF-*.md` |
| Plan of record and open work | `docs/ROADMAP.md`, `docs/OPEN_DECISIONS.md` |
| The original v1 plan | `docs/EXTRACTION_PLAN.md` |
| The reset diagnosis | `docs/archive/RESET-2026-08-24.md`, `docs/process/HARNESS_FIELD_NOTES.md` |
| Portable harness lessons | `docs/process/HARNESS_LESSONS.md`, `docs/SUBAGENT_MODEL_POLICY.md` |
| Running a project through a fleet | `docs/practice/fleet-project-playbook.md` |
| Pilot-user reports | `docs/pilot-feedback.md` (read the PII warning first) |
| Sponsor requirements | `docs/SPONSOR_REQUIREMENTS_PLAN.md` |
| Deployment | `docs/deploy/sfuai-ca.md`, `docs/model-profiles/` |
| The next piece of work | `docs/plans/2026-11-job-page-redesign/README.md` |
