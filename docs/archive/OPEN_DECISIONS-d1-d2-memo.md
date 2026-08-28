# D1 / D2 — the memo as written before either was answered

> **ARCHIVED 2026-08-27.** Both decisions were answered on 2026-08-19 and shipped
> by 2026-08-20; the live record is [docs/OPEN_DECISIONS.md](../OPEN_DECISIONS.md).
> This is the reasoning kept for reviewability — the options *not* taken, which is
> what makes the shipped choice defensible later. [ADR-036](../adr/036-auditor-audit-log-viewer.md)'s
> D1 amendment links here for exactly that. **It is not a question.**

---

## The memo, as written before either was answered

---

## D1 — Should an auditor be able to read résumé withdrawal reasons?

**Owner:** whoever owns privacy policy for the pilot. **Status: ANSWERED 2026-08-19 (D1 = option C) and SHIPPED 2026-08-20** — see [ADR-036](../adr/036-auditor-audit-log-viewer.md)'s D1 amendment for what was built, the two defects the work surfaced, and its own accepted residuals. **Formerly blocked:** [ADR-036](../adr/036-auditor-audit-log-viewer.md)'s
named residual; the auditor role is otherwise complete and usable.

### What the code does today

`audit_log.details` is the only free-text column in the table. Two writers populate it, and
`redact_audit_details` is an **allowlist scoped by action**, so anything unclassified is withheld by default:

| Action | Payload | Today |
|---|---|---|
| `role_changed` | `{"old_role", "new_role"}` | Disclosed — enum-shaped, non-PII |
| `withdraw_resume` | `{"reason": <operator prose>}` | **Withheld** |

Withheld is not dropped: the auditor is told a value *exists* and is not shown it, so they can tell "no
reason recorded" from "a reason I may not see."

### Why it is genuinely contested

A withdrawal reason is free text a staff member typed about a **named, identifiable candidate** — an opinion
about an identifiable individual, which is personal information under PIPEDA/FIPPA. Blanket disclosure widens
the readership of unstructured PII to a role that exists to audit *access*.

Against that: an auditor investigating a wrongful-withdrawal complaint cannot do the job without it. Telling
an auditor a reason exists and refusing to show it is exactly the "implies a capability that does not exist"
problem ADR-036 was written to avoid.

### The asymmetry that should drive the answer

**This choice is retroactive, and only in one direction.** Operator prose already sitting in `audit_log` was
typed under today's withheld expectation. Choosing to disclose later changes the disclosure status of data
already collected — you cannot re-obtain the context in which it was written. Choosing to withhold after
disclosing does not un-see anything either. So the cheap, reversible move is to stay closed for existing rows
and open deliberately for new ones.

### Options

| | Option | Cost | Consequence |
|---|---|---|---|
| **A** | Keep withheld (status quo) | none | Auditor cannot investigate a withdrawal complaint without an engineer running SQL — the exact unaudited-read problem ADR-036 closed elsewhere |
| **B** | Disclose to auditors | ~1 line in the allowlist | Every reason ever typed, including pre-decision rows, becomes auditor-readable forever. Retroactive |
| **C** ⭐ | Reveal on request, separately audited | moderate — one route + audit action, mirroring the existing PII reveal | Auditor gets the reason when they need it; each read is attributable and logged. Purpose-limited rather than blanket |
| **D** | Structured reason codes; free text separate | write-path change + backfill | Best long-term hygiene; largest change; does nothing for existing rows |

### Recommended default: C

The codebase **already has this exact pattern** for this exact class of data — candidate PII behind an
audited reveal (`reveal_service`). C reuses a mechanism the product already defends, keeps the auditor's
remit intact, and produces the record that makes the access defensible under PIPEDA/FIPPA: purpose-limited
and logged, rather than a standing grant.

**If you pick A instead:** no code change; keep the UI's "withheld" label; accept that a withdrawal complaint
escalates to an engineer. Reasonable if the pilot is short and no complaint is likely.
**If you pick B:** cheapest, but decide explicitly whether it applies to rows written before today — the
honest implementation would disclose only rows written after the policy changed, which is most of C's work
anyway.
**If you pick D:** worth it only if withdrawal reasons are expected to be analysed in aggregate later.


## Coupling now resolved

D1's option C — reveal on request, separately audited — rests on the reveal being **attributable**. Before
2026-08-19, `reveal_service` sourced its actor from the CAS session identity and fell back to the literal
`"api"` when no identity resolved. D2 = option B (2026-08-19) closes that fallback case — every read now
requires a real principal — so D1=C's audited reveal will carry a real actor `(actor_kind='user', actor_id=...)`,
not `actor='api'`.

When D1=C is implemented: the reveal will be logged with the user who requested it, on the audit trail that
is being read. Closure is complete.

## Implementation — done

D1 and D2 shipped on separate branches: D2 in PR #95 (2026-08-19), D1 on
`feat/d1-audited-reason-reveal` (2026-08-20). Both ADRs carry their amendments.

**One thing this memo got wrong, and it is worth keeping visible.** The memo priced option C at "moderate
— one route + audit action". The route was indeed that. What it missed is that **the withdraw form never
collected a reason at all**, so C as scoped would have shipped a control with nothing to reveal, forever —
all five withdrawals in the live database have a NULL `details`. That was found by running the product,
not by the test suite, and it is now part of the same branch. A memo that reasons about the disclosure of
a field should check that the field is ever populated.
