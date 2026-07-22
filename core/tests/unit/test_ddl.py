"""Unit tests for the Postgres startup DDL (``src/models/ddl.py``).

The DDL is the schema contract for the whole port. These tests pin:

* **idempotency** — every ``CREATE`` carries ``IF NOT EXISTS`` so the app can
  run ``init_schema`` on every boot (integration proves it for real),
* **scope** — the ported tables exist (now including ``users``/``audit_log``
  per ADR-019, FU-5 slice 1 — schema only) and the still-cut ones (review
  workflow, JD-Harmonizer) cannot creep back in,
* **PII at rest** — name/email/phone/cover-letter-text are ``BYTEA``
  (pgcrypto), and only the email *hash* is plaintext,
* **blind review ON by default** (decision 4),
* **attributable audit** — ``audit_log`` carries an actor-identity CHECK
  distinguishing a human actor from a service actor (ADR-019 §1.4/§6).

Contract with the implementation: ``init_schema`` awaits ``.execute(stmt)`` on
the object it is handed (an asyncpg ``Connection`` or ``Pool``) once per
statement, in ``_STATEMENTS`` order.
"""

from __future__ import annotations

import inspect
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.ddl import _STATEMENTS, init_schema

EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "jobs",
        "resumes",
        "shortlist_entries",
        "reverse_match_entries",
        "outbox",
        "reveal_audit",
        "users",
        "audit_log",
    }
)

# Explicitly out of scope: review workflow (hris 0004/0007) and the
# JD-Harmonizer (`jd_*`). See EXTRACTION_PLAN keep/cut boundary.
#
# NOTE: `users` was ALSO listed here originally — ddl.py's module docstring
# (line 6) still says CAS auth tables were "deliberately excluded" from v1.
# ADR-019 (FU-5) explicitly REVERSES that specific cut: a `users` table is
# the foundation for attributable audit (per-person reveal/blind_review
# actor identity). `users` therefore moves OUT of this cut-tables tuple and
# INTO EXPECTED_TABLES above. This is a legitimate RED-phase update
# authorized by ADR-019, not a weakening of an existing assertion.
CUT_TABLES: tuple[str, ...] = (
    "shortlist_decisions",
    "stage_transitions",
    "jd_documents",
    "jd_harmonizations",
)

# Columns cut from hris `jobs`: Taleo ingest, JD-Harmonizer, review workflow.
CUT_JOB_COLUMNS: tuple[str, ...] = (
    "source",
    "external_id",
    "external_url",
    "external_last_seen_at",
    "description_sfu",
    "title_autofilled",
    "approval_required_2nd_review",
)

ENCRYPTED_COLUMNS: tuple[str, ...] = (
    "candidate_name",
    "candidate_email",
    "candidate_phone",
    "cover_letter_text",
)

# JSONB in ddl.py is already correct; this guard stops a silent downgrade to a
# lossy TEXT column. `evidence` holds the per-requirement verified quotes whose
# fabrication is a hard fail from Phase 3 — a TEXT column would accept '{}' and
# ship green, so the guard must fail on TEXT.
JSONB_PAYLOAD_COLUMNS: tuple[str, ...] = (
    "score_breakdown",
    "evidence",
    "pipeline_meta",
)

EXPECTED_INDEXES: tuple[str, ...] = (
    "jobs_status_open_idx",
    "jobs_created_at_idx",
    "jobs_description_sha256_idx",
    "resumes_job_status_idx",
    "resumes_email_hash_idx",
    "resumes_uploaded_at_idx",
    "resumes_has_cover_idx",
    "shortlist_job_rank_idx",
    "reverse_match_entries_resume_job_idx",
    "reverse_match_entries_resume_idx",
    "outbox_undelivered_idx",
    "outbox_aggregate_idx",
    "reveal_audit_resume_idx",
)

# ADR-019 §1: the minimum column set for `users` — real identity entity.
USERS_EXPECTED_COLUMNS: tuple[str, ...] = (
    "id",
    "cas_username",
    "display_name",
    "email",
    "role",
    "active",
    "created_at",
    "last_seen_at",
)

