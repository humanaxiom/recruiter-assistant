# Session Handoff — recruiter-assistant

**Eight items. Hard cap.** A ninth means one of these is no longer relevant —
delete it. History goes to [docs/archive/](docs/archive/), never inline. Plan of
record: [docs/ROADMAP.md](docs/ROADMAP.md). The previous file, including the
lessons lists verbatim, is archived at
[docs/archive/HANDOFF-2026-08-24-to-11-21.md](docs/archive/HANDOFF-2026-08-24-to-11-21.md).

---

### 1. Start here — account hand-over, 2026-11-24

The product owner moved this project to a new Claude account after this
iteration. **New session: read in this order.**

1. [CLAUDE.md](CLAUDE.md), then this file, then
   [docs/handover/DECISION-LOG.md](docs/handover/DECISION-LOG.md): every
   product-owner decision and correction that could be recovered. **Search it
   before starting anything.** If a decision there covers your work, follow it;
   don't re-ask and don't silently reverse it.
2. [docs/handover/OPERATOR-NOTES.md](docs/handover/OPERATOR-NOTES.md): standing
   orders, the two stacks, how to deploy, host gotchas. This was previously in
   private Claude memory that may not have carried over.
3. [docs/history/ORAL-HISTORY.md](docs/history/ORAL-HISTORY.md): how and why
   the project got here.
4. [docs/practice/fleet-project-playbook.md](docs/practice/fleet-project-playbook.md):
   how the product owner runs work through the agent fleet.
5. **Setting up a new box?** Use
   [docs/deploy/FRESH-INSTALL.md](docs/deploy/FRESH-INSTALL.md). It starts
   from an empty machine and covers a new LLM host (`model-check.sh` before
   users) and the offline release bundle.

