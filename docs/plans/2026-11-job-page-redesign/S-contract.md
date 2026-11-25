# S0 slice contract: job-page redesign, Option A (workspace tabs)

| | |
|---|---|
| **Status** | S0 deliverable, DRAFT for product-owner review. Written 2026-11-25 against `main` 765b678, branch `feat/job-page-v2`. |
| **Plan** | [README.md](README.md). §7 decisions are fixed and are not reopened here. |
| **Audience** | The product owner (§0, §6, §12, §13 are the pages to read), and the tester and coder agents (everything else). |
| **Rule of evidence** | Every count or location below has a file:line or a pasted command. Anything unconfirmed says "not verified". |

## 0. What the product owner must decide before S1 starts

Only one item needs a decision, and it gates only part of S1.

| # | Question | Recommended default | Blocks |
|---|---|---|---|
| Q1 | How is the per-user preview stored? Full memo in §6. | **DECIDED 2026-11-25 by the PO: both Option 2 and Option 3.** v2 renders when `JOB_PAGE_V2` is on, **or** the CAS username is in `JOB_PAGE_V2_USERS`, **or** the user's `users.job_page_v2` column is true. | S1 now carries a schema change and is proven by `./scripts/verify.sh all` (§3 S1a). |

The other open questions (§12) have defaults and do not block.

## 1. What S0 found (the plan's premises that did not survive contact with the code)

| Plan said | Code says | Consequence |
|---|---|---|
| "15 one-shot `csrf_token` slots plus 12 `csrf_page_token`" (README §2, HANDOFF.md:88, DECISION-LOG §3.7) | `job_detail.html` has **13** `name="csrf_token"` inputs: **12 page-token and 1 one-shot** (`export_csrf_token`, :423). "15" appears nowhere in the code. Evidence in §4. | The top risk is much smaller. Page tokens are minted for every render by the context processor (`app.py:418`), so they cannot be "minted on the wrong tab". Only the export download needs per-tab minting. Every form still gets a round-trip test (§4). |
| A mis-homed token "fails silently, as a 400" | `_csrf_gate` does `abort(403)` (`app.py:321-323`). The one-shot `export_download` also 403s (`app.py:1359-1362`). | Tests assert 403 as the failure. A missing download form fails by **absence**, because of `{% if ... and export_csrf_token %}` at :420. |
| "27 CSRF tokens on the page" | 13 form inputs, plus the `hx-headers` page token on `<body>` (`base.html:14`). | The figure is stale. |
| "8 files pin section headings or wording" | 8 files mention `job_detail.html`, but only in docstrings. Heading text is asserted in **5 files** (§5). 41 `test_frontend_*` files reference `/jobs/` or `job_detail`, which matches "about 40". | The test inventory in §5 is smaller than feared. Because v1 is untouched, **no v1 test changes**. |
| Résumé status is an "HTMX poll" (§2 row 13) | It is a one-shot lazy load (`hx-trigger="load"`, `job_detail.html:498-503`). Only `parse-status` and `resumes-table` re-poll every 3 s. | See §7. |

## 2. Shared design (applies to S1–S5)

### 2.1 Hard rule: v1 is not touched

With the flag off, the page must render byte for byte as today, because managers are testing live. So:

- `core/frontend/templates/job_detail.html` is **not edited**.
- The shared fragments `parse_status.html`, `resumes_table.html` and `resume_status_breakdown.html` are **not edited**. They are also the poll fragments for the unchanged routes `parse_status` (`app.py:1568`), `resumes_table` (`app.py:1543`) and `resume_status_widget` (`app.py:2567`). v2 includes them as they are.
- `base.html` is **not edited**. v2 needs no nav change, and `base.html:14` carries the `hx-headers` page token that smoke and e2e scrape (`e2e/driver.py:18`).
- Every v2 file is new:
  - `templates/job_detail_v2.html` (shell)
  - `templates/_job_v2_header.html`
  - `templates/_job_v2_tab_overview.html`
  - `templates/_job_v2_tab_candidates.html`
  - `templates/_job_v2_tab_requirements.html`
  - `templates/_job_v2_tab_shortlist.html`
  - `templates/_job_v2_tab_settings.html`
- A URL helper returns `url_for("job_detail", job_id=...)` **unchanged** when v2 is off. It appends `?tab=` only when v2 is on. The existing assertions `Location.endswith(f"/jobs/{job_id}")` (§5) therefore keep passing.

### 2.2 Layout resolution (`frontend/app.py`, new helper)

`_use_job_page_v2() -> bool` is the single seam for Q1. It depends on `Settings.job_page_v2` (bool, default False) in `core/src/settings.py` (near the frontend block at :393-395), and on a per-user override (§6).

- `.env` reaches the frontend through `env_file` (`docker-compose.yml:165`), so there is no compose edit.
- `.env.example` gets the new keys, commented and off.
- `get_settings()` is `lru_cache`d (`settings.py:579`), so a changed `.env` needs `docker compose up -d frontend`. The tests monkeypatch it.

### 2.3 Tab routing

`GET /jobs/<id>?tab=<name>`. `tab` is an enum: `overview | candidates | requirements | shortlist | settings`.

- Missing, unknown, or not permitted for the role: render **Overview with 200**. Never 400, never echo the value.
- The tabs visible to a role are computed in one function. HM and auditor have no `settings` (§9).
- `_render_job_detail(job_id, *, error, status_code)` (`app.py:868`) gains `tab: str | None = None`. When v2 is off the parameter is ignored and the current body runs unchanged. When v2 is on, it fetches only what the tab shows (§2.4).
- The header's "Open for applicants" form posts a hidden `tab` field. `transition_status` validates it against the same enum and falls back to Overview. It is never used as a URL.

### 2.4 Reads per tab, v2 only

The header needs `get_job` everywhere.

| Tab | Reads beyond `get_job` | Mints a one-shot token? |
|---|---|---|
| Overview | none (the résumé-status fragment lazy-loads; the checklist derives from job fields only, Q4) | no |
| Candidates | `list_resumes`; writer only: `list_resume_splits`, `get_latest_resume_export` | **yes**, one: export download, only if the latest export is `ready` |
| Requirements | none | no |
| Shortlist | `list_shortlist` (teaser, top N) | **no. This is a tested invariant, §4.4.** |
| Settings | writer only: `list_job_assignees`, `list_users(role="hiring_manager")` (today `_fetch_job_assignees_and_hiring_managers`, `app.py:837-865`) | no |

### 2.5 Fixed rules for all new templates

- Timestamps are strings in the BFF. Use `| day` (`app.py:346`) and never `.strftime`. The existing raw `{{ s.created_at }}` (`job_detail.html:463`) and `{{ job.external_last_seen_at }}` (:113) are not copied.
- Form `id`s must be unique per page. Cards 1 and 2 post to the same route, and a copy-pasted `id="consent_acknowledged"` would duplicate.
- Every POST form carries `name="csrf_token"`. Polls inherit the header from `<body>`.

## 3. Slices

Per slice the order is tester (red) then coder (green), as in README §5. Reviewer and security run once, as step R, over the whole branch. Each slice names the `./scripts/verify.sh` mode that proves it.

**Mode note.** S1–S4 are Flask templates, handlers and settings only. They touch no `models/`, `api/`, `services/` or `worker/`, and the tests use the Flask test client with a stubbed `api_client`. So `offline` proves them. What `offline` structurally cannot prove is the browser to Flask to API seam. That is proven only by `smoke.sh` and `doctor.sh` at S5 and H. The coordinator runs `./scripts/verify.sh all` once before the PR (S5), as CI does. If Q1 is answered Option 3, S1 becomes `all` (§6).

### S1. Shell, flag, tab routing, Overview

**Goal:** with `JOB_PAGE_V2` on (or the user in the allowlist), `/jobs/<id>` renders the shell: header with lifecycle stepper and next-action button, tab nav, and the Overview tab. The other four tabs exist as placeholders that render the shell with "coming in S2/S3/S4" text, for the dev stack only. Flag off means v1, unchanged.

