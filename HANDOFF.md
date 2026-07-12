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

The working copy now holds the **ranking-domain foundation** (Phases 0–2: infra + storage + schemas) on the template chassis, all merged to `main`. The template demo app is gone.

## Current state

**Done:** repo created + `origin` repointed + pushed; 4 decisions locked; plan-of-record and the `data-pipeline` + `ranking-evals` subagents committed. **Phases 0, 1, and 2 are all complete and merged to `main`, CI green:** Phase 0 (seed & infra) via PR #1 (merge `8b2b47c`), Phase 1 (storage) via PR #2 (merge `f7e7cbe`), Phase 2 (schemas) via PR #3 (merge `cefd545`). All merged 2026-07-11.

### ⚠️ Phase 3 is IN FLIGHT on branch `feat/phase-3-ingest-parse` — NOT merged, NOT yet PR'd

**Read the "Phase 3 resume — exact next step" section below FIRST.** The branch is 15 commits ahead of `main`, working tree clean, all gates green **offline + integration**, but **the round-3 reviewer/security re-audit has not run yet** — that is the immediate next action, not a new phase.

**What landed on the branch (TDD, red→green throughout):** the full ingest/parse pipeline ported from `C:\repos\hris`:
- `core/src/pipeline/parsing/{extract,chunk}.py` — PyMuPDF/python-docx/striprtf extraction + section-aware chunker (chunk ids **one-based**, `c_001`/`cl_001`; `_sanitize` NUL-strip preserved).
- `core/src/pipeline/llm/{client,cache}.py` — hris's hand-rolled **httpx** OpenAI-compatible client (retry + circuit breaker, chat/JSON-mode/embeddings) + Redis read-through `CachedEmbedder`. **`openai` dep was REMOVED** (locked decision: port httpx verbatim).
- `core/src/pipeline/skills.py` + `skill_data/aliases.yaml` — Neo4j-free slice of `skill_normalize` (match/canonicalize/`build_summary_text`); the Neo4j skill-graph half is deferred to Phase 4.
- `core/src/prompts/` — Jinja loader + **4** template pairs (`jd_extract_v1`, `resume_core_v1`, `resume_skills_v2`, `cover_letter_v1`). NOTE: the handoff's old "4 pairs" list was wrong — it named `shortlist_evidence_v1` (Phase 4) and omitted `jd_extract_v1` (which `parse_job` calls). Corrected.
- `core/src/services/{pii,job_service,resume_service,outbox_service}.py` — **this dir did not exist before**; it's a hard prerequisite the plan under-scoped. `pii.py` uses the STRICT `current_setting('app.pii_key')` (no `missing_ok`) sourced from `settings.pii_key` (env), not hris's secrets-file ladder. `record_parsed` has the optimistic-concurrency race guard (0 rows → `False` → task `"stale"`, no outbox row).
- `core/src/worker/{tasks,resume_tasks}.py` + `main.py` wiring — `parse_job` / `parse_resume`; `WorkerSettings.functions = [parse_job, parse_resume]`. **Graph projection (`project_to_graph`/`normalize_skill`) deliberately CUT to Phase 4** — Phase 3 stops at parse → Postgres → outbox row (undelivered rows are the outbox pattern working).
- `core/src/schemas/{resumes,jobs}.py` — added `max_length` caps on LLM-output fields (carried-forward Phase-2 security item) **and fixed a real Phase-2 data-loss bug**: the `mode="before"` row filters dropped already-validated sub-model instances (`ResumeParsed(skills=[ResumeSkill(...)])` yielded `skills == []`); dict path is byte-identical.
- `core/src/settings.py` (+6 LLM/cache knobs), `core/requirements.txt` (added PyMuPDF/python-docx/striprtf/jinja2/pyyaml; removed openai; `redis>=5.0.1`).
- `docs/adr/007-phase3-ingest-parse-hardening.md` — records all Phase 3 decisions + the PII-at-rest boundary.

