# Session Handoff — recruiter-assistant

**Eight items. Hard cap.** A ninth means one of these is no longer relevant —
delete it. History goes to [docs/archive/](docs/archive/), never inline. Plan of
record: [docs/ROADMAP.md](docs/ROADMAP.md).

---

### 1. The objective — it changed on 2026-08-27

> **The four people on the pilot box can do their real hiring work in it, and
> what they hit gets fixed before anything they haven't hit gets built.**

The product was demoed to the CIO and HR. Both approved. It is deployed on a
**dedicated box and four people are using it.** The previous objective — "get a
user" — is met, and [docs/RESET.md is archived](docs/archive/RESET-2026-08-24.md)
because of it.

**What this does and does not license.** The build phase is open again: new
features are wanted. It does not license going back to inspecting the code the
repo already has. **User-sourced work outranks self-sourced work** — that is the
whole of `CLAUDE.md` §Economy, and it is the only thing that has ever stopped
this repository gold-plating itself.

### 2. Do this first — the pilot box is not this box

Everything in `docs/ROADMAP.md` §"Where things stand" describes the *product*.
Nobody has written down the *deployment*: its address, whose `.env` it runs, when
it was last updated from `main`, or whether it has ever had `doctor.sh` run
against it. **Find that out and record it here**, replacing this paragraph.

Two things to check the moment you have access:

- **`FLASK_SECRET_KEY: dev-only-change-me` is committed** in
  [docker-compose.yml:150](docker-compose.yml#L150) and
  [compose.cas.yml:31](compose.cas.yml#L31). If the pilot box booted from either,
  four real sessions are signed with a published key and any of them is forgeable.
  Highest-severity open item in the repo — ROADMAP open item 1.
- **`./scripts/doctor.sh` against the pilot box.** It exits non-zero on a failed
  invariant *or on a datastore it could not reach* — "could not check" is never a
  clean bill of health. It is the only tool that sees defects living in state
  rather than in code, and this repo has shipped four of those.

The local dev stack is still up on `:29500` UI · `:29800` API · `:29432` pg ·
`:29474`/`:29687` neo4j. If CAS login is needed here, `.env` still carries the
stale `CAS_SERVICE_BASE_URL=http://localhost:8000`; the correct values are in
`.env.example` (`:29800` API, `:29500` frontend, `LLM_TIMEOUT_S=900`).

### 3. Then, in order

1. **Open a channel from the four users back into this repo.** There isn't one.
   Every pilot defect so far arrived by someone mentioning it in conversation, and
   each was worth more than a week of inspection. A dated `docs/pilot-feedback.md`
   is enough to start. ROADMAP open item 2.
2. **Close ROADMAP open item 1** — the committed secret, `pg.jobs_stuck` in
   `doctor.py`, retention enforcement. A live deployment demands these; a dev
   stack did not.
3. **Then pick a feature.** Three cards are framed and **none is committed**:
   "Why this rank?" slice 2 (lowest risk, extends what HR just saw), "Ask the
   pool" NL search (highest demo impact), Policy Studio (answers "who owns this
   decision?"). Let the feedback channel pick.

### 4. Current state

| | |
|---|---|
| `main` | `ec2f2d2` |
| Gates, last recorded | 5,545 unit · 541 integration · 93.02% coverage — measured at `b9859df`; **re-run, do not cite** |
| Verification | `verify.sh` code · `smoke.sh` screen · `doctor.sh` data · `model-check.sh` before a model swap |
| Postgres | `psql -U app -d recruiter` — there is no `postgres` role |
| LLM hosts | gb10 `100.88.247.106` · spark1 `100.114.185.88` — **both shared** |

Working end to end: upload → parse → rank → shortlist; JD ingest; blind review,
PII encryption, audited reveal; CAS identity, session role enforcement, CSRF,
auditor viewer.

### 5. Never diagnose the model on a contended peer

Check `GET /api/ps` first — **and again during a long run**. Both attempts to
measure ADR-045's transport gap were destroyed mid-flight: a foreign 70GB
`gpt-oss:120b` landed on gb10 after it was verified idle, and the re-run's host
rebooted. A contended box once produced a confidently wrong retraction of a
correct fix.

Related and equally load-bearing: **the token floor is per-PROMPT, not
per-model.** `REASONING_JSON_MIN_TOKENS = 8192` is right for résumé/JD extraction
and wrong as a universal — the `skills_graph` tiebreaker at `max_tokens=128` gives
*identical* answers to 8192, measured. Do not "fix" a small budget on sight, and
do not inherit a literal into a new call site; measure it.

### 6. PII — one deferred step, now overdue

`fixtures/` (117 files, 99 MiB of real résumés) was pushed to a PUBLIC repo by a
`git add -A`. Done: branch ref deleted, history rewritten, `.gitignore` guard
committed. **Still open:** deleting the branch did *not* stop GitHub serving the
data — tested, not assumed. Only a **GitHub Support purge** removes it. Both
`humanaxiom/` and `sfu-aria/` are PUBLIC. ~15 minutes of someone's time, open
since 2026-08-21, and the only item in this repo with a live external exposure.

**Never `git add -A` in this repo.** `fixtures/` is untracked by design —
provision it out-of-band. Both harnesses hard-fail rather than skip without it,
so an unprovisioned clone cannot report a green run that tested nothing.

### 7. Settled — do not re-litigate

- **ADR-045's transport gap.** Direction chosen (give the app schema-constrained
  decoding, portable to vLLM `guided_json`); **no measurement exists to support
  it.** Do not treat the decision as data.
- **D1 and D2 are answered and shipped** (audited reveal of withdrawal reasons;
  every keyed read needs a real principal). `docs/OPEN_DECISIONS.md` is a record,
  not a question.
- **The A7 defect taxonomy is closed at 21 instances and is not to be extended.**
  The pattern is real; naming it turned finding more instances into a work
  generator. What survives is in ROADMAP §"What the gates cannot see".
- **Competency scoring is no longer deferred by precondition.** It was blocked on
  *"corpus owner + HR, with pilot data"* — there is now pilot data. ROADMAP open
  item 3.

### 8. Host quirks

No usable Python on this host (only the WindowsApps stub) — use
`./scripts/verify.sh`, never a hand-written `docker run`. `quickstart.ps1` needs
`pwsh` 7; it fails to *parse* under PowerShell 5.1, and `powershell.exe` still
exits 0. Publish unique host ports (29xxx) — many other containers on this
machine collide on stock 5432/8000/5000. Two Claude sessions drive this repo at
once: re-read git and PR state in the same call that commits, pushes, or merges.
