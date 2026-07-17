# Phase 5 — Persist + anonymize + export

**Status:** built and gate-green on branch `feat/phase-5-persist-anonymize-export`, off `main` @
`5945320` (Phase 4d's merge commit, PR #13), tip `b6b1ec7`, 4 commits. **All three merge-blocking gates
green (reviewer APPROVE, security PASS, ranking-evals PASS). NOT yet PR'd, NOT merged** — a PR opens
after a human check-in. CI (`gates-all`, including a live `run_evals.py` re-measurement against Ollama)
has not yet run, since no PR exists.

This sub-phase ships the read/export side of the shortlist + résumé persistence Phase 4d wrote:
`list_for_job`/`get_one`/`export_rows` (+ the pure csv/evidence-csv/json formatters) for shortlists, and
`list_for_job`/`get_one(reveal=False)` for résumés — plus the display-redaction layer
(`core/src/services/redaction.py`) that makes the ADR-006 §4 blind-review contract real code instead of a
recorded risk. Full decisions and residuals: [ADR-011](../adr/011-display-redaction-read-export-boundary.md).

## TDD sequence

Two RED/GREEN pairs — an initial build, then a security-finding regression fix:

| Commit | Label | What it added |
|---|---|---|
| `3e383ff` | `red` | Failing unit + integration tests for `redaction.py` (didn't exist), `errors.py` (didn't exist), the extended `shortlist_service.list_for_job`/`get_one`/`export_rows`/formatters, and `resume_service.list_for_job`/`get_one(reveal=...)`. Fails at collection/import against modules that don't exist yet. |
| `33512c2` | `green(5)` | `src/services/redaction.py` (ported from hris, with the alternation-grouping fix, §5 of ADR-011); `src/errors.py` (`AppError`/`NotFoundError`); the read/export extensions to `shortlist_service.py` and `resume_service.py`. Minimal implementation to turn every RED test green. |
| `8b1597e` | `red` | A security-gate finding, written as a failing regression test FIRST: `resumes.parsed.cover_letter_chunks[].text` was still reachable, unredacted, through a blind `ResumeOut` — raw letterhead PII (name/email/phone in the opening lines of a cover letter) leaking past the ADR-006 §4 boundary this phase exists to close. |
| `b6b1ec7` | `green(5-fix)` | `resume_service._blind_parsed` extended to redact `cover_letter_chunks[].text` with the same `_r()` helper already used for `chunks[].text`/`experience[].bullets[].text`. Mutation-proven: removing the new redaction line re-fails the round-2 regression test. |

Four commits total — the security finding surfaced **after** first green (not caught by the initial RED
suite), which is why this phase has two RED→GREEN cycles instead of one.

## What each new module/function does (see ADR-011 for full rationale)

1. **`core/src/services/redaction.py` (new)** — `redact_text(text, *, name, email, phone, term_map,
   location, redact_locations=False)` (name/email/phone masking + employer/institution relabeling via
   `term_map` + optional foreign-location scrub); `pseudonym(rank)` ("Candidate A/B/…", shared letter
   scheme with `blind_label_map`); `blind_label_map(employers, institutions)` (stable per-résumé
   "Employer A"/"Institution A" labels, deduped case-insensitively, ordered by first appearance);
   `is_foreign_location(location)` (US-state/foreign-country classifier — Canadian and unrecognised
   locations stay visible, matching the "foreign employment history is the bias concern, not a Canadian
   city" framing carried from hris). Ported near-verbatim from hris
   `apps/api/src/api/services/redaction.py`, with one latent bug fixed in the port (ADR-011 §5): the
   name/term alternation is grouped so a middle name-part can't match inside a longer unrelated word.
2. **`core/src/errors.py` (new)** — `AppError` (base, carries `**context`) and `NotFoundError` (404,
   raised by both new `get_one`s on a missing id). Minimal port of hris `apps/api/src/api/errors.py`'s
   base + the one exception class Phase 5's service layer needs; the FastAPI envelope/handlers land with
   routes in Phase 6.
3. **`shortlist_service.list_for_job`/`get_one`** — blind/reveal branch keyed on `jobs.blind_review`;
   under blind, decrypts candidate name/email/phone + the résumé's `parsed` json inside one transaction
   *only* to redact with, pops those `_c_*` columns before the DTO is built, redacts the evidence object
   (`_redact_evidence` — closes the hris cover-letter-evidence gap, ADR-011 §3), and stamps
   `blinded=True`/`display_label=pseudonym(rank)`. `_parse_entry_jsonb` pops the 4d-folded
   `score_structured`/`score_evidence` keys out of `score_breakdown` before `ScoreBreakdown.model_validate`
   (ADR-011 §2 — required to read *any* 4d-written row, not an edge case).
4. **`shortlist_service.export_rows(conn, *, job_id, reveal)`** — flat rows for csv/json export, already
   reveal-applied. `_apply_reveal` (when `reveal=False`) redacts the evidence dict, swaps
   `candidate_name` for the rank pseudonym, and blanks `candidate_email`/`candidate_phone` — using the
   real name/labels/location derived from the row **before** the swap. `original_filename` is
   deliberately **not** touched here (ADR-011's open residual).
5. **`shortlist_csv`/`shortlist_evidence_csv`/`shortlist_json`** — pure formatters, no DB access, no
   redaction of their own; they operate on rows `export_rows` already reveal-applied. `shortlist_csv` is
   the one recruiter-facing row-per-candidate file (score breakdown, skill gap summary, evidence
   completeness); `shortlist_evidence_csv` is one row per (candidate, requirement) — the audit detail;
   `shortlist_json` is the full nested payload, Decimal/datetime-normalised.
6. **`resume_service.list_for_job`** — blind → `candidate_name=None` (not a pseudonym: a résumé list
   carries no rank to build one from, unlike the shortlist — ADR-011 §6).
7. **`resume_service.get_one(reveal=False)`** — blind → `CandidateInfo(name=None, email=None,
   phone=None, location=<masked if foreign>)`, `cover_letter_text=None`, `cover_letter_parsed=None`, and
   `parsed=_blind_parsed(...)` — which redacts `summary`/`chunks[].text`/`cover_letter_chunks[].text`
   (added in the security-fix commit)/`experience[].bullets[].text`, relabels
   `experience[].company`/`education[].institution`, and nulls `education[].year` (a graduation-year age
   proxy).

## Reviewer, security, ranking-evals — verdicts

**Three merge-blocking gate verdicts on GREEN `b6b1ec7`:** reviewer **APPROVE** (5 mutation obligations
fired — every redaction call site mutated out and caught by a test, per ADR-011 §1), security **PASS**
(after the `cover_letter_chunks` fix — the branch's one HIGH finding, closed in the second RED→GREEN
cycle), ranking-evals **PASS** (scoring code byte-unchanged: `stages.py`/`orchestrator.py` untouched by
this phase — Phase 5 is entirely read/export/redaction, no ranking logic; CI's `gates-all` will re-measure
`run_evals.py` live on the PR).

The reviewer's sign-off rests on the redaction-boundary being provably real, not merely intended — the
guard set (ADR-011 §1):

| # | Guard | Would fail if... | Test class |
|---|---|---|---|
| 1 | Blind `ResumeOut` never contains the candidate's real name/email/phone byte-sequence anywhere in its serialized form | any redaction call site was skipped or a new PII-bearing field was added without redacting it | black-box byte-scan test on the full serialized DTO, not per-field |
| 2 | Blind `ShortlistEntry` never contains the candidate's real name/email/phone anywhere in its serialized form (including inside evidence quotes) | `_redact_evidence` was skipped, or only partially applied | black-box byte-scan test |
| 3 | Blind export dict/CSV/JSON never contains the candidate's real name/email/phone | `_apply_reveal`/`_redact_evidence_dict` was skipped for `reveal=False` | black-box byte-scan test |
| 4 | `cover_letter_evidence[].evidence`/`overall_motivation` are redacted, not just `requirements[].evidence`/`overall_summary` | the hris `_redact_evidence` gap was ported verbatim | targeted unit test feeding a cover-letter quote containing the candidate's name |
| 5 | `cover_letter_chunks[].text` is redacted under blind `ResumeOut.parsed` | the security-fix commit's guard regressed | the round-2 regression test (`8b1597e` → `b6b1ec7`), mutation-proven by removing the redaction line |

5 of 5 verified — every guard is a live, currently-passing test against the real implementation (the
integration-tagged ones, `test_shortlist_read_export_pg.py`/`test_resume_read_pg.py`, against a real
Postgres, including the `ScoreBreakdown` fold-pop round trip against the real jsonb codec).

## Final gate state — HEAD `b6b1ec7`

- Offline: ruff / black / `mypy --strict` clean.
- **2024 unit tests @ 91.80% coverage** (up from 1947 on `main` post-4d — Phase 4d is not yet merged to
  `main`, so this delta is measured against this branch's own base).
- Integration tests green vs a real Postgres: `test_shortlist_read_export_pg.py`,
  `test_resume_read_pg.py`.
- **All three merge-blocking gates green:** reviewer APPROVE (guard table above), security PASS (empty
  findings table on the final HEAD, after the `cover_letter_chunks` fix), ranking-evals PASS (no scoring
  code touched — `stages.py`/`orchestrator.py` byte-unchanged).

## Accepted-for-v1 residuals (see ADR-011 for full detail — not restated here)

- **`original_filename` shown verbatim under blind — an OPEN decision, not resolved this phase.** A
  résumé uploaded as `First_Last_Resume.pdf` is a real blind de-anonymization vector, surfaced in both
  `resume_service.get_one`/`list_for_job` and `shortlist_csv`'s `resume_file` column. Flagged for a human:
  accept, or normalize the filename under blind review.
- `candidate_email_hash` returned under blind — accepted, one-way sha256, plaintext-by-design for
  subject-access lookup, symmetric with the at-rest posture.
- CSV formula/injection (unneutralized leading `=`/`+`/`-`/`@` in exported cells) — accepted for v1
  (offline, trusted-recruiter export), one-line fix noted for a future hardening pass.
- Shortlist `evidence={}` ambiguity (ADR-010 §2, carried) — the read layer cannot distinguish "never
  evidence-scored" from "scored, found nothing" at the raw jsonb level; still accepted, first touched by
  this phase's read code but not fixed here.

## Carried forward, still unresolved

- **`score_education` ignores `jd.education.fields`** (ADR-009 §7, restated ADR-010 §5) — Phase 5 touches
  no scoring code, so this is untouched again.
- **`original_filename` under blind** (new this phase, ADR-011) — needs a human decision before Phase 6
  or any later phase builds a route that surfaces it.
