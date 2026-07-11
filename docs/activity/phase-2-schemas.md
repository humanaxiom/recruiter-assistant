# Phase 2 — Schemas (pydantic contract layer) — Activity Report

**Branch:** `feat/phase-2-schemas` (pushed to `origin` = github.com/humanaxiom/recruiter-assistant)
**Base:** `main` (Phase 0, merge commit `8b2b47c`; Phase 1 landed on `feat/phase-1-storage`)
**Date:** 2026-07-11
**Status:** Complete — all offline gates green; three merge-blocking subagent gates (reviewer APPROVE, security PASS, ranking-evals PASS). Not yet merged.

---

## 1. Summary

Phase 2 ports the pydantic **v2** schema layer from hris (`packages/schemas/src/schemas/`) into
`core/src/schemas/` — three modules plus an `__init__` re-export — trimmed to recruiter-assistant's
scope and aligned to the Phase 0 DDL. These are the contract types Phases 3–6 code against: the API
request/response DTOs, the strict LLM-output schemas fed to `chat_json`, the jsonb shapes persisted
verbatim, and the ranking-weight contract. Phase 2 ships pure data models only — no I/O, no
services, no routes, no DB or LLM calls.

`jobs.py` carries the job DTOs (`JobCreate` / `JobUpdate` / `JobTransition` / `JobOut` /
`JobDeleteOut` / `JobListItem`, `JDExtractText`, `BulkJobResult`) and the LLM-extraction schemas
(`Skill` / `Education` / `JDExtracted`). `resumes.py` carries the resume/cover-letter parse shapes
and the resume DTOs, plus the `_coerce_year` helper and the lossy `_drop_invalid_rows` /
`_coerce_names` / `_coerce_rows` pre-validators that keep one malformed row from failing a whole
parse. `matching.py` carries the ranking contract `MatchWeights` (+ `DEFAULT_WEIGHTS`), the score /
evidence / pipeline-meta shapes, and `ShortlistEntry` / `JobMatchEntry` / `JobMatchResultOut`.

Two boundaries were held: the **2nd-review workflow and Taleo/JD-comments were CUT** (not merely
unused — a merge-blocking cut-guard test proves the symbols are not importable), and the DTOs are
**aligned to the Phase 0 DDL** at three points (`created_by`/`uploaded_by` nullable `str`,
`JobCreate.blind_review` defaulting `True`, no `approval_required_2nd_review`). `MatchWeights` is the
ranking contract — its defaults and the sums-to-1.0 validator are load-bearing for ranking
correctness. Interface, boundaries, and the redaction-boundary contract are recorded in **ADR-006**.

---

## 2. Deliverables (mapped to the plan's Phase 2 item)

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | `jobs.py` — job DTOs + `Skill`/`Education`/`JDExtracted` LLM schemas | Done | `core/src/schemas/jobs.py` |
| 2 | `resumes.py` — parse shapes + resume DTOs + `_coerce_year`/`_drop_invalid_rows` | Done | `core/src/schemas/resumes.py` |
| 3 | `matching.py` — `MatchWeights` contract + score/evidence/shortlist shapes, review types CUT | Done | `core/src/schemas/matching.py` |
| 4 | `__init__.py` — re-export KEEP surface only (no CUT name reachable) | Done | `core/src/schemas/__init__.py` |
| 5 | Cut guard: review workflow + Taleo/JD-comments not importable | Done | cut-guard tests; ranking-evals CUT check |
| 6 | DDL-alignment deviations (str actor cols; blind_review default True; no 2nd-review col) | Done | inline `DEVIATION` comments; DDL-alignment tests |

**Not in Phase 2 (by design):** no parsing, no LLM client/cache, no PII encryption, no redaction, no
services, no routes, no scorer. Phase 2 is the schema layer only; Phases 3–6 consume it.

---

## 3. Commit timeline

| Commit | Type | What / why |
|---|---|---|
| `1645178` | red | Failing unit tests for the three schema modules — happy-path validation + jsonb round-trip, field-constraint rejection, `extra="forbid"` vs `extra="ignore"`, `_coerce_year` (two-digit pivot 69, bool rejection, range), `_drop_invalid_rows` lossy filter, `_coerce_names`/`_coerce_rows` coercion + cap, the `MatchWeights` contract (exact defaults, sums-to-1.0 rejection, frozen), the cut-scope guard, and the DDL-alignment deviations. Written before implementation, confirmed RED. |
| `5bbf7c2` | green | `schemas/{jobs,resumes,matching,__init__}.py` — ported the KEEP set, applied the review/Taleo CUTs and the three DDL deviations, wired the re-export. All gates green. |

**Note on the GREEN step.** A `data-pipeline` coder subagent hit an account session limit mid-port.
The coordinator completed GREEN directly: `matching.py` and `__init__.py` were hand-authored from the
extraction spec and verified against the source, then re-checked by the reviewer, security, and
ranking-evals gates (including the `MatchWeights` mutation test) before this report. The two-commit
red → green shape and the green tree are as landed.

---

## 4. Quality gates

Verified in the `python:3.11-slim` container (offline suite), `ruff check` with **no `--fix`**
(matching CI, so committed import order is proven not masked).

