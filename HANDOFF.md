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

**Done:** repo created + `origin` repointed + pushed; 4 decisions locked; plan-of-record and the `data-pipeline` + `ranking-evals` subagents committed. **Phases 0, 1, 2, and 3 are all complete and merged to `main`, CI green:** Phase 0 (seed & infra) via PR #1 (merge `8b2b47c`), Phase 1 (storage) via PR #2 (merge `f7e7cbe`), Phase 2 (schemas) via PR #3 (merge `cefd545`), Phase 3 (ingest + parse) via PR #6 (merge `49196d7`). Phases 0–2 merged 2026-07-11; Phase 3 merged 2026-07-12. **Phase 4 (Ranking engine) is ✅ complete — all 4 gated sub-phases merged to `main`.** Sub-phase **4a (evals corpus) is MERGED to `main` via PR #8** (merge `875eac2`), CI green, 2026-07-12, and its **falsifiability hardening is also MERGED via PR #10** (merge `464a479`), CI green. **Sub-phase 4b (graph projection) MERGED to `main` via PR #11** (merge `68fe821`), CI green. **Sub-phase 4c (matching engine) is MERGED to `main` via PR #12** (merge `fd12d1a`), CI green. **Sub-phase 4d (shortlist + reverse-match write path) is MERGED to `main` via PR #13** (merge `5945320`) this session, CI green. **Phase 5 (persist + anonymize + export — read/list/get/export + display redaction) is MERGED to `main` via PR #14** (merge `6deade3`), CI green. **Phase 6 (API routes — job create/read/list/status, résumé upload/read/list, shortlist generate/list/get/export, reverse-match, configurable auth) is COMPLETE and MERGED to `main` via PR #15** (squash merge `e910669`, CI `gates-all` fully green, merged 2026-07-17), tip `837de9e` — all three merge-blocking gates were green (reviewer APPROVE, security PASS, ranking-evals PASS) AND CI's `gates-all` (offline `run_evals.py` running inside the gated unit suite — CI never calls a model endpoint; inference is host-only by design) went fully green before merge. **Phase 7 (evals + minimal Flask viewer) is now MERGED to `main` via PR #16** (squash merge `1039e5c`, 2026-07-17), off `main` @ `e910669`, pre-merge tip `92ca4ae` — all three merge-blocking gates were green (reviewer APPROVE, security PASS, ranking-evals PASS). Branch `feat/phase-7-evals-viewer` is deleted (local + remote). **Post-review addition (2026-07-17): the live end-to-end eval against the real stack — previously recorded as deferred — was reversed, built, run, and PASSED (reproduced identically twice), and was a prerequisite for merging PR #16.** **All seven phases of the locked v1 extraction-plan scope (0–7) are now merged to `main`, CI green — there is no Phase 8.** See "Phase 4a status", "Phase 4b status", "Phase 4c status", "4d status", "Phase 5 status", "Phase 6 status", and "Phase 7 status" below.

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
     ruff check --fix src frontend tests && black src frontend tests && \
     ruff check src frontend tests && black --check src frontend tests && mypy src frontend --strict && \
     pytest tests/unit --cov=src --cov=frontend --cov-fail-under=80 -q"
  ```
  (The `--fix` + `black` write pass auto-formats; the following `check`/`--check` then verify. **This must
  match `Makefile:27`'s `mypy src frontend --strict` exactly** — an earlier version of this snippet only
  ran `mypy src --strict`, which let a `core/frontend/` type error slip past a subagent's self-check
  during the FU-4 session; the real gate has always covered both trees.)
- **Docker is available.** Integration/e2e that need live Postgres/Neo4j/Redis run via Docker/testcontainers (CI does `gates-all`). For testcontainers in the container, mount the docker socket + install `docker.io` + set `TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal`.
- **Two container gotchas:** (1) prefix `docker run` with `MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'` or Git Bash mangles `/w/core`→`W:/core`. (2) stale `__pycache__` on the Windows bind mount can mask source edits (coarse mtime → reused bytecode); when re-running pytest after editing source, add `PYTHONDONTWRITEBYTECODE=1` or clear `__pycache__`.
- **No git identity configured** — commit with inline `git -c user.name='Adam Salah' -c user.email=<owner-email> commit …` (the real address is in the owner's git config / the owner knows it — kept as a placeholder here per the security-flagged chore above: this repo's own PII invariant bans real personal emails in committed text, and this file is committed text).
- **Windows 11**, PowerShell primary; Bash tool available (Git Bash). `.claude/settings.json` hooks shell to `bash`, so Git Bash must be on PATH.
- **`gh` CLI** authed as `adamsalah13`, **admin on the `humanaxiom` org**. Pushing to `humanaxiom/recruiter-assistant` is authorized.
- Template Python is **3.11**; hris is **3.12**. Keep 3.11 (the template's) and port hris code to it — nothing in the ranking core needs 3.12.
- Model note: this is Claude Opus 4.8 (1M context); the latest models are the Claude 5 family / Opus 4.8 / Haiku 4.5.
- **`core/requeue.py` is untracked stray operational scratch work**, found in the working tree during the
  FU-4 session — a hardcoded job UUID, blocking `urllib` inside an async function, `localhost:8000`
  instead of going through `settings`, and no tests. It is not part of any branch and was deliberately
  left untracked. A resuming session should not mistake it for product code, and should not `git add` or
  commit it without first rewriting it to the codebase's actual conventions.

## Subagent roster (`.claude/agents/`)

Build harness (from the template): `planner`, `tester`, `coder`, `reviewer`, `security`, `docs`.
Domain additions (this project): **`data-pipeline`** (ranking coder with the invariants baked in) and **`ranking-evals`** (merge-blocking quality gate: precision@k, evidence-verification rate = 1.0, PII-leak check).

Per-phase flow: planner → tester (+ evals fixture) → data-pipeline coder (ReviewLoop, ≤5 iters) → reviewer + security + ranking-evals (all merge-blocking) → docs. `make gates` green before the next phase.

**Subagent model tiering ([docs/SUBAGENT_MODEL_POLICY.md](docs/SUBAGENT_MODEL_POLICY.md)):** cheap producers + strong verifiers. The three merge-blocking gates (`reviewer`, `security`, `ranking-evals`) run on **opus** and are never downgraded; producers (`data-pipeline`, `planner`, `tester`, `coder`) default to **sonnet**; `docs` runs on **haiku**. Defaults are in each `.claude/agents/*.md` frontmatter. The coordinator overrides per-call: `data-pipeline` UP to `opus` for diffs touching the 4-stage ranking algorithm / evidence verifier / PII crypto / Neo4j scoring; `docs` UP to `sonnet` for load-bearing handoff/plan refreshes; `coder`/`Explore` DOWN to `haiku` for mechanical fixes / lookups. Quality holds because every producer's diff passes the opus-tier gates + CI before merge.

## Non-negotiables (from CLAUDE.md)

Never commit to `main` for feature work (branch `agent|feat|fix|chore/<slug>`); TDD (failing tests first); offline only (no cloud endpoints — local Ollama/OpenAI-compatible client); config via settings; a single red gate = not done. Privacy: PII never enters embeddings; anonymization non-destructive; PIPEDA/FIPPA.

## v1 status — all phases merged, plan complete (read this first)

**Phase 6 is MERGED** (PR #15, squash merge `e910669`, CI `gates-all` fully green, 2026-07-17). **Phase 7
(a minimal read-only Flask viewer over the Phase 6 API, the gate-scope fix, and the live end-to-end eval)
is MERGED to `main` via PR #16** (squash merge `1039e5c`, 2026-07-17) — all three merge-blocking gates
were green (reviewer APPROVE, security PASS, ranking-evals PASS) on pre-merge tip `92ca4ae`. Branch
`feat/phase-7-evals-viewer` is deleted (local + remote). **The live end-to-end eval — recorded below as
originally deferred — was reversed, built, run, and PASSED (reproduced identically twice against a real
stack), and was a prerequisite for merging PR #16.** See "Phase 7 status" below for the full write-up (the
viewer's blind-only posture, the gate-scope widening to `core/frontend/`, the confirmation that the
evals-fixtures line item was already satisfied in 4a/4c, and the live end-to-end eval's build-run-PASS).

**This is the last phase.** `docs/EXTRACTION_PLAN.md`'s phase table ends at Phase 7 — the locked v1 scope
(the plan's four decisions) is now fully delivered, all seven phases merged, CI green. There is **no Phase
8**. See "Next session" below for what a human might scope next — it is a list of options, not a
to-do list to auto-start. The full historical resume trail is retained below for context.

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
MERGED to `main` via PR #15** (squash merge `e910669`, off `main` @ `6deade3`, tip `837de9e`) — all three
merge-blocking gates green (reviewer APPROVE, security PASS, ranking-evals PASS) AND CI's `gates-all`
went fully green before merge. See "Phase 6 status" below for the auth switch, the upload/zip scope, the
status-transition route, the reverse-match-no-redaction decision, the security hardening (SEC-1/2/4), and
the `pool.py` latent-bug fix. **Phase 7 (evals + minimal Flask viewer) is MERGED to `main` via PR #16**
(squash merge `1039e5c`, 2026-07-17) — the live end-to-end eval (previously deferred, then built, run, and
PASSED — see "Phase 7 status" below) was a prerequisite for that merge and passed.
`docs/EXTRACTION_PLAN.md`'s phase table ends at Phase 7 —
**all seven phases are now merged and the extraction plan's locked v1 scope is complete.** See "Phase 7
status" below for the full write-up, and "Next session" below for what, if anything, a human might scope
next as a follow-up chore rather than a new phase.

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
merge-blocking gates were green (reviewer APPROVE, security PASS, ranking-evals PASS) AND CI's
`gates-all` (offline `run_evals.py` running inside the gated unit suite — CI never calls a model
endpoint) went fully green — PR #13 was MERGED to `main` (merge `5945320`) this session.** The scoring
code itself, `stages.py`/
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

### Phase 6 status — DONE, MERGED to `main` via PR #15 (merge `e910669`) — read this before starting Phase 7

`core/src/api/deps.py` (new — `require_api_key`/`resolve_actor`/`get_arq`/`log_auth_mode`),
`core/src/api/routes/{jobs,resumes,shortlist}.py` (new — 11 routes), `core/src/services/zip_upload.py`
(new — `expand_zip_entries`/`ZipRejected`), `core/src/services/jd_import_service.py` (new —
`extract_jd_text`) were built and gate-green on branch `feat/phase-6-api-routes`, off `main` @ `6deade3`
(PR #14's merge), tip `837de9e`, commit chain: RED `209bff7` → GREEN `bc9a3d6` (initial routes, resumed
mid-build after a session-limit interruption) → RED `1f2b161` → GREEN `344f6bf` (SEC-1/SEC-2/SEC-4
security hardening + exact `fastapi`/`starlette`/`python-multipart` pins) → RED `c75f4a7` → GREEN
`837de9e` (non-ASCII `X-API-Key` 401 generalization + upload file-count-ordering regression pin). **All
three merge-blocking gates green (reviewer APPROVE, security PASS, ranking-evals PASS) — re-verified
after the security-hardening round.** Opened as PR #15
(https://github.com/humanaxiom/recruiter-assistant/pull/15) on 2026-07-17 after the human check-in;
**CI's `gates-all` (offline `run_evals.py` running inside the gated unit suite — CI never calls a model
endpoint; inference is host-only by design) went fully green, and PR #15 was squash-merged to `main`
(merge `e910669`) on 2026-07-17.** Full
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
(SEC-1/SEC-2 closed on re-audit), ranking-evals PASS (scoring byte-unchanged; CI's `gates-all` runs the
offline `run_evals.py` stand-in inside the gated unit suite — no live Ollama call, by design).

**Carried forward into Phase 7:** `score_education` ignores `jd.education.fields` (still open,
untouched); `reverse_match_job`'s `allowed_job_ids` filter still `description_parsed IS NOT NULL`, not
`status='open'`, even though a status route now exists (ADR-012 §3 revisits but does not resolve this);
the `redacted_filename` `os.path.splitext` truncation LOW residual (not addressed by Phase 6's upload
validation); no advisory lock on concurrent shortlist/reverse-match runs — a user-facing regenerate route
now exists (`POST /jobs/{id}/shortlist`, `POST /resumes/{id}/match-jobs`), so this question (ADR-010 §1)
is now live, not hypothetical.

### Phase 7 status — MERGED via PR #16 (squash `1039e5c`), CI green — v1 extraction plan complete

`core/frontend/api_client.py` (new — sync `httpx` wrapper: `build_client` + one fn per Phase-6 route +
`BackendError`/`NotFound`/`BackendUnavailable`), `core/frontend/app.py` (extended from a `/health`-only
stub with routes `/`, `/jobs/<uuid>`, `/jobs/<uuid>/shortlist`, `/shortlist/<uuid>`, `/resumes/<uuid>`,
`/resumes/<uuid>/match-results`, `/jobs/<uuid>/shortlist/export`), `core/frontend/templates/*.html` (new,
server-side Jinja2, autoescaped) were built and gate-green on branch `feat/phase-7-evals-viewer`, off
`main` @ `e910669` (PR #15's merge), pre-merge tip `92ca4ae`, commit chain: `55ee0a0` docs (interim
HANDOFF/plan stamp) → `942e8f5` red → `f28c22e` green (the viewer + client + gate-scope fix) → `92ca4ae`
refactor/fix (post-review security findings closed). **All three merge-blocking gates were green (reviewer
APPROVE, security PASS, ranking-evals PASS).** **MERGED to `main` via PR #16, squash commit `1039e5c`,
2026-07-17, CI green.** Branch `feat/phase-7-evals-viewer` is deleted (local + remote). Full write-up:
[docs/activity/phase-7-evals-viewer.md](docs/activity/phase-7-evals-viewer.md);
decisions + residuals: [ADR-013](docs/adr/013-phase7-evals-viewer.md).

**Gate-scope fix, the other half of this phase.** `Makefile` (`gates`/`gates-fast`) and
`.github/workflows/ci.yml` (`static`/`unit` jobs) are widened so ruff/black/mypy/coverage now cover
`core/frontend/` alongside `core/src`/`core/tests` — previously the frontend directory was invisible to
every quality gate (it is a sibling of `core/src/`, not nested under it). A meta-test
(`core/tests/unit/test_gates_cover_frontend.py`) pins this so it can't silently regress.

**Locked human decisions this phase (ADR-013):** (1) blind-only viewer for v1 — no reveal control
anywhere; shortlist list/detail reads are unconditionally blind (`api_client.list_shortlist`/
`get_shortlist_entry` take no `reveal` param at all) and the résumé route hardcodes `reveal=False`,
ignoring any browser-supplied `?reveal=`. (2) the blind résumé page is structurally PII-incapable —
`resume_detail.html` has no branch that renders `candidate.name/email/phone/location` at all, closing a
latent path that had been gated only on the backend's `blinded` flag (which ADR-012 notes had a fail-open
history). (3) gate scope widened to `core/frontend/`, pinned by a meta-test, rather than a second gate
suite. (4) no new evals fixtures this phase — the plan's Phase 7 evals line item (precision@k,
evidence-verification rate) was already satisfied by 4a (corpus) + 4c (live orchestrator wiring);
`run_evals.py::main()` already runs inside the gated unit suite. (5) a live end-to-end eval (the 4a/4c
corpus run through the real pipeline, re-checking thresholds against persisted rows) was originally
recorded as deferred — it needs a reachable host Ollama + `docker compose up`, which CI does not provide by
design — but that decision was **reversed on 2026-07-17**: the human un-deferred it and made it a
prerequisite for merging PR #16. It has since been **built, run, and PASSED**, reproduced identically
twice against a real stack (real `nomic-embed-text` embeddings, real Neo4j, real `shortlist_job`, real
Postgres persistence). See "Live end-to-end eval — built, run, PASS (post-review addition)" immediately
below.

**Live end-to-end eval — built, run, PASS (post-review addition, 2026-07-17).**
`core/tests/evals/run_evals_live.py` (new, 812 lines) + `core/tests/unit/test_evals_live_metrics.py` (new,
16 offline tests) were built after PR #16 was opened. The corpus is pre-parsed by design (4a fixed the
parsed representation to isolate ranking from non-deterministic LLM parsing — no raw docs exist in the
corpus), so the harness seeds the pre-parsed corpus at the **post-parse boundary** (a `jobs` row + 20
`resumes` rows with `parsed` jsonb, PII encrypted via the real `pii.py` path, and `job.parsed`/
`resume.parsed` outbox events carrying real `nomic-embed-text` embeddings through the production embed
boundary with PII redaction), then drives the real `project_to_graph` (Neo4j) → real `shortlist_job` →
reads the persisted `shortlist_entries` → evaluates every `thresholds.toml` gate, reusing
`run_evals.load_corpus`/`load_thresholds`/`_labels` and the real `stages.verify_evidence` + real redaction
functions. Ran against a remote Ollama with the calibrated models (`nomic-embed-text` + `gpt-oss:20b`); the
local metal host lacked them. Run via `docker compose ... exec -T api python tests/evals/run_evals_live.py`
against a stack pointed at that Ollama. **Verified results, reproduced exactly on two independent runs,
exit 0 both times:** `precision@5 = 1.000`; adversarial bait (r09) ranked 14th, outside k=5, no
`must_not_surface` offenders; `evidence.verification_rate = 78/78 = 1.000`;
`evidence.min_completeness_in_topk = 5/5 = 1.000`; `evidence.gold_recall = 4/4 = 1.000`;
`evidence.negative_evidence_must_fail`: 4 fabrications, all scrubbed; `ordering_controls` all pass
(education +0.0411, overqual +0.0120, motivation +0.0900, skill_missing_must +0.1460, recency +0.1440);
`pii.embedding_input_pii_free`: 0/20; `pii.exported_output_pii_free`: 0/top-5; determinism: order
identical, `max_rank_delta=0`, `max_score_delta=0`. The pure metric layer (`eval_*`) is offline-unit-tested
(16 tests, bad rankings FAIL); the live orchestration script lives under `tests/evals` (not collected by
`pytest tests/unit`), so CI stays green with no Ollama. Offline suite: **2245 unit tests @ 91.67%**
(was 2229; +16), ruff/black/mypy clean. **Deviations, recorded honestly:** ADR-013 §5's literal "HTTP
upload" wording is intentionally not followed (seeding at the post-parse boundary is what keeps thresholds
meaningful); `project_to_graph`/`shortlist_job` ran with a direct `ctx`, not enqueued on the worker; the
second determinism run used a warm Redis embed cache (embed half compares cache to itself); the
`jd.education.fields` open decision remains unresolved and untouched. Full detail:
[ADR-013 §5](docs/adr/013-phase7-evals-viewer.md) and
[docs/activity/phase-7-evals-viewer.md](docs/activity/phase-7-evals-viewer.md)'s "Live end-to-end eval
(post-review addition)" section.

