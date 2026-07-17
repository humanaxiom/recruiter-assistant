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

**Done:** repo created + `origin` repointed + pushed; 4 decisions locked; plan-of-record and the `data-pipeline` + `ranking-evals` subagents committed. **Phases 0, 1, 2, and 3 are all complete and merged to `main`, CI green:** Phase 0 (seed & infra) via PR #1 (merge `8b2b47c`), Phase 1 (storage) via PR #2 (merge `f7e7cbe`), Phase 2 (schemas) via PR #3 (merge `cefd545`), Phase 3 (ingest + parse) via PR #6 (merge `49196d7`). Phases 0–2 merged 2026-07-11; Phase 3 merged 2026-07-12. **Phase 4 (Ranking engine) is ✅ complete — all 4 gated sub-phases merged to `main`.** Sub-phase **4a (evals corpus) is MERGED to `main` via PR #8** (merge `875eac2`), CI green, 2026-07-12, and its **falsifiability hardening is also MERGED via PR #10** (merge `464a479`), CI green. **Sub-phase 4b (graph projection) MERGED to `main` via PR #11** (merge `68fe821`), CI green. **Sub-phase 4c (matching engine) is MERGED to `main` via PR #12** (merge `fd12d1a`), CI green. **Sub-phase 4d (shortlist + reverse-match write path) is MERGED to `main` via PR #13** (merge `5945320`) this session, CI green. **Phase 5 (persist + anonymize + export — read/list/get/export + display redaction) is MERGED to `main` via PR #14** (merge `6deade3`), CI green. **Phase 6 (API routes — job create/read/list/status, résumé upload/read/list, shortlist generate/list/get/export, reverse-match, configurable auth) is COMPLETE and MERGED to `main` via PR #15** (squash merge `e910669`, CI `gates-all` fully green, merged 2026-07-17), tip `837de9e` — all three merge-blocking gates were green (reviewer APPROVE, security PASS, ranking-evals PASS) AND CI (incl. a live `run_evals.py` re-measurement against Ollama) went fully green before merge. **Phase 7 (evals + minimal Flask viewer) is now the ACTIVE sub-phase, being built on branch `feat/phase-7-evals-viewer`** (off `main` @ `e910669`). See "Phase 4a status", "Phase 4b status", "Phase 4c status", "4d status", "Phase 5 status", and "Phase 6 status" below.

### Phase 4a status — corpus + hardening both MERGED (read this before starting 4c)

**Corpus.** `core/tests/evals/` (JD fixture + labelled synthetic résumés + `labels.json` + `thresholds.toml`
+ RED-pending-4c harness stub `run_evals.py`) is **MERGED to `main` via PR #8** (merge `875eac2`), CI
green, 2026-07-12. Zero product code.

**Falsifiability hardening.** Branch `fix/phase-4a-corpus-falsifiability`, opened as **PR #10**
(https://github.com/humanaxiom/recruiter-assistant/pull/10), off `main` @ `463cbaa`, tip `583427f`, 18
commits. **MERGED to `main` via merge commit `464a479`, CI green.** Why this
branch exists: the three merge-blocking gates audited the *merged* corpus and proved by mutation that it
**could not fail a bad Phase-4c engine** — the artifact whose whole purpose is to make 4c's first green
build falsifiable. Nine cumulative rounds of findings-and-fix closed every hole found (rounds 1–2 pre-merge
on the original 4a branch; rounds 3–9 on this branch). Full history:
[docs/activity/phase-4a-ranking-evals-corpus.md](docs/activity/phase-4a-ranking-evals-corpus.md); contract
detail: [docs/EXTRACTION_PLAN.md](docs/EXTRACTION_PLAN.md) ("4a hardening" subsections and "Current status
& next step").

**Final gate verdicts, HEAD `583427f`:**
- reviewer: **APPROVE** (31 of 32 mutations killed across the branch's history; the one survivor is **R1**
  below — a consciously-carried residual, not an open defect)
- security: **PASS** (empty findings table)
- ranking-evals: **PASS**
- Offline: ruff · black · `mypy --strict` clean · **1040 unit tests @ 96.63% coverage** (up from **955** on
  `main` before this branch — zero `core/src/` changes, so the whole delta is new eval-corpus tests) ·
  **65 integration tests** vs real Postgres+Neo4j · `run_evals.py` still exits 1 (correct pre-4c RED state)
- **Zero `core/src/` changes** across the whole branch

**The six-arm baseline battery** (verified against an engine replica ported from hris
`packages/pipeline/src/pipeline/matching/{stages,orchestrator}.py`, real `nomic-embed-text` on a cold
cache, real `rapidfuzz`, both input orders):

| engine | precision@5 | r09 rank | ordering pairs | verdict |
|---|---|---|---|---|
| keyword-overlap | 0.80 | 1 | 0/3 | FAIL |
| lexical tf-idf | 1.00 | 8 | 0/3 | FAIL |
| embedding pure-vector | 0.80 | 4 | 0/3 | FAIL |
| faithful + no-op evidence verifier | 0.80 | 1 | 3/3 | FAIL (adversarial arm) |
| faithful + hris `_fuzz_substring` | 0.80 | 1 | 3/3 | FAIL |
| faithful + correct verifier | 1.00 | 8 | 3/3 | **PASS** |

**The single most important finding for 4c.** hris's `_fuzz_substring` — the evidence verifier 4c is
slated to port verbatim — **verifies all four of the corpus's fabricated quotes** (0.928 / 0.943 / 0.988 /
0.935, all ≥ the 0.85 bar) and puts the keyword-stuffer at **rank 1**. It is a character-**set** overlap
ratio, not a sequence ratio. **It must be REPLACED, not ported** — use `rapidfuzz.partial_ratio` or
`token_set_ratio` (both measured safe: negatives score 0.36–0.46, golds 1.000). Other measured landmines:
`fuzz.WRatio` scores a fabricated anchor at **0.855** (leaks); `partial_token_set_ratio` returns **1.000**
on 2 of 4 negatives; `fuzz.ratio` scores the corpus's own **gold** anchors at 0.648/0.796 (would reject
valid evidence).

**The recurring lesson, worth stating once, plainly.** The corpus was wrong three separate times in the
**same way**: it asserted what a sub-score *should* mean rather than what the code *does*. `seniority` is
**not** a years check — it is `cosine(jd.title, most-recent job title)` rescaled from `seniority_floor`.
`score_education` reads only the degree **level** and never `jd.education.fields`. Each time, a control
that looked rigorous was **inert**. The gates only caught it once they stopped reasoning from the spec and
**ported the actual `stages.py`**. **4c should read `stages.py` first, not third.**

**Open decisions / accepted residuals carried into 4c:**
- **R1 — the corpus is blind to the skill sub-score's internals.** `weights.skill = 0.0` **passes**;
  recency decay disabled passes (even though r10's `decision_point` is literally
  `recency_decay_stale_skills` — that label is decorative); `must_have_miss_penalty 0.5 → 1.0` passes; the
  implied-experience relief path and the ontology junk-bucket are never exercised decisively.
  **Deliberately CARRIED INTO 4c, not closed on this branch** — closing it needs skill-dimension twin
  fixtures, which churns the rank bands. 4c must add: a recency twin for r10, a must-have-miss twin, and
  confirm `weights.skill = 0` **FAILS**.
- **R2 — the corpus gates the evidence *verifier*, never the *extractor*.** An LLM that simply fails to
  *find* real evidence is caught only in the limit (`min_completeness_in_topk` catches "no quote at all").
