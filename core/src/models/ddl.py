"""Idempotent Postgres startup DDL — the schema comes up with the app.

There is no migration framework: ``init_schema`` runs on every boot of the API
and the worker, so **every statement must be re-runnable**. Ported from the
hris migrations, with the review workflow, the JD-Harmonizer, and the Taleo
ingest columns cut (see docs/EXTRACTION_PLAN.md). CAS auth tables were
originally cut too, but ADR-019 (FU-5) reverses that: ``users`` and
``audit_log`` below are the foundation for attributable audit.

Deviations from hris, all deliberate:

* ``jobs.blind_review`` defaults ``TRUE`` (hris: ``FALSE``) — decision 4.
* ``jobs.created_by`` / ``resumes.uploaded_by`` are nullable ``TEXT`` actor
  labels, not UUID FKs — this predates ADR-019's ``users`` table and is not
  yet wired to it (FU-5 slice 1 is schema only).
* ``score_final`` is ``DOUBLE PRECISION`` in both ranking tables (hris mixed
  ``NUMERIC(5,4)`` and ``DOUBLE PRECISION``, so asyncpg handed back a
  ``Decimal`` from one and a ``float`` from the other). The 0..1 CHECK stays.
* ``reverse_match_entries.rank`` gains the ``> 0`` CHECK its twin already had.

PII (``candidate_name``/``candidate_email``/``candidate_phone``/
``cover_letter_text``) is ``BYTEA``, encrypted at rest with pgcrypto's
``pgp_sym_encrypt`` under the ``app.pii_key`` GUC. Only the *hash* of the email
is plaintext, and only so subject-access requests can find a candidate.
"""

from __future__ import annotations

from typing import Protocol


class _Executor(Protocol):
    """The slice of asyncpg's Connection/Pool that the DDL needs."""

    async def execute(self, query: str, *args: object) -> object: ...


