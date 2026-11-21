# Session Handoff — recruiter-assistant

**Eight items. Hard cap.** A ninth means one of these is no longer relevant —
delete it. History goes to [docs/archive/](docs/archive/), never inline. Plan of
record: [docs/ROADMAP.md](docs/ROADMAP.md). The previous file, including the
lessons lists verbatim, is archived at
[docs/archive/HANDOFF-2026-08-24-to-11-21.md](docs/archive/HANDOFF-2026-08-24-to-11-21.md).

---

### 1. Start here — `feat/taleo-inapp-split` (worktree `C:\repos\ra-taleo-split`)

**State, 2026-11-21.** Built on this branch, not merged, PR not yet opened.

**Done:**
- **In-app split of a combined Taleo export.** Upload → model segmentation
  (heuristic fallback) → recruiter review (correct, accept or discard) → each
  accepted applicant is ingested as a résumé file and a cover-letter file,
  through the same `ingest_files` path as ordinary uploads. Pure logic:
  `core/src/services/taleo_split_service.py`. State machine:
  `split_request_service.py`. Routes: `core/src/api/routes/resume_splits.py`.
  Decision record: ADR-017, amendment 2026-11-21.
- **Résumé export.** A background arq job builds a ZIP with `as-is/` and
  `anonymized/`. The anonymized copy is a **rebuilt, text-only PDF**, not an
  in-place redaction, because in-place redaction leaked through images, links,
  annotations, XMP, layers and ActualText. Pipeline: extract → redact → verify
  → render → re-extract → verify; it refuses scanned pages, a missing name
  vouch, other scripts and letters it cannot draw. Blind-review jobs omit
  `as-is/`. Routes: `POST /jobs/{job_id}/resumes/exports` (202),
  `GET /jobs/{job_id}/resumes/exports/latest`, `GET /resume-exports/{id}`,
  `POST /resume-exports/{id}/download` (requester only, one-shot CSRF token,
  `no-store`, audit row with counts only). Caps: TTL 60 min, 500 applicants,
  20 pages per file; over the cap, the manifest records how many were left
  out. Hourly `expire_resume_exports` cron. Doctor invariants
  `pg.export_rows_retain_blob` and `pg.export_ready_past_expiry`.
- Nothing here egresses. `TALEO_ENABLED` is unrelated.

**Hand-over results:**
- `smoke.sh`: **10 passed** against the isolated `recruiter-stress` stack in
  stub-LLM mode. The real-model smoke run **did not complete**: it timed out at
  900 s on parse while gb10 was contended.
- `doctor.sh`: only `deploy.auth_disabled`, expected with CAS off.
- By-hand drive on the real model: split, accept, export and download all
  worked, with no leaks and no 500s.

**Not done:** F1/F2 (ROADMAP §5, under the split item), and the anonymizer and
export residuals recorded beside it.

**Next step, exactly:** open the PR for `feat/taleo-inapp-split`; then features
3 (interview-question generator), 2, 4, 5.

**Before this reaches the pilot box (CAS on):** the rule in §3 applies. `smoke.sh`
cannot run there, so the obligation is `doctor.sh` plus a by-hand drive signed
in as a real principal.

### 2. The objective

The four people on the pilot box can do their real hiring work in it, and what
they hit gets fixed before anything they haven't hit gets built. User-sourced
work outranks self-sourced work. ROADMAP §0 still carries the 2026-09-09 pause on
feature work pending hardware; the next step in §1 is the user's own
instruction, and §0 should be updated when the pause is lifted.

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
- The stack serves the **working tree**, not an image (see §8).

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
is on the stack. This branch lives in its own `git worktree`
(`C:\repos\ra-taleo-split`); keep it that way.