| Touches | Where |
|---|---|
| `core/src/settings.py` | add `job_page_v2: bool = False`, `job_page_v2_users: str = ""` |
| `.env.example` | two commented keys |
| `frontend/app.py` | `_use_job_page_v2` (new); `job_detail` route `app.py:1563-1565` reads `request.args["tab"]`; `_render_job_detail` `app.py:868-928` branches to `job_detail_v2.html`; tab-enum and per-role-tab helpers (new); `transition_status` `app.py:1583-1603` honours the validated `tab` field (v2 only) |
| New templates | `job_detail_v2.html`, `_job_v2_header.html`, `_job_v2_tab_overview.html` |

**Header content** (all derived from `job` fields, with no extra reads). The conditions mirror v1 at `job_detail.html:25-31`, :310-315 and :353.

| State | Stepper | Next-action (writer) |
|---|---|---|
| draft, `parsed_at` none, no `failure_reason` | Draft (current) | disabled "Waiting for parse…" and a "Refresh status" link to the same tab |
| draft, `failure_reason` | Draft | link "Re-parse the job description" to `?tab=requirements` |
| draft, `parsed_at` set | Parsed | form to `transition_status` (to=open): "Open for applicants" |
| open | Open | link "Add candidates" to `?tab=candidates`, plus "View shortlist" |
| closed | Closed | "View shortlist" |
| archived | Closed (archived pill) | none |

The v1 "View shortlist" link (`job_detail.html:351`, all roles) stays visible as a secondary link. HM and auditor get "View shortlist" only.

**Red tests** (tester, all `core/tests/unit/`):

| File | Intent |
|---|---|
| `test_frontend_job_page_v1_golden.py` | **Byte-for-byte guard.** Goldens are generated from unmodified `main` before any change. They cover four states (writer open job with résumés, writer parsing draft, writer draft with failure, HM) with the flag unset. They render the full page, normalise the two CSRF token values and compare to the stored HTML. They also assert that `?tab=candidates` with the flag off yields the identical v1 page. Failing for the stated reason means the file and golden exist and the v2 marker is absent. |
| `test_frontend_job_page_v2_flag.py` | `Settings().job_page_v2 is False`. Flag on gives a v2 marker (`data-job-layout="v2"`). The allowlist user sees v2 with the flag off, and an unlisted user sees v1. CAS off with the flag off gives v1. A CAS-off dev session can never match an allowlist, so only the global flag applies. `?tab` unknown or missing gives Overview with 200. HM `?tab=settings` gives Overview with 200. `?tab=<script>` is never reflected. |
| `test_frontend_job_page_v2_shell.py` | The header renders on all five tabs. The stepper and next-action match the table above for each state. The tab nav links carry `?tab=` and `aria-current` on the active tab. The Overview page token is present: `e2e/driver.page_token` finds `hx-headers`, and the "Open for applicants" form carries `csrf_token`. The `transition_status` hidden `tab` is validated (junk falls back to Overview, never a redirect to arbitrary input). |
| `test_frontend_job_page_v2_dto_fixtures.py` | Builds every fixture via `JobOut(...).model_dump(mode="json")` (precedent: `test_templates_render_api_shaped_rows.py:102`) with ISO-string timestamps and renders all five tabs, so `.strftime` or a missing field fails here. |

**DoD:** red commit fails only for the reasons above (coordinator greps the log). Green passes `./scripts/verify.sh` (offline). The golden test passes unchanged, and `git diff` on `templates/job_detail.html`, `parse_status.html`, `resumes_table.html`, `resume_status_breakdown.html` and `base.html` is empty. No test edited in green.
**Estimate:** 1 cycle (about 3 h), with a +0.5 risk because the golden capture is new.

### S1a. Per-user preview column (Q1, decided 2026-11-25: column + allowlist)

**Goal:** an admin can turn the v2 preview on or off for one user, durably and audited. `_use_job_page_v2()` returns true if any of three holds: `Settings.job_page_v2`, the CAS username is in `Settings.job_page_v2_users` (comma-separated, trimmed, case-insensitive), or `users.job_page_v2` is true for the signed-in user. The schema change is applied by the API's startup DDL. **No hand edit to any database.**

| Touches | Where |
|---|---|
| `core/src/models/ddl.py` | `ALTER TABLE users ADD COLUMN IF NOT EXISTS job_page_v2 BOOLEAN NOT NULL DEFAULT false`, in the startup statement list (pattern `ddl.py:170`, `:213-214`, `:388-389`). It must be **`NOT NULL DEFAULT false`**, because a first login omits the column (the FU-5 lesson, ADR-019). |
| `core/src/api/routes/auth.py:256` | the CAS status payload (`GET /auth/cas/user`) carries `job_page_v2: bool`, so the frontend reads it from `g.cas_user` with **no extra request per page** |
| API admin route (new) | `PATCH`/`POST` setting a user's `job_page_v2`, admin-only via `require_session_role` (ADR-033). It writes an `audit_log` row carrying who, whom and the new value. The tester confirms the action vocabulary. |
| `core/frontend/api_client.py`, `frontend/app.py` | a client call and a Flask POST handler behind the page token; a control per row on `admin_users.html` ("New job page preview: on/off") |
| `core/src/settings.py` | `job_page_v2: bool = False`, `job_page_v2_users: str = ""` (as S1) |

**Red tests:** `tests/integration/` covers three things against real Postgres: the column exists after startup DDL on a fresh **and** an existing `users` table; a first-login INSERT that omits the column succeeds and reads false; and the admin route flips it, writes one audit row, and returns 403 for recruiter, hiring manager and auditor. Unit tests cover the OR of the three sources, including the CAS-off case (no identity, so only the global flag applies), the payload field, and the admin control rendering only for admins. `test_frontend_csrf_covers_every_write_route.py` picks up the new POST automatically.

**Gate:** **`./scripts/verify.sh all`**: this slice touches `models/` and `api/`.
**DoD:** as S1, plus the integration output pasted.
**Estimate:** +1 to 1.5 cycles (3–4.5 h). It runs straight after S1 and before S2, so H2 can show the preview switched on for one real user.

### S2. Candidates tab (highest risk, then H2)

**Goal:** the Candidates tab holds everything that adds, enriches, exports or lists candidates, with the explained empty states of §8.

| Touches | Where |
|---|---|
| New | `_job_v2_tab_candidates.html`, using in-place unchanged includes of `resumes_table.html` and the résumé-status placeholder (:498-503 equivalent) |
| `frontend/app.py` | `_render_job_detail` v2 Candidates branch: `list_resumes` (`:873`), `list_resume_splits` (`:886`), `get_latest_resume_export` + `csrf.ensure_token(..., action="export")` (`:902-910`) move behind `tab == "candidates"` |
| `frontend/app.py` | redirect and re-render targets for the 7 candidate handlers (table in §3.1) |
| `templates/split_review.html:19` | "Back to the job" link gets `tab=candidates` **only when v2 is on**, so v1 render stays byte-identical. This is a change to an existing template, so a test proves the v1 output is unchanged. |

**Four intake cards** (v1 sources: upload `job_detail.html:353-396`, export :398-431, split :433-469, roster :471-490):

| Card | Label and "use this when…" | Posts to | Notes |
|---|---|---|---|
| 1 One résumé | "Use when a single candidate emailed you their file." | `upload_resumes` `/jobs/<id>/resumes` | Same fields as :355-395 (`files`, cover letter file/text, `consent_acknowledged`). `files` is not `multiple` here. |
| 2 Many résumés (.zip) | "Use when you have a folder of separate files." | `upload_resumes` (same route) | `files` multiple, plus manifest field (:373-379). Both cards carry the consent checkbox with unique ids. |
| 3 Taleo export PDF | "Use for the one combined PDF HR downloads from Taleo; you review the split before anything is imported." | `split_upload` `/jobs/<id>/resumes/split-upload` | Pending splits list (:455-468) lives inside this card. Uses `| day` for `created_at`. |
| 4 Taleo roster CSV | "Use to enrich candidates already added. **Adds no résumés.** Upload after the résumés have finished parsing." | `upload_candidate_roster` `/jobs/<id>/candidate-roster` | The ordering sentence is mandatory. It is HANDOFF.md §4 lesson 1: a roster uploaded before the parse matches 0 of 315 and says nothing about why. |