| Gate | Tool | Result |
|---|---|---|
| Lint | ruff check (no `--fix`) | PASS |
| Format | black --check | PASS |
| Types | mypy --strict | PASS |
| Unit tests | pytest tests/unit | PASS — **486 tests** |
| Coverage | pytest --cov (threshold 80%) | PASS — **97.52%** |
| Branch name | naming gate | PASS (`feat/phase-2-schemas`) |

Subagent gate verdicts (all three merge-blocking):

- **Reviewer — APPROVE.** KEEP/CUT boundary correct, deviations documented inline, `__all__` prunes
  the CUT names, no I/O in the schema layer.
- **Security — PASS** with guardrails carried forward (see §6). The one substantive contract:
  `ResumeOut`/`ResumeListItem` can serialize decrypted PII with `blinded=True` — the schema is the
  redaction *boundary* but cannot enforce masking; Phase 5 must mask before DTO construction.
- **Ranking-evals — PASS.** No corpus yet (no pipeline), so precision@k is not measurable and was not
  fabricated. The gate asserted the ranking-weight contract instead: `DEFAULT_WEIGHTS` matches the
  plan's algorithm exactly (0.6/0.3/0.1 top; 0.40/0.25/0.10/0.15/0.10 sub; `evidence_verify_fuzz ==
  0.85`), the `_sums_close_to_one` validator genuinely rejects off-sum weights (a **weight-validator
  mutation test** flips a default off-sum and confirms the reject), and no CUT review type leaked
  back in.

---

## 5. Boundaries held by the gates

Two boundaries are enforced, not just intended:

### 5.1 Review workflow + Taleo/JD-comments CUT (merge-blocking cut guard)

The 2nd-review pipeline types (`PipelineStage`, `TERMINAL_STAGES`, `DispositionReason`,
`DecisionKind`, `ShortlistDecisionCreate/Out`, `StageTransitionCreate/Out`) are deleted from
`matching.py` and are not importable; `ShortlistEntry` drops `current_decision`/`current_stage` (but
keeps the blind-review `blinded`/`display_label`). `JobListItem` drops `comment_count` (JD comments),
`source` and `external_last_seen_at` (Taleo). `approval_required_2nd_review` is gone from
`JobCreate`/`JobUpdate`/`JobOut`. The cut-guard test asserts every CUT symbol is absent and the
`__init__` re-exports only the KEEP surface — review creep is a red gate.

### 5.2 DDL alignment (three deviations, tested)

`JobOut.created_by` and `ResumeOut.uploaded_by` are `str | None` (nullable TEXT actor labels — no
users table in v1), a plain string validates. `JobCreate().blind_review is True` (matches the DDL
default / decision 4). No `approval_required_2nd_review` field. Each is commented inline with
`DEVIATION` and asserted in the DDL-alignment tests.

---

## 6. Deferred / carried forward (security-noted)

Recorded in ADR-006 and EXTRACTION_PLAN so nothing is lost:

1. **Redaction-boundary contract (Phase 5, substantive).** `ResumeOut`/`ResumeListItem` expose
   `candidate.*`, `candidate_name`, and `cover_letter_text` as decrypted plaintext and carry a
   `blinded` flag the schema does **not** act on. Phase 5 redaction MUST mask those fields **before**
   constructing the DTO — the schema cannot enforce it. (The ciphertext fields `blob_key` /
   `candidate_email` bytea are deliberately *not* exposed; that half of the boundary holds.)
2. **Per-field `max_length` on LLM string fields (Phase 3, low).** Belt-and-braces caps on the
   free-text LLM-output fields at the ingest boundary.
3. **`JobOut.blind_review` fail-open default (Phase 6, low).** The response DTO defaults
   `blind_review` to `False`; a route that builds `JobOut` without setting the flag would fail open.
   Set it explicitly from the row at the route layer.

Also carried for Phase 3 (unchanged from Phase 1's correction): the strict
`current_setting('app.pii_key')` GUC read with **no** `missing_ok` (a missing-ok read of an unset key
yields NULL ciphertext → silent data loss). That belongs to `pii.py`, which lands in Phase 3.

---

## 7. Metrics

| Metric | Value |
|---|---|
| Commits on branch | 2 (red `1645178` → green `5bbf7c2`) |
| Unit tests | 486 |
| Coverage | 97.52% (threshold 80%) |
| New `src` module | `core/src/schemas/` (`jobs.py`, `resumes.py`, `matching.py`, `__init__.py`) |
| New public surface | job/resume/matching DTOs + LLM schemas + `MatchWeights` / `DEFAULT_WEIGHTS` (re-exported from `src.schemas`) |
| Live HTTP routes | still only `/health` — schemas have no route surface yet |
| Call sites consuming the schemas | none yet (Phases 3–6) |

**Reporting note:** produced in a docs environment without a shell, so a fresh coverage run could not
be recomputed here. Test count (486) and coverage (97.52%) are the figures from the verified
`python:3.11-slim` container gate run; the KEEP/CUT boundary, the three DDL deviations, the
`MatchWeights` defaults + validator, and the redaction-boundary shape were verified directly against
`core/src/schemas/jobs.py`, `resumes.py`, `matching.py`, and `__init__.py`.
