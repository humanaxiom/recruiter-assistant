# Open decisions — memos, not notes

**One product decision remains open.** D2 was answered by the product owner on 2026-08-19 (D2 = option B,
implemented on branch `feat/d2-close-unscoped-reads`); see the amendment in [ADR-034](adr/034-auth-boundary-fails-open.md).

D1 has also been answered by the product owner on 2026-08-19 (D1 = option C) and is being implemented on a separate
branch; it is updated below with that decision noted.

Each remaining memo is decidable in one sitting. Reply with a letter per decision, or say "take the default".

**How to answer:** pick an option, and an agent implements it. Nothing here is implemented already — the
status quo in the remaining case is option A, which is a real choice with real costs, not a neutral waiting state.

---

## D1 — Should an auditor be able to read résumé withdrawal reasons?

**Owner:** whoever owns privacy policy for the pilot. **Status: ANSWERED 2026-08-19 — D1 = option C. NOT YET IMPLEMENTED** (D2 was implemented first, deliberately: see the coupling note below). This memo is kept until the implementation lands. **Blocks:** [ADR-036](adr/036-auditor-audit-log-viewer.md)'s
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

## Implementation

D1 and D2 are implemented on separate branches. D2 is already done. When D1 = option C ships, update
[ADR-036](adr/036-auditor-audit-log-viewer.md) with its decision.