**Export menu:** a single "Export" control holding the start form (`export_start`) and, when the latest export is `ready`, the one-shot download form (`export_download`), plus the status text (:405-419).

**Polls:** `resumes_table.html` (`id="resumes-table"`) and the lazy résumé-status placeholder (§7).

**Red tests:**

| File | Intent |
|---|---|
| `test_frontend_job_page_v2_candidates.py` | Four cards present for a writer on an open job. Card labels contain the "use this when" text. Card 4 contains "adds no résumés" and the parse-first sentence. Unique element ids on the page. The role × status matrix of §8, one assertion row each. Parsing-draft empty state contains why intake is unavailable and "about 5" minutes. Table renders withdrawn and degraded pills (reuse of the unchanged fragment). Pending splits list, blind-job filename suppression (`app.py:893-897`) and the failing-listing tolerance (`app.py:887-892`) carry over. |
| `test_frontend_job_page_v2_csrf_roundtrip.py` | **The CSRF guard, §4.** One parametrised case per form that renders its tab, extracts the form and submits its token. Also: `test_the_export_download_slot_is_minted_only_on_the_candidates_tab`. |
| `test_frontend_job_page_v2_redirects_candidates.py` | Per handler in §3.1 (candidates rows): flag on gives `Location` with `?tab=candidates`. Error re-renders (400/409) show the Candidates tab with the error. Flag off gives the exact v1 `Location` (a parity twin of the §5 assertions). |
| `test_frontend_job_page_v2_card_scale.py` | Per-card controls at **32 and 50** cards: tests that the Candidates tab with 32 and 50 résumés and a ready export leaves the session one-shot mapping at exactly 1 entry and below `MAX_TOKENS_PER_SESSION`. |

**DoD:** as S1, plus **H2**: the coordinator runs a worktree stack with CAS off and `JOB_PAGE_V2=true`, then sends the product owner screenshots of the Candidates tab in four states (parsing draft, open, ready export, HM). **S3 does not start until H2 is answered.**
**Estimate:** 2 cycles (about 6 h). README budgets 1. This is the slice with 7 handlers, 4 re-render paths, 4 cards and the empty-state matrix.

### S3. Requirements and Settings tabs, PRG parity

**Goal:** move the remaining forms and make every redirect land on the right tab.

| Tab | Content (v1 source) | Forms |
|---|---|---|
| Requirements | Parsed requirements and the `parse-status` poll (:196-197); manager's note read-back (:174-194); additional-requirements drawer (:133-172); re-parse (:221-233); description editor, draft only (:241-262) | `edit_job_requirements`, `reparse_job`, `edit_job_description` |
| Settings (writer only) | Department and campus (:51-87, including the provisional-title note at :93-98 staying in the header area); assigned hiring managers (:268-299); status (:301-334); blind review (:336-347) | `edit_job_details`, add and remove assignee, `transition_status`, `blind_review` |

The mockup shows description and additional requirements as one "Edit" panel with one Save button. **Do not merge the forms** (Q9). They have different effects: the requirements save costs an LLM call and does not re-rank, and the description save clears the parse. Keep the two routes and two forms. They may share one visual panel.

| Touches | Where |
|---|---|
| New | `_job_v2_tab_requirements.html`, `_job_v2_tab_settings.html` |
| `frontend/app.py` | v2 branches for the Requirements and Settings tabs; redirect and re-render targets for 8 handlers (§3.1). Settings reads are fetched only on `tab == "settings"` and only for writers. |

**Red tests:**

| File | Intent |
|---|---|
| `test_frontend_job_page_v2_requirements_settings.py` | Controls present on the right tab for a writer; absent for HM and auditor; description editor only when draft; re-parse only when `failure_reason` or zero-requirements draft (`jd_yielded_no_requirements` logic, :221-225); the `parse-status` poll attributes render on Requirements; the zero-requirements warning (`parse_status.html:32-37`) renders for every role; assignee list `None` renders "could not be loaded" and `[]` the invisibility sentence; status buttons disabled before parse; blind-review button text flips. |
| `test_frontend_job_page_v2_redirects_requirements_settings.py` | The §3.1 rows for Requirements and Settings, success and 409 re-render (`reparse_job`, `edit_job_description`, `transition_status`). Flag off gives v1 `Location`. |
| `test_frontend_job_page_v2_form_inventory_parity.py` | **The "feature vanished" guard.** For a writer, open job, ready export, pending splits, with assignees: the set of POST form actions in v1 equals the union over the five v2 tabs. No form dropped, none duplicated except the deliberate card 1 and card 2, and the header Open button. |
| extended `test_frontend_job_page_v2_csrf_roundtrip.py` | The remaining forms of §4. |

**DoD:** as S1. Parity test green. No test edited in green.
**Estimate:** 1 cycle (about 3 h).

### S4. Shortlist teaser and the HM and auditor views

**Goal:** the Shortlist tab is a teaser with a link to the existing shortlist page, which is not changed. HM and auditor views are finished and pinned.

| Touches | Where |
|---|---|
| New | `_job_v2_tab_shortlist.html` |
| `frontend/app.py` | Shortlist branch reads `api_client.list_shortlist(job_id)` (`app.py:1853` usage pattern) and shows the top 5 with rank, score label and the existing zero-requirements and degraded disclosures (reuse `_fetch_jd_has_no_requirements`, `app.py:808`). |

**The teaser has no per-card forms and no tokens.** There is no reveal, withdraw or decision control. Decisions stay on the shortlist page (`_DECIDER_ROLES`, `app.py:332`). The shortlist page mints 2 one-shot tokens per card up to k=50, which is 100 slots against a cap of 64 (`csrf.py:61-69`). A teaser that did the same would evict the export slot.

**Red tests:**

| File | Intent |
|---|---|
| `test_frontend_job_page_v2_shortlist.py` | Empty, partial and full shortlist render. Blind-review job: no names. "Open full shortlist" links to `job_shortlist`. **At 32 and 50 entries the tab renders ≤ 5 rows and mints zero one-shot tokens (session mapping unchanged).** A previously minted export slot survives a Shortlist-tab render at 50 entries. A backend failure on `list_shortlist` renders a tolerant message, not a 500 (precedent `app.py:821-825`). |
| `test_frontend_job_page_v2_roles.py` | The §9 matrix for all four roles: the tab set and the controls on each. Hiding is a compensating UX control only (the backend is the boundary, ADR-033, `app.py:388-397`), so the test also asserts that a hidden control's route still 403s for the role (reuse the pattern at `test_frontend_write_route_403_handling.py`). |

**DoD:** as S1. **Estimate:** 1 cycle (about 3 h).

### S5. Smoke, e2e, manager's guide, final `all`

**Goal:** the running product is driven in both layouts, and the guide matches v2.

| Touches | Where |
|---|---|
| `scripts/smoke.sh:51-58` | pass `-e SMOKE_EXPECT_JOB_PAGE=${SMOKE_EXPECT_JOB_PAGE:?v1 or v2}`. It fails rather than defaults, in the spirit of its "FAILS rather than skips" rule. |
| `core/tests/smoke/test_product_flows.py` | edits listed in §10 |
| `core/tests/smoke/` (new) `test_job_page_v2_flows.py` | drives each tab and each redirect over HTTP; asserts the layout marker equals `SMOKE_EXPECT_JOB_PAGE` |
| `core/tests/e2e/stress.py`, `driver.py` | no change expected (§10); verify only |
| `docs/guides/managers-guide.md` | rewrite §3 "The JD parse" / "Opening the job" / "Blind review", §4 "Uploading résumés", §5 "Importing the candidate roster", and the assign-a-hiring-manager walk-through at :97, plus "Department and campus after the fact" at :218. Screenshots from the H2 stack. |

**Red tests:** the new smoke module is itself the red (it fails against a v1 stack and against an unimplemented route). Unit side: `test_smoke_expects_a_layout.py` asserts that `smoke.sh` refuses to run without `SMOKE_EXPECT_JOB_PAGE`.