# ADR-019 §1.4/§6: the minimum column set for the generalized `audit_log`,
# which must be able to record both reveals and `blind_review` flips.
AUDIT_LOG_EXPECTED_COLUMNS: tuple[str, ...] = (
    "id",
    "actor_kind",
    "actor_user_id",
    "actor_service",
    "action",
    "subject_type",
    "subject_id",
    "job_id",
    "context",
    "details",
    "occurred_at",
)

# ADR-019 §4: the Role vocabulary — data now, not code. `recruiter` is the
# default role on first CAS login (ADR-019 §2 step 3).
ROLE_VALUES: tuple[str, ...] = ("admin", "recruiter", "hiring_manager", "auditor")


# ── Helpers ────────────────────────────────────────────────────────────────


def _squash(sql: str) -> str:
    """Collapse all whitespace so multi-line DDL can be matched literally."""
    return " ".join(sql.split())


def _all_sql() -> str:
    return _squash(" ".join(_STATEMENTS))


def _created_tables() -> set[str]:
    return {
        m.group(1).lower()
        for m in re.finditer(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)",
            _all_sql(),
            re.IGNORECASE,
        )
    }


def _table_sql(table: str) -> str:
    """The single CREATE TABLE statement for ``table`` (squashed)."""
    pattern = re.compile(
        rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{table}\b", re.IGNORECASE
    )
    matches = [_squash(s) for s in _STATEMENTS if pattern.search(_squash(s))]
    assert len(matches) == 1, f"expected exactly one CREATE TABLE for {table}"
    return matches[0]


def _statement_index(table: str) -> int:
    pattern = re.compile(
        rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{table}\b", re.IGNORECASE
    )
    for i, stmt in enumerate(_STATEMENTS):
        if pattern.search(_squash(stmt)):
            return i
    raise AssertionError(f"no CREATE TABLE for {table}")


def _fake_conn() -> Any:
    """asyncpg Connection/Pool double: ``execute`` is awaitable, and
    ``async with conn.transaction():`` works if the impl wraps the DDL."""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="OK")
    return conn


def _executed(conn: Any) -> list[str]:
    return [_squash(call.args[0]) for call in conn.execute.await_args_list]


# ── Idempotency ────────────────────────────────────────────────────────────


def test_statements_is_a_non_empty_tuple() -> None:
    assert isinstance(_STATEMENTS, tuple)
    assert len(_STATEMENTS) > 0
    assert all(isinstance(s, str) and s.strip() for s in _STATEMENTS)


@pytest.mark.parametrize("stmt", _STATEMENTS, ids=range(len(_STATEMENTS)))
def test_every_create_statement_is_idempotent(stmt: str) -> None:
    """Every CREATE TABLE/INDEX/EXTENSION must be re-runnable on each boot."""
    sql = _squash(stmt)
    if re.search(r"CREATE\s+(UNIQUE\s+)?(TABLE|INDEX|EXTENSION)\b", sql, re.IGNORECASE):
        assert re.search(r"IF\s+NOT\s+EXISTS", sql, re.IGNORECASE), sql


def test_enums_are_guarded_by_a_pg_type_existence_check() -> None:
    """Enums have no IF NOT EXISTS — they need the DO $$ ... pg_type guard."""
    for enum in ("job_status", "resume_status"):
        guarded = [
            _squash(s)
            for s in _STATEMENTS
            if f"'{enum}'" in _squash(s) or f"typname = '{enum}'" in _squash(s)
        ]
        assert guarded, f"no guarded CREATE TYPE for {enum}"
        block = " ".join(guarded)
        assert "pg_type" in block
        assert "CREATE TYPE" in block.upper()


def test_job_status_enum_values() -> None:
    sql = _all_sql()
    for value in ("'draft'", "'open'", "'closed'", "'archived'"):
        assert value in sql


def test_resume_status_enum_values() -> None:
    sql = _all_sql()
    for value in ("'uploaded'", "'parsing'", "'parsed'", "'failed'"):
        assert value in sql


# ── Extensions ─────────────────────────────────────────────────────────────


