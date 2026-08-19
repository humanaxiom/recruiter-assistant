# Open decisions — memos, not notes

Two product decisions block work. Both have been carried as bare "blocked on a human" lines for four
sessions; this file exists because `CLAUDE.md`'s Economy rule 3 says that is not a terminal state, and that a
blocked item must be handed on as **options plus a recommended default**, not as a note.

Each memo below is decidable in one sitting. Reply with a letter per decision, or say "take the default".

**How to answer:** pick an option, and an agent implements it. Nothing here is implemented already — the
status quo in both cases is option A, which is a real choice with real costs, not a neutral waiting state.

---

## D1 — Should an auditor be able to read résumé withdrawal reasons?

**Owner:** whoever owns privacy policy for the pilot. **Blocks:** [ADR-036](adr/036-auditor-audit-log-viewer.md)'s
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

---

## D2 — Should a bare service key get unscoped READS?

**Owner:** product. **Blocks:** [ADR-034](adr/034-auth-boundary-fails-open.md)'s explicitly carried question.

### What the code does today

`require_role_assigned` ([deps.py:366-368](../core/src/api/deps.py#L366-L368)) passes when `user is None`, by
documented design — "this gate never judges the ABSENCE of a session, only a REAL session's role." So a
caller holding a valid API key with **no session** gets unscoped reads. Verified live in a previous session:
with an admin key and no session, `GET /jobs` and `GET /audit/reveals-legacy` both return **200**.

Writes are already closed — ADR-034 made `require_session_role` 403 on `user is None`. The asymmetry is
deliberate and undecided, not an oversight.

The sharpest instance is `/audit/reveals-legacy`: a bare key reading the audit log, unattributably. That is
an unaudited read of the audit trail — the same shape as the problem ADR-036 was created to fix.

### What I verified about the blast radius

The question ADR-034 could not answer is "are machine readers legitimate at all". Two facts narrow it:

- **The CAS-off dev boot does not depend on this.** When auth is disabled, `resolve_user` returns a
  `dev-anonymous` admin sentinel *before* the `if not ra_session: return None` branch, so `user` is non-None
  and a 403-on-None rule would not touch that path.
- **The Flask viewer forwards the browser session cookie** alongside its fixed recruiter key
  ([api_client.py:117-128](../core/frontend/api_client.py#L117-L128)), and with CAS on an unauthenticated
  browser is redirected to login before reaching a data page. Its data reads therefore carry a session.

So closing this looks survivable for both shipped entry points. **I have not proven it against the eval and
integration tooling**, which calls the API with a key directly — that is the real cost, and it should be
measured before implementing, not assumed.

### Options

| | Option | Cost | Consequence |
|---|---|---|---|
| **A** | Keep (status quo) | none | A leaked key is a full unscoped **read** credential with no attributable actor, including over the audit log |
| **B** ⭐ | 403 on `user is None` for reads too | small in product code; unknown in test/eval tooling | Symmetric with writes. Every read has a real principal. May break keyed tooling |
| **C** | Named service principals with roles | large | Correct end state if machine readers are legitimate; real auth work |
| **D** | Keep keyed reads, but 403 on the audit routes only | small | Closes the sharpest instance, leaves the general question open |

### Recommended default: B

ADR-034 rejected "F1a alone" precisely because it "closes the write path but leaves reads open." B finishes
that job and makes the boundary say one thing rather than two. There is no known legitimate machine reader in
this product — the two shipped entry points both carry a real principal — so B closes a live hole at the cost
of a constraint on tooling, and tooling can hold a session.

**If keyed tooling turns out to depend on it,** take **D** now and **C** later: D removes the unattributable
audit-log read, which is the part that is indefensible on its own terms, and leaves the broader question for
when a real machine reader actually exists.
**If you pick A:** record it as an accepted risk with an owner, rather than carrying it as undecided — a
leaked read key over candidate data is a reportable event, and "we discussed it" is a materially better
position than "nobody decided."

---

## These two are coupled — answer D2 first

D1's recommended option C rests on the reveal being **attributable**. But `reveal_service` sources its actor
from the CAS session identity and falls back to the literal `"api"` when no identity resolves — which is
exactly the unattributable caller D2 is about. So C answered on its own, with D2 left at option A, buys an
audited reveal whose audit row can read `actor = "api"`: a log entry that records *that* a withdrawal reason
was read and not *who* read it.

That is not fatal — it is still better than no record — but it means **D2=B (or D)** is what makes **D1=C**
worth building. Answer D2 first, or answer both together.

## Answering these

Both are recorded in [ROADMAP.md](ROADMAP.md) and the current [HANDOFF.md](../HANDOFF.md) banner as
outstanding human actions. When one is answered, implement it, update the owning ADR
(036 for D1, 034 for D2) and delete the memo from this file — a decided question should not keep
occupying the next session's attention.
