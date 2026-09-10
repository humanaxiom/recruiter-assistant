# Pilot feedback

**The channel from a pilot user's confusion back into this repo.** ROADMAP open
item 2 asked for this file and called it the cheapest version of the thing —
"a file, not a feature". Every defect the pilot has produced so far arrived by
someone mentioning it in conversation and was then lost unless a session
happened to write it into `HANDOFF.md`.

**How to use it.** Newest first. One entry per report. Record what the person
actually said, **verbatim and in quotes**, before recording what it turned out
to be — the two are different, and the gap between them is usually the finding.
An entry is closed by naming the commit or PR that closed it, not by deleting
it. An entry nobody will action gets deleted rather than left to be re-read
(`CLAUDE.md` §Economy 0).

Reports here **outrank anything self-sourced**. That is the whole of
`CLAUDE.md` §Economy 0 and it is the only rule that has ever stopped this
repository gold-plating itself.

> ⚠️ **NEVER put a real candidate's name, email, phone number, or any other
> identifying detail in this file.** It is tracked, and both remotes are
> PUBLIC. Describe the defect, not the person: *"one applicant was split into
> two records"*, never the applicant. A quoted user report gets the same
> treatment — redact identifiers out of the quote and say you did.
>
> This warning exists because it was violated on 2026-09-09, in this file, by
> the session that created it: a real applicant's surname, a phone number, and
> an email local-part that was itself a name were committed while documenting
> PII hygiene. Caught by the security gate before push, and the commit was
> rewritten. The prior incident it echoes — 99 MiB of real résumés pushed to a
> public remote by a `git add -A` — is still open in `HANDOFF.md` §6, because
> deleting a branch does **not** make GitHub stop serving the blobs. Only a
> Support purge does.
>
> The failure mode is specific and worth naming: writing *about* candidate
> data makes quoting a real example feel like precision. It is not precision;
> the defect is fully describable without it, and every detail you add is
> permanent.

---

## 2026-09-09 — DTO — a candidate bundle, and "a simple workflow"

> "Attached the bundle given by DTO. JD (Prompt.txt), All_Candidates_…csv and
> PDF resumes (printout of multiples) from Taleo. About This Role.txt for
> additional details and Prompt.txt
>
> They are interested in simple workflow: Upload, Parse and rank
>
> CSV contains 3 important columns: Work Authorization (false or student
> permit=bottom rank), APSA/CUPE indicate SFU employee gets high marks"

Asked whether this was one requisition or a shape: **"This is a sample data, so
BA is NOT the only job."** So it is a general capability, not a one-off run.

