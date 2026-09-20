# End-to-end run of 2026-09-17 — what was driven, what broke, what the DTO should know

Companion to [managers-guide.md](managers-guide.md). The guide was written
first, from the code; this run followed it step by step against an
**isolated copy** of the product built from the same tree as the live site
(`docker compose -p recruiter-stress`, CAS off, its own database and blob
store, the real `gpt-oss:20b` on the shared GPU host). The live site was not
touched: with CAS on, every write needs a real SFU login, which a script
cannot do. Nothing in this document names a candidate.

## What was run

| Step of the guide | How | Result |
|---|---|---|
| Sign in, roles, pending access, admin grants roles | **Not scriptable** (real CAS only) | Owed to a human on the live site — see "What only you can check" |
| Create a requisition from a JD file, parse, open | `scripts/smoke.sh` against the isolated stack | 10 of 10 passed, 901 s with the real model |
| Manager's additional requirements, edited on an existing job | by hand, browser route | Saved; extraction ran (138 s); "Added by you" chip on the card after a regenerate |
| Department and campus form | by hand | Saved and rendered |
| Upload résumés with a cover-letter pair, parse | smoke | 3 of 3 parsed |
| Import the Taleo roster CSV | by hand, a CSV built from what the app extracted | 3 of 3 matched once the CSV was well-formed; see finding 3 |
| Declare work authorization on a shortlist card | by hand | Written, audited; the not-eligible band renders "n/a" on every sub-score |
| Generate / regenerate the shortlist | by hand | Works when the queue is empty; see finding 2 |
| Read the shortlist: chips, uplift, disclosures | by hand | Manager term measured (1.0), CUPE-internal +0.05 applied and disclosed |
| Withdraw with a reason, reinstate, leaves the list, marked on the job page | smoke | Passed |
| CSV export | by hand | 200, 33 columns, one row per ranked candidate, anonymised |
| Candidate page, audited document download | by hand | PDF returned through the one-shot form |
| Audit page, withheld-reason reveal | smoke + by hand | Passed |
| Blind review switch on an existing job | by hand | Off → names shown, reveal forms gone |
| Multi-user stress (stub model) | `USERS=3 RESUMES_PER_USER=3 scripts/stress.sh` | PASS: 3 users × 16 steps, 48 samples, 0 errors, 0 timeouts |
| One user, whole guide, real model | `scripts/e2e.sh` | PASS — smoke 10 of 10 in 1046 s, then all 16 driver steps with 0 errors and 0 timeouts (JD parse 265 s, three résumés plus a cover letter 660 s, ranking 215 s); doctor reports only the CAS-off state, which is the isolated stack's design |

## Findings, in order of what a manager will hit

1. **A fresh install did not boot** (fixed on this branch). On an empty
   graph database the API and the worker create the schema at the same
   moment and deadlock; the API exits. The pilot box never saw it because its
   graph was created months ago. Any new box for the DTO would have. The
   bootstrap now retries.

2. **A second "Generate" while a run is in progress is dropped without a
   word** (recorded, not fixed). Measured sequence: a ranking started; the
   manager's-requirements extraction queued behind it; a second Generate
   arrived and the worker discarded it as `already_running`. The shortlist on
   screen was therefore ranked **before** the roster import and **before**
   the manager's requirements existed, and nothing said so. Only a third
   Generate, posted after the queue drained, produced the expected result.
   What to do today: after editing requirements or importing a roster, wait
   until the page stops saying it is regenerating, then click Generate once.
   Recommended fix: when a run is already in progress, remember that a
   re-run was requested and run it when the current one ends.

   **Fixed 2026-09-17 on `feat/complete-build-gaps`** — a Generate posted
   while a run is in progress is now remembered (`jobs.shortlist_rerun_requested`)
   and the worker runs it once more when the current run reaches a terminal
   state; the shortlist page tells the user a re-run is queued instead of
   discarding the click.

3. **A credential after the name defeats the roster match** (recorded,
   pinned as an expected failure in the tests). A résumé whose extracted name
   reads "First Last, CSM" does not match the Taleo row "Last, First" when the
   row has no email, because the credential becomes an extra name token. With
   an email present the match works. Ask candidates' rows to carry emails;
   names with PMP/CPA/CSM suffixes will otherwise be reported as unmatched.

   **Fixed 2026-09-17 on `feat/complete-build-gaps`** — the name match now
   strips a trailing credential suffix after the last comma, against a closed
   vocabulary; CA/BA/MA are excluded so a genuine surname-as-initials is not
   stripped.

