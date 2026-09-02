# Open decisions

## Nothing is open.

Both decisions this file was created for were answered on 2026-08-19 and shipped
by 2026-08-20. **Adding a new decision means adding it above this line.**

| | Decision | Answered | Shipped |
|---|---|---|---|
| **D2** | Close unscoped keyed reads → **option B** | 2026-08-19 | PR #95 · [ADR-034](adr/034-auth-boundary-fails-open.md) amendment |
| **D1** | Auditor access to withdrawal reasons → **option C** (reveal on request, separately audited) | 2026-08-19 | 2026-08-20 · [ADR-036](adr/036-auditor-audit-log-viewer.md) amendment |

Both were the recommended defaults, and **the coupling was respected**: D2 removed
`reveal_service`'s `actor = "api"` fallback, so D1=C's audited reveal carries a
real principal rather than an unattributable one. D2 answering first is what made
D1 worth building.

The full memo — the options not taken, the PIPEDA/FIPPA reasoning, and the
retroactivity asymmetry that drove the answer — is archived at
[OPEN_DECISIONS-d1-d2-memo.md](archive/OPEN_DECISIONS-d1-d2-memo.md).

## Two lessons worth more than either answer

**"Blocked on a human" is not a terminal state.** These sat as bare *blocked*
lines for five sessions, each of which re-noted the block and moved on. What
unstuck them was writing the options down with a **recommended default** — after
which both were answered in a day, both as the default. See `CLAUDE.md` §Economy 3.

**A memo that reasons about disclosing a field should check the field is ever
populated.** The memo priced option C at "moderate — one route plus an audit
action". The route was indeed that. What it missed is that **the withdraw form
never collected a reason at all**, so C as scoped would have shipped a control
with nothing to reveal, forever — all five withdrawals in the live database had a
NULL `details`. Found by running the product, not by 5,448 tests.

## Decisions that are open elsewhere

Two live product decisions are tracked in [ROADMAP.md](ROADMAP.md), not here,
because each is attached to work rather than standing alone:

- **Competency scoring** (ROADMAP open item 3) — `years × recency ×
  ontology_weight` is a semantically odd model for "three years of interpersonal
  skills, last used 2024". Owner: corpus owner + HR. It was deferred pending
  pilot data; **there is now pilot data**, so the deferral has expired.
- **Revoke-and-purge semantics** (ROADMAP open item 1, ADR-026 §4) — the repo's
  first destructive PII operation. Needs an HR decision *and* its own security
  review before any code.
