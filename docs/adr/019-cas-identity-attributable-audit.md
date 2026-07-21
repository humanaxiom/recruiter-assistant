# ADR-019: CAS identity, user records, and attributable audit (FU-5)

**Status:** Accepted (closes ADR-018's actor-attribution residual and its auditor-has-no-capability residual partially; makes ADR-016's R3 actionable; separates service-to-service identity from browser-user identity; adds SFU-hosted CAS as an internal runtime dependency outside the compose stack — fail-closed on outage, no effect on local model inference)
**Date:** 2026-07-20

## Context

ADR-018 shipped RBAC via keyed roles — `admin`, `recruiter`, `hiring_manager`, `auditor` — but the *actor* recorded in audit rows remains unresolved. `reveal_audit.actor` and `created_by`/`uploaded_by` are populated by the optional, unverified `X-Actor-Name` header (`core/src/api/deps.py:131-141`) or default to `"api"`. Worse: the Flask frontend never sends `X-Actor-Name` and presents one shared recruiter key for every browser (`core/frontend/api_client.py:101-109`), so every UI-driven reveal records `actor = "api"` — an auditor reviewing `reveal_audit` rows cannot name a person (ADR-018:284-287 recorded this).

More broadly: there is no `users` table anywhere in the schema (`core/src/models/ddl.py:11-12` says "there is no auth table in v1"). Identity today is wholly derived from which API key was presented. This means:

1. **No per-user audit trail.** An `auditor` role key that is leaked, shared, or rotated creates no audit distinction. A recruiter forwarding their own key to a colleague has no record.
2. **No role provisioning.** Roles are hardcoded across at least six coupled places — `deps.py:50-61` (the enum), `deps.py:64-70` (the key tuple), `settings.py:78-81` (the field declarations), `settings.py:207-212` (the env-var name list), `settings.py:195-202` (the `auth_enabled` logic), `settings.py:243-251` (the collision-check `zip(..., strict=True)`), plus multiple deploy environment files. Adding a role requires editing multiple locations across settings and environment configuration. Roles cannot be configured or revoked at runtime.
3. **No per-user last-seen tracking or session history.** A login session is not a data structure; a key is simply presented and honored.
4. **Expand audit beyond reveals.** ADR-018 §7 deferred adding an audit row when `PATCH /jobs/{id}` flips `blind_review`, a wider blast radius than the reveal audited in ADR-016. A generalized audit table, keyed to a `users` row, is the foundation for recording any future action.

**Prior decision — CAS tables deliberately cut.** The project was extracted from a larger HRIS system; `core/src/models/ddl.py:6` documents that CAS authentication tables were deliberately excluded from the v1 schema. That decision held true while identity was modeled purely through API key presentation. ADR-019 reverses it: the driver is FU-4's RBAC (ADR-018), which shipped roles but left the *actor* unresolved. Attributable reveal — auditing which human *person* accessed which candidate — requires bridging that gap with real identity. CAS, as the institution's SSO, becomes necessary.