**DoD:** `./scripts/verify.sh` offline and then `./scripts/verify.sh all` both green with output pasted. `smoke.sh` is run twice on a local CAS-off dev stack (once `SMOKE_EXPECT_JOB_PAGE=v1`, once with `JOB_PAGE_V2=true` and `=v2`) and the output is pasted verbatim. `doctor.sh` is pasted. Docs updated.
**Estimate:** 1 cycle (about 3 h), which includes the docs.

### 3.1 PRG table: every POST handler that lands on the job page

"Lines" are in `frontend/app.py`. "v2 tab" applies only when v2 is on. With v2 off, every `Location` stays the bare `/jobs/<id>`.

| # | Handler | Route | Success redirect | 400/409 re-render (`_render_job_detail`) | v2 tab | Slice |
|---|---|---|---|---|---|---|
| 1 | `create_job` | `POST /jobs` | `:650` | n/a (re-renders `index.html`) | Overview (no param) | S1 |
| 2 | `upload_resumes` | `POST /jobs/<id>/resumes` | `:1005` | `:939`, `:955`, `:992` | **candidates** | S2 |
| 3 | `upload_candidate_roster` | `POST /jobs/<id>/candidate-roster` | `:1095` | `:1072`, `:1086` | **candidates** | S2 |
| 4 | `split_upload` | `POST /jobs/<id>/resumes/split-upload` | `:1301` and `:1315` (flash and return); success goes to `split_review` `:1322` (unchanged, off-page) | `:1277`, `:1292` | **candidates** | S2 |
| 5 | `export_start` | `POST /jobs/<id>/resumes/exports` | `:1340` | n/a | **candidates** | S2 |
| 6 | `export_download` | `POST /resume-exports/<id>/download` | Conflict path `:1375` (job id from hidden `job_id`) | n/a | **candidates** | S2 |
| 7 | `split_accept` | `POST /resume-splits/<id>/accept` | via `_redirect_to_split_job` `:1489-1494` | n/a (re-renders `split_review`) | **candidates** | S2 |
| 8 | `split_discard` | `POST /resume-splits/<id>/discard` | via `_redirect_to_split_job` | n/a | **candidates** | S2 |
| 9 | `transition_status` | `POST /jobs/<id>/status` | `:1603` | `:1589` (409) | Settings form: **settings**. Header Open button: the hidden validated `tab`, default **overview**. | S1 / S3 |
| 10 | `edit_job_details` | `POST /jobs/<id>/details` | `:1649`, `:1658` | n/a | **settings** | S3 |
| 11 | `edit_job_requirements` | `POST /jobs/<id>/requirements` | `:1684`, `:1696` | n/a | **requirements** | S3 |
| 12 | `reparse_job` | `POST /jobs/<id>/reparse` | `:1720` | `:1711` (409) | **requirements** | S3 |
| 13 | `add_job_assignee_route` | `POST /jobs/<id>/assignees` | `:1744` | n/a | **settings** | S3 |
| 14 | `remove_job_assignee_route` | `POST /jobs/<id>/assignees/<uid>/remove` | `:1759` | n/a | **settings** | S3 |
| 15 | `edit_job_description` | `POST /jobs/<id>/description` | `:1772`, `:1791` | `:1782` (409) | **requirements** | S3 |
| 16 | `blind_review` | `POST /jobs/<id>/blind-review` | `:1811` | n/a | **settings** | S3 |

`split_correct` (`app.py:1402`) redirects to `split_review` and is not a job-page redirect, so it is unchanged. Link-only references to `job_detail` in `index.html:178`, `jobs_bulk.html:27` and `match_results_cards.html:42` are unchanged (they land on Overview). The `split_review.html:19` link is covered in S2.

**Status of the redirect table:** all 16 handlers are listed with their line numbers; the v2 tab column is a design choice that rests on README §5's S3 brief ("assignees, blind review, status" in Settings).

## 4. CSRF inventory (the top risk, re-measured)

### 4.1 Commands and output (run from `core/frontend/templates`)

```
$ grep -n "csrf_token\|csrf_page_token" job_detail.html
57:      <input type="hidden" name="csrf_token" value="{{ csrf_page_token }}">
141:      <input type="hidden" name="csrf_token" value="{{ csrf_page_token }}">
229:      <input type="hidden" name="csrf_token" value="{{ csrf_page_token }}">
246:      <input type="hidden" name="csrf_token" value="{{ csrf_page_token }}">
279:        <input type="hidden" name="csrf_token" value="{{ csrf_page_token }}">
290:    <input type="hidden" name="csrf_token" value="{{ csrf_page_token }}">
307:      <input type="hidden" name="csrf_token" value="{{ csrf_page_token }}">
340:    <input type="hidden" name="csrf_token" value="{{ csrf_page_token }}">
357:    <input type="hidden" name="csrf_token" value="{{ csrf_page_token }}">
402:    <input type="hidden" name="csrf_token" value="{{ csrf_page_token }}">
420:  {% if export_latest.status == 'ready' and export_csrf_token %}
423:    <input type="hidden" name="csrf_token" value="{{ export_csrf_token }}">
437:    <input type="hidden" name="csrf_token" value="{{ csrf_page_token }}">
475:    <input type="hidden" name="csrf_token" value="{{ csrf_page_token }}">

$ grep -c "csrf_page_token" job_detail.html        -> 12
$ grep -n 'name="csrf_token"' *.html | grep -v csrf_page_token   (every one-shot input in every template)
job_detail.html:423:   value="{{ export_csrf_token }}"
resume_detail.html:78,149,173,206,219,257   (csrf_token / withdraw / document / work_auth)
shortlist_cards.html:253,274
$ grep -n "include" job_detail.html     -> :197 parse_status.html, :506 resumes_table.html  (neither carries a token)
$ grep -n "ensure_token\|issue_token(" ../app.py
910: csrf.ensure_token(export_latest["id"], action="export")      <- the ONLY job-page mint
1843 (_mint_card_tokens, shortlist), 2205-2221 (resume_detail)
```

**Counts:** 12 page-token uses (matches README), **1 one-shot** (README said 15), 13 form inputs in total.

### 4.2 Per-form table (13 forms; the single one-shot slot is row 13)

All page-token rows: the token is `{{ csrf_page_token }}`, injected into **every** render by `inject_current_user` (`app.py:418`, idempotent `csrf.issue_page_token()`), verified non-consumingly by `_csrf_gate` (`app.py:316-324`). So moving the form to another tab changes nothing about minting. The only way to break one is to write the wrong variable name, and the failure is then a 403.

| # | v1 file:line | Form | POST route (endpoint) | Token kind and v1 mint | v2 tab | v2 mint |
|---|---|---|---|---|---|---|
| 1 | `job_detail.html:57` | Edit department & campus | `/jobs/<id>/details` (`edit_job_details`) | page; context processor `app.py:418` | Settings | context processor, unchanged |
| 2 | :141 | Additional requirements | `/jobs/<id>/requirements` (`edit_job_requirements`) | page; same | Requirements | same |
| 3 | :229 | Re-parse JD | `/jobs/<id>/reparse` (`reparse_job`) | page; same | Requirements | same |
| 4 | :246 | Edit job description | `/jobs/<id>/description` (`edit_job_description`) | page; same | Requirements | same |
| 5 | :279 | Remove assignee (per row) | `/jobs/<id>/assignees/<uid>/remove` (`remove_job_assignee_route`) | page; same | Settings | same |
| 6 | :290 | Add assignee | `/jobs/<id>/assignees` (`add_job_assignee_route`) | page; same | Settings | same |
| 7 | :307 | Status transition (one per legal next state) | `/jobs/<id>/status` (`transition_status`) | page; same | Settings; **plus** the header "Open for applicants" form (extra instance) | same |
| 8 | :340 | Blind review toggle | `/jobs/<id>/blind-review` (`blind_review`) | page; same | Settings | same |
| 9 | :357 | Upload résumés | `/jobs/<id>/resumes` (`upload_resumes`) | page; same | Candidates, cards 1 and 2 (two instances) | same |
| 10 | :402 | Start export | `/jobs/<id>/resumes/exports` (`export_start`) | page; same | Candidates (export menu) | same |
| 11 | :437 | Split a Taleo export | `/jobs/<id>/resumes/split-upload` (`split_upload`) | page; same | Candidates, card 3 | same |
| 12 | :475 | Import roster CSV | `/jobs/<id>/candidate-roster` (`upload_candidate_roster`) | page; same | Candidates, card 4 | same |
| **13** | **:423** | **Download the export (audited)** | `/resume-exports/<id>/download` (`export_download`, exempt from the page-token hook, `app.py:279`) | **one-shot**, slot `(export_id, "export")`. Minted in the handler `_render_job_detail` `app.py:909-910` via `ensure_token` (reuse-if-live) **only** when the writer's latest export is `ready`. Consumed on first valid POST (`app.py:1359-1362`). | Candidates (export menu) | `_render_job_detail` v2 Candidates branch, same call, on **no other tab** |

