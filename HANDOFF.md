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

**Done:** repo created + `origin` repointed + pushed; 4 decisions locked; plan-of-record and the `data-pipeline` + `ranking-evals` subagents committed. **Phases 0, 1, 2, and 3 are all complete and merged to `main`, CI green:** Phase 0 (seed & infra) via PR #1 (merge `8b2b47c`), Phase 1 (storage) via PR #2 (merge `f7e7cbe`), Phase 2 (schemas) via PR #3 (merge `cefd545`), Phase 3 (ingest + parse) via PR #6 (merge `49196d7`). Phases 0–2 merged 2026-07-11; Phase 3 merged 2026-07-12. **Phase 4 (Ranking engine) is 🔄 in progress, split into 4 gated sub-phases.** Sub-phase **4a (evals corpus) is MERGED to `main` via PR #8** (merge `875eac2`), CI green, 2026-07-12, and its **falsifiability hardening is also MERGED via PR #10** (merge `464a479`), CI green. **Sub-phase 4b (graph projection) MERGED to `main` via PR #11** (merge `68fe821`), CI green. **Sub-phase 4c (matching engine) is MERGED to `main` via PR #12** (merge `fd12d1a`), CI green. **Sub-phase 4d (shortlist + reverse-match write path) is COMPLETE and gate-green on branch `feat/phase-4d-shortlist-writepath`** (off `main` @ `fd12d1a`) — **all three merge-blocking gates green (reviewer APPROVE, security PASS, ranking-evals PASS); NOT yet PR'd, NOT merged — a PR opens after a human check-in.** CI (`gates-all`, incl. a live `run_evals.py` re-measurement) has not yet run since no PR exists. **Phase 5 (persist + anonymize + export — read/list/get/export + display redaction) is the next sub-phase to build, once 4d is reviewed and merged.** See "Phase 4a status", "Phase 4b status", "Phase 4c status", "4d status", and "Phase 4 resume" below.

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

## Phase 4 resume — EXACT next step (do this first, before anything else)

