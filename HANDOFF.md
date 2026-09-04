# Session Handoff — recruiter-assistant

**Eight items. Hard cap.** A ninth means one of these is no longer relevant —
delete it. History goes to [docs/archive/](docs/archive/), never inline. Plan of
record: [docs/ROADMAP.md](docs/ROADMAP.md).

---

### 1. The objective — it changed on 2026-08-27

> **The four people on the pilot box can do their real hiring work in it, and
> what they hit gets fixed before anything they haven't hit gets built.**

The product was demoed to the CIO and HR. Both approved. It is deployed on a
**dedicated box and four people are using it.** The previous objective — "get a
user" — is met, and [docs/RESET.md is archived](docs/archive/RESET-2026-08-24.md)
because of it.

**What this does and does not license.** The build phase is open again: new
features are wanted. It does not license going back to inspecting the code the
repo already has. **User-sourced work outranks self-sourced work** — that is the
whole of `CLAUDE.md` §Economy, and it is the only thing that has ever stopped
this repository gold-plating itself.

### 2. Do this first — the pilot box is not this box

Everything in `docs/ROADMAP.md` §"Where things stand" describes the *product*.
Nobody has written down the *deployment*: its address, whose `.env` it runs, when
it was last updated from `main`, or whether it has ever had `doctor.sh` run
against it. **Find that out and record it here**, replacing this paragraph.

Two things to check the moment you have access:

