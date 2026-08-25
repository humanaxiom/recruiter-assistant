# Session Handoff — recruiter-assistant

**Eight items. Hard cap.** A ninth means one of these is no longer relevant —
delete it. The 3,251-line predecessor is archived at
[docs/archive/HANDOFF-2026-07-10-to-08-23.md](docs/archive/HANDOFF-2026-07-10-to-08-23.md);
it is history, not required reading. Plan of record: [docs/RESET.md](docs/RESET.md).
Harness lessons for the seed maintainer:
[docs/process/HARNESS_FIELD_NOTES.md](docs/process/HARNESS_FIELD_NOTES.md).

---

### 1. The objective

> **A real recruiter, signed in as themselves, ranks a real requisition against
> real applicants and says whether the shortlist is sensible.**

Nothing that does not move that forward gets built. Not a test, not an ADR, not a
roadmap entry. The product has been feature-complete since 2026-07-17; what it
has never had is a user.

### 2. Do this first — CAS login is broken by four stale `.env` values

The frontend builds its login link as `{CAS_SERVICE_BASE_URL}/auth/cas/login`
(`core/frontend/app.py:156`), and that value is still `http://localhost:8000` —
a container-internal port nothing serves on the host. The API is published on
**29800**. Login fails on the first click. Correct values are already in
`.env.example`; only the live file is stale:

```
CAS_ENABLED=true
CAS_SERVICE_BASE_URL=http://localhost:29800
CAS_FRONTEND_BASE_URL=http://localhost:29500
LLM_TIMEOUT_S=900
```

Then `docker compose up -d --force-recreate api worker frontend` and sign in at
`localhost:29500`. `LLM_TIMEOUT_S` matters independently — the running stack has
900 only as a shell override that will not survive a plain `docker compose up`.
`auth_enabled` is already true, so this will not trip the "CAS on with zero role
keys" boot refusal. Unknown until tried: SFU CAS may reject a `localhost`
service URL.

### 3. Then, in order

1. **Walk the product signed in** — recruiter, hiring manager, auditor. Highest-
   information hour available.
2. **Load one real requisition and its real applicants.** Not fixtures. Read the
   shortlist and judge whether the order is defensible. No test can answer this.
3. **Put it in front of one recruiter, watched.** Their confusion is the backlog.
4. **Fix only what that session surfaced.** One week, hard stop.

### 4. Current state

| | |
|---|---|
| `main` | `b9859df` — clean, all gates green |
| Gates | 5,545 unit · 541 integration · 93.02% coverage |
| Live data | 24 jobs parsed · 1 failed (`306c573c`) |
| Stack | `:29500` UI · `:29800` API · `:29432` pg · `:29474`/`:29687` neo4j |
| Postgres | `psql -U app -d recruiter` — there is no `postgres` role |
| LLM hosts | gb10 `100.88.247.106` · spark1 `100.114.185.88` — **both shared** |

Working: upload → parse → rank → shortlist (smoke: 10 passed on real résumés);
JD ingest; blind review, PII encryption, audited reveal. Built but never
exercised by a signed-in human: authorization, CSRF, audit viewer.

### 5. The three small items that survive the cut

All found by *running* the product, not inspecting it.

| Item | Why | Size |
|---|---|---|
| `pg.jobs_stuck` in `core/src/doctor.py` | Why 20 dead jobs reached a human instead of the tooling | ~1h |
| Job `306c573c` fails extraction | Longest real posting (9,523 chars), `llm output invalid: title: missing`. Measure before guessing — `temperature=0`, so a retry reproduces it | ~2h |
| GitHub Support PII purge | Real candidate résumés still fetchable by SHA on a public repo | ~15m |

### 6. Never diagnose the model on a contended peer

Check `GET /api/ps` first — **and again during a long run**. Both attempts to
measure ADR-045's transport gap were destroyed mid-flight: a foreign 70GB
`gpt-oss:120b` landed on gb10 after it was verified idle, and the re-run's host
rebooted. A contended box once produced a confidently wrong retraction of a
correct fix.

Related and equally load-bearing: **the token floor is per-PROMPT, not
per-model.** `REASONING_JSON_MIN_TOKENS = 8192` is right for résumé/JD
extraction and wrong as a universal — the `skills_graph` tiebreaker at
`max_tokens=128` gives *identical* answers to 8192. Do not "fix" a small budget
on sight; measure it.

### 7. PII — contained, one deferred human step

`fixtures/` (117 files, 99 MiB of real résumés) was pushed to a PUBLIC repo by a
`git add -A`. Done: branch ref deleted, history rewritten, `.gitignore` guard
committed. **Still open:** deleting the branch did *not* stop GitHub serving the
data — tested, not assumed (`gh api .../contents/fixtures?ref=349aadd...` still
returned the tree). Only a GitHub Support purge removes it. Both `humanaxiom/`
and `sfu-aria/` are PUBLIC.

**Never `git add -A` in this repo.** `fixtures/` is untracked by design —
provision it out-of-band. Both harnesses hard-fail rather than skip without it,
so an unprovisioned clone cannot report a green run that tested nothing.

### 8. Settled — do not re-litigate

- **ADR-045's transport gap.** Direction chosen (give the app schema-constrained
  decoding, portable to vLLM `guided_json`); **no measurement exists to support
  it.** Do not treat the decision as data. Not pilot-blocking.
- **Competency scoring.** Deferred, owner: corpus owner + HR, *with pilot data*.
  It needs the pilot; the pilot does not need it.
- **Host quirks.** No usable Python here — use `./scripts/verify.sh`.
  `quickstart.ps1` needs `pwsh` 7; it fails to parse under PowerShell 5.1.
