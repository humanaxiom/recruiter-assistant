# Job page redesign: proposal, plan and fleet estimate

| | |
|---|---|
| **Status** | PROPOSED. Waiting on checkpoint H1, the product owner's choice of option. |
| **Opened** | 2026-11-24 |
| **Product owner** | the user, who approves, decides and accepts |
| **Coordinator** | the main Claude Code session, which plans, dispatches, verifies and reports |
| **Visual** | [mockups.html](mockups.html). Open it locally in a browser; it uses the app's real `app.css`. |
| **Practice** | This is the first project run under the [fleet project playbook](../../practice/fleet-project-playbook.md). |

## 1. Problem (user-reported, 2026-11-24)

> "The job page is so cluttered and confusing. Upload (individual, zip, taleo pdf) are not clearly
> delineated. The export taleo split is embedded under all that."

The same day, the product owner believed résumé upload had been removed. It hadn't: the form only
renders on an **open** job, and after a clean slate every job was an unparsed Taleo draft. Nothing on
the page explained why the form was missing. **A layout that hides a feature without saying why reads
as a regression.** That is the strongest evidence for this project.

## 2. What the page is today (inventory)

`core/frontend/templates/job_detail.html` has 508 lines and is rendered by `_render_job_detail`
(`core/frontend/app.py:868`). It is one long scroll with fifteen stacked sections:

| # | Section | Who sees it | Kind |
|---|---|---|---|
| 1 | Header: title, parse badge, Taleo source link | all | read |
| 2 | Edit department & campus (drawer) | writer | setting |
| 3 | Additional requirements (drawer) | writer | setting |
| 4 | Parsed requirements, re-parse | all / writer | read + action |
| 5 | Edit job description (drawer) | writer | setting |
| 6 | Assigned hiring managers | writer | setting |
| 7 | Status transition | writer | lifecycle |
| 8 | Blind review | writer | setting |
| 9 | **Upload résumés**: single or .zip, cover letter, manifest, pasted text | writer, **open job only** | intake |
| 10 | Export résumés: as-is and anonymised ZIP | writer | output |
| 11 | **Split a combined Taleo export** (PDF), pending splits | writer | intake |
| 12 | **Import candidate roster** (Taleo CSV) | writer | intake (enrich) |
| 13 | Résumé status (HTMX poll) | all | read |
| 14 | Résumés table | all | read |

Four of these sections add or enrich candidates (9, 11, 12, plus the zip variant inside 9). They are
interleaved with settings and an export. Two facts constrain the redesign:

- **27 CSRF tokens on the page.** There are 15 one-shot `csrf_token` slots and 12 uses of
  `csrf_page_token`. Moving a form so that its token is no longer minted where it renders fails
  silently, as a 400 on submit. This risk is not visual, and it is the biggest one in the project.
- **The page polls.** Parse status and résumé status refresh through HTMX and must keep working
  wherever they end up.

## 3. Options

All three keep **every existing POST route and the API unchanged**. This is a Flask template and
handler change only; no schema, worker or ranking change.

### Option A: Workspace tabs (RECOMMENDED)

- **Header on every tab:** a persistent header with a lifecycle stepper (Draft → Parsed → Open →
  Closed) and **one primary "next action" button** that changes with the job's state.
- **Tabs:** Overview · Candidates · Requirements · Shortlist · Settings.
- **Adding candidates:** "Add candidates" opens **four separate, labelled cards**, each with a
  one-line "use this when…":
  1. One résumé.
  2. Many résumés (.zip).
  3. Taleo export PDF, which includes reviewing the proposed split.
  4. Taleo roster CSV, which enriches existing candidates and adds none.
- **Empty states explain themselves:** on a parsing draft, the Candidates tab says *why* you can't
  add candidates yet and how long the parse takes.
- **Exports** sit in one menu on the Candidates toolbar.
- **Hiring managers** see the same tabs with intake, export and settings removed.