**Accepted residual (ADR-013):** `_unavailable(exc: BackendUnavailable)` in `app.py` has an unused `exc`
parameter (its value is no longer rendered after the security fix below made the error page fully
static). Kept because the signature documents the handler's intent; ruff's unused-arg rules aren't
enabled in this repo. Security finding #2 (an earlier draft rendered the raised exception's message,
risking a backend-URL leak to the browser) is CLOSED, not a residual.

**Final gate state, HEAD `92ca4ae`:** ruff/black/`mypy src frontend --strict` clean; **2229 unit tests @
91.67% coverage** (frontend now format/type/coverage-gated for the first time). Reviewer APPROVE, security
PASS (both hardening findings closed — the structurally-PII-incapable résumé template and the fully
static error page), ranking-evals PASS (scoring code byte-unchanged; offline corpus 352 tests green,
`run_evals.py::main()` exits 0). **Post-review (2026-07-17):** `test_evals_live_metrics.py`'s 16 new
offline tests bring the count to **2245 unit tests @ 91.67% coverage**, ruff/black/mypy still clean — see
"Live end-to-end eval — built, run, PASS (post-review addition)" above.

**Carried forward, still unresolved:** `score_education` ignores `jd.education.fields` (ADR-009 §7,
restated through ADR-012 — untouched, scoring byte-unchanged); `reverse_match_job`'s `allowed_job_ids`
filter still `description_parsed IS NOT NULL`, not `status='open'`; no advisory lock on concurrent
shortlist/reverse-match runs (the viewer is read-only, so this is unaffected). **Resolved post-review
(2026-07-17):** the live end-to-end eval — the one genuine verification gap Phase 7 originally left open —
was built, run, and PASSED against the real stack (reproduced twice); see "Live end-to-end eval — built,
run, PASS (post-review addition)" above and ADR-013 §5. It does not exercise the Phase 6 HTTP upload/parse
routes themselves or the arq/Redis queue hop — those remain covered only by Phase 3/4/6's own tests.

