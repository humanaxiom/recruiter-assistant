# ADR-047: Screening facts are declared, never inferred

**Status:** Accepted
**Date:** 2026-09-09
**Related:** [ADR-004](004-phase-0-storage-schema-embedding-contract.md) (the `resumes` schema and PII contract), [ADR-008](008-skill-graph-pii-by-construction.md) (never embed PII), [ADR-017](017-bulk-ingest-pairing.md) (reconciliation, amended separately), [ADR-009](009-matching-engine-port.md) (how the uplift reaches `score_final`, amended separately)
**Owed since:** 2026-09-02 — `HANDOFF.md` §3 recorded the work-authorization ADR as *"owed and not yet written"*, and required it to record **why inference was rejected**. This pays that down, and covers the second instance of the same decision rather than writing a near-identical sibling next month.

## Context

Two candidate attributes now affect where a person lands on a ranked list:

1. **Work authorization** (sponsor §O2, shipped 2026-09-02). An ineligible
   candidate keeps their card but shows `—` for rank and `n/a` for every
   sub-score, with the reason on screen.
2. **SFU internal-employee status** — APSA or CUPE membership (sponsor
   2026-09-09: *"APSA/CUPE indicate SFU employee gets high marks"*). Applied as
   a bounded, disclosed uplift inside `score_final`.

Both are **screening facts**: statements about a person that change a hiring
outcome, and that the system could plausibly guess at from text it already
holds. A résumé says "Study Permit" or "authorized to work in Canada"; it lists
"Simon Fraser University" as an employer. The LLM parsing that résumé could
emit either attribute as a field, at no extra cost, today.

**It must not, and this ADR is the record of why.**

## Decision

**Neither attribute is ever inferred — from a résumé, a cover letter, a name,
an email domain, an employer string, or any model output. Both are read from a
source where a human declared them, or from a recruiter's explicit,
audited correction. There is no third path, and adding one requires amending
this document.**

Concretely:

- Work authorization comes from the candidate's own Taleo prescreen answer, via
  the roster CSV (`Work Authorization`), or from a recruiter's audited
  declaration on the résumé page.
- Internal status comes from the roster CSV's `APSA Internal` / `CUPE Internal`
  columns, or from an audited write. Never from an employer field.
- The mapping from a Taleo string to our three-state enum lives in exactly one
  place, `bulk_ingest_service.WORK_AUTHORIZATION_MAP`, and **an unrecognised
  value maps to `unknown`, never `not_eligible`** — see §"The default that
  matters most".

## Alternatives that were live, and why they lost

These were real options, not strawmen. Option A was this plan's own
recommendation, and option C is what actually shipped first.

### A — Read the candidate's declaration from the Taleo CSV ✅ **chosen**

Recommended in `SPONSOR_REQUIREMENTS_PLAN.md` §2.1 and deferred on 2026-09-02
only because the CSV's shape was unknown ("TBD"). The sponsor delivered a real
315-row export on 2026-09-09 and it settled the question: **every row carries
the prescreen answer.** The source is the candidate's own self-attestation, it
is auditable, and it is exactly what a human reviewer would use.

### B — Infer it from résumé text with the LLM ❌ **rejected**

Cheap, already-plumbed, and requires no new integration. Rejected for four
reasons, in descending order of how much they matter:

1. **It is an adverse decision on a protected ground, made from a guess.**
   Immigration status is protected under the BC Human Rights Code. A model that
   reads "student" near "visa" and bands a real candidate last has produced a
   discriminatory outcome from a hallucination, and the candidate has no way to
   see it, contest it, or know it happened.
2. **It cannot be defended in a review.** "Why was I screened out?" has exactly
   one acceptable answer — *"you told Taleo this, here is the field"* — and no
   acceptable version of *"the model concluded it from your résumé."* The same
   is true in the other direction for the internal uplift: "why did they get
   +5?" must be answerable with a roster column, not an employer string.
