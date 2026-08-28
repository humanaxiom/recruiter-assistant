# Harness lessons — porting these fixes to another project on the same template

This document is written to be **lifted into any repo built on the same agent harness**
(`agent-harness-template`: `CLAUDE.md` + `.claude/agents/*.md` + a `Makefile` gate suite +
merge-blocking reviewer/security/domain gates).

Every problem below was observed in production use of that harness, not theorized. Each section
gives the **symptom**, the **root cause**, the **fix**, and **how to verify the fix worked**.

The unifying insight, stated once up front:

> **Agents don't fail the gate. They fail to *run* it — because the harness made the narrow check
> the easy one, and made verification optional.**
>
> Fixing this is not about telling agents to try harder. It is about removing the ability to do the
> wrong thing, and requiring evidence for the right one.

---

## 1. The canonical gate command must actually run in the agent's environment

**Severity: highest. Fix this one first — most other verification failures are downstream of it.**

### Symptom

Agents report green. The gate is red in CI, or a defect ships. Reading transcripts, every agent ran
a *different* verification command, and each was slightly narrower than the real gate.

Recorded instances from one project:
- An agent ran `mypy src` where the real gate runs `mypy src frontend` → a frontend type error
  reached a PR.
- An agent ran `black --check frontend` only → test files were left unformatted, gate went red later.
- An agent ran the unit suite only for a **schema** change → a `NOT NULL` column with no `DEFAULT`
  passed 2764 unit tests and would have failed the first real `INSERT`.

### Root cause

`CLAUDE.md` said *"run `make gates`"*. But the dev host had **no usable Python**, so `make gates`
could not run natively. Every agent therefore had to improvise a containerized equivalent — and
improvisation means *re-listing the gate's commands from memory*. Each re-listing drifted.

Generalize the rule: **any gap between "the documented gate command" and "a command the agent can
actually execute" is filled by improvisation, and improvisation is always narrower.** The same gap
appears when the gate needs credentials the agent lacks, a service that isn't running, or a
platform-specific invocation.

### Fix

Create **one executable entry point** that works in the agent's real environment, and have it
**invoke the real gate definition** rather than re-listing the commands:

```bash
# scripts/verify.sh — the ONE way to verify work
docker run --rm -v "${REPO}:/repo" -w /repo python:3.11-slim bash -lc "
  apt-get install -y -qq make git
  pip install -q -r core/requirements.txt -r core/requirements-dev.txt
  make ${TARGET}          # <-- the Makefile stays the single source of truth
"
```

The critical design choice is the last line. **Do not copy the gate's commands into the script** —
call the Makefile (or `package.json` script, or `tox.ini`, or whatever CI invokes). If the script
re-lists commands, you have created a *second* source of truth that will drift from CI exactly the
way the agents did. By shelling into the real target:

- narrowing becomes impossible (there is nothing left to narrow),
- drift from CI becomes impossible (CI and agents run the same definition),
- updating the gate updates every agent automatically.

Have it also absorb every environment quirk agents kept getting wrong — path translation, which
directory to mount, cache clearing, socket mounts for containerized integration tests.

### Verify the fix

1. Run it on a clean checkout of the main branch and confirm it passes.
2. Run **every mode** (`offline`, `integration`, `all`) — a mode nobody tested is a mode agents will
   silently avoid.
3. Grep the agent definitions for any *other* verification command and delete them all. If two
   commands exist, agents will pick the faster one.

---

## 2. `offline`/unit mode is not always sufficient — map paths to required gate depth

### Symptom

A change is green on the unit suite, reviewed, merged — and breaks against a real database, driver,
or external service.

### Root cause

Unit tests of infrastructure code frequently **string-match the source** rather than execute it.
A test asserting `"role TEXT NOT NULL" in DDL_SOURCE` passes whether or not the schema is correct.

The canonical example: a `users.role` column declared `NOT NULL` with **no `DEFAULT`**. The spec
required a first login to omit `role` and receive a default. Every unit test passed. Only an
`INSERT` against a real Postgres could see it.

### Fix

Put an explicit **path → required mode** table in `CLAUDE.md` *and* in each producer agent file:

| If the diff touches | Required verification |
|---|---|
| schema/DDL, raw SQL, any DB query | full (integration) |
| API routes, services, background workers | full (integration) |
| graph/vector DB, embeddings, external protocol clients | full (integration) |
| pure functions, data models, formatting, docs | offline is enough |

State the *reason*, not just the table, so it generalizes to paths you didn't enumerate:

> **If the correctness of a change depends on how a real database, driver, or service behaves, the
> unit suite structurally cannot prove it.**

### Verify the fix

