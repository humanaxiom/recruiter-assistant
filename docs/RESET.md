# Reset — from a finished product to a used one

**2026-08-24.** This document replaces the priority order in `docs/ROADMAP.md`.
Part A of that file described pilot readiness; this describes the pilot.

---

## The finding

Every phase of the Plan of Record was complete on **2026-07-17**. In the 38 days
since, the repository produced **94 commits, 32 ADRs, and zero recruiters using
the product.**

Measured against the working tree on 2026-08-23:

| | |
|---|---|
| ADRs to build Phases 0–7 (the whole product) | **12** |
| ADRs written after it was finished | **32** |
| Source lines | 18,983 |
| Test lines | 97,517 — **5.1×** the product |
| Documentation lines | ~16,700 (`HANDOFF.md` alone was 3,251) |
| Commits since scope complete: `docs` vs `feat` | **32 vs 14** |
| Recruiters who have used it | **0** |

The product is roughly **14% of what the repository contains**.

## This is not a quality problem

The code is good and the rigour is real. The pipeline was proven end to end on
2026-08-22; 24 of 25 real SFU postings parse; blind review, PII encryption and
the audited reveal all work.

It is a **stopping problem**. The process has no condition under which it
declares something finished, so it keeps finding work in the thing it already
built.

**The mechanism:** fixing a defect creates a new invariant, a new invariant
needs a guard, and a guard is a new surface to probe for defects — so the work
regenerates itself, and every cycle is individually justifiable. Without a user,
the only feedback is introspection, and introspection has no natural stopping
point.

The evidence for that last claim is direct: **two days of real usage produced
better signal than the four weeks before it.** The smoke suite's first run found
ranking silently dropping candidates. The first human to open a job page found
20 dead jobs the entire test suite was content with.

**The repository already knew.** On 2026-08-14 `docs/ROADMAP.md` recorded "85
commits, 35 `docs`/`chore` against 26 `feat`/`fix`, twelve ADRs, on a product
nobody has yet used end to end", and `CLAUDE.md` gained an Economy section to
stop it. Ten days later every ratio was worse. A guardrail was written and the
work continued straight through it — which is why this document sets a
destination rather than another rule.

## Definition of done

> **A real recruiter, signed in as themselves, ranks a real requisition against
> real applicants and says whether the shortlist is sensible.**

Nothing that does not move that forward gets built. Not a test, not an ADR, not
a roadmap entry.

## The plan

1. **Turn authentication on and walk the product.** Four `.env` values, three
   services recreated, then click every path as recruiter, hiring manager and
   auditor. Highest-information action available; it has been one edit away for
   two days. *(~1 hour, needs a human at a keyboard, blocks everything below.)*

2. **Load one real requisition and its real applicants.** Not fixtures. Read the
   shortlist and judge whether the order is defensible. This is the only question
   that matters and no test can answer it. *(~half a day.)*

3. **Put it in front of one recruiter.** One person, one session, watched. Their
   confusion is the backlog — everything currently in `ROADMAP.md` is a guess
   about what they will need. *(~1 session. This is the pilot, not a rehearsal.)*

4. **Fix only what that session surfaced.** Anything they did not hit goes to a
   backlog file and is not discussed again until a second recruiter hits it.
   *(1 week, hard stop.)*

Three known items survive the cut, all small, all found by *running* the product:

| Item | Why it survives | Size |
|---|---|---|
| `pg.jobs_stuck` in `core/src/doctor.py` | The reason 20 dead jobs reached a human instead of the tooling | ~1h |
| Job `306c573c` fails extraction | Longest real posting (9,523 chars); live data defect, not hypothetical | ~2h |
| GitHub Support PII purge | Real candidate résumés remain fetchable by SHA on a public repo | ~15m |

## What stops

"Be more efficient" has already failed once, so these are explicit thresholds,
not exhortations. All are enforced in `CLAUDE.md` §Economy 0/0a/0b.

**ADRs — 32 were written about a finished product.** One is now an exceptional,
roughly monthly act, and needs **all three** of: live alternatives a competent
engineer would have chosen between; expensive to reverse (schema, wire format,
auth model, storage layout, cross-cutting invariant); and irrecoverable from the
code, tests and commit message six months out. Fails any one → the reasoning goes
in the **commit message**, which nobody has to maintain or reconcile later. Never
write one to record a bug fix, restate a test, document a measured number, or
memorialise a decision *not* to change something. Amend an existing ADR rather
than adding a sibling.

**PRs — four were opened in one session**, one purely to update a handoff the
next PR then rewrote. Each costs a ~9-minute CI run, a review, a merge and a
`main` sync. One PR per coherent *change*, not per commit; push to the open PR
instead of opening another. Docs-only changes ride along with the next code PR or
wait. Never open a PR just to record state. Branch-per-task and never-commit-to-
`main` both stand — what changes is how much a branch carries before it earns a PR.

**`HANDOFF.md` — capped at eight items**, not a line count. A ninth means one is
no longer relevant; delete it. History goes to `docs/archive/`, never inline.

**Also stopping:** hunting A7 instances (21 catalogued; the taxonomy became a
generator — fix defects users hit, stop auditing for ones nobody has hit);
full-gate runs on documentation commits; mutation-probing new guards unless the
invariant protects money, PII or authorization; and treating "recorded in
ROADMAP" as free — a finding nobody will action gets deleted, not filed.

## What this does not change

The gates stay. `./scripts/verify.sh` before a PR, `doctor.sh` after anything
touching state, `smoke.sh` before handing the stack to anyone. They are cheap and
they have each caught real defects. What changes is what gets *built* between
them — not how it is verified.