3. **The evidence is systematically unreliable.** A résumé is a marketing
   document. Candidates omit immigration status far more often than they state
   it, and absence would read as a negative signal precisely for the people
   most exposed to one. For internal status, "Simon Fraser University" on a
   résumé may be a degree, a co-op term, a contract that ended in 2019, or a
   current APSA appointment — the string does not distinguish them, and three
   of those four earn no preference.
4. **It would be silent.** Inference produces a value indistinguishable from a
   declaration once it is in the column, so nobody downstream can tell which
   rows were guessed.

### C — Recruiter marks each candidate manually ⚠️ **retained as the correction path, not the primary**

This shipped first, because answer 1 of 2026-09-02 promoted it while the CSV
was unknown. It is honest and fully audited, and it is **useless as the primary
at 315 applicants** — which is exactly the scale the sponsor's real export
arrived at. It survives as the mechanism for correcting a row the CSV did not
cover or got wrong, which is a genuine need: reconciliation leaves rows
unmatched by design rather than guessing (see the ADR-017 amendment).

## The default that matters most

`unknown` exists so that **not knowing is representable**, and the whole design
turns on refusing to collapse it into `not_eligible`:

- The column is `NOT NULL DEFAULT 'unknown'`, so every pre-existing row
  back-fills to "undeclared" the instant the migration lands. A nullable column
  would leave consumers to remember that NULL means undeclared, and the first
  one that coerced falsy to "not eligible" would band real people last. **The
  database refuses to represent that state at all.**
- An unrecognised CSV string maps to `unknown` and the raw cell is retained in
  `work_authorization_source`, so a recruiter is told *the tool did not
  understand this value* rather than having a state nobody chose recorded
  silently. A parametrized test pins this over several unrecognised strings.

⚠️ **The 2026-09-09 sample does not exercise this path.** All 315 rows carry a
recognised declaration, so `unknown` is never reached by real data; its only
coverage is the synthetic vendor fixture. A clean run against a real export is
**not** evidence that this branch works.

### Why internal status is `NOT NULL DEFAULT FALSE`, and is not the same trap

`internal_apsa`/`internal_cupe` default to `FALSE`, which looks like the exact
mistake §"The default that matters most" forbids. It is not, and the difference
is **directional**:

- A falsy default on *eligibility* produces an **adverse** outcome — a real
  candidate banded last on a protected ground.
- A falsy default on *internal status* produces the **absence of a bonus**,
  which is the correct reading until a roster says otherwise, and which
  self-corrects the moment one is ingested.

There is no adverse action in a false negative on a bonus, so the extra state
would buy nothing. Note that the **parsed** representation still distinguishes
the three cases — `CandidateRosterRow.internal_apsa` is `bool | None`, where
`None` means the column was absent from that export — because a roster that
never mentions APSA must not clear a flag a previous roster legitimately set.
The distinction is needed where it does work, and not persisted where it does
none.

## Consequences

- **The roster CSV becomes a screening-data path, with the obligations that
  implies.** It carries PII, so it crosses the same encryption boundary as
  `candidate_name`/`candidate_email` and is never embedded (ADR-008). The
  reconciliation report and its audit event carry counts, line numbers and
  résumé ids only — never a decrypted name or email — which matters because
  the name fallback decrypts names in bulk.
- **Every write is audited and idempotent**, through `set_work_authorization`
  and `set_internal_status`. There is no second write path to either column,
  deliberately: a screening fact with two writers has no single audit trail.
- **Reconciliation refuses rather than guesses.** Conflicting duplicate rows,
  and names matching more than one résumé, are reported and left unwritten. A
  wrong attribution here is worse than an absent one, because it is invisible.
- **A future "infer it" feature is now a decision that has to argue with this
  document**, rather than a field someone adds to a parse prompt because it was
  easy. That is the point of writing it down.
- **This does not settle the uplift's magnitude**, which is a hiring-policy
  number (currently `match_internal_uplift = 0.05`), owned by HR. See the
  ADR-009 amendment for how it reaches `score_final` without becoming a weight.