def test_pgcrypto_extension_is_created_first() -> None:
    """pgp_sym_encrypt/decrypt underpin every PII column — it must exist."""
    first = _squash(_STATEMENTS[0])
    assert re.search(
        r"CREATE\s+EXTENSION\s+IF\s+NOT\s+EXISTS\s+pgcrypto", first, re.IGNORECASE
    )


# ── Scope: the expected tables in, the cut ones out ────────────────────────


def test_exactly_the_expected_tables_are_created() -> None:
    assert _created_tables() == EXPECTED_TABLES


@pytest.mark.parametrize("table", CUT_TABLES)
def test_cut_tables_are_not_created(table: str) -> None:
    assert table not in _created_tables()
    assert not re.search(rf"\b{table}\b", _all_sql(), re.IGNORECASE)


def test_no_jd_harmonizer_tables() -> None:
    assert not [t for t in _created_tables() if t.startswith("jd_")]


@pytest.mark.parametrize("column", CUT_JOB_COLUMNS)
def test_cut_job_columns_are_absent(column: str) -> None:
    assert not re.search(rf"\b{column}\b", _table_sql("jobs"), re.IGNORECASE)


def test_no_grant_statements() -> None:
    """hris's app_writer/app_auditor roles are not ported."""
    assert "GRANT" not in _all_sql().upper()


def test_tables_are_created_before_the_tables_that_reference_them() -> None:
    assert (
        _statement_index("jobs")
        < _statement_index("resumes")
        < _statement_index("shortlist_entries")
        < _statement_index("reverse_match_entries")
    )


# ── jobs ───────────────────────────────────────────────────────────────────


def test_jobs_blind_review_defaults_true() -> None:
    """Decision 4 — DEVIATION from hris, whose default was FALSE."""
    assert re.search(
        r"blind_review\s+BOOLEAN\s+NOT\s+NULL\s+DEFAULT\s+TRUE",
        _table_sql("jobs"),
        re.IGNORECASE,
    )


def test_jobs_retention_days_check() -> None:
    sql = _table_sql("jobs")
    assert re.search(
        r"retention_days\s+INTEGER\s+NOT\s+NULL\s+DEFAULT\s+180", sql, re.I
    )
    assert re.search(r"retention_days\s+BETWEEN\s+30\s+AND\s+730", sql, re.IGNORECASE)


def test_jobs_created_by_is_a_nullable_text_actor_label() -> None:
    """DEVIATION: hris had a UUID FK -> users(id); there is no users table."""
    sql = _table_sql("jobs")
    assert re.search(r"created_by\s+TEXT", sql, re.IGNORECASE)
    assert not re.search(r"created_by[^,]*REFERENCES", sql, re.IGNORECASE)


def test_jobs_uses_the_job_status_enum() -> None:
    assert re.search(
        r"status\s+job_status\s+NOT\s+NULL\s+DEFAULT\s+'draft'",
        _table_sql("jobs"),
        re.IGNORECASE,
    )


def test_jobs_description_sha256_in_create_table() -> None:
    """FU-3 Slice 4 dedup key — the column is declared in the CREATE TABLE."""
    assert re.search(r"description_sha256\s+TEXT", _table_sql("jobs"), re.IGNORECASE)


def test_jobs_description_sha256_has_an_idempotent_alter() -> None:
    """RISK 3 — ``CREATE TABLE IF NOT EXISTS`` is a NO-OP against an existing
    dev/CI volume, so the new column would never appear. A separate, idempotent
    ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` guarantees it lands. This is a
    new convention (the port has never used ALTER before)."""
    alters = [
        _squash(s)
        for s in _STATEMENTS
        if re.search(
            r"ALTER\s+TABLE\s+jobs\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
            r"description_sha256\s+TEXT",
            _squash(s),
            re.IGNORECASE,
        )
    ]
    assert len(alters) == 1, "expected exactly one idempotent ALTER for the column"


def test_jobs_description_sha256_alter_runs_after_the_create_table() -> None:
    """The ALTER (and its dependent partial index) must come AFTER the jobs
    CREATE TABLE so the column exists when they run."""
    alter_idx = next(
        i
        for i, s in enumerate(_STATEMENTS)
        if re.search(
            r"ALTER\s+TABLE\s+jobs\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
            r"description_sha256",
            _squash(s),
            re.IGNORECASE,
        )
    )
    assert _statement_index("jobs") < alter_idx