**Gate history on this branch (this is the important part):** first full pass was reviewer=CHANGES-REQUIRED, security=FAIL, ranking-evals=PASS. Both blocking gates **mutation-tested every guard** and found real defects. Two fix rounds followed:
- **Round 1** (`e24f9dc` red → `c8485b9` green): PII-in-`ValidationError` redaction (pydantic embeds `input_value` → leaked into `failure_reason`/logs), unbounded-chunks → uncaught `ValidationError`, decompression-bomb caps, LLM-emitted NUL, `embed()` dim validation, dropped `candidate` from outbox payload.
- **Round 2** (`86f66d1` red → `c57a1c1` green): the re-audit **defeated round 1 by mutation** — DOCX bomb guard trusted the zip's self-declared central-directory sizes (forged CD → 198 MB inflation), a corrupt PDF raised a bare `RuntimeError` that still escaped uncaught, `_strip_nuls` could hit `RecursionError`, embedding-call failures escaped `parse_resume`, and dropping only the `candidate` field while still shipping raw chunk text (résumé header PII) into the outbox was "theatre." All six fixed: DOCX guard now streams members with a real 50 MB decompression ceiling; `_extract_pdf` page-count + loop wrapped → `UnsupportedMimeError`; `chat_json` catches `RecursionError` (+ depth-bounded `_strip_nuls`); permanent embedding `LLMOutputInvalidError` → `record_parse_failure` (transient `LLMUnavailableError` deliberately still escapes so arq retries an outage); outbox payload now excludes chunk `text` too (Phase 4 reads text from `resumes.parsed`, the system of record).

