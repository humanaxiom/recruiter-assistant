# Session Handoff — recruiter-assistant

Read this first if you're resuming cold. It captures state, environment quirks, and the exact next step. The full plan is [docs/EXTRACTION_PLAN.md](docs/EXTRACTION_PLAN.md) — this file is the orientation layer.

## What we're doing

Building a **local-first recruiter assistant**: evidence-backed resume ranking → shortlists, fully offline. We are **porting the resume-ranking feature out of `C:\repos\hris`** onto a golden template, stripping the review workflow and JD-Harmonizer, and replacing MinIO with filesystem storage. See the plan for the keep/cut boundary and the 4-stage ranking algorithm.

## Repos & locations

| Thing | Location |
|---|---|
| **This project** (the real thing) | `github.com/humanaxiom/recruiter-assistant` (private) — local `origin` points here |
| Working copy | `C:\repos\recruiter-assistant` — on `main` |
| **Golden template** (frozen, don't build here) | `github.com/adamsalah13/agent-harness-template` (private, is-template) |
| **Source to port FROM** | `C:\repos\hris` (Python 3.12 uv monorepo; the ranking feature lives in `packages/` + `apps/api` + `apps/worker`) |

The working copy now holds the **ranking-domain foundation** (Phase 0) on the template chassis. The template demo app is gone.

## Current state

**Done:** repo created + `origin` repointed + pushed; 4 decisions locked; plan-of-record and the `data-pipeline` + `ranking-evals` subagents committed. **Phase 0 (seed & infra) complete and merged to `main` via PR #1** (merge commit `8b2b47c`, merged 2026-07-11), CI green. **Phase 1 (storage) complete and green on `feat/phase-1-storage`** (not yet merged). **Phase 2 (schemas) complete and green on `feat/phase-2-schemas`** (not yet merged). `main` contains Phase 0; Phases 1 and 2 await merge.

**Phase 2 landed** (two commits — red `1645178` → green `5bbf7c2`): the pydantic **v2** contract layer in `core/src/schemas/` — three modules + an `__init__` re-export (`from src.schemas import JobCreate, ResumeParsed, MatchWeights, …`), the contracts Phases 3–6 code against (API DTOs, strict LLM `chat_json` schemas, jsonb shapes, ranking weights). `jobs.py` = job DTOs + `Skill`/`Education`/`JDExtracted`; `resumes.py` = parse shapes + resume DTOs + the `_coerce_year`/`_drop_invalid_rows`/`_coerce_*` lossy validators; `matching.py` = `MatchWeights` (+ `DEFAULT_WEIGHTS`) + score/evidence/shortlist shapes. Pure data models — no I/O. **Review workflow + Taleo/JD-comments CUT and not importable** (`PipelineStage`/`DispositionReason`/`ShortlistDecision*`/`StageTransition*` deleted; `ShortlistEntry` drops `current_decision`/`current_stage`, keeps blind-review `blinded`/`display_label`; `JobListItem` drops `comment_count`/`source`/`external_last_seen_at`; no `approval_required_2nd_review`) — a merge-blocking cut guard enforces it. **Three DDL-alignment deviations**: `created_by`/`uploaded_by` are `str | None` (nullable TEXT actor labels), `JobCreate.blind_review` defaults `True` (blind-by-default), no `approval_required_2nd_review`. **`MatchWeights` is the ranking-weight contract** (0.6/0.3/0.1 top; 0.40/0.25/0.10/0.15/0.10 sub; `evidence_verify_fuzz=0.85`; frozen; sums-to-1.0 validator). Gates: offline green — ruff (no `--fix`), black, mypy --strict, **486 unit tests, 97.52% coverage**; reviewer APPROVE, security PASS, ranking-evals PASS (incl. a weight-validator mutation test). The GREEN step was completed by the coordinator directly after a coder subagent hit a session limit mid-port (`matching.py` + `__init__.py` hand-authored, re-verified by reviewer + evals). Security flagged a **redaction-boundary contract for Phase 5**: `ResumeOut`/`ResumeListItem` can serialize decrypted PII with `blinded=True`, so Phase 5 redaction MUST mask `candidate.*`/`candidate_name`/`cover_letter_text` before DTO construction (the schema can't enforce it). Details: [docs/activity/phase-2-schemas.md](docs/activity/phase-2-schemas.md); rationale: [docs/adr/006-*.md](docs/adr/006-schema-port-trim-ddl-alignment.md).

**Phase 1 landed** (four commits — red → green → red-harden → green-harden): the filesystem `BlobStore` (`core/src/storage/blob_store.py`) exists — async `put`/`get`/`delete`/`exists`/`list_keys` over `settings.storage_dir`, stdlib-only (`pathlib`/`asyncio`/`os`, IO via `asyncio.to_thread`), replacing MinIO. `BlobNotFound` / `InvalidBlobKey` exceptions. Security core: the `_resolve` guard rejects `..` segments, absolute/Windows-drive/backslash keys, empty/root/null-byte keys, and symlink escapes (realpath + `is_relative_to`); blobs are `0o600` and store-created dirs `0o700` (PIPEDA/FIPPA — blobs-at-rest are permission-gated, distinct from the pgcrypto-encrypted PII *columns*); `list_keys` realpath-filters escaping symlinks out of listings. Wired onto `app.state.blob_store` (with a `get_blob_store` dependency) and worker `ctx["blob_store"]`; **no call site invokes it yet** — the upload/fetch/flush sites are ported in Phases 3–6. Gates: offline green — ruff (no `--fix`), black, mypy --strict, **240 unit tests, 99.46% coverage**; all three merge-blocking gates passed (reviewer APPROVED, security PASS, ranking-evals PASS with a guard-mutation test). Details: [docs/activity/phase-1-storage.md](docs/activity/phase-1-storage.md); rationale: [docs/adr/005-*.md](docs/adr/005-filesystem-blobstore-interface-path-safety.md).

**Phase 0 landed** (seven commits + a merge commit, red → green → 3 review fixes → docs → ruff-pin fix):
- Template demo app removed (`core/src/agents|memory|gates`, `models/db.py`) and replaced with the ranking-domain foundation. Rebrand to `recruiter-assistant`.
- Compose: pg/neo4j/redis/ollama, **no MinIO**, `./data` bind mount. Settings: `llm_embedding_dim = 768` (contract source), `storage_dir`, LLM/Neo4j config.
- **asyncpg idempotent startup DDL** for 5 tables (`jobs`, `resumes` +PII BYTEA, `shortlist_entries`, `reverse_match_entries`, `outbox`; SQLAlchemy dropped). **Neo4j bootstrap**: 4× 768-d cosine vector indexes + skill-graph constraints, dim derived from settings. Schema deviations recorded in **ADR-004**.
- **Gates:** offline green (ruff / black / mypy --strict, 172 unit, coverage 88.79%); integration green (39 tests vs real Postgres + Neo4j). **CI (GitHub Actions) went fully green before merge** — branch-name, `ruff·black·mypy`, `unit·coverage ≥ 80%`, `integration (pg + neo4j + redis)`.
- **Ruff-pin fix (7th commit, `22abcb9`):** CI's ruff (0.15.21) and the local container had resolved different ruff versions (`requirements-dev.txt` only floor-pinned `ruff>=0.6.0`), which disagreed on first-party import grouping and failed the static gate with I001. Fixed by pinning `ruff==0.15.21` and adding `known-first-party = ["src"]` to `core/pyproject.toml`.

**Note on `core/src/gates/`:** the deleted `gates/` was the template demo's *product-code* gate-runner, not the build harness. `make gates`, CI, `.claude/`, and pre-commit are all intact. The Phase 0 checklist's "keep gates" meant the build suite.

**Not started:** Phase 3 onward — see below.

**Decisions locked:** template-first port · filesystem storage (MinIO dropped — community edition archived 2026-04-25) · keep Neo4j (load-bearing) · v1 includes cover-letter/motivation, reverse-match, a minimal Flask viewer, and blind-review redaction ON by default.

## Environment quirks (IMPORTANT — a fresh session won't know these)

- **No real Python on this host** — only the WindowsApps stub. You **cannot** run `make gates` natively. Verify the offline gate suite in a container:
  ```bash
  docker run --rm -v "C:\repos\recruiter-assistant:/w" -w /w/core python:3.11-slim bash -lc \
    "pip install -q -r requirements.txt -r requirements-dev.txt && \
     ruff check --fix src tests && black src tests && \
     ruff check src tests && black --check src tests && mypy src --strict && \
     pytest tests/unit --cov=src --cov-fail-under=80 -q"
  ```
  (The `--fix` + `black` write pass auto-formats; the following `check`/`--check` then verify.)
- **Docker is available.** Integration/e2e that need live Postgres/Neo4j/Redis run via Docker/testcontainers (CI does `gates-all`).
- **Windows 11**, PowerShell primary; Bash tool available (Git Bash). `.claude/settings.json` hooks shell to `bash`, so Git Bash must be on PATH.
- **`gh` CLI** authed as `adamsalah13`, **admin on the `humanaxiom` org**. Pushing to `humanaxiom/recruiter-assistant` is authorized.
- Template Python is **3.11**; hris is **3.12**. Keep 3.11 (the template's) and port hris code to it — nothing in the ranking core needs 3.12.
- Model note: this is Claude Opus 4.8 (1M context); the latest models are the Claude 5 family / Opus 4.8 / Haiku 4.5.

## Subagent roster (`.claude/agents/`)

Build harness (from the template): `planner`, `tester`, `coder`, `reviewer`, `security`, `docs`.
Domain additions (this project): **`data-pipeline`** (ranking coder with the invariants baked in) and **`ranking-evals`** (merge-blocking quality gate: precision@k, evidence-verification rate = 1.0, PII-leak check).

Per-phase flow: planner → tester (+ evals fixture) → data-pipeline coder (ReviewLoop, ≤5 iters) → reviewer + security + ranking-evals (all merge-blocking) → docs. `make gates` green before the next phase.

## Non-negotiables (from CLAUDE.md)

Never commit to `main` for feature work (branch `agent|feat|fix|chore/<slug>`); TDD (failing tests first); offline only (no cloud endpoints — local Ollama/OpenAI-compatible client); config via settings; a single red gate = not done. Privacy: PII never enters embeddings; anonymization non-destructive; PIPEDA/FIPPA.

## Immediate next step — Phase 3 (Ingest + parse)

Port the ingest/parse pipeline: `parsing/{extract,chunk}` (PyMuPDF/python-docx), the LLM client + Redis embed cache, `parse_resume`/`parse_job`, cover-letter parse, and **PII encryption on parse** (`pii.py`, pgcrypto). hris source paths are in **Appendix A** of the plan. These schemas are the parse targets: `JDExtracted` (job parse), `ResumeParsed`/`ResumeCore`/`ResumeSkill*` (resume parse), `CoverLetterParsed` (cover-letter parse). Then Phases 4–7 per the plan table.

**Phases 1 and 2 are done** (see Current state). Carried-forward criteria to apply in Phase 3:
1. **Path-traversal rejection — DONE in Phase 1.** `BlobStore._resolve` rejects `..`, absolute paths, null-byte keys, and symlink escapes before any IO. Nothing further needed.
2. **STRICT PII-key GUC read — a Phase 3 acceptance criterion.** It concerns `pii.py` (the PII read path). Wire `settings.pii_key` into `app.pii_key` with `current_setting('app.pii_key')` **without** `missing_ok=true` — a missing_ok read of an unset key yields NULL → NULL ciphertext → silent data loss. Fail loud.
3. **Per-field `max_length` on LLM string fields — a Phase 3 acceptance criterion** (Phase 2 security low). Add belt-and-braces caps on the free-text LLM-output fields at the ingest boundary.

Carried further: the **Phase 5 redaction-boundary contract** (Phase 2 security, ADR-006 §4) — `ResumeOut`/`ResumeListItem` can serialize decrypted PII with `blinded=True`, so Phase 5 redaction MUST mask `candidate.*`/`candidate_name`/`cover_letter_text` before DTO construction (the schema can't enforce it). And the **Phase 6 `JobOut.blind_review` fail-open** note (Phase 2 security low) — the DTO defaults `blind_review` to `False`, so a route must set it explicitly from the row.

hris source paths for every phase are in **Appendix A** of the plan; architecture rationale: Phase 0 in **ADR-004**, Phase 1 in **ADR-005**, Phase 2 in **ADR-006**.

## Trigger prompt (paste into a new session)

```
Resume the recruiter-assistant build. Working dir C:\repos\recruiter-assistant
(origin github.com/humanaxiom/recruiter-assistant). Read HANDOFF.md and
docs/EXTRACTION_PLAN.md first — they are the source of truth for state,
decisions, environment quirks, and the hris source-file map (Appendix A).
Architecture rationale: Phase 0 in docs/adr/004-*.md, Phase 1 in docs/adr/005-*.md,
Phase 2 in docs/adr/006-*.md.

We are porting the resume-ranking feature from C:\repos\hris onto this template
(template-first, filesystem storage instead of MinIO, keep Neo4j, v1 includes
cover-letter/reverse-match/minimal viewer/blind-default). Phase 0 (seed & infra)
is complete and merged to main via PR #1 (merge commit 8b2b47c), CI green. Phase 1
(storage) is complete and green on feat/phase-1-storage (240 unit tests, 99.46%
coverage). Phase 2 (schemas) is complete and green on feat/phase-2-schemas (486
unit tests, 97.52% coverage; reviewer/security/ranking-evals all green) — the
pydantic contract layer (core/src/schemas: jobs/resumes/matching) exists, review
workflow cut, MatchWeights = the ranking-weight contract. Phase 3 is next.

Start Phase 3 (Ingest + parse): port parsing/{extract,chunk} (PyMuPDF/python-docx),
the LLM client + Redis embed cache, parse_resume/parse_job, cover-letter parse, and
PII encryption on parse (pii.py, pgcrypto) — via the TDD subagent loop using the
data-pipeline and ranking-evals subagents. Parse targets are the Phase 2 schemas:
JDExtracted, ResumeParsed/ResumeCore/ResumeSkill*, CoverLetterParsed.

Phase 3 acceptance criteria carried forward: (1) STRICT current_setting('app.pii_key')
with NO missing_ok — NULL key → NULL ciphertext → silent data loss, fail loud;
(2) per-field max_length on the LLM string fields at the ingest boundary. Further
out: Phase 5 redaction MUST mask candidate.*/candidate_name/cover_letter_text before
building ResumeOut (schema can't enforce it, ADR-006 §4); Phase 6 must set
JobOut.blind_review explicitly (the DTO defaults it False, fail-open).

Note: no local Python — verify gates in the python:3.11-slim Docker container per
HANDOFF.md. Do Phase 3 on a feat/ branch, land make gates green, then check in
with me before Phase 4.
```