- **Open decision needing a human:** `score_education` ignores `jd.education.fields`, so JD field-relevance
  is **decorative** today. Either extend the scorer, or drop `fields` from the JD contract. The r14/r11
  ordering pair is deliberately built to survive **either** resolution.
- **Ported engine helpers are trusted, not verified, until 4c lands.**
  `test_ported_engine_helpers_agree_with_the_real_ones` **skips** today and wakes up when
  `src.pipeline.matching.{stages,orchestrator}` exists. **If it fails then, re-derive every corpus claim
  that depends on the ports** (r09's potency, r11/r14's partial credit, the education twins, the
  ordering-pair gaps) — do NOT relax the comparison.
- **The three blind-engine mutations are a documented review OBLIGATION, not a gate**
  (`weights.education = 0`, `overqual_ratio = 99`, `weights.motivation = 0` must each FAIL on **both**
  input orders). No gate in this repo can run them — they need the engine with *mutated* `MatchWeights`,
  which is a property of 4c's own test suite. **The 4c reviewer is the last line of defence on the
  ordering pairs.**

**Documented process deviation (flag for the human, not silently absorbed).** Three commits on the
hardening branch are labelled `test(...)` rather than `red:`/`green:` — a deliberate, reviewer-verified
deviation from CLAUDE.md's mandatory TDD order, because the guards they add pass against the unmutated
tree and can only be shown red by *mutation*, not by an honest failing-test-first commit. Detail in
`docs/EXTRACTION_PLAN.md`'s round-numbering note.

**Suggested chores flagged by security, OUTSIDE this branch's scope (not fixed, just flagged):**
- `recruiter@sfu.ca` appears in `core/tests/unit/test_schemas_jobs.py` and `test_schemas_resumes.py`
  (Phase-2 code, already on `main`) — a real institutional domain used as test data, in a repo whose own
  PII invariant (this corpus) bans exactly that shape. Suggested chore branch: replace with `@example.test`.