def test_jobs_description_sha256_index_is_partial() -> None:
    """The dedup-lookup index skips NULLs (rows predating the backfill)."""
    assert re.search(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+jobs_description_sha256_idx\s+"
        r"ON\s+jobs\s*\(\s*description_sha256\s*\)\s+"
        r"WHERE\s+description_sha256\s+IS\s+NOT\s+NULL",
        _all_sql(),
        re.IGNORECASE,
    )


# ── resumes / PII ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("column", ENCRYPTED_COLUMNS)
def test_pii_columns_are_bytea(column: str) -> None:
    """Encrypted at rest via pgcrypto — never plaintext, never embedded."""
    assert re.search(rf"\b{column}\s+BYTEA\b", _table_sql("resumes"), re.IGNORECASE)


def test_candidate_email_hash_is_plaintext_text() -> None:
    """Subject-access lookup key: sha256(lower(trim(email))), not encrypted."""
    sql = _table_sql("resumes")
    assert re.search(r"candidate_email_hash\s+TEXT", sql, re.IGNORECASE)
    assert not re.search(r"candidate_email_hash\s+BYTEA", sql, re.IGNORECASE)


def test_resumes_consent_acknowledged_has_no_default() -> None:
    """Every insert must supply consent explicitly."""
    sql = _table_sql("resumes")
    assert re.search(r"consent_acknowledged\s+BOOLEAN\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert not re.search(
        r"consent_acknowledged\s+BOOLEAN\s+NOT\s+NULL\s+DEFAULT", sql, re.IGNORECASE
    )


def test_resumes_dedupe_constraint() -> None:
    assert re.search(
        r"UNIQUE\s*\(\s*job_id\s*,\s*sha256\s*\)", _table_sql("resumes"), re.IGNORECASE
    )


def test_resumes_cascade_from_jobs() -> None:
    assert re.search(
        r"job_id\s+UUID\s+NOT\s+NULL\s+REFERENCES\s+jobs\s*\(\s*id\s*\)"
        r"\s+ON\s+DELETE\s+CASCADE",
        _table_sql("resumes"),
        re.IGNORECASE,
    )


def test_resumes_file_size_check() -> None:
    assert re.search(
        r"file_size_bytes\s+BIGINT\s+NOT\s+NULL\s+CHECK\s*\(\s*file_size_bytes\s*>=\s*0",
        _table_sql("resumes"),
        re.IGNORECASE,
    )


# ── ranking tables ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("table", ["shortlist_entries", "reverse_match_entries"])
def test_score_final_is_double_precision_bounded_0_to_1(table: str) -> None:
    """DEVIATION: unified on DOUBLE PRECISION so asyncpg never returns Decimal."""
    sql = _table_sql(table)
    assert re.search(r"score_final\s+DOUBLE\s+PRECISION\s+NOT\s+NULL", sql, re.I)
    assert not re.search(r"score_final\s+NUMERIC", sql, re.IGNORECASE)
    assert re.search(r"score_final\s+BETWEEN\s+0\s+AND\s+1", sql, re.IGNORECASE)


@pytest.mark.parametrize("table", ["shortlist_entries", "reverse_match_entries"])
def test_rank_is_positive(table: str) -> None:
    assert re.search(
        r"rank\s+INTEGER\s+NOT\s+NULL\s+CHECK\s*\(\s*rank\s*>\s*0\s*\)",
        _table_sql(table),
        re.IGNORECASE,
    )


@pytest.mark.parametrize("table", ["shortlist_entries", "reverse_match_entries"])
@pytest.mark.parametrize("column", JSONB_PAYLOAD_COLUMNS)
def test_ranking_payload_columns_are_jsonb(table: str, column: str) -> None:
    """The 4-stage ranking output columns must be JSONB, never TEXT.

    Parsed per-table so a downgrade of one table cannot hide behind the other:
    reverse_match_entries is checked independently of shortlist_entries.
    """
    sql = _table_sql(table)
    assert re.search(rf"\b{column}\s+JSONB\b", sql, re.IGNORECASE), sql
    assert not re.search(rf"\b{column}\s+TEXT\b", sql, re.IGNORECASE), sql