HTMX posts (none are on this page today) would inherit `X-CSRF-Token` from `<body>` (`base.html:14`). The shell extends `base.html` unchanged, so nothing is lost.

### 4.3 Guards, one per form (all in `test_frontend_job_page_v2_csrf_roundtrip.py`)

Each case does the same: with the flag on and a writer session, GET the form's tab with the Flask test client (DTO fixtures via `model_dump(mode="json")`), extract the `<form action=...>` and its hidden `csrf_token` (via `html.parser`, not a regex over the template), then POST it back on the same client (same cookie jar) with a stubbed `api_client`. Assert: not 403, the stub is called, and `Location` carries the §3.1 tab.

| Guard test (parametrised id) | Covers rows |
|---|---|
| `test_form_roundtrips[details]` | 1 |
| `test_form_roundtrips[requirements]` | 2 |
| `test_form_roundtrips[reparse]` | 3 (fixture has `failure_reason`) |
| `test_form_roundtrips[description]` | 4 (fixture is draft) |
| `test_form_roundtrips[assignee_remove]` | 5 |
| `test_form_roundtrips[assignee_add]` | 6 |
| `test_form_roundtrips[status_settings]`, `[status_header]` | 7 (both instances) |
| `test_form_roundtrips[blind_review]` | 8 |
| `test_form_roundtrips[upload_single]`, `[upload_zip]` | 9 (both cards) |
| `test_form_roundtrips[export_start]` | 10 |
| `test_form_roundtrips[split_upload]` | 11 |
| `test_form_roundtrips[roster]` | 12 |
| `test_export_download_token_roundtrips_once` | 13: the form renders on Candidates, the token verifies, the POST streams, a second POST with the same token is 403 (one-shot), and the page token does not open it (precedent `test_download_without_a_token_or_with_a_page_token_is_403`, `test_frontend_resume_export_job.py:447`) |
| `test_every_post_form_on_every_v2_tab_has_a_nonempty_token` | structural: render 5 tabs × {admin, recruiter, hiring_manager, auditor} × {parsing draft, open, closed}; every `<form method="post">` has a non-empty `csrf_token` |
| `test_missing_token_is_403_not_a_silent_success` | one form with the token stripped gives 403 |

### 4.4 Does per-tab rendering change the `MAX_TOKENS_PER_SESSION = 64` budget?

**It reduces it, and nothing the page does can evict a token the user still needs.**

| Question | Answer and evidence |
|---|---|
| One-shot tokens minted by one v1 job-page view | At most 1: the export slot, on every render, if the export is `ready` and the viewer is a writer (`app.py:900-910`). |
| One-shot tokens minted by one v2 tab view | At most 1, on **Candidates only**. Overview, Requirements, Shortlist and Settings mint 0. |
| Do the polls mint? | No. `ensure_token`/`issue_token` call sites are only `app.py:910, 1843, 2205-2221`. The routes `resumes_table` (:1543), `parse_status` (:1568) and `resume_status_widget` (:2567) call none. |
| Can the job page fill the cap? | No. It owns 1 slot of 64. |
| What can evict the export slot? | Only the **shortlist page**, which mints 2 per card up to k=50 (`csrf.py:61-69`), so 100 against 64, FIFO. The export slot, being old, goes first. This exists in v1 today: a stale job page whose export slot was evicted by a later 32+ card shortlist visit 403s on Download. **Unchanged by v2, recorded, not planned (§12).** |
| What would make v2 worse? | A Shortlist-tab teaser that mints per-card tokens. It is forbidden, and `test_frontend_job_page_v2_shortlist.py` guards it at 32 and 50 entries (S4). |
| Does a render re-mint after eviction? | Yes: `ensure_token` mints when the slot is empty, so reloading Candidates restores a working form. |

## 5. Existing tests whose spec changes

**Method.** With the flag off (the default) v1 renders byte for byte, so **no existing test changes and the coder edits none.** Every test below stays as the v1 test, unchanged. Where the control moves to a v2 tab, the tester writes a v2 twin in red, in the new files of §3. Tests that assert only a redirect target need a twin only to prove the v2 `?tab=`.

Greps used (run in `core/tests/unit`):

```
grep -n "job_detail.html" *.py            -> 8 files, all docstring mentions (none reads the template file)
grep -nE "<heading/wording regex of the 14 headings and button labels>" test_frontend_*.py test_manager_requirements_editable.py test_templates_render_api_shaped_rows.py
    -> heading/wording asserted in 5 files: job_assignees (5), job_detail (1), resume_export_job (2), split_review (9), templates_render_api_shaped_rows (1)
grep -n '\.get(f\?"/jobs/<one-segment>"' test_{frontend,manager,templates}*.py      -> 47 GETs of the job page in 17 files
grep -nE "Location.*(jobs/|job_detail)" ...                                         -> 15 redirect assertions
grep -lE "/jobs/|job_detail" test_frontend_*.py | wc -l                             -> 41
```

### 5.1 Tests that render the job page (v1 stays; v2 twin where marked)

