# ADR-048: Shared SFU telemetry: JSON logs, traces and a default-off export to a zone collector

**Status:** Proposed (pilot; code default-OFF for export. Enabling export on the pilot box needs the obligations in §Consequences)
**Date:** 2026-11-28

## Context

Every SFU product built on this harness logs, but none of them can be followed
across a request. In this repo specifically:

- **Most log lines never appear.** About 34 modules call `logging.getLogger(__name__)`,
  and nothing configures a handler. Under uvicorn and arq only WARNING+ reaches
  stderr (through Python's last-resort handler), and every `extra=` field,
  including the whole `llm.attempt` record, is dropped.
- **Nothing links the hops.** There is no request id, trace id or correlation key.
  A ranking that goes browser → Flask BFF → API → arq → worker → Ollama can only
  be reassembled by guessing from timestamps and domain ids.
- **`llm.attempt` has no model name**, so latency and token data cannot be tied to
  the model that produced it, which matters every time `scripts/model-check.sh`
  says a model profile changed.
- **`audit_log` rows cannot be tied to the request that wrote them.**

The same gaps exist across the sibling SFU repos (jd-assistant, jd-bank-pilot,
sfu-site, student-assistant, SFU-digital-twin), with three telemetry dialects
between them. A shared library and a per-zone collector fix them once; this repo
is the pilot.

### Alternatives that were live

1. **Configure stdlib logging here only** (a `dictConfig` in this repo). This
   fixes the dropped lines but gives no correlation across processes, and every
   sibling repo would reinvent it.
2. **A vendor or SaaS APM** (Datadog, New Relic, Langfuse Cloud, App Insights).
   Rejected outright: telemetry about candidate processing would leave SFU.
3. **Phoenix/OpenInference only** (what Torque, Forge and Knowledge-engine use).
   Good for LLM traces, but it is not a logs or metrics system, and its Elastic
   License 2.0 is an open question.
4. **OpenTelemetry through a shared library, exported to a self-hosted collector
   in the same security zone (chosen).** Open standard; the same library serves
   every SFU repo; Grafana/Loki/Tempo/Prometheus behind it are self-hosted; and
   spans carry both the OTel GenAI and the OpenInference attribute names, so
   Phoenix still works where a team wants it.

## Decision

1. **`sfu-telemetry` (vendored wheel, `core/vendor/`)** is called once per process
   from the production entrypoint only: `telemetry_lifespan` (API),
   `startup_with_telemetry` (worker) and `frontend.wsgi` (frontend). Unit tests
   import the app modules without it, so process-wide logging is never changed
   under pytest.
2. **Logs** become one JSON object per line on stdout, with `trace_id`/`span_id`,
   `extra=` fields under `attrs`, and `event.name` taken from the message template.
   No call site changes. `LOG_FORMAT=text` gives a human-readable fallback.
3. **Traces:**
   - **API:** an ASGI middleware creates one span per request, named by route template.
   - **Frontend:** a Flask instrumentation does the same, named by URL rule.
   - **Hop to the API:** an httpx hook carries the trace from the BFF.
   - **Queue:** an arq Redis carrier links enqueue to worker. It is a
     `sfu:traceparent:<job_id>` key, 24 h TTL. The task registry and task
     signatures are unchanged.
   - **Model calls:** one GenAI span per `LLMClient` call, retries included.

   Responses carry `X-Request-ID: <trace_id>`.
4. **What enters telemetry is metadata only, and that is fixed, not configured:**
   `src/telemetry.py` pins `data_class_max="S"` and `content_level="minimal"`, and
   `Settings` has no field for either. LLM spans record the model, token counts,
   attempt count and the existing prompt HASH, never a body. Exceptions are
   recorded by type only. Log exceptions render frames without the message.
   Access logs use the route template, with no query string and no client address.
5. **Export is a separate switch and DEFAULTS OFF** (`TELEMETRY_EXPORT=off`). With
   `otlp`, spans and metrics go to `TELEMETRY_OTLP_ENDPOINT`, a collector inside
   the same security zone (the SFU reference stack, `sfu-aria/sfu-observability`).
   Logs never need export: the collector reads container stdout on the same host.
6. **`audit_log.trace_id TEXT NULL`** is added idempotently and written from the
   current span by `record_audit`, so the provenance ledger can link an audit
   event to its trace.

7. **Trust and collection boundaries** (from the review and security audit):
   - **Trace context:** the Flask BFF is the public edge and ignores a
     browser's `traceparent`, so no client can choose the trace id written
     next to its audit rows. The API continues the BFF's trace
     (`trust_incoming_context=True`); its callers are the BFF and key-holding
     services.
   - **Log collection is opt-in per container.** The zone collector can read
     every container's log file on a host, so it ingests only lines from
     containers labelled `sfu.telemetry=opt-in`. `compose.telemetry.yml` opts in
     api, worker and frontend, never a datastore.
   - **Log arguments are redacted (class S).** The JSON formatter renders
     every log argument as `[redacted]` unless it is an id, a number, a flag or
     None. ruff `G004` forbids f-string log messages, which would bypass that.

## Consequences

- **Egress carve-out #2, stated per `CLAUDE.md` §Stack.**
  - **Flag:** `TELEMETRY_EXPORT`, default `off`.
  - **Destination:** one operator-set collector endpoint, which must sit inside the
    same trust zone as this box (same host or same tailnet). It is never a vendor.
  - **What crosses:** metadata only, per Decision 4. No candidate data, résumé,
    prompt, response or shortlist content.
  - **Before turning it on in production:** name the collector's owner, confirm
    it is on SFU infrastructure, and have the privacy officer review the
    attribute list. The reference stack's README lists those attributes.
- **Operators reading `docker compose logs`** see JSON; set `LOG_FORMAT=text`
  locally if preferred.
- **Deploy ordering for the arq carrier:** the worker tolerates a missing
  carrier (the job gets a root span), and an old worker ignores the key. Either
  order is safe.
- **The vendored wheel** is replaced by an index pin once SFU has an internal
  package index. The `+sfu` local version cannot be published to PyPI, so no
  public package can shadow it in the meantime.
- **Not done here:** shipping `audit_log` rows to the zone ledger. It needs a
  server-side read-only Postgres role for the shipper and a decision on
  exposing the database to the zone network, so `compose.telemetry.yml`
  does not put Postgres on that network. (The pilot's end-to-end run proved
  the path with a session-only overlay.) Also not done: an audit hash chain
  at source (the ledger chains on ingestion), a SHA-256 pin for the vendored
  wheel, and a model gateway.