**Documentation correction made this phase:** prior HANDOFF/plan text repeatedly said CI runs "a live
`run_evals.py` re-measurement against Ollama" for Phases 4d/5/6. That was inaccurate — CI's `gates-all`
runs the offline deterministic stand-in harness (`run_evals.py::main()`) inside the gated unit suite; it
never calls Ollama (`.github/workflows/ci.yml`'s own comment: "CI never calls a model endpoint; inference
is host-only by design"). Corrected everywhere it appeared in this file and in
`docs/EXTRACTION_PLAN.md`.

**Merge status:** PR #16 was gated in CI (`gates-all` fully green) and **squash-merged to `main` as
`1039e5c`** on 2026-07-17 — the live end-to-end eval (above) was the merge prerequisite and had already
passed, reproduced twice. `docs/EXTRACTION_PLAN.md`'s phase table ends at Phase 7, and it is now fully
merged: **the extraction plan's v1 scope (as locked in the plan's four decisions) is complete.**

### Workflow UI status — DONE, gates green — a post-v1 feature, NOT "Phase 8"

`core/frontend/app.py` (extended with 9 new write-capable routes: résumé upload, job status transitions,
blind-review toggle, shortlist generation, and three 3-second HTMX poll fragments — `parse-status`,
`resumes-table`, `shortlist-cards`), `core/frontend/templates/*.html` (rewritten as a full recruiter
workflow — create job → upload → generate shortlist → review → export — replacing Phase 7's read-only
pages), `core/frontend/static/app.css` (new, hand-authored) + `core/frontend/static/vendor/htmx.min.js`
(new, vendored htmx 2.0.4 + `htmx.LICENSE`), `core/src/services/job_service.py`
(`update_job`, new) + `core/src/api/routes/jobs.py` (`PATCH /jobs/{id}`, new) were built on branch
`feat/workflow-ui`. It reproduces the recruiter workflow that exists in the source `hris` Next.js
frontend, scoped strictly to **job → résumé → shortlist** — the review/decision workflow, JD-Harmonizer,
comment threads, admin console, and CAS auth all stay cut, per the plan's original keep/cut boundary.
Full detail: [ADR-014](docs/adr/014-workflow-ui.md).

**This is a new post-v1 feature, not a numbered phase.** `docs/EXTRACTION_PLAN.md`'s phase table still
ends at Phase 7 and stays closed — do not call this "Phase 8" anywhere.

**Stack decision (ADR-014 §1):** Flask + HTMX (vendored, served locally, no CDN) + a hand-authored
`app.css` utility stylesheet. Deliberately **not** a Tailwind/Node build — there is no Node toolchain in
the container, and CLAUDE.md locks the frontend stack at Flask. No `tailwind.config.js` exists; a future
contributor should not look for one. This keeps the app offline/air-gapped and keeps every redacted
response assembled server-side in Python — HTMX only ever swaps in Jinja2-rendered fragments, never raw
JSON assembled client-side.

**Blind-only, by construction, carried forward from ADR-013 (§2):** the Flask layer never forwards
`reveal` to the backend, even though this is now a write-enabled surface. `get_resume` stays hardcoded
`reveal=False`; `list_shortlist`/`get_shortlist_entry` take no `reveal` parameter at all; the three export
formats (csv/evidence-csv/json) proxy the backend's `reveal=False` default without exposing a browser-side
way to flip it. `resume_detail.html` still has no template branch capable of rendering candidate name/
email/phone/location — proven by structural byte-scan tests, not merely gated on the backend's `blinded`
flag.

