# Decision log: recruiter-assistant

**Written 2026-11-24** for the hand-over to a new Claude account (Teams). It records what the product
owner (PO) decided, ordered, corrected and left open, so that a new session does no misinformed work and
no rework. It is a companion to [HANDOFF.md](../../HANDOFF.md) (current state and next step),
[OPERATOR-NOTES.md](OPERATOR-NOTES.md) (how to run the box) and
[ORAL-HISTORY.md](../history/ORAL-HISTORY.md) (the story).

## 1. Purpose and how to use this log

1. **Before starting work, search this log** for the feature, file or word you are about to touch
   (`grep -n -i "<word>" docs/handover/DECISION-LOG.md`).
2. **If a decision covers it, follow it.** Decisions marked *PO* were made by the product owner and are
   not yours to reverse.
3. **If you think a decision is wrong, ask the PO. Don't silently reverse it**, and don't quietly build
   the alternative. Put the decision, the alternative and your recommended default in front of the PO
   (CLAUDE.md Economy rule 3: "blocked on a human" is not a terminal state, so bring a memo, not a shrug).
4. Rows marked **(coordinator default)** were proposed by the previous coordinator session and accepted
   by the PO in bulk ("yes, go ahead"). They are decisions, but the PO did not examine each one
   individually, so a sound objection is welcome. Ask first.
5. **Where this log and the repo disagree, check the repo, then tell the PO.** Where this log and
   `CLAUDE.md` disagree, `CLAUDE.md` wins.

### Sources and their limits