**The bundle.** SFU IT Services *Business Analyst*: `About This Role.txt` +
`JD.pdf` (the posting), `Prompt.txt` (the hiring manager's own requirements —
"SQL Experience", "Data Analysis Experience", exactly the §I4 box that shipped
in #102), a **315-row** Taleo *All Candidates* export, and four combined
applicant-export PDFs totalling ~186 pages — so the résumé printouts cover a
**subset** of the 315, not all of them.

Measured against the real file, not assumed: 315 data rows, every one carrying
a name and a **unique** email; work authorization `No Restrictions` 180,
`Work Permit` 123, `Not eligible to work in Canada` 7, `Study Permit` 5, and
**zero blanks**; `APSA Internal = I` on 6 rows, `CUPE Internal = I` on 5.

**What the export settles.** §S3/I1 was recorded as *"blocked on a sample
export"* ([SPONSOR_REQUIREMENTS_PLAN.md](SPONSOR_REQUIREMENTS_PLAN.md) §S3).
It is now unblocked, and the sample contradicts two of the plan's assumptions:

1. The plan said to **match on attachment filename first**. There is no
   attachment-filename column — `Resume` is blank on all 315 rows — so
   reconciliation must be **email-hash first, then normalised name**.
2. Work authorization is not present/absent but **four strings**: `No
   Restrictions` (180), `Work Permit` (123), `Study Permit` (5), `Not eligible
   to work in Canada` (7).

**A warning about what this sample does NOT exercise.** Every one of the 315
rows carries a recognised declaration, so the `unknown` state — the one that
exists precisely so an undeclared candidate is never banded last on a protected
ground — is **never reached by this data**. Its only coverage is the synthetic
fixture at `core/tests/vendor/taleo/all_candidates_export.csv`. Do not read a
clean run against this export as evidence that the `unknown` path works.

**Reconciliation, measured on the real bundle** (the design's central unknown,
so it was tested rather than assumed). Splitting one combined PDF gave 21
résumés; extracting emails from them and intersecting with the CSV: **19 of 21
matched by email exactly, with zero false matches.** The two misses had no
email anywhere in the PDF text, which is what the normalised-name fallback is
for. Email-first is therefore the right primary key, and it is deterministic.

**Decisions taken with the user, 2026-09-09.** These are hiring policy, not
implementation detail:

- **`Work Permit` → `eligible`.** Only `Study Permit` and `Not eligible to work
  in Canada` band last — 12 of 315 rather than 135. The user's own wording was
  "false or student permit", which names neither work permits nor the 39% of
  the pool holding one.
- **An unrecognised value maps to `unknown`, never `not_eligible`**, and the
  raw cell is retained and reported so a recruiter can see the tool did not
  understand it.
- **APSA/CUPE internal status is a bounded, disclosed uplift inside the score**
  — not a hard band above all externals, not a tiebreak. 7 of 315 candidates carry
  the flag; a hard band would put all 7 on top regardless of fit. The uplift
  is stated on the card so a review can explain it.
- **The uplift is +5 of 100, and configurable** (`match_internal_uplift`), so
  HR can retune it per deployment without a code change. Chosen against the
  measured spread on the user's own ranked job — ten candidates scoring 19–50,
  so ~31 points of competitive range. +5 moves an internal candidate roughly
  two to four places: enough to beat a comparable external, not enough for a
  weak internal to displace a strong one. **This is a hiring-policy number,
  not an engineering one.** Do not retune it without HR.

**What running the bundle surfaced — the splitter silently drops pages.**
`core/scripts/split_taleo_pdf.py` was committed in #104 but had never been run
against a real Taleo export. On the first one (43 pages → 21 applicants) it
dropped **page 25** — a complete 3,544-character résumé page carrying its own
contact block and the applicant's only email address — with no warning and
**exit code 0**. The segmentation prompt itself already commands *"Assign EVERY page
(1..N) to exactly one applicant… Every page must appear exactly once"*
([split_taleo_pdf.py:251](../core/scripts/split_taleo_pdf.py#L251)) and
**nothing verifies it**: this repo's signature defect, an invariant in prose
with no enforcement.

It is not only lost content — it is a lost **match key**. That dropped page
held the applicant's only email, and he is one of the two résumés out of 21
that email-reconciliation could not match. A silent page drop converts a
deterministic match into an unmatched row, so the candidate silently receives
no work-authorization and no internal status. Fixed on this branch: page
accounting over the whole input, reporting unassigned **and** double-assigned
pages, and a non-zero exit.

Two lower findings recorded, not fixed — LLM segmentation is inexact by design
and the plan's mandatory confirmation screen (§2.0) is the real mitigation: one
applicant was split into two records across adjacent pages, the two halves
differing only in the letter-case of the name; and on another PDF a phone
number was captured as a candidate's name.

*(Both findings are stated without the candidates' details deliberately — see
the note at the top of this file. The specifics are reproducible from the
bundle in gitignored `data/` by anyone who needs them.)*

**The CSV is the FULL export; the résumés are a subset** (user, 2026-09-09).
This is the permanent shape, not an artifact of this bundle: 315 roster rows
against ~75 printed résumés. It has a consequence the reconciliation report
currently gets backwards.

**An unmatched CSV row is the normal state, not a fault.** ~240 of them will
be unmatched on every single upload of this requisition, forever, because
nobody printed those candidates' résumés. Listing all 240 line numbers in the
report — and in the audit `details` blob, which the security review flagged
independently for volume — is noise that buries the one number that means
something.

**The signal is the mirror image: of the résumés we DO have, how many failed
to match?** That is what catches a real defect — a dropped page that destroyed
an applicant's only email, a name the fallback could not resolve. Today both
counts sit in the same report with equal weight, so a recruiter cannot tell
"240 people didn't print a résumé" (expected) from "3 résumés we hold could
not be identified" (investigate).

**Recorded, not yet fixed.** The report should lead with résumé-side coverage
("71 of 75 résumés matched a roster row; 4 did not — here they are") and treat
the CSV-side remainder as a bare count, not an enumeration.

**"Generate shortlist" queues behind every parse, on the same four slots.**
Measured 2026-09-09 with 75 résumés mid-ingest: clicking Generate returned
200, set `shortlist_state = 'ranking'`, and then **did nothing for hours**,
because arq is FIFO with `max_jobs=4` and all four slots were held by
`parse_resume` tasks running **570–840s each** with **~1,400 jobs queued**
behind them.

**This reproduces, from a completely different cause, the exact complaint a
pilot user made on 2026-09-09** — *"Generate shortlist has not been producing
anything"*. That one was the 2048-token evidence budget. This one is queue
starvation. A recruiter cannot tell the two apart, and in both cases the
screen says the same reassuring thing.

The shortlist page's own copy — *"a full pass realistically takes several
minutes"* — is **wrong by two orders of magnitude** under these conditions,
and it is the only thing the user has to go on.

**Recorded, not fixed. The options, roughly in order of cost:**

1. **Say the true thing.** The queue depth is already known to the worker
   (`j_ongoing`, `queued` are in its health line). Surfacing *"ranking is
   queued behind N parse jobs; expect ~H hours"* costs one query and removes
   the entire class of "is it broken?" It does not make it faster, but it
   stops it looking broken, which is the actual damage.
2. **Give user-triggered work its own lane** — a second queue name, or a
   dedicated worker, so an interactive request never waits on bulk background
   ingest. This is the real fix and it is a deployment change more than a code
   one.
3. Raise `max_jobs`, which trades against the LLM host's own concurrency
   limits — and `HANDOFF.md` lesson 7 is explicit that budgets measured at one
   concurrency tell you nothing about another. **Do not raise it without
   re-measuring.**

At the sponsor's real scale — 315 candidates — this is not a corner case: it
is ~11 hours of parsing during which the product's headline feature appears
to be broken.

**The workflow is not the three steps it looks like.** Driving the real
bundle end to end on 2026-09-09 found an ordering dependency nothing tells the
user about. The DTO asked for *"Upload, Parse and rank"*. It is actually
**Upload → Parse → Roster → Rank**, because a résumé's
`candidate_email_hash` and `candidate_name` are NULL until the parse extracts
them — and those are the only two keys reconciliation can match on.

Uploading the roster immediately after the résumés, which is the obvious
thing to do, matched **0 of 315** rows against 75 freshly-uploaded résumés.
The tool was honest about it (315 CSV rows and 75 résumés both reported
unmatched — ADR-017's invariant held) but a recruiter has no way to connect
"0 matched" to "parsing has not finished yet", and would reasonably conclude
the CSV was wrong.

**Recorded, not yet fixed.** The cheap fix is a sentence, not machinery —
though it needs one more column than the reconciler currently reads:
`_JOB_RESUMES_SQL` does **not** select `resumes.status`, so the response cannot
count how many are still `uploaded`/`parsing` without adding it. With that
column it can say *"N of M résumés have not finished
parsing — re-upload the roster once they have"*. ROADMAP open item; it needs
its own TDD cycle rather than riding this branch.

**Status:** in flight on `feat/candidate-roster-csv`.

---

## 2026-09-09 — pilot user — "i see no manager skills preference input"

Four things were broken at once: the input existed only on the CREATE form
while all 23 pilot jobs arrived through BULK; the job page was read-only;
`additional_requirements` was accepted by `JobUpdate` but absent from
`_UPDATABLE_JOB_COLUMNS`, so `PATCH` returned 200 having changed nothing; and
an edited note kept the previous text's extraction. **Closed by #104.**

## 2026-09-09 — pilot user — "Generate shortlist has not been producing anything"

`match_evidence_max_tokens` was 2048; `gpt-oss:20b` spent it reasoning and
returned empty content, and ADR-029's fail-closed meant one starved candidate
withheld the whole run. The budget had been probed **twice at concurrency 1 and
passed both times**; only at the real fan-out of 4 did it fail. **Closed by
#104** (budget 8192). The structural tail is still open: `shortlist_evidence`
is not one of the prompts `model_probe_live.py` measures, so it had no measured
floor at all.

## 2026-09-09 — pilot user — "Work authorization is not working"

First user click on the §O2 control. `POST /reveal` 200, then `POST
/work-authorization` 403 eleven seconds later: `resume_reveal` re-rendered the
page without any of the four one-shot CSRF tokens, so every audited form on a
just-revealed page carried an empty token. The second half of the report — "the
declaration is a critical eval parameter" — is why every shortlist card now
carries the control. **Closed by #104.**
