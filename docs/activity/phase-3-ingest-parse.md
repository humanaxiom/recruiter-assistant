# Phase 3 — Ingest & Parse — Activity Report

**Branch:** `feat/phase-3-ingest-parse` (pushed to `origin` = github.com/humanaxiom/recruiter-assistant)
**Base:** `main` (Phase 2, merge commit `cefd545`)
**Date:** 2026-07-11
**Status:** Complete and green — all three merge-blocking gates pass on final HEAD `c7b497e` (reviewer APPROVE, security PASS, ranking-evals PASS). **Not yet merged** — awaiting a human check-in before the PR is opened (see HANDOFF.md).

---

## 1. Summary

Phase 3 ports the résumé/JD **ingest + parse** pipeline from hris (`packages/pipeline/`,
`apps/worker/`) into `core/`: PDF/DOCX/RTF/TXT extraction (`pipeline/parsing/{extract,chunk}.py`), the
hand-rolled `httpx` OpenAI-compatible LLM client + Redis embedding cache (`pipeline/llm/`), the skills
scan/merge (`pipeline/skills.py`), pgcrypto PII encryption (`services/pii.py`), and the `parse_job` /
`parse_resume` arq tasks that land a parsed job/résumé in Postgres and enqueue an outbox event for
Phase 4. `core/src/services/` did not exist before this phase; it is a hard prerequisite the plan
under-scoped. Graph projection (`project_to_graph`) is deliberately **not** ported — Phase 3 stops at
`parse → Postgres → outbox`, and the undelivered outbox rows are the outbox pattern working as
intended.

This was the most heavily re-audited phase to date: the first pass was `reviewer=CHANGES-REQUIRED`,
`security=FAIL`, and **four** rounds of findings-and-fix followed before all three merge-blocking gates
went green on the same HEAD. The load-bearing decision — the **PII-at-rest boundary** (identity may
live at rest behind the DB boundary; identity must never ride the outbox into the graph, not as the
structured block, not as chunk/summary text, and not encoded inside an embedding vector) — is recorded
in **ADR-007**, along with every round's findings and the two deliberate accepted residuals (N1, N2).

---

## 2. Deliverables (mapped to the plan's Phase 3 item)

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | `pipeline/parsing/{extract,chunk}.py` — PyMuPDF/python-docx/striprtf extraction + section-aware chunker | Done | one-based chunk ids (`c_001`/`cl_001`), `_sanitize` NUL-strip, 10 MB/300-page/50 MB DOCX caps |
| 2 | `pipeline/llm/{client,cache}.py` — httpx client (retry + circuit breaker) + Redis `CachedEmbedder` | Done | `openai` dependency removed; offline-egress test; `emb:v1:*` keyspace |
| 3 | `pipeline/skills.py` + `skill_data/aliases.yaml` — Neo4j-free skill scan/merge | Done | `_extract_skills_merged` floor (names-only when the model fails) |
| 4 | `prompts/` — Jinja loader + 5 template pairs (`jd_extract_v1`, `resume_core_v1`, `resume_skills_v2`, `cover_letter_v1`) | Done | 4 pairs, not the larger hris review/harmonizer set |
| 5 | `services/{pii,job_service,resume_service,outbox_service}.py` | Done | STRICT `current_setting('app.pii_key')`, no `missing_ok`; optimistic-concurrency race guard in `record_parsed` |
| 6 | `worker/{tasks,resume_tasks}.py` + `main.py` wiring — `parse_job` / `parse_resume` | Done | `WorkerSettings.functions = [parse_job, parse_resume]`; graph projection cut to Phase 4 |
| 7 | `schemas/{resumes,jobs}.py` — per-field `max_length` caps on LLM-output fields | Done | carried-forward Phase-2 security item; also fixed a Phase-2 data-loss bug in the `mode="before"` row filters |
| 8 | PII encryption on parse (`pii.py`, pgcrypto) — STRICT GUC read | Done | carried-forward Phase-0/1 acceptance criterion |
| 9 | `docs/adr/007-phase3-ingest-parse-hardening.md` | Done | records every decision + the PII-at-rest boundary + all four audit rounds |

**Not in Phase 3 (by design):** Neo4j graph projection (`project_to_graph`), the ranking engine
(`orchestrator`/`stages`), shortlist persistence/export, routes — those are Phases 4–6.

---

## 3. Commit timeline

20 commits ahead of `main`, base `2a5486b` → final HEAD `c7b497e`.

