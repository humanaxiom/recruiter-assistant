# Session Handoff — recruiter-assistant

**Eight items. Hard cap.** A ninth means one of these is no longer relevant —
delete it. History goes to [docs/archive/](docs/archive/), never inline. Plan of
record: [docs/ROADMAP.md](docs/ROADMAP.md).

---

## ▶ START HERE — the site is LIVE; this branch carries a guide, a findings doc, and an isolated stress stack

**Resumed 2026-09-15**, superseding the 2026-09-09 pause: the zero-requirements
guard shipped (PR #105, merged), and the site went live at
**https://sfuai.ca:8000** with CAS on (PR #106, open). **On 2026-09-17 the user
asked for three more things before sharing the product with the DTO**: a
manager's guide, an end-to-end run of it, and a multi-user stress build. All
three are on this branch, `feat/e2e-stress-build`.

> 🔴 **IMPORTANT DTO REQUIREMENT (user, 2026-09-18) — next after the five
> gap fixes land.** *"The Taleo PDF which contains résumés and potentially
> cover letters for the same candidate are treated all as résumés by the
> extraction module upon upload to a job."* Revisit and fix. What exists:
> `core/scripts/split_taleo_pdf.py` (run via `scripts/split-taleo.{sh,ps1}`)
> segments the combined export per applicant with a cover-letter detector and
> an LLM manifest, emitting `NNN_name_resume.pdf` + `NNN_name_cover_letter.pdf`;
> the upload pairs cover letters by that filename convention or by a manifest
> (`bulk_ingest_service.pair_applicants`). The reported behaviour means one of:
> the combined PDF is uploaded unsplit and parsed as one résumé; the split
> output's cover letters lose the suffix or the pairing and are ingested as
> résumés; or cover-letter-only applicants (a recorded gap: pages written but
> excluded from `manifest.json`). Reproduce with the DTO's bundle on the
> isolated stack, then fix so a combined Taleo PDF uploaded to a job yields
> one résumé per applicant with its cover letter attached, never a cover
> letter parsed as a résumé.
>
> **Status 2026-09-18, `feat/taleo-combined-pdf`: the defect is closed; the
> in-app split is the follow-on.** Four causes were found and three fixed:
> (1) upload classification was filename-suffix only, so an unsplit export
> became ONE résumé row and every applicant's pages, cover letters included,
> parsed as one candidate — now refused as a rejected row ("looks like a
> combined Taleo export … split it first with scripts/split-taleo.sh") by a
> page-count + distinct-header-email rule that never raises; (2) the splitter
> zipped `manifest.json`, which the upload's allowlist refuses, so its own
> instruction could not succeed — the zip now carries PDFs only and the
> printed instruction names the two form fields; (3) a cover-letter-only
> applicant was promoted to a résumé by design — a cover-named orphan whose
> text reads as a cover letter is now disclosed as unattached, never
> ingested (ADR-017 amended). Driven by hand on the isolated stack with a
> combined PDF built from fixture pairs: refused with the guidance, count
> unchanged; a pair plus an orphan → one accepted with its cover letter, one
> rejected, one parse enqueued. **Not done, deliberately:** the sponsor's
> in-app split (SPONSOR_REQUIREMENTS_PLAN §2.0, with its mandatory
> confirmation screen) — the segmentation prompt has no measured budget in
> `docs/model-profiles/`, and a mis-split attributes one person's experience
> to another. Until it exists the operator runs `scripts/split-taleo.sh`
> first; the product now says so instead of silently ingesting. Known modes
> of the detector: a single-applicant PDF carrying three distinct header
> emails is refused (recoverable, the reason says why); a scanned export
> with no header text is not detected.
>
> **Driven with the DTO's real bundle on 2026-09-19** (`data/HR Hriing
> Tool`, gitignored: four exports of 43–53 pages, one 3-page PDF, the
> 316-row roster CSV). All four exports refused with the right counts (19
> distinct-email pages each); the 3-page file accepted as one résumé. The
> splitter's LLM segmentation was **intermittent on one export**: 20
> applicants with one page assigned to nobody (twice) or 18 with two people
> merged into one file (once) — page accounting caught the first, nothing
> caught the second. Two more splitter invariants landed from that: a
> résumé file carrying two applicants' emails fails the run, and an orphan
> page whose email equals its neighbour's is attached with a `REPAIRED:`
> line; the third run then produced 20 + 4 cover letters cleanly. Upload of
> the zip + manifest: 18 accepted, 4 with cover letters, 0 cover-named rows.
> Roster CSV on the parsed set: **14 of 15 matched**, 14 work-authorization
> facts, 301 rows unmatched (the whole pool; normal). **4 of 19 real parses
> failed**: 1 with the known skills-pass empty-content mode, 3 abandoned by
> the stalled-parse reconciler — real parse latency was median 715 s, max
> 1157 s against `LLM_TIMEOUT_S=900`, **while the splitter's own segmentation
> was running on the same GPU**, so that number is contended and not a clean
> measurement. `scripts/split-taleo.sh` also needed the MSYS path guard the
> other scripts carry. Still not built: the in-app split with the sponsor's
> confirmation screen.
>
> **2026-09-23, `feat/resume-reparse` (stacked on this branch): a failed or
> degraded résumé can be re-parsed from its own page.** The 4-of-19 failures
> above had no recovery but re-upload. `POST /resumes/{id}/reparse` resets
> the row to `uploaded` (failure reason, parse output and reconcile count
> cleared, `reparse_requested_at` stamped so the reconciler gives the retry
> its own 30-minute grace instead of double-enqueueing it) and enqueues
> `parse_resume`; 409 with a plain reason for withdrawn, in-flight or
> cleanly-parsed rows. The button sits on the reusable page token — no
> fourth one-shot slot. **Driven end to end on the isolated stack** (stub
> LLM stopped → upload → `failed: llm unavailable after 5 retries` → stub
> started → page shows "Reason:" + button → POST without token 403, with
> the form token 302 → `uploaded`/`parsing`/`parsed` in 30 s → button gone →
> POST on the clean parse 409); the worker log shows exactly one new arq
> job and no reconciler re-queue; `doctor.sh` on that stack reports only
> its by-design CAS-off finding. Gates: `verify.sh all` 6451 unit @ 92.12%,
> 655 integration, green. **Also on that branch: `scripts/verify.sh` now
> works from a `git worktree`** (it mounts the main checkout's `.git`
> read-only and sets `GIT_DIR`) — before that it died on the branch-name
> gate, so every earlier "gated from the worktree" claim went through
> something narrower.

**What's on this branch:**
- [docs/guides/managers-guide.md](docs/guides/managers-guide.md) — screen-by-screen,
  derived from the deployed code, with "Be aware" boxes for gaps.
- [docs/guides/e2e-findings-2026-09-17.md](docs/guides/e2e-findings-2026-09-17.md) —
  the guide driven step by step against an **isolated** copy of the product
  (`docker compose -p recruiter-stress`, 28xxx ports, CAS off, its own
  Postgres/Neo4j/blob volumes, the real `gpt-oss:20b`). **The live site was
  never touched** — CAS on means every write needs a real SFU login, which a
  script cannot do.
- `scripts/e2e.sh` (one user, whole guide, real model, isolated stack) and
  `scripts/stress.sh` (N concurrent users, `STRESS_LLM=stub` by default,
  `STRESS_LLM=real` requires explicit confirmation and is capped at 30
  résumés/run).
- A first-boot Neo4j bootstrap retry fix — on an empty graph, the API and
  worker raced to create schema and the API exited; the pilot box never hit
  this because its graph predates the race. Any fresh box for the DTO would
  have.

**Results, this session:**
- `smoke.sh` on the isolated stack, real model: **10 of 10 passed, 901s**.
- `scripts/stress.sh USERS=3 RESUMES_PER_USER=3` (stub model): **PASS — 3
  users × 16 steps, 48 samples, 0 errors, 0 timeouts**.
- `scripts/e2e.sh` (one user, real model): **PASS — smoke 10 of 10 in 1046 s, then all 16 driver steps with 0 errors and 0 timeouts (JD parse 265 s, three résumés plus a cover letter 660 s, ranking 215 s); doctor reports only the CAS-off state, which is the isolated stack's design** (to be
  filled in when that run completes/is re-run).

**What the DTO must do by hand** (not scriptable — real CAS only; see the
findings doc's "What only you can check"):
- Sign in with SFU CAS from **outside the LAN**; confirm a first-time
  colleague lands on pending-access and can be granted a role from the admin
  screen.
- Make one real write (a declaration or a withdraw) on the live site and
  confirm it does not 403 behind the proxy.
- Assign yourself (or another recruiter) as hiring manager on a job from the
  new "Assigned hiring managers" section on the job page, then confirm a
  hiring-manager sign-in shows that job and no others.

**All five candidate fixes from the findings doc are done, on
`feat/complete-build-gaps`**: a dropped second Generate is now queued and
re-run automatically; the job page has an "Assigned hiring managers" section
(add/remove, admins and recruiters); every shortlist card carries a "Why this
rank?" link to the entry-detail page; the roster name match strips a trailing
credential suffix (CA/BA/MA excepted); and a draft JD that parsed to zero
requirements gets a Re-parse button plus a description editor that re-parses
on save (refused on an open job).

**Standing rules that changed or newly apply:**
- **The stack serves the working tree, not an image.** Never `git checkout`
  while a stack (smoke/stress/e2e/hand-drive) is running against it — use a
  separate worktree, exactly as this task did.
- **Scripted `e2e.sh`/`stress.sh` never target the live site** — isolated
  stack only, CAS off, own volumes/ports.
- **Stress in real-LLM mode is capped at 30 résumés/run** and needs explicit
  confirmation — it spends the shared GPU's time, which pilot users need.

**The roster branch is unchanged and still unpushed.** `feat/candidate-roster-csv`
is `main` + the proxy/TLS work; this e2e/stress branch is not merged into it yet. Still pending the
user's own review before push — see §3 below for what it carries; the
roster-specific "must not rediscover" points there are still true and are not
repeated here.

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
**Recorded 2026-09-09: this box IS the pilot box.** It runs at
`:29500`/`:29800` on this machine, booted from this checkout's `.env` plus the
untracked override below.

> ⚠️ **The pilot data was WIPED on 2026-09-09 at the user's explicit request**
> ("the pilot data can be wiped clean"), to load the DTO's bundle onto a clean
> box. Removed: 29 jobs, 48 résumés, 32 shortlist entries, 88 outbox rows and
> 52 blobs — including the DTO's own ranked job and the director-demo job that
> earlier entries in this file describe as live. **Those are gone; do not go
> looking for them.** `users` and `audit_log` were KEPT, which is a deliberate
> asymmetry worth knowing: the audit log still references candidates whose data
> no longer exists. The user was told and did not ask for it to be purged.
>
> What is on the box now: the **Business Analyst** requisition from the DTO's
> bundle, with the manager's prompt attached, 75 résumés, and the 315-row
> roster reconciled onto them. It was rebuilt from the branch head on
2026-09-09 (three times that day, each after a gate), `doctor.sh` has run
against it after every deploy, and the one finding it reports is the CAS-off
decision. `FLASK_SECRET_KEY` is no longer committed anywhere; the quickstart
generates it and boot refuses a published default.

The local dev stack is still up on `:29500` UI · `:29800` API · `:29432` pg ·
`:29474`/`:29687` neo4j. If CAS login is needed here, `.env` still carries the
stale `CAS_SERVICE_BASE_URL=http://localhost:8000`; the correct values are in
`.env.example` (`:29800` API, `:29500` frontend, `LLM_TIMEOUT_S=900`).

**CAS is ON as of 2026-09-15.** The box is now served at
`https://sfuai.ca:8000` through a second, non-git compose project
(`C:\repos\web`, container `sfuai-web`, nginx) that terminates TLS with a
Let's Encrypt cert and reverse-proxies `/auth/cas/` to the API (`:29800`) and
everything else to the frontend (`:29500`); `:8000` rather than `:443` because
the router only forwards a public TCP port range to this box, and 443 isn't
in it. The untracked `docker-compose.override.yml` that forced CAS off is
retired (moved to the scratchpad). Full runbook — topology, exact nginx
config, `.env` keys, cutover and revert steps, residuals — is
[docs/deploy/sfuai-ca.md](docs/deploy/sfuai-ca.md).

**Same day, a debugger exposure was found and closed.** A forged `Host`
header could reach the Werkzeug interactive debugger through the proxy. Fixed
same-day with `--no-debugger` on the frontend (load-bearing) plus a
default-deny `444` catch-all in nginx for any request whose Host/SNI isn't
`sfuai.ca` (a second, independent layer — Host injection, not the debugger
itself). `FLASK_SECRET_KEY` and the four `API_KEY_*` values were rotated the
same day (2026-09-15) as a precaution; rotating `FLASK_SECRET_KEY` logs
everyone out.

**`smoke.sh` can no longer run on this box** — it requires CAS off and fails
rather than skips when CAS is on. The obligation is now `doctor.sh` (run
after every deploy; the `deploy.auth_disabled` finding should be gone) plus a
by-hand drive **from off the LAN** (hairpin NAT blocks an on-LAN client from
reaching the public IP): CAS login as a real principal, land on the jobs
list, one real write that does not 403. That off-LAN drive has not yet been
run and is owed before this state is trusted.

### 3. IN FLIGHT — the candidate roster CSV (`feat/candidate-roster-csv`)

**The DTO delivered a real Taleo bundle on 2026-09-09** and confirmed *"this is
sample data, so BA is NOT the only job"* — so this is a general capability, not
one requisition. 16 commits, gates green, **not pushed**. Full report:
[docs/pilot-feedback.md](docs/pilot-feedback.md).

**Delivered:** `parse_candidate_csv` + reconciliation (email-hash → normalised
name, both refusing ambiguity rather than guessing), `resumes.internal_apsa`/
`internal_cupe`, `set_internal_status`, `POST /jobs/{id}/candidate-roster`, a
+0.05 disclosed uplift for SFU-internal candidates, the card chip, the upload
form, ADR-047 + amendments to ADR-009/ADR-017, and two splitter fixes.

**Verified on the live box, not only in tests:** 75 résumés uploaded, and
**every parsed résumé matched its roster row — 39 of 39 at last count, 100%.**
One real candidate is both APSA and CUPE internal; one real candidate is
`not_eligible` **and** CUPE-internal, which is the two mechanisms in tension on
a real person — the band wins, the uplift cannot resurrect them, both facts
disclosed. No fixture would have produced that case.

**Four things a future session must not rediscover:**

1. **The workflow is Upload → Parse → ROSTER → Rank.** `candidate_email_hash`
   and `candidate_name` are NULL until the parse extracts them, and they are
   the only two reconciliation keys. A roster uploaded before parsing finishes
   matches **0 of 315** and says nothing about why. Recorded, not fixed.
2. **Parsing runs ~29 résumés/hour** (batches of 4, ~8 min each, measured).
   75 résumés ≈ 2.5 h; a full 315-candidate requisition ≈ **11 hours**.
   **This is a HARDWARE bound and the arithmetic says so.** One parse is 2–3
   SEQUENTIAL LLM calls (`resume_core_v1`, `resume_skills_v2`, and
   `cover_letter_v1` when present), so `max_jobs=4` puts **up to 12 concurrent
   requests on a single GPU** running a 20B model — which relocates the queue
   into Ollama rather than raising throughput. Against the profile's ~35s
   uncontended call, the floor for 75 résumés is ≈1.8 h and we observe ≈2.5 h:
   **within ~1.4× of the floor.** Tuning buys ~30%, not 10×. A step change
   needs more/faster GPUs, a smaller model, or fewer calls per résumé.
   **The user's call, 2026-09-09: pause feature work until the datacenter
   hardware is ready.** Do not spend sessions optimising the queue against
   this ceiling.
   **And the sharp edge of it: "Generate shortlist" QUEUES BEHIND every
   parse.** arq is FIFO at `max_jobs=4`; with ~1,400 jobs queued the request
   returns 200, sets `shortlist_state='ranking'`, and then sits for hours. It
   reproduces the exact pilot complaint of 2026-09-09 ("Generate shortlist has
   not been producing anything") from an unrelated cause, and the page still
   says "several minutes". Surfacing the real queue depth is cheap and removes
   the whole "is it broken?" class; a separate lane for interactive work is
   the real fix. **Do not just raise `max_jobs`** — lesson 7 applies.
3. **The CSV is the FULL export; the résumés are a subset.** ~240 unmatched CSV
   rows are the NORMAL state, forever. The report currently enumerates them,
   which buries the number that matters: **résumé-side** coverage. Recorded.
4. **`About This Role.txt` arrived as Windows-1252, not UTF-8.** Uploaded raw it
   pushes mojibake into the JD text and every screen rendering it. Convert with
   `iconv -f WINDOWS-1252 -t UTF-8` before ingest.

**Two gate failures worth remembering, because everything was green for both:**

- The roster upload **503'd on first real use** — `pgp_sym_decrypt` with no
  transaction, so no PII key. 6067 unit tests passed because they mock
  `pii_service` wholesale, and `reconcile_candidate_roster` had **no
  integration test at all**. A mock agrees with any transaction state you ask
  it about.
- A mutant flipping `if resolved_wa != "unknown":` to `if True:` **survived all
  6067 unit tests** — a blank CSV cell would overwrite a recruiter's audited
  screening decision. Both now pinned.

**⚠️ And the one that was self-inflicted:** real candidate PII (a name, a phone
number, an email) was **committed** to `docs/pilot-feedback.md` and three other
files while documenting PII hygiene. Caught by the security gate before push.
History rewritten; verified clean by extracting **all 925 name and email tokens
from the real roster** and scanning the entire branch diff and every commit
message against them. **That scan is the only method that worked** — three
earlier passes using remembered patterns each missed something (a name split
across a line break, lowercase token forms, a 4-character surname excluded by
my own length filter). A standing warning now sits at the top of
`pilot-feedback.md`.

**Remaining, recorded not fixed:** the parse-ordering message; the report's
CSV-side emphasis; an unbounded `_JOB_RESUMES_SQL` fetch; cover-letter-only
applicants whose pages are written but excluded from `manifest.json`.

#### What came before — the sponsor set, delivered as PR #104 (merged 2026-09-09)

The DTO/CIO sent a requirements set on 2026-09-02 and answered all four open
decisions the same day. Plan of record:
[docs/SPONSOR_REQUIREMENTS_PLAN.md](docs/SPONSOR_REQUIREMENTS_PLAN.md); read its
§0 first, because **three of the four answers went against the recommended
default.** Delivered as PR #104 and merged 2026-09-09. **The DTO has a new set
of major changes; they start on a NEW branch in a new session** — do not reopen
`feat/sponsor-requirements`.

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
- **The manager's requirements are editable on an existing job** (reported
  2026-09-09 as "i see no manager skills preference input"). Four things were
  broken at once: input only on the CREATE form while all 23 pilot jobs came
  through BULK; a read-only job page; `additional_requirements` accepted by
  `JobUpdate` but absent from `_UPDATABLE_JOB_COLUMNS`, so PATCH returned 200
  having changed nothing; and no re-extraction, so an edited note kept the
  previous text's extraction. `extract_manager_prompt` is now its own arq task
  — deliberately not a `parse_job` re-run, which would re-derive the posting's
  requirements under a live shortlist and is 'draft'-gated anyway.
- **The shortlist could not finish** (reported 2026-09-09, "Generate shortlist
  has not been producing anything"). `match_evidence_max_tokens` was 2048;
  gpt-oss:20b spent it reasoning and returned empty content, and ADR-029's
  fail-closed meant one starved candidate withheld the whole run. Now 8192 —
  see lesson 7 below for why the measurement took three attempts. The page's
  "briefly unavailable … no action needed" banner now shows the reason the
  worker actually recorded, which was already in the database the whole time.

- **The 18 draft pilot jobs re-parsed under `jd_extract_v2`** (2026-09-09) —
  the other half of the 2026-09-03 request, unblocked by the 900s override.
  Driven through the frontend's own re-parse form (page token + session
  cookie, the way a browser does it — the bare API 401s without the key),
  four at a time to match the worker's `max_jobs`, gb10 checked idle before
  every batch. **18 of 18 parsed, 0 failures, 18 of 18 outbox events
  delivered, 16 minutes end to end** — not the hour budgeted; the JD prompt
  runs 30s–3min at this concurrency. Department filled on 15; the other three
  JDs (AV Technician, Research Chairs Facilitator, Research Contracts Officer)
  state none. Campus on 0 of 18, as predicted. The never-parsed CUPE JD that
  had been failing on `title: missing` parsed first time, and its filename
  title was replaced with the extracted one — titles are now right on 26 of
  26 real rows. The two stale `circuit breaker open` reasons are gone.

- **"Work authorization is not working"** (reported 2026-09-09, the first
  user click on the §O2 control). The frontend log had the whole story:
  `POST /reveal` 200, then `POST /work-authorization` 403 eleven seconds
  later. `resume_reveal` re-rendered the résumé page with three context
  values and none of the four one-shot CSRF tokens, so every audited form on
  a just-revealed page — declaration, withdraw/reinstate, download — carried
  an empty token. Lesson 6 below, in the form that reached a user. One shared
  render helper now, using `csrf.ensure_token` (reuse a live slot, mint an
  empty one) — re-minting would have traded the 403 for a different 403 on
  the next form. **And the second half of the report — "the declaration is a
  critical eval parameter" — is why every shortlist card now carries the
  control** plus a "N of M candidates have no declaration" line inside the
  swapped fragment. The card posts to its own hook-guarded route on the
  reusable page token, not a third one-shot slot: security and review both
  measured a third slot evicting live reveal tokens from 22 cards up against
  the 64-token session budget. That budget already breaks at 33 cards with
  the two existing slots — recorded in ROADMAP §5 "Privacy / access", not
  fixed; nobody has a shortlist that big yet.
- **Blind review is OFF by default** (sponsor, 2026-09-09: *"reverse the blind
  review to be off by default, keep the on switch button"*). DDL default,
  `JobCreate`, the Taleo upsert, the unread setting, the create form; an
  idempotent `ALTER ... SET DEFAULT FALSE` because `CREATE TABLE IF NOT EXISTS`
  reaches no existing deployment; and `scripts/backfill_blind_review_default.py`,
  which flips only jobs nobody toggled (the audit log's own
  `blind_review_toggled` rows are the record of a human choice) and audits
  each flip as a service actor. **Applied on this box: 26 flipped, all 28
  jobs now off, second run proposes nothing.** ADR-004 §4, ADR-006, ADR-014
  and ADR-016 are amended in place.
- **A non-blind job shows the candidate's name** — on the card and on the
  résumé page. The review of the default flip found that a non-blind card
  rendered the literal `None` (the user's own job had shown ten of them since
  17:03) and a non-blind résumé page rendered no identity at all: both paths
  were only ever finished for blind-by-default. The name comes from a
  correlated decrypting subquery (never a JOIN on `resumes`, lesson 4), falls
  back to the filename, never to a pseudonym; the reveal button renders only
  on a blind card.
- **The ranking guard no longer waits for a degraded parse** (the director
  demo, 21:42). One résumé's skills pass returned empty content and fell
  back; ADR-030 never projects such a parse; the projection guard counted it
  as eligible and deferred 20 × 45 s before ranking the other nine. The guard
  now counts only what will be projected, and the shortlist page says how
  many résumés are excluded as degraded. Lesson 9.

**Next, in order.** (The DTO's new set outranks all of these and starts on a
new branch; these are what remains of the first set.)

1. **Department on the two OPEN jobs** — *Associate Director, Finance* (the
   user's ranked job) and *Multimedia Specialist*. Re-parse is draft-gated by
   design (it would re-derive requirements under a live shortlist), so the only
   path is the department/campus form on the job page: type it, or ask. Note
   the UI only renders its "Re-parse JD" button when `failure_reason` is set,
   so a clean draft is re-parseable through the route alone — nobody has asked
   for more than that.
2. **Notifications** (§S7) — `mailhost.sfu.ca:25`, in-app table first.
3. **Candidate CSV** (§S3) — **blocked on a sample export**; ask for one rather
   than guessing the column shape.
4. **Blind review on the ranked list** (§O5) — re-read it against the
   2026-09-09 reversal before building: blind is now opt-in per job.
5. **The skills prompt's budget is at its edge.** `resume_skills_v2` runs at
   the measured 8192 floor and still returned empty content for one real
   résumé at concurrency 4 (15k thinking chars measured). Lesson 7 applies:
   reproduce at the real fan-out before raising it, and raise
   `LLM_TIMEOUT_S` with it.

**Two config duties that outranked all four — both discharged 2026-09-15.**
`.env` now sets `LLM_TIMEOUT_S=900` directly (no longer masked by an
override); `docker-compose.override.yml` is deleted and CAS is on. See
[docs/deploy/sfuai-ca.md](docs/deploy/sfuai-ca.md) for the cutover that did
this and §2 above for what it leaves owed (the off-LAN drive).

**Owed and not yet written: two ADRs** from the work-authorization slice — the
screening decision (it must record *why inference was rejected*) and an ADR-009
amendment for the weight move. The document-download route needs no ADR: it is
one obvious implementation, and the reasoning is in its commit.

**Ten things a future session must not rediscover the hard way:**

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
7. **An LLM token budget measured at concurrency 1 tells you nothing.**
   2026-09-09: the shortlist produced nothing for hours because
   `match_evidence_max_tokens` was 2048 and gpt-oss:20b spent it all reasoning,
   returning empty `content`. **Two separate probes called ONE evidence prompt
   at a time and both PASSED at 2048** — a 5-chunk résumé in 205s, a 15-chunk
   one in 303s. Only at the real fan-out (`match_llm_concurrency = 4`) did 2 of
   4 fail, and *not* the biggest ones: the 15-chunk résumé passed while 12- and
   9-chunk ones failed. A budget near the edge looks intermittent, not broken.
   The committed profile already says this — *"a single uncontended call took
   ~35s while four concurrent ones blew a 300s timeout"* — and it was read past
   twice. **Reproduce at the real concurrency or do not claim a floor.**
   Budget and `LLM_TIMEOUT_S` are coupled: those calls take 219–315s, so
   raising one without the other converts empty responses into timeouts.

   **The structural tail is OPEN and is why nothing caught it.**
   `model_probe_live.py` measures three prompts — `resume_core_v1`,
   `resume_skills_v2`, `jd_extract_v2` — and `shortlist_evidence` is not one of
   them, so the prompt that broke had no measured floor at all. Worse, it
   probes with `think: False` and schema-constrained `format`, a transport the
   application does not use (`llm_ollama_native=False`), so
   `docs/model-profiles/gpt-oss-20b.json` describes calls the product never
   makes and `doctor.sh` reported the box healthy throughout. That is ADR-045's
   recorded "transport gap", still unmeasured. **Any new prompt added to this
   product currently ships with no measured budget.**
8. **A per-card one-shot CSRF token has a budget, and a shortlist can exceed
   it.** `MAX_TOKENS_PER_SESSION = 64`, FIFO-evicted, two slots per card
   (reveal, withdraw), cap 50 cards. Adding a third slot per card passed
   every unit test (they render 1–3 cards) and was measured dead from 22
   cards. The cookie ceiling is pinned, so the cap cannot rise; the résumé
   page's route must not accept the page token, also pinned. Anything new on
   a card uses the page token or a new mechanism, and **any test of a
   per-card control renders 32 and 50 cards** — the unit suite now does.
9. **Two fail-closed decisions can deadlock into a bounded stall.** ADR-030
   never projects a degraded parse; the projection guard (smoke, 2026-08-22)
   waits for every parsed résumé to be projected. Each was right alone;
   together, one degraded résumé cost a 15-minute silent wait in front of a
   director, then ranked nine of ten anyway. **A guard that waits for a
   count must count only what the producer will ever produce.** The general
   question: for every "wait until X catches up", what does X deliberately
   skip?
10. **Reversing a default reaches no existing row, and exposes every path the
    old default hid.** The blind-review flip needed an `ALTER ... SET
    DEFAULT` for deployments that already had the table, a backfill that
    respected the audit trail of who had toggled what, and then two read
    paths that had never been finished for the other value: a card labelled
    `None`, a résumé page with no identity. **When a default flips, walk
    every branch the old value made unreachable** — the tests only ever
    exercised the default.

### 4. Current state

| | |
|---|---|
| `main` | PR #104 squash-merged 2026-09-09 (see `git log -1 main`) — the whole sponsor set |
| Branch in flight | **`feat/complete-build-gaps`** — `feat/candidate-roster-csv` (`main` + #106 TLS/proxy + the e2e/stress-build guide and findings) with three merged lanes closing all five candidate fixes: dropped-regenerate queueing, hiring-manager assignment screen, "Why this rank?" card link, roster credential-suffix match, and zero-requirements JD recovery (Re-parse + description editor). Gates: `make gates-all` on the merged branch: 6341 unit @ 92.08%, 643 integration, ALL GATES GREEN; all five fixes driven by hand on the isolated stack (see the findings doc). Still not pushed, still awaiting the user's own review (see START HERE). |
| Live site | **https://sfuai.ca:8000, CAS on.** [#105](https://github.com/humanaxiom/recruiter-assistant/pull/105) (zero-requirements guard) MERGED 2026-09-15. [#106](https://github.com/humanaxiom/recruiter-assistant/pull/106) (TLS/proxy hardening) OPEN. |
| Pilot box contents | Unchanged since 2026-09-15's cutover — see §2. The isolated stress stack (`-p recruiter-stress`, 28xxx ports) is a **separate**, throwaway copy built for this session's smoke/stress/e2e runs; it does not touch the pilot box's data. |
| Gates, last local run | `verify.sh all` → 6246 unit @ 92.15% + 630 integration, ✅ ALL GATES GREEN — **re-run, do not cite** |
| Smoke / stress / e2e (this session, isolated stack) | `smoke.sh`: **10/10 passed, 901s**, real model. `stress.sh USERS=3 RESUMES_PER_USER=3` (stub): **PASS, 48 samples, 0 errors**. `e2e.sh` (one user, real model, whole guide): **PASS — smoke 10 of 10 in 1046 s, then all 16 driver steps with 0 errors and 0 timeouts (JD parse 265 s, three résumés plus a cover letter 660 s, ranking 215 s); doctor reports only the CAS-off state, which is the isolated stack's design**. |
| ⚠️ Before pushing | The branch history was **rewritten four times** to purge committed candidate PII. A `backup-pre-redact-*` branch still holds the unredacted history — **delete it before any push**, and re-run the 925-token scan in §3 if you rewrite again. |
| Lint paths | `src tests frontend scripts` in **both** the Makefile and `ci.yml`; a test pins them equal |
| Verification | `verify.sh` code · `smoke.sh` screen · `doctor.sh` data · `model-check.sh` before a model swap |
| Postgres | `psql -U app -d recruiter` — there is no `postgres` role |
| LLM hosts | gb10 `100.88.247.106` · spark1 `100.114.185.88` — **both shared** |
| Egress | **ONE carve-out**, ADR-046, `TALEO_ENABLED=false` by default. Nothing else leaves. |

Working end to end: upload → parse → rank → shortlist; JD ingest; blind review,
PII encryption, audited reveal; CAS identity, session role enforcement, CSRF,
auditor viewer; work-authorization screening; the manager's own requirements.

**Verified against the RUNNING product** during this work, not only in tests:

- `smoke.sh` **10 passed in 13m18s** (2026-09-03) — the browser→Flask→API seam,
  first green run in weeks. It needs CAS OFF and FAILS rather than skips, so it
  had not run at all while CAS was on; that is how a 500 on the jobs list
  reached a user.
- The jobs list, rendered from real rows: titles corrected on 25 of 26,
  `resume_count` populating, no 500.
- A real `PATCH` through the deployed API persisting the manager's note
  (scratch job created and removed).
- **The shortlist, end to end on the user's own job (2026-09-09): 10 ranked
  candidates, scores 19–50, `shortlist_state` NULL** — the run that had failed
  every attempt for hours before the evidence-budget fix.
- `doctor.sh`: one finding, `deploy.auth_disabled`, which is the CAS decision
  in §2 surfacing correctly. The `deploy.timeout_below_profile` finding is gone
  **because the override masks it**, not because `.env` was fixed. Re-run
  after the 18 re-parses: same single finding.
- **The jobs list after the re-parses** (2026-09-09), rendered at `/` from
  real rows: Department populated on 22 of 27, `School of Medicine` visible
  on five rows in three spellings — ROADMAP §"Data quality" was right.
- **The work-authorization fix, on the rebuilt frontend** (2026-09-09), with
  write-free probes: the user's shortlist renders 10 card controls on the
  page token and "10 of 10 candidates have no work-authorization
  declaration"; the card route 403s without a token and reaches its 400
  status check with one; the résumé page's own declaration still validates
  after a shortlist visit (403 before). No declaration was written — all 35
  résumés still read `unknown`, and that is now the user's to change.

- **The blind-review reversal and the non-blind name, deployed** (2026-09-09,
  after the demo): column default reads `false`; the demo job's nine cards
  show names, zero reveal forms, nine declaration controls; backfill applied
  and idempotent; `doctor.sh` still one finding (CAS off).
- **The demo job itself**: 9 ranked, scores 32–13, three minutes of ranking
  once the guard stopped waiting. The tenth résumé is degraded and shown as
  such on its own page.

**Still not clicked successfully by a human:** the work-authorization radio
was clicked once on 2026-09-09 and produced the 403 above; nobody has clicked
it since the fix. The manager's requirements box and the department/campus
form have never been driven in a browser. CAS is off, so that is cheap —
`:29500`, no login.

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
- **Blind review is opt-in per job, by the sponsor's word on 2026-09-09.** The
  switch and its audit stay; the audited reveal remains the only path to
  identity on a blind job. Decision 4 of ADR-004 is amended, not reopened.

### 8. Host quirks

No usable Python on this host (only the WindowsApps stub) — use
`./scripts/verify.sh`, never a hand-written `docker run`. `quickstart.ps1` needs
`pwsh` 7; it fails to *parse* under PowerShell 5.1, and `powershell.exe` still
exits 0. Publish unique host ports (29xxx) — many other containers on this
machine collide on stock 5432/8000/5000. Two Claude sessions drive this repo at
once: re-read git and PR state in the same call that commits, pushes, or merges.

**The stack serves the WORKING TREE, not an image.** `docker-compose.yml`
bind-mounts `./core:/app` into api, worker and frontend, with uvicorn
`--reload` and Flask `--debug`. `docker compose up -d --build` pins nothing:
a `git checkout` changes what the box executes and renders within seconds.
On 2026-09-15 a checkout to a `main`-based branch, made while `smoke.sh` was
running against a database the roster branch had written, 500'd every
shortlist list on `ScoreBreakdown extra_forbidden` and failed the run after
24 minutes. **Never switch branches while smoke, doctor, a hand-drive, or a
user is on the stack**; to edit another branch meanwhile, use a `git
worktree`. Hand-drive evidence is only valid for the branch that was checked
out when it was gathered.
