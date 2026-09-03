"""RED — the half of the Taleo source that actually reaches the network.

ADR-046. The parsers landed first, network-free; this is the client, and it is
the only code in this repository that egresses. Everything below is about
bounding what it can do.

**The SSRF hole this closes, which the hris source leaves to the firewall.**
``fetch_requisition`` fetches ``listing.external_url`` — and that URL comes out
of *parsed HTML*. Anyone who can influence the Taleo page (a template change, a
compromised upstream, a posting whose title smuggles markup past the parser)
chooses where the worker's next request goes. On this box that request would
originate inside the tailnet, next to the GPU hosts.

hris relies on the firewall allowlist to contain that. A firewall rule is a
real control and ADR-046 still requires one — but it lives in a different
system, owned by different people, and it is not enforced by anything in this
repository or its tests. So the client refuses off-host URLs itself, and the
refusal is tested. Defence in depth, on this repo's standing "refuse rather
than fail open" discipline.

**Redirects are not followed**, for the same reason: a 302 is an off-host fetch
the allowlist check above would never see.

**Nothing here runs unless ``TALEO_ENABLED``**, which defaults to ``false`` —
a fresh checkout, a CI run and every airgapped deployment never construct this.

``TaleoClient`` does not exist yet — RED half of the TDD cycle.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

_BASE = "https://tre.tbe.taleo.net"


def _client(handler: Any, **kw: Any) -> Any:
    """A ``TaleoClient`` wired to an in-memory transport — no sockets."""
    from src.pipeline.sources.taleo import TaleoClient

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TaleoClient(
        http,
        base_url=kw.pop("base_url", _BASE),
        org="SIMOFRAS",
        cws="37",
        request_delay_s=0.0,  # no real sleeping in tests
        **kw,
    )


def _row(url: str) -> Any:
    from src.pipeline.sources.taleo import TaleoListingRow

    return TaleoListingRow(
        external_id="7124",
        title="Analyst",
        external_url=url,
        location=None,
        department=None,
        employment_type=None,
    )


# ------------------------------------------------------------ the host guard


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.example.com/steal",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://localhost:8000/api/v1/jobs",  # the app's own API
        "http://100.88.247.106:11434/api/tags",  # a tailnet GPU host
        "https://tre.tbe.taleo.net.evil.com/",  # suffix trick
        "file:///etc/passwd",
    ],
)
async def test_a_requisition_url_off_the_allowed_host_is_refused(
    hostile: str,
) -> None:
    """The URL comes from PARSED HTML, so whoever influences the page chooses
    the destination. On this box that request originates inside the tailnet,
    next to the GPU hosts — hence the metadata and localhost cases."""
    from src.pipeline.sources.taleo import TaleoHostNotAllowedError

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"the client fetched a forbidden host: {request.url}")

    client = _client(handler)
    with pytest.raises(TaleoHostNotAllowedError):
        await client.fetch_requisition(_row(hostile))


@pytest.mark.asyncio
async def test_the_allowed_host_is_fetched_normally() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="<html><body><h1>Analyst</h1></body></html>")

    client = _client(handler)
    await client.fetch_requisition(_row(f"{_BASE}/tre01/ats/careers/v2/req?rid=7124"))
    assert seen and seen[0].startswith(_BASE)


@pytest.mark.asyncio
async def test_the_guard_compares_hosts_not_prefixes() -> None:
    """``startswith(base_url)`` would accept ``tre.tbe.taleo.net.evil.com``.
    The check has to parse the URL and compare the host exactly."""
    from src.pipeline.sources.taleo import TaleoHostNotAllowedError

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("prefix-matched a hostile host")

    client = _client(handler)
    with pytest.raises(TaleoHostNotAllowedError):
        await client.fetch_requisition(_row(f"{_BASE}.evil.com/x"))


@pytest.mark.asyncio
async def test_redirects_are_not_followed() -> None:
    """A 302 is an off-host fetch the allowlist check never sees. httpx does
    not follow redirects by default — this pins that nobody turns it on."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "start" in str(request.url):
            return httpx.Response(
                302, headers={"location": "https://evil.example.com/"}
            )
        raise AssertionError(f"followed a redirect to {request.url}")

    client = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.fetch_requisition(_row(f"{_BASE}/start"))


# ------------------------------------------------------------- polite by design


@pytest.mark.asyncio
async def test_every_request_carries_an_attributable_user_agent() -> None:
    """ADR-046's politeness obligation. SFU IT must be able to correlate this
    traffic to this system without asking around — an unattributed scraper on
    a university careers page is indistinguishable from an abusive one."""
    agents: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        agents.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, text="<html></html>")

    client = _client(handler)
    await client.fetch_requisition(_row(f"{_BASE}/x"))
    assert agents and "recruiter-assistant" in agents[0].lower()


@pytest.mark.asyncio
async def test_listing_pagination_stops_at_max_pages() -> None:
    """Runaway protection. A template that always renders a "next" link would
    otherwise walk forever against a live university server."""
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(str(request.url))
        # Always offers another page — the pathological case.
        return httpx.Response(
            200,
            text=(
                f'<html><body><a href="/req?rid={len(pages)}">Job</a>'
                '<a href="/searchResults?pageNo=99">Next</a></body></html>'
            ),
        )

    client = _client(handler, max_pages=3)
    await client.fetch_listings()
    assert len(pages) <= 3


@pytest.mark.asyncio
async def test_a_page_that_adds_no_new_requisitions_ends_pagination() -> None:
    """Some Taleo templates loop on the last page. Without this the walk only
    stops at ``max_pages``, tripling the traffic for no new rows."""
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(str(request.url))
        return httpx.Response(
            200,
            text=(
                '<html><body><a href="/req?rid=7124">Job</a>'
                '<a href="/searchResults?pageNo=2">Next</a></body></html>'
            ),
        )

    client = _client(handler, max_pages=20)
    rows = await client.fetch_listings()
    assert len(rows) == 1
    assert len(pages) == 2, "pagination did not stop when a page added nothing new"


@pytest.mark.asyncio
async def test_an_http_error_is_raised_not_swallowed() -> None:
    """A 500 from Taleo must not read as "no jobs today" — that would archive
    every job in the system on the next sweep."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    client = _client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.fetch_listings()