4. **The roster overwrites a recruiter's declaration.** Declaring "eligible"
   on a card and then importing a roster row that says "Study Permit" left the
   candidate "not eligible". This is the documented design (ADR-047: the
   manual control is the correction path). Import the roster first, correct
   by hand afterwards, never the other way round.

5. **The "Why this rank?" page exists but nothing links to it.** It renders
   when addressed directly; no card or export carries its address.

   **Fixed 2026-09-17 on `feat/complete-build-gaps`** — every shortlist card
   now carries a "Why this rank?" link to the entry-detail page.

6. **No screen assigns a requisition to a hiring manager.** The API supports
   it; the UI does not. A hiring manager who signs in today sees an empty job
   list.

   **Fixed 2026-09-17 on `feat/complete-build-gaps`** — the job page now has
   an "Assigned hiring managers" section for admins/recruiters: a list with
   remove, a select of hiring managers to add, and, when nobody is assigned,
   the line "No hiring manager is assigned — this requisition is invisible to
   hiring managers."

7. **Every export is anonymised**, including on non-blind jobs: names become
   "Candidate A", email and phone are blank.

8. **A JD that parsed to zero requirements has no recovery in the UI.** The
   warning says re-parse or replace the JD, but the Re-parse button appears
   only after a failed parse and the description cannot be edited. Create the
   job again from the right document.

   **Fixed 2026-09-17 on `feat/complete-build-gaps`** — a draft JD that
   parsed to zero requirements now shows a Re-parse button and a description
   editor; editing the description clears the parse and re-parses
   automatically. An open job still refuses the edit (409); the advice there
   remains to create a new requisition.

9. **Throughput is the hardware.** Parsing runs at about 29 résumés an hour
   on the shared GPU. The guide's arithmetic (a 315-candidate requisition ≈
   11 hours) held on this run: three résumés and one JD took the smoke suite
   15 minutes end to end.

The guide's per-section "Be aware" boxes list the rest (department is free
text, no notifications, exports, retention stored but not enforced, the
64-token reveal budget past ~32 cards, no HTTP→HTTPS redirect).

## What only you can check, on the live site

- Sign in with SFU CAS from **outside the LAN**; confirm a first-time
  colleague lands on the pending-access page and that you can grant them a
  role from the admin screen.
- Make one real write (a declaration or a withdraw) and confirm it does not
  fail with a 403. The unit tests pin the mechanism behind the proxy; a
  browser is the proof.
- Finding 6 is fixed on `feat/complete-build-gaps` (the job page now assigns
  hiring managers); confirm on the live site that assigning a real hiring
  manager there actually populates their job list, signed in as that
  principal.

## The stress build

`scripts/e2e.sh` brings up the isolated stack and runs one user through the
whole guide with the real model. `scripts/stress.sh` runs N users against
an offline stand-in for the model (`STRESS_LLM=stub`, the default), so the
application tier can be loaded without spending GPU time the pilot users
need; `STRESS_LLM=real` requires an explicit confirmation and is capped at
30 résumés per run. Reports land in `report/` (gitignored) as Markdown and
JSON with per-step percentiles and a verdict that fails rather than skips.

## Addendum 2026-09-19 — the DTO's real Taleo bundle

Driven on the isolated stack with the real bundle (four combined exports of
43–53 pages, one 3-page PDF, the 316-row roster CSV; the PDFs are a subset
of the roster). Nothing here names a candidate.

| Step | Result |
|---|---|
| Upload a real export unsplit | Refused: "43 pages and 19 distinct applicant emails … split it first"; all four exports refuse the same way; the 3-page file is accepted as one résumé |
| Split one export with the splitter (real model) | Intermittent: 20 applicants with one page assigned to nobody (twice), or 18 with two people merged into one file (once). Two new checks: a merged file fails the run; an orphan page with its neighbour's email is attached with a `REPAIRED:` line. Third run: 20 résumés + 4 cover letters, clean |
| Upload the zip plus manifest | 18 accepted, 4 with a cover letter attached, 0 cover-letter rows |
| Parse 19 résumés (real model, concurrency 4) | 15 parsed, 4 failed: 1 skills-pass empty content (known), 3 abandoned by the reconciler at median 715 s / max 1157 s per parse against a 900 s timeout — measured while the splitter was also using the GPU, so contended |
| Import the real roster CSV | 14 of 15 parsed résumés matched (93%), 14 work-authorization facts; 301 roster rows unmatched, the normal whole-pool state |

**Be aware:** the split is the step that needs a human eye until the
in-app confirmation screen exists — the model can merge two applicants and
only an email check catches it. A failed résumé cannot be re-parsed from the
UI today; re-upload it. Parse time on real résumés is long enough that four
at once can exceed the timeout when the GPU is shared; do not run the
splitter and a parse batch at the same time.
