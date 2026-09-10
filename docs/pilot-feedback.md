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
   attachment-filename column — `Resume` is blank on all 316 rows — so
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
  in Canada` band last — 12 of 316 rather than 135. The user's own wording was
  "false or student permit", which names neither work permits nor the 39% of
  the pool holding one.
- **An unrecognised value maps to `unknown`, never `not_eligible`**, and the
  raw cell is retained and reported so a recruiter can see the tool did not
  understand it.
- **APSA/CUPE internal status is a bounded, disclosed uplift inside the score**
  — not a hard band above all externals, not a tiebreak. 11 of 315 rows carry
  the flag; a hard band would put all 11 on top regardless of fit. The uplift
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
contact block and the applicant's only email address — with no warning and **exit
code 0**. The segmentation prompt itself already commands *"Assign EVERY page
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
and the plan's mandatory confirmation screen (§2.0) is the real mitigation: the
same person was split into two applicants (one applicant across adjacent pages, the halves differing only in letter-case
p19–20), and a phone number was captured as a name (a phone number).

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