| File::test | What it pins | v2 twin (tab) |
|---|---|---|
| `test_frontend_job_detail.py::test_job_detail_open_button_disabled_while_unparsed` (:224), `::test_job_detail_open_button_enabled_once_parsed` (:246, asserts "Open for applicants" :248), `::test_job_detail_only_shows_legal_next_states` (:276, `Location` :292) | status buttons and parse gating | yes: header and Settings |
| `::test_open_job_upload_form_shows_parse_time_hint` (:264) | upload hint | yes: Candidates |
| `::test_job_detail_page_includes_a_lazy_loading_status_breakdown_placeholder` (:374) | the `hx-get .../resume-status` placeholder | yes: Candidates and Overview |
| `::test_a_withdrawn_candidate_is_marked_in_the_resume_table`, `::test_an_active_candidate_is_not_marked_withdrawn`, `::test_the_withdrawn_row_is_retained_not_hidden`, `::test_the_withdrawn_marker_says_the_candidate_is_out_of_the_shortlist` (:545-601) | table markers | yes: Candidates (the table fragment is reused unchanged) |
| `test_frontend_job_assignees.py::test_assignees_section_renders_with_current_assignees_and_a_select` (:122, "Assigned hiring managers" :124), `::test_no_hiring_manager_assigned_shows_the_invisibility_sentence` (:139, :142), `::test_a_failed_assignee_fetch_shows_a_could_not_load_message_not_the_empty_one` (:184, :188), `::test_section_is_hidden_for_a_non_writer_session` (:214, :218) | heading and sentences | yes: Settings |
| `test_frontend_job_details_edit.py::test_the_detail_page_offers_a_department_and_campus_form` (:64), `::test_the_campus_options_come_from_the_canonicaliser` (:80), `::test_a_provisional_title_is_explained_on_the_page` (:112), `::test_a_chosen_title_gets_no_such_note` (:123) | dept and campus drawer; provisional-title note | yes: Settings; the note in the header area |
| `::test_the_description_textarea_renders_when_draft_and_writer` (:274), `::test_the_description_textarea_is_absent_when_not_draft` (:289) | description editor | yes: Requirements |
| `test_frontend_job_reparse.py::test_job_detail_renders_the_reparse_control_when_the_parse_failed` (:156), `::test_job_detail_shows_why_the_parse_failed` (:174), `::test_job_detail_hides_the_reparse_control_when_nothing_failed` (:189) | re-parse and reason | yes: Requirements, plus the header badge |
| `test_manager_requirements_editable.py::test_the_job_page_offers_an_input_for_the_note` (:145), `::test_the_existing_note_is_prefilled_for_editing` (:160), `::test_the_page_says_the_shortlist_needs_regenerating` (:174) | additional-requirements drawer | yes: Requirements |
| `test_frontend_zero_requirements_disclosure.py::test_job_detail_warning_renders_for_every_session_role` (:438, helper `_get_detail` :144) | `jd-no-requirements-warning` for every role | yes: Requirements and Overview |
| `test_frontend_jd_parse_failed_display.py` (tests near :105, "Wait for the LLM…" hint) | tri-state badge, disabled Open | yes: header |
| `test_frontend_resume_upload.py::test_job_detail_open_job_shows_upload_form_with_consent_checkbox` (:971) | upload form + consent | yes: Candidates (cards 1 and 2) |
| `test_frontend_candidate_roster_upload.py::test_the_roster_upload_form_renders_on_the_job_page_for_a_writer` (:112) | roster form | yes: Candidates card 4 (+ "adds no résumés" assertion is new) |
| `test_frontend_split_review.py::test_job_page_has_the_split_upload_form_for_a_writer` (:216, "Split a combined Taleo export" :218), `::test_split_form_csrf_token_is_the_reusable_page_token` (:239), `::test_job_page_hides_the_split_form_from_a_non_writer` (:270, :273), `::test_job_page_lists_pending_splits_with_review_links_and_status` (:1289), `::test_job_page_has_no_pending_section_when_there_are_none` (:1301), `::test_job_page_survives_a_failing_pending_listing` (:1320), `::test_job_page_pending_list_shows_no_filename_on_a_blind_job` (:1332), `::test_job_page_pending_list_is_hidden_from_non_writers` (:1368) | heading text "Split a combined Taleo export" and "Pending splits" (a v2 heading change is expected; the twin pins the new card label) | yes: Candidates card 3 |
| `test_frontend_resume_export_job.py::test_job_page_has_the_post_start_form_with_a_token_for_a_writer` (:227, "Export résumés (as-is + anonymized)" :237), `::test_job_page_hides_the_export_from_a_non_writer` (:243, :266), `::test_job_page_shows_a_non_ready_status_and_no_download_form` (:272), `::test_job_page_with_no_export_shows_only_the_start_button` (:283), `::test_job_page_survives_the_latest_lookup_failing` (:297), `::test_a_ready_export_renders_a_post_download_form_with_a_live_token` (:309) | export start and download, status text | yes: Candidates export menu (the v2 label may differ; the twin pins it) |
| `test_frontend_write_controls_hidden_for_non_writer_roles.py::test_job_detail_hides_every_write_control_for_a_non_writer_session` (:101), `::test_job_detail_shows_the_write_controls_for_a_recruiter_session` (:121) | `action="/jobs/{id}/status"`, `/blind-review`, `/resumes` presence | yes: §9 matrix, S4 |
| `test_templates_render_api_shaped_rows.py::test_the_job_detail_page_renders_a_real_api_body` (:163), `::test_the_job_detail_page_renders_real_assignee_and_hiring_manager_rows` (:205), `::test_the_job_detail_page_renders_a_bare_job` (:229), `::test_the_job_detail_page_shows_zero_requirements_warning` (:250) | DTO-shaped fixtures render without raising | yes: S1 `test_frontend_job_page_v2_dto_fixtures.py` renders all five tabs |
| `test_frontend_styling.py::test_job_detail_uses_card_and_status_pill` (:125) | `card` and `pill` classes on the page | yes: header pill and cards (S1) |
| `test_frontend_routes.py::test_job_detail_renders_job_and_its_resumes` (:128), `::test_job_detail_404s_when_the_job_does_not_exist` (:139), `::test_job_detail_backend_unreachable_is_not_a_500` (:152) | 200 / 404 / 503 | yes: S1, per tab |
| `test_frontend_auth_gate.py` (:103), `test_frontend_auth_widget.py` (:244), `test_frontend_pending_access.py` (:133) | CAS gate, header widget, pending access | no: gating happens before any layout branch |

### 5.2 Redirect assertions (v1 unchanged; twin proves `?tab=`)

`test_frontend_candidate_roster_upload.py:139`, `test_frontend_job_detail.py:292`, `test_frontend_job_details_edit.py:147, 344`, `test_frontend_resume_export_job.py:338, 372, 508`, `test_frontend_resume_upload.py:363`, `test_frontend_shortlist_top_percent.py:229`, `test_frontend_split_review.py:384, 941, 1036`, `test_frontend_create_job.py:309` all assert `Location.endswith(f"/jobs/{job_id}")` or `str(job_id) in Location`. They pass untouched with the flag off. Twins are in the `..._redirects_*` files of S2 and S3. `test_frontend_resume_detail.py:1281` and `test_frontend_work_authorization_on_shortlist.py:341` redirect to the shortlist, not the job page, and are unaffected.

### 5.3 Route-protection tests that need no twin

`test_frontend_csrf_covers_every_write_route.py` enumerates `app.url_map`, and `_MINIMUM_EXPECTED_WRITE_ROUTES = 12`. S1–S5 add **no POST route** under the recommended Q1 option, so it needs no change. Under Q1 Option 1 a new POST route would be covered by it automatically. `test_frontend_csrf_write_route_enforcement.py` asserts the exemption set (`app.py:260-281`) is exactly the six one-shot endpoints. No new exemption is added.

## 6. Decision memo Q1: where the per-user preview is stored

**What the product owner decided (README §7 Q4):** a per-user toggle behind `JOB_PAGE_V2` (default off), with no second deployment. Managers keep the current page during their test.

**What H4 needs:** "flag on for one manager". The word "toggle" leaves open whether the user or the operator flips it.

**What the existing code gives us:**