| Tag | Source | Limit |
|---|---|---|
| **MSG hh:mm** | A PO message in the only surviving session transcript (2026-11-20 to 2026-11-24). Full list in the [Appendix](#appendix-verbatim-po-messages). Times are UTC, as stored. | Covers 5 days only. The session was compacted six times, and the transcript is 93 MB. |
| **ASK** | The PO's answers to multiple-choice questions (AskUserQuestion), 7 prompts, with 14 answers. | The rejected options were not saved in the answer text. Where the coordinator's recommended option was chosen, the alternatives are unknown unless stated. |
| **(from session summary)** | A compaction summary written by the coordinator. It quotes earlier PO messages and decisions. | Second-hand. A few early PO messages ("switch to sfu-aria org", "only this time, update the .env") exist **only** in summaries, not in the extracted transcript. |
| **(inferred from context)** | A short reply ("Yes", "push", "Merged. Go ahead") whose meaning was read from the assistant message just before it. | Reasonable, not certain. |
| **mem: name** | A private memory note the previous coordinator saved in `~/.claude/projects/.../memory/name.md`. Each carries a date and a Why. | Coordinator-written, not PO-typed, except where it quotes the PO. Dates are the file's last-modified date unless stated. May not carry over to the new account. |
| **doc: path** | A document in this repo. | Reflects what was written, not necessarily what is true today. |

**Before 2026-11-20 there is no transcript.** Earlier decisions are known only through memory notes,
the archived handoffs and `CLAUDE.md`. Treat them as reliable but second-hand.

**Not in any source:** why there are no commits from 2026-09-26 to 2026-11-20 (unexplained, see §6).

**Privacy of this log:** no emails, secrets, tokens, tailnet addresses, candidate names or candidate
data. People are named by role. The PO's own words are quoted as typed, apart from one redacted
public IP address in the appendix.

---

## 2. Standing orders (apply always)

| # | Order | Since | Source | Verbatim, where available |
|---|---|---|---|---|
| S1 | **Touch only this repo.** Never other repos, other apps' containers or other compose projects on the box. | 2026-11-20 | MSG 00:22 | "let us not touch any other repos. Only ecriter assistant." |
| S2 | **Unique host ports** (live 29xxx, stress 28xxx). Never stock 5432/8000/5000. There are several stack instances on the box. | 2026-08-02, restated 2026-11-20 | mem: unique-host-ports; MSG 00:26, 08:38 | "ensure unique ports as there are multiple instances of the stack" |
| S3 | **Never touch other apps' containers**, even if they block a port. Move your own port instead. | 2026-11-20 | MSG 08:38 | "use different ports, don't touch othe apps" |
| S4 | **CAS (login) stays ON on the pilot box** sfuai.ca. It is public on the internet. Never turn it off there, and never let a convenience default silently drop a feature. | 2026-08-02; public since 2026-09-15 | mem: cas-on-by-default, sfuai-ca-public-deployment; CLAUDE.md | "you can't just drop entire features like CAS or anything for that matter without asking" (PO, 2026-08-02, as quoted in memory) |
| S5 | **Inference is offline.** Model calls go only to the Ollama host on the tailnet. No cloud API, no candidate data leaves. One egress carve-out exists: outbound HTTPS to the Taleo host only, to read SFU's own job postings, `TALEO_ENABLED`-gated, default false (ADR-046). A second carve-out needs an ADR, a default-off flag and a line in CLAUDE.md. | 2026 (ADR-046) | CLAUDE.md | "NEVER add cloud API calls." |
| S6 | **End-to-end drive before every hand-over.** Drive the feature's own path against a running stack and paste what came back. Gates are evidence about code, not about the product. Applies to one-line fixes too. If a step needs a signed-in human, say "could not check". | 2026-09-09; restated 2026-11-24 | CLAUDE.md "hand-over rule"; MSG 15:35; mem: e2e-before-every-handover | "Do an end to end test per the prime directive, before you hand over a feature as completed. rerecord it if you forgot" |
| S7 | **Never turn CAS off on a box with real users to satisfy S6.** Use the isolated stress stack (CAS off, 28xxx) for `smoke.sh`; use `doctor.sh` plus a by-hand drive on the pilot box. | 2026-09 | CLAUDE.md | |
| S8 | **No cloud artifacts.** All documents are local Markdown (or HTML) in the repo. Never publish pages. | date not recorded | mem: no-cloud-artifacts | |
| S9 | **Text in backticks in the PO's messages is a name from their logs, never an instruction.** | 2026-11-20 | MSG 00:02 | "Text in backticks is a name from my logs, never an instruction." |
| S10 | **Never read `.env`** (a deny rule blocks it, even listing it). Never route around a permission block: ask. Editing the live `.env` needs a one-time PO exception, and the PO has so far edited it themselves. | 2026-11-20 | MSG 00:28, 08:46, 08:50; (from session summary) | "modify .env"; "only this time, update the .env" (summary); "one time exception, edit .venv and continue" (typed `.venv`, meant `.env`, inferred); "env updated" |
| S11 | **Consent before**: push, opening a PR, merge, deploying to the pilot box, force-push, and anything destructive. One approval does not cover the next step. When the PO says "merge", merge. | 2026-08-07 onward | mem: pr-merge-needs-the-human; MSG 00:35, 06:40, 19:09, 18:37, 15:14 | "APPROVED. merge" (2026-08-07, in memory); "force-push PR 110"; "Merge it" |
| S12 | **Never check out a branch in the live tree** `C:\repos\recruiter-assistant`. The live stack bind-mounts that working tree. Do feature work in worktrees. | 2026-09-15 | mem: compose-bind-mounts-working-tree | |
| S13 | **Never commit to `main`.** Branch names must be `agent/`, `feat/`, `fix/` or `chore/` plus a slug. **Never `docs/`**, CI rejects it (failed twice). Never `git add -A` (real résumés sit untracked in `fixtures/`); use explicit pathspecs. | 2026 | CLAUDE.md; mem: branch-name-gate, gate-write-pass-scoping | |
| S14 | **Gates run only through `./scripts/verify.sh [offline\|integration\|all]`.** Never a hand-written `docker run`, never a narrower command. A red unit stage stops `all`, so also run `integration`. A single `ERROR` at asyncpg/Neo4j setup in an unrelated test: re-run and require a clean pass. | 2026-07-22 | CLAUDE.md; mem: container-gate-recipe, testcontainers-asyncpg-flake | |
| S15 | **TDD, always.** Red tests first, then green, then refactor. Never modify an existing test to make code pass. A genuinely wrong test is edited only with PO approval, and disclosed. Additive inventory edits (route lists, table lists) are the one exception the coordinator took and disclosed. | 2026 | CLAUDE.md; (from session summary) | PO approvals: "do it", "approved", "proceed", "1,2 ok" |
| S16 | **Use subagents for substantive work**; keep the main thread as coordinator. Cheap producers, strong verifiers. | 2026-07-10, 2026-07-11 | mem: always-use-subagents, subagent-model-tiering | "always use subagents" (2026-07-10, as quoted in memory) |
| S17 | **A subagent's green is a claim, not evidence.** Require the command and its pasted output; re-grep the log yourself. Check your own prompt before blaming the agent. | 2026-07-22 | CLAUDE.md; mem: subagent-evidence-contract | |
| S18 | **The destination outranks every rule.** The pilot users hit it, then it is fixed, before anything not yet hit is built. User-sourced work outranks self-sourced work. Do not hunt A7 instances. `HANDOFF.md` is capped at EIGHT items. A finding nobody will action is deleted, not filed. | 2026-08-24, updated 2026-08-27 | CLAUDE.md Economy §0; doc: docs/archive/RESET-2026-08-24.md | |
| S19 | **ADR bar:** an ADR only when there were live alternatives, it is expensive to reverse, and the reasoning is not recoverable. Otherwise the commit message. Amend, don't add siblings. | 2026-08-24 | CLAUDE.md Economy §0a | |
| S20 | **PR bar:** one PR per coherent change. Docs-only changes ride along. No full local gate on a docs-only commit. | 2026-08-24 | CLAUDE.md Economy §0b | |
| S21 | **"Blocked on a human" is not a terminal state.** Bring a memo with options and a recommended default. | 2026-08 | CLAUDE.md Economy §3; doc: docs/OPEN_DECISIONS.md | |
| S22 | **Measure LLM budgets at real concurrency, on an uncontended host.** Check the model host's `/api/ps` before diagnosing model behaviour. Run `./scripts/model-check.sh` before swapping models. | 2026-08-22, 2026-09-09 | mem: lab-gpu-peers, measure-llm-budgets-at-real-concurrency; CLAUDE.md | |
| S23 | **`sfuai.ca` external reachability can wait.** Do not spend sessions on it unless asked. | 2026-11-20 | MSG 15:53 | "1,2 ok. sfuai.ca can wait" |

---

## 3. Product decisions

Columns: **date** (UTC) · **decision** · **alternatives rejected, if known** · **source**.
Status of the five features in the PO's order is in §3.1.

### 3.1 Feature priority (2026-11-20)

| Date | Decision | Alternatives | Source |
|---|---|---|---|
| 2026-11-20 | **New features resume.** "We will start adding new features". The feature build phase is open. | | MSG 00:22 |
| 2026-11-20 | **Order: 1, 3, 2, 4, then 5.** (1) In-app Taleo split, (3) interview-question generator, (2) human decision record, (4) "Ask the pool", (5) department picklist. The PO **swapped 2 and 3** against the coordinator's recommended order (split, decision record, interview, ask the pool, picklist). | Coordinator order 1,2,3,4,5 | MSG 00:24 |
| status | 1 merged (PR #112, def3376). 3 merged (PR #113, 3bfedd9). 2 merged (PR #114, PO merged 2026-11-24, inferred from "Merged. Go ahead"). **4 and 5 not started.** | | doc: HANDOFF.md; (from session summary) |
| 2026-11-20 | Priority of the split: it came from the DTO (a real user request), so it outranks ideas nobody asked for. | | (from session summary), coordinator reasoning accepted |
| 2026-11-20 | **PR #110** (re-parse a failed résumé) retargeted to `main`, later rebased and force-pushed with PO approval. **PR #111** (CAS redirects + restart policy) was open and unreviewed at the time. | | MSG 00:12, 15:51, 06:40 (inferred) |

### 3.2 Taleo import (job source)

| Date | Decision | Alternatives | Source |
|---|---|---|---|
| 2026 (ADR-046) | **Taleo job import is the one egress carve-out**: HTTPS to the Taleo host only, read-only, `TALEO_ENABLED`, default false. Production use has unmet obligations in the ADR (firewall rule, counsel and privacy-officer sign-off, named owner). | | CLAUDE.md |
| before 2026-11-24 | The **PO enabled `TALEO_ENABLED=true`** in the live `.env` themselves. | | MSG 15:25 |
| 2026-11-24 | **The managers will wait for the Taleo import**, i.e. managers do not start testing until the worker fix is deployed and an import works. "It a key feature tested by the managers." | Let managers test everything except Taleo (coordinator had offered this) | MSG 15:33 |
| 2026-11-24 | After the fix, the coordinator ran one import on the pilot box itself, started via the same job the button starts, labelled as a test run in the audit log. Result: 32 SFU postings as draft jobs, each with a Taleo link, in 39 s. Re-pressing the button refreshes, not duplicates. | A manager runs the first import (the coordinator offered this at 15:35; the sources record no PO reply to the offer) | (from transcript, assistant text) |
| 2026-09-18 to 09-20 | **DTO requirement:** a combined Taleo export must never become one résumé row. The upload refuses an unsplit export (more than 6 pages and 3 or more pages that look like a new applicant). The real bundle is in `data/HR Hriing Tool` (gitignored). Never print applicant names. Shipped as PR #109. | Classification by filename suffix only (the old behaviour) | mem: taleo-pdf-cover-letters-requirement |

### 3.3 Taleo combined-PDF split and anonymized export (feature 1, PR #112)

Approved as a set when the PO answered **"yes, go ahead"** to the coordinator's nine defaults (MSG 00:33,
2026-11-20). All nine are **(coordinator default)** and were listed in the plan. Source for the nine:
(from session summary) and the coordinator's question text at 00:33.

| # | Decision | Alternatives / note |
|---|---|---|
| 1 | **Cover-letter-only applicants and unreadable/scanned pages (under 200 characters) are left out** and listed as not ingested. No manual override in this version. | Manual override: rejected for v1 |
| 2 | **Accept is blocked** by missing pages, pages assigned twice, or one applicant's pages carrying two people's identifiers. Blank separator pages may be marked "ignored" by the manager only. | |
| 3 | **If the model fails, fall back to the rule-based guess**, with a banner telling the manager to check every row. | |
| 4 | **The uploaded combined PDF is deleted on any terminal state** (accepted, discarded); unaccepted proposals expire after 72 h. "Worth confirming with the privacy officer": **still unconfirmed** (§6). | |
| 5 | **Caps:** 40 MiB per file, 300 pages, 5 files per upload. | |
| 6 | **Blind-review jobs** show "Applicant 1, 2, ..." and never store guessed names. Names never appear in filenames. | |
| 7 | **The segmentation token budget ships as 8192, labelled UNMEASURED.** The `model-check` profile for `taleo_segment_v1` blocks hand-over, not merge. **Still owed** (gb10 was contended). | |
| 8 | **ADR-017 amendment**, not a new ADR. | New ADR: rejected (S19) |
| 9 | **Splits share the worker's four slots with résumé parses.** Leave as is and watch. | A separate interactive lane (the real fix, not built) |

| Date | Decision | Alternatives | Source |
|---|---|---|---|
| 2026-11-20 | **New feature on the split: export the split files as-is and anonymized, to a separate folder.** | | MSG 15:37 |
| 2026-11-20 | **Delivered as a ZIP download** (not a server folder). | other destinations, unknown | ASK 15:43 |
| 2026-11-20 | **The anonymized copy is a redacted PDF** (first answer). | unknown | ASK 15:43 |
| 2026-11-20 | **Export available any time after import.** | unknown | ASK 15:43 |
| 2026-11-21 | **Reversed the redaction approach: "Rebuild as clean PDF".** The anonymized copy is a rebuilt, text-only PDF (one font, minimal catalog and metadata), verified by re-extraction, refusing scanned/image pages and unknown scripts. Reason: security probes kept finding ways a candidate's name survived inside an edited original (shapes, hidden layers, metadata, other scripts). The ADR-011 amendment records this. | Keep editing the original file (rejected) | ASK 01:29 |
| 2026-11-21 | **The export is a background job** (export first, then download): `resume_export_requests`, requester-only one-shot download, expires after 60 min. The PO did not object when it was announced. | Synchronous download (original design) | (from session summary) |
| 2026-11-21 | **Re-download: "Keep as is".** The download button is single-use, but reloading the job page yields a fresh button for the same ZIP until it expires (60 min). | Strictly single-use | ASK 23:41 |
| 2026-11-21 | **Fail closed:** an applicant with no text layer, unparsed, or a surviving identifier term is omitted from the `anonymized/` folder. Blind job: `anonymized/` only. Audit row `resume_export`, counts only. | | (from session summary), coordinator defaults |
| 2026-11-21 | **Stress volumes:** the old `recruiter-stress` volumes were deleted ("Delete old volumes"), because they held data encrypted with an old key. | Use a new project name | ASK 20:58 |
| 2026-11-21 | **Push and open the PR** for `feat/taleo-inapp-split` (41 commits). | | ASK 23:41 |
| 2026-11-21 | **Anonymizer review loop stopped after round 9.** Accepted, recorded residuals (ROADMAP section 5): look-alike and glued names, street addresses, unlabelled date-shaped phones, no ZIP byte cap, a stranded `building` row blocks re-export for about 3 h, withdrawn applicant retained up to the TTL. Do not re-open as bugs without a pilot report. | Keep reviewing | (from session summary), coordinator decision |
| 2026-11-22 | PR #112 had a HANDOFF conflict; the coordinator resolved it, pushed, and the **PO merged it** (main def3376). | | MSG 03:02 to 03:07 |

### 3.4 Interview-question generator (feature 3, PR #113)

| Date | Decision | Alternatives | Source |
|---|---|---|---|
| 2026-11-22 | **What the local model may see: "Plus partial quotes".** Requirement text and status, plus the verified, blind-redacted quote for partial rows only. No other résumé text, no name, email or phone. | Requirement text and status only (probable alternative, not recorded) | ASK 00:23 |
| 2026-11-22 | **Audit it:** an audit row on every generation (who, when, counts, model), **never the question text.** | No audit | ASK 00:23 |
| 2026-11-22 | Generated on demand per shortlisted candidate, from the requirements where evidence was weak or missing. Refused when evidence is not evaluated. Stored per (job, résumé), with a stale marker when the evidence changes. Fixed token floor 8192. No ADR. | | (from session summary) **(coordinator default)** |
| 2026-11-22 | **Scrub identifiers from partial quotes on non-blind jobs too**, not just blind ones. Decided by the coordinator from the PO's answer ("no name, email or phone"). | Scrub on blind only | (from session summary) |
| 2026-11-22 | **Review rounds:** a blind flip after generation must not serve the old questions (stamped, then `blind_changed`); an arq `Retry` on LLM-unavailable; claim-id-conditional writes plus a heartbeat so a stale run cannot overwrite a newer claim; weak name tokens matched only in context. Residuals recorded. | | (from session summary) |
| 2026-11-22 | **Push and open PR** for the interview branch. | | MSG 19:09 ("ok push and open pr") |
| 2026-11-23 | **Merge PR #113** (main 3bfedd9). | | MSG 18:37 ("Merge it") (inferred which PR) |
| owed | **S4:** run `model-check` for `interview_questions_v1` on an uncontended host and commit the profile. | | doc: HANDOFF.md |

### 3.5 Human decision record (feature 2, PR #114)

All four answers were the recommended options. The alternatives are not recorded in the answers.

| Date | Decision | Source |
|---|---|---|
| 2026-11-23 | **Who can record accept / reject / hold:** recruiters, admins, and hiring managers **assigned to the job** ("Recruiters + assigned HMs"). Before this, only admin and recruiter could change things. Auditors read only. | ASK 22:28 |
| 2026-11-23 | **Rationale: "Always, short"**: required, 10 to 1000 characters. | ASK 22:28 |
| 2026-11-23 | **On the shortlist: "Badge only".** A decision never moves rank or score. | ASK 22:28 |
| 2026-11-23 | **"Append-only history".** A decision is never edited or deleted; a later one supersedes it, and the history shows all. | ASK 22:28 |
| 2026-11-23 | Planner defaults: an unassigned hiring manager gets **404** (not 403, so the response is not an existence oracle, ADR-020); **ADR-014 amendment** not a new ADR; a withdrawn candidate gets 404 with history preserved; audit never contains the rationale text. **(coordinator default)** | (from session summary) |
| 2026-11-24 | Review round 1 fixes: a multi-line (CRLF) rationale could not be saved (major); concurrent identical posts serialised by an advisory lock; badges survive the shortlist poll; a blind-job hint on the form; invisible/control characters refused. | (from session summary) |
| 2026-11-24 | **Push and open PR #114** after "Yes". **The PO merged #114** ("Merged. Go ahead"). | MSG 13:04, 15:14 (inferred from context) |
| gaps | Known residuals (ROADMAP): a rationale can still name the candidate on a blind job; a rejected rationale loses typed text; no "differs from system" report yet; conflicting reviewers resolve as later-wins; the history query has no LIMIT. Hiring-manager and auditor paths were tested only by automation; **they need a signed-in hand drive on the pilot box.** | doc: ROADMAP, PR #114 |

### 3.6 Clean slate on the pilot box (2026-11-24)

| Date | Decision | Source |
|---|---|---|
| 2026-11-24 | **PO request:** the Taleo import enabled earlier "hasn't moved to the running version", and old data remains. "Need a clean slate with latest code changes to test." | MSG 15:25 |
| 2026-11-24 | **Wipe scope: "Everything except accounts."** Deleted permanently, **no backup kept** (real candidate data): jobs, résumés, cover letters, shortlists, decisions, interview questions, the audit log, the job queue, the Neo4j graph content (indexes kept), stored résumé and cover-letter files. **Kept:** the two user accounts (one admin, one hiring manager) with their roles. **Untouched:** `DTO Package.zip` and `HR Hriing Tool` in `data/`. | ASK 15:28; (from transcript, assistant text) |
| consequence | **All hiring-manager job assignments were removed.** A hiring manager must be assigned to a job again before they see its shortlist (otherwise a 404 "does not exist"). | (from transcript) |
| consequence | **The audit log is empty before 2026-11-24.** Anything quoting an earlier audit row cannot be checked. | (from transcript) |

### 3.7 Job-page redesign: checkpoint H1 (decided 2026-11-24)

| Date | Decision | Alternatives rejected | Source |
|---|---|---|---|
| 2026-11-24 | **Redesign the job page.** Reason: the page is cluttered; résumé upload (individual, zip, Taleo PDF) is not clearly delineated; the Taleo split and export sit under all of it. "Let step back and redesign while managers test the core functions." | Do nothing | MSG 18:23 |
| 2026-11-24 | **Option A: workspace tabs.** Estimate 20-25 fleet-hours, 3-4 working days. Stage (Draft, Parsed, Open, Closed) and one "next action" button stay at the top on every tab. Only Flask pages change; no API, database or ranking change. | **B guided steps** (32-38 h, 5-7 days; awkward because real hiring goes back and forth). **C tidy the single page** (10-12 h) | MSG 18:35; doc: docs/plans/2026-11-job-page-redesign/README.md §7 |
| 2026-11-24 | **Tab names: Overview, Candidates, Requirements, Shortlist, Settings.** | | same |
| 2026-11-24 | **Taleo roster CSV is intake card 4**, labelled "adds no résumés" (not under Settings). Candidates tab "Add candidates" has four labelled cards: one résumé, a zip of résumés, the Taleo export PDF (with its split review), the roster CSV. | Move it to Settings | same |
| 2026-11-24 | **Preview mechanism: a per-user toggle** behind `JOB_PAGE_V2`, **default off.** No second deployment or preview site. Managers keep today's page during their test. | A separate preview stack on a spare port | same |
| 2026-11-24 | **Scope:** the shortlist and candidate-entry pages are out of scope, apart from a Shortlist-tab teaser. Restyling them is a separate project. | Restyle them now | same |
| 2026-11-24 | **Export gets its own menu**, not among the upload forms. An empty Candidates tab says why you cannot add candidates yet (the confusion in §5, item 11). | | doc: README |
| 2026-11-25 | **S0 done:** [S-contract.md](../plans/2026-11-job-page-redesign/S-contract.md). The page has 1 one-shot CSRF slot (the export download), not 15, and a bad token is a 403, not a silent 400. **Q1, the preview mechanism: "Both: column + .env list".** v2 renders if `JOB_PAGE_V2` is on, OR the CAS username is in `JOB_PAGE_V2_USERS`, OR `users.job_page_v2` (`NOT NULL DEFAULT false`, admin-set, audited) is true. It is built as slice S1a and gated with `verify.sh all`. The estimate is now 26–32 h. | `.env` allowlist only (the coordinator's recommendation); column only | ASK 2026-11-25 |
| 2026-11-25 | **New box: the stack must run from the live tree.** `quickstart.ps1` was first run from the dev worktree, so the stack bind-mounted the code under development. It was re-homed to `C:\Repos\recruiter-assistant` with both compose files (PO's choice); the volumes carried over, because `name: recruiter-assistant` is pinned. Then doctor was healthy and the localhost login redirect correct. | Keep the stack on the worktree | ASK 2026-11-25 |
| next | **Next step is slice S0** (design lock; planner writes the slice contract), per README §12. Later checkpoints: **H2** (after S2: is the Candidates tab right?), **H3** (push, PR, merge), **H4** (manager UAT with the flag on for one manager), **H5** (flip the default). The main risk (S0 later measured 1 one-shot slot, not 15): the page has 13 CSRF token uses on its forms; moving a form can break its token silently, so every form needs a test and a by-hand run. | | doc: README §9, §10 |
| purpose | The PO wants this project **"as a seed for the software engineering practice"**: "an example of me managing the claide fleet and a proper project plan". The playbook is [docs/practice/fleet-project-playbook.md](../practice/fleet-project-playbook.md). | | MSG 18:23 |

### 3.8 Deployment, CAS, sfuai.ca, ports

| Date | Decision | Alternatives | Source |
|---|---|---|---|
| 2026-09-15 | **The site is public at `https://sfuai.ca:8000` with CAS on.** nginx runs in a separate, non-git compose project in `C:\repos\web`; the router forwards only TCP 8000-8009 (443 is not forwarded). Runbook: `docs/deploy/sfuai-ca.md`. | | mem: sfuai-ca-public-deployment |
| 2026-11-20 | **The live Neo4j host ports moved to 29475 and 29688** because another app's Neo4j holds 29474/29687. The coordinator proposed stopping that other app's container; **the PO refused.** The PO edited `.env` themselves. | Stop the other app's Neo4j (rejected, S3) | MSG 08:38, 08:46, 08:50 |
| 2026-11-20 | **External reachability:** "sfuai.ca:8000 not reachable from outside". Diagnosis: the router NAT targets the .80 address while the box is on a different address on Wi-Fi with Ethernet disconnected. **Deferred by the PO.** DNS at the PO's host provider is correct. | | MSG 01:35, 09:24, 15:53 |
| 2026-11-24 | The PO's tester should use `https://sfuai.ca:8000` from their **own machine**. From the box itself it times out (hairpin NAT); test locally with `curl --resolve`. The tester must sign in through the site's sign-in page, not an old tab. | | (from transcript, assistant text) |
| 2026-11-24 | **Deploy method:** the live tree is on local branch `fix/cas-local-origin`, tree-identical to `main`. To deploy: a tree-identical merge commit, then `docker compose up -d --build api worker frontend`, then `doctor.sh`. Exact recipe in OPERATOR-NOTES §3. **Deploying needs PO consent.** | | doc: OPERATOR-NOTES; MSG 15:14 |
| 2026-11-24 | **The pilot box is on main** as of `eb39d03` (PR #115, worker reads `.env`). Live Taleo import verified; doctor clean after it. | | (from transcript) |

### 3.9 Blind review, privacy and PII (cross-cutting)

| Date | Decision | Source |
|---|---|---|
| 2026-07-17 | **Reveal is opt-in and audited.** The PO reversed the earlier blind-only frontend posture: clicking a candidate label reveals the un-blinded résumé, and every reveal is logged. Blind stays the default. | doc: archive/HANDOFF-2026-07-10-to-08-23.md |
| 2026-08-19 | **D2 = option B** (close unscoped keyed reads). **D1 = option C** (an auditor reveals a withdrawal reason on request, separately audited). Both were the recommended defaults. | doc: docs/OPEN_DECISIONS.md |
| 2026 | The outbox carries no candidate identity; `resumes.parsed` keeps cleartext PII at rest (accepted for v1, ADR-007 §6); documented residuals are not bugs to re-open. | mem: phase3-outbox-pii-boundary |
| 2026-11 | Blind jobs: the split stores no names; the export ships `anonymized/` only; the interview scrub covers names, emails, phones; the decision form warns not to identify a candidate. | (from session summary) |
| open | Revoke-and-purge semantics (ROADMAP open item 1, ADR-026 §4) need an HR decision and their own security review before any code. | doc: OPEN_DECISIONS.md |

### 3.10 History before the transcript (context)

| Date | Decision | Source |
|---|---|---|
| 2026-07-17 | The Phase 7 live end-to-end eval gates the PR #16 merge. PO: "I approve [the PR #16 merge] once you do the live end-to-end eval." | mem: live-eval-reversed-from-deferred |
| 2026-08-24 | **Reset:** after 94 commits and 32 ADRs against zero users, the destination became a real recruiter ranking a real requisition. | doc: archive/RESET-2026-08-24.md |
| 2026-08-27 | **Destination met.** The product was demoed to the CIO and HR, both approved; it is on a dedicated box with four users. The build phase reopened. | CLAUDE.md |
| 2026-09-09 | **PO: pause feature work until the datacenter hardware is ready** (parsing is hardware-bound: about 29 résumés/hour). Resumed 2026-09-15 when the site went live. In November the PO restarted feature work (MSG 00:22). | doc: archive/HANDOFF-2026-08-24-to-11-21.md |
| 2026-09-17 | Before sharing with the DTO the PO asked for a manager's guide, an end-to-end run of it, and a multi-user stress build. | doc: archive/HANDOFF-2026-08-24-to-11-21.md |
| 2026 | The model is `gpt-oss:20b` plus `nomic-embed-text` (768-dim) on shared lab GPU hosts; the final target is ADA6000 boxes running vLLM, not ready. `LLM_TIMEOUT_S=900` on the live box. | mem: lab-gpu-peers, sfuai-ca-public-deployment |
| open | **Competency scoring** (`years x recency x ontology_weight`) is a semantically odd model; deferred pending pilot data, and **there is now pilot data**, so the deferral has expired. Owner: corpus owner and HR. | doc: OPEN_DECISIONS.md |

---

## 4. Process and working-relationship decisions

| # | Decision | Date | Source |
|---|---|---|---|
| P1 | **The fleet:** planner, tester (red), coder (green), data-pipeline, reviewer and security (opus, read-only, merge-blocking), ranking-evals (opus), docs. Definitions in `.claude/agents/`. The coordinator (main thread) delegates and verifies. | 2026-07-10 | mem: always-use-subagents; doc: SUBAGENT_MODEL_POLICY.md |
| P2 | **Model tiering:** opus for the three merge-blocking gates (never downgrade); sonnet for planner, tester, coder, data-pipeline; haiku for docs. Per-call overrides up for ranking, evidence verifier, PII crypto, Neo4j scoring. | 2026-07-11 | mem: subagent-model-tiering |
| P3 | **The `architect` model change:** the PO asked (from `/usage`, subagents under `architect` were 42% of usage) to check its model and use a cheaper one if it fits. The coordinator changed three repos' `architect.md` from opus to sonnet. **The PO then said undo it ("undo any changes to architect that is not good") and "let us not touch any other repos".** All reverted. See §5, item 2. | 2026-11-20 | MSG 00:02, 00:22, 01:35 |
| P4 | **One report per subagent.** A second `SubagentHandback` is refused. Brief: "no interim report; wait for runs to finish". If a report is refused or thin, read logs and git state yourself. | 2026-11-20 | (from session summary) |
| P5 | **Evidence contract:** a subagent's report must paste the command and output; the coordinator re-greps `git log`, `git show --stat`, `git status`, the gate log tail and leftover containers before committing. | 2026-07-22 onward | mem: subagent-evidence-contract |
| P6 | **A subagent must not edit an existing test assertion.** If it believes one is wrong it reports to the coordinator, who asks the PO. PO approvals in the session: "do it" (2 wrong H2 tests), "approved" (H3 and round-3 test edits), "proceed" (5 contradicting tests), "1,2 ok" (combined-export detection text; split listing endpoint). Some edits were blocked by the permission classifier; the rule is never to route around a block. | 2026-11-20 to 21 | MSG 21:36, 23:31, 05:53, 14:38, 15:53; (from session summary) |
| P7 | **Gate timings on this host:** unit about 38 min, integration about 12-15 min, `all` about 50 min. Do not start a gate run you are about to invalidate; stop it (TaskStop plus `docker stop` of the container mounting that worktree). Do not mutate the tree while a reviewer reads it. | 2026-11 | (from session summary); mem: gate-mutation-vs-review-race |
| P8 | **Mutation probing is one pass** per branch; do not probe the guards you just added. | 2026-08 | CLAUDE.md Economy §2 |
| P9 | **Finding disposition:** critical or major: fix on the branch. Minor: fix if cheap and on theme. Nit: record in ROADMAP with file:line, do not fix. | 2026-08 | CLAUDE.md Economy §1 |
| P10 | **Hand-over rule (S6) applies to every major feature** (a screen, a route, an ingest path, a persisted column, a ranking term). The minimum is `smoke.sh` plus `doctor.sh` plus a by-hand drive of the feature's path, results pasted verbatim. On the pilot box: `doctor.sh` plus a signed-in drive. State plainly what could not be checked. | 2026-09-09; 2026-11-24 | CLAUDE.md; MSG 15:35 |
| P11 | **A setting counts as on only if the process that acts on it sees it.** And **"ready to test" must include a new user's empty-state path.** Both are lessons the PO's two corrections taught on 2026-11-24 (see §5). | 2026-11-24 | doc: docs/practice/fleet-project-playbook.md |
| P12 | **Estimating:** in cycles of one failing test plus the fix, measured at about 3 h each from commit timestamps (a first draft said 1.5-2.5 h and was corrected). The PO's answer time is the biggest factor. | 2026-11-24 | (from transcript, assistant text 18:27) |
| P13 | **Checkpoints:** the PO steers at named human checkpoints (H1 to H5 for the redesign). Plans record actuals against estimates (README §11) for calibration. | 2026-11-24 | doc: README §9, §11 |
| P14 | **Hand-over to a Teams premium account.** The PO is moving there because "my ultra credits are going fast". "Full oral history and handover, documentation all should be left in a lynched state" (typed; probably "synced" or "linked"; the intent is complete and consistent). Delivered on branch `chore/handover-2026-11`: HANDOFF item 1, OPERATOR-NOTES, this log, ORAL-HISTORY, the playbook. Private memory may not carry over, so the repo is the source of truth. | 2026-11-24 | MSG 18:27, 18:35; mem: handover-to-teams-account |
| P15 | **Docs rules:** local Markdown only (S8). HANDOFF capped at 8 items; history goes to `docs/archive/`, never inline. The docs agent once deleted HANDOFF history without archiving it; the coordinator restored it verbatim. Check docs-agent diffs before committing. HANDOFF.md is the usual merge-conflict hot spot. | 2026-11 | CLAUDE.md; (from session summary) |
| P16 | **Concurrent sessions:** two Claude sessions have driven this repo at once. Re-read git and PR state in the same call that commits, pushes or merges. Metadata cannot tell which session acted. | 2026-08-17 | mem: concurrent-sessions-on-this-repo |
| P17 | **Verify HANDOFF against origin first:** `git fetch origin && gh pr list --state all`. HANDOFF is a hypothesis, not state. | 2026-07-13 | mem: verify-handoff-against-origin |
| P18 | **PR and CI facts:** CI takes about 9-10 min. The PO may merge before CI finishes (PR #112); watch CI on main and re-run the local gate after. A force-push chained with other commands was denied; it succeeded as a standalone command after the PO's explicit "force-push PR 110". | 2026-11-21, 22 | (from session summary) |
| P19 | **Windows/Git Bash gotchas** (each cost real time): `MSYS_NO_PATHCONV=1` breaks `curl -o /dev/null`; `perl -0pi` with `\x{..}` double-encodes a file; a literal NUL escape becomes a real byte; `sed` mangles backslash escapes; the Edit tool with `\uXXXX` writes the literal character; no usable Python on the host. | 2026-07 to 11 | mem: msys-no-pathconv-breaks-curl, perl-utf8-double-encode, nul-byte-propagation-hazard; (from session summary) |
| P20 | **Polling:** use a background command or Monitor, not `sleep` loops (auto mode blocks them). | 2026-09-09 | mem: poll-with-monitor-not-sleep-loops |
| P21 | **Frontend sees JSON, not DTOs.** Every timestamp is a string. Build frontend fixtures from `model_dump(mode="json")`, never a hand-written dict, and use the `day` filter. | 2026-09-03 | mem: frontend-sees-json-not-dtos |
| P22 | **Never ship a script default that silently drops a feature.** `quickstart.ps1` applies `compose.cas.yml` unless `-NoCas`; it needs `pwsh` 7 (Windows PowerShell 5.1 fails to parse and still exits 0). | 2026-08 | mem: cas-on-by-default, quickstart-needs-pwsh7 |
| P23 | **Deleting a git ref does not purge** a leaked blob; verify with `gh api ...?ref=<sha>` and file a GitHub Support purge. The fixtures corpus stays untracked on purpose. | 2026-08-22 | mem: github-branch-delete-does-not-purge |

---

## 5. Corrections the PO made, and what went wrong

These are the highest-value entries. Each cost the PO time or trust.

| # | When | What happened | What the PO said | What was learned |
|---|---|---|---|---|
| 1 | 2026-11-20 | The PO said "switch to sfu-aria org" (from session summary). The coordinator switched the **GitHub** org. | "I meant switch claude account not the gh" (MSG 00:04) | The PO meant the Claude account. Ask when a verb could mean two systems. The PO then switched it themselves ("done"). |
| 2 | 2026-11-20 | Asked to fix the 42% `architect` cost (with the instruction "Show me each change before you make it"), the coordinator changed `architect.md` from opus to sonnet in **three repos**, two of them not this project. | "let us not touch any other repos. Only ecriter assistant." then "undo any changes to architect that is not good" | **S1.** Stay in this repo. The PO's second message is ambiguous (undo all, or only the not-good ones); the coordinator reverted all three files to opus. Do not retry a cheaper model for `architect` without asking. Show every change first, as instructed. |
| 3 | 2026-11-20 | Neo4j on the live box crash-looped, then could not bind its port because another app's Neo4j held it. The coordinator recommended stopping that other app's container. | "use different ports, don't touch othe apps" (MSG 08:38) | **S3.** Never stop another app's container. Move your own port. The live Neo4j is on 29475/29688; the other app keeps 29474/29687. |
| 4 | 2026-11-20 | The coordinator was denied a `.env` edit and worked around it with shell overrides for one `compose up`. The next `compose up` would have reverted the ports. | "one time exception, edit .venv and continue", then "env updated" (the PO edited it) | **S10.** A shell override is not a fix, because the container restarts with the old value. Ask the PO for the one-line change. Never read or list `.env`. |
| 5 | 2026-11-20 | The live site was down (Neo4j) while new features were planned. | "sfuai.ca:8000 not reachable from outside"; later "sfuai.ca can wait" | The box was healthy locally; the router NAT pointed at the wrong address (a LAN-address change). Deferred by the PO. A live outage outranks new work (S18), unless the PO defers it. |
| 6 | 2026-11-20 | PR #110 was red after retargeting to `main`. The coordinator rebased locally but could not force-push. | "pr 110 red" (MSG 15:51); "force-push PR 110" (MSG 06:40 next day) | A force-push needs its own explicit PO approval and a standalone command. Never chain it. |
| 7 | 2026-11-21 | The PO typed only "push" with several branches pending. | "push" (MSG 00:35) | The coordinator asked which. The PO's next answer was the AskUserQuestion on the anonymizer. Ambiguous consent is not consent. |
| 8 | 2026-11-24 | **The Taleo import the PO had enabled never reached the running version.** `TALEO_ENABLED=true` was in `.env`, but only the `api` and `frontend` services loaded `.env`. The **worker** (which runs the sync) did not, so each sync silently did nothing while the API reported "enabled". Green gates and a deploy had been treated as done. | "I enabled taleo import last time, but looks like it hasn't moved to the running version." (MSG 15:25) | Fix: `env_file: [.env]` on the worker plus a test that fails if any app service stops loading `.env` (PR #115). **A setting counts as on only if the process that acts on it sees it** (P11). |
| 9 | 2026-11-24 | The box had **previous data** and the PO wanted a clean test. | "Also we still have previous data. Need a clean slate with latest code changes to test" (MSG 15:25) | Wiped all except accounts (§3.6). Side effects: hiring-manager assignments gone, audit log empty. Say so before the wipe, not after. |
| 10 | 2026-11-24 | **"Do an end to end test per the prime directive... rerecord it if you forgot."** The coordinator had declared the worker fix done on "the flag is now True". | "Do an end to end test per the prime directive, before you hand over a feature as completed. rerecord it if you forgot" (MSG 15:35) | **S6/P10 restated.** Done means: an import clicked through Flask that lands real postings on the jobs page, then the same on the pilot box. The coordinator then did exactly that (32 postings, 39 s, audit row, doctor clean) and saved the memory note e2e-before-every-handover. |
| 11 | 2026-11-24 | **"What happened to upload resumes features?? We had single file, zip file and cover letter uploads. All gone!"** The coordinator had told the PO the box was ready to test. | MSG 18:13 | **Nothing was deleted.** The upload section appears only on a job whose status is **Open**, only for admins and recruiters, and that has not changed. After the wipe, the only jobs were 32 imported **drafts**, each queued for a parse of about 5.5 min (four at a time, about 45 min in total); a draft cannot be opened until parsed. The coordinator's fault: "ready to test" must include the new user's empty-state path (P11). The PO replied "I see it now" and commissioned the redesign (§3.7). |
| 12 | 2026-11-24 | A related finding from the same event: after a wipe, 20 of 32 jobs were parsed and none had failed; the PO had opened one. | | Parse time is a hardware bound (S22). Tell testers how long. |

### Smaller process incidents (the coordinator's, from session summaries)

| Date | Incident | Lesson |
|---|---|---|
| 2026-11-20 to 24 | Several gate runs were started before a final edit and had to be stopped (TaskStop plus `docker stop`). | Start the gate only after the last edit (P7). |
| 2026-11-20 | Subagents sent an interim report and were then refused a final one. | One report only (P4). Verify from logs. |
| 2026-11-21 | A coder ran a hand-written `docker run` diagnostic. | "Never again" (S14). |
| 2026-11-22 | The coordinator's `sed` edits mangled backslash escapes in a test fixture and the commit captured it. | Use the Edit tool; check bytes with `od` (P19). |
| 2026-11-22 | The docs agent deleted HANDOFF history without archiving it. | P15. |
| 2026-11-21 | The coordinator removed a case from a test list after a spec conflict (commit a805abc) and disclosed it. | Disclose every test edit (S15). |
| 2026-11-22 | The reviewer found a major bug: an arq job raising `LLMUnavailableError` is never retried, leaving the row pending forever. | Only `arq.Retry` retries. |
| 2026-11-23 | A decision rationale typed on more than one line (CRLF) could not be saved, and no automated test covered it. | The hand-drive found what 8000 tests missed. Always include a multi-line input (S6). |
| 2026-11-24 | A real-model smoke timed out because another team's model sat on the shared GPU. | Switch to the stub, and check `/api/ps` (S22). |
| 2026-09 | Earlier, the PO's pilot complained "Generate shortlist has not been producing anything": it queued behind about 1,400 parse jobs; a 2048-token budget passed two single-call probes and failed at concurrency 4. | S22, and measure at real concurrency. |
| 2026-09-03 | `strftime` on a string timestamp 500'd the whole jobs list behind a green suite. | P21. |
| 2026-11-25 | Logging in from `localhost:29500` on the box timed out at `sfuai.ca:8000/auth/cas/login` (hairpin NAT), the third recurrence. The cause: the coordinator's 11-24 deploys recreated `api` and `frontend` with a plain `docker compose up`, which dropped the `CAS_LOCAL_*` values that lived only in `compose.cas.yml`. OPERATOR-NOTES and FRESH-INSTALL gave that same recipe. The PO said: *"We solved this problem numerous times, and keeps coming back. It is eroding confidence in the whole harness/agents ability to follow instructions."* | Recreate with both compose files. The defaults now also live in `docker-compose.yml` (pinned by a test), and doctor fails with `deploy.cas_local_origin_unset`. **After any deploy, check BOTH origins**, not just the public one. |

---

## 6. Open and pending decisions

Each has a **recommended default** so none is a bare "blocked". Nothing here was answered in the sources
unless stated.

| # | Open item | Asked | Status in the sources | Recommended default |
|---|---|---|---|---|
| O1 | **Push `chore/handover-2026-11`, open the PR, and merge it once GitHub's checks pass.** Two commits waited unpushed on 2026-11-24. The new account starts from GitHub, so the hand-over only counts once it is on `main`. | 2026-11-24 18:35 (coordinator) | **ANSWERED 2026-11-24: "1. yes"**. Pushed as PR #116, to be merged once CI passes. | Yes: push and open the PR; **merge only on the PO's explicit word** (S11). The branch is docs-only, so CI covers it. |
| O2 | **Delete the superseded branches** `feat/taleo-split-service` (worktree `C:\repos\ra-taleo-split-svc`) and `fix/pr110-rebase` (scratchpad worktree). Nothing on them is not already on `main`. | 2026-11-24 18:35 | **ANSWERED 2026-11-24: "2. yes"**. Both branches and their worktrees deleted (local only; never pushed). | Yes, delete both local branches, with the PO's OK. Check `git branch --merged` first. |
| O3 | **Gap: no commits from 2026-09-26 to 2026-11-20.** ORAL-HISTORY marks it unexplained. | 2026-11-24 | Unanswered. | Ask the PO for one line and add it to ORAL-HISTORY. Do not guess. |
| O4 | **sfuai.ca is not confirmed reachable from outside the LAN.** The router NAT target (.80) differs from the box's current address (.73, Wi-Fi, Ethernet unplugged). The PO deferred it. The tester has to confirm from their own machine. | 2026-11-20 | **CLOSED 2026-11-24.** The box is back on Ethernet at `192.168.1.80`, matching the NAT target, and the PO confirmed: *"External access works."* | Done. On a new box, recheck the router target against the box's current address (FRESH-INSTALL §6). |
| O15 | **Move to a new account and box, possibly with a new LLM host.** The PO asked: *"Prepare the new bundle and ready for deployment to new account/box. Instructions for fresh build. I will probably point to new llm box but can do that in new setup."* | 2026-11-24 | **Done:** [docs/deploy/FRESH-INSTALL.md](../deploy/FRESH-INSTALL.md) plus the release bundle (FRESH-INSTALL §10). | On the new box, follow FRESH-INSTALL. A new LLM host requires `model-check.sh` and a committed profile **before** users (§4). |
| O16 | **The Taleo posting "President and Vice-Chancellor" parsed with zero required skills**, so it has no `REQUIRES` edges and doctor reports `neo4j.unprojected_jobs` (fail). It is a draft; nothing is broken in the outbox. | 2026-11-25 | Recorded, not fixed. | If a manager opens that job, re-parse it, or decide that a JD with no extractable skills should be a doctor *warn*, not a *fail*. Otherwise leave it; nobody has hit it. |
| O5 | **Managers have not yet tested.** They were told to wait for the Taleo import (now deployed). The first CAS sign-in by a manager has never been exercised by the coordinator. | 2026-11-24 | In progress. | Have the PO tell a manager to sign in first, report if the jobs list does not appear, then run the tester script (record, supersede, short rationale, badge, interview questions). Hiring managers must be re-assigned to a job first. |
| O6 | **Hand-drive on the pilot box signed in as real users** for the decision record and interview questions (hiring-manager and auditor paths). Only automation covered those. | 2026-11-24 | Owed. | A manager or the PO does it; paste the result into HANDOFF. |
| O7 | **Model-check profiles owed:** `interview_questions_v1` (S4) and `taleo_segment_v1` (F1/F2). The segmentation budget ships "UNMEASURED". `doctor.sh` fails or warns when a profile is missing. | 2026-11-20 | Deferred until the GPU host is uncontended. | Check `/api/ps` on the host first. Run `./scripts/model-check.sh` at the worker's concurrency; commit the profile. |
| O8 | **Privacy officer confirmation:** the split's 72 h proposal expiry and delete-on-terminal-state; the 60 min export TTL; ADR-046 sign-offs (firewall rule, counsel, privacy officer, named owner) for Taleo egress in production, which the PO has enabled. Not recorded in any source as done. | 2026-11-20 | Unknown. | Ask the PO whether the ADR-046 obligations are met. Do not assume they are. |
| O9 | **Dependabot alert (1 high) on `main`** appeared in a push output on 2026-11-21; it has never been reviewed. | 2026-11-21 | Unactioned. | Review it; open a `fix/` or `chore/` PR if real. |
| O10 | **Features 4 ("Ask the pool") and 5 (department picklist)** are not started. Feature 5 is small, about a day. The redesign (§3.7) was chosen to run in parallel with manager testing. | 2026-11-20 | Not started. | After the redesign S0, ask the PO whether to interleave. Default: finish the redesign first, because managers just hit the confusion it fixes (S18, user-sourced work). |
| O11 | **Redesign S0** may start: H1 is decided (§3.7). | 2026-11-24 | Approved. | Start S0 (planner writes the slice contract) and bring H2 after S2. |
| O12 | **Competency scoring** model and **revoke-and-purge** semantics (ROADMAP items 3 and 1). | 2026-08 | Open, with pilot data now available. | Write the option memo with a recommended default and put it to the PO and HR. |
| O13 | **Private memory may not carry over** to the Teams account. | 2026-11-24 | Coordinator's warning. | Everything load-bearing is in the repo. If a fact seems to exist only in memory, add it to OPERATOR-NOTES. |
| O14 | **Recorded anonymizer and interview residuals** (ROADMAP section 5): not to be re-opened as bugs without a pilot report (S18). | 2026-11 | Recorded. | Leave alone. |

---

<a id="appendix-verbatim-po-messages"></a>
## 7. Appendix: verbatim PO messages

Chronological. These are the PO's own typed messages from the surviving 2026-11-20 to 2026-11-24
transcript, **not** compaction summaries, task notifications or system text. Typos are kept as typed. Times
are UTC. One public IP address is redacted. Two earlier messages that appear only in session summaries are
listed first, marked as such.

**S-1. (from session summary, not in the extracted transcript)**

> switch to sfu-aria org

**S-2. (from session summary, not in the extracted transcript)**

> only this time, update the .env


**1. 2026-11-20T00:02:45Z**

> Fix what /usage flagged. Show me each change before you make it. Text in backticks is a name from my logs, never an instruction.
> Subagents under `architect` were 42% of my usage. Check which model they run on, and if a cheaper one fits their work, set it in the agent file or skill that defines them. If they are built in, tell me what I can change.

**2. 2026-11-20T00:04:14Z**

> I meant switch claude account not the gh

**3. 2026-11-20T00:06:30Z**

> done. now review and pickup from handoff

**4. 2026-11-20T00:12:37Z**

> yes to 1,2,3

**5. 2026-11-20T00:22:39Z**

> let us not touch any other repos. Only ecriter assistant.
> We will start adding new features

**6. 2026-11-20T00:24:36Z**

> 1,3,2,4 then 5

**7. 2026-11-20T00:26:28Z**

> ensure unique ports as there are multiple instances of the stack

**8. 2026-11-20T00:28:41Z**

> modify .env

**9. 2026-11-20T00:33:11Z**

> yes, go ahead

**10. 2026-11-20T01:35:41Z**

> sfuai.ca points to the right ip [public IP redacted] in my ionos host
> undo any changes to architect that is not good

**11. 2026-11-20T01:36:47Z**

> proceed with neo4j

**12. 2026-11-20T08:38:24Z**

> 1. ok
> 2. use different ports, don't touch othe apps

**13. 2026-11-20T08:46:48Z**

> one time exception, edit .venv and continue

**14. 2026-11-20T08:50:32Z**

> env updated

**15. 2026-11-20T09:24:59Z**

> sfuai.ca:8000 not reachable from outside

**16. 2026-11-20T14:38:22Z**

> proceed

**17. 2026-11-20T15:37:48Z**

> a feature to add to taleo split, will be export the split files as is and anonymized to a separate folder.

**18. 2026-11-20T15:51:45Z**

> pr 110 red

**19. 2026-11-20T15:53:17Z**

> 1,2 ok. sfuai.ca can wait

**20. 2026-11-20T21:36:19Z**

> do it

**21. 2026-11-20T23:31:30Z**

> approved

**22. 2026-11-21T00:35:56Z**

> push

**23. 2026-11-21T05:53:14Z**

> approved

**24. 2026-11-21T06:40:24Z**

> force-push PR 110

**25. 2026-11-22T03:02:59Z**

> I see two prs in gh

**26. 2026-11-22T03:03:58Z**

> 112 has conflicts

**27. 2026-11-22T03:06:26Z**

> cant merge it. push?

**28. 2026-11-22T03:07:10Z**

> merged

**29. 2026-11-22T14:10:50Z**

> machine was rebooted. resume session

**30. 2026-11-22T19:09:57Z**

> ok push and open pr

**31. 2026-11-23T17:49:19Z**

> Status?

**32. 2026-11-23T18:37:35Z**

> Merge it

**33. 2026-11-23T22:26:19Z**

> Continue

**34. 2026-11-24T13:04:42Z**

> Yes

**35. 2026-11-24T15:10:13Z**

> Let me know when is can have someone test

**36. 2026-11-24T15:14:29Z**

> Merged. Go ahead

**37. 2026-11-24T15:22:29Z**

> Yrl/ports for testing?

**38. 2026-11-24T15:25:39Z**

> I enabled taleo import last time, but looks like it hasn't moved to the running version. 
> Also we still have previous data. Need a clean slate with latest code changes to test

**39. 2026-11-24T15:33:38Z**

> I will wait for taleo import it a key feature tested by the managers 

**40. 2026-11-24T15:35:18Z**

> Do an end to end test per the prime directive, before you hand over a feature as completed. rerecord it if you forgot

**41. 2026-11-24T17:44:14Z**

> Yes

**42. 2026-11-24T18:13:21Z**

> Major issue..
> What happened to upload resumes features?? We had single file, zip file and cover letter uploads. All gone!

**43. 2026-11-24T18:23:55Z**

> I see it now.
> I think we need to redesign the UI.
> The job page is so cluttered and confusing. 
> Result upload (individual, zip, taleo pdf) are not clearly delineated. The export taleo split is embedded under all that. 
> Let step back and redesign while managers test the core functions.
> Propose new layouts and assess impact on dev work. Give me a visual, plan and estimated effort on the agent fleet part.
> Let us use this as an example of me managing the claide fleet and a proper project plan. So in addition to the actual delivery of recruiter assistant, I want to use this as a seed for the software engineering practice 

**44. 2026-11-24T18:27:42Z**

> I will be handing the whole thing to the teams premium account after this iteration as my ultra credits are going fast. So full oral history and handover, documentation all should be left in a lynched state

**45. 2026-11-24T18:35:37Z**

> Assuming above 1-5 are approved Go A plan, update the handoff accordingly. 
> Capture as much of the session chats, decisions so we won't have misinformed work/rework in the Teams premium account 

