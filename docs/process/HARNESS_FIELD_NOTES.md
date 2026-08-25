# Harness field notes — `pmo-harness-template`

**For the seed maintainer.** Written 2026-08-24 from 45 days on
`recruiter-assistant`, built from this seed. Every figure is measured against
that repository's working tree; nothing here is carried on assertion.

> **The harness can tell a *change* is done nine different ways, and cannot tell
> a *product* is done at all.**

Every criterion in `docs/DOD.md` asks whether the work is *correct*. None asks
whether it *reached anyone*. That is not a neutral omission: a gate can only ever
add work, so a harness assembled entirely from gates is a ratchet with no
release.

On this project that produced **12 ADRs to build the product and 32 more after it
was finished**, **97,517 lines of test against 18,983 of source**, and **zero
users in 45 days** — with every individual decision defensible on its own terms.

I went looking for project-level mistakes. Three of the four root causes turned
out to be in the template, and each is checkable against a file you own.

---

## Part I — Three of these are in the seed, not the project

### 1. The Definition of Done is change-level only

`docs/DOD.md` — **anti-pattern**

Nine criteria, all of them about a diff:

| # | Criterion | Kind |
|---|---|---|
| 1 | Plan + acceptance criteria | process |
| 2 | Tests before implementation, coverage floor | process |
| 3 | lint / type / project gates | correctness |
| 4 | No new external dependency | constraint |
| 5 | Domain provenance requirement | correctness |
| 6 | Migrations reversible | correctness |
| 7 | Judge APPROVE recorded | process |
| 8 | CHANGELOG + README updated | documentation |
| 9 | PMO gate recorded | process |

Satisfy all nine, on every ticket, for six weeks, and you can still have shipped
nothing. **That is exactly what happened.** Every phase of the plan of record
passed this DoD. The product was feature-complete on 2026-07-17 and had no user
on 2026-08-23.

**Seed change:** add a second, *product-level* DoD in the same file — one line
naming a person outside the build who consumes the output, and the date they get
it. Phrase it so it can be false on a given day. *"A real recruiter ranks a real
requisition and says whether the shortlist is sensible"* can be false.
*"High-quality software"* cannot.

> **Evidence** — 9/9 DoD criteria satisfied per ticket · v1 scope complete
> 2026-07-17 · first human use 2026-08-23

### 2. The retrospective is a one-way ratchet

`.claude/commands/retro.md` — **anti-pattern**

`/retro` mines CI health and recurring judge findings, then produces *"max 3
process changes, each with a concrete edit to `CLAUDE.md`, an agent definition,
or the `Makefile`. Apply approved edits."*

There is no removal step, no budget on total rule count, and no expiry. **The cap
of three bounds the rate, not the total.** Twenty sprints is up to sixty process
edits, monotonically increasing, each justified by a real finding — and each one
more thing every future session must read before it can act. The instrument built
to improve the process can only ever enlarge it.

It also writes `docs/retros/sprint-N.md` per sprint, so the artifact recording the
growth grows too.

**Seed change:** require each retro to propose **at least one removal**, and make
the cap net rather than gross. A rule that has not fired in three sprints is a
deletion candidate, and the retro is the only place anyone will ever look for it.

> **Evidence** — `CLAUDE.md` in this project reached 260+ lines of process · every
> addition traceable to a real finding · nothing ever removed

### 3. The one stakeholder touchpoint exists in prose and nowhere in enforcement

`docs/SDLC.md` — **anti-pattern**

`SDLC.md` lists it plainly: *"Review/demo → human stakeholder demo."* The cadence
is right. But it is not a line in `DOD.md`, not a submittable PMO gate, and not a
step in `/tdd-feature` — so nothing anywhere reports its absence.

**It never fired once in 45 days, and no gate went red as a result.** Meanwhile
`register_run`, `submit_gate`, `submit_work_item` and `release_run` all faithfully
recorded a project that was not reaching anyone.

The sharp part: this project named its own characteristic defect as *"an invariant
asserted in prose with nothing enforcing it"* and catalogued **21 instances** of
it. **The harness that taught it to hunt that pattern contains the same defect**,
in the one place where it costs the most.

**Seed change:** promote the demo to a recorded gate with a date. If a run is
released without one, the PMO board should show it. An audit trail that cannot
distinguish *shipped* from *merged* is measuring the wrong thing.

> **Evidence** — 0 stakeholder demos in 45 days · 0 gates red as a result · 21
> catalogued instances of the identical defect class, all in project code

### 4. Rigour is self-amplifying, and naming a defect class accelerates it

Emergent — **anti-pattern**

Two mechanisms that will recur in any project built from this seed.

**First:** fixing a defect creates an invariant, an invariant needs a guard, and a
guard is a new surface that can itself be probed. *The output of the quality
machinery is more input for it.*

**Second, less obvious:** this project named its characteristic defect and began
counting instances. The count reached 21. The taxonomy stopped being a diagnosis
and became **a scoreboard** — finding instance 22 felt like progress in a way that
shipping did not, because the harness could score the former and had no measure at
all for the latter.

The seed already half-knows this. It caps mutation probing at one pass, explicitly
because the author noticed that recursion *"has no natural exit."* That instinct
was correct and got applied to exactly one mechanism. It generalises to all of
them.

> **Evidence** — 6,086 tests · 5.1× more test code than source · mutation-probe
> cap already present and still the default reflex

---

## Part II — Four more seed changes, all cheap

### 5. Documentation mandated per change is a documentation generator

`docs/DOD.md` §8 — **add to seed**