Take the last three infrastructure bugs that escaped to CI or production. For each, ask: *would this
table have forced the run that catches it?* If not, the table is too narrow.

---

## 3. Require pasted evidence, not claims of success

### Symptom

A subagent's final report says "all gates pass" or summarizes its diff. The work is unverified. The
coordinator trusts it because the report reads confidently.

### Root cause

Two compounding causes, and **the second is usually yours**:

1. No agent definition said what "done" must *contain*. "Report back" is satisfiable by prose.
2. **The task prompt asked for the wrong thing.** In the observed case the coordinator's prompt
   literally said *"report back with the diff summary"* — and got a diff summary. That is a prompt
   bug, not agent misbehavior.

### Fix

Add an explicit evidence contract to every **producer** agent definition:

```markdown
## Report back with evidence, not claims

Your final message MUST contain, verbatim:
1. The exact command you ran
2. Its last ~15 lines of real output, pasted — including pass/fail counts
3. One line on what is now green that was red

A claim of green without pasted output is not an acceptable completion report and
will be sent back. If you did not run it, say so plainly — an honest "I did not
verify this" is useful; an unverified claim of green is worse than no report,
because it gets believed.
```

For **tester**-type agents, the same contract applies to RED: *"a test that passes when you believe
it fails silently converts the whole TDD cycle into theatre."*

And add the matching coordinator rule to `CLAUDE.md`:

```markdown
## Trusting subagent reports

A subagent's claim of green is not evidence of green. Require the pasted command and
its real output; if a report only summarizes a diff, treat the work as unverified and
re-run the gate yourself before committing.

When a report is thin, check what you actually asked for before attributing the miss
to the agent. Asking for "a diff summary" and receiving one is a prompt bug.
```

### Honest limitation

This makes the *narrow* failure impossible but **cannot** stop an agent from claiming it ran the
script when it didn't. Only the coordinator re-running the gate closes that. Put that duty in
`CLAUDE.md` rather than relying on agent files alone — it is cheap and it has already caught a real
defect.

---

## 4. Audit agent definitions for stale instructions — they rot silently

### Symptom

Agents waste tokens on impossible steps, or follow guidance that contradicts the codebase. Nobody
notices, because a *prompt* file has no compiler, no test, and no CI check.

### Root cause

Agent definitions are written once, at template time, and never re-read. The codebase moves; the
instructions don't. Three found in a single audit of one repo:

| Stale instruction | Reality |
|---|---|
| "Check graph memory: `curl localhost:8000/memory/similar`" (in **3** agent files) | Route deleted phases ago. `CLAUDE.md` explicitly said not to curl it. A planner spent its opening paragraphs explaining why it was declining. |
| "All model calls go through `AsyncOpenAI(...)`" | The `openai` package had been **removed** by an architecture decision in favour of a hand-rolled client. |
| "Run `make gates`" / "run `pytest`" | Neither works on the dev host — cause of problem #1. |

Note the second-order cost: agents that *correctly* detect the contradiction still burn reasoning
and output tokens narrating the conflict, on every single run.

### Fix — run this audit on day one in the new project

```bash
# 1. Every command an agent is told to run — does each actually work here?
grep -rn "curl \|make \|pytest\|npm \|docker run\|http://localhost" .claude/agents/ CLAUDE.md

# 2. Every library/API an agent is told to use — is each still a dependency?
grep -rn "import \|Client(\|SDK" .claude/agents/

# 3. Every path/file an agent is told to read — does each still exist?
grep -roh "[a-z_/]*\.\(py\|ts\|md\|toml\)" .claude/agents/ | sort -u | while read f; do
  [ -e "$f" ] || echo "MISSING: $f"
done

# 4. Contradictions with the top-level instructions file
#    (read CLAUDE.md's prohibitions, then grep the agent files for each)
```

Then make it recurring: **when a decision record removes a dependency, deletes a route, or changes a
command, grep `.claude/` in the same change.** Add it to the docs agent's checklist so it happens at
the end of every feature.

### Verify the fix

Every command in every agent file should be one you have personally executed successfully in that
environment. If you haven't run it, assume it's broken.

---

## 5. Mutation-testing hygiene for merge-blocking gates

Applies to any gate agent that proves findings by **editing source and re-running the suite**
(reviewer, security, domain-quality gates).

### 5a. Stale bytecode produces false GREENs

**Symptom.** A mutation "survives" — the gate concludes a guard is missing — but the mutant never
executed.

**Root cause.** Bytecode caches validate on **mtime + size**. Two single-token mutations of equal
byte length (`default=32` → `default=16`, `True` → `True`) leave size unchanged, and coarse
filesystem mtime granularity (notably on Windows bind mounts) lets the interpreter reuse the cached
bytecode of the *restored* source. The mutant looks like a survivor without ever running.

