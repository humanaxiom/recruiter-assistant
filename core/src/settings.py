"""Central configuration — the single source of truth, loaded from env.

Nothing else in the codebase may read ``os.environ``. Two fields carry
contracts that other modules assert against:

* ``llm_embedding_dim`` — the Neo4j vector indexes are built from this number
  (see ``src/worker/neo4j_bootstrap.py``); the two must never drift apart.
* ``llm_base_url`` — must always point at a local, OpenAI-compatible endpoint
  (Ollama on the host). This project is offline by design: no cloud inference.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings

from src.schemas.matching import MatchWeights


class Settings(BaseSettings):
    # ── Postgres (transactional store; raw asyncpg DSN, not a SQLAlchemy URL) ─
    postgres_dsn: str = "postgresql://app:app@postgres:5432/recruiter"
    postgres_pool_min: int = 2
    postgres_pool_max: int = 10

    # ── Neo4j (skill/experience graph + vector retrieval) ────────────────────
    neo4j_uri: str = "bolt://neo4j:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "recruiterpass"

    # ── Redis (arq broker + embedding cache) ─────────────────────────────────
    redis_url: str = "redis://redis:6379/0"

    # ── Inference (Ollama on metal, OpenAI-compatible /v1 — never a cloud host)
    llm_base_url: str = "http://host.docker.internal:11434/v1"
    llm_model_generation: str = "gpt-oss:20b"
    llm_model_embedding: str = "nomic-embed-text"
    llm_embedding_dim: int = 768  # CONTRACT: == Neo4j `vector.dimensions`
    llm_timeout_s: float = 120.0
    llm_max_retries: int = 2
    llm_breaker_threshold: int = 10  # consecutive failures before the breaker opens
    llm_breaker_cooldown_s: float = 30.0
    # Ollama's OpenAI-compat layer only intermittently honours `think: false`, so a
    # reasoning model (the default gpt-oss:20b is one) can burn its whole token budget
    # on a discarded reasoning trace and return empty content. The native /api/chat
    # route honours it reliably — flip this on if JSON-mode parses come back empty.
    llm_ollama_native: bool = False
    # RESERVED / INERT: passed to LLMClient, read by nothing. It does NOT gate a
    # prompt-logging path, because there isn't one — no log site in the client
    # emits prompt or response bodies at any setting (only a prompt hash), and
    # validation errors are logged as a PII-free digest. Flipping this on today
    # changes no behaviour; if a verbose mode is ever added it must not log
    # prompt bodies, which carry résumé PII.
    debug_llm: bool = False

    # ── Embedding cache (Redis read-through) ─────────────────────────────────
    embedding_cache_ttl_s: int = 60 * 60 * 24 * 90  # 90 days

    # ── Storage (filesystem BlobStore root — no MinIO/S3) ────────────────────
    storage_dir: str = "/data"

    # ── Privacy ──────────────────────────────────────────────────────────────
    pii_key: str = ""  # env-supplied pgcrypto key for the app.pii_key GUC
    blind_review_default: bool = True  # decision 4 — redaction ON by default
    # ADR-008 — the salt folded into every NON-VOCAB skill's canonical Skill-
    # graph key (``h:sha256(salt + normalised_name)[:32]``). Empty by default,
    # same discipline as ``pii_key`` above: ``src.worker.main.startup`` refuses
    # to start on an empty salt (an UNSALTED hash of a closed-ish set of likely
    # candidate names is dictionary-attackable — precompute hashes of common
    # names and confirm a candidate is in the graph). Rotating this value
    # changes every non-vocab skill's key, which requires re-projecting the
    # whole graph (every job/résumé re-parsed) to reconnect REQUIRES/HAS_SKILL
    # edges under the new keys.
    skill_hash_salt: str = ""

    # ── Gates ────────────────────────────────────────────────────────────────
    coverage_threshold: int = 80

    # ── Phase 4b: graph-projection outbox drainer ─────────────────────────────
    # hris hard-codes both as Python default parameters / module constants;
    # CLAUDE.md forbids hard-coded tunables, so they live here instead.
    outbox_drain_batch_size: int = 50
    # Decision 2 — poison rows are capped, not retried forever (hris retries
    # forever). The drainer's SELECT excludes rows at/past this many failed
    # delivery attempts — dead-lettered, not deleted, not retried.
    outbox_max_delivery_attempts: int = 200
    # F5 (security re-audit) — the whole batch (SELECT + every row's model
    # round trips) runs under ONE Postgres transaction with no deadline; a
    # handful of skill-heavy rows can hold that transaction open well past
    # the arq cron tick that invoked the drain. `project_to_graph` always
    # attempts at least one row (forward progress guaranteed even if that one
    # row alone busts the budget), then stops dispatching further rows once
    # either bound is hit — the untouched rows are simply left for the next
    # drain tick (no attempt increment, no dead-lettering).
    outbox_drain_deadline_seconds: float = 4.0
    outbox_max_skill_resolutions_per_drain: int = 200

    # ── Phase 4b: skill-normalisation (Neo4j half) thresholds ─────────────────
    # hris's AUTO_MERGE_THRESHOLD / TIEBREAKER_THRESHOLD module constants.
    # [tiebreaker, auto_merge) is the LLM-tiebreaker grey zone.
    skill_auto_merge_threshold: float = 0.92
    skill_tiebreaker_threshold: float = 0.88

    # ── Phase 4c: matching / ranking tunables (ADR 0021 port) ─────────────────
    # Every default below is copied verbatim from hris
    # ``packages/pipeline/src/pipeline/config.py`` lines 99-159 and mirrors
    # ``src.schemas.matching.MatchWeights``' defaults. CLAUDE.md forbids
    # hard-coded tunables, so the orchestrator sources these via
    # ``weights_from_settings`` below rather than the in-code MatchWeights
    # defaults. Top-level blend + the five structured sub-weights each sum to
    # ~1.0 (the MatchWeights validator enforces it on build).
    match_structured: float = 0.6
    match_evidence: float = 0.3
    match_motivation: float = 0.1
    match_skill: float = 0.40
    match_experience: float = 0.25
    match_education: float = 0.10
    match_seniority: float = 0.15
    match_vector: float = 0.10
    match_must_have_miss_penalty: float = 0.5
    match_implied_experience_relief: float = 0.75
    match_recency_recent_years: int = 2
    match_recency_mid_years: int = 5
    match_recency_recent: float = 1.0
    match_recency_mid: float = 0.7
    match_recency_old: float = 0.4
    match_overqual_ratio: float = 2.0
    match_overqual_slope: float = 0.1
    match_overqual_floor: float = 0.8
    match_education_partial: float = 0.5
    match_seniority_floor: float = 0.5
    match_implied_seniority_factor: float = 1.5
    match_implied_min_coverage: float = 0.5
    match_evidence_met_confidence: float = 0.7
    match_evidence_partial_weight: float = 0.5
    match_evidence_verify_fuzz: float = 0.85
    match_motivation_min_confidence: float = 0.7
    # Semantic skill matching (ADR 0020) + latency caps (ADR 0021).
    match_family_weight: float = 0.5
    match_non_matchable_families: str = "other,domain"
    match_coarse_k: int = 50
    match_evidence_k: int = 15
    # Blocker #10: recruiter-assistant has NO synchronous reverse-match endpoint
    # (nothing on a proxied request path to protect from LLM fan-out), so it
    # inherits hris's CURRENT worker-path default (> 0), never the pre-ADR-0023
    # synchronous-endpoint value of 0.
    match_reverse_evidence_k: int = 10
    match_llm_concurrency: int = 4
    match_evidence_max_tokens: int = 2048

    # ── Flask viewer ─────────────────────────────────────────────────────────
    api_base_url: str = "http://api:8000"
    flask_secret_key: str = "dev-only"

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def weights_from_settings(settings: Settings) -> MatchWeights:
    """Build a validated ``MatchWeights`` from flat settings (ADR 0021 port).

    Kept IN ``src/settings.py`` (not a sibling ``matching/config.py`` the way
    hris splits it) per CLAUDE.md's "config only via src/settings.py". The
    ``MatchWeights`` validator enforces that ``structured+evidence+motivation``
    and the five sub-weights each sum to ~1.0, so a misconfigured ``.env`` fails
    fast (ValueError) at the start of a matching run rather than silently
    skewing ranks.
    """
    return MatchWeights(
        structured=settings.match_structured,
        evidence=settings.match_evidence,
        motivation=settings.match_motivation,
        skill=settings.match_skill,
        experience=settings.match_experience,
        education=settings.match_education,
        seniority=settings.match_seniority,
        vector=settings.match_vector,
        must_have_miss_penalty=settings.match_must_have_miss_penalty,
        implied_experience_relief=settings.match_implied_experience_relief,
        recency_recent_years=settings.match_recency_recent_years,
        recency_mid_years=settings.match_recency_mid_years,
        recency_recent=settings.match_recency_recent,
        recency_mid=settings.match_recency_mid,
        recency_old=settings.match_recency_old,
        overqual_ratio=settings.match_overqual_ratio,
        overqual_slope=settings.match_overqual_slope,
        overqual_floor=settings.match_overqual_floor,
        education_partial=settings.match_education_partial,
        seniority_floor=settings.match_seniority_floor,
        implied_seniority_factor=settings.match_implied_seniority_factor,
        implied_min_coverage=settings.match_implied_min_coverage,
        evidence_met_confidence=settings.match_evidence_met_confidence,
        evidence_partial_weight=settings.match_evidence_partial_weight,
        evidence_verify_fuzz=settings.match_evidence_verify_fuzz,
        motivation_min_confidence=settings.match_motivation_min_confidence,
    )


def non_matchable_families_from_settings(settings: Settings) -> tuple[str, ...]:
    """Parse the comma-separated non-matchable-families setting into a tuple of
    lower-cased family names (empty entries dropped)."""
    return tuple(
        part.strip().lower()
        for part in settings.match_non_matchable_families.split(",")
        if part.strip()
    )