**2026-11-25: the product owner is moving to the new box and the Teams account
now.** The exact sequence is
[FRESH-INSTALL §0a](docs/deploy/FRESH-INSTALL.md#0a-the-short-path-in-order).
Leave sfuai.ca running on the old box until the new box passes the signed-in
drive (§0a step 6), so the managers' testing isn't interrupted. Development
happens **on the new box**, in a worktree, never in its live tree.

**Fixed 2026-11-25 (`fix/cas-overlay-drift`):** login from `localhost:29500`
timed out at sfuai.ca, the third recurrence. The 11-24 deploys used a plain
`docker compose up`, which dropped `CAS_LOCAL_*` (they lived only in
`compose.cas.yml`).
- The defaults now also live in `docker-compose.yml`.
- `doctor.sh` fails with `deploy.cas_local_origin_unset` when they're missing.
- Every `.env` should carry `COMPOSE_FILE=docker-compose.yml;compose.cas.yml`.
  The old box's `.env` now does.
- **After any deploy, check both origins** (DECISION-LOG §5).

**State, 2026-11-24.** External access to sfuai.ca was confirmed by the product
owner the same day.

- **`main` = `eb39d03`.** Merged this iteration:
  - #112: in-app Taleo split and anonymised résumé export.
  - #113: interview-question generator.
  - #114: human decision record.
  - #115: the worker now loads `.env`, so `TALEO_ENABLED` reaches the Taleo
    import.
- **The pilot box runs exactly `main`** (tree-identical; deploy recipe in
  OPERATOR-NOTES §3).
  - Clean slate 2026-11-24: all hiring data and the audit log wiped at the
    product owner's request; 2 accounts kept.
  - Taleo import ran once by hand: 32 SFU postings landed as drafts and are
    parsing (~5.5 min each, 4 at a time).
  - `doctor.sh` is healthy.
- **Managers are testing the core functions now.** Don't change what they see
  mid-test: any UI change ships behind a default-off flag.

**Next, in the product owner's order:**

1. **The job-page redesign (Option A) is BUILT on `feat/job-page-v2`.** S1–S5
   and review are done, and it is awaiting **H3** (push, PR, merge), which
   needs the product owner's consent. Contract:
   [S-contract.md](docs/plans/2026-11-job-page-redesign/S-contract.md); actuals
   are in README §11.
   - **Rollout:** `JOB_PAGE_V2` is off by default. A per-user preview is set by
     the `JOB_PAGE_V2_USERS` allowlist or the `users.job_page_v2` column. An
     admin toggles the column on the Users screen. With the flag off, v1 is
     byte-identical (golden test).
   - **Next:**
     - **H3.**
     - Deploy the merge to the pilot. The DDL adds
       `users.job_page_v2 … DEFAULT false` on startup.
     - **H4:** turn the preview on for one manager.
     - The **HR demo** runs from the local stress stack (`recruiter-stress`,
       http://localhost:28500, CAS off, v2 on). Refresh it to branch HEAD and
       give it realistic data first.
   - **`smoke.sh` now requires `SMOKE_EXPECT_JOB_PAGE=v1|v2`** and refuses to
     run without it.
   - **CSRF, measured:** the page has 1 one-shot slot (the export download,
     minted on the Candidates tab only), not 15. A wrong page token is a 403.
2. Feature 4, "Ask the pool"; then feature 5, the department picklist. Neither
   has been started.
3. Owed, not built:
   - S4 model-check profile for `interview_questions_v1`, once gb10 is
     uncontended.
   - A signed-in drive on the pilot box of the decision record and interview
     questions. Every check of those on the pilot box so far ran without a
     sign-in; the managers' UAT is the first real one.
   - The Dependabot alert on `main` (high, `black`, GitHub Action
     `required-version`).

### 2. The objective

The four people on the pilot box can do their real hiring work in it, and what
they hit gets fixed before anything they haven't hit gets built. User-sourced
work outranks self-sourced work. The 2026-09-09 hardware pause in ROADMAP §0 was
lifted in practice: the product owner ordered features 1–5 in November. Update
§0 to say so the next time ROADMAP is edited.

### 3. The pilot box and deployment

- **CAS is on.** Served at `https://sfuai.ca:8000` through a second, non-git
  compose project (`C:\repos\web`, container `sfuai-web`, nginx) that
  terminates TLS. Runbook: [docs/deploy/sfuai-ca.md](docs/deploy/sfuai-ca.md).
  **Never turn CAS off on that box.**
- **`smoke.sh` cannot run on the pilot box.** It requires CAS off and fails
  rather than skips. The obligation there is `doctor.sh` plus a by-hand drive
  from **off the LAN** (hairpin NAT blocks an on-LAN client), signed in as a
  real principal. The off-LAN drive owed for the 2026-09-15 cutover was still open when this
file was last written; confirm whether it has been run.
- **Rotating `FLASK_SECRET_KEY` logs everyone out.** Tell the four users first.
- **Owed by the DTO, not by a session:** sign in with SFU CAS from outside the
  LAN and confirm a first-time colleague lands on pending-access; make one real
  write on the live site and confirm it does not 403 behind the proxy; assign a
  hiring manager from the job page and confirm they see only their jobs.
- The stack serves the **working tree**, not an image (see §8). The live tree
  sits on local branch `fix/cas-local-origin`, whose tree equals `main`. Deploy
  with the tree-identical commit recipe in OPERATOR-NOTES §3; never check out a
  branch there.
- **Upload only appears on an OPEN job**, for admins and recruiters. On a box
  whose jobs are all parsing drafts, the product owner reasonably concluded upload
  had been deleted. When handing a box over, walk the empty-state path first.

### 4. Lessons the roster and pilot work paid for

**Four things a future session must not rediscover:**

1. **The workflow is Upload → Parse → ROSTER → Rank.** `candidate_email_hash`
   and `candidate_name` are NULL until the parse extracts them, and they are
   the only two reconciliation keys. A roster uploaded before parsing finishes
   matches **0 of 315** and says nothing about why. Recorded, not fixed.
2. **Parsing runs ~29 résumés/hour** (batches of 4, ~8 min each, measured).
   75 résumés ≈ 2.5 h; a full 315-candidate requisition ≈ **11 hours**. This is
   a hardware bound. One parse is 2–3 sequential LLM calls, so `max_jobs=4`
   puts up to 12 concurrent requests on one GPU. "Generate shortlist" queues
   behind every parse; a request can return 200 and then sit for hours. Do not
   just raise `max_jobs` (lesson 7).
3. **The CSV is the full export; the résumés are a subset.** ~240 unmatched CSV
   rows are the normal state. Résumé-side coverage is the number that matters.
4. **`About This Role.txt` arrived as Windows-1252, not UTF-8.** Convert with
   `iconv -f WINDOWS-1252 -t UTF-8` before ingest.

**Gate failures that were green for a whole run:**

- The roster upload 503'd on first real use: `pgp_sym_decrypt` ran with no
  transaction, so no PII key. Unit tests mocked `pii_service` wholesale.
- A mutant flipping `if resolved_wa != "unknown":` to `if True:` survived all
  unit tests: a blank CSV cell would overwrite a recruiter's audited decision.

**Ten things a future session must not rediscover the hard way:**

1. **`pipeline_meta.weights` is a historical stamp and the read path validates
   it UNCAUGHT.** Adding a weight field with a non-zero default makes every
   pre-existing stamp fail its sum validator. There is a `mode="before"` shim
   and a regression test; do not remove either.
2. **A new weight can be declared, validated, and applied by nothing.**
   `test_top_blend_is_fully_applied.py` asserts a perfect candidate scores
   exactly 1.0. Do not delete it to "simplify".
3. **The eval corpus's `motivation` control was inverted** into
   `[cover_letter_neutrality]`, not deleted. The threshold key set is a
   three-way contract (`thresholds.toml`, `run_evals.py`,
   `.claude/agents/ranking-evals.md`), plus `labels.json` and `_THRESHOLD_KEYS`.
4. **Do not JOIN `resumes` into the shortlist read.** `_ENTRY_COLS` selects bare
   `id`/`job_id`, which `resumes` also has, so a join makes them ambiguous. The
   unit suite cannot see this; it asserts on SQL as a string.
5. **An LLM extraction can be written to a blob and to no column.** The general
   question for any new extracted field: which column does it land in, and what
   happens on the second parse?
6. **A Jinja template can render a field the DTO does not have, silently.**
   Undefined renders as an empty string. A frontend is a BFF: every timestamp is
   a **string**, so `{{ x.strftime(...) }}` 500s the page. Use the `| day`
   filter; never `.strftime`. Do not write a fixture dict for a page test; build
   the DTO and `model_dump(mode="json")` it.
7. **An LLM token budget measured at concurrency 1 tells you nothing.** Two
   probes at one call each passed at 2048; at the real fan-out, 2 of 4 failed.
   Reproduce at the real concurrency or do not claim a floor. Budget and
   `LLM_TIMEOUT_S` are coupled. Any new prompt currently ships with no measured
   budget unless `model-check.sh` covers it.
8. **A per-card one-shot CSRF token has a budget, and a shortlist can exceed
   it.** `MAX_TOKENS_PER_SESSION = 64`, FIFO-evicted. Anything new on a card
   uses the page token or a new mechanism; any test of a per-card control
   renders 32 and 50 cards.
9. **Two fail-closed decisions can deadlock into a bounded stall.** A guard that
   waits for a count must count only what the producer will ever produce.
10. **Reversing a default reaches no existing row, and exposes every path the
    old default hid.** When a default flips, walk every branch the old value made
    unreachable.

**Remaining, recorded not fixed:** the parse-ordering message; the report's
CSV-side emphasis; an unbounded `_JOB_RESUMES_SQL` fetch. Cover-only applicants
are now disclosed by the in-app split (ADR-017 amendment 2026-11-21). Whether the
CLI splitter (`scripts/split-taleo.sh`) still shares that gap was not re-checked when this file was written.

### 5. Never diagnose the model on a contended peer

Check `GET /api/ps` first, and again during a long run. Both attempts to measure
ADR-045's transport gap were destroyed mid-flight by a foreign model landing on
gb10. A contended box once produced a confidently wrong retraction of a correct
fix.

The token floor is per-**prompt**, not per-model. `REASONING_JSON_MIN_TOKENS =
8192` is right for résumé/JD extraction and wrong as a universal. Do not "fix" a
small budget on sight, and do not inherit a literal into a new call site;
measure it.

### 6. PII

`fixtures/` (117 files, 99 MiB of real résumés) was pushed to a PUBLIC repo by a
`git add -A`. The branch was deleted and history rewritten, but deleting a
branch did **not** stop GitHub serving the data (tested). Only a **GitHub
Support purge** removes it. Both `humanaxiom/` and `sfu-aria/` are PUBLIC.
Still open; see ROADMAP §5 Privacy / access.

**Never `git add -A` in this repo.** `fixtures/` is untracked by design. Both
harnesses hard-fail rather than skip without it.

A `backup-pre-redact-*` branch, if it still exists, holds unredacted history
from the candidate-roster work. Delete it before any push (`git branch`).

Real candidate PII must never be committed to docs. Describe a defect, never a
person; redact identifiers out of any quoted report.

### 7. Settled — do not re-litigate

- **ADR-045's transport gap.** Direction chosen (schema-constrained decoding,
  portable to vLLM `guided_json`); no measurement supports it yet. Do not treat
  the decision as data.
- **D1 and D2 are answered and shipped** (audited reveal of withdrawal reasons;
  every keyed read needs a real principal).
- **The A7 defect taxonomy is closed at 21 instances and is not to be extended.**
  Fix defects users hit; do not audit for defects nobody has hit.
- **Blind review is opt-in per job**, by the sponsor's word on 2026-09-09. The
  switch and its audit stay; the audited reveal remains the only path to
  identity on a blind job.
- **Every export is anonymized in the old shortlist CSV**, including on
  non-blind jobs (design; ROADMAP §5). The résumé ZIP export is a separate path
  with its own anonymizer (ADR-017 amendment; ROADMAP §5 anonymizer residuals).

### 8. Host quirks

No usable Python on this host (only the WindowsApps stub). Use
`./scripts/verify.sh`, never a hand-written `docker run`. `quickstart.ps1` needs
`pwsh` 7; it fails to *parse* under PowerShell 5.1, and `powershell.exe` still
exits 0. Publish unique host ports (29xxx). Two Claude sessions drive this repo
at once: re-read git and PR state in the same call that commits, pushes or
merges.

**The stack serves the WORKING TREE, not an image.** `docker-compose.yml`
bind-mounts `./core:/app` into api, worker and frontend, with uvicorn `--reload`
and Flask `--debug`. A `git checkout` changes what the box executes and renders
within seconds. Never switch branches while smoke, doctor, a hand-drive or a user
is on the stack. Each work branch lives in its own `git worktree` (this one is
`C:\repos\ra-interview-q`); keep it that way.
