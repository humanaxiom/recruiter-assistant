# ADR-017: Bulk ingest — per-résumé cover-letter pairing + bulk JD upload (FU-3)

**Status:** Accepted
**Date:** 2026-07-18

## Context

Post-v1 user request (2026-07-18): recruiters need to ingest candidates and jobs **in bulk**, offline.
Concretely: (1) upload many résumés at once, each paired with its OWN cover letter (a cover letter is an
optional "intention/motivation" bonus, not shared across the batch); (2) upload many job descriptions at
once, one job per file; (3) navigate candidate↔job both ways; and (4) fix a UX defect found in live
testing where the shortlist "Generating…" poll never stopped. This is the **offline** half of the old
"connectors" concept — the Taleo *job-source scraper* remains separately deferred (ADR-012 §2).

Prior art was ported from `C:\repos\hris\apps\api\src\api\services\bulk_ingest_service.py`
(`pair_applicants`/`_classify`/`parse_pairing_manifest`/`parse_csv_manifest`/`title_from_filename`),
adapted to this repo's `(filename, bytes)` upload representation and its hardened
`zip_upload.expand_zip_entries` (rather than porting hris's weaker `expand_archive`).

## Decision

Delivered as five independently-gated slices on `feat/fu3-bulk-ingest`:

1. **Shortlist-poll UX fix.** The `shortlist_cards` poll now carries a server-clamped `attempt` counter and
   stops at `_MAX_SHORTLIST_POLL_ATTEMPTS` (~20 min) with a give-up message; the **Generate button is
   disabled until ≥1 résumé is `parsed`** (the primary fix — ranking a job with no parsed résumé was what
   produced the endless poll). A parse-time hint ("~1–2 min per large PDF on the local model") was added so
   the inherent LLM latency isn't mistaken for a hang.
2. **Per-résumé cover-letter pairing (filename convention).** New pure, I/O-free
   `core/src/services/bulk_ingest_service.py` (`pair_applicants`/`_classify`/`ApplicantFiles`/
   `PairingResult`). Résumé/cover files are paired by suffix convention (`_resume`/`_cv` ↔
   `_cover_letter`/`_coverletter`/`_cover_note`/`_cover`, longest-first, case-insensitive). Each résumé is
   stored with its OWN `cover_letter_blob_key`. `resume_service.upload_resumes` grew additive optional
   `cover_letter_map`/`warnings_map` params (old callers untouched). A post-upload **results summary**
   ("N accepted (M with a cover letter), K duplicate, J rejected") is shown.
3. **Manifest-driven pairing.** `parse_pairing_manifest` + `ManifestError` (an `AppError`, 422). A
   `pairing_manifest` upload field maps résumé→cover by name; the manifest takes precedence, and files it
   doesn't name fall back to convention.
4. **Bulk JD upload.** `POST /jobs/bulk` → `job_service.create_jobs_bulk`: one job per file (loose or
   `.zip`), optional CSV metadata manifest (`parse_csv_manifest`), with per-file resilience and dedup.
5. **Reverse-match UI (candidate→jobs).** `trigger_reverse_match` + a POST-only "Find matching jobs"
   button + a bounded results poll; rows link to job detail — completing the many-to-many navigation
   (job→candidates already existed via the shortlist).

### Load-bearing decisions

- **Manifest is its own field, never zipped.** `expand_zip_entries`'s allowlist has no `json`/`csv`, so a
  manifest zipped with the résumés trips the allowlist and the whole zip 400s with guidance to upload it
  separately. This keeps the zip threat surface unchanged rather than widening it for a manifest.
- **`expand_zip_entries(allowed_extensions=…)` is an ADDITIVE kwarg.** Default preserves the résumé
  allowlist `{pdf,docx,rtf,txt}` exactly (résumé call site untouched); bulk-JD passes `{pdf,docx,txt,json}`
  through the SAME hardened expander (no second/weaker zip impl).
- **Ambiguity is a 422, never a silent choice.** Supplying BOTH per-résumé pairing AND the singular
  batch `cover_letter_file`/`cover_letter_text` → 422. A plain no-suffix upload classifies every file as a
  résumé with no cover → empty map → the singular path is fully backward-compatible.
- **Nothing is silently dropped.** An orphan cover-named file demotes to a standalone résumé with a
  (static-English) note; a manifest-named-but-absent résumé/cover surfaces as a `rejected` row / note.
- **Bulk-JD batch resilience.** A <50-char extracted JD → `outcome="failed"` (not a 422 aborting the
  batch); a manifest field that fails validation → `failed`; in-batch dedup stops two identical JDs in one
  request from both inserting.
- **Bounded polls + POST-only triggers.** Both the shortlist and reverse-match polls clamp `attempt`
  server-side and stop at the cap; the reverse-match trigger is POST-only (a side-effecting enqueue must
  not be a prefetchable GET).

### New convention: idempotent `ALTER TABLE`

Jobs gained a `description_sha256 TEXT` dedup column. Because `CREATE TABLE IF NOT EXISTS` is a **no-op**
against an already-existing dev/CI Postgres volume, the column is added in BOTH the `CREATE TABLE jobs`
block (fresh DBs) AND a separate idempotent `ALTER TABLE jobs ADD COLUMN IF NOT EXISTS description_sha256
TEXT` (existing volumes), plus a partial dedup index. **This is the port's first use of `ALTER`** — a new
convention for adding columns to an existing table without a migration framework.

## Consequences

- Recruiters can bulk-load candidates (with per-applicant cover letters via convention or manifest) and
  bulk-create jobs, offline. Live-verified against the `hris/fixtures/llm_split` sample PDFs: the
  `004_ayomide_abass` résumé+cover pair matched by convention ("2 accepted (1 with a cover letter)"), a
  manifest paired files by name, and 3 JD files → 2 jobs created + 1 deduped.
- Gates: **reviewer APPROVE, security PASS, ranking-evals PASS** (scoring code byte-identical to `main`;
  corpus green: precision@5=1.0, evidence 1.0, 0 PII leaks, exact determinism). ruff/black/mypy --strict
  clean; **~2527 unit tests @ 91.36%**. The one merge-blocking-adjacent finding (both gates flagged it) —
  the `/jobs/bulk` route lacked the résumé route's file-count cap — was fixed (reject on count before any
  body is read).