**One backend addition — the only `core/src/` change in this feature:** `PATCH /jobs/{id}`
(`job_service.update_job`), needed for the blind-review toggle. Allowlist-guarded partial update built
from `payload.model_dump(exclude_unset=True)` (an omitted field means "unchanged," not "set to null" —
matters for `blind_review: bool | None`, since `False` is a legitimate deliberate value); `status` remains
unwritable through this route (`JobUpdate` carries no `status` field, `extra="forbid"` 422s a client that
tries) — every status change still goes through the Phase 6 state-machine-guarded
`PATCH /jobs/{id}/status`. `stages.py`/`orchestrator.py` and every other Phase 6 route are byte-unchanged.

**Screens:** jobs list (create-job form with JD-file auto-extract + blind-review checkbox +
status-filter pills) → job detail (3s-polled "parsing…" badge, status-transition buttons with draft→open
disabled until parsed, blind-review toggle, consent-gated résumé upload + 3s-polled status-pill résumé
table) → résumé detail (blind banner, recency-coloured skill chips, experience/education/cover letter, no
PII code path) → shortlist (Generate/Regenerate button that polls until ranked, per-candidate cards with
rank/`score_final × 100`/five sub-score tiles/matched-missing skill chips/evidence panel with cited
quotes, three anonymized export formats).