| Commit | Type | What / why |
|---|---|---|
| `508064a` | red | Failing tests for parsing/chunking/LLM client/cache/prompts/skills |
| `b3a69df` | red | Failing tests for PII encryption, schema field caps, writeback services |
| `38c208d` | red | Failing tests for worker parse tasks + PII-in-embeddings guard |
| `2ce2d3e` | green | Ported hand-rolled httpx LLM client + Redis embedding cache |
| `bf5f491` | green | Ported resume/JD parsing pipeline (extract, chunk, skills, prompts) |
| `639355d` | green | Added Phase 3 LLM/cache settings; swapped the `openai` dep for the httpx port |
| `22d687f` | green | Ported PII/job/resume/outbox services + LLM-boundary field caps |
| `a9f9601` | refactor | Accept a pooled connection in the service signatures |
| `f3f7e2c` | green | Ported `parse_job` + `parse_resume` worker tasks |
| `eee1f38` | green | Wired worker parse tasks; fixed two provably-wrong tests + a Phase-2 schema data-loss bug |
| `f12faf6` | chore | Reverted `.claude/settings.json` to `main` after parallel-agent pollution |
| `e24f9dc` → `c8485b9` | red → green | **Round 1** — PII-in-`ValidationError` redaction, unbounded-chunks guard, decompression-bomb caps, LLM-emitted NUL handling, `embed()` dim validation, dropped `candidate` from the outbox payload |
| `86f66d1` → `c57a1c1` | red → green | **Round 2 (F1–F6)** — DOCX streaming decompression ceiling (the CD-size guard was forgeable), `_extract_pdf` broad-wrap → `UnsupportedMimeError`, `_strip_nuls` `RecursionError` guard, permanent-vs-transient embedding-error split, dropped chunk **text** from the outbox too. ADR-007 written. |
| `d7afe53` → `13c74d8` | red → green | **Round 3 (F1/F2/F3/F5)** — embed-boundary `_redact_candidate_pii` scrub (F1, HIGH — embeddings are PII-equivalent), dropped `summary` from the outbox (F2), worker-startup fail-loud on empty `PII_KEY` (F3), wrapped `doc.needs_pass` (F5). ADR-007 §7/§7a extended. |
| `6e1d35e` → `c7b497e` | red → green | **Round 4 (F1-R)** — closed a MEDIUM residual under-redaction (whitespace/format-divergent identifiers) with a whitespace-flexible pattern + email-local-part scrub; pinned `black==26.5.1` for gate reproducibility |

---

## 4. Quality gates

Verified in the `python:3.11-slim` container (offline suite), `ruff check` with **no `--fix`**.

| Gate | Tool | Result |
|---|---|---|
| Lint | ruff check (no `--fix`) | PASS |
| Format | black --check (pinned `==26.5.1`) | PASS |
| Types | mypy --strict | PASS |
| Unit tests | pytest tests/unit | PASS — **729 tests** |
| Coverage | pytest --cov (threshold 80%) | PASS — **~96.6%** |
| Branch name | naming gate | PASS (`feat/phase-3-ingest-parse`) |
| Integration | pytest tests/integration (real Postgres + Neo4j) | PASS |

Subagent gate verdicts on final HEAD `c7b497e` (after four rounds of findings-and-fix):

- **Reviewer — APPROVE.**
- **Security — PASS.** See §5 for what each round found.
- **Ranking-evals — PASS.**

---

## 5. Findings caught by the gates, across four rounds

See **ADR-007** for full technical detail; this is the summary.

- **Round 1.** PII leaking into `failure_reason`/logs via pydantic's `ValidationError.input_value`;
  unbounded chunk counts reaching an uncaught `ValidationError`; DOCX/PDF caps; LLM-emitted NUL bytes;
  `embed()` dimension validation; the structured `candidate` block dropped from the outbox payload.
- **Round 2 (F1–F6).** The re-audit defeated round 1 by mutation: the DOCX guard trusted the zip's
  self-declared central-directory size (a forged 50-byte declaration over a 60 MB member passed the
  guard, then inflated anyway); a corrupt PDF raised a bare `RuntimeError` that still escaped uncaught;
  `_strip_nuls` could itself hit `RecursionError`; embedding-call failures escaped `parse_resume`
  unguarded; and dropping only the `candidate` field while résumé header chunks still carried
  name/email/phone verbatim was "theatre." All six fixed.