This feature — FU-5 — establishes that foundation: a `users` table with institutional identity via CAS (Central Authentication Service, the institution's SSO), and a generalized `audit_log` table replacing the reveal-only `reveal_audit`.

## Decision

### 1. A `users` table — real identity entity

The schema gains an append-mostly `users` table (rows are created, updated infrequently, never deleted):

```
id              UUID PRIMARY KEY
cas_username    TEXT NOT NULL UNIQUE
display_name    TEXT  -- may be empty if CAS does not release it
email           TEXT  -- may be empty if CAS does not release it
role            TEXT NOT NULL  -- one of (admin, recruiter, hiring_manager, auditor)
active          BOOLEAN NOT NULL DEFAULT true
created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now()  -- refreshed on every login
```

Each row represents a real person with institutional credentials. `cas_username` is the unique, immutable identity from CAS (typically a NetID or SSO username); `display_name` and `email` are attributes released by the institution's CAS server (which vary by institution — some release neither, some release both, some release only email). `active` allows deprovisioning without deletion (historical audit rows remain valid). `role` is now a *column*, not encoded in which key was presented, so a role change is a data change.

### 2. Authentication via CAS — the flow

The Flask frontend becomes the authentication gateway. A recruiter accessing `GET /` or any `/resumes/...` route without an existing session is redirected to the CAS login server (`settings.cas_login_url`), which prompts for credentials, then redirects back to Flask's `/login/callback` with a ticket. Flask:

1. Contacts CAS's `/serviceValidate` endpoint (sync httpx call) with the ticket and the expected `settings.cas_service_url` (this Flask app's public URL).
2. Parses the response to extract `cas_username`, `display_name`, `email`.
3. Upserts a `users` row: if a row with matching `cas_username` exists, refreshes `display_name`/`email` (in case the institution's attribute release changed) and `last_seen_at`; if not, creates a new row with `role=recruiter` by default.
4. Establishes a Flask session bound to the `user_id`.
5. Redirects to the original request URI.

On every subsequent Flask route, Flask's session middleware ensures a valid session exists; an expired session returns the recruiter to CAS login.

**Both `settings.cas_login_url` and `settings.cas_service_url` MUST be settings-driven** (`core/src/settings.py`, pydantic-settings), never hardcoded. `cas_login_url` is institution-specific and must not be baked into the binary; `cas_service_url` is the public URL of *this* deployment (localhost in dev, a real domain in prod) and must be configurable per environment.

### 3. Internal infrastructure dependency — CAS outage, fail-closed

**This is not a deviation from offline-first.** CLAUDE.md's constraint is "NEVER add cloud API calls", and CAS does not add one: it is SFU's own internally-hosted authentication service, running on institutional infrastructure on the same network, with no third-party involvement and no dependency on outbound internet connectivity. It is closer in kind to Postgres or Neo4j — infrastructure the deployment relies on — than to a vendor API. Nothing about this ADR sends data to an external provider, and model inference remains entirely local.

What it *does* add is a **new runtime dependency outside the compose stack**, and therefore a new failure mode: unlike Postgres and Neo4j, CAS is not a container this project starts, versions, or can restart. That is the honest cost, and the reason this decision gets its own section rather than a footnote.

**When CAS is unreachable:**
- A recruiter without an existing Flask session is unable to log in — the redirect to CAS fails, and they receive a 503 "Service Unavailable" page. This is **fail-closed**: no identity is falsely granted, and no unauthenticated work happens.
- A recruiter with an *existing* Flask session continues to work normally — the session cookie is valid and does not depend on CAS being available. The `last_seen_at` refresh on their next login attempt will fail, but they can still use the site (with a stale `last_seen_at` until CAS recovers).

**Why acceptable:**
- CAS is SFU-hosted internal infrastructure, not a third-party API. It is operated by the institution, reachable without outbound internet access, and typically carries higher uptime than this application itself — an outage of the identity provider is very likely accompanied by an outage of everything else the user needs anyway.
- Model inference — the heavy lifting (parsing, embedding, matching, generation) — remains 100% local and unaffected by CAS outage. The UI is unavailable, but the backend service continues to work; a service-to-service caller (the worker, or an API client using keys, see §5) is unaffected.
- The institution can monitor and escalate CAS outages via their own NOC; it is not a vendor SLA problem the recruiter-assistant project can solve.

### 4. Roles become data, not code — the `Role` enum is now vocabulary

The `Role` enum in `core/src/api/deps.py:50-61` is retained as the *vocabulary* of known permissions — the valid set of role labels, a source of truth for the API's role model. It is no longer the *derivation* of identity:

- **Old (ADR-018).** The API key presented → which role key matched → which `Role` enum member → what permissions apply.
- **New (FU-5).** The `X-API-Key` header (if present, for service-to-service callers — see §5) is treated as opaque service identity. For human users (browser session), the Flask session → the `user_id` → the `users.role` *column* value → check against the `Role` vocabulary.

A `users` row's `role` column MUST be one of the strings in `Role` (enforced by application logic, not a DB constraint — pydantic validators on the user-update endpoint). This decouples role management (which is a data operation, done via an admin endpoint) from code and configuration.

### 5. Service-to-service auth is retained — separate from human identity

The worker, and any non-browser caller (an external system querying the API), continue to use API keys for authentication. Be explicit: **this is a separate mechanism from human user identity.** A service API key can never satisfy an action requiring an attributable human actor — most critically, *reveal*. The backend's `require_role` gate still checks the key-derived role; the new audit table records the `actor_user_id` as `NULL` for any action initiated by a key-authenticated caller. This makes it immediately clear in audit rows which actions came from people (user_id present) and which from systems (user_id is NULL).

Routes that require human attribution — `POST /resumes/{id}/reveal`, and any future human-action auditing — 403 if the caller is unauthenticated (no Flask session, no API key) or is service-authenticated (API key present but human session absent).

### 6. A generalized `audit_log` table — append-only, single schema for all actions

Replace the reveal-only `reveal_audit` table with a broader `audit_log`:

```
id              UUID PRIMARY KEY
actor_kind      TEXT NOT NULL  -- one of 'user' or 'service'
actor_user_id   UUID  -- nullable; set when actor_kind='user'
actor_service   TEXT  -- nullable; set when actor_kind='service', e.g. 'worker'
action          TEXT NOT NULL  -- vocabulary: 'reveal', 'blind_review_toggled', 'role_changed', …
subject_type    TEXT NOT NULL  -- 'resume', 'job', 'user', …
subject_id      UUID NOT NULL  -- resume_id, job_id, user_id, …
job_id          UUID  -- nullable; context for resume-reveals, etc.
context         TEXT  -- optional caller-supplied context (e.g. "reviewing for role X")
details         JSONB  -- additional structured data (e.g. old_role='recruiter', new_role='admin')
occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
CONSTRAINT audit_log_actor_identity CHECK (
  (actor_kind = 'user' AND actor_user_id IS NOT NULL AND actor_service IS NULL) OR
  (actor_kind = 'service' AND actor_user_id IS NULL AND actor_service IS NOT NULL)
)
```

All actions — reveals, `blind_review` flips, role changes, and (forward-looking in FU-6) job-assignment changes — write a single row with identity metadata (`actor_kind`, `actor_user_id`, or `actor_service`). The `action` vocabulary grows as features add audited operations, but the schema never changes. `details` is unvalidated JSONB — this is an accepted gap (see Accepted residuals).

**On actor identity and nullability.** The `actor_user_id` column is nullable *by design*: service-initiated actions (worker background jobs, external API clients authenticating with keys) must be loggable and auditable. A blanket `NOT NULL` on `actor_user_id` would force service actions to record a false human identity or not log at all — both unacceptable. Instead, the `actor_kind` discriminator and `CHECK` constraint enforce the invariant: every row names exactly one actor, either a human (`actor_kind='user'`, `actor_user_id NOT NULL`) or a service (`actor_kind='service'`, `actor_service NOT NULL`). Actions requiring an attributable human — *reveal* above all — reject `actor_kind='service'` at the route handler with a 403. The NOT NULL guarantee that matters (a human must be named for reveal) is enforced by the action itself, not by the schema for all rows.

**Migration posture.** The project has no migration framework (`init_schema` in `ddl.py:235-242` re-runs idempotently on every boot). The old `reveal_audit` table is kept, read-only:

- New reveals write only to `audit_log`, not `reveal_audit`.
- Existing `reveal_audit` rows are NOT migrated (no migration path exists that can be tested).
- An admin endpoint (e.g. `GET /audit/reveals-legacy?limit=100&offset=0`) reads the old table for historical review only.
- Future audit viewers treat `audit_log` as the canonical source.

### 7. Reveal requires an attributable human — 403 if non-human caller

Restate ADR-016's ordering guarantee (`core/src/api/routes/resumes.py:248-254`): the audit row is written BEFORE decryption, so a crash during reveal does not leave an audit gap, and an attacker cannot de-anonymize without an audit trace. Extend it: a caller attempting `POST /resumes/{id}/reveal` without a valid Flask session (i.e., no `user_id` in the action context) receives 403, not a fallback to a label like `"api"`. This ensures every audit row has a resolvable person.

The backend's `require_role` gate checks the presented API key (if any) and resolves it to a role; the human-audit gate is *separate* and checks for session identity. Both must pass: the key must grant `reveal` permission AND the session must exist.

### 8. User identity in API requests — HMAC-signed actor assertion header

**The problem.** After CAS login, Flask establishes a session, but the backend API (FastAPI) runs in a separate process and trusts only API keys (today, `X-API-Key` in `core/src/api/deps.py:73-107`). The Flask frontend (`core/frontend/api_client.py:101-109`) sends only the key and the `X-API-Key` header; there is no channel for the session's `user_id` to reach the backend. As written, the backend cannot distinguish a human-authenticated call from a key-authenticated call — both look identical. This blocks FU-6 (ADR-020), which requires the API to know who the human is for scoping audit and access predicates.

**The solution.** After CAS validation and upsert of the `users` row, Flask creates a short-lived, HMAC-signed assertion — a compact token carrying the validated `user_id`, `role`, and expiry — and passes it to the API in a dedicated header, `X-Actor-Assertion`. The backend verifies the signature and expiry and extracts the user identity. This mechanism:

1. **Uses a shared secret, not a new service.** Flask and the API are configured (via pydantic-settings, following the discipline of `PII_KEY` and `SKILL_HASH_SALT`) with an `ACTOR_ASSERTION_SECRET`. No external token service, no new infrastructure — offline-first is preserved.
2. **Verifies before trusting.** On every request, the API parses, verifies the HMAC, and checks expiry. An assertion that fails verification is treated as absent (no user identity), not as an error or hint. Callers who attempt to forge or tamper with the assertion receive the same treatment as callers without one.
3. **Replaces `X-Actor-Name`.** The optional, unverified `X-Actor-Name` header (currently `core/src/api/deps.py:131-141`) is **removed entirely**. A spoofable identity-shaped header next to a cryptographically verified one invites confusion and misconfiguration; removing `X-Actor-Name` closes that risk.
4. **Enables independent role confirmation.** The assertion carries the `user_id` and `role` at the time of login. The backend independently queries the `users` table to confirm the user is `active` and has not had their role revoked. The API does not blindly trust the assertion's `role`; it is advisory and can be overridden by a database lookup.
5. **New dependency: `resolve_user`.** The API's `require_role` dependency (checking API key and role) is orthogonal and unchanged. A new `resolve_user` dependency (or a companion to `require_role`) checks for a valid, non-expired `X-Actor-Assertion` header and returns the authenticated user's `id` and `role`. Routes that need human identity call this dependency. ADR-020's scoping predicates consume the result of `resolve_user`, not `require_role`'s `Role`.

**Tradeoff and mitigation.** A compromised Flask process can forge any `X-Actor-Assertion`, since it holds the signing secret. An attacker with code execution on Flask can impersonate any user. Mitigations: short expiry (e.g., 15 minutes, renewed on each request), so a forged token's window is bounded; independent role validation by the API (the backend checks the `users` table and rejects requests from deactivated users, even if the assertion says otherwise); and separation of concerns (the API still validates role permissions for the resource being accessed, so a forged admin assertion against a recruiter-only route still 403s).

## Consequences

- **Flask becomes the identity gateway.** Every human recruiter must authenticate via CAS before accessing the UI. The `/login` and `/login/callback` routes are new and public (unauthenticated). All other routes are protected by a session check, which redirects to `/login` on expiry.
- **Audit rows now carry actor identity.** Every `audit_log` row records either a human actor (via `actor_kind='user'` and `actor_user_id`) or a service actor (via `actor_kind='service'` and `actor_service`). The auditor role becomes useful — a new `GET /audit/log` endpoint (out of FU-5's scope) can filter by `action='reveal'`, `actor_kind='user'`, `actor_user_id=<auditor-interested-in>`, etc.
- **Service identity is orthogonal to user identity.** The worker, other services, and direct API clients authenticate with API keys and appear in audit rows as `actor_kind='service'`. This is intentional, not a fallback. Human-only actions (reveal) explicitly reject service-authenticated callers at the route layer.
- **Human identity crosses the boundary via signed assertion.** Flask mints an HMAC-signed `X-Actor-Assertion` header carrying the user's identity and role; the FastAPI backend verifies and parses it. This avoids creating a new infrastructure dependency while enabling the backend to know who is making each request.
- **Role provisioning is no longer a deploy step.** An admin endpoint (scope of a future feature) updates `users.role` in the database; no env-var rename or code change required.

### Accepted residuals

- **CAS attribute release varies by institution.** Some institutions release only `username`; others release `username + email`, or `username + display_name + email`, or a subset. The code must handle any of these gracefully (no hard requirement that `display_name` or `email` be non-null). In a practical sense, an auditor can always fall back to the canonical `cas_username` for attribution.
- **CAS session lifetime vs. reveal capability.** A recruiter's Flask session expires (typically 8–24 hours), but a revealed candidate's identity remains visible on the page until they reload. The *reveal* is audited and permanent; the session is not. An audit viewer does not lose the record of who revealed whom, even after the revealer's session expires.
- **No per-user rate limiting.** The code does not track reveal frequency per user. A recruiter can reveal all candidates in a shortlist in one click; an admin could theoretically write a bot to mass-reveal every candidate. Revisit as a follow-up if abuse is observed in practice.
- **`audit_log.details` is unvalidated JSONB.** Application code writes structured data (e.g. `{"old_role": "recruiter", "new_role": "admin"}`), but the schema enforces no schema on the column. An auditor querying this column must anticipate and handle variable structure.
- **No de-provisioning sync when a person leaves.** When an employee leaves the institution, their CAS account is disabled, so they cannot log in — but their `users` row remains (with `active=false` after a manual admin action, if implemented). Historical audit rows remain valid and attributable. A de-provisioning *sync* (periodic fetch of active CAS users and flag inactive rows as `active=false`) is a future admin feature, not built here.
- **ADR-018's auditor-has-nothing-to-view residual is partially closed.** An auditor can now see audit rows attributed to users. The viewer itself (the `/audit/log` UI, the ability to filter/sort/export audit logs) is out of FU-5's scope and is a separate feature, likely FU-6 or a parallel admin-panel task.

## Alternatives Considered

- **Per-user API keys instead of CAS.** Each recruiter generates and stores a key in the app, authentication goes via key presentation (like today, but one key per person instead of one for all). Rejected: simpler than CAS (no dependency outside the compose stack), but users must manage secrets (key rotation, expiry), there is no central de-provisioning (a leaked key is useless only if the app revokes it manually), and the audit trail is no stronger (a shared key is just as anonymous as `"api"`). CAS leverages the institution's existing identity infrastructure and de-provisioning.
- **Local password accounts.** Recruiters create usernames and passwords in the app. Rejected: credential management burden on a team that does not want to run a password reset flow, and the app has no recovery mechanism if an admin is locked out. Per-user keys face the same issue. CAS solves both by delegating to the institution.
- **OIDC (OpenID Connect) instead of CAS.** OIDC is a modern, more widely standardized protocol than CAS. Some institutions offer both. Rejected for this decision, but deliberately *not* architecture-wise: the Flask OAuth2/OIDC library ecosystem is mature, and this ADR's design isolates the protocol behind `settings.cas_login_url` / `settings.cas_service_url` and Flask-side session establishment. Switching from CAS to OIDC requires changing the `/login/callback` implementation in the Flask frontend but no changes to the audit table, the `users` schema, or the backend role model. If the institution offers OIDC (and most modern ones do), a future ADR can narrow the dependency to OIDC without rework.
- **Embedding user identity into the API key itself** (JWT-style, a key that decodes to user info without a lookup). Rejected: every API key is already a bearer token; there is no benefit to encoding user info into the string. The backend *must* query the `users` table anyway to check `active`, `role`, and `last_seen_at`. A simple UUID + lookup is clearer and extends to role changes / de-provisioning without key rotation.
- **Skipping the `users` table and storing session state in Redis.** Rejected: sessions are ephemeral (invalidated on logout or expiry), but audit rows are permanent. A user's identity must be queryable from historical audit rows long after their session has expired. The `users` table is the durable record; sessions are the ephemeral mechanism on top of it.