def test_shortlist_entries_unique_per_job_resume() -> None:
    assert re.search(
        r"UNIQUE\s*\(\s*job_id\s*,\s*resume_id\s*\)",
        _table_sql("shortlist_entries"),
        re.IGNORECASE,
    )


def test_reverse_match_upsert_target_is_a_unique_index() -> None:
    """The DELETE + re-INSERT-per-run target: UNIQUE (resume_id, job_id)."""
    assert re.search(
        r"CREATE\s+UNIQUE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+"
        r"reverse_match_entries_resume_job_idx\s+ON\s+reverse_match_entries\s*"
        r"\(\s*resume_id\s*,\s*job_id\s*\)",
        _all_sql(),
        re.IGNORECASE,
    )


# ── outbox ─────────────────────────────────────────────────────────────────


def test_outbox_aggregate_id_has_no_foreign_key() -> None:
    """Polymorphic aggregate id — an FK would break job/resume reuse."""
    sql = _table_sql("outbox")
    assert re.search(r"aggregate_id\s+UUID\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert "REFERENCES" not in sql.upper()
    assert re.search(r"id\s+BIGSERIAL\s+PRIMARY\s+KEY", sql, re.IGNORECASE)


# ── users / audit_log (ADR-019, FU-5 slice 1: schema only) ─────────────────


@pytest.mark.parametrize("column", USERS_EXPECTED_COLUMNS)
def test_users_table_has_expected_columns(column: str) -> None:
    assert re.search(rf"\b{column}\b", _table_sql("users"), re.IGNORECASE)


def test_users_id_is_uuid_primary_key() -> None:
    assert re.search(r"id\s+UUID\s+PRIMARY\s+KEY", _table_sql("users"), re.IGNORECASE)


def test_users_cas_username_is_unique_and_not_null() -> None:
    """ADR-019 §1 — the unique, immutable identity from CAS."""
    sql = _table_sql("users")
    assert re.search(r"cas_username\s+TEXT\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert re.search(r"cas_username[^,]*\bUNIQUE\b", sql, re.IGNORECASE)


def test_users_role_is_not_null() -> None:
    """Roles become data (ADR-019 §4) — the column must always be populated."""
    assert re.search(r"role\s+TEXT\s+NOT\s+NULL", _table_sql("users"), re.IGNORECASE)


def test_users_active_flag_defaults_true() -> None:
    """ADR-019 §1 — deprovisioning without deletion; new rows are active."""
    assert re.search(
        r"active\s+BOOLEAN\s+NOT\s+NULL\s+DEFAULT\s+true",
        _table_sql("users"),
        re.IGNORECASE,
    )


def test_users_display_name_and_email_are_nullable() -> None:
    """ADR-019 Accepted residuals — CAS may release neither attribute."""
    sql = _table_sql("users")
    assert not re.search(r"display_name\s+TEXT\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert not re.search(r"\bemail\s+TEXT\s+NOT\s+NULL", sql, re.IGNORECASE)


def test_users_last_seen_at_defaults_to_now() -> None:
    """Refreshed on every login (ADR-019 §2 step 3); must default on create."""
    assert re.search(
        r"last_seen_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+now\s*\(\s*\)",
        _table_sql("users"),
        re.IGNORECASE,
    )


@pytest.mark.parametrize("column", AUDIT_LOG_EXPECTED_COLUMNS)
def test_audit_log_table_has_expected_columns(column: str) -> None:
    assert re.search(rf"\b{column}\b", _table_sql("audit_log"), re.IGNORECASE)


def test_audit_log_id_is_uuid_primary_key() -> None:
    assert re.search(
        r"id\s+UUID\s+PRIMARY\s+KEY", _table_sql("audit_log"), re.IGNORECASE
    )


def test_audit_log_action_and_subject_are_not_null() -> None:
    """ADR-019 §6 — required for every row regardless of actor kind."""
    sql = _table_sql("audit_log")
    assert re.search(r"action\s+TEXT\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert re.search(r"subject_type\s+TEXT\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert re.search(r"subject_id\s+UUID\s+NOT\s+NULL", sql, re.IGNORECASE)


def test_audit_log_actor_kind_is_not_null() -> None:
    assert re.search(
        r"actor_kind\s+TEXT\s+NOT\s+NULL", _table_sql("audit_log"), re.IGNORECASE
    )


def test_audit_log_actor_user_id_and_actor_service_are_nullable() -> None:
    """ADR-019 §6 — nullable BY DESIGN; exactly one is set per row, enforced
    by the CHECK constraint below, not by column-level NOT NULL."""
    sql = _table_sql("audit_log")
    assert not re.search(r"actor_user_id\s+UUID\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert not re.search(r"actor_service\s+TEXT\s+NOT\s+NULL", sql, re.IGNORECASE)


def test_audit_log_details_is_jsonb_not_text() -> None:
    """Accepted residual (ADR-019) — unvalidated JSONB, never a lossy TEXT."""
    sql = _table_sql("audit_log")
    assert re.search(r"\bdetails\s+JSONB\b", sql, re.IGNORECASE)
    assert not re.search(r"\bdetails\s+TEXT\b", sql, re.IGNORECASE)


def test_audit_log_occurred_at_defaults_to_now() -> None:
    assert re.search(
        r"occurred_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+now\s*\(\s*\)",
        _table_sql("audit_log"),
        re.IGNORECASE,
    )


def test_audit_log_actor_identity_check_constraint_is_present() -> None:
    """ADR-019 §1.4/§6 — a row must name EXACTLY ONE actor: a human
    (actor_kind='user' AND actor_user_id set AND actor_service NULL) or a
    service (actor_kind='service' AND actor_user_id NULL AND actor_service
    set). This is the constraint that distinguishes attributable-human rows
    from service rows and is the whole point of the generalized table."""
    sql = _table_sql("audit_log")
    assert re.search(r"CHECK\s*\(", sql, re.IGNORECASE), sql
    assert re.search(r"actor_kind\s*=\s*'user'", sql, re.IGNORECASE)
    assert re.search(r"actor_kind\s*=\s*'service'", sql, re.IGNORECASE)
    assert re.search(r"actor_user_id\s+IS\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert re.search(r"actor_user_id\s+IS\s+NULL", sql, re.IGNORECASE)
    assert re.search(r"actor_service\s+IS\s+NOT\s+NULL", sql, re.IGNORECASE)
    assert re.search(r"actor_service\s+IS\s+NULL", sql, re.IGNORECASE)


def test_users_table_is_created_before_audit_log() -> None:
    """audit_log's actor_user_id is conceptually keyed to a users row
    (ADR-019 §6); users must exist first."""
    assert _statement_index("users") < _statement_index("audit_log")


# ── indexes ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("index", EXPECTED_INDEXES)
def test_expected_index_is_created(index: str) -> None:
    assert re.search(
        rf"CREATE\s+(UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+{index}\b",
        _all_sql(),
        re.IGNORECASE,
    )


# ── init_schema ────────────────────────────────────────────────────────────


def test_init_schema_is_async() -> None:
    assert inspect.iscoroutinefunction(init_schema)


@pytest.mark.asyncio
async def test_init_schema_executes_every_statement_in_order() -> None:
    conn = _fake_conn()
    await init_schema(conn)
    assert _executed(conn) == [_squash(s) for s in _STATEMENTS]


@pytest.mark.asyncio
async def test_init_schema_is_idempotent_when_run_twice() -> None:
    conn = _fake_conn()
    await init_schema(conn)
    await init_schema(conn)
    expected = [_squash(s) for s in _STATEMENTS]
    assert _executed(conn) == expected + expected


@pytest.mark.asyncio
async def test_init_schema_propagates_driver_errors() -> None:
    conn = _fake_conn()
    conn.execute = AsyncMock(side_effect=RuntimeError("connection reset"))
    with pytest.raises(RuntimeError, match="connection reset"):
        await init_schema(conn)
