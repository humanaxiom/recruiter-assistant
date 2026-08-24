# Session Handoff — recruiter-assistant

**Hard cap: 150 lines.** If you are about to exceed it, delete something instead.
The 3,251-line predecessor is at
[docs/archive/HANDOFF-2026-07-10-to-08-23.md](docs/archive/HANDOFF-2026-07-10-to-08-23.md) —
it is history, not required reading.

---

## The one objective

> **A real recruiter, signed in as themselves, ranks a real requisition against
> real applicants and says whether the shortlist is sensible.**

Nothing that does not move that forward gets built. Not a test, not an ADR, not
a roadmap entry. See [docs/RESET.md](docs/RESET.md) for why this replaced the
previous plan.

## Do this first

`.env` is stale and **CAS login breaks on the first click**: the frontend builds
its login link as `{CAS_SERVICE_BASE_URL}/auth/cas/login`
(`core/frontend/app.py:156`) and that value is still `http://localhost:8000` —
the container-internal port. The API is published on **29800**. Add these four
lines to `.env` (they are already correct in `.env.example`):

```
CAS_ENABLED=true
CAS_SERVICE_BASE_URL=http://localhost:29800
CAS_FRONTEND_BASE_URL=http://localhost:29500
LLM_TIMEOUT_S=900
```

Then `docker compose up -d --force-recreate api worker frontend`, sign in at
`localhost:29500`, and walk it as recruiter, hiring manager and auditor.

`LLM_TIMEOUT_S` matters independently — the running stack has 900 only as a
SHELL OVERRIDE that will not survive a plain `docker compose up`. `auth_enabled`
is already true, so this will not trip the "CAS on with zero role keys" boot
refusal. Unknown until tried: SFU CAS may reject a `localhost` service URL.

## Then

1. **Load one real requisition and its real applicants.** Not fixtures. Read the
   shortlist and judge whether the order is defensible. No test can answer this.
2. **Put it in front of one recruiter.** Their confusion is the backlog.
3. **Fix only what that session surfaced.** One week, hard stop.

Three small items survive the cut because running the product found them:
`pg.jobs_stuck` in `core/src/doctor.py` (~1h — the reason 20 dead jobs reached a
human instead of the tooling); job `306c573c` fails extraction (~2h — longest
real posting, 9,523 chars, `llm output invalid: title: missing`, measure before
guessing); the GitHub Support PII purge (~15m, below).

## State

| | |
|---|---|
| `main` | `b9859df` — clean, all gates green |
| Gates | 5,545 unit · 541 integration · 93.02% coverage |
| Live data | 24 jobs parsed · 1 failed (`306c573c`) |
| Stack | `:29500` UI · `:29800` API · `:29432` pg · `:29474/:29687` neo4j |
| Postgres | `psql -U app -d recruiter` — there is no `postgres` role |
| LLM | gb10 `100.88.247.106` · spark1 `100.114.185.88` — **both shared** |

Shipped and working: upload → parse → rank → shortlist (smoke: 10 passed on real
résumés); JD ingest; blind review, PII encryption, audited reveal; authorization,
CSRF, audit viewer (built, never exercised by a signed-in human).

## Environment facts worth not rediscovering

- **Never diagnose the model on a contended peer.** Check `GET /api/ps` first —
  and again *during* a long run. Both attempts to measure ADR-045's transport gap
  were destroyed mid-flight: a foreign 70GB `gpt-oss:120b` landed on gb10 after
  it was verified idle, and the re-run's host rebooted. A contended box once
  produced a confidently wrong retraction of a correct fix.
- **The token floor is per-PROMPT, not per-model.** `REASONING_JSON_MIN_TOKENS =
  8192` is right for résumé/JD extraction and wrong as a universal. The
  `skills_graph` tiebreaker at `max_tokens=128` gives *identical* answers to 8192.
  Do not "fix" a small budget on sight — measure it.
- **Never `git add -A`.** Explicit pathspecs only. One `git add -A` swept 99 MiB
  of real candidate résumés onto a public repo.
- There is no usable Python on this host. Use `./scripts/verify.sh`.
- `quickstart.ps1` needs `pwsh` 7 — it fails to parse under PowerShell 5.1.

## PII — contained, one deferred human step

`fixtures/` (117 files, 99 MiB of real résumés) was pushed to a PUBLIC repo.
Done: branch ref deleted, history rewritten, `.gitignore` guard committed.
**Still open, deferred by the owner:** deleting the branch did NOT stop GitHub
serving the data — tested, not assumed:
`gh api .../contents/fixtures?ref=349aadd...` still returned the tree. Only a
GitHub Support purge removes it. Both `humanaxiom/` and `sfu-aria/` are PUBLIC.

`fixtures/` is untracked by design — provision it out-of-band. Both harnesses
hard-fail rather than skip without it, so an unprovisioned clone cannot report a
green run that tested nothing.

## Open decisions nobody should re-litigate

- **ADR-045's transport gap** — the harness measures schema-constrained decoding
  on Ollama's native route; the app runs unconstrained `json_object` on
  openai-compat. Direction chosen (give the app schema-constrained decoding,
  portable to vLLM `guided_json`); **no measurement exists to support it.** Do
  not treat the decision as data. Not pilot-blocking.
- **Competency scoring** — deferred, owner: corpus owner + HR, *with pilot data*.
  It needs the pilot; the pilot does not need it.