**Human decision made this session (record, don't re-ask):** for PII-at-rest, we **drop `candidate` (and now chunk text) from the outbox payload only**; `resumes.parsed` jsonb retains cleartext candidate — accepted for v1, documented in ADR-007 §6, revisit before any multi-tenant deploy. Phase 5 redaction is display-only and must not be mistaken for at-rest protection.

**Current gate status:** offline (ruff/black/mypy --strict/**708 unit @ 96.62%**) and integration (**65** vs real Postgres+Neo4j) all GREEN as of `c57a1c1`, host write-back verified. `.claude/settings.json` was reverted to `main` (`f12faf6`) after parallel agents polluted it — do not let it back into the diff.

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
- **Docker is available.** Integration/e2e that need live Postgres/Neo4j/Redis run via Docker/testcontainers (CI does `gates-all`). For testcontainers in the container, mount the docker socket + install `docker.io` + set `TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal`.
- **Two container gotchas:** (1) prefix `docker run` with `MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'` or Git Bash mangles `/w/core`→`W:/core`. (2) stale `__pycache__` on the Windows bind mount can mask source edits (coarse mtime → reused bytecode); when re-running pytest after editing source, add `PYTHONDONTWRITEBYTECODE=1` or clear `__pycache__`.
- **No git identity configured** — commit with inline `git -c user.name='Adam Salah' -c user.email=asalah@sfu.ca commit …`.
- **Windows 11**, PowerShell primary; Bash tool available (Git Bash). `.claude/settings.json` hooks shell to `bash`, so Git Bash must be on PATH.
- **`gh` CLI** authed as `adamsalah13`, **admin on the `humanaxiom` org**. Pushing to `humanaxiom/recruiter-assistant` is authorized.
- Template Python is **3.11**; hris is **3.12**. Keep 3.11 (the template's) and port hris code to it — nothing in the ranking core needs 3.12.
- Model note: this is Claude Opus 4.8 (1M context); the latest models are the Claude 5 family / Opus 4.8 / Haiku 4.5.

## Subagent roster (`.claude/agents/`)

Build harness (from the template): `planner`, `tester`, `coder`, `reviewer`, `security`, `docs`.
Domain additions (this project): **`data-pipeline`** (ranking coder with the invariants baked in) and **`ranking-evals`** (merge-blocking quality gate: precision@k, evidence-verification rate = 1.0, PII-leak check).

Per-phase flow: planner → tester (+ evals fixture) → data-pipeline coder (ReviewLoop, ≤5 iters) → reviewer + security + ranking-evals (all merge-blocking) → docs. `make gates` green before the next phase.

**Subagent model tiering ([docs/SUBAGENT_MODEL_POLICY.md](docs/SUBAGENT_MODEL_POLICY.md)):** cheap producers + strong verifiers. The three merge-blocking gates (`reviewer`, `security`, `ranking-evals`) run on **opus** and are never downgraded; producers (`data-pipeline`, `planner`, `tester`, `coder`) default to **sonnet**; `docs` runs on **haiku**. Defaults are in each `.claude/agents/*.md` frontmatter. The coordinator overrides per-call: `data-pipeline` UP to `opus` for diffs touching the 4-stage ranking algorithm / evidence verifier / PII crypto / Neo4j scoring; `docs` UP to `sonnet` for load-bearing handoff/plan refreshes; `coder`/`Explore` DOWN to `haiku` for mechanical fixes / lookups. Quality holds because every producer's diff passes the opus-tier gates + CI before merge.

## Non-negotiables (from CLAUDE.md)

Never commit to `main` for feature work (branch `agent|feat|fix|chore/<slug>`); TDD (failing tests first); offline only (no cloud endpoints — local Ollama/OpenAI-compatible client); config via settings; a single red gate = not done. Privacy: PII never enters embeddings; anonymization non-destructive; PIPEDA/FIPPA.

## Phase 3 resume — EXACT next step (do this first, before anything else)

Phase 3 is **built and green but not yet re-audited or merged**. The next actions, in order:

1. **Re-run the two merge-blocking gates that last returned FAIL/CHANGES-REQUIRED** — `reviewer` and `security`, both on **opus** — over `git diff main...HEAD` on `feat/phase-3-ingest-parse`. They FAILED twice and were fixed twice (rounds 1 & 2 above); round-2 fixes (`c57a1c1`) have **not** been re-audited. Tell each gate exactly what round 2 changed (see the round-2 list in Current state) and have them **mutation-test the new guards** — especially: DOCX streaming decompression cap (forge a lying central directory again → must still reject), `_extract_pdf`'s broad `except → UnsupportedMimeError` (the corrupt-PDF `code=7` / cyclic-page-tree fixtures), `_strip_nuls` recursion catch, the permanent-vs-transient embedding-error split in `parse_resume`, and that no chunk-text PII rides in the outbox payload. `ranking-evals` already PASSed round 0 but re-run it too since source changed. If any gate finds a NEW real defect, do another red→green fix round (max sensible; each gate has been right so far — do not overrule a mutation-proven finding).
   - Verify gates in the `python:3.11-slim` container per "Environment quirks" — no local Python. The `--fix`/`black` **write** pass MUST be scoped to `src` only; running it over `tests/` clobbers other work and is how `.claude/settings.json` got polluted.
2. **When all three gates are green**, `docs` (haiku, or sonnet if load-bearing) does a final pass: confirm ADR-007 is complete, update `docs/EXTRACTION_PLAN.md` Phase 3 row to ✅, and **check in with the human before opening the PR** (they asked to gate Phase 4 behind a check-in). Push `feat/phase-3-ingest-parse`, open the PR to `main` via `gh` (authed as `adamsalah13`, admin on `humanaxiom`), let CI (`gates-all`) go green, then merge.
3. **Only then** start Phase 4 (Ranking engine) per the plan table.

**Carried into Phase 4** (from Phase 3 gate findings — don't lose these):
- The **outbox drainer** (`project_to_graph`) lands in Phase 4 — it consumes the `job.parsed`/`resume.parsed` rows Phase 3 enqueues. It **must not** project `parsed.candidate` into Neo4j and **must not** log the payload. hris's `_resume_projection_tx` only sets `total_years_experience` on the `Resume` node — keep it that way.
- `ResumeSkill.evidence_chunk_ids` is always `[]` after a Phase 3 parse (matches hris) — Phase 4's evidence verifier sources citations from the `shortlist_evidence_v1` prompt against `parsed.chunks`, and `scrub_invalid_chunk_refs` (ported-but-uncalled) wires in at that citation boundary.
- `core/tests/evals/` does not exist yet — the precision@k / evidence-verification-rate corpus + `thresholds.toml` must be created before Phase 4's matching engine so its first green build is falsifiable.
- Chunk-cap coupling nit: `chunk.py`'s `{"c":200,"cl":100}` and `ResumeParsed.chunks`/`cover_letter_chunks` `max_length` are two magic numbers coupled only by a comment — consider a single source.

## Historical: original Phase 3 plan (for reference)

Port the ingest/parse pipeline: `parsing/{extract,chunk}` (PyMuPDF/python-docx), the LLM client + Redis embed cache, `parse_resume`/`parse_job`, cover-letter parse, and **PII encryption on parse** (`pii.py`, pgcrypto). hris source paths are in **Appendix A** of the plan. These schemas are the parse targets: `JDExtracted` (job parse), `ResumeParsed`/`ResumeCore`/`ResumeSkill*` (resume parse), `CoverLetterParsed` (cover-letter parse). Then Phases 4–7 per the plan table.

**Phases 1 and 2 are done** (see Current state). Carried-forward criteria to apply in Phase 3:
1. **Path-traversal rejection — DONE in Phase 1.** `BlobStore._resolve` rejects `..`, absolute paths, null-byte keys, and symlink escapes before any IO. Nothing further needed.
2. **STRICT PII-key GUC read — a Phase 3 acceptance criterion.** It concerns `pii.py` (the PII read path). Wire `settings.pii_key` into `app.pii_key` with `current_setting('app.pii_key')` **without** `missing_ok=true` — a missing_ok read of an unset key yields NULL → NULL ciphertext → silent data loss. Fail loud.
3. **Per-field `max_length` on LLM string fields — a Phase 3 acceptance criterion** (Phase 2 security low). Add belt-and-braces caps on the free-text LLM-output fields at the ingest boundary.

Carried further: the **Phase 5 redaction-boundary contract** (Phase 2 security, ADR-006 §4) — `ResumeOut`/`ResumeListItem` can serialize decrypted PII with `blinded=True`, so Phase 5 redaction MUST mask `candidate.*`/`candidate_name`/`cover_letter_text` before DTO construction (the schema can't enforce it). And the **Phase 6 `JobOut.blind_review` fail-open** note (Phase 2 security low) — the DTO defaults `blind_review` to `False`, so a route must set it explicitly from the row.

hris source paths for every phase are in **Appendix A** of the plan; architecture rationale: Phase 0 in **ADR-004**, Phase 1 in **ADR-005**, Phase 2 in **ADR-006**.

### Phase 3 starting map (verified)

Two read-only audits confirmed the following against `C:\repos\hris` — orientation for a cold start, not a spec:

- **Dependency gap.** `core/requirements.txt` is missing `PyMuPDF` (import `fitz`), `python-docx` (import `docx`), and `striprtf` (lazy-imported for RTF) — add all three. Already present and sufficient: `redis` (ships `redis.asyncio`), `httpx`, `openai`, `tenacity`.
- **LLM client decision.** hris's `LLMClient` (`packages/pipeline/src/pipeline/llm/client.py`) hand-rolls the OpenAI-compatible REST calls over `httpx` (chat / JSON-mode / embeddings) with its own retry + circuit breaker — it does **not** use the `openai` SDK. `cache.py` (`CachedEmbedder`) is a Redis read-through cache over `LLMClient.embed` via `redis.asyncio`. Phase 3 decision to make: port the httpx client verbatim (recommended — matches source) vs. rewrite on the `openai` SDK already in requirements.
- **Both carried-forward PII criteria are already satisfied in the hris source — port verbatim, don't re-invent:**
  - `_build_summary_text` (`apps/worker/src/worker/resume_tasks.py`) excludes PII structurally — it only reads `parsed.summary`/`skills`/`experience`/`education`, never `parsed.candidate` (the `CandidateInfo` holding name/email/phone). Preserve this "never touch `.candidate`" discipline when building embedding/summary text.
  - `pii.py` (`apps/api/src/api/services/pii.py`) already uses the strict GUC read — `current_setting('app.pii_key')` single-arg, no `missing_ok` — so an unset key raises rather than silently yielding NULL. This is exactly the Phase 3 acceptance criterion; port the SQL verbatim (`set_pii_key` = `SELECT set_config('app.pii_key', $1, true)`; `encrypt`/`decrypt` via `pgp_sym_encrypt/decrypt(..., current_setting('app.pii_key'))`).
- **Source + target schemas confirmed ready.** hris side: `parsing/{extract,chunk}.py`, `llm/{client,cache}.py`, `pipeline/config.py` (scope down its many `match_*`/`jd_*` knobs), `worker/resume_tasks.py` (`parse_resume`, `project_to_graph`, `_build_summary_text`, `_parse_cover_letter`), `worker/tasks.py` (`parse_job`), `services/pii.py`. Target side: `core/src/schemas/` already has `JDExtracted`, `ResumeParsed`/`ResumeCore`/`ResumeSkill`/`ResumeSkillDetails`, `CoverLetterParsed`, `ResumeChunk`.
- Prompt templates live at `packages/prompts/src/prompts/templates/` (Appendix A corrected); the four pairs needed (`.system.j2` + `.user.j2`) all exist: `resume_core_v1`, `resume_skills_v2`, `shortlist_evidence_v1`, `cover_letter_v1`.

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
cover-letter/reverse-match/minimal viewer/blind-default). Phases 0, 1, and 2 are
ALL complete and merged to main, CI green: Phase 0 (seed & infra) PR #1, Phase 1
(storage — filesystem BlobStore) PR #2, Phase 2 (schemas) PR #3. main contains
Phases 0-2 (486 unit tests, 97.52% coverage). The pydantic contract layer
(core/src/schemas: jobs/resumes/matching) exists, review workflow cut,
MatchWeights = the ranking-weight contract. Phase 3 is next.

Subagent model tiering is in effect (docs/SUBAGENT_MODEL_POLICY.md): the three
merge-blocking gates (reviewer/security/ranking-evals) run on opus; producers
(data-pipeline/planner/tester/coder) default to sonnet; docs on haiku. Override
data-pipeline UP to opus for the 4-stage ranking algorithm / evidence verifier /
PII crypto / Neo4j scoring diffs. Defaults live in .claude/agents/*.md frontmatter.

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

See the "Phase 3 starting map (verified)" subsection above for the verified
dependency gap, LLM-client decision, and PII port-verbatim details before starting.
```