Phases 0–3 are **merged to `main`, CI green** (Phase 3 via PR #6, merge `49196d7`, 2026-07-12). Phase 4
(Ranking engine) is split into 4 gated sub-phases (4a→4b→4c→4d, each its own branch/PR — see the plan
table). **Sub-phase 4a (evals corpus) is COMPLETE and MERGED** (PR #8), and its **falsifiability
hardening is also COMPLETE and MERGED via PR #10** (merge `464a479`). **Sub-phase 4b (graph
projection) is COMPLETE and MERGED via PR #11** (merge `68fe821`) — see "Phase 4b status" below for the
full detail (the PII architectural pivot, the ranking-cost measurement, final gate numbers). **Sub-phase
4c (matching engine) is COMPLETE and MERGED to `main` via PR #12** (merge `fd12d1a`) — see "4c status"
below for the closed blockers and final gate numbers. **Sub-phase 4d (shortlist + reverse-match write
path) is COMPLETE and gate-green on branch `feat/phase-4d-shortlist-writepath`** (off `main` @
`fd12d1a`) — all three merge-blocking gates green; **NOT yet opened as a PR — awaiting a human
check-in before opening one**; see "4d status" below for the closed Requirement 1, the persistence
asymmetry, and the PII residual. **Your next action is Phase 5 (persist + anonymize + export —
`list_for_job`/`get_one`/`export_rows` + display redaction), once 4d is reviewed, PR'd, and merged.**
Confirm 4d's merge status with the human first (it is currently pre-PR, on its own branch).

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

### 4d status — DONE, gate-green, NOT yet PR'd (pre-PR) — read this before starting Phase 5

`core/src/services/shortlist_service.py` (`persist_shortlist`/`persist_reverse_match`) +
`core/src/worker/matching_tasks.py` (`shortlist_job`/`reverse_match_job` arq tasks) +
`matching_context_from_settings`/`non_matchable_families_from_settings` (the ADR-009 "Requirement 1"
settings-wiring closure) were built and gate-green on branch `feat/phase-4d-shortlist-writepath`, tip
`6c2bf43`, 2 commits (RED `24419b0` → GREEN `6c2bf43`), off `main` @ `fd12d1a` (PR #12's merge).
**All three merge-blocking gates are green (reviewer APPROVE, security PASS, ranking-evals PASS).
NOT yet opened as a PR, NOT merged — awaiting a human check-in before opening one, per protocol.** CI
(`gates-all`, incl. a live `run_evals.py` re-measurement against Ollama) has **not** run yet, since no
PR exists — do not treat 4c's last-measured live-eval numbers as re-confirmed by this branch until CI
runs (the scoring code itself, `stages.py`/`orchestrator.py`, is byte-unchanged by 4d — only the new
additive `matching_context_from_settings` factory touches `orchestrator.py`). Full write-up:
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
shortlist/reverse-match write path in docs/adr/010-shortlist-reverse-match-write-path.md.

We are porting the resume-ranking feature from C:\repos\hris onto this template
(template-first, filesystem storage instead of MinIO, keep Neo4j, v1 includes
cover-letter/reverse-match/minimal viewer/blind-default). Phases 0, 1, 2, and 3 are
ALL complete and merged to main, CI green: Phase 0 (seed & infra) PR #1, Phase 1
(storage — filesystem BlobStore) PR #2, Phase 2 (schemas) PR #3, Phase 3 (ingest +
parse) PR #6 (merge 49196d7). Phase 4 (Ranking engine) is split into 4 gated
sub-phases, ALL FOUR now built: 4a (evals corpus) MERGED (PR #8, merge 875eac2),
falsifiability hardening MERGED via PR #10 (merge 464a479); 4b (graph projection)
MERGED via PR #11 (merge 68fe821); 4c (matching engine) MERGED via PR #12
(merge fd12d1a) — all three merge-blocking gates were green (security PASS,
reviewer APPROVE, ranking-evals PASS) AND CI was fully green before merge.
main today has 1916 unit tests @ 90.71% coverage + 87 integration tests
(post-4c-merge), and run_evals.py exits 0 (the corpus's first real live-engine run).

**4d (shortlist + reverse-match write path) is COMPLETE and gate-green on branch
feat/phase-4d-shortlist-writepath** (off main @ fd12d1a, tip 6c2bf43, 2 commits:
red 24419b0 -> green(4d) 6c2bf43). All three merge-blocking gates are green
(reviewer APPROVE, security PASS, ranking-evals PASS) — **NOT yet opened as a
PR, NOT merged; a PR opens only after a human check-in.** CI (gates-all, incl. a
live run_evals.py re-measurement against Ollama) has NOT run yet since no PR
exists. The branch brings 1947 unit tests @ 91.98% coverage + 93 integration
tests (87 carried forward + 6 new). Full detail:
docs/activity/phase-4d-shortlist-writepath.md and
docs/adr/010-shortlist-reverse-match-write-path.md.

**4d closed ADR-009's carried "Requirement 1"** — matching_context_from_settings
(new, src/pipeline/matching/orchestrator.py) is the single call site that builds
MatchingContext from Settings (family_weight, non_matchable_families,
llm_concurrency, evidence_max_tokens, model_gen/emb, git_sha); shortlist_job/
reverse_match_job (src/worker/matching_tasks.py, new) call it with get_settings()
and pass weights=weights_from_settings(get_settings()) — never DEFAULT_WEIGHTS.
This closes the one bug class run_evals.py structurally cannot catch: Settings()'s
own defaults equal MatchWeights' defaults by construction, so a silent fallback to
DEFAULT_WEIGHTS is invisible to the live-eval corpus and can only be caught by a
settings-wiring unit test built around NON-default Settings values (see
test_matching_context_settings_wiring.py / ADR-010 §3). stages.py/orchestrator.py's
SCORING code is byte-unchanged by 4d — this branch is persistence wiring only.

**4d also shipped src/services/shortlist_service.py (persist_shortlist/
persist_reverse_match)** — both DELETE-first per-run (rerun-replaces idempotency
keyed on the DDL's real unique constraints, shortlist_entries(job_id,resume_id) /
reverse_match_entries(resume_id,job_id)), and deliberate MIRROR-IMAGE handling of
score_structured/score_evidence/evidence dictated by the two tables' different DDL
shapes (shortlist folds scores into score_breakdown jsonb + coerces evidence=None
to {} for a NOT NULL column; reverse-match uses dedicated columns + passes
evidence=None as SQL NULL for a nullable column) — full rationale + the accepted
residual (shortlist's {} conflates "never scored" with "scored empty" at the raw
SQL level) in ADR-010 §2. reverse_match_job scopes allowed_job_ids to
description_parsed IS NOT NULL, never None (ADR-010 §4 — NOT status='open',
because jobs.status is never transitioned anywhere in the codebase yet). Evidence
quotes are written verbatim with no new redaction — a deliberate v1 decision,
symmetric with ADR-007 §6/§7's already-accepted cleartext-at-rest posture for
resumes.parsed, recorded explicitly in ADR-010 §6 (security-flagged).

**Your next action is Phase 5 (persist + anonymize + export)** — NOT the evals
corpus (done), NOT graph projection (done), NOT the matching engine (done), and
NOT the write path (done, pre-PR). Confirm 4d's status with the human first: it
is on branch feat/phase-4d-shortlist-writepath, gate-green, NOT yet opened as a
PR — check whether a PR has since been opened/merged (gh pr list) before
building on top of it. Phase 5 ships list_for_job/get_one/export_rows +
display redaction (blind-default, ADR-006 §4's redaction-boundary contract:
ResumeOut/ResumeListItem can serialize decrypted PII with blinded=True, so
redaction must mask candidate.*/candidate_name/cover_letter_text BEFORE DTO
construction). Run the per-phase subagent loop (planner -> tester ->
data-pipeline coder -> reviewer + security + ranking-evals -> docs) on a
feat/phase-5-... branch once 4d is merged.

Subagent model tiering is in effect (docs/SUBAGENT_MODEL_POLICY.md): the three
merge-blocking gates (reviewer/security/ranking-evals) run on opus; producers
(data-pipeline/planner/tester/coder) default to sonnet; docs on haiku. Override
data-pipeline UP to opus for Phase 5 if the export/redaction wiring touches the
PII boundary directly (the hard-core surface the model policy escalates for);
mechanical list/get route plumbing can stay on sonnet. Defaults live in
.claude/agents/*.md frontmatter.

**Open human decision, carried forward across 4c AND 4d, still UNRESOLVED:**
score_education ignores jd.education.fields (ADR-009 §7, restated ADR-010 §5) —
either extend the scorer or drop fields from the JD contract. Neither 4c nor 4d
touched stages.py's scoring code, so this remains exactly as open as it was
after 4c. Do not resolve this silently while building Phase 5.

Note: no local Python — verify gates in the python:3.11-slim Docker container per
HANDOFF.md. Check in with me before opening the Phase 4d PR or any Phase 5 PR.

See the "Phase 3 starting map (verified)" subsection above (historical) and
docs/adr/007 for how the ingest/parse layer Phase 4 builds on was ported.
```