**Gate outcome:** GREEN — ruff/black/`mypy --strict` clean; **2364 unit tests @ 91.30% coverage**; all
screens live-verified end-to-end against the real running stack (create job → LLM parse → upload →
shortlist → ranked cards, confirmed blind throughout). Reviewer **APPROVE** (after fixing one Major: the
export route had silently dropped the `?format=` query parameter, always exporting csv — now reads and
validates it against the allowed set); security **PASS** after two fixes: `MAX_CONTENT_LENGTH` (210 MiB,
sized off the backend's 10 MB/file × 20-file caps) added so an oversized multipart request 413s before
Flask buffers it into process memory, and an explicit `httpx.Timeout` (30s/5s connect) added to the
`api_client` build so outbound calls never rely on `httpx`'s implicit no-timeout default.

**Accepted LOW residual (ADR-014, documented not fixed):** the create/upload error paths render the
backend's 4xx `detail` verbatim (Jinja2-autoescaped). Today the backend only ever puts field-level
validation text there — no PII, no raw upload content — accepted for v1. If a future backend change ever
surfaces something PII-bearing or attacker-controlled in `detail`, map it to fixed friendly messages
instead of rendering verbatim.

**Deferred, not built — the reverse-match UI is now a concrete follow-up.** A "find matching jobs"
trigger button on the résumé-detail screen was scoped as an optional slice (S9) and cut for time. The
backend endpoints already exist and are unchanged (`POST /resumes/{id}/match-jobs`,
`GET /resumes/{id}/match-results`, both Phase 6), and the old `match_results.html` view remains, already
wired to `app.py::resume_match_results`. Wiring a trigger button that calls the existing
`api_client.get_match_results`/a thin new POST wrapper is a clean, low-risk follow-up needing no backend
change.

**Pre-existing, out of scope:** weak/empty `flask_secret_key`/`api_key` defaults (env-overridable) —
hardening backlog, inherited from Phase 6/7, not introduced or worsened by this feature.

## Next session

**The v1 extraction plan is fully delivered, and FIVE post-v1 features have shipped on top of it: the
Workflow UI, then FU-1/FU-2/FU-3.** All seven plan phases (0–7) are merged to `main`, CI green: Phase 0
(PR #1, `8b2b47c`), Phase 1 (PR #2, `f7e7cbe`), Phase 2 (PR #3, `cefd545`), Phase 3 (PR #6, `49196d7`),
Phase 4a–4d (PR #8/#10/#11/#12/#13), Phase 5 (PR #14, `6deade3`), Phase 6 (PR #15, `e910669`), Phase 7
(PR #16, `1039e5c`). **There is no Phase 8** — `docs/EXTRACTION_PLAN.md`'s phase table intentionally ends
at Phase 7. **Post-v1, all merged to `main`, CI green:** the **Workflow UI** (PR #18, `3eba9cf`, ADR-014),
**FU-2** evidence chunk expansion (PR #19, `8d7ce0b`, ADR-015), **FU-1** audited reveal + cover-letter file
upload (PR #20, `bc055f4`, ADR-016), and **FU-3** bulk ingest (PR #21, `e033d31`, ADR-017). Also merged
this session: **PR #22** (`chore/fu3-merged-docs`, squash merge `2fc3d4f`) — the docs-only PR marking
FU-1/FU-2/FU-3 merged. **FU-4 — RBAC is MERGED to `main` via PR #23 (merge `961caab`, 2026-07-21),
CI green on all five gates** — `main` is now at `961caab`. It was the last item planned as of
2026-07-19; **FU-5/FU-6/FU-7 were scoped on 2026-07-20** (see "Queued next work"). Each post-v1 feature
is a named feature, not a numbered phase.

### Workflow-UI enhancements — FU-1, FU-2, FU-3, FU-4 ✅ ALL MERGED

The three user-requested enhancements (built order FU-2 → FU-1 → FU-3) are **all merged to `main`, CI
green**: **FU-2** evidence chunk-id expansion (PR #19, merge `8d7ce0b`, ADR-015), **FU-1** audited reveal +
cover-letter file upload + reveal-on-shortlist-card (PR #20, merge `bc055f4`, ADR-016), **FU-3** bulk ingest
(PR #21, merge `e033d31`, ADR-017), **FU-4** RBAC (PR #23, merge `961caab`, ADR-018). FU-4 is no longer
the last planned item: **FU-5/FU-6/FU-7 were scoped on 2026-07-20** — see "Queued next work" further
down. The original per-FU detail is retained below for history; each
of FU-1/FU-2/FU-3 is DONE, FU-4 is pending merge.

**Blind-review model (user-confirmed 2026-07-17, matches hris) — now LIVE:** blind is ON at every step by
default; identity is exposed only through an explicit, **audited** reveal (FU-1, shipped: `reveal_audit`
sink + `POST /resumes/{id}/reveal` + a "Reveal identity (audited)" button on the résumé page AND each
shortlist card). **RBAC is a SEPARATE task** (FU-4 below) — mandated in the early design, still not
implemented; FU-1's reveal shipped audited-first, and RBAC (who is *permitted* to reveal, closing FU-1
residuals R1/R2/R5 in ADR-016) layers on top.

- **FU-1 — Audited reveal — ✅ MERGED (PR #20, merge `bc055f4`, ADR-016).** Shipped: `reveal_audit`
  append-only sink + `POST /resumes/{id}/reveal` (records actor/resume/timestamp, returns the un-blinded
  résumé) + a "Reveal identity (audited)" button on the résumé detail AND each shortlist card (`context`
  distinguishes origin). Also folded into #20: cover-letter **file** upload (blob-stored, worker-parsed).
  Residuals R1 (no RBAC), R2 (unaudited `GET ?reveal=true` still exists), R5 (no CSRF token) are closed by
  FU-4. Original spec below (now delivered):
  Clicking the candidate label
  ("Candidate A") on a shortlist card reveals the full, un-blinded résumé (name/email/phone/employers/
  schools/grad years). **This deliberately reverses the blind-only frontend posture** locked in
  ADR-013/014 — the user reversed that decision on 2026-07-17. Backend already supports it:
  `GET /resumes/{id}?reveal=true` decrypts PII (the frontend currently hardcodes `reveal=False` and
  never forwards reveal). Build: a reveal action on the card/entry (shortlist entry → `resume_id` is the
  link) that calls `get_resume(reveal=True)` and renders the un-blinded record; **blind stays the
  default, reveal is opt-in.** MUST be **audited** (log actor + `resume_id` + timestamp on every reveal —
  hris did this; recruiter-assistant has no reveal-audit sink yet, so add an append-only audit table/log).
  Record the reversal + the audit control in a new ADR (015). Keep the blind byte-scan tests on the
  default (non-reveal) paths.

- **FU-2 — Evidence chunk expansion — ✅ MERGED (PR #19, merge `8d7ce0b`, ADR-015).** Shipped: a pure
  `_resolve_chunk_context` resolver + an `evidence_context` CSV column + a source-text collapsible in the
  shortlist cards, redacted under blind/anon (resolve-before-pseudonym ordering) and full under reveal.
  Original spec below (now delivered):
  The evidence export (`shortlist_evidence_csv`)
  and the UI evidence `<details>` panel show opaque `evidence_chunk_ids` (`c_001`). Resolve each id → its
  real chunk text from `resumes.parsed.chunks[]` (`id → {section, text}`) and show that instead of / next
  to the id. **Redaction-aware**: under anonymized export / blind view the expanded chunk text runs
  through the same display redaction as everything else; under reveal it's full text. Backend: the export
  path (`shortlist_service.export_rows` / `shortlist_evidence_csv`) needs the résumé chunks joined in to
  resolve ids — today it likely doesn't; add a chunk-id→text resolver with redaction applied.

- **FU-3 — Bulk ingest — ✅ MERGED (PR #21, merge `e033d31`, ADR-017).** All gates green (reviewer APPROVE,
  security PASS, ranking-evals PASS), live-verified against the `hris/fixtures/llm_split` sample PDFs.
  Five slices shipped: (1) shortlist "Generating… forever" fix + Generate-gated-until-parsed + parse hint;
  (2) per-résumé cover-letter pairing by filename convention (new pure `bulk_ingest_service.py`) + results
  summary; (3) `manifest.json` pairing (precedence over convention); (4) bulk JD upload (`POST /jobs/bulk`,
  `create_jobs_bulk`, CSV manifest, `description_sha256` dedup via the repo's first idempotent `ALTER
  TABLE`); (5) reverse-match UI (candidate→jobs, POST-only trigger + bounded poll + rows link to job).
  Gates: reviewer APPROVE, security PASS (file-count-cap parity fixed), ranking-evals PASS (scoring
  byte-unchanged); ~2528 unit @ 91.37%. Live-verified against the `hris/fixtures/llm_split` sample PDFs.
  Decisions + accepted residuals: **ADR-017**. Everything below is the original plan detail, now delivered.

- **FU-3 (original plan) — Bulk ingest (local, offline): many résumés + per-résumé cover letters + bulk JDs.** The
  clarified shape of the "connectors" ask. Model: **candidates apply to a job** (résumé tied to a job);
  the **cover letter is optional** and counts as bonus intention/motivation (feeds the motivation
  sub-score). Three parts:
  1. **Bulk résumé upload** — many résumés in one action, loose files OR a `.zip` (backend already
     multi-file + zip-expands per Phase 6). NEW: **per-résumé cover-letter pairing** — match each résumé
     to its own cover letter via a `manifest.json` or a filename convention (`<base>_resume` ↔
     `<base>_cover_letter`); not all résumés have one; unmatched cover files demote to standalone/ignore.
     `upload_resumes` today takes a single `cover_letter_text` → extend to a pairing map. **hris prior
     art:** `C:\repos\hris\apps\api\src\api\services\bulk_ingest_service.py`
     (`pair_applicants`/`parse_pairing_manifest`).
     - **Cover letter must be uploadable as a FILE, not just pasted text** (user request 2026-07-18).
       Today only the pasted `cover_letter_text` textarea is wired end-to-end. IMPORTANT: the service
       layer `resume_service.upload_resumes` **already accepts a `cover_letter_file: tuple[str, bytes]
       | None` param** (currently unwired) — so the SINGLE-résumé cover-letter-file case is a small
       wire-through: add a `cover_letter_file: UploadFile` Form field to the API route
       (`routes/resumes.py::upload_resumes`) + a file input to the job-detail upload form +
       `api_client.upload_resumes` passthrough. This is the natural FIRST slice of FU-3 and can ship
       ahead of the full bulk/pairing work.
  2. **Bulk JD upload** — multiple JD files (individual OR a `.zip`) → parse each into its own job;
     optional CSV manifest mapping filename → job metadata (title/dept/…). Backend has single
     `jd-extract` + `POST /jobs`; NEW: a bulk endpoint that expands files/zip, extracts JD text per file,
     and creates + enqueues a `parse_job` per file. **hris prior art:** bulk-JD create in its `jobs.py` +
     `bulk_ingest_service.parse_csv_manifest`.
  3. **Many-to-many views** — a candidate can be shortlisted across multiple jobs; navigate candidate↔job
     both ways ("which jobs is this candidate matched to" = reverse-match; "candidates for this job" =
     shortlist). Ties to the reverse-match UI (FU adjacent).
  4. **Shortlist-generation UX fixes (from live testing 2026-07-18, folded into FU-3):**
     - **The "Generating… forever" bug** — `shortlist_cards.html` polls every 3s and shows "Generating…"
       whenever `entries` is empty, with NO stop condition. Clicking **Generate before any résumé has
       finished parsing** makes `shortlist_job` return `empty`, and the page then polls indefinitely — it
       LOOKS stuck (this is exactly what the 009_adejoke test hit). FIX: bound the poll (e.g. stop after
       ~2–3 min / N attempts) with a real empty-state ("No ranked candidates yet — make sure résumés show
       'parsed', then Generate again"), and/or disable/​warn the **Generate** button until ≥1 résumé is
       `parsed`. hris used a 20-min safety valve on this poll.
     - **Parse speed is accepted as inherent** to the offline local LLM (real PDFs take ~60–116s each to
       parse on the remote 20B model; shortlist_job adds ~30–57s) — NOT a bug. Just **surface a UI hint**
       ("large PDFs take ~1–2 min to parse on the local model") so the wait isn't mistaken for a hang.
  - **Sample data for testing:** `C:\repos\hris\fixtures\llm_split\*.pdf` (21 real résumé/cover-letter
    PDFs, e.g. `009_adejoke_adeyemi_resume.pdf`, some `NNN_name_cover_letter.pdf` pairs) — ideal for
    exercising FU-3's bulk upload + per-résumé cover-letter pairing against realistic inputs.
  Security: forward `.zip` bytes verbatim (never client-expand — preserves the backend zip-bomb/
  path-traversal guards); consent gate per résumé; blind posture unchanged. This is the **offline** half
  of the old "connectors" concept; the Taleo *job-source scraper* remains a separate, still-deferred
  thing (see the connectors bullet below).

- **FU-4 — RBAC — ✅ MERGED (PR #23, merge `961caab`, 2026-07-21, ADR-018).** Branch `feat/fu4-rbac`, off
  `main` @ `2fc3d4f`, 13 commits, merged after the org billing block was cleared and CI ran green on all
  five gates (its first real execution on this branch — see the billing bullet below). Decisions +
  full detail: **ADR-018** (`docs/adr/018-rbac-keyed-roles.md`).
  - **CI billing block — RESOLVED 2026-07-21. CI IS NOW GREEN.** For history: `Gate: branch-name` on
    PR #23 showed FAILURE with every downstream gate SKIPPED, but the job never ran — the annotation
    read *"The job was not started because recent account payments have failed or your spending limit
    needs to be increased."* Actions was disabled for the `humanaxiom` org (a private org repo meters
    Actions minutes to the org). `feat/fu4-rbac` always matched the branch-name regex fine (verified
    against `Makefile:18-22` and `.github/workflows/ci.yml:20-27`); PR #22 ran green earlier the same
    day, so this lapsed mid-session. **The human fixed org billing on 2026-07-21 and both runs were
    re-run to full green** — run `29701584800` (pull_request) and `29701583639` (push):
    `Gate: branch-name` ✅ · `Gates: ruff · black · mypy` ✅ · `Gates: unit · coverage ≥ 80%` ✅ ·
    `Gate: integration (pg + neo4j + redis)` ✅ · `✅ ALL GATES GREEN` ✅. This was the **first time CI
    ever executed on this branch** — everything before it was the never-started billing failure.
    **Diagnostic note for a future block:** the real signal is `steps` on the job, not `conclusion`. A
    billing-refused job reports `conclusion: failure` with `steps: 0`; a job that genuinely ran and
    failed has `steps > 0`. Check with
    `gh api repos/humanaxiom/recruiter-assistant/actions/runs/<id>/jobs --jq '.jobs[]|{name,conclusion,steps:(.steps|length)}'`.
    PR #23 is now MERGEABLE with gates green; the merge decision itself was left to the human.
  - **The model:** four `Role(StrEnum)` values (`admin`, `recruiter`, `hiring_manager`, `auditor`) and
    four flat settings fields (`api_key_admin`, `api_key_recruiter`, `api_key_hiring_manager`,
    `api_key_auditor`) replace the old single `api_key` switch. `resolve_role` (reads `X-API-Key`,
    401s on no/unmatched key) and a new `require_role(*allowed)` dependency factory (403s an
    authenticated-but-not-allowed role) split Phase 6's old single-boolean `require_api_key` into real
    authentication-vs-authorization. Auth-disabled (all four keys empty) still resolves every caller to
    `Role.ADMIN`, unchanged fail-open-by-explicit-configuration posture from ADR-012. Two fail-closed
    startup refusals in `validate_startup_auth_config`: a stale legacy `API_KEY` env var hard-fails boot
    (a WARNING was rejected — indistinguishable in the log stream from the legitimate disabled-auth
    WARNING), and two configured role keys being byte-identical also hard-fails boot (silent role
    collapse otherwise).
  - **The `PATCH /jobs/{id}` finding (§7 of ADR-018) — the widest blast-radius item found, not recorded
    in ADR-016.** `JobUpdate` can flip `blind_review: false`, and every redaction key in the service
    layer gates off `jobs.blind_review`, not off any per-request `reveal` flag — before this feature,
    any authenticated caller could PATCH one job and permanently un-blind every résumé and shortlist
    entry under it, for every future reader, with **no audit row written anywhere**. Now restricted to
    admin/recruiter (`_JOB_WRITERS`). The authorization gap is closed; an audit row on a `blind_review`
    flip is still not added (deferred, see ADR-018 Consequences).
  - **R2 (ADR-016) closed further than ADR-016 described.** `reveal` is removed entirely from both
    `GET /resumes/{id}` and `GET /jobs/{id}/shortlist/export` — the export case was an unaudited **bulk**
    de-anonymization (every résumé on a shortlist in one response) never recorded in ADR-016 at all.
    `POST /resumes/{id}/reveal` (admin/recruiter only) is now the only un-blinding path in the system.
  - **R5/CSRF — closed, with two load-bearing amendments worth remembering if this area is touched
    again.** (a) The first cut stored one bare token per Flask session; since the FU-1 reveal button
    appears on every shortlist card posting to the same route, minting a token for one card invalidated
    every other card's token — only the first reveal click on a page worked. Fixed by scoping the token
    map per résumé id. (b) That fix then overflowed the ~4093-byte browser cookie ceiling at the
    `MAX_TOKENS_PER_SESSION = 64` cap (~5.2 KB measured) — browsers **silently drop** an oversized cookie
    rather than error, which re-triggered the exact same regression at full shortlist size (a 50-row
    shortlist was precisely the scenario that overflowed). Fixed with `secrets.token_urlsafe(16)` +
    a 12-hex-char SHA-256 mapping key (~2,440 B measured at cap), now pinned by a regression test that
    measures the real signed cookie, not a re-derived estimate. **The lesson stated plainly in ADR-018:
    the original 64-token cap was reasoned about entropy, never measured against the serialized cookie
    — measure, don't re-derive an estimate, if this size or cap changes again.**
  - **Two honest limitations a resuming session must not miss (both accepted residuals, ADR-018).** (1)
    The Flask viewer attaches one fixed `recruiter` role key outbound for every browser it serves —
    backend RBAC is largely decorative against frontend-originated traffic (every browser gets the same
    role regardless of who's sitting at it), and every browser-originated reveal audits as the same
    actor (`reveal_audit.actor = "api"`); RBAC's real enforcement is against direct API callers. (2)
    Roles are role-level, not row-level — there is no owner/company scoping, so a single
    `hiring_manager` or `auditor` key reads every job, résumé, and shortlist company-wide.
  - **Gate state at handoff:** both merge-blocking gates green — reviewer **APPROVE** (0 critical, 0
    major; 5 minor findings, all closed in `6da32ee`), security **PASS** (16 mutations, 15 killed; the
    one survivor — an unpinned no-short-circuit invariant on `resolve_role`'s comparison loop — closed
    in `a826d97`). `ranking-evals` is **not** a required gate for this branch (no scoring code touched:
    `pipeline/matching/*`, `stages.py`, `orchestrator.py`, `matching_tasks.py` byte-unchanged). Offline:
    ruff/black/`mypy src frontend --strict` clean; **2703 unit tests @ 91.57% coverage**; **123
    integration tests** passed live against real Postgres+Neo4j.
  - **`a826d97` is a `test:`-prefixed commit, a declared TDD-order deviation, not sloppiness** — it pins
    already-correct behavior (the no-short-circuit comparison loop) that could only be shown RED by
    mutation testing, not by a normal failing-test-first cycle; same precedent as Phase 4a.

### Queued next work — FU-5, FU-6, FU-7 (user-scoped 2026-07-20)

> **Status of this planning work: COMMITTED.** It was uncommitted at the 2026-07-20 session end (working-
> tree files on `feat/fu4-rbac`, no branch). On 2026-07-21 it was committed to its own docs-only branch
> `chore/fu5-7-plan` — the separate-branch option that had been left on the table, rather than growing
> PR #23 — and opened as **PR #24** (`391aafd` ADRs 019/020/021, `14aeddd` the 9 filed gaps + this
> HANDOFF refresh), CI green on all five gates. `docs/adr/{019,020,021}-*.md` and `docs/process/` are
> tracked files now, not working-tree scratch. **An earlier version of this block said the opposite** and
> is corrected here; if you are looking for uncommitted planning work, there is none.
>
> Still uncommitted-by-design: `compose.live-eval.yml` is gitignored and now carries
> `LLM_TIMEOUT_S: "300"` for the worker. A fresh clone will not have it and will hit the 120s parse
> failure described in the incident below.

**This is a queued plan, not an options list.** The user scoped it on 2026-07-20 after an operational
incident (below) exposed the silent-failure class. Build order matters: **FU-5 → FU-6 → FU-7**, because
FU-6's scoping predicates and FU-7's attributable failure states both key off FU-5's `users` table.
Each is a named feature, not a numbered phase — `docs/EXTRACTION_PLAN.md`'s table stays closed at Phase 7.

- **FU-5 — CAS identity, user records, attributable audit (ADR-019).** Adds the first real `users` table
  (there is none today), authenticates humans via **CAS** rather than an API key, moves `role` onto the
  user row so roles become data instead of a hardcoded `StrEnum`, and generalizes `reveal_audit` into an
  `audit_log` that also captures **`blind_review` flips** (today's widest-blast-radius unaudited action).
  Closes ADR-018's actor-attribution residual: a reveal will name a person instead of `"api"`.
  **On CAS and offline-first:** CAS is **SFU-hosted internal infrastructure**, not a cloud API — it does
  not breach CLAUDE.md's "NEVER add cloud API calls" constraint, and no data leaves the institution.
  What it adds is a runtime dependency *outside the compose stack*: unlike Postgres and Neo4j, it is not
  a container this project starts or can restart. ADR-019 §3 records that honestly — CAS unreachable =
  fail closed on new logins only, existing sessions keep working, and local model inference is entirely
  unaffected. Service-to-service API keys survive as a *separate* mechanism that can never satisfy an
  action requiring an attributable human.
- **FU-6 — Per-job assignment and row-level scoping (ADR-020).** A `job_assignees` table; hiring managers
  and auditors see only assigned jobs. **Scoping is enforced in SQL, not in the handler** — a Python-side
  filter after fetching is a leak waiting to happen. Unassigned reads return **404, not 403**, so the
  existence of a requisition is not leaked. Closes ADR-018's role-level-not-row-level residual. One item
  is deliberately left for ratification rather than silently decided: whether `auditor` should be scoped
  at all, since scoping an auditor may defeat the role's purpose.
- **FU-7 — LLM failover and fail-closed ranking (ADR-021).** An ordered provider chain (A → B) with
  per-provider breakers, failover only on availability errors and never on schema-validation failures.
  **The pipeline refuses to emit a ranking containing a silently-zeroed component** — a job blocks in an
  `awaiting_llm` state and retries rather than publishing a degraded shortlist. Also makes parse status
  honest: claim `uploaded → parsing` on start, write `failed` + `failure_reason` on retry exhaustion, and
  surface partial-parse degradation instead of marking it `parsed`.
- **HR-facing explainer (already written, untracked).** `docs/process/ranking-metrics-explainer.html` is
  a plain-language explainer of the scoring model for HR/compliance, with Mermaid diagrams and a
  **ratification register** of 15 policy decisions currently encoded as config defaults (weights, the
  must-have penalty, education-field blindness, the top-15 evidence cliff, recency banding, over-qual
  dampening, and the audit/scoping gaps). It has **not** had a `reviewer` pass; the weight table and
  evals thresholds in it were relayed from explorer agents, not read line-by-line by the author. Get it
  reviewed before it goes to HR.
- **Chore — config plumbing and fail-closed auth.** No `MATCH_*` tunable and none of the four `API_KEY_*`
  vars appear in `docker-compose.yml` or `compose.live-eval.yml`. Two consequences: the documented ranking
  knobs are unreachable in the running containers, and since auth is disabled iff all four keys are empty,
  **the shipped compose runs auth-disabled with every caller resolving to `admin`.** Fail-open is the
  *shipped* default, not merely a possible misconfiguration. Also raise the `LLM_TIMEOUT_S` default off
  120 (see incident below).

**The 2026-07-20 incident that motivated FU-7 — read this before touching the LLM or parse path.**
16 résumés sat at `uploaded` for ~18 hours. `gpt-oss:20b` on the calibrated peer generates at **~23.5
tok/s** (measured: 1338 completion tokens in 56.8s from inside the worker container); `parse_resume`
calls `chat_json(max_tokens=3072)`, so a full-length core extraction needs **~131s** against a
`LLM_TIMEOUT_S` of **120**. The failure was **deterministic, not transient** — short generations
succeeded, which disguised it as flaky infrastructure for two diagnostic rounds. Raising the timeout to
300 in `compose.live-eval.yml` cleared all 16 (real parses measured 150–205s). **But 10 of the 16 then
logged `parse_resume.skills_llm_failed` and were still marked `parsed`** — skills silently fell back to
the deterministic vocabulary scan. Root cause of the empty content: `gpt-oss:20b` is a reasoning model
that returns its chain in a separate `reasoning` field and can exhaust `max_tokens` before emitting any
`content`. `reasoning_effort: "low"` is a large latency lever (~7x on a toy prompt) but changes
extraction quality — **it must go through the `ranking-evals` gate, never be set unilaterally.**

**Diagnostic lesson worth keeping:** do not exonerate the LLM endpoint with a small curl. A 10-token
probe returns in ~4s and proves nothing. Measure `completion_tokens / elapsed` at realistic `max_tokens`
from **inside the worker container**, then compare `max_tokens / tok_s` against `LLM_TIMEOUT_S`.

The remaining backlog below is still **options, not a queued to-do list**:

- **Wire the reverse-match UI** — ✅ **CLOSED (FU-3 slice 5, PR #21, merge `e033d31`).** Shipped as the
  POST-only trigger + bounded poll + rows linking to the job. Listed here as an option long after it was
  delivered; retained per the repo's record-closure-forward convention rather than deleted.
- **The open `jd.education.fields` decision** (ADR-009 §7, restated through ADR-013) — `score_education`
  ignores `jd.education.fields` entirely, so JD field-relevance is decorative. Either extend the scorer to
  read `fields`, or drop `fields` from the JD contract. Still unresolved after 4c/4d/5/6/7/Workflow UI all
  touched no scoring code.
- **The deferred connectors feature** (Taleo/CSV-manifest upload) — explicitly cut in Phase 6 (ADR-012 §2),
  the user's own framing was "Taleo was a shortcut to get sample data … will add more connectors in the
  future." Upload today only accepts local multi-file or `.zip`, from the browser or the API directly.
- **No advisory lock on concurrent shortlist/reverse-match regenerate** (ADR-010 §1) — now live and
  reachable from the browser (the Workflow UI's Generate/Regenerate button calls
  `POST /jobs/{id}/shortlist` directly). Last-committer-wins today.
- **`reverse_match_job`'s `allowed_job_ids` filter** is still `description_parsed IS NOT NULL`, not
  `status = 'open'`, even though Phase 6 added the first code path that ever transitions `jobs.status`
  (ADR-012 §3 revisits, does not resolve).
- **CSV formula/injection** in `shortlist_csv`/`shortlist_evidence_csv` — accepted for v1 (ADR-011), a
  one-line fix (leading-character escaping) was noted but not applied.
- **The `redacted_filename` `os.path.splitext` LOW residual** — a pathological filename with no true
  extension can leak a lowercased name-derived suffix under blind review (ADR-011). Accepted for v1.
- **The live-eval harness's synthetic-only skill-name-scrub shortcut** — `run_evals_live.py` was built and
  verified only against the synthetic 4a/4c corpus; its embed-boundary PII handling should not be reused
  as-is on a path carrying real candidate PII without a fresh security review (security note, not a defect
  in the merged code).
- **At-rest cleartext PII posture** (ADR-007 §6/§7, ADR-010 §6) — `resumes.parsed`,
  `shortlist_entries`/`reverse_match_entries`'s evidence quotes, and structured experience/education/skills
  fields are all cleartext at rest in Postgres (protected by pgcrypto only on the four dedicated PII
  columns). Accepted for v1; revisit before any multi-tenant deploy.
- **Weak/empty `flask_secret_key`/`api_key` defaults** — env-overridable, but weak-by-default; harden
  before any non-local deployment (Workflow UI status, above).
- **Reverse-match ranking quality is entirely ungated** (ADR-013) — the `[reverse_match]` section of
  `core/tests/evals/thresholds.toml` is a commented-out placeholder, so no precision, evidence-verification
  or ordering bar applies to the résumé→jobs direction, while the forward direction is gated at 100%
  precision@5. Revisit before reverse match informs any decision.
- **Reverse-match scores are not comparable to forward-match scores** (ADR-009) — `rank_job_matches` omits
  the motivation term, so reverse `score_final` maxes at 0.9 under default weights while forward maxes at
  1.0. Nothing in the API, the export or the UI signals this. Must be documented wherever both numbers can
  reach the same reader.
- **Two ranking numbers are unreachable from settings** (ADR-009) — `_STRUCTURED_ONLY_WEIGHTS` and the
  stage-1 3x oversample factor are in-code literals, while all 26 `MatchWeights` values and both k values
  are env-configurable. Also: reverse match reuses `match_coarse_k`; there is no `match_reverse_coarse_k`.
  Minor, but an inconsistency in an otherwise fully-tunable engine.
- **The circuit breaker's half-open docstring contradicts its code** — `core/src/pipeline/llm/client.py`
  claims a failing half-open trial "will re-open immediately", but the failure counter was reset on
  cooldown expiry, so it takes another full `breaker_threshold` (10) consecutive failures to trip again.
  Either the doc or the behaviour is wrong; decide which when FU-7 touches this file.
- **ADR-016's R3 and R4 remain open** — R3 (no reveal-audit viewer: the `auditor` role added by FU-4 still
  has nothing to view, so retrieval is a manual SQL query) and R4 (unredacted `source_context` on reveal).
  FU-5's `audit_log` makes R3 actionable but does not itself ship a viewer.

**Re-running the live eval, if a future session needs to:**

```bash
docker compose -f docker-compose.yml -f compose.live-eval.yml up -d postgres neo4j redis api worker
docker compose -f docker-compose.yml -f compose.live-eval.yml exec -T worker \
  python tests/evals/run_evals_live.py
```

Two files this depends on are git-ignored and not in the repo: `.env` and `compose.live-eval.yml`. The
calibrated models (`nomic-embed-text` + `gpt-oss:20b`) live on a **remote** Ollama — the local metal host
does not have them pulled. A fresh clone must recreate both `.env` and the compose override before this
will run; see ADR-013 §5 / §"Live end-to-end eval" for the harness's design and verified results.

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
auth/upload scope in docs/adr/012-api-routes-auth-upload-scope.md, and the
Phase 7 read-only Flask viewer in docs/adr/013-phase7-evals-viewer.md.

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

**Phase 6 (API routes) is COMPLETE and MERGED to main via PR #15** (squash
merge e910669, off main @ 6deade3, tip 837de9e), commit chain:
red 209bff7 -> green bc9a3d6 (initial routes, resumed mid-build after a
session-limit interruption) -> red 1f2b161 -> green 344f6bf (SEC-1/SEC-2/SEC-4
security hardening + exact fastapi/starlette/python-multipart pins) -> red
c75f4a7 -> green 837de9e (non-ASCII X-API-Key 401 generalization + upload
file-count-ordering regression pin). **All three merge-blocking gates were
green (reviewer APPROVE, security PASS, ranking-evals PASS) AND CI's
gates-all went fully green before merge (2026-07-17).** Note: CI's gates-all
runs the offline run_evals.py stand-in inside the gated unit suite — it never
calls a live Ollama endpoint, by design (see the Phase 7 correction below).
Full detail: docs/activity/phase-6-api-routes.md and
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

**Phase 7 (a minimal read-only Flask viewer over the Phase 6 API) is MERGED to
main via PR #16** (squash merge 1039e5c, 2026-07-17), built on branch
feat/phase-7-evals-viewer (off main @ e910669, pre-merge tip 92ca4ae, now
deleted local + remote), commit chain: docs 55ee0a0 (interim HANDOFF/plan
stamp) -> red 942e8f5 -> green f28c22e (core/frontend/api_client.py new,
core/frontend/app.py extended with 7 new routes + templates, Makefile/ci.yml
gate-scope widening) -> refactor/fix 92ca4ae (two post-review security
findings closed: the résumé-detail template rewritten so it is structurally
incapable of rendering candidate.name/email/phone/location rather than merely
flag-gated on the backend's blinded flag, and the error page made fully
static so no backend-supplied text reaches the browser). **All three
merge-blocking gates were green (reviewer APPROVE, security PASS,
ranking-evals PASS), CI's gates-all went fully green, and PR #16 was
squash-merged.** Full detail:
docs/activity/phase-7-evals-viewer.md and docs/adr/013-phase7-evals-viewer.md.

**Phase 7 shipped a blind-only viewer, by construction, not by default.**
Shortlist list/detail reads (api_client.list_shortlist/get_shortlist_entry)
take no reveal parameter at all; the résumé route hardcodes reveal=False and
ignores any browser-supplied ?reveal=. Reveal/reveal-export remains an
audited, non-viewer backend surface (ADR-011/012) — the viewer can never
de-anonymize from the browser. **Phase 7 also fixed a real, previously
un-gated hole:** core/frontend/ (api_client.py, app.py, the new tests) was
invisible to every quality gate before this phase — it is a sibling of
core/src/, and every gate command named "src tests" explicitly. Makefile and
.github/workflows/ci.yml now run ruff/black/mypy/coverage over frontend too,
pinned by a new meta-test (test_gates_cover_frontend.py).

**Phase 7 shipped NO new evals fixtures as part of the viewer build — that line
item was already done.** The plan's Phase 7 row said "ranking-quality fixtures
(precision@k, evidence-verification rate)"; those shipped in 4a (corpus) + 4c
(live orchestrator wiring, run_evals.py::main() already running inside the
gated unit suite). A live end-to-end eval of the corpus through the real
pipeline (post-parse boundary -> project_to_graph -> shortlist_job -> persisted
rows) had never run (4c only proved it against the orchestrator directly) and
was originally deferred this session — needing a reachable host Ollama +
docker compose up, which CI cannot provide by design. **Reversed later the
same day (2026-07-17): the human un-deferred it and made it a prerequisite for
merging PR #16. It is now BUILT + RUN + PASS, reproduced identically twice**
(core/tests/evals/run_evals_live.py, new, 812 lines +
core/tests/unit/test_evals_live_metrics.py, new, 16 tests). Full detail:
ADR-013 §5 and docs/activity/phase-7-evals-viewer.md's "Live end-to-end eval
(post-review addition)" section.

**Documentation correction made this session, apply it wherever you see the
old phrasing:** prior HANDOFF/plan text said CI runs "a live run_evals.py
re-measurement against Ollama" for Phases 4d/5/6. That is inaccurate — CI's
gates-all runs the OFFLINE run_evals.py stand-in inside the gated unit suite;
it never calls Ollama (.github/workflows/ci.yml's own comment: "CI never
calls a model endpoint; inference is host-only by design"). CI itself still
never calls Ollama — the live measurement against a real Ollama endpoint
(above) runs as a separate script outside CI, via
`docker compose ... exec -T api python tests/evals/run_evals_live.py`
against a stack pointed at an Ollama with nomic-embed-text + gpt-oss:20b, not
inside the gated unit suite.

**Phase 7 final state (tip 92ca4ae, pre-live-eval):** 2229 unit tests @ 91.67%
coverage (frontend now format/type/coverage-gated for the first time).
Reviewer APPROVE, security PASS (both findings closed), ranking-evals PASS
(scoring code byte-unchanged; offline corpus 352 tests green;
run_evals.py::main() exits 0). **Post-review (2026-07-17): the 16 new
test_evals_live_metrics.py tests bring the offline suite to 2245 unit tests @
91.67% coverage**, ruff/black/mypy still clean.

**PR #16 was gated in CI (gates-all fully green) and squash-merged to main as
1039e5c on 2026-07-17** — the live end-to-end eval (above) was the merge
prerequisite and had already passed, reproduced twice.
docs/EXTRACTION_PLAN.md's phase table ends at Phase 7, and it is now fully
merged: **the extraction plan's locked v1 scope (the four decisions at the top
of the plan) is complete. All seven phases (0-7) are merged to main, CI
green. There is NO Phase 8.** Do not invent a new numbered phase on your own
initiative; any further work (the still-open jd.education.fields decision,
the accepted residuals catalogued across ADR-009 through ADR-013, the
deferred connectors feature, the no-advisory-lock gap, at-rest cleartext PII
posture before multi-tenant — see HANDOFF.md's "Next session" section for the
full list) is a follow-up chore that needs a human to scope it, not an
automatic Phase 8.

Subagent model tiering is in effect (docs/SUBAGENT_MODEL_POLICY.md): the three
merge-blocking gates (reviewer/security/ranking-evals) run on opus; producers
(data-pipeline/planner/tester/coder) default to sonnet; docs on haiku. Defaults
live in .claude/agents/*.md frontmatter.

**Open human decision, carried forward across 4c/4d/5/6/7, still UNRESOLVED:**
score_education ignores jd.education.fields (ADR-009 §7, restated ADR-010 §5,
ADR-011, ADR-013) — either extend the scorer or drop fields from the JD
contract. Neither 4c, 4d, 5, 6, nor 7 touched stages.py's scoring code, so
this remains exactly as open as it was after 4c. Do not resolve this silently.
Also carried: reverse_match_job's allowed_job_ids filter is still
description_parsed IS NOT NULL, not status='open', even though Phase 6 added a
status route (ADR-012 §3 revisits but does not resolve this); no advisory
lock on concurrent shortlist/reverse-match runs (ADR-010 §1, still open, the
viewer is read-only so Phase 7 didn't touch this either).

Note: no local Python — verify gates in the python:3.11-slim Docker container per
HANDOFF.md. Phase 7's PR #16 is merged; v1 is complete.

CURRENT STATE AS OF 2026-07-20 — read HANDOFF.md's "Queued next work" section
before doing anything:
- FU-4 (RBAC) is MERGED to main via PR #23 (merge 961caab, 2026-07-21). The
  org billing block was fixed that morning and CI ran green on all five gates
  including integration against real pg/neo4j/redis — its first real execution
  on that branch. `main` is now at 961caab.
- Work IS now scoped, contrary to the "needs a human to scope it" note above:
  FU-5 (CAS identity + attributable audit, ADR-019), FU-6 (per-job assignment +
  row-level scoping, ADR-020), FU-7 (LLM failover + fail-closed ranking,
  ADR-021). Build order is FU-5 -> FU-6 -> FU-7; each depends on the previous.
- THE PLANNING WORK IS UNCOMMITTED. ADRs 019/020/021 and docs/process/ are
  untracked; HANDOFF/README/EXTRACTION_PLAN/ADR-007/009/013/018 are modified.
  No branch was made for it. Ask the human before committing, and do not assume
  it landed. A `docs/fu5-7-plan` branch was recommended but not created.
- Operational: `compose.live-eval.yml` (gitignored) now sets LLM_TIMEOUT_S=300
  for the worker. The 120s default is BELOW the real parse time on the
  calibrated peer (~23.5 tok/s vs max_tokens=3072 => ~131s), so a fresh clone
  will see resumes hang at status 'uploaded' forever with no error. This is
  deterministic, not flaky. See the incident writeup in HANDOFF.md.

See the "Phase 3 starting map (verified)" subsection above (historical) and
docs/adr/007 for how the ingest/parse layer Phase 4 builds on was ported.
```
