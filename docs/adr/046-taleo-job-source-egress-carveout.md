# ADR-046: Taleo job source — an egress carve-out for `tre.tbe.taleo.net`

**Status:** Accepted (code default-OFF; enabling it in production has unmet obligations — see §Consequences)
**Date:** 2026-09-03
**Supersedes:** the Taleo-connector deferral in [ADR-012 §2](012-api-routes-auth-upload-scope.md)
**Ports:** `hris` ADR-0012 (`docs/adr/0012-taleo-egress-carveout.md`), adapted

## Context

The sponsor (DTO/CIO) asked on 2026-09-02 for the product to take a **link to
the job posting req** rather than only pasted or uploaded JD text
([SPONSOR_REQUIREMENTS_PLAN.md](../SPONSOR_REQUIREMENTS_PLAN.md) §I3). Asked
which host serves those postings, they answered: *"Taleo job import was
implemented in `C:\repos\hris`. Let us bring it here."*

That answer supersedes both options the plan had framed (store the URL for
provenance; write an allowlisted fetcher). It is also the better answer, and
not mainly for the code: **the hard part of this feature was never the HTTP
call, it is the egress carve-out** — and that argument has already been had,
written down and accepted once, in a repository this team owns.

### The collision this ADR exists to resolve

`CLAUDE.md` §Stack states the non-negotiable plainly: **"NEVER add cloud API
calls."** The whole product is offline-first — inference runs on `aria-gb10`
over Tailscale precisely so that no candidate data crosses a boundary.
[ADR-012 §2](012-api-routes-auth-upload-scope.md) recorded the Taleo connector
as **explicitly deferred**, with the reasoning that "a future sources/connectors
feature is the right home for it".

A worker that fetches `tre.tbe.taleo.net` on a schedule is, definitionally, an
outbound network call at runtime. This ADR does not pretend otherwise. It makes
the carve-out explicit and bounds its blast radius.

### What actually crosses the boundary, stated precisely

**Nothing of ours.** This is an INBOUND data flow: the worker fetches a public
careers page that SFU itself publishes, and parses it. No candidate data, no
résumé, no shortlist, no prompt content leaves the host. The only thing an
observer of that traffic learns is *which public job postings this system is
interested in*, at a cadence of once a day.

That is a materially weaker disclosure than the one `CLAUDE.md`'s rule was
written to prevent, and the distinction is why a carve-out is arguable here at
all. It is **not** an argument that the rule does not apply — it is an argument
about which risk the rule is buying down.

## Decision

**Permit outbound HTTPS to `tre.tbe.taleo.net`, and only that host, from the
worker container — gated by `TALEO_ENABLED`, which defaults to `false`.**

Ported from hris with the deviations in §Port notes. The shape:

- A daily cron and an admin-triggered route both invoke `sync_taleo_jobs`, which walks the SIMOFRAS listing pages, fetches each requisition's detail page, and upserts on `(source, external_id)`.
- Jobs that vanish upstream are **archived**, never hard-deleted — a shortlist referencing one stays intact.
- Each run writes an `audit_log` row, so a silent failure is impossible to miss.
- `parse_job` is re-enqueued for new or changed JD text, so the existing LLM extraction and graph projection paths are unchanged. **This feature produces `jobs` rows and nothing else** — it touches no scoring, no matching, no résumé path.

### Why default-off is the load-bearing part

`TALEO_ENABLED=false` means a fresh checkout, a CI run, the test suite, a
developer's laptop, and any deployment that chose to stay airgapped **never
touch the network for jobs**. The carve-out is opt-in per deployment, and
turning it on is a deliberate act by someone who can be named.

This mirrors the discipline the repo already applies to
`match_use_classified_families` (ADR-044, shipped disabled) and to
`validate_startup_session_secret`'s scoping: a capability that changes the
system's posture ships inert until someone opts in.

## Alternatives considered

### A: Store `posting_url` for provenance only; no fetch

- **Summary.** The recruiter pastes the link; it is recorded and clickable. JD text still arrives by paste or upload.
- **Pros.** Zero new attack surface. ~2 hours. Delivers the traceability §I3 is arguably really after.
- **Why not chosen as the whole answer.** It does not remove the manual step the sponsor asked to remove, and 20+ requisitions were already parsed by hand.
- **Status: ALSO BEING DONE.** This is not either/or — `posting_url` is worth having for manually-created jobs the sync never touches, and it does not depend on the carve-out landing. Tracked as slice S8.

### B: Write a fresh allowlisted fetcher here

