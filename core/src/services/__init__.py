"""Service layer — the SQL that the worker (and later the API) writes through.

Raw asyncpg, hand-written SQL, no ORM. Every function here takes an open
``asyncpg.Connection`` and leaves transaction scoping to the caller: the
worker wraps the write-back + the outbox INSERT in ONE transaction so a
parsed row and its projection event commit atomically.

Three modules in Phase 3:

* ``pii`` — pgcrypto encrypt/decrypt under the transaction-scoped
  ``app.pii_key`` GUC, plus the plaintext ``email_hash``.
* ``job_service`` / ``resume_service`` — the worker write-back functions
  (``record_parsed`` / ``record_parse_failure``), each guarded by an
  optimistic-concurrency WHERE clause on the source status.
* ``outbox_service`` — append-only event enqueue, drained to Neo4j in Phase 4.
"""

from __future__ import annotations
