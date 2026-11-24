# Fresh install on a new box

**Written 2026-11-24** for moving recruiter-assistant to a new account and a new box, possibly with a
different LLM host. It starts from an empty machine and ends with a stack you can hand to users.
Read [HANDOFF.md](../../HANDOFF.md) and [OPERATOR-NOTES](../handover/OPERATOR-NOTES.md) first; this
file is only the build.

**Nothing in this guide copies data from the old box.** The new box starts empty. Migrating pilot
data is a separate, deliberate decision (§9).

## 0. What you need before you start

| Need | Notes |
|---|---|
| A Windows box with **Docker Desktop**, **Git**, **GitHub CLI** (`gh`) and **PowerShell 7** (`pwsh`) | `scripts/quickstart.ps1` fails to *parse* under Windows PowerShell 5.1, and still exits 0 (OPERATOR-NOTES §5). Linux works too, but the scripts were run on Windows + Git Bash. |
| Access to the repo, `humanaxiom/recruiter-assistant` | Or the offline git bundle (§1b). |
| **An LLM host** serving an OpenAI-compatible API that this box can reach | Today that is Ollama on the lab GPU box over Tailscale: `gpt-oss:20b` for generation and `nomic-embed-text` (768-dim) for embeddings. If the new box uses a different host, see §4. |
| **A Tailscale join**, if the LLM host is on the tailnet | Inference must stay on the tailnet; no cloud inference, ever (CLAUDE.md). |
| **`fixtures/`**: real résumés and JDs, about 100 MB, **untracked by git** | `smoke.sh` and `model-check.sh` need them. **They contain real candidate PII.** Copy them only through a channel the product owner approves, and never commit them (§7). |
| For a public URL: DNS, a router NAT rule, a TLS cert, and possibly SFU CAS registration of the new service URL | §6 |

**Ports.** The live stack publishes **29xxx** host ports and the stress stack **28xxx**. Keep them
unique on the new box too, and check `docker ps` for collisions first.

## 1. Get the code

### 1a. From GitHub (normal)

```bash
git clone https://github.com/humanaxiom/recruiter-assistant.git C:/repos/recruiter-assistant
cd C:/repos/recruiter-assistant
git checkout main
```

### 1b. From the offline bundle (no GitHub access)