- **`FLASK_SECRET_KEY: dev-only-change-me` is committed** in
  [docker-compose.yml:150](docker-compose.yml#L150) and
  [compose.cas.yml:31](compose.cas.yml#L31). If the pilot box booted from either,
  four real sessions are signed with a published key and any of them is forgeable.
  Highest-severity open item in the repo — ROADMAP open item 1.
- **`./scripts/doctor.sh` against the pilot box.** It exits non-zero on a failed
  invariant *or on a datastore it could not reach* — "could not check" is never a
  clean bill of health. It is the only tool that sees defects living in state
  rather than in code, and this repo has shipped four of those.

The local dev stack is still up on `:29500` UI · `:29800` API · `:29432` pg ·
`:29474`/`:29687` neo4j. If CAS login is needed here, `.env` still carries the
stale `CAS_SERVICE_BASE_URL=http://localhost:8000`; the correct values are in
`.env.example` (`:29800` API, `:29500` frontend, `LLM_TIMEOUT_S=900`).

### 3. The sponsor picked the next feature — and it is none of the three cards

The DTO/CIO sent a requirements set on 2026-09-02 and answered all four open
decisions the same day. Plan of record:
[docs/SPONSOR_REQUIREMENTS_PLAN.md](docs/SPONSOR_REQUIREMENTS_PLAN.md); read its
§0 first, because **three of the four answers went against the recommended
default.** Work in flight on `feat/sponsor-requirements`.

Still open alongside it, and neither is superseded:

1. **Open a channel from the four users back into this repo.** There isn't one.
   Every pilot defect so far arrived by someone mentioning it in conversation, and
   each was worth more than a week of inspection. A dated `docs/pilot-feedback.md`
   is enough to start. ROADMAP open item 2.
2. **Close ROADMAP open item 1** — the committed secret, `pg.jobs_stuck` in
   `doctor.py`, retention enforcement. A live deployment demands these; a dev
   stack did not.

#### What has landed, and what is next

**Landed on `feat/sponsor-requirements`** (gates green, offline + integration):

- **Work authorization (§O2) end to end.** `resumes.work_authorization`, three
  states, `NOT NULL DEFAULT 'unknown'`; audited idempotent write; recruiter
  control on the résumé page; read-time band; card + CSV. The sponsor's answer
  was *"Last but visible … all other metrics are invalidated"*, so an ineligible
  candidate keeps its card but shows `—` for rank and `n/a` for every sub-score,
  with the reason on screen.
- **The manager's additional requirements (§I4/§O3) end to end.** The 10% moved
  off the cover letter (`manager_prompt = 0.10`, `motivation = 0.0`), the
  `manager_prompt_v1` extraction as a second pass that never re-runs the JD
  parse, deterministic scoring, the create-form box, and **"Added by you" chips
  on the shortlist** so a manager can see whether what *they* asked for was
  considered. Merged as #101 and #102.
- **`FLASK_SECRET_KEY` (ROADMAP open item 1) closed.** Three layers: generated
  by `quickstart.ps1`, sourced from the environment by both compose files, and
  refused at boot on a published default. **The pilot box still needs the
  quickstart run against it** — and rotating logs everyone out, so warn them.
- **Taleo import (§I3), both slices.** [ADR-046](docs/adr/046-taleo-job-source-egress-carveout.md)
  — the egress carve-out, superseding ADR-012 §2's deferral — the pure parsers
  and five vendored fixtures, then the client, DDL, upsert, sync task and admin
  trigger. All behind `TALEO_ENABLED=false`; the three obligations ADR-046
  records are **unmet**, so it must not be enabled anywhere yet.
- **Document links (§O4).** The résumé and cover letter are served from their
  blobs, audited, with the filename derived from the résumé id rather than the
  uploaded `original_filename`.
- **The jobs table's three dead columns.** Location, Source and Résumés each
  rendered a `job.<field>` that `JobListItem` did not have — an em-dash or a
  blank cell forever, indistinguishable from real null data. Source is now the
  LINK back to the posting the sponsor asked for, Last updated joins it, and
  `resume_count` is a correlated count in the list query. Fixing the plumbing
  populated nothing by itself — 0 of 26 rows had a location — which is what the
  next item exists to change.
- **Department and campus, parsed and overridable** (sponsor, 2026-09-03).
  `jd_extract_v2` adds `department` and asks for the campus; `record_parsed`
  now writes `title`/`department`/`location` onto the ROW instead of only into
  a JSONB blob nothing reads — which is why 23 requisitions displayed their own
  filenames while the extraction beside them held the real title. Fill-when-
  empty, so an override survives a re-parse. `src/campus.py` is the one place a
  campus is spelled; a form on the job page fills or overrides either field.
- **The Taleo combined-PDF splitter**, `core/scripts/split_taleo_pdf.py`, run
  via `scripts/split-taleo.{sh,ps1}`. It had sat untracked at the repo root for
  eleven days importing two hris modules that do not exist here. `make gates`
  and CI now lint and type-check `core/scripts` (not coverage), which is what
  makes that impossible to repeat.

**Next, in order:** **re-parse the 20 pilot jobs that still have no
department** — this is the only unfinished half of the 2026-09-03 request, and
it is BLOCKED ON A LOCAL CONFIG BUG, not on code. `./scripts/doctor.sh` says
so on its own — its ONLY finding is `deploy.timeout_below_profile`:
`LLM_TIMEOUT_S` is 120s where the committed model profile measured
`gpt-oss:20b` at **838s** under this concurrency. Two re-parses were observed
dying on `ReadTimeout` and tripping the circuit breaker, exactly as the
2026-08-21 note predicts. Set it to 838+ (`.env.example` says 900) before
enqueuing anything.
`core/scripts/backfill_job_fields.py --reparse-plan` prints the ids. Then:
notifications (`mailhost.sfu.ca:25`, in-app table first) → candidate CSV (§S3,
**blocked on a sample export** — ask for one) → blind review on the ranked
list (§O5).

**Owed and not yet written: two ADRs** from the work-authorization slice — the
screening decision (it must record *why inference was rejected*) and an ADR-009
amendment for the weight move. The document-download route needs no ADR: it is
one obvious implementation, and the reasoning is in its commit.

**Six things a future session must not rediscover the hard way:**

1. **`pipeline_meta.weights` is a historical stamp and the read path validates
   it UNCAUGHT.** Adding a weight field with a non-zero default makes every
   pre-existing stamp fail its sum validator — a 500 on every shortlist page for
   every job ranked before the change. There is now a `mode="before"` shim and a
   regression test; do not remove either.
2. **A new weight can be declared, validated, and applied by nothing.**
   `manager_prompt` passed its sums-to-1.0 validator and neither combine site
   multiplied it in -- every score came out 10% low, and no gate could see it
   because uniform deflation reorders nobody while `ranking-evals` is an
   ORDERING gate. `test_top_blend_is_fully_applied.py` now asserts a perfect
   candidate scores exactly 1.0. Do not delete it to "simplify".
3. **The eval corpus had a control asserting a cover letter RAISES rank.** It
   was inverted into `[cover_letter_neutrality]` (exact score equality), not
   deleted. The threshold key set is a **three-way contract** —
   `thresholds.toml`, `run_evals.py`, `.claude/agents/ranking-evals.md` — plus
   `labels.json` and `_THRESHOLD_KEYS`. All five move together.
4. **Do not JOIN `resumes` into the shortlist read.** `_ENTRY_COLS` selects bare
   `id`/`job_id`, which `resumes` also has, so a join makes them ambiguous and
   every non-blind integration test fails; `WHERE job_id = $1` is also the
   `.replace` anchor `list_for_job` uses for FU-6 row scoping. The band uses a
   correlated subquery for exactly this reason. **The unit suite cannot see
   this** — it asserts on the SQL as a string.
5. **An LLM extraction can be written to a blob and to no column.**
   `record_parsed` wrote `description_parsed` and nothing else for the whole
   life of the project, so the title, department and location it read out of
   every JD went somewhere no screen looks. It surfaced as three separate
   complaints (empty Department, empty Location, requisitions named after their
   own files) that were one defect. **The general question to ask of any new
   extracted field: which column does it land in, and what happens on the
   second parse?** The merge rules are in `_RECORD_PARSED_SQL` and the
   real-Postgres proof in `test_jd_writeback_merge_pg.py`.
6. **A Jinja template can render a field the DTO does not have, silently.**
   Undefined renders as an empty string and compares as "not none", so three of
   the jobs table's seven columns were dead for months and looked exactly like
   null data. `test_jobs_table_columns.py` now compares every `job.<x>` in
   `index.html` against `JobListItem`. **No other template has that guard** —
   `detail.html` and `shortlist.html` have never been checked, and a page whose
   cells are all populated is not evidence, only a page with a suspiciously
   empty column is.

   **And the sharper version of the same thing, which reached users on
   2026-09-03: a template can call a method the JSON TYPE does not have.**
   `{{ job.updated_at.strftime(...) }}` 500'd the whole jobs list, because the
   frontend is a BFF — it reads JSON over HTTP, so every timestamp is a
   **string** (`'2026-09-03T23:10:55.264135Z'`), never a `datetime`. Nothing
   caught it: the new tests string-matched the template SOURCE, the older
   frontend tests hand-write fixture dicts (which is where a `datetime` gets
   typed by mistake), and `smoke.sh` FAILS rather than runs while CAS is on.
   **Do not write a fixture dict for a page test.** Build the DTO and
   `model_dump(mode="json")` it, the way
   `tests/unit/test_templates_render_api_shaped_rows.py` does — that is what
   makes the guard survive the next field. Use the `| day` filter for dates;
   never `.strftime` in a template.

### 4. Current state

| | |
|---|---|
| `main` | `b012e82` — sponsor PRs #101 + #102 merged |
| Branch in flight | `feat/sponsor-requirements` — §I3 Taleo, §I4 manager prompt, §O2/§O3/§O4 |
| Gates, this branch | 5,762 unit · 567 integration · 91.99% coverage — **re-run, do not cite** |
| Lint paths | `src tests frontend scripts` in **both** the Makefile and `ci.yml`; a test pins them equal |
| Verification | `verify.sh` code · `smoke.sh` screen · `doctor.sh` data · `model-check.sh` before a model swap |
| Postgres | `psql -U app -d recruiter` — there is no `postgres` role |
| LLM hosts | gb10 `100.88.247.106` · spark1 `100.114.185.88` — **both shared** |
| Egress | **ONE carve-out**, ADR-046, `TALEO_ENABLED=false` by default. Nothing else leaves. |

Working end to end: upload → parse → rank → shortlist; JD ingest; blind review,
PII encryption, audited reveal; CAS identity, session role enforcement, CSRF,
auditor viewer; work-authorization screening; the manager's own requirements.

**Verified against the RUNNING product** during this work: `smoke.sh` 10 passed
(the browser→Flask→API seam); the work-authorization DDL confirmed live with all
19 existing résumés back-filled to `unknown` and zero NULLs; the new route
registered and 401ing an unauthenticated write. `doctor.sh` returns one finding —
CAS is disabled on the **local dev stack**, which `smoke.sh` requires, so the two
cannot both be satisfied here; it is a real finding for the pilot box, not for
this branch.

**Still not clicked by a human:** the work-authorization radio and the manager's
requirements box render and their round trips are unit-tested, but nobody has
used them in a browser. Authenticating from here needs a role key out of `.env`,
which this session did not read.

**The integration suite flakes.** `ERROR at setup` on `asyncpg.connect` in
whichever file draws the short straw — seen once in this branch's history on
`test_job_assignees_pg.py`. It is a testcontainers resource limit, not a defect.
**Re-run to confirm rather than assuming**; a clean re-run is the evidence, and
"known flake" is exactly the phrase that hides a real failure.

### 5. Never diagnose the model on a contended peer

Check `GET /api/ps` first — **and again during a long run**. Both attempts to
measure ADR-045's transport gap were destroyed mid-flight: a foreign 70GB
`gpt-oss:120b` landed on gb10 after it was verified idle, and the re-run's host
rebooted. A contended box once produced a confidently wrong retraction of a
correct fix.

Related and equally load-bearing: **the token floor is per-PROMPT, not
per-model.** `REASONING_JSON_MIN_TOKENS = 8192` is right for résumé/JD extraction
and wrong as a universal — the `skills_graph` tiebreaker at `max_tokens=128` gives
*identical* answers to 8192, measured. Do not "fix" a small budget on sight, and
do not inherit a literal into a new call site; measure it.

### 6. PII — one deferred step, now overdue

`fixtures/` (117 files, 99 MiB of real résumés) was pushed to a PUBLIC repo by a
`git add -A`. Done: branch ref deleted, history rewritten, `.gitignore` guard
committed. **Still open:** deleting the branch did *not* stop GitHub serving the
data — tested, not assumed. Only a **GitHub Support purge** removes it. Both
`humanaxiom/` and `sfu-aria/` are PUBLIC. ~15 minutes of someone's time, open
since 2026-08-21, and the only item in this repo with a live external exposure.

**Never `git add -A` in this repo.** `fixtures/` is untracked by design —
provision it out-of-band. Both harnesses hard-fail rather than skip without it,
so an unprovisioned clone cannot report a green run that tested nothing.

### 7. Settled — do not re-litigate

- **ADR-045's transport gap.** Direction chosen (give the app schema-constrained
  decoding, portable to vLLM `guided_json`); **no measurement exists to support
  it.** Do not treat the decision as data.
- **D1 and D2 are answered and shipped** (audited reveal of withdrawal reasons;
  every keyed read needs a real principal). `docs/OPEN_DECISIONS.md` is a record,
  not a question.
- **The A7 defect taxonomy is closed at 21 instances and is not to be extended.**
  The pattern is real; naming it turned finding more instances into a work
  generator. What survives is in ROADMAP §"What the gates cannot see".
- **Competency scoring is no longer deferred by precondition.** It was blocked on
  *"corpus owner + HR, with pilot data"* — there is now pilot data. ROADMAP open
  item 3.

### 8. Host quirks

No usable Python on this host (only the WindowsApps stub) — use
`./scripts/verify.sh`, never a hand-written `docker run`. `quickstart.ps1` needs
`pwsh` 7; it fails to *parse* under PowerShell 5.1, and `powershell.exe` still
exits 0. Publish unique host ports (29xxx) — many other containers on this
machine collide on stock 5432/8000/5000. Two Claude sessions drive this repo at
once: re-read git and PR state in the same call that commits, pushes, or merges.
