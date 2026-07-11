# Phase 1 — Storage (filesystem BlobStore) — Activity Report

**Branch:** `feat/phase-1-storage` (pushed to `origin` = github.com/humanaxiom/recruiter-assistant)
**Base:** `main` (Phase 0, merge commit `8b2b47c`)
**Date:** 2026-07-10
**Status:** Complete — all gates green; three merge-blocking subagent gates (reviewer APPROVED, security PASS, ranking-evals PASS). Not yet merged.

---

## 1. Summary

Phase 1 builds the filesystem `BlobStore` primitive that ADR-004 chose in place of MinIO, plus its API/worker wiring. `BlobStore(root)` is rooted at `settings.storage_dir` (default `/data`, bind-mounted to `./data`), stdlib-only (`pathlib` / `asyncio` / `os`, no new dependency), with all IO wrapped in `asyncio.to_thread`. It exposes `put` / `get` / `delete` / `exists` / `list_keys` and two exceptions (`BlobNotFound`, `InvalidBlobKey`). The security core is a single `_resolve` path-traversal guard that runs before any IO and rejects `..` segments, absolute/Windows-drive/UNC/backslash keys, empty/root-resolving keys, null-byte keys, and symlink escapes (realpath + `is_relative_to`); created blobs are `0o600` and store-created dirs `0o700` (the PIPEDA/FIPPA control for blobs at rest). The store is wired onto `app.state.blob_store` (with a `get_blob_store` dependency) in the API lifespan and onto `ctx["blob_store"]` in the worker startup — **no route or service invokes it yet**; those call sites (`resume_service`, `resume_tasks`, admin/flush, routes) are ported in Phases 3–6. Final status is green: the offline suite passes (ruff with no `--fix`, black, mypy --strict, 240 unit tests, 99.46% coverage) and all three merge-blocking gates passed, including a guard-mutation test proving the traversal guard is real. The interface and path-safety decisions are recorded in **ADR-005**.

---