- Tables are created at startup from the `ddl.py` statement list. There is no migration framework (CLAUDE.md). A new column is `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in that list (pattern at `ddl.py:170`, `:213-214`, `:388-389`).
- The FU-5 lesson (CLAUDE.md): `users.role` was declared `NOT NULL` with no `DEFAULT` and the first real INSERT failed, which only the integration run saw. Any new column here must be `NOT NULL DEFAULT false`, or nullable. The relaxed `users.role` ALTERs are at `ddl.py:673-674`.
- The frontend identifies a person only through the CAS status dict `g.cas_user` (`app.py:233`), key `username` (`base.html` uses `current_user.username`). With CAS off, `g.cas_user` is never set (`app.py:225-226`), so there is no identity at all.

| | **Option 1: Flask session key** | **Option 2: allowlist in settings (recommended)** | **Option 3: `users` column** |
|---|---|---|---|
| How | The user clicks a "Try the new layout" link, `POST /prefs/job-page`, and `session["job_page_v2"]` is set. | `JOB_PAGE_V2_USERS=alice,bob` in `.env`. v2 if the CAS username is listed, or `JOB_PAGE_V2=true`. | `ALTER TABLE users ADD COLUMN IF NOT EXISTS job_page_v2 BOOLEAN NOT NULL DEFAULT false`. Read via `/auth/cas/user`, written by a new API route. |
| Schema change | No | No | **Yes**: `core/src/models/ddl.py`, plus `api/routes/auth.py:256` (status payload), a new route, `frontend/api_client.py`, a Flask handler, tests |
| Gate | `offline` (+ final `all`) | `offline` (+ final `all`) | **`all`** (SQL, route, real Postgres) |
| Cost over the recommended plan | +0.5 cycle | 0 | **+1 to 1.5 cycles** (3–4.5 h) |
| Visible to managers who are not previewing? | **Yes**: a new link appears on every manager's screen, which breaks "managers keep today's page" | No: nothing changes for anyone not on the list | Only if the UI shows the toggle to all |
| Survives logout / another browser | Per browser. Not verified whether it survives logout (no `session.clear` in `app.py`; the logout link goes to the backend). Another browser starts as v1. | Yes, both (it is config) | Yes, both |
| Audited | No | No row in `audit_log`; the audit trail is the `.env` change | Could write an `audit_log` row (action vocabulary not verified) |
| Who may flip | The user, for themselves | The operator who edits `.env`, then `docker compose up -d frontend` | The user, or an admin, by design |
| Drivable by `smoke.sh` (CAS off) | **Yes** | Only through the global `JOB_PAGE_V2`. The allowlist needs a CAS identity, so it is driven by hand on an authenticated box (as the hand-over rule already requires for such features). | No (needs identity) |

**DECIDED 2026-11-25 by the PO: Options 2 and 3 together** (allowlist in `.env` **and** a `users` column set by an admin, combined by OR with the global flag). Built as slice S1a (§3). The coordinator's original recommendation follows for the record.

**Recommended default (superseded): Option 2.** It is the only option that leaves the screen byte-for-byte unchanged for everyone not named (the standing rule: "any UI change ships behind a default-off flag"). It needs no schema and no second deployment, it matches H4 literally, and flipping it for everyone at H5 is `JOB_PAGE_V2=true`.

**What changes if the product owner picks otherwise:**
- *Option 1* (self-serve): add one POST route and a link shown only when `JOB_PAGE_V2` is on, so the master flag must then mean "preview available to all". Smoke can drive it.
- *Option 3* (durable, audited, admin-settable): S1 grows a schema slice and becomes `all`. README §12 step 3 already budgets for that ("rides with S1").
- In all cases `_use_job_page_v2()` is the only function that changes. Everything else in the contract is unchanged.

**Interpretation note:** Option 2 is a per-user switch that the operator flips, not one the user flips. If the product owner meant the latter, answer Option 1 or 3.

## 7. HTMX polls

| Poll | Today | Moves to | v2 rule | How smoke asserts it there |
|---|---|---|---|---|
| Parse status (re-polls every 3 s while `parsed_at` is null and there is no `failure_reason`) | `parse_status.html:17-21` (`hx-get` `parse_status`, `hx-trigger="every 3s"`, `hx-swap="outerHTML"`); included at `job_detail.html:197`; route `app.py:1568-1580` | **Requirements**, and Overview (the same unchanged include; one per page, so `id="parse-status"` stays unique) | The stepper and next-action in the header are rendered once per request and are **not** polled, as in v1 (`job_detail.html:25-31` is static). The parsing state shows a "Refresh status" link (Q6). | `GET /jobs/<id>?tab=requirements` on a parsing draft contains `id="parse-status"` and `hx-trigger="every 3s"` and `hx-get` ending `/parse-status`. The fragment route itself is unchanged, so `smoke/conftest.py:158` (`"badge-parsing" not in client.get(f"/jobs/{job_id}/parse-status").text`) is untouched. A parsed job's tab has no `hx-trigger`. |
| Résumés table (re-polls every 3 s while any row is uploaded/parsing) | `resumes_table.html:12-17`; included at `job_detail.html:506`; route `app.py:1543-1560` | **Candidates** | Unchanged fragment. | `GET /jobs/<id>?tab=candidates` contains `id="resumes-table"`, plus `hx-trigger="every 3s"` while pending. Fragment-level assertion `smoke/conftest.py:191` (`pill-parsed` count from `/resumes-table`) is untouched. |
| Résumé status breakdown (a **one-shot lazy load**, `hx-trigger="load"`, not a re-poll) | `job_detail.html:498-503`; fragment `resume_status_breakdown.html`; route `app.py:2567-2581` | **Candidates** (above the table) and Overview | Same placeholder markup on both tabs, never both on one page. | The tab contains `hx-get=".../resume-status"` and `hx-trigger="load"`; `GET /jobs/<id>/resume-status` returns the 5 buckets. |

All three fragments inherit `X-CSRF-Token` from `<body>`. None of them posts.

## 8. Empty states: Candidates tab, role × job status

The 2026-11-24 incident: the upload form renders only when `job.status == 'open' and is_writer` (`job_detail.html:353`), with no explanation. The same page shows the split form (:433) and roster form (:471) to every writer regardless of status. The measured parse time is about 5.5 min per posting, 4 at a time (HANDOFF.md:61-64).

Roles: **writer** = admin or recruiter (and the CAS-off dev session, `app.py:343`). **non-writer** = assigned hiring manager, auditor. A user with no role never reaches the page (`pending_access.html`, `app.py:235-236`).

| Job state | Writer: banner, always shown first | Writer: controls | Non-writer |
|---|---|---|---|
| **draft, parsing** (`parsed_at` none, no `failure_reason`) | "You can't add résumés yet. Résumé upload opens once the job is **open**, and a job can only be opened after its description has been parsed. That takes about 5 minutes per posting; several postings parse at once, so it may be longer. [Refresh status]" | Cards 1 and 2 absent (v1 parity, :353). Cards 3 and 4 and the export menu shown per v1 parity (:398, :433, :471); Q2 reviews this. | Read-only table, empty state "No résumés yet.", no banner |
| **draft, parse failed** (`failure_reason`) | "The job description could not be parsed (reason shown). Re-parse it on the **Requirements** tab, then open the job." (link `?tab=requirements`) | as above | as above |
| **draft, parsed** | "The job is parsed but not open. Open it to accept résumés." with the "Open for applicants" form (hidden `tab=candidates`) | as above | as above |
| **open** | no banner | cards 1–4, export menu, table | table only |
| **closed** | "This job is closed and no longer accepts résumés." | cards 1–2 absent; cards 3–4 and export per v1 parity | table only |
| **archived** | "This job is archived." | as closed | table only |

Table empty state (all roles, all states): the existing "No résumés uploaded yet." (`resumes_table.html:52`).

**Not verified:** whether the API itself refuses a split or roster upload on a non-open job. The template-only gate for cards 1 and 2 is preserved. The tester pins the banner text per cell above (S2).

## 9. Role views

Where gating happens today:

| Layer | Where |
|---|---|
| CAS gate and pending-access | `_cas_auth_gate`, `app.py:210-241` |
| `is_writer` / `is_decider` / `is_admin` template flags | `inject_current_user`, `app.py:378-429`; roles `_WRITER_ROLES` `app.py:327`, `_DECIDER_ROLES` `app.py:332`, `_is_writer_session` `app.py:335-343` |
| Template gates in v1 | `is_writer` at `job_detail.html:51, 133, 226, 241, 268, 301, 353, 398, 433, 471` |
| Real boundary | Backend `require_session_role` (ADR-033). The frontend flags are compensating UX only (`app.py:388-397`). Hiring managers see only assigned jobs through row scoping (ADR-020, `job_assignees`), enforced by the API. |

| | admin | recruiter | hiring manager (assigned) | auditor |
|---|---|---|---|---|
| Tabs | all 5 | all 5 | Overview, Candidates, Requirements, Shortlist (no Settings) | same as HM |
| Header next-action | per state (§3 S1) | same | "View shortlist" only | "View shortlist" only |
| Overview | full | full | job facts and résumé-status fragment; checklist items that are theirs | same |
| Candidates | table, four intake cards, export menu, pending splits | same | table only ("Read-only for your role") | table only |
| Requirements | parsed requirements, forms, re-parse, description editor | same | parsed requirements and the manager's note, read-only | same |
| Shortlist | teaser and link | same | teaser and link; decisions are on the shortlist page itself (`_DECIDER_ROLES`) | teaser and link, read-only |
| Settings | dept/campus, assignees, status, blind review | same | hidden, `?tab=settings` falls back to Overview | hidden, same fallback |

**Plan fact to fix:** README §3 says "Hiring managers see the same tabs with intake, export and settings removed". Read together with the mockup (`mockups.html`, `p-settings` marked `writer`), the Settings **tab** is removed, not shown empty. This contract follows the mockup.

## 10. `smoke.sh` and e2e: what changes in S5

All fragment routes are unchanged, so most of the existing drivers do not move.

| File:line | Today | Change |
|---|---|---|
| `scripts/smoke.sh:51-58` | no layout awareness | add `-e SMOKE_EXPECT_JOB_PAGE` and fail if unset |
| `core/tests/smoke/conftest.py:158` | `parse-status` fragment poll | none |
| `core/tests/smoke/conftest.py:165, 179` | `_page_token(client, f"/jobs/{job_id}")`, which scrapes the `hx-headers` page token off the job page | none: the token is on `<body>` of every v2 tab (`base.html:14`). The coordinator verifies that the status and upload POSTs still succeed on a v2 stack. |
| `core/tests/smoke/conftest.py:191` | `resumes-table` fragment, counts `pill-parsed` | none |
| `core/tests/smoke/test_product_flows.py:125` | `client.get(f"/jobs/{ranked_job['job_id']}").text` contains "withdrawn" | **change**: in v2 the withdrawn marker is in the table on Candidates, so the URL gains `?tab=candidates` when `SMOKE_EXPECT_JOB_PAGE=v2`. v1 expectation unchanged. |
| `core/tests/e2e/stress.py:259, 308, 318` | fragment routes `/parse-status`, `/resumes-table` | none |
| `core/tests/e2e/stress.py:268, 281, 335` | `_page_token` on the job page, then POST `/status`, `/resumes`, `/candidate-roster` | none (same reason as `conftest.py:165, 179`) |
| `core/tests/e2e/driver.py:18-28` | `_PAGE_TOKEN_RE` on `hx-headers` | none. This is why `base.html` is untouched. |
| New `core/tests/smoke/test_job_page_v2_flows.py` | n/a | the layout marker matches `SMOKE_EXPECT_JOB_PAGE`. Each tab returns 200 as a writer. Each §3.1 POST on the stack redirects to `Location` ending `?tab=<tab>`. The four intake cards and the export menu are present on an open job's Candidates tab. The parsing-draft banner is present. |

The existing unit test `test_e2e_driver.py` pins the driver helpers and is unaffected.

## 11. Traps encoded as tests (HANDOFF.md §4 lessons 6 and 8)

| Trap | Source | Test that pins it |
|---|---|---|
| Timestamps are strings in the BFF: `.strftime` 500s the page, so use `| day` | `app.py:346-375`, HANDOFF §4 lesson 6 | S1 `test_frontend_job_page_v2_dto_fixtures.py` renders all five tabs with ISO-string DTO timestamps; S2 `test_...candidates.py` asserts the pending-split `created_at` renders as `YYYY-MM-DD` |
| A fixture dict can hold a type the API never sends; Jinja renders a missing field as empty | HANDOFF §4 lesson 6 | Every v2 page-test fixture is built with the DTO class and `model_dump(mode="json")` (`JobOut`, `ResumeListItem` `resumes.py:472`, `ShortlistEntry` `matching.py:636`). The export and split read models' DTO classes were not located in S0: the tester finds them. |
| One-shot per-card token budget (64, FIFO) | `csrf.py:61-69`, HANDOFF §4 lesson 8 | S2 `test_frontend_job_page_v2_card_scale.py` and S4 shortlist test at **32 and 50** items |
| A form moved and its token lost | §4 | S2/S3 csrf round trip, S3 form-inventory parity |
| A new control silently shown to the wrong role | `app.py:388-397` | S4 `test_frontend_job_page_v2_roles.py` |
| v1 changed by accident | README §6 | S1 golden test, plus the empty `git diff` on the shared templates |

## 12. Open questions (each with a default) and the found-not-planned list

### Open questions

| # | Question | Recommended default |
|---|---|---|
| Q1 | Preview storage | Option 2 (§6) |
| Q2 | Should the split and roster cards show on a non-open job? v1 shows them to writers in any status (`job_detail.html:433, 471`), but hides upload. | Parity with v1, with the banner (§8). Hiding them would remove a v1 capability, and whether the API allows them on drafts is not verified. |
| Q3 | The mockup's Candidates table has Source and Taleo-status columns, filter chips and a Retry/View link column. `ResumeListItem` (`resumes.py:472-497`) has no source or Taleo-status field, and the API is out of scope. | Omit them. v2 reuses the unchanged table (Candidate, File, Status, Uploaded). |
| Q4 | The mockup's Overview shows "3 / 5 decisions recorded" and a shortlist-ready stat. Not verified: whether the existing read DTOs give a decision count without a new endpoint. | Overview = checklist derived from job fields plus the résumé-status fragment. No decisions stat. |
| Q5 | The Candidates tab label shows a count (mockup: "24"). | Omit. It costs a `list_resumes` on every tab, against "each tab fetches only what it shows". |
| Q6 | Auto-refresh the header when a parse finishes. | No. Show a "Refresh status" link. Same behaviour as v1's static badge. |
| Q7 | Mockup blind review is a checkbox. v1 is a toggle button posting an explicit desired value (`job_detail.html:339-345`). | Keep the button and its form. |
| Q8 | Mockup "Danger zone" for Close and Archive. v1 posts them with no confirmation. | Settings keeps the `transition_status` forms and adds no confirm dialog (parity). |
| Q9 | Merge the description and additional-requirements forms into one "Edit" panel with one Save button (mockup). | **No**: two forms and two routes, which may sit in one visual panel (§3 S3). Merging changes behaviour. |

### Found, recorded, not planned

| # | Defect | Where |
|---|---|---|
| F1 | The withdrawn pill says "Open the candidate to reinstate", but **no row in the job-page table links to the candidate** (the file has no `<a>` at all). Reinstate is reachable only via the shortlist. | `resumes_table.html:41`; no link in rows `:30-50` |
| F2 | The export hint says "refresh to see it", but the export status is not polled. The flash says the same. | `job_detail.html:412-413`; `app.py:1339` |
| F3 | Timestamps rendered as raw strings (no `| day`): `external_last_seen_at`, pending-split `created_at`, `uploaded_at`. They show full ISO text. | `job_detail.html:113, 463`; `resumes_table.html:49` |
| F4 | The header badge is static and is not refreshed by the `parse-status` poll, so it says "parsing…" until a reload. | `job_detail.html:25-31` |
| F5 | A stale job page's export download 403s once a later shortlist visit with 32+ cards has evicted the export slot (FIFO, 2 tokens per card). | `csrf.py:61-69`; `app.py:1843` |
| F6 | A multi-split queue flashes raw URLs as plain text, not links. | `app.py:1317-1321` |
| F7 | `list_resumes` is unbounded on the job page. Already recorded in HANDOFF.md §4 ("an unbounded `_JOB_RESUMES_SQL` fetch"). | `app.py:873` |
| F8 | Stale counts in README §2 and HANDOFF.md:88, and DECISION-LOG §3.7's "27 CSRF tokens", see §1. A doc edit that rides with the next code PR. | docs |

## 13. Estimate (cycle ≈ 3 h; baselines README §8 and playbook §5)

| Step | Cycles | Fleet wall clock | Note |
|---|---|---|---|
| S0 | n/a | about 1 h | this document |
| S1 | 1 | 3 h | golden capture is new, so risk +0.5 |
| S1a | 1–1.5 | 3–4.5 h | Q1 decided as column + allowlist; `verify.sh all` |
| S2 | **2** | 6 h | README budgets 1; 7 handlers, 4 re-render paths, 4 cards, the empty-state matrix, 13 CSRF round trips. **H2 follows.** |
| S3 | 1 | 3 h | |
| S4 | 1 | 3 h | |
| S5 | 1 | 3 h | includes docs and the final `all` |
| R (reviewer and security) | 1–2 | 3–6 h | security because of the CSRF re-homing and the `tab` input |
| H (hand-over) | about 0.7 | 2–3 h | smoke twice, doctor, a by-hand drive of every form on every tab as writer and as HM, flag on for one manager |
| **Total** | 8–9.5 cycles + H | **26–32 h** | includes S1a (Q1 decided as column + allowlist) |

**Checked against README §8** (A: S1–S5 = 5, plus R 1–2, plus H = 20–25 h): this contract is about **3 h above** at both ends. The whole difference is S2 at 2 cycles. If Q1 is answered Option 3, add 1–1.5 cycles (3–4.5 h) for a total of 26–32 h. Elapsed time is dominated by checkpoint turnaround, as README §8 says: H2 (after S2) is the first place a slow answer costs a day.