Criterion 8 requires a CHANGELOG entry and updated README/docstrings on *every*
change. Reasonable per ticket; compounding across hundreds. This project ended
with ~16,700 lines of prose against 18,983 lines of source, and documentation
commits outnumbered feature commits more than two to one for its final five weeks.

The deeper issue is that prose carries a **compounding read cost**: every session
pays to read it before it can add to it, and every session adds. The handoff file
reached 3,251 lines before anyone capped it.

**Seed change:** make §8 conditional on user-visible change, and cap the standing
artifacts by **item count rather than length** — a line cap is satisfied by
compression, an item cap forces a deletion decision. Eight items worked here.

> **Evidence** — ~16,700 lines docs vs 18,983 source · `HANDOFF.md` 3,251 lines ·
> docs commits 32 vs feat 14 since scope completion

### 6. Put a bar on ADRs, and name the commit message as the alternative

`docs/adr/` — **add to seed**

Twelve ADRs carried this product's entire build. Thirty-two followed it, about a
finished product. The seed ships ADR-0001 ("record architecture decisions") and no
threshold, so the default became *one per branch*.

Require **all three**:

1. **Live alternatives** a competent engineer would have chosen between — not one
   obvious implementation written up after the fact.
2. **Expensive to reverse**: schema, wire format, auth model, storage layout, a
   cross-cutting invariant.
3. **Irrecoverable** from the code, its tests and the commit message six months
   out.

Failing any one, the reasoning goes in the **commit message** — which is where the
best writing in this repository already is, is attached to the diff permanently,
and is the only artifact nobody has to reconcile against a later change.

> **Evidence** — 12 ADRs for Phases 0–7 · 32 afterwards · the same reasoning
> already present in the commits that made the changes

### 7. Verification cost should scale with risk

`Makefile` · `CLAUDE.md` — **add to seed**

One bar applied uniformly — full gates before every commit — is right for a schema
change and absurd for a Markdown edit. The absurd case is the common one, so
people either burn nine minutes or quietly stop running gates. **A bar that is
wrong in the cheap case teaches people to bypass it in the expensive one.**

**Seed change:** three named tiers — docs-only (CI covers it), source (offline
gates), and schema/migration/auth/datastore (full suite). Naming them in the
template makes the choice explicit instead of improvised.

> **Evidence** — ~9 min per full run · four full runs in one session for changes
> touching no source

### 8. Ship the three verification primitives tests cannot replace

`scripts/` — **add to seed**

This project invented three independently on day 43, and **each found a real
defect on its first run**. Every project needs them; none discovers that early
enough.

| Primitive | Proves | Found on first run |
|---|---|---|
| `doctor.sh` | The **data** in a live deployment | 20 stranded rows; an auth-disabled stack serving the audit log |
| `smoke.sh` | The **seam** every test mocks away | Ranking silently dropping candidates |
| `model-check.sh` | The **dependency**, at real concurrency | A token budget 2× too low, failing every extraction |

Two design details are load-bearing and neither is obvious:

- They **fail rather than skip** when they cannot run. A green run that exercised
  nothing is worse than no run.
- The dependency check uses **real inputs at real concurrency**. A synthetic
  prompt of identical length passed while the real document failed three times out
  of three; one uncontended call took 35s where four concurrent ones blew a 300s
  timeout.

> **Evidence** — all three written day 43 · all three found defects immediately ·
> 4 of the last 9 user-visible defects lived in the smoke seam

---

## Part III — What worked, do not trim these

### 9. Model routing, and the human checkpoints

`docs/SDLC.md` — **keep**

Opus for judgment-heavy low-volume roles, sonnet for structured production, haiku
for mechanical work, with a global override for cost ceilings. Quality held
because every producer diff passed strong verifiers before merge. Best
cost-to-quality lever in the seed; needs no change.

The named human checkpoints — push, promotion, sprint scope — are also right, and
are the one place the seed already refuses to automate. **The stakeholder demo
belongs in that list.**

### 10. The evidence contract for delegated work

`CLAUDE.md` — **keep**

*"A subagent's claim of green is not evidence of green — require the pasted
command and its real output."* This caught real defects, and it generalises beyond
subagents: I hit the identical failure driving a background task whose exit code
was masked by a trailing `echo`, reported success, and was caught only by reading
the log this rule requires.

Its companion is subtler and worth keeping verbatim: **when a report is thin,
suspect the instruction before the agent.** Asking for a diff summary and
receiving one is a prompt bug, not an agent failure.

### 11. An adversarial judge — with the severity table bound to it

`.claude/agents/judge.md` — **keep, with one adjustment**

A separate judgment role that can return REVISE is genuinely valuable and should
stay. One adjustment: make **merge-blocking a property of the finding severity,
not of the agent**.

Four roles in this project were declared merge-blocking wholesale. Each can add
work; none can say *ship it*. Against a good reviewer — and this one is good — the
exit condition silently becomes "the judge ran out of findings" rather than "the
change is good enough." The finding-disposition table already exists and already
says minors get recorded, not fixed. It just needs wiring to the blocking status.

---

## The caveat that matters

**None of this argues for less rigour.** The rigour produced genuinely good code:
strict typing throughout, real integration tests against real datastores, a
defensible privacy model, and a defect-finding culture that caught things most
projects ship. I would keep every gate exactly as it is.

The argument is that **rigour needs a terminating condition, and the seed does not
supply one** — so every project invents it late, usually after someone outside the
build asks why nothing has shipped. That question *is* the terminating condition.
The harness should ask it on day one rather than leaving it to a frustrated
stakeholder on day 45.