**Why:** it fixes all three complaints and the "feature vanished" failure. It scales to the next
features (the "ask the pool" chat would be a tab) without the page growing.

### Option B: Guided steps

The job is a four-step flow: Set up · Add candidates · Rank · Review & decide. A step rail sits on the
left, and finished steps collapse.

- **Strength:** best for a first-time user.
- **Weakness:** clumsy for the real workflow, which is non-linear. Recruiters add candidates *after*
  ranking, and re-parse after editing. It needs flow-state logic and a rework of the shortlist and
  decision pages to fit the steps.

### Option C: Tidy the single page

The same single page, with four headed groups (Job · Candidates · Shortlist · Settings), a sticky
in-page nav, intake as the same four cards, and settings collapsed.

- **Strength:** cheapest, lowest risk.
- **Weakness:** it is still one scroll, and the next two features land on it.

## 4. Impact on dev work

| | A: Tabs | B: Steps | C: Tidy |
|---|---|---|---|
| Templates touched | `job_detail.html` split into a shell plus 5 tab partials; `base.html` nav | A's, plus shortlist and entry pages | `job_detail.html` only |
| Flask handler | `_render_job_detail` gains a `tab`; each tab fetches only what it shows, which makes it faster | A's, plus flow-state derivation | none / minimal |
| Redirect (PRG) targets | ~15 POST handlers redirect to the right tab | ~15 + flow | none |
| CSRF token placement | **every one of the 15 one-shot slots re-homed** | same + more | re-ordered only |
| Existing tests rewritten as spec changes (tester, red step) | the 8 files that pin section headings or wording on this page; about 40 frontend files reference the page | more | the same 8, fewer edits |
| `smoke.sh` / e2e driver selectors | update | rewrite | small update |
| Manager's guide (`docs/guides`) | rewrite the job-page chapter | rewrite | touch-ups |
| Backend / API / DB | **none** | none | none |
| Risk to live pilot | low with the flag (§6) | medium | low |

## 5. Plan for Option A (slices)

**One branch and one PR** (CLAUDE.md §0b), built slice by slice. Every slice is red → green through
`./scripts/verify.sh` and is not done until it passes the same gates as any other change.

| Slice | Content | Fleet roles |
|---|---|---|
| **S0** | Design lock (H1) and open questions (§7) answered. Planner writes the slice contract. | coordinator, planner |
| **S1** | Shell: header, lifecycle stepper, next-action button, tab nav (`?tab=`), Overview tab. **Behind `JOB_PAGE_V2` (default off).** | tester → coder |
| **S2** | Candidates tab: table and status poll, the four intake cards, export menu, explained empty states. **Highest risk: tokens.** | tester → coder |
| **S3** | Requirements and Settings tabs: move drawers, assignees, blind review, status. PRG redirects land on the right tab. | tester → coder |
| **S4** | Shortlist teaser tab; hiring-manager and auditor views. | tester → coder |
| **S5** | `smoke.sh` and e2e selectors for v2, then the manager's guide chapter. | tester → coder, docs |
| **R** | Review: reviewer (opus) and security (opus, because of the CSRF re-homing). One round expected, two budgeted. | reviewer, security → tester/coder |
| **H** | Hand-over: smoke and doctor, then a by-hand drive of **every form on every tab as a writer and as a hiring manager**, with output pasted. Preview on the pilot box with the flag on for one manager. | coordinator |

## 6. Rollout: don't change the screen under the managers mid-test

The managers are testing the core functions now, so v2 ships behind a **`JOB_PAGE_V2`** setting that
defaults to **off**:

1. Merge with the flag off. The pilot box looks exactly as it does today.
2. Turn it on for a preview: a per-user toggle, or a second compose profile on a spare port. That is
   decided at H1.
3. Managers compare both layouts (H4).
4. Flip the default (H5). Delete v1 one release later.

## 7. Open questions for H1 (each has a recommended default)