**Fix.** Clear the cache in the verification script itself (`find . -name __pycache__ -prune -exec
rm -rf {} +`) and disable writing (`PYTHONDONTWRITEBYTECODE=1`) — note the env var stops *writing*,
not *reading*, so clearing is the load-bearing half. Warn explicitly in the gate agent files that
**single-token numeric/boolean mutations are especially suspect**. Equivalent traps exist in any
ecosystem with a build cache — check yours.

### 5b. Concurrent mutation gates corrupt each other

**Symptom.** A gate reports a survivor that cannot be reproduced.

**Root cause.** Two mutation-testing agents on the **same working tree**: each reads the other's
edits as its own, and a mutation reverted by one mid-run looks like a survivor to the other.

**Fix.** State in every gate agent file: **run mutation-testing gates sequentially, never
concurrently.** Parallel *producers* are fine — but only with exclusive file ownership. If you want
real parallelism for gates, give each agent its own worktree.

### 5c. Always restore, and prove it

Require each gate agent to restore every mutation and **confirm `git status` is clean in its
report**. A mutation left behind becomes the next agent's phantom bug.

---

## 6. File-ownership boundaries create orphaned breakage

### Symptom

A tester is scoped to specific files. It notices that a *different*, out-of-scope test contradicts
the new spec — and correctly leaves it alone. That test then fails during implementation, landing on
a coder who is **forbidden to edit tests**. The coder either stalls or violates the rule.

### Root cause

Scoping rules were written for *write-conflict* avoidance (real, necessary for parallel agents) but
silently doubled as *responsibility* boundaries. Nobody owned the contradiction.

### Fix

Add to the tester agent definition:

```markdown
If an EXISTING test elsewhere contradicts the spec you are pinning (e.g. it asserts a
table must NOT exist and the decision record now requires it), do NOT leave it for the
coder — a coder editing a test to go green is forbidden. Fix it in the RED commit and
say in your report which assertion you changed and which decision record authorizes it.
```

The general principle: **whoever creates a contradiction resolves it, in the same commit.** Scoping
prevents write conflicts; it does not transfer responsibility.

### Related: make authorized test changes legible

Removing a "this must not exist" assertion looks exactly like weakening a test to force a pass — the
single most suspicious pattern in a TDD repo. When a decision record genuinely reverses a prior
constraint, **say so in the commit message and in a comment at the assertion**, citing the record.
Reviewers should never have to reconstruct that from context.

---

## 7. Coordinator discipline

Harness rules that are about *your* behavior, not the agents':

- **Re-run the gate yourself before committing subagent work.** Cheap; already caught a real defect.
- **Read the diff, don't just read the report.** Both defects found in the observed session (the
  missing `DEFAULT`, a missing foreign key) were visible in a 50-line diff and invisible in the
  summary.
- **Check your prompt before blaming the agent.** If the report is thin, the instruction usually
  was too.
- **Prefer fixing the agent definition over re-explaining in the prompt.** A prompt fix helps one
  run; a definition fix helps every future run. If you find yourself pasting the same instruction
  twice, it belongs in `.claude/agents/`.
- **Record decisions in the repo, not just in conversation.** Ratified choices that live only in
  chat history are lost at session end. Write them into the decision record *before* building on
  them.
- **Verify state against the remote before trusting a handoff document.** Local branches and
  handoff notes lag; `git fetch` + a PR status check costs seconds.

---

## Porting checklist

Day one in the new project:

- [ ] Identify the real gate definition (Makefile / CI workflow) and confirm what CI actually runs
- [ ] Determine whether that command runs natively in the agent's environment — **if not, §1 applies**
- [ ] Write `scripts/verify.sh` (or equivalent) that **invokes the real target**, never re-lists it
- [ ] Run every mode of it on a clean main-branch checkout; fix until green
- [ ] Delete every *other* verification command from `.claude/agents/` and `CLAUDE.md`
- [ ] Add the path → required-mode table (§2) to `CLAUDE.md` and each producer agent
- [ ] Add the evidence contract (§3) to every producer agent
- [ ] Add the coordinator trust rule (§3) to `CLAUDE.md`
- [ ] Run the four stale-instruction audits (§4) and fix everything they surface
- [ ] Add mutation hygiene (§5) to every merge-blocking gate agent
- [ ] Add the contradiction-ownership rule (§6) to the tester agent
- [ ] Verify the whole thing by running one small real feature end to end through the pipeline

The last item matters most. **These fixes are themselves unverified until an actual feature has
passed through them** — which is precisely the lesson the rest of this document is about.