- **Pros.** No dependency on a sibling repo's design.
- **Cons.** Re-derives an argument already made and accepted, and re-earns the parser's hard-won robustness (see §Port notes on layout drift) from scratch.
- **Why rejected.** Strictly more work for a strictly less-tested result.

### C: External fetcher writing a file drop

- **Summary.** A script on an internet-connected box fetches Taleo and drops JSON where the airgapped worker imports it.
- **Pros.** The app host stays 100% airgapped; no carve-out, no firewall change.
- **Cons.** A second host to own, monitor, secure and keep alive — forever — for a once-daily read of a public page.
- **Why rejected.** hris weighed the same option and chose operational simplicity. Worth revisiting **only if** a security assessment refuses the carve-out.

### D: An SFU-internal feed

- **Summary.** If SFU IT already ingests Taleo into a warehouse, read from there over the LAN.
- **Why deferred, not rejected.** This is the right long-term shape and needs no carve-out at all. It depends on another team's roadmap and on the feed actually carrying the JD body. If it materialises, a follow-up ADR supersedes this one.

### E: Fetch the "Full Job Description" PDF

- **Why deferred.** Doubles traffic and adds PyMuPDF to the sync path. hris captures the PDF URL in the description and does not fetch the body. **This limitation matters more here than there**, because §O1 ranks against the JD and a thin inline summary is a thin requirement set — but measure whether the summaries are usable before lifting it.

## Consequences

- ✅ **Opt-in per deployment.** CI, tests, fresh checkouts and airgapped installs never egress.
- ✅ **Inbound only.** No candidate data, prompt content or shortlist crosses the boundary; the traffic reveals only which public postings the system reads.
- ✅ **Idempotent.** `UNIQUE (source, external_id)` + `ON CONFLICT`. Same feed → same rows. Re-running mid-day is safe.
- ✅ **Deletions handled without data loss.** Vanished postings archive; shortlists referencing them survive.
- ✅ **Tamper-evident.** One audit row per run, so a silently-failing sync is visible.
- ⚠️ **This IS a compliance-posture change**, and the repo's own non-negotiable says otherwise. Anyone reading `CLAUDE.md` §Stack must find this ADR — the rule's wording is updated to point here.
- ⚠️ **HTML scraping is fragile.** The Taleo template has changed before. Mitigations ported intact: parsers pin to landmarks (`rid=NNNN`, field labels) not positional XPaths; vendored fixture tests are the regression net; an empty listing is logged as an anomaly rather than treated as "no jobs today".
- ⚠️ **Jobs the sync creates have no human owner** until a recruiter acts on one. Their audit trail resolves to a system actor.
- ❌ **UNMET OBLIGATIONS before `TALEO_ENABLED=true` in production**, carried over from hris's ADR and not discharged by this one:
  1. The **firewall rule must enumerate `tre.tbe.taleo.net:443`** explicitly — not a category, not a wildcard.
  2. **Counsel and privacy-officer sign-off** on the carve-out. hris's ADR required it; nothing about doing it twice makes it unnecessary.
  3. A **named owner** who can revoke it in one change.

  The code merging does not enable it. These gate the flag, not the branch — which is exactly what default-off buys.

## Port notes — deviations from the hris source

- **`structlog` → stdlib `logging`.** This repo has no structlog dependency and uses `logging.getLogger(__name__)` everywhere.
- **New dependencies: `beautifulsoup4` + `lxml`.** The parsers need an HTML tree. Both are parse-only and reach no network.
- **`TaleoClient` lands in a later slice than the parsers.** The pure parse functions carry all the fragility and all the tests, and they are network-free — so they land first, with the fixtures, and the client that actually egresses lands behind the flag afterwards. That split is deliberate: it means the risky-to-maintain half is under test before the risky-to-run half exists.
- **Settings live in `src/settings.py`**, never a sibling `config.py` — `CLAUDE.md` §Code rules.
- **No Alembic.** This repo has no migration framework; the `jobs` columns land as idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, per the `withdrawn_at`/`work_authorization` precedent.

## Notes

- Related: [ADR-012](012-api-routes-auth-upload-scope.md) §2 (the deferral this supersedes), [ADR-003](003-offline-inference-ollama.md) (the offline-inference posture this does *not* change — inference stays local; only a public HTML read is permitted).
- The sponsor's requirement and the four decisions behind it: [SPONSOR_REQUIREMENTS_PLAN.md](../SPONSOR_REQUIREMENTS_PLAN.md) §0, §2.3.