The release folder holds `recruiter-assistant-<sha>.bundle` and `SHA256SUMS` (see
[§10](#10-the-release-bundle)).

```bash
sha256sum -c SHA256SUMS
git clone recruiter-assistant-<sha>.bundle C:/repos/recruiter-assistant
cd C:/repos/recruiter-assistant && git checkout main
git remote set-url origin https://github.com/humanaxiom/recruiter-assistant.git   # when access exists
```

**The live tree is what the stack serves**, because `./core` is bind-mounted. Do development in
**separate `git worktree`s**, never in this checkout (OPERATOR-NOTES §3 and §6).

## 2. Configure `.env`

```bash
cp .env.example .env
```

Then edit these keys. Leave everything else at the `.env.example` values unless you know why.

| Key | Set to | Why |
|---|---|---|
| `LLM_BASE_URL` | the new LLM host, e.g. `http://<host>:11434/v1` | §4 |
| `LLM_TIMEOUT_S` | `900` for `gpt-oss:20b` on a shared GPU | It is coupled to throughput. Re-measure on a new host (§4). |
| `LLM_MODEL_GENERATION` / `LLM_MODEL_EMBEDDING` | the model names on that host | Changing the embedding model also means changing `LLM_EMBEDDING_DIM` **and** rebuilding the vector indexes. Avoid it unless you have to. |
| `CAS_ENABLED` | `true` | **On by default and on for any box with users.** It must be a real boolean; an empty value stops the containers. |
| `CAS_SERVICE_BASE_URL` | the API's external URL, e.g. `https://<host>:<port>` | CAS validates against it |
| `CAS_FRONTEND_BASE_URL` | the frontend's external URL | Where the browser lands after login |
| `DEFAULT_ADMIN_CAS_USERNAME` | the SFU computing ID of the first admin | That account becomes admin on its first login (ADR-019 §10a). Everyone else lands as pending until an admin assigns a role. |
| `TRUST_PROXY_HEADERS` | `true` **only** behind a proxy you control that *sets* the forwarded headers (nginx) | Otherwise leave it `false` |
| `SESSION_COOKIE_SECURE` | `true` when served over HTTPS | It isn't in `.env.example`; add it |
| `TALEO_ENABLED` | `false` until ADR-046's obligations are met on this box | This is the only egress carve-out: a firewall rule enumerating `tre.tbe.taleo.net:443`, sign-offs, and an owner. The worker now reads `.env` too (#115). |

**Do not hand-write the secrets.** `quickstart.ps1` generates `PII_KEY`, `SKILL_HASH_SALT`,
`FLASK_SECRET_KEY` and the four `API_KEY_*` values. It also replaces any published default it finds.
**Back up `PII_KEY` somewhere safe and offline.** If it is lost, every encrypted candidate field is
unrecoverable.

## 3. First boot

```powershell
pwsh -ExecutionPolicy Bypass -File ./scripts/quickstart.ps1 -Build
```

The script:
1. generates the secrets;
2. writes the 29xxx port block;
3. checks that every port is free;
4. **checks that both models exist at `LLM_BASE_URL`**;
5. builds and starts the stack.

Postgres tables and the Neo4j indexes are created when the API starts; there is no migration step.
**Read the output, not the exit code.** Then:

```bash
docker compose ps                      # six services, the datastores healthy
./scripts/doctor.sh                    # must print ✅ healthy
```

## 4. A new LLM host or model: measure before trusting

Every token budget, timeout and concurrency number in this repo was measured on `gpt-oss:20b` on a
shared GPU. Point the stack at a different host or model and they are all wrong at once, with no
signal. So, **before users touch it**:

1. Check the host is idle: `curl http://<host>:11434/api/ps`. Never measure on a contended GPU.
2. `./scripts/model-check.sh`. It builds the real prompts from `fixtures/`, runs them at the worker's
   own concurrency, finds the smallest budget that gives valid JSON, and writes
   `docs/model-profiles/<model>.json`.
3. **Commit that profile** on a `chore/` branch and open a PR. `doctor.sh` fails when the configured
   model has no profile, or when `LLM_TIMEOUT_S` is below the measured latency.
4. Set `LLM_TIMEOUT_S` to the value the profile calls for.

Same model on a faster host: the budgets hold, but re-run `model-check.sh` anyway so the timeout
matches. The **ADA6000 boxes running vLLM** are the planned production target. vLLM is
OpenAI-compatible, but ADR-045 records an unmeasured transport gap (schema-constrained decoding), so
treat it as a new model.

## 5. Prove it works: the hand-over rule applies to a new box too

`smoke.sh` needs **CAS off**, so it never runs against the CAS-on live stack. Run it on an
**isolated stress stack on the same box** (28xxx, named volume, CAS off). That stack needs its own
checkout with its own `.env` (CAS off, fresh secrets):

```bash
git worktree add C:/repos/ra-stress main
# Create .env there by hand, NOT by quickstart. It needs CAS_ENABLED=false, fresh PII_KEY,
# SKILL_HASH_SALT and FLASK_SECRET_KEY, and NO *_PORT lines: docker-compose.stress.yml defaults
# them to 28xxx, but a 29xxx value in .env would win and collide with the live stack.
cd C:/repos/ra-stress
docker compose -p recruiter-stress -f docker-compose.yml -f docker-compose.stress.yml up -d --build
SMOKE_NETWORK=recruiter-stress_default SMOKE_IMAGE=recruiter-stress-api ./scripts/smoke.sh   # expect 10 passed
```

Add `--profile stub` and `LLM_BASE_URL=http://llmstub:8000/v1` to test without the GPU. A
real-model run is the stronger check once the host is idle.

Then, on the **live** stack (CAS on):
1. `./scripts/doctor.sh` must print healthy.
2. **A by-hand drive signed in as a real person, from off the LAN:**
   1. The first admin signs in and lands as admin.
   2. A second colleague signs in and lands on pending access.
   3. The admin creates a job, opens it once the parse finishes, and uploads one résumé. Upload only
      shows on an OPEN job.
   4. Generate the shortlist, record a decision, and generate interview questions.
3. If Taleo is enabled: press "Import from Taleo" as admin and confirm the postings appear.

**If any step can't be done, say so in the hand-over.** "Could not check" is never a pass.

## 6. Public URL (optional)

[docs/deploy/sfuai-ca.md](sfuai-ca.md) is the exact runbook for the current box: nginx
TLS-termination config, compose ports, `.env` keys, cutover and revert. For a new host or name:

- **DNS:** point the hostname at the new public IP.
- **Router:** forward the public port to the new box. Check that the target LAN IP is the box's
  **current** address; a Wi-Fi/Ethernet switch changes it.
- **TLS cert:** issue one for the hostname.
- **CAS:** update `CAS_SERVICE_BASE_URL` and `CAS_FRONTEND_BASE_URL`.
- **SFU CAS registration:** if CAS rejects the service URL (an error page instead of a login form),
  SFU IT must register `https://<host>[:port]/auth/cas/validate`.
- **Proxy cookies:** `TRUST_PROXY_HEADERS=true` and `SESSION_COOKIE_SECURE=true` behind nginx.

**Never turn CAS off on a box reachable from the internet.**

## 7. Fixtures and other untracked material

- **`fixtures/`** (real résumés and JDs) is needed by `smoke.sh`, `model-check.sh`, `e2e.sh` and the
  eval harnesses. These fail rather than skip without it. It is real PII, so transfer it only as the
  product owner approves. **Never `git add -A`**: that is how it once reached a public repo, and a
  GitHub Support purge is still open (HANDOFF §6).
- The old box's `data/` also holds HR source material (`DTO Package.zip`, `HR Hriing Tool`). It isn't
  needed to run the app.

## 8. Day-2 operations

- **Deploy a newer `main`:** see OPERATOR-NOTES §3. The tree-identical-commit recipe applies if the
  live checkout sits on a non-main branch. On a fresh clone that's simply `main`, so use
  `git pull --ff-only` and then `docker compose up -d --build api worker frontend`.
- **Gates** (before every commit): `./scripts/verify.sh` (about 39 min), or `./scripts/verify.sh all`
  for `models/ api/ services/ worker/`.
- **Logging everyone out:** rotating `FLASK_SECRET_KEY` does it; tell users first.

## 9. Migrating pilot data instead of starting empty (not recommended by default)

The old box was wiped to a clean slate on 2026-11-24, so it holds 2 accounts and recently imported
Taleo drafts, with nothing hard to recreate. If migration is wanted anyway, it needs all four of these
together, or the result is unreadable:

1. a Postgres dump;
2. the `data/` blob store;
3. the Neo4j volume, or a re-projection;
4. **the same `PII_KEY` and `SKILL_HASH_SALT`**.

That moves real candidate data between machines, so it is the product owner's decision.

## 10. The release bundle

Each release folder (`recruiter-assistant-release-<date>/`) contains:

| File | What it is |
|---|---|
| `recruiter-assistant-<sha>.bundle` | `git bundle` of `main` (full history) |
| `SHA256SUMS` | checksums for the files in the folder |
| `FRESH-INSTALL.md` | this guide |
| `RELEASE-NOTES.md` | what's in this cut and what's open |

It deliberately contains **no `.env`, no secrets, no `fixtures/` and no data.**