1. **Option.** Default: **A**.
2. **Tab names.** Default: Overview · Candidates · Requirements · Shortlist · Settings.
3. **Roster CSV:** should it be an intake card, or live in Settings? Default: **intake card 4**,
   clearly labelled "adds no résumés".
4. **Preview mechanism for H2/H4:** per-user toggle, or a separate preview stack? Default: **per-user
   toggle**, because it needs no second deployment.
5. **Scope creep guard:** the entry and shortlist pages are *out of scope* except the Shortlist tab
   teaser. Default: **agree**. Restyling them is a separate project.

## 8. Estimate (Option A)

These are measured baselines from this repo, not guesses (see the
[playbook's estimating section](../../practice/fleet-project-playbook.md#5-estimating)):

- **Gate floor:** a full `verify.sh` unit run takes about 39 min and integration about 13 min. A slice
  runs the gate at least twice, once to confirm red and once for green.
- **One slice cycle** (red, green, my verification) has taken **2.5–3 h wall clock**. Red takes about
  1 h; green took 1 h 47 m – 1 h 51 m in each of the decision record's three cycles. The decision
  record went from first red commit to docs commit in **9 h 13 m** (16:13 → 01:26): three cycles, one
  test-inventory fix and the docs.
- **A review round** is one more cycle (about 3 h), and the interview-question feature needed three of
  them. Budget **1–2 rounds**.
- **Cycles run one at a time.** They share one Docker host, and parallel gate runs make the
  testcontainers flake worse. So slices don't overlap; the planner and docs work overlaps with them
  instead.
- **Subagent tokens per cycle** have been about **100–130k for the producer** (red 106k, green 130k
  on the decision record's review round). Reviewer and security usage was not captured.

| | Cycles | Fleet wall clock | Elapsed (with human turnarounds) | Product owner time |
|---|---|---|---|---|
| **A: Tabs** | S1–S5 = 5, + R 1–2, + H | **20–25 h** | **3–4 working days** (the fleet runs evenings too) | ~3 h: H1 30 min, H2 review 45 min, PR 30 min, UAT coordination |
| B: Steps | ~8, + R 2, + H | 32–38 h | 5–7 days | ~5 h |
| C: Tidy | 2, + R 1, + H | 10–12 h | 1.5–2 days | ~1.5 h |

The elapsed time is dominated by **waiting on checkpoints, not by agent work**. A decision answered
within an hour keeps the fleet moving, while one answered the next morning costs a day. That is the
single biggest lever the product owner has on delivery.

## 9. Human checkpoints (where the product owner steers)

| ID | When | Decision | Input |
|---|---|---|---|
| **H1** | now | Option, plus the open questions in §7 | this doc and mockups.html |
| **H2** | after S2 | Is the Candidates tab right? This is the riskiest slice, and the first one worth seeing. | preview link + screenshots |
| **H3** | after R | Push and open the PR; merge | PR with evidence |
| **H4** | after deploy, flag on for one manager | Manager UAT: does it fix the complaint? | manager feedback |
| **H5** | after H4 | Flip the default to v2 | — |

## 10. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| A one-shot CSRF token minted on the wrong tab, so a form 400s silently | **high** without a guard | S2 red step: a test per form that renders its tab and submits; by-hand drive of every form at H |
| Tests that string-match the template break en masse, tempting edits to tests | high | Those rewrites are **spec changes made by the tester in red**, listed in the slice contract; the coder never edits tests |
| HTMX polls break inside tabs | medium | Smoke asserts the poll fragment on the Candidates tab |
| Layout changes under the managers mid-UAT | medium | `JOB_PAGE_V2` default off (§6) |
| Scope creep into the shortlist and entry pages | medium | §7 Q5; anything else goes to the ROADMAP, not this branch |

## 11. Actuals (filled in as slices land, for calibration)

| Slice | Estimate | Actual | Review findings | Notes |
|---|---|---|---|---|
| S0 | | | | |