- **Round 3 (F1/F2/F3/F5).** **F1 (HIGH)** — `chunk_embs`/`summary_emb` in the outbox payload encode
  candidate identity inside the embedding vectors themselves (a header-chunk or name-opened-summary
  embedding is PII-equivalent under PIPEDA/FIPPA), closed with a deterministic
  `_redact_candidate_pii` scrub at the embedder boundary. **F2** — the outbox `summary` field was still
  cleartext, dropped. **F3** — an empty `PII_KEY` did not fail loud; worker startup now refuses to
  start rather than silently encrypting PII with an empty passphrase. **F5** — `_extract_pdf`'s
  `doc.needs_pass` read was unwrapped and could raise untyped on a corrupt PDF; wrapped the same way as
  the page-count/page-loop reads.
- **Round 4 (F1-R).** A MEDIUM residual: the round-3 scrub matched the LLM's *normalized* identifiers
  against the *un-normalized* résumé body, so whitespace/format divergence (line-broken names, reflowed
  phone numbers, a bare email local-part) could still leak into embedded text. Closed with a
  whitespace-flexible pattern plus a separate email-local-part scrub. Two residuals were then
  **deliberately accepted, documentation-only**: **N1** (structured experience/education/skills fields
  ride the outbox unscrubbed — non-contact, needed for Phase 4's graph projection, symmetric with the
  §6/§7 at-rest cleartext decision) and **N2** (the scrub errs toward over-redaction of embedded text,
  e.g. a common-word `location` substring — favors privacy over retrieval precision).

---

## 6. The PII-at-rest boundary (load-bearing decision, ADR-007 §7)

Candidate identity exists in three places with three different postures: **encrypted at rest**
(`candidate_*` BYTEA columns, pgcrypto); **cleartext at rest** in `resumes.parsed` jsonb (accepted for
v1 — it sits behind the same DB-access boundary as the encrypted columns and is the system of record
Phase 4 reads chunk text from; Phase 5 owns display-time redaction); and the **outbox payload**, which
must carry **no** candidate identity at all — not the structured block, not chunk/summary text, and not
encoded inside an embedding vector. Getting the third bullet right took three of the four audit rounds
above (round 1: drop the structured block; round 2: drop chunk text too; round 3: drop `summary` and
scrub the embeddings; round 4: close the embed-scrub's format-divergence gap).

Also recorded: **ADR-007 §7a** ratifies Redis as the embedding-cache backend (`emb:v1:*` keyspace, same
Redis instance as the arq broker, distinct key space) as an accepted deviation from CLAUDE.md's "Redis
only as arq broker" — the vectors are non-authoritative (Neo4j is the system of record) and no PII
lands in Redis (cache keys/values are computed from already-scrubbed embedder input).

---

## 7. Carried forward to Phase 4

- The **outbox drainer** (`project_to_graph`) lands in Phase 4. It must not project `parsed.candidate`
  into Neo4j and must not log the payload.
- `ResumeSkill.evidence_chunk_ids` is always `[]` after a Phase 3 parse; Phase 4's evidence verifier
  sources citations from `shortlist_evidence_v1` against `parsed.chunks`.
- `core/tests/evals/` does not exist yet — the precision@k / evidence-verification-rate corpus must be
  created before Phase 4's matching engine.
- A chunk-cap coupling nit: `chunk.py`'s `{"c":200,"cl":100}` and the schema `max_length` caps are two
  magic numbers coupled only by a comment.
- **N1/N2** (§5/§6 above) are Phase-4/Phase-5 context, not Phase-3 defects.

---

## 8. Metrics

| Metric | Value |
|---|---|
| Commits on branch | 20 (`508064a` → `c7b497e`) |
| Merge-blocking audit rounds | 4 (round 1 general findings; round 2 F1–F6; round 3 F1/F2/F3/F5; round 4 F1-R) |
| Unit tests | 729 |
| Coverage | ~96.6% (threshold 80%) |
| New `src` modules | `pipeline/{parsing,llm,skills}`, `prompts/`, `services/` (new directory), worker parse tasks |
| `openai` dependency | removed (client ported on `httpx`) |
| New ADR | `docs/adr/007-phase3-ingest-parse-hardening.md` |

**Reporting note:** produced in a docs environment without a shell, so a fresh coverage/test run could
not be recomputed here. Test count (729) and coverage (~96.6%) are the figures reported for final HEAD
`c7b497e`, per HANDOFF.md; commit hashes and round-by-round findings are read directly from
`.git/logs/HEAD` and `docs/adr/007-phase3-ingest-parse-hardening.md`.

---

## 9. Merge

**Not yet merged.** Per the human's standing instruction, Phase 4 is gated behind a check-in before
this PR is opened. As of this writing, the branch has not been pushed for a PR and no check-in has
occurred — see HANDOFF.md's "Phase 3 resume — EXACT next step" for the exact next action.