### Accepted residuals

- **Global job dedup.** `description_sha256` dedup is cross-job (jobs have no parent aggregate to scope it
  to, unlike résumés' per-`(job_id, sha256)`). A byte-identical JD anywhere → `duplicate`. Deliberate.
- **Manifest-in-zip is rejected, not merged.** By design (see above) — the recruiter must upload the
  manifest as its own field.
- **Reverse-match shows real job titles (no redaction).** Intentional (ADR-012 §4) — the caller owns the
  résumé and jobs aren't candidate PII. No résumé PII is on that path.
- **Consent stays batch-level** (one `consent_acknowledged` per upload request), unchanged from Phase 6 —
  per-résumé consent was not requested.

## Amendment 2026-07-29 — separator-agnostic pairing + ReDoS guard (branch `fix/cover-letter-pairing-separators`)

The filename-convention pairing (decision 2) originally recognized only **underscore**-joined suffixes
(`str.endswith` on `_cover_letter`/`_cover`/`_resume`/`_cv`). Real-world uploads use spaces or dashes —
`Jane Smith Cover Letter.pdf`, `jane-cover-letter.pdf` — which were NOT recognized, so a cover letter in a
zip was silently demoted to a standalone résumé and parsed **as a résumé** (the "resumes parse but not
cover letters" bug). Fixed: `_classify` now treats **space / dash / underscore as equivalent separators**
(case-insensitive) via two anchored regexes, with a normalized pairing `base` so a résumé and its cover
share a key regardless of separator style. The false-hit guards are preserved (a real separator is required
before the suffix, so `discover.pdf` stays a résumé; a non-empty name is required, so a bare `Cover.pdf` /
leading-separator stem stays a résumé).

**Security (ReDoS).** The suffix regexes have two adjacent ambiguous quantifiers → O(n²) backtracking on a
pathological, attacker-controllable filename (zip entry names can be tens of KB), and pairing runs
synchronously inside the async upload route. `_classify` now **caps the stem at 256 chars** before the
regexes (over-length names short-circuit to a plain résumé; no real name is that long) — restoring linear
behaviour. Gates green: reviewer APPROVE, security PASS, `./scripts/verify.sh all` = 3977 unit @ 92.64% +
422 integration. Scoring/ranking code untouched (ranking-evals N/A).

## Amendment 2026-09-09 — the candidate roster CSV, and reconciling it to résumés (branch `feat/candidate-roster-csv`)

Sponsor requirement §S3/I1 (`SPONSOR_REQUIREMENTS_PLAN.md`) adds a second
producer for this ADR's existing consumer: a Taleo **"All Candidates"** export
carrying, per applicant, a work-authorization prescreen answer and APSA/CUPE
internal-employee flags. This is not a new ingest path — it writes onto
résumés that the pairing machinery above already created.

### What the real export changed about the plan

§S3 was written while the file's shape was unknown, and was **wrong in a way
that mattered**: it instructed the implementer to *"match on attachment
filename first (deterministic)"*. The sponsor's real 315-row export has **no
attachment-filename column** — `Resume` is blank on every row — so that
strategy could not be implemented at all. The plan has been corrected in
place; this amendment records the strategy that replaced it.

### Decision — match on email hash, then on a normalised name, and refuse rather than guess

**1. Email hash first.** `pii.email_hash` against `resumes.candidate_email_hash`,
scoped to the job. Pure, deterministic, needs no decryption. **Measured before
being chosen**: of 21 résumés split out of one real combined PDF, **19 matched
by exact email with zero false matches**; the two misses carried no email
anywhere in their text.

**2. Normalised-name fallback**, only for rows email did not resolve. The name
is lower-cased, **NFKD-folded with combining marks dropped**, split on any
non-letter character, and compared as an **order-invariant token set** — the
CSV writes `"Last, First"` while a parsed résumé does not, so a set comparison
is symmetric and privileges neither convention.

The accent fold is not defensive coding; it came from the data. **The two
sides of this comparison are encoded differently**: Taleo ASCII-folds its
export (zero of 315 rows carry a non-ASCII byte) while the résumé side is
parsed from the candidate's own PDF and keeps its diacritics. The delivered
bundle contains exactly that pair: an ASCII-folded surname in the CSV against
the same surname carrying an acute accent on the résumé. Unfolded,
`[^A-Za-z]+` treats a character like `í` as a **separator** and shatters such
a surname into two meaningless fragments, so one name shares no token with
itself.
**No unit test would have produced this pair**, because a fixture author
writes the same name on both sides of a match.

**2026-09-17 addendum — a bounded credential-suffix strip.** A résumé parsed
as "First Last, CSM" (the candidate's own signature block) failed to match a
Taleo row spelled "Last, First" with no email to fall back on, because the
extra `csm` token broke the strict set-equality comparison above. The fix
adds exactly one narrow rule, ahead of that comparison, with three guards
meant to make two spellings of one name MORE likely to be judged equal
without introducing a false POSITIVE — a match between two genuinely
different people. **It is not purely one-directional, though**: the strip
can also break a match strict equality would have made, in the narrow case
where a candidate's own given name or initials happen to equal a credential
token ("Del Rosario, Md" is a real "Last, First" name, and stripping "md"
from its tail would stop it matching a résumé spelled "Md Del Rosario").
That is why the vocabulary excludes short tokens that double as common given
names/initials (MD, JD, RN) rather than including every real credential
abbreviation — the closed vocabulary is a trade-off against that failure
mode, not a one-way ratchet: (1) **tail-only** — the ORIGINAL string is split
on its last comma, and only a token after that comma is ever a stripping
candidate, so a credential *before* the last comma is left alone; (2)
**closed vocabulary** — a tail token, or the tail's letters concatenated (so
a punctuated "P.Eng." is still recognised as one credential), must exactly
match a fixed, spelled-out list (CSM, PMP, CPA, CFA, MBA, PHD, PENG, CHRP,
CPHR, CISSP, PMIACP, MSC, BSC, BSW, MSW, LLB, CMA, CGA, SHRM, GPHR, ITIL,
CCNA, MCSE) — CA/BA/MA/MD/JD/RN are deliberately excluded because they are
real surnames/given-names/initials, so nothing is ever dropped merely for
looking short; (3) **two-token floor** — a tail is stripped only if ≥2
tokens survive overall, so "Solo, Pmp" keeps its credential rather than
collapsing to the single bare token "solo". Ambiguity elsewhere in the
matching pipeline still refuses exactly as before.

**3. The comparison stays strict set equality** — deliberately, and at a known
cost. A résumé carrying a middle name and a second surname (four tokens) does
not match a two-token CSV cell. Relaxing to a subset test
would make the fallback markedly more useful, and would also let `Kim, Min`
match the wrong `Min Ji Kim`. What gets written is a screening decision on a
protected ground ([ADR-047](047-screening-facts-are-declared-never-inferred.md)),
so an unmatched row — which is *surfaced* for a human — is the better failure
than a confident wrong attribution.

### "Nothing is silently dropped", extended to a case this ADR did not have

The original invariant covered an absent file. Reconciliation adds three
failure modes that are about **two real people**, and all three refuse:

- **Ambiguous name match** — one CSV row matching ≥2 résumés (or one résumé
  matching ≥2 rows) is reported, never resolved arbitrarily to either.
- **Conflicting duplicates** — the real export contains one person twice with
  *disagreeing* declarations. Rows are grouped by resolved résumé **before any
  write**, and a disagreement writes nothing for that field. Never last-wins.
  The grouping must precede the write because `set_work_authorization` is a
  single-value guarded UPDATE with no concept of a conflict.
- **Unmatched in both directions**, plus rows whose declaration string the
  parser did not recognise.

Reported the same way this ADR already reports bulk-ingest outcomes: **an
in-response summary plus one audit event, and no new table.** The report and
the audit `details` blob carry counts, line numbers and résumé ids only —
never a decrypted name or email, which matters because the name fallback
decrypts names in bulk and that is exactly where PII leaks into a response.

### Schema shape — two booleans, not an enum

`resumes.internal_apsa` and `internal_cupe`, each `BOOLEAN NOT NULL DEFAULT
FALSE`, in **both** the `CREATE TABLE` block and a separate idempotent
`ALTER ... ADD COLUMN IF NOT EXISTS`: `CREATE TABLE IF NOT EXISTS` is a no-op
on an already-migrated volume, so a `CREATE TABLE`-only change reaches no live
row. Two independent booleans rather than an enum because the source carries
two independent columns that the format does not guarantee are mutually
exclusive — the real export has a row flagged both — and an enum would invent
a `both` state for no consumer.

`DEFAULT FALSE` is safe here for a directional reason argued in full in
[ADR-047](047-screening-facts-are-declared-never-inferred.md): a falsy default
on a *bonus* is the absence of a bonus, not an adverse decision. The **parsed**
row still distinguishes three cases (`bool | None`, `None` = the column was
absent from that export) so that a roster which never mentions APSA cannot
clear a flag a previous roster legitimately set — Taleo exports are snapshots
that get re-uploaded as applicants trickle in.

Gates: `./scripts/verify.sh all` green — 6028 unit @ 91.92% + 616 integration.
## Amendment 2026-09-15 — the mirror guard on the JD side

Decision 1 above gated the **résumé** side of ranking in the UI only (Generate disabled until ≥1 parsed
résumé). It had no counterpart on the **JD** side. Reported by the pilot user in conversation on
2026-09-10: a job whose JD parse yielded zero required skills AND zero nice-to-have skills still ranked 50
candidates — the manager's additional-requirements prompt (weight 0.10, ADR-041) ended up as 100% of the
ranking signal, and nothing on screen said so.

Fixed on `fix/zero-requirements-rank-guard`:

(a) `shortlist_service.assert_job_has_requirements` runs FIRST in `POST /jobs/{job_id}/shortlist`, before
`set_shortlist_ranking`, and raises a 409 `resource.conflict` when both counts are zero. Because it runs
before the state write, a refused job never pins `shortlist_state='ranking'`. Both counts are read
`coalesce`d over the `description_parsed` JSONB in one query, so a never-parsed JD and a parsed blob simply
missing the keys refuse identically — never a silent 202. A nonexistent job is left to the route's own
404 handling (the guard returns silently on no row), so that pre-existing behaviour is unchanged. The gate
is on BOTH counts being zero, not `required_count` alone: nice-to-have skills with no hard requirements are
still a real signal and are not refused.

(b) The frontend disables the Generate button with the reason, banners over an already-ranked shortlist
(any role, since it's parse state, not PII), and warns in the job page's parse status. A refusal surfaced
mid-poll (`generate_shortlist`'s `Conflict` branch, caught ahead of the generic 409 handler since
`Conflict` subclasses `BadRequest`) re-renders the shortlist-cards fragment with the reason and stops
polling, rather than aborting to a bare error page.

(c) The signal is DERIVED from `description_parsed` rather than a new column deliberately: the
`record_parsed` write nulls `failure_reason` on every successful parse, and that column already drives
both the parse-status poll and the job page's Re-parse button, so a warning wired through it would grow a
spurious retry loop for a state re-parsing cannot fix. `description_raw` already has a `min_length` of 50,
so "the JD had a non-trivial description" holds by construction independent of this guard.

(d) `parse_job` logs a WARNING (`parse_job.zero_requirements`), not INFO, at the moment the counts are
known, so an operator can find these jobs without waiting for a recruiter to hit Generate.

Same shape as decision 1 and as ADR-040/041: refuse and disclose, never silently degrade.