## 2. Deliverables (mapped to the plan's Phase 1 item)

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | `BlobStore` async interface (`put`/`get`/`delete`/`exists`/`list_keys`) replacing MinIO | Done | `core/src/storage/blob_store.py` |
| 2 | Path-traversal rejection (carried-forward security criterion #1) | Done | `_resolve` guard; parametrized traversal + symlink tests; guard-mutation eval |
| 3 | At-rest perms (`0o600` blobs / `0o700` dirs) for raw resume bytes | Done | `put` `os.open(..., 0o600)` + per-level `mkdir`/`chmod` `0o700` |
| 4 | API lifespan wiring + `get_blob_store` dependency | Done | `core/src/api/main.py`; `get_blob_store` in `blob_store.py` |
| 5 | Worker startup wiring on `ctx["blob_store"]` | Done | `core/src/worker/main.py` |

**Not in Phase 1 (by design):** no resume/cover-letter upload or fetch logic, no `resume_service`, no `resume_tasks`, no routes, no `pii.py`, no MIME sniffing. Phase 1 is the primitive + wiring only.

**Scope correction recorded:** the plan carried *two* security criteria forward to Phase 1. Only **#1 (path-traversal)** was a Phase 1 concern — done here. **#2 (strict `current_setting('app.pii_key')`, no `missing_ok`)** belongs to **Phase 3** (`pii.py`), where the PII read path lands; it is not a `BlobStore` concern. The plan and HANDOFF are corrected to attribute it to Phase 3 so it is not lost.

---

## 3. Commit timeline

| Commit | Type | What / why |
|---|---|---|
| — | red | Failing unit tests for `BlobStore` — roundtrip/overwrite, `get`-missing → `BlobNotFound`, idempotent `delete`, `exists`, `list_keys` (recursive, sorted, prefix), `content_type`-accepted parity, the parametrized path-traversal + symlink-escape battery, root autocreate, and the lifespan/worker wiring tests. Written before implementation, confirmed RED. |
| — | green | `storage/blob_store.py` + API/worker wiring — minimal implementation to GREEN: `_resolve` guard, async `to_thread` IO, `get_blob_store` dependency, `app.state`/`ctx` wiring. |
| — | red (harden) | Failing tests for the three hardenings the gates demanded — `0o600`/`0o700` perms assertions, null-byte key rejection, and `list_keys` symlink-escape filtering. RED against the first green. |
| — | green (harden) | Implemented the hardenings: per-level `mkdir` + `chmod 0o700`, `os.open` with `0o600` + post-write `chmod`, explicit null-byte check in `_resolve`, and the realpath filter in `list_keys`. All gates green. |

(Hashes omitted — this report was produced in a docs environment without a shell; the four-commit red → green → red-harden → green-harden shape is per the Phase 1 spec and the landed tree.)

---

## 4. Quality gates

Verified in the `python:3.11-slim` container (offline suite), `ruff check` with **no `--fix`** (matching CI, so committed import order is proven not masked).

| Gate | Tool | Result |
|---|---|---|
| Lint | ruff check (no `--fix`) | PASS |
| Format | black --check | PASS |
| Types | mypy --strict | PASS |
| Unit tests | pytest tests/unit | PASS — **240 tests** |
| Coverage | pytest --cov (threshold 80%) | PASS — **99.46%** |
| Branch name | naming gate | PASS (`feat/phase-1-storage`) |

Subagent gate verdicts (all three merge-blocking):

- **Reviewer — APPROVED.**
- **Security — PASS.** Path-traversal + symlink + null-byte + perms accepted; three forward-looking guardrails deferred (see §6), none blocking for a primitive with no call sites.
- **Ranking-evals — PASS.** No ranking logic in Phase 1, so precision@k / evidence-verification are not measurable; the gate confirmed the store is a dumb byte sink with no parse/text-extraction path (PII-never-in-embeddings groundwork intact) and ran a **guard-mutation test** — mutating the traversal guard off makes the traversal tests go red, proving the guard is real.

---

## 5. Findings caught by the gates & how resolved

The gates turned the first green pass into a hardened one. Three fixes:

### 5.1 World-readable blobs at rest (security → perms hardening)

The first implementation wrote blobs and dirs at the process default (umask-dependent, typically `0o644` / `0o755`). Raw resume bytes are the record on disk and the PIPEDA/FIPPA control here is filesystem perms (blobs are not encrypted, unlike the pgcrypto PII *columns*). Fixed: blobs created `0o600` via `os.open(..., O_CREAT, 0o600)` then `chmod` (so an overwrite tightens a looser pre-existing file), and every store-created directory level `mkdir` + `chmod 0o700` — deterministic regardless of umask. **Why it mattered:** a world-readable `./data` defeats blind-review at the filesystem layer.

### 5.2 Null-byte key crashed with a bare `ValueError` (security → guard hardening)

A key containing `\x00` made `Path.resolve()` raise a *bare* `ValueError` rather than the store's `InvalidBlobKey`, so a caller catching the specific type would miss it. Fixed: an explicit null-byte check in `_resolve` raising `InvalidBlobKey`, keeping the "bad key" contract uniform. **Why it mattered:** callers must be able to distinguish a bad key from a disk error by type; a leaking bare `ValueError` breaks that contract.

### 5.3 Escaping symlinks leaked into `list_keys` (security → read-side hardening)

The write side rejected symlink escapes, but `list_keys` used `rglob`, which *follows* symlinks — so a link inside root pointing outside could enumerate outside files into the listing. Fixed: a realpath filter (`p.resolve().is_relative_to(self._root)`) so the read side is as strict as the write side. **Why it mattered:** a guard that only covers writes still leaks the tree on reads.

---

## 6. Deferred to Phase 3+ (security-noted, non-blocking)

1. **Symlink TOCTOU** — `_resolve` realpaths, then IO runs in a separate `to_thread`; a symlink planted between the two could be followed. Acceptable while no adversarial caller reaches the store directly; revisit if untrusted keys ever hit it.
2. **Unbounded blob size** — `put` writes whatever it is handed. The upload size cap is a **Phase 3** concern (resume ingest, at the HTTP boundary).
3. **Unbounded `list_keys` walk** — `rglob("*")` walks the whole subtree into memory; pagination lands with the flush/retention call site (**Phase 5**).

Also recorded for the call-site port: `list_keys(prefix)` is **directory-scoped**, not MinIO's substring-prefix match — Phases 3/4 must map hris's `list_objects(prefix=…)` to a directory boundary (ADR-005 §3).

---

## 7. Metrics

| Metric | Value |
|---|---|
| Commits on branch | 4 (red → green → red-harden → green-harden) |
| Unit tests | 240 |
| Coverage | 99.46% (threshold 80%) |
| New `src` module | `core/src/storage/` (`blob_store.py` + `__init__.py`) |
| New public surface | `BlobStore`, `BlobNotFound`, `InvalidBlobKey`, `get_blob_store` |
| Live HTTP routes | still only `/health` — the store has no HTTP surface yet |
| Call sites invoking the store | none yet (ported in Phases 3–6) |

**Reporting note:** produced in a docs environment without a shell, so commit hashes and a fresh coverage run could not be recomputed here. Test count (240) and coverage (99.46%) are the figures from the verified `python:3.11-slim` container gate run; the interface, guard behaviour, perms, and wiring claims were verified directly against `core/src/storage/blob_store.py`, `core/src/api/main.py`, and `core/src/worker/main.py`.