_STATEMENTS: tuple[str, ...] = (
    # ── Extensions ───────────────────────────────────────────────────────────
    # pgp_sym_encrypt/pgp_sym_decrypt underpin every PII column.
    "CREATE EXTENSION IF NOT EXISTS pgcrypto",
    # ── Enums (no IF NOT EXISTS for CREATE TYPE — guard on pg_type) ──────────
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'job_status') THEN
            CREATE TYPE job_status AS ENUM ('draft', 'open', 'closed', 'archived');
        END IF;
    END
    $$
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'resume_status') THEN
            CREATE TYPE resume_status
                AS ENUM ('uploaded', 'parsing', 'parsed', 'failed');
        END IF;
    END
    $$
    """,
    # ── jobs ─────────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        title             TEXT NOT NULL,
        department        TEXT,
        location          TEXT,
        employment_type   TEXT,
        seniority         TEXT,
        min_years         INTEGER,
        description_raw   TEXT NOT NULL,
        description_sha256 TEXT,
        description_parsed JSONB,
        status            job_status NOT NULL DEFAULT 'draft',
        retention_days    INTEGER NOT NULL DEFAULT 180
                          CHECK (retention_days BETWEEN 30 AND 730),
        shortlist_top_percent INTEGER NOT NULL DEFAULT 100
                          CHECK (shortlist_top_percent BETWEEN 1 AND 100),
        blind_review      BOOLEAN NOT NULL DEFAULT TRUE,
        failure_reason    TEXT,
        created_by        TEXT,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        parsed_at         TIMESTAMPTZ,
        closed_at         TIMESTAMPTZ
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS jobs_status_open_idx ON jobs (status)
        WHERE status IN ('draft', 'open')
    """,
    "CREATE INDEX IF NOT EXISTS jobs_created_at_idx ON jobs (created_at DESC)",
    # FU-3 Slice 4 (bulk-JD dedup): ``description_sha256`` is the dedup key. This
    # is the FIRST use of ALTER in the port — ``CREATE TABLE IF NOT EXISTS`` is a
    # NO-OP against an already-existing dev/CI Postgres volume, so the column
    # added to the CREATE above would silently never appear on those volumes and
    # the first dedup query would 500. A separate idempotent ALTER guarantees the
    # column lands on both fresh and existing databases. New convention: when a
    # column is added to an existing table, add BOTH the CREATE-TABLE column and a
    # matching ``ADD COLUMN IF NOT EXISTS`` here.
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS description_sha256 TEXT",
    # The dedup lookup (``WHERE description_sha256 = $1``). Partial so it skips
    # rows predating the column (NULLs) — the ALTER back-fills nothing.
    """
    CREATE INDEX IF NOT EXISTS jobs_description_sha256_idx
        ON jobs (description_sha256)
        WHERE description_sha256 IS NOT NULL
    """,
    # Per-job configurable shortlist cap (slice A, schema only). Same
    # already-migrated-volume risk as description_sha256 above: CREATE TABLE
    # IF NOT EXISTS is a no-op against an existing dev/CI volume, so a
    # separate idempotent ALTER guarantees the column lands there too. There
    # is no idempotent-CHECK clause in Postgres pre-15 (no
    # ``ADD CONSTRAINT IF NOT EXISTS``), so the 1-100 CHECK rides inline on
    # the ADD COLUMN clause itself — the whole clause is skipped once the
    # column exists, which keeps this statement safely re-runnable.
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS shortlist_top_percent INTEGER "
    "NOT NULL DEFAULT 100 CHECK (shortlist_top_percent BETWEEN 1 AND 100)",
    # ── resumes ──────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS resumes (
        id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        job_id               UUID NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        blob_key             TEXT NOT NULL,
        original_filename    TEXT NOT NULL,
        mime_type            TEXT NOT NULL,
        file_size_bytes      BIGINT NOT NULL CHECK (file_size_bytes >= 0),
        sha256               TEXT NOT NULL,
        candidate_name       BYTEA,
        candidate_email      BYTEA,
        candidate_phone      BYTEA,
        candidate_email_hash TEXT,
        parsed               JSONB,
        status               resume_status NOT NULL DEFAULT 'uploaded',
        uploaded_by          TEXT,
        uploaded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        parsed_at            TIMESTAMPTZ,
        failure_reason       TEXT,
        consent_acknowledged BOOLEAN NOT NULL,
        cover_letter_blob_key TEXT,
        cover_letter_text    BYTEA,
        cover_letter_parsed  JSONB,
        cover_letter_sha256  TEXT,
        UNIQUE (job_id, sha256)
    )
    """,
    "CREATE INDEX IF NOT EXISTS resumes_job_status_idx ON resumes (job_id, status)",
    """
    CREATE INDEX IF NOT EXISTS resumes_email_hash_idx ON resumes (candidate_email_hash)
        WHERE candidate_email_hash IS NOT NULL
    """,
    "CREATE INDEX IF NOT EXISTS resumes_uploaded_at_idx ON resumes (uploaded_at DESC)",
    """
    CREATE INDEX IF NOT EXISTS resumes_has_cover_idx ON resumes (job_id)
        WHERE cover_letter_blob_key IS NOT NULL OR cover_letter_text IS NOT NULL
    """,
    # ── shortlist_entries ────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS shortlist_entries (
        id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        job_id          UUID NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        resume_id       UUID NOT NULL REFERENCES resumes (id) ON DELETE CASCADE,
        rank            INTEGER NOT NULL CHECK (rank > 0),
        score_final     DOUBLE PRECISION NOT NULL
                        CHECK (score_final BETWEEN 0 AND 1),
        score_breakdown JSONB NOT NULL,
        evidence        JSONB NOT NULL,
        pipeline_meta   JSONB NOT NULL,
        generated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (job_id, resume_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS shortlist_job_rank_idx
        ON shortlist_entries (job_id, rank)
    """,
    # ── reverse_match_entries ────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS reverse_match_entries (
        id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        resume_id         UUID NOT NULL REFERENCES resumes (id) ON DELETE CASCADE,
        job_id            UUID NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        rank              INTEGER NOT NULL CHECK (rank > 0),
        score_final       DOUBLE PRECISION NOT NULL
                          CHECK (score_final BETWEEN 0 AND 1),
        score_structured  DOUBLE PRECISION NOT NULL,
        score_evidence    DOUBLE PRECISION NOT NULL,
        score_breakdown   JSONB NOT NULL,
        evidence          JSONB,
        requirement_count INTEGER NOT NULL,
        must_have_count   INTEGER NOT NULL,
        pipeline_meta     JSONB NOT NULL,
        generated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # The DELETE + re-INSERT-per-run upsert target.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS reverse_match_entries_resume_job_idx
        ON reverse_match_entries (resume_id, job_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS reverse_match_entries_resume_idx
        ON reverse_match_entries (resume_id, rank)
    """,
    # ── outbox (polymorphic aggregate id — deliberately no FK) ───────────────
    """
    CREATE TABLE IF NOT EXISTS outbox (
        id                BIGSERIAL PRIMARY KEY,
        aggregate         TEXT NOT NULL,
        aggregate_id      UUID NOT NULL,
        event_type        TEXT NOT NULL,
        payload           JSONB NOT NULL,
        created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
        delivered_at      TIMESTAMPTZ,
        delivery_attempts INTEGER NOT NULL DEFAULT 0,
        last_error        TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS outbox_undelivered_idx ON outbox (id)
        WHERE delivered_at IS NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS outbox_aggregate_idx
        ON outbox (aggregate, aggregate_id)
    """,
    # FU-1 (audited reveal): an append-only audit sink. Revealing a candidate's
    # identity is the de-anonymization action, so every reveal writes one row
    # here. Never UPDATEd or DELETEd by app code. `actor` is sourced from the
    # CAS session identity as of FU-5 slice 7 (ADR-019 §8.3/§9.2), via the new
    # `users`/`audit_log` tables below. This table is kept read-only per
    # ADR-019 §6 — no data migration into `audit_log`.
    """
    CREATE TABLE IF NOT EXISTS reveal_audit (
        id          UUID PRIMARY KEY,
        resume_id   UUID NOT NULL,
        job_id      UUID,
        actor       TEXT,
        context     TEXT,
        revealed_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS reveal_audit_resume_idx
        ON reveal_audit (resume_id, revealed_at DESC)
    """,
    # ── users (ADR-019 §1, FU-5 slice 1: schema only) ────────────────────────
    # Real identity entity: a row per CAS-authenticated person. Append-mostly —
    # rows are created and updated (display_name/email/last_seen_at refresh on
    # login), never deleted, so historical audit_log rows stay attributable.
    """
    CREATE TABLE IF NOT EXISTS users (
        id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        cas_username TEXT NOT NULL UNIQUE,
        display_name TEXT,
        email        TEXT,
        role         TEXT NOT NULL DEFAULT 'recruiter',
        active       BOOLEAN NOT NULL DEFAULT true,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # ── audit_log (ADR-019 §1.4/§6, FU-5 slice 1: schema only) ───────────────
    # Generalized, append-only audit sink replacing reveal-only reveal_audit.
    # Every row names EXACTLY ONE actor: a human (actor_kind='user', with
    # actor_user_id set and actor_service NULL) or a service (actor_kind=
    # 'service', with actor_service set and actor_user_id NULL) — enforced by
    # the CHECK constraint below, not by column-level NOT NULL.
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        actor_kind    TEXT NOT NULL,
        actor_user_id UUID REFERENCES users (id) ON DELETE RESTRICT,
        actor_service TEXT,
        action        TEXT NOT NULL,
        subject_type  TEXT NOT NULL,
        subject_id    UUID NOT NULL,
        job_id        UUID,
        context       TEXT,
        details       JSONB,
        occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT audit_log_actor_identity CHECK (
            (actor_kind = 'user' AND actor_user_id IS NOT NULL
                AND actor_service IS NULL)
            OR
            (actor_kind = 'service' AND actor_user_id IS NULL
                AND actor_service IS NOT NULL)
        )
    )
    """,
    # ── sessions (ADR-019 §10 step 4, FU-5 slice 3) ──────────────────────────
    # The ported hris session store: an opaque, server-held session row behind
    # an httpOnly cookie. ``id`` is TEXT (a ``secrets.token_urlsafe(32)``
    # value generated by the app), not UUID like every other primary key here.
    # ``ON DELETE CASCADE`` on ``user_id`` is deliberate: deleting/deprovisioning
    # a user removes their sessions too — the revocation-on-deprovision
    # behaviour hris relied on.
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id          TEXT PRIMARY KEY,
        user_id     UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        expires_at  TIMESTAMPTZ NOT NULL,
        revoked_at  TIMESTAMPTZ,
        user_agent  TEXT,
        ip_addr     INET,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS sessions_user_id_idx ON sessions (user_id)",
    # ADR-019 §10 step 5: the sliding-window active-session lookup — partial
    # so it only ever covers unrevoked sessions.
    """
    CREATE INDEX IF NOT EXISTS sessions_active_idx ON sessions (expires_at)
        WHERE revoked_at IS NULL
    """,
    # ── job_assignees (ADR-020 §1, FU-6 slice 1) ─────────────────────────────
    # Per-job assignment / row-level scoping: links a user to a single job,
    # with an attributable ``assigned_by``. PK is the (job_id, user_id) pair —
    # a user may be assigned to each job at most once. ``assigned_by`` uses
    # ON DELETE RESTRICT (not CASCADE like job_id/user_id): if the assigning
    # user is deleted, orphaned assignments are not silently cascaded away;
    # the DELETE fails until the assignment is explicitly cleared first — a
    # safety guard against accidental admin-account cleanup erasing
    # delegation records.
    """
    CREATE TABLE IF NOT EXISTS job_assignees (
        job_id       UUID NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
        user_id      UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        assigned_by  UUID NOT NULL REFERENCES users (id) ON DELETE RESTRICT,
        assigned_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (job_id, user_id)
    )
    """,
    # ADR-020 §1: powers the fast "all jobs for this user" query.
    """
    CREATE INDEX IF NOT EXISTS job_assignees_user_idx
        ON job_assignees (user_id, assigned_at DESC)
    """,
)


async def init_schema(conn: _Executor) -> None:
    """Create the schema. Idempotent — safe to run on every boot.

    Executes against whatever it is handed: an asyncpg ``Connection`` (the
    integration tests, the worker startup) or a ``Pool`` (the API lifespan).
    """
    for statement in _STATEMENTS:
        await conn.execute(statement)