- The repo owner's real email is also in **this file's own git-identity recipe** below (see "Environment
  quirks") — kept here only as a placeholder now; the real value lives in the owner's git config.
- Pre-existing: **18 `mypy --strict` errors in `core/tests/unit/`** — the gate is `mypy src --strict`
  only, so repo *tests* are not type-gated at all. Suggested chore, not a blocker.

### Phase 3 is MERGED to `main` (PR #6, merge `49196d7`) — DONE

Phase 3 (ingest + parse) landed on `main` on 2026-07-12 via PR #6 after **four rounds of gate findings-and-fix** on branch `feat/phase-3-ingest-parse` (now deleted). All three merge-blocking gates were green on final HEAD `c7b497e` (reviewer APPROVE, security PASS, ranking-evals PASS) and **CI (`gates-all`) went fully green before merge**. The gate history below is retained because its findings (especially the PII-at-rest / outbox-embedding boundary) are load-bearing context for Phase 4. **The next action is Phase 4 — see "Phase 4 resume" below.**

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

Full write-up: [docs/activity/phase-3-ingest-parse.md](docs/activity/phase-3-ingest-parse.md); rationale: [docs/adr/007-*.md](docs/adr/007-phase3-ingest-parse-hardening.md).

**Gate history on this branch (this is the important part):** first full pass was reviewer=CHANGES-REQUIRED, security=FAIL, ranking-evals=PASS. All blocking gates **mutation-tested every guard** and found real defects across four rounds:
- **Round 1** (`e24f9dc` red → `c8485b9` green): PII-in-`ValidationError` redaction (pydantic embeds `input_value` → leaked into `failure_reason`/logs), unbounded-chunks → uncaught `ValidationError`, decompression-bomb caps, LLM-emitted NUL, `embed()` dim validation, dropped `candidate` from outbox payload.
- **Round 2** (`86f66d1` red → `c57a1c1` green, findings F1–F6): the re-audit **defeated round 1 by mutation** — DOCX bomb guard trusted the zip's self-declared central-directory sizes (forged CD → 198 MB inflation), a corrupt PDF raised a bare `RuntimeError` that still escaped uncaught, `_strip_nuls` could hit `RecursionError`, embedding-call failures escaped `parse_resume`, and dropping only the `candidate` field while still shipping raw chunk text (résumé header PII) into the outbox was "theatre." All six fixed: DOCX guard now streams members with a real 50 MB decompression ceiling; `_extract_pdf` page-count + loop wrapped → `UnsupportedMimeError`; `chat_json` catches `RecursionError` (+ depth-bounded `_strip_nuls`); permanent embedding `LLMOutputInvalidError` → `record_parse_failure` (transient `LLMUnavailableError` deliberately still escapes so arq retries an outage); outbox payload now excludes chunk `text` too (Phase 4 reads text from `resumes.parsed`, the system of record). ADR-007 was written at this point.
- **Round 3** (`d7afe53` red → `13c74d8` green, findings F1/F2/F3/F5): the re-audit found the round-2 outbox fix still incomplete — **F1 (HIGH): `chunk_embs`/`summary_emb` in the outbox payload encode candidate identity inside the embedding vectors themselves** (a `nomic-embed-text` vector of a header chunk, or of a summary a small model opened with the candidate's name, is PII-equivalent under PIPEDA/FIPPA); **F2: the outbox `summary` field** was still cleartext and could open with the candidate's name; **F3: an empty `PII_KEY`** did not fail loud, so a misconfigured deploy would silently `pgp_sym_encrypt` PII with an empty passphrase; **F5: `_extract_pdf`'s `doc.needs_pass` read** was unwrapped and could raise an untyped exception on a corrupt (not merely password-protected) PDF, escaping `extract_text` uncaught. All four fixed: a deterministic `_redact_candidate_pii` scrub (whitespace-flexible identifier match) applied to every string handed to the embedder; `summary` dropped from the outbox payload; `worker/main.py` startup now raises `RuntimeError` on an empty `PII_KEY` before opening any pool/driver/store; `needs_pass` wrapped the same way as the page-count/page-loop reads. ADR-007 §7/§7a were extended.
- **Round 4** (`6e1d35e` red → `c7b497e` green, finding F1-R): the re-audit found a **MEDIUM residual under-redaction** in the round-3 embed scrub — identifiers are matched as the LLM's *normalized* values against the *un-normalized* résumé body, so whitespace/format divergence (a line-broken name, a reflowed phone number, a bare email local-part) could still leave identity in the embedded text. Fixed with a whitespace-flexible redaction pattern (tokens joined by `\s+`) plus a separate email-local-part scrub. Two deliberate, accepted, documentation-only residuals were recorded alongside the fix: **N1** (structured experience/education/skills fields ride the outbox unscrubbed — non-contact, symmetric with the §6/§7 at-rest cleartext decision) and **N2** (the scrub errs toward over-redaction of embedded text, e.g. a common-word `location` substring). Same commit also **pinned `black==26.5.1`** in `requirements-dev.txt` for gate reproducibility (CI and local containers had been resolving different `black` versions).

**All three merge-blocking gates are green on final HEAD `c7b497e`** — reviewer APPROVE, security PASS, ranking-evals PASS. No further gate rounds are outstanding.

**Human decision made this session (record, don't re-ask):** for PII-at-rest, we **drop `candidate` (and now chunk text) from the outbox payload only**; `resumes.parsed` jsonb retains cleartext candidate — accepted for v1, documented in ADR-007 §6, revisit before any multi-tenant deploy. Phase 5 redaction is display-only and must not be mistaken for at-rest protection.

**Current gate status:** offline (ruff/black/mypy --strict/**729 unit @ ~96.6% coverage**) and integration (vs real Postgres+Neo4j) all GREEN as of final HEAD `c7b497e`, host write-back verified. `.claude/settings.json` was reverted to `main` (`f12faf6`) after parallel agents polluted it — do not let it back into the diff.

**Phase 2 landed** (two commits — red `1645178` → green `5bbf7c2`): the pydantic **v2** contract layer in `core/src/schemas/` — three modules + an `__init__` re-export (`from src.schemas import JobCreate, ResumeParsed, MatchWeights, …`), the contracts Phases 3–6 code against (API DTOs, strict LLM `chat_json` schemas, jsonb shapes, ranking weights). `jobs.py` = job DTOs + `Skill`/`Education`/`JDExtracted`; `resumes.py` = parse shapes + resume DTOs + the `_coerce_year`/`_drop_invalid_rows`/`_coerce_*` lossy validators; `matching.py` = `MatchWeights` (+ `DEFAULT_WEIGHTS`) + score/evidence/shortlist shapes. Pure data models — no I/O. **Review workflow + Taleo/JD-comments CUT and not importable** (`PipelineStage`/`DispositionReason`/`ShortlistDecision*`/`StageTransition*` deleted; `ShortlistEntry` drops `current_decision`/`current_stage`, keeps blind-review `blinded`/`display_label`; `JobListItem` drops `comment_count`/`source`/`external_last_seen_at`; no `approval_required_2nd_review`) — a merge-blocking cut guard enforces it. **Three DDL-alignment deviations**: `created_by`/`uploaded_by` are `str | None` (nullable TEXT actor labels), `JobCreate.blind_review` defaults `True` (blind-by-default), no `approval_required_2nd_review`. **`MatchWeights` is the ranking-weight contract** (0.6/0.3/0.1 top; 0.40/0.25/0.10/0.15/0.10 sub; `evidence_verify_fuzz=0.85`; frozen; sums-to-1.0 validator). Gates: offline green — ruff (no `--fix`), black, mypy --strict, **486 unit tests, 97.52% coverage**; reviewer APPROVE, security PASS, ranking-evals PASS (incl. a weight-validator mutation test). The GREEN step was completed by the coordinator directly after a coder subagent hit a session limit mid-port (`matching.py` + `__init__.py` hand-authored, re-verified by reviewer + evals). Security flagged a **redaction-boundary contract for Phase 5**: `ResumeOut`/`ResumeListItem` can serialize decrypted PII with `blinded=True`, so Phase 5 redaction MUST mask `candidate.*`/`candidate_name`/`cover_letter_text` before DTO construction (the schema can't enforce it). Details: [docs/activity/phase-2-schemas.md](docs/activity/phase-2-schemas.md); rationale: [docs/adr/006-*.md](docs/adr/006-schema-port-trim-ddl-alignment.md).

**Phase 1 landed** (four commits — red → green → red-harden → green-harden): the filesystem `BlobStore` (`core/src/storage/blob_store.py`) exists — async `put`/`get`/`delete`/`exists`/`list_keys` over `settings.storage_dir`, stdlib-only (`pathlib`/`asyncio`/`os`, IO via `asyncio.to_thread`), replacing MinIO. `BlobNotFound` / `InvalidBlobKey` exceptions. Security core: the `_resolve` guard rejects `..` segments, absolute/Windows-drive/backslash keys, empty/root/null-byte keys, and symlink escapes (realpath + `is_relative_to`); blobs are `0o600` and store-created dirs `0o700` (PIPEDA/FIPPA — blobs-at-rest are permission-gated, distinct from the pgcrypto-encrypted PII *columns*); `list_keys` realpath-filters escaping symlinks out of listings. Wired onto `app.state.blob_store` (with a `get_blob_store` dependency) and worker `ctx["blob_store"]`; **no call site invokes it yet** — the upload/fetch/flush sites are ported in Phases 3–6. Gates: offline green — ruff (no `--fix`), black, mypy --strict, **240 unit tests, 99.46% coverage**; all three merge-blocking gates passed (reviewer APPROVED, security PASS, ranking-evals PASS with a guard-mutation test). Details: [docs/activity/phase-1-storage.md](docs/activity/phase-1-storage.md); rationale: [docs/adr/005-*.md](docs/adr/005-filesystem-blobstore-interface-path-safety.md).

**Phase 0 landed** (seven commits + a merge commit, red → green → 3 review fixes → docs → ruff-pin fix):
- Template demo app removed (`core/src/agents|memory|gates`, `models/db.py`) and replaced with the ranking-domain foundation. Rebrand to `recruiter-assistant`.
- Compose: pg/neo4j/redis/ollama, **no MinIO**, `./data` bind mount. Settings: `llm_embedding_dim = 768` (contract source), `storage_dir`, LLM/Neo4j config.
- **asyncpg idempotent startup DDL** for 5 tables (`jobs`, `resumes` +PII BYTEA, `shortlist_entries`, `reverse_match_entries`, `outbox`; SQLAlchemy dropped). **Neo4j bootstrap**: 4× 768-d cosine vector indexes + skill-graph constraints, dim derived from settings. Schema deviations recorded in **ADR-004**.
- **Gates:** offline green (ruff / black / mypy --strict, 172 unit, coverage 88.79%); integration green (39 tests vs real Postgres + Neo4j). **CI (GitHub Actions) went fully green before merge** — branch-name, `ruff·black·mypy`, `unit·coverage ≥ 80%`, `integration (pg + neo4j + redis)`.
- **Ruff-pin fix (7th commit, `22abcb9`):** CI's ruff (0.15.21) and the local container had resolved different ruff versions (`requirements-dev.txt` only floor-pinned `ruff>=0.6.0`), which disagreed on first-party import grouping and failed the static gate with I001. Fixed by pinning `ruff==0.15.21` and adding `known-first-party = ["src"]` to `core/pyproject.toml`.

**Note on `core/src/gates/`:** the deleted `gates/` was the template demo's *product-code* gate-runner, not the build harness. `make gates`, CI, `.claude/`, and pre-commit are all intact. The Phase 0 checklist's "keep gates" meant the build suite.

**Not started:** Phase 4 onward — see below. (Phase 3 is merged to `main`; see "Current state" above.)

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
- **No git identity configured** — commit with inline `git -c user.name='Adam Salah' -c user.email=<owner-email> commit …` (the real address is in the owner's git config / the owner knows it — kept as a placeholder here per the security-flagged chore above: this repo's own PII invariant bans real personal emails in committed text, and this file is committed text).
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

## Phase 7 resume — EXACT next step (do this first, before anything else)

**Phase 6 is MERGED** (PR #15, squash merge `e910669`, CI `gates-all` fully green, 2026-07-17; human go-ahead received 2026-07-17). **Phase 7 (ranking-quality evals fixtures + a minimal read-only Flask viewer) is the active sub-phase, in progress on branch `feat/phase-7-evals-viewer`** (off `main` @ `e910669`). A `planner` subagent decomposed it; run the per-phase loop (planner → tester → data-pipeline/coder → reviewer + security + ranking-evals → docs) and **check in with the human before opening the Phase 7 PR.** The full historical resume trail is retained below for context.

Phases 0–3 are **merged to `main`, CI green** (Phase 3 via PR #6, merge `49196d7`, 2026-07-12). Phase 4
(Ranking engine) was split into 4 gated sub-phases (4a→4b→4c→4d, each its own branch/PR — see the plan
table) and **all four are now MERGED to `main`, CI green**: 4a (evals corpus) via PR #8 (merge
`875eac2`) plus falsifiability hardening via PR #10 (merge `464a479`); 4b (graph projection) via PR #11
(merge `68fe821`); 4c (matching engine) via PR #12 (merge `fd12d1a`); 4d (shortlist + reverse-match
write path) via PR #13 (merge `5945320`) — see "4d status" below for the closed Requirement 1, the
persistence asymmetry, and the PII residual. **Phase 5 (persist + anonymize + export —
`list_for_job`/`get_one`/`export_rows` + display redaction) is MERGED to `main` via PR #14** (merge
`6deade3`), CI green — see "Phase 5 status" below for the redaction-boundary enforcement, the
`ScoreBreakdown` fold-read guard, the `cover_letter_chunks` security fix, and the (now resolved)
`original_filename` residual. **Phase 6 (API routes — job create/read/list/status, résumé
upload/read/list, shortlist generate/list/get/export, reverse-match, configurable auth) is COMPLETE and
gate-green on branch `feat/phase-6-api-routes`** (off `main` @ `6deade3`, HEAD `837de9e`) — all three
merge-blocking gates green (reviewer APPROVE, security PASS, ranking-evals PASS). **Human check-in
received 2026-07-17; opened as PR #15** (https://github.com/humanaxiom/recruiter-assistant/pull/15) —
CI (`gates-all`) running, merge held for a human go-ahead. See "Phase 6 status" below for the auth switch, the
upload/zip scope, the status-transition route, the reverse-match-no-redaction decision, the security
hardening (SEC-1/2/4), and the `pool.py` latent-bug fix. **Your next action is Phase 7 (ranking-quality
evals fixtures + a minimal read-only Flask viewer — see the plan table), once Phase 6's PR #15 is
reviewed and merged.** Confirm PR #15's merge status with the human first (`gh pr view 15`).

### 4a recap (see "Phase 4a status" above for the full write-up)
`core/tests/evals/` holds the labelled corpus (JD fixture + synthetic résumés, `labels.json`,
`thresholds.toml`, harness stub `run_evals.py`) and is now gate-hardened by 9 cumulative rounds of
findings-and-fix (1040 unit tests, 65 integration tests, reviewer/security/ranking-evals all green on
PR #10's tip `583427f`, since merged). **Do not start 4c without first reading the 4c warnings in
"Phase 4a status" above** — several of them (the `_fuzz_substring` replacement, reading `stages.py`
first, the skill-sub-score residual R1) are exactly the surface 4c itself lands on.

### 4b status — DONE, MERGED via PR #11 (merge `68fe821`) — read this before starting 4c/4d

`core/src/worker/graph_tasks.py` (the outbox drainer) + the job/résumé Neo4j projection
(`worker/{tasks,resume_tasks}.py`) + the Neo4j skill-graph half of `skill_normalize`
(`pipeline/skills_graph.py`, `skill_data/categories.yaml`) were built and gate-green on branch
`feat/phase-4b-graph-projection`, tip `429adc7`, 20 commits, off `main` @ `464a479`, opened as
**PR #11** (https://github.com/humanaxiom/recruiter-assistant/pull/11) and **MERGED to `main` on
2026-07-15 (merge `68fe821`), CI green.** Full write-up:
[docs/activity/phase-4b-graph-projection.md](docs/activity/phase-4b-graph-projection.md); PII
architecture rationale: [docs/adr/008-skill-graph-pii-by-construction.md](docs/adr/008-skill-graph-pii-by-construction.md).

**The headline.** `ResumeSkill.name` is untrusted free text; a small model that fumbles a résumé
header into `skills[]` can put a candidate's name into a Neo4j `Skill` node and its embedding.
**Five rounds of heuristic pattern-matching (shape rejection → offline name lexicon → quantifier
tuning → vendor veto) each closed one hole and opened another, hitting CLAUDE.md's 5-iteration cap
with a critical still open.** The human then chose an architectural fix that eliminates the class by
construction (**ADR-008**), on the insight that a job description carries no candidate PII:
`Skill.canonical_key` is either a closed-vocab cleartext term or a salted hash — never free text; the
résumé side never embeds, vector-searches, or writes cleartext; every name-detection heuristic was
**deleted outright**, not tuned again. Security PASSED after mutation-killing all four
`_resolve_one` branches and verifying the canonical-key constraint on a real Neo4j.

**Ranking-evals then did something the 4a hardening rounds, working against an idealized engine
replica, couldn't:** it projected the 4a corpus through 4b's real code into a real Neo4j and measured
the actual cost. Spelling-recall was **37.5%** — one variant (`REST APIs` vs `REST API design`) cost a
strong candidate **−0.144 on `score_final`**, more than education + overqual + motivation *combined*,
enough to drop them out of the shortlist. Fixed in-branch (`_basic_normalise` trailing-version/
parenthetical stripping, symmetric both sides): recall on the measured divergence class went
**40% → 100%**. A follow-up commit deduped `_basic_normalise` (it had drifted between two "byte-
identical by convention" copies) and gated the new parenthetical-split against skill inflation
(`Casey Rivera (Python)` must not extract `python`).

**Final gate state, HEAD `429adc7`:** ruff/black/`mypy --strict` clean; **1739 unit tests @ 97.04%
coverage** (up from 1040 pre-branch); **82 integration tests** vs real Postgres+Neo4j (up from 65);
`run_evals.py` still exits 1 (correct pre-4c RED). **All three merge-blocking gates green:** security
PASS, reviewer APPROVE, ranking-evals PASS.

**Read ADR-008's residuals before building on the graph** — 14 accepted residuals, not restated here.
The two that matter most going forward: **the vocabulary (147 concepts / ~229 spellings) is now the
single ranking bottleneck** — growing it is the only lever that improves non-vocab recall, since
auto-merge no longer affects scoring at all; and a candidate whose name collides with a vocab term
(`julia`, `hudson`, `kafka`, …) still gets a deniable-but-cleartext `canonical_key`.

### 4c status — DONE, MERGED to `main` via PR #12 (merge `fd12d1a`) — read this before starting 4d/5

`core/src/pipeline/matching/{stages,orchestrator}.py` (the 4-stage ranking engine) + `MatchWeights`
settings wiring (`src/settings.py::weights_from_settings`) + the live orchestrator wired into
`run_evals.py::main()` were built and gate-green on branch `feat/phase-4c-matching-engine`, off `main`
@ `68fe821` (PR #11's merge), opened as **PR #12**
(https://github.com/humanaxiom/recruiter-assistant/pull/12) on 2026-07-15. **All three merge-blocking
gates were green (security PASS, reviewer APPROVE, ranking-evals PASS) AND CI (`gates-all`) was fully
green — PR #12 was MERGED to `main` (merge `fd12d1a`).** Full write-up:
[docs/activity/phase-4c-matching-engine.md](docs/activity/phase-4c-matching-engine.md); decisions +
residuals: [docs/adr/009-matching-engine-port.md](docs/adr/009-matching-engine-port.md).

**All four 4b→4c blockers are closed** (full detail in ADR-009 and the activity report; the
`docs/EXTRACTION_PLAN.md` "4b → 4c BLOCKERS" section is now marked CLOSED, not restated here):
1. `missing_must` now keys off `reason == "missing"` (row `ontology_weight == 0`), never the built
   contribution's `score == 0.0` — verified single-candidate on r18 (the must-have-miss penalty is
   provably uncatchable by any pairwise rank+gap check; ADR-009 §2 has the algebra).
2. Two new skill-dimension twins landed — a must-have-miss twin (r18) and a recency twin (r19 vs
   r10) — both independently prove `weights.skill = 0`, `must_have_miss_penalty 0.5→1.0`, and
   disabled recency decay each FAIL.
3. The spelling-divergence twin was **not** needed as a separate fixture — the −0.144 swing that
   motivated it was a 4b graph-projection normalisation issue, already fixed in 4b (`_basic_normalise`);
   4c's own r18/r19 twins cover the skill sub-score's internals for 4c's own scoring bugs.
4. `canonical_name` → `canonical_key` renamed in `_stage2_skill_rows`' Cypher on day one of the port,
   verified against a real Neo4j.

**Also closed from Phase 4a's carry-forward list:** `_fuzz_substring` REPLACED with
`rapidfuzz.fuzz.partial_ratio` (re-measured against the full corpus: rejects all 4 fabrications at
0.41–0.46, survives all 4 gold anchors at 1.000); an evidence-recall assertion against `gold_evidence`
now runs (`gold_recall_min = 1.0`, R2 closed). **Still open, carried to a human, NOT resolved by this
port:** `score_education` ignores `jd.education.fields` — extend the scorer or drop `fields` from the
JD contract (ADR-009 §7).

**New in 4c, not anticipated by 4a/4b:** a reviewer finding that `orchestrator.py` read
`os.environ.get("GIT_SHA")` directly (CLAUDE.md violation) — fixed by routing `git_sha` through
`Settings`/`MatchingContext`, and backed by a new AST meta-test
(`test_no_scattered_os_environ.py`) that fails the gate if any module other than `settings.py` reads
`os.environ`/`os.getenv` again.

**Final gate state, HEAD `ed4a142`:** ruff/black/`mypy --strict` clean; **1916 unit tests @ 90.71%
coverage**; **87 integration tests** vs real Postgres+Neo4j; **`run_evals.py` exits 0** — the corpus's
first real live-engine run, not a scaffold. `precision@5 = 1.0`; r09 (adversarial bait) ranks 12th
(outside top-5); `gold_recall = 4/4`; 0 PII leaks / 116 inputs scanned; determinism exact. All six
mutation obligations (`weights.education=0`, `overqual_ratio=99`, `weights.motivation=0`,
`weights.skill=0`, `must_have_miss_penalty 0.5→1.0`, disabled recency) FAIL the corpus as required,
plus the optional WRatio-swap.

**Carried forward into 4d** (see the "4d status" section immediately below — the settings-wiring item is
now CLOSED there; the `jd.education.fields` decision is still open): wire `MatchingContext`/`weights`
from `Settings` at the real worker call sites (4c only proves the bridge in isolation); the open
`jd.education.fields` human decision; 4d ships the write path only.

### 4d status — DONE, MERGED to `main` via PR #13 (merge `5945320`) — read this before starting Phase 6

`core/src/services/shortlist_service.py` (`persist_shortlist`/`persist_reverse_match`) +
`core/src/worker/matching_tasks.py` (`shortlist_job`/`reverse_match_job` arq tasks) +
`matching_context_from_settings`/`non_matchable_families_from_settings` (the ADR-009 "Requirement 1"
settings-wiring closure) were built on branch `feat/phase-4d-shortlist-writepath`, tip `6c2bf43`, 2
commits (RED `24419b0` → GREEN `6c2bf43`), off `main` @ `fd12d1a` (PR #12's merge). **All three
merge-blocking gates were green (reviewer APPROVE, security PASS, ranking-evals PASS) AND CI
(`gates-all`, incl. a live `run_evals.py` re-measurement against Ollama) went fully green — PR #13 was
MERGED to `main` (merge `5945320`) this session.** The scoring code itself, `stages.py`/
`orchestrator.py`, is byte-unchanged by 4d — only the new additive `matching_context_from_settings`
factory touches `orchestrator.py`. Full write-up:
[docs/activity/phase-4d-shortlist-writepath.md](docs/activity/phase-4d-shortlist-writepath.md);
decisions + residuals: [ADR-010](docs/adr/010-shortlist-reverse-match-write-path.md).

**Requirement 1 (ADR-009, carried from 4c) is now CLOSED.** `matching_context_from_settings(settings,
*, db, neo4j, llm, embedder)` is the single call site populating every non-weight `MatchingContext`
tunable from `Settings`; `shortlist_job`/`reverse_match_job` call it with `get_settings()` and pass
`weights=weights_from_settings(get_settings())` into the orchestrator — never `DEFAULT_WEIGHTS`. The
load-bearing test class here is a settings-wiring unit test built around **non-default** `Settings`
values, because `run_evals.py` structurally cannot catch a silent fallback to `DEFAULT_WEIGHTS`:
`Settings()`'s own `match_*` defaults equal `MatchWeights`' defaults by construction, so the corpus
(which only ever runs against default `Settings`) would pass either way. See ADR-010 §3.

**The mirror-image persistence asymmetry (ADR-010 §2).** `shortlist_entries` has no dedicated
`score_structured`/`score_evidence` columns and `evidence JSONB NOT NULL` → `persist_shortlist` folds
those two scores into the `score_breakdown` jsonb and coerces `evidence=None` to `{}`.
`reverse_match_entries` has dedicated columns and a nullable `evidence JSONB` →
`persist_reverse_match` writes those as their own SQL args and passes `evidence=None` through as SQL
`NULL`. Residual: at the raw-SQL level, shortlist's `{}` cannot distinguish "never evidence-scored"
from "scored, found nothing" — a minor info-loss Phase 5's read layer should be aware of.

**PII-at-rest residual, security-flagged and recorded (not just in a test docstring, per instruction) —
ADR-010 §6.** Evidence quotes (verbatim résumé/cover-letter chunk text) are written unredacted into
both tables. This is symmetric with, not a new instance beyond, ADR-007 §6/§7's already-accepted
cleartext-at-rest posture for `resumes.parsed` — same Postgres instance, same DB-access boundary,
derivative of already-accepted-cleartext chunk text, and it never rides the outbox or an embedding.
Accepted for v1; revisit before multi-tenant.

**Both DELETE-first persist functions are per-run idempotent, keyed on the DDL's real unique
constraints** (`shortlist_entries (job_id, resume_id)`, `reverse_match_entries (resume_id, job_id)`),
proven against a real Postgres including the `NOT NULL`/`UniqueViolationError` cases a mocked
connection can't exercise. **No advisory lock exists for concurrent duplicate enqueues** — accepted
(last-committer-wins; nothing currently enqueues duplicates by design), revisit once Phase 6 ships a
user-facing regenerate route (ADR-010 §1).

**`reverse_match_job`'s `allowed_job_ids` filter is `description_parsed IS NOT NULL`, not
`status = 'open'`** — `jobs.status` is never transitioned by any code path through 4d (no Phase-6 route
yet), so filtering on it would filter to zero or an arbitrary default; ADR-010 §4 has the full
reasoning and the note to revisit once Phase 6 starts transitioning `status`.

**Open human decision, carried forward AGAIN, still unresolved:** `score_education` ignores
`jd.education.fields` (ADR-009 §7) — 4d touches none of `stages.py`/`orchestrator.py`'s scoring code
(byte-unchanged), so this is untouched, not newly relevant. Either extend the scorer to read `fields`,
or drop `fields` from the JD contract.

**Final gate state, HEAD `6c2bf43`:** ruff/black/`mypy --strict` clean; **1947 unit tests @ 91.98%
coverage**; **93 integration tests** vs real Postgres+Neo4j (87 carried forward + 6 new, all in
`test_shortlist_persistence_pg.py`). Reviewer's sign-off rests on 8 behavioral guards (rerun-replaces
×2, `NOT NULL` coercion, weights-from-settings ×2, `matching_context_from_settings` tunable coverage,
the `allowed_job_ids` scoping, and the no-silent-redaction guard) — full table in the activity report.

**Carried forward into Phase 5:** `list_for_job`/`get_one`/`export_rows` + display redaction (deferred
by 4d's own scope, per the plan-of-record); the shortlist-side `evidence = {}` ambiguity (above); the
still-open `jd.education.fields` decision; no advisory lock on concurrent runs.

### Phase 5 status — DONE, MERGED to `main` via PR #14 (merge `6deade3`) — read this before starting Phase 7

`core/src/services/redaction.py` (new — `redact_text`/`pseudonym`/`blind_label_map`/
`is_foreign_location` + `redacted_filename`, ported near-verbatim from hris) + `core/src/errors.py` (new —
`AppError`/`NotFoundError`) + the read/export extensions to `shortlist_service.py`
(`list_for_job`/`get_one`/`export_rows` + pure `shortlist_csv`/`shortlist_evidence_csv`/`shortlist_json`
formatters) and `resume_service.py` (`list_for_job`/`get_one(reveal=...)`) were built and gate-green on
branch `feat/phase-5-persist-anonymize-export`, off `main` @ `5945320` (PR #13's merge), tip `02af27c`,
6 commits across three RED→GREEN cycles (RED `3e383ff` → GREEN `33512c2` initial build; RED `8b1597e` →
GREEN `b6b1ec7` cover-letter-chunks security fix; RED `c1e4e04` → GREEN `02af27c` filename
de-anonymization fix). **All three merge-blocking gates are green (reviewer APPROVE, security PASS,
ranking-evals PASS) — re-verified after each post-first-green fix.** **MERGED to `main` via PR #14
(merge `6deade3`), CI green.** The scoring code itself, `stages.py`/`orchestrator.py`, is byte-unchanged
by Phase 5 (this phase is entirely read/export/redaction, no ranking logic). Full write-up:
[docs/activity/phase-5-persist-anonymize-export.md](docs/activity/phase-5-persist-anonymize-export.md);
decisions + residuals: [ADR-011](docs/adr/011-display-redaction-read-export-boundary.md).

**ADR-006 §4's redaction-boundary contract is now enforced in code, not just recorded.** Every blind
read path builds the redacted value first and only then constructs the DTO — `resume_service.get_one`'s
blind branch builds `_blind_parsed(...)` before `ResumeOut(parsed=...)`;
`shortlist_service._row_to_blind_entry` builds `_redact_evidence(...)` before
`ShortlistEntry.model_validate(raw)`; `export_rows`/`_apply_reveal` builds `_redact_evidence_dict(...)`
before the export dict is finalized. Proven by three black-box byte-scan tests (assert the candidate's
real name/email/phone byte-sequence is absent anywhere in the serialized blind output, not just in
specific fields) plus reviewer mutation testing on every redaction call site. **This is display-only
redaction, not at-rest protection** — ADR-007 §6/§7's cleartext-at-rest posture and ADR-010 §6's
extension of it to `shortlist_entries`/`reverse_match_entries` are both unchanged.

**The `ScoreBreakdown` fold read guard (ADR-011 §2) is required to read ANY 4d-written shortlist row.**
`persist_shortlist` (4d, ADR-010 §2) folds `score_structured`/`score_evidence` into the
`score_breakdown` jsonb; `ScoreBreakdown` is `extra="forbid"`, so the read layer
(`_parse_entry_jsonb`) pops those two keys out before `.model_validate()` — without this pop, every row
4d ever wrote raises `ValidationError` on read. Proven against the real jsonb codec in
`test_shortlist_read_export_pg.py`.

**Two post-first-green security/residual fixes (ADR-011 §4/§1):** (1) HIGH: the first GREEN
(`33512c2`) redacted `resumes.parsed.chunks[].text` but not `cover_letter_chunks[].text` — raw letterhead
PII still reachable under blind — written as a failing regression test first (`8b1597e`), fixed by
extending `_blind_parsed` (`b6b1ec7`), mutation-proven. (2) RESIDUAL FIX: the human decided preemptively
to close the `original_filename` de-anonymization vector (a `First_Last_Resume.pdf` identifying a candidate
under blind review) rather than accept it as v1. Fixed by adding `redacted_filename()` helper, wired at
three blind surfaces (`resume_service.get_one`/`list_for_job`, `shortlist_service._apply_reveal` for
csv/json export), returning generic `resume<ext>` under blind; real filename under reveal/non-blind
(RED `c1e4e04` → GREEN `02af27c`, mutation-proven).

**Two hris gaps closed beyond a verbatim port (ADR-011 §3/§5):** `_redact_evidence` now redacts
`cover_letter_evidence[].evidence`/`overall_motivation` in BOTH the read and export paths (hris's
version never did); the name/term redaction regex is now grouped
(`(?<![\w])(?:{alt})(?![\w])`) so a middle name-part can't match inside a longer unrelated word — a
latent hris bug, not a Phase-5-introduced one.

**A LOW residual from the filename fix (not a blocker):** `redacted_filename()` trusts `os.path.splitext`,
so a pathological filename like `cover.Jane_Smith` (no true extension) yields `resume.jane_smith`,
leaking the lowercased suffix. Accepted for v1 (low risk: requires dot-containing name component AND
upload under that exact name). Recommend an extension allowlist + length cap when Phase 6's upload
validation lands.

**One pre-existing CI flake (not introduced by Phase 5):** `test_evals_corpus.py::test_every_threshold_key_is_enumerated_by_both_consumers`
is order-dependent (passes in isolation, can fail under `pytest-randomly`). Flagged by the reviewer as a
separate follow-up chore, not a gate blocker.

**Open human decision, carried forward AGAIN, still unresolved:** `score_education` ignores
`jd.education.fields` (ADR-009 §7, restated ADR-010 §5) — Phase 5 touches no scoring code, so this is
untouched.

**Final gate state, HEAD `02af27c`:** ruff/black/`mypy --strict` clean; **2039 unit tests @ 91.86%
coverage**; **18 integration tests** green vs real Postgres (`test_shortlist_read_export_pg.py`,
`test_resume_read_pg.py`). Reviewer APPROVE (5 mutation obligations fired — full table in the activity
report), security PASS (after the `cover_letter_chunks` fix), ranking-evals PASS (scoring code
byte-unchanged).

**Carried forward into Phase 6:** the `original_filename` open decision (resolved by Phase 5's own
post-first-green fix — see above); the still-open `jd.education.fields` decision; the shortlist-side
`evidence = {}` ambiguity (ADR-010 §2, still unresolved, first touched by this phase's read code but not
fixed); no advisory lock on concurrent shortlist/reverse-match runs (ADR-010 §1) — revisit once Phase 6
ships a user-facing regenerate route; CSV formula/injection in `shortlist_csv`/`shortlist_evidence_csv` —
accepted for v1, one-line fix noted.

### Phase 6 status — DONE, gate-green, PR #15 OPEN (CI running) — read this before starting Phase 7

`core/src/api/deps.py` (new — `require_api_key`/`resolve_actor`/`get_arq`/`log_auth_mode`),
`core/src/api/routes/{jobs,resumes,shortlist}.py` (new — 11 routes), `core/src/services/zip_upload.py`
(new — `expand_zip_entries`/`ZipRejected`), `core/src/services/jd_import_service.py` (new —
`extract_jd_text`) were built and gate-green on branch `feat/phase-6-api-routes`, off `main` @ `6deade3`
(PR #14's merge), HEAD `837de9e`, commit chain: RED `209bff7` → GREEN `bc9a3d6` (initial routes, resumed
mid-build after a session-limit interruption) → RED `1f2b161` → GREEN `344f6bf` (SEC-1/SEC-2/SEC-4
security hardening + exact `fastapi`/`starlette`/`python-multipart` pins) → RED `c75f4a7` → GREEN
`837de9e` (non-ASCII `X-API-Key` 401 generalization + upload file-count-ordering regression pin). **All
three merge-blocking gates green (reviewer APPROVE, security PASS, ranking-evals PASS) — re-verified
after the security-hardening round.** **Opened as PR #15
(https://github.com/humanaxiom/recruiter-assistant/pull/15) on 2026-07-17 after the human check-in;
merge held for go-ahead.** CI (`gates-all`, incl. a live `run_evals.py` re-measurement against Ollama)
is running on the PR. Full
write-up: [docs/activity/phase-6-api-routes.md](docs/activity/phase-6-api-routes.md); decisions +
residuals: [ADR-012](docs/adr/012-api-routes-auth-upload-scope.md).

**Route map:** `POST/GET /jobs`, `GET/PATCH /jobs/{id}`, `PATCH /jobs/{id}/status` (draft→open, the only
status-mutating route, forward-only, 409 on invalid transition), `POST /jobs/jd-extract` (pre-fill
helper, no DB write), `POST/GET /jobs/{id}/resumes`, `GET/PATCH /jobs/{id}/shortlist`, `GET
/jobs/{id}/shortlist/export`, `GET /resumes/{id}`, `POST /resumes/{id}/match-jobs`, `GET
/resumes/{id}/match-results`, `GET /shortlist/{id}`.

**Locked human decisions this phase (ADR-012):** (1) one settings flag `api_key` — empty disables auth
(loud startup warning), non-empty enables fail-closed 401 with constant-time UTF-8-byte comparison;
optional `X-Actor-Name` (128-char cap) populates `created_by`/`uploaded_by`. (2) Upload accepts local
multi-file + zip only — Taleo/CSV-manifest connector pairing explicitly CUT and deferred to a future
"sources/connectors" feature (the user's framing: "Taleo was a shortcut to get sample data … will add
more connectors in the future"); zip expansion mirrors the Phase-3 DOCX-bomb defense (never trusts
`ZipInfo.file_size`, streams real decompressed bytes, path-traversal/extension-allowlist/entry-count/
per-entry/total-size guards, writes nothing on reject). (3) `PATCH /jobs/{id}/status` is the only
status-mutating route. (4) Reverse-match is a subresource of `routes/resumes.py`; **explicitly NO
redaction** on the reverse-match read (the caller owns the résumé they matched — no third party to
protect, unlike every other blind-review-aware read path). (5) `POST /jobs/jd-extract` included.

**Carry-forwards now CLOSED:** `JobOut.blind_review` fail-open (ADR-006 §4 note) — `_row_to_jobout` now
sets it explicitly from the row on every path, reviewer mutation-proved both directions. Redaction
boundary at the HTTP layer (ADR-006 §4 / ADR-011) — read/export routes route straight through to the
already-redacting service functions; security byte-scanned actual serialized HTTP responses.

**Security hardening + accepted residuals (ADR-012):** Fixed SEC-1 (non-ASCII API-key compare crashing
to 500 instead of 401), SEC-2 (upload file-count cap now checked before any file body is read —
regression-pinned), SEC-4 (`X-Actor-Name` 128-char cap); `fastapi`/`starlette`/`python-multipart` pinned
`==` exactly (the route-walker test depends on a FastAPI-internal structure). Accepted-for-v1: SEC-3 (no
LIMIT/OFFSET on shortlist/reverse-match reads, bounded by shortlist size in practice), SEC-5
(`detect_mime`'s `txt` catch-all, intentional), blob-write-inside-transaction (a rollback leaves a
harmless uuid-keyed orphan blob, no orphaned enqueue). Also fixed: a latent `pool.py` bug —
`PoolConnectionProxy[Record]` isn't subscriptable at runtime; under `from __future__ import annotations` +
FastAPI's `eval_str` signature introspection it crashed at route registration the first time any route
actually used `Db` (never true before Phase 6) — fixed with a `TYPE_CHECKING`-gated alias.

**Final gate state, HEAD `837de9e`:** ruff/black/`mypy --strict` clean; **2156 unit tests @ 91.68%
coverage**; **123 integration tests** vs real Postgres+Neo4j+Redis, incl. 12 new Phase-6 ASGI integration
tests (real HTTP through the FastAPI app). Reviewer APPROVE (6 mutation obligations fired), security PASS
(SEC-1/SEC-2 closed on re-audit), ranking-evals PASS (scoring byte-unchanged; CI's `gates-all`
re-measures `run_evals.py` live on the PR).

**Carried forward into Phase 7:** `score_education` ignores `jd.education.fields` (still open,
untouched); `reverse_match_job`'s `allowed_job_ids` filter still `description_parsed IS NOT NULL`, not
`status='open'`, even though a status route now exists (ADR-012 §3 revisits but does not resolve this);
the `redacted_filename` `os.path.splitext` truncation LOW residual (not addressed by Phase 6's upload
validation); no advisory lock on concurrent shortlist/reverse-match runs — a user-facing regenerate route
now exists (`POST /jobs/{id}/shortlist`, `POST /resumes/{id}/match-jobs`), so this question (ADR-010 §1)
is now live, not hypothetical.

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
Phase 2 in docs/adr/006-*.md, Phase 3 in docs/adr/007-*.md, the Phase 4b PII
rearchitecture in docs/adr/008-skill-graph-pii-by-construction.md, the Phase 4c
matching-engine port in docs/adr/009-matching-engine-port.md, the Phase 4d
shortlist/reverse-match write path in docs/adr/010-shortlist-reverse-match-write-path.md,
the Phase 5 display-redaction read/export boundary in
docs/adr/011-display-redaction-read-export-boundary.md, the Phase 6 API-routes
auth/upload scope in docs/adr/012-api-routes-auth-upload-scope.md.

We are porting the resume-ranking feature from C:\repos\hris onto this template
(template-first, filesystem storage instead of MinIO, keep Neo4j, v1 includes
cover-letter/reverse-match/minimal viewer/blind-default). Phases 0, 1, 2, and 3 are
ALL complete and merged to main, CI green: Phase 0 (seed & infra) PR #1, Phase 1
(storage — filesystem BlobStore) PR #2, Phase 2 (schemas) PR #3, Phase 3 (ingest +
parse) PR #6 (merge 49196d7). Phase 4 (Ranking engine) was split into 4 gated
sub-phases and ALL FOUR are now MERGED to main, CI green: 4a (evals corpus)
MERGED (PR #8, merge 875eac2), falsifiability hardening MERGED via PR #10
(merge 464a479); 4b (graph projection) MERGED via PR #11 (merge 68fe821);
4c (matching engine) MERGED via PR #12 (merge fd12d1a); 4d (shortlist +
reverse-match write path) MERGED via PR #13 (merge 5945320) — all three
merge-blocking gates were green (security PASS, reviewer APPROVE, ranking-evals
PASS) AND CI was fully green before each merge.

**4d closed ADR-009's carried "Requirement 1"** — matching_context_from_settings
(src/pipeline/matching/orchestrator.py) is the single call site that builds
MatchingContext from Settings (family_weight, non_matchable_families,
llm_concurrency, evidence_max_tokens, model_gen/emb, git_sha); shortlist_job/
reverse_match_job (src/worker/matching_tasks.py) call it with get_settings()
and pass weights=weights_from_settings(get_settings()) — never DEFAULT_WEIGHTS.
It also shipped src/services/shortlist_service.py's persist_shortlist/
persist_reverse_match (DELETE-first per-run idempotency, mirror-image handling
of score_structured/score_evidence/evidence dictated by the two tables'
different DDL shapes). Full detail: docs/activity/phase-4d-shortlist-writepath.md
and docs/adr/010-shortlist-reverse-match-write-path.md.

**Phase 5 (persist + anonymize + export) is MERGED to main via PR #14
(merge 6deade3), CI green.** ADR-006 §4's redaction-boundary contract is now
ENFORCED IN CODE, not just recorded: every blind read path
(shortlist_service.list_for_job/get_one/export_rows,
resume_service.list_for_job/get_one(reveal=...)) redacts BEFORE building the
DTO, proven by black-box byte-scan tests plus reviewer mutation testing on
every redaction call site. This is display-only redaction, NOT at-rest
protection — ADR-007 §6/§7 and ADR-010 §6's cleartext-at-rest postures are
UNCHANGED. The ScoreBreakdown fold-read guard (ADR-011 §2) pops
score_structured/score_evidence back out of score_breakdown before
model_validate — required to read ANY 4d-written shortlist row. Two
post-first-green fixes landed before merge: a HIGH cover-letter-chunks PII leak
(blind ResumeOut.parsed.cover_letter_chunks[].text still carried raw letterhead
PII) and the original_filename de-anonymization vector (redacted_filename()
now returns generic resume<ext> under blind at three surfaces; real filename
under reveal/non-blind) — both mutation-proven, both merge-blocking gates
re-verified. Full detail: docs/activity/phase-5-persist-anonymize-export.md and
docs/adr/011-display-redaction-read-export-boundary.md.

**Phase 6 (API routes) is COMPLETE and gate-green on branch
feat/phase-6-api-routes** (off main @ 6deade3, HEAD 837de9e), commit chain:
red 209bff7 -> green bc9a3d6 (initial routes, resumed mid-build after a
session-limit interruption) -> red 1f2b161 -> green 344f6bf (SEC-1/SEC-2/SEC-4
security hardening + exact fastapi/starlette/python-multipart pins) -> red
c75f4a7 -> green 837de9e (non-ASCII X-API-Key 401 generalization + upload
file-count-ordering regression pin). **All three merge-blocking gates green
(reviewer APPROVE, security PASS, ranking-evals PASS). Opened as PR #15
(https://github.com/humanaxiom/recruiter-assistant/pull/15) on 2026-07-17 after
the human check-in; merge held for a human go-ahead.** CI (gates-all, incl.
a live run_evals.py re-measurement against Ollama) is running on PR #15. Full
detail: docs/activity/phase-6-api-routes.md and
docs/adr/012-api-routes-auth-upload-scope.md.

**Phase 6 shipped src/api/deps.py (new — require_api_key/resolve_actor/
get_arq/log_auth_mode), src/api/routes/{jobs,resumes,shortlist}.py (new — 11
routes), src/services/zip_upload.py (new — expand_zip_entries/ZipRejected),
src/services/jd_import_service.py (new — extract_jd_text).** The configurable
auth switch is ONE settings flag (settings.api_key): empty disables auth (loud
startup WARNING), non-empty enables fail-closed 401 with constant-time
UTF-8-byte comparison. Upload accepts local multi-file + zip ONLY — the
Taleo/CSV-manifest connector is explicitly CUT and deferred to a future
connectors feature; zip expansion mirrors the Phase-3 DOCX-bomb defense
(streams real decompressed bytes, never trusts ZipInfo.file_size).
PATCH /jobs/{id}/status is the only status-mutating route (forward-only, 409
on an invalid transition) — the first code path in the whole repo that
transitions jobs.status. Reverse-match (POST /resumes/{id}/match-jobs, GET
/resumes/{id}/match-results) is a subresource of routes/resumes.py with
EXPLICITLY NO redaction on the read (the caller owns the résumé they matched).
ADR-006 §4's JobOut.blind_review fail-open note is now CLOSED —
_row_to_jobout sets it explicitly from the row on every path, reviewer
mutation-proved both directions. A latent pool.py bug was also fixed:
PoolConnectionProxy[Record] isn't subscriptable at runtime; under
`from __future__ import annotations` + FastAPI's eval_str signature
introspection it crashed at route registration the first time any route
actually used Db (never true before Phase 6) — fixed with a
TYPE_CHECKING-gated alias.

**Phase 6 final state (HEAD 837de9e):** 2156 unit tests @ 91.68% coverage;
123 integration tests vs real Postgres+Neo4j+Redis, incl. 12 new Phase-6 ASGI
integration tests. Reviewer APPROVE (6 mutation obligations fired), security
PASS (SEC-1/SEC-2 closed on re-audit), ranking-evals PASS (scoring
byte-unchanged).

**Your next action is Phase 7 (ranking-quality evals fixtures + a minimal
read-only Flask viewer)** — NOT the evals corpus (done), NOT graph projection
(done), NOT the matching engine (done), NOT the write path (done, merged), NOT
persist+anonymize+export (done, merged), NOT API routes (done, gate-green,
PR #15 OPEN with CI running). Confirm PR #15's merge status (gh pr view 15)
before building on it. Once Phase 6 is merged, Phase 7 ships the precision@k /
evidence-verification-rate fixture harness (already partially live via 4c's
run_evals.py wiring) and the minimal Flask viewer (read-only, consumes the
Phase 6 routes). Run the per-phase subagent loop (planner -> tester ->
data-pipeline coder -> reviewer + security + ranking-evals -> docs) on a
feat/phase-7-... branch.

Subagent model tiering is in effect (docs/SUBAGENT_MODEL_POLICY.md): the three
merge-blocking gates (reviewer/security/ranking-evals) run on opus; producers
(data-pipeline/planner/tester/coder) default to sonnet; docs on haiku. Defaults
live in .claude/agents/*.md frontmatter.

**Open human decision, carried forward across 4c/4d/5/6, still UNRESOLVED:**
score_education ignores jd.education.fields (ADR-009 §7, restated ADR-010 §5,
ADR-011) — either extend the scorer or drop fields from the JD contract.
Neither 4c, 4d, 5, nor 6 touched stages.py's scoring code, so this remains
exactly as open as it was after 4c. Do not resolve this silently while
building Phase 7. Also carried: reverse_match_job's allowed_job_ids filter is
still description_parsed IS NOT NULL, not status='open', even though Phase 6
added a status route (ADR-012 §3 revisits but does not resolve this); no
advisory lock on concurrent shortlist/reverse-match runs now that a
user-facing regenerate route exists (ADR-010 §1, now live not hypothetical).

Note: no local Python — verify gates in the python:3.11-slim Docker container per
HANDOFF.md. Check in with me before opening the Phase 6 PR or any Phase 7 PR.

See the "Phase 3 starting map (verified)" subsection above (historical) and
docs/adr/007 for how the ingest/parse layer Phase 4 builds on was ported.
```
