"""Taleo (Oracle Cloud Recruiting) HTML parsers for the SIMOFRAS org.

Two halves, kept apart on purpose.

``parse_listing_page`` and ``parse_requisition_page`` are PURE -- HTML text in,
typed records out, no I/O, no config. They hold all the fragility (HTML
scraping is the part that rots; the Taleo template has changed before) and are
covered by vendored fixtures, so they stay testable in the ordinary offline
gate whether or not any deployment ever switches the source on.

``TaleoClient`` is the ONLY code in this repository that egresses. It runs
solely when ``TALEO_ENABLED`` is true -- default ``false``, so a fresh checkout,
a CI run and every airgapped deployment never construct it -- and it refuses
any URL outside the configured Taleo host before each request. See ADR-046.

SFU publishes jobs on the legacy Taleo v2 careers UI:

    https://tre.tbe.taleo.net/tre01/ats/careers/v2/searchResults
        ?org=SIMOFRAS&cws=37

The page is server-rendered HTML — there is no public JSON API at that org.
The listing table yields requisition ids (``rid=NNNN``); each requisition's
view page yields the structured fields and the inline JD body.

Ported from hris ``packages/pipeline/src/pipeline/sources/taleo.py``
(see ADR-046 §Port notes). DEVIATIONS from that source:

* **``structlog`` -> stdlib ``logging``.** hris logs through structlog, which
  this repo does not depend on. The pure parsers log nothing at all; the
  empty-listing anomaly ADR-046 promises is logged by the *sync task*, where
  the run context lives.
* **The client refuses off-host URLs itself.** hris leaves that to the firewall.
  ``fetch_requisition`` follows a URL parsed OUT OF HTML, so whoever influences
  the page picks the destination -- and here that request would originate inside
  the tailnet, beside the GPU hosts. The firewall rule is still required; this
  is the same boundary enforced again, where a test can hold it.

Two design properties worth preserving on any edit:

* **Landmarks, not positional XPaths.** Everything keys off the requisition
  link pattern and the field labels. A template tweak that moves a column
  must not silently yield empty results — and an empty listing is treated by
  the caller as an anomaly to report, never as "no jobs today".
* **PDF JDs are captured as a URL, not fetched.** Taleo pages often link a
  "Full Job Description" PDF; the body is deliberately not retrieved
  (ADR-046 alternative E). The inline summary is what gets parsed, and
  whether it is rich enough to rank against is an open measurement.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

log = logging.getLogger(__name__)

# Captures the requisition id from any link href on the listing page.
# Module-level so the pure parsers below don't reach into the client class.
_RID_PATTERN = re.compile(r"[?&]rid=(\d+)")


# ---------------- typed records ----------------


@dataclass(slots=True, frozen=True)
class TaleoListingRow:
    """One row of the search-results table."""

    external_id: str  # Taleo's `rid` query param value
    title: str
    location: str | None
    department: str | None
    employment_type: str | None
    external_url: str  # absolute URL to viewRequisition


@dataclass(slots=True)
class TaleoRequisition:
    """Full requisition detail (the body of one viewRequisition page).

    Mutable (not frozen) because fields are populated incrementally
    during parsing.
    """

    external_id: str
    title: str
    external_url: str
    location: str | None = None
    department: str | None = None
    employment_type: str | None = None
    description_raw: str = ""
    structured_fields: dict[str, str] = field(default_factory=dict)
    pdf_url: str | None = None  # operator follow-up when the JD body is a PDF


# ---------------- the network half (ADR-046) ----------------
#
# Everything above and below this section is pure. THIS is the only code in the
# repository that egresses, and it runs only when TALEO_ENABLED is true.


class TaleoHostNotAllowedError(RuntimeError):
    """A URL outside the configured Taleo host was about to be fetched.

    Raised rather than skipped: an off-host URL appearing in a Taleo page means
    either the template changed in a way the parser misread, or the page is
    serving links it should not. Both are worth stopping the run over, and
    neither should be silently ignored on the next 200 requisitions.
    """


#: Identifies this system to SFU IT in the request log. ADR-046's politeness
#: obligation: an unattributed scraper against a university careers page is
#: indistinguishable from an abusive one, and the person fielding the question
#: should not have to ask around to find the owner.
TALEO_USER_AGENT = (
    "recruiter-assistant/1.0 (SFU job-source sync; contact SFU IT ARIA team)"
)


class TaleoClient:
    """Stateful client — one per sync run.

    Takes an ``httpx.AsyncClient`` so the worker reuses its own connection
    pool.

    **Every fetch is host-checked first** (:meth:`_assert_allowed`), which is a
    deliberate deviation from the hris source. ``fetch_requisition`` follows a
    URL that came out of PARSED HTML, so whoever influences the Taleo page
    chooses where this worker's next request goes — and on this deployment that
    request originates inside the tailnet, alongside the GPU hosts and the
    app's own API. ADR-046 still requires the firewall rule; this is the same
    boundary enforced a second time, in the repository that can test it.

    **Redirects are not followed.** httpx's default, pinned by a test: a 302 is
    an off-host fetch that the allowlist check would never see.
    """

    _LISTING_PATH = "/tre01/ats/careers/v2/searchResults"

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str,
        org: str,
        cws: str,
        request_delay_s: float = 1.0,
        max_pages: int = 20,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._allowed_host = (urlparse(self._base_url).hostname or "").lower()
        self._org = org
        self._cws = cws
        self._delay = request_delay_s
        self._max_pages = max_pages

    # ---------------- public ----------------

    async def fetch_listings(self) -> list[TaleoListingRow]:
        """Walk the paginated listing pages; one row per requisition.

        Two independent stopping conditions, and both matter. ``max_pages`` is
        runaway protection against a template that always renders a "next"
        link. The "this page added nothing new" break handles the commoner
        case: some Taleo templates loop on the last page, which without it
        costs the full page budget in live requests for no new rows.

        An HTTP error propagates. A 503 must never read as "no jobs today" —
        the caller's archive sweep would then retire every job in the system.
        """
        rows: list[TaleoListingRow] = []
        seen_rids: set[str] = set()
        next_url: str | None = self._listing_url(page=1)
        page_count = 0

        while next_url is not None and page_count < self._max_pages:
            page_count += 1
            html = await self._get_text(next_url)
            page_rows, page_next = parse_listing_page(html, base_url=self._base_url)
            new_this_page = 0
            for row in page_rows:
                if row.external_id in seen_rids:
                    continue
                seen_rids.add(row.external_id)
                rows.append(row)
                new_this_page += 1
            log.debug(
                "taleo.listing.parsed page=%d rows=%d new=%d",
                page_count,
                len(page_rows),
                new_this_page,
            )
            if new_this_page == 0:
                break
            next_url = page_next
            if next_url is not None:
                await asyncio.sleep(self._delay)

        log.info("taleo.listings.complete count=%d pages=%d", len(rows), page_count)
        return rows

    async def fetch_requisition(self, listing: TaleoListingRow) -> TaleoRequisition:
        """Hydrate one listing row with its requisition detail page."""
        await asyncio.sleep(self._delay)
        html = await self._get_text(listing.external_url)
        return parse_requisition_page(html, listing=listing)

    # ---------------- internals ----------------

    def _assert_allowed(self, url: str) -> None:
        """Refuse anything that is not http(s) on the configured Taleo host.

        Compares the parsed HOST exactly. A ``startswith(base_url)`` check —
        the obvious shortcut — accepts ``tre.tbe.taleo.net.evil.com``, and the
        scheme check is what keeps ``file://`` out.
        """
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or host != self._allowed_host:
            raise TaleoHostNotAllowedError(
                f"refusing to fetch {parsed.scheme}://{host or '?'} — the Taleo "
                f"source may only reach {self._allowed_host}. This URL came "
                "from parsed HTML, so an off-host link means the template "
                "changed or the page is serving links it should not."
            )

    def _listing_url(self, *, page: int) -> str:
        q = {"org": self._org, "cws": self._cws}
        if page > 1:
            q["pageNo"] = str(page)
        return f"{self._base_url}{self._LISTING_PATH}?{urlencode(q)}"

    async def _get_text(self, url: str) -> str:
        self._assert_allowed(url)
        resp = await self._http.get(url, headers={"User-Agent": TALEO_USER_AGENT})
        resp.raise_for_status()
        return resp.text


# ---------------- pure parsers (no I/O; unit-tested on vendored fixtures) ------


def parse_listing_page(
    html: str, *, base_url: str
) -> tuple[list[TaleoListingRow], str | None]:
    """Extract rows + the absolute URL of the 'next' page (or None).

    The Taleo template renders requisitions inside a table where each
    row's first cell is a link to ``viewRequisition?...&rid=NNNN``.
    Surrounding cells carry the location / department / employment
    fields — we parse them positionally with a fallback to label-based
    lookup.
    """
    soup = BeautifulSoup(html, "lxml")
    rows: list[TaleoListingRow] = []
    # The live accordion repeats each rid across the title + View + Apply
    # links; dedup by external_id keeping the FIRST occurrence (document
    # order puts the head title link, with the real title + metadata,
    # before the action buttons).
    seen_rids: set[str] = set()

    for link in soup.find_all("a"):
        if not isinstance(link, Tag):
            continue
        href = str(link.get("href") or "")
        rid_match = _RID_PATTERN.search(href)
        if not rid_match:
            continue
        rid = rid_match.group(1)
        if rid in seen_rids:
            continue
        title = _clean(link.get_text())
        if not title:
            continue
        seen_rids.add(rid)
        # Read the sibling metadata (location/department/employment).
        loc, dept, emp = _listing_fields(link)
        rows.append(
            TaleoListingRow(
                external_id=rid,
                title=title,
                location=loc,
                department=dept,
                employment_type=emp,
                external_url=urljoin(base_url + "/", href),
            )
        )

    next_url = _find_next_link(soup, base_url=base_url)
    return rows, next_url


def parse_requisition_page(html: str, *, listing: TaleoListingRow) -> TaleoRequisition:
    """Pull structured fields + inline JD body out of one detail page.

    Strategy:
      - structured fields come from a dl/dt/dd or table:th/td pattern;
        we accept either by walking 'label-ish' nodes.
      - description_raw is the longest text block on the page after the
        structured fields are stripped — Taleo wraps the JD in a div
        whose class varies across templates, so we lean on heuristics.
      - pdf_url is set if we find an <a href="...pdf"> labelled as a
        full job description.
    """
    soup = BeautifulSoup(html, "lxml")
    req = TaleoRequisition(
        external_id=listing.external_id,
        title=listing.title,
        external_url=listing.external_url,
        location=listing.location,
        department=listing.department,
        employment_type=listing.employment_type,
    )

    _extract_structured_fields(soup, req)
    req.description_raw = _extract_description_body(soup)
    req.pdf_url = _find_full_jd_pdf(soup, base_url=req.external_url)
    _promote_listing_fields(req)
    return req


def _extract_structured_fields(soup: BeautifulSoup, req: TaleoRequisition) -> None:
    """Populate req.structured_fields from dl/dt/dd AND th/td patterns
    (Taleo templates use both). dl wins on collisions (more specific)."""
    for dl in soup.find_all("dl"):
        if not isinstance(dl, Tag):
            continue
        terms = [t for t in dl.find_all("dt") if isinstance(t, Tag)]
        defs = [d for d in dl.find_all("dd") if isinstance(d, Tag)]
        for dt, dd in zip(terms, defs, strict=False):
            label = _clean(dt.get_text())
            value = _clean(dd.get_text())
            if label and value:
                req.structured_fields[label] = value

    for row in soup.find_all("tr"):
        if not isinstance(row, Tag):
            continue
        th = row.find("th")
        td = row.find("td")
        if not (isinstance(th, Tag) and isinstance(td, Tag)):
            continue
        label = _clean(th.get_text())
        value = _clean(td.get_text())
        if label and value:
            req.structured_fields.setdefault(label, value)


_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "br", "tr", "div"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _extract_description_body(soup: BeautifulSoup) -> str:
    """Longest contiguous text block on the page — the JD body, with
    paragraph + bullet structure preserved.

    The previous implementation called ``get_text(separator=" ")`` which
    collapsed headings, paragraphs and list items into one giant line.
    Two costs: humans saw a wall of text, AND the LLM extractor's
    chunker couldn't find section boundaries, so requirements blurred
    together. Fix: walk block-level descendants and emit one line per
    block, with list items prefixed by ``- `` and headings followed by
    a blank line. We still pick the longest candidate container (the
    structure-agnostic heuristic that survives Taleo template changes),
    but render its contents structurally instead of as flat text.
    """
    # Drop page chrome BEFORE scoring. SFU's Taleo template renders the
    # nav bar ("New Search", "Login") as <nav>, and there's <script>
    # noise throughout; removing them fixes the longest-container scoring
    # and the output. NOTE: do NOT decompose <header> — SFU wraps the
    # ENTIRE posting (including the JD) inside a <header>, so removing it
    # would empty the description.
    for chrome in soup.find_all(["nav", "footer", "script", "style", "noscript"]):
        if isinstance(chrome, Tag):
            chrome.decompose()
    # The attribute filter as a KWARG rather than ``attrs={"role": ...}``.
    # Same query, but it type-checks: ``dict`` is invariant in its value type,
    # so a ``dict[str, str]`` literal is not assignable to the wide
    # ``dict[str, str | bytes | Pattern | ...]`` bs4's overloads declare —
    # whereas the ``**kwargs`` form accepts a plain ``str`` directly. hris
    # runs a laxer mypy configuration and did not surface this.
    for chrome in soup.find_all(role="navigation"):
        if isinstance(chrome, Tag):
            chrome.decompose()

    containers = [
        div
        for div in soup.find_all(["div", "section", "article"])
        if isinstance(div, Tag)
    ]
    if not containers:
        return ""
    # Score by raw text length so we still pick the JD body, not nav.
    longest = max(containers, key=lambda c: len(c.get_text(" ", strip=True)))
    rendered = _render_block(longest)
    # Many real Taleo postings (SFU is one) wrap the entire body in a
    # single <div> with no inner <p>/<h2>/<li> tags — the browser uses
    # CSS to fake paragraphing, so HTML-aware extraction can't help.
    # Apply a section-keyword pass that breaks runs on the JD heading
    # phrases recruiters actually use. Conservative keyword list to
    # minimise false positives mid-sentence.
    return _strip_chrome_preamble(_split_on_section_keywords(rendered))


# Leading Taleo UI/breadcrumb phrases that precede the actual posting.
# Stripped only from the START of the extracted body (never mid-text) so
# a JD that legitimately says "Login to the portal" later isn't touched.
_CHROME_PREFIX: tuple[str, ...] = (
    "New Search",
    "Return to Search Results",
    "Login",
    "Print Preview",
    "Bookmark Job",
    "Email to a Friend",
    "Page",
    "Posting Details",
)


def _strip_chrome_preamble(text: str) -> str:
    """Strip a leading run of known Taleo chrome phrases (nav/breadcrumb)
    that some templates render at the top of the content block, so the
    description starts at the posting itself, not the page furniture."""
    s = text.lstrip()
    changed = True
    while changed:
        changed = False
        for phrase in _CHROME_PREFIX:
            if s[: len(phrase)].casefold() == phrase.casefold():
                s = s[len(phrase) :].lstrip(" \t\r\n:|-")
                changed = True
    return s


# Title-case phrases that recruiters consistently use as JD section
# headers. Order matters only for documentation — the matcher splits on
# any of them, anywhere a line contains them as a discrete phrase.
_SECTION_HEADINGS: tuple[str, ...] = (
    "Who We Are",
    "About the Role",
    "About the Position",
    "About the Job",
    "About Us",
    "Position Summary",
    "Position Overview",
    "Role Overview",
    "Job Overview",
    "Key Responsibilities",
    "Responsibilities",
    "Duties",
    "Full Job Description",
    "Job Description",
    "Qualifications",
    "Required Qualifications",
    "Minimum Qualifications",
    "Preferred Qualifications",
    "Requirements",
    "Skills",
    "Skills and Experience",
    "Experience",
    "Education",
    "What We Offer",
    "Benefits",
    "Compensation",
    "Salary",
    "Additional Information",
    "Equity Statement",
    "Equal Opportunity",
    "How to Apply",
    "Application Instructions",
    "Closing Date",
)


def _split_on_section_keywords(text: str) -> str:
    """Insert paragraph breaks before recognisable JD section headings.

    SFU and other Taleo templates often emit the entire posting as a
    flat blob of text with no inner block tags. The browser uses CSS
    to fake paragraphing, so our paragraph-aware extractor can't help —
    there literally are no paragraphs to preserve. This pass walks the
    rendered text and inserts a blank line + standalone heading line
    before each recognised section keyword phrase, leaving the rest of
    the prose intact.

    Conservative on false positives: we only match a heading when it
    sits at a sentence-ish boundary (start of line, after a period, or
    after `# of openings:`-style label-ends) and is followed by a
    space + an upper-case letter (the start of the section body).
    """
    if not text:
        return text
    # Build one regex alternating all phrases, longest first so
    # "About the Position" wins over "About". re.escape handles spaces.
    phrases = sorted(_SECTION_HEADINGS, key=len, reverse=True)
    pattern = r"(?<!\w)(" + "|".join(re.escape(p) for p in phrases) + r")(?=\s+[A-Z])"
    out_lines: list[str] = []
    for line in text.split("\n"):
        # Insert a marker before each heading match, then split on it.
        # Use \g<1> for the backref so the literal \x1f sentinel
        # doesn't fuse with the group number into "\x1f1".
        marked = re.sub(pattern, "\x1f\\g<1>", line)
        parts = [p.strip() for p in marked.split("\x1f") if p.strip()]
        if len(parts) <= 1:
            out_lines.append(line)
            continue
        for i, part in enumerate(parts):
            # First part is whatever came before the first heading.
            if i == 0:
                out_lines.append(part)
                continue
            # Subsequent parts START with a heading phrase. Split the
            # phrase off onto its own line + blank line, leave the
            # body following it.
            heading_match = re.match(
                r"(" + "|".join(re.escape(p) for p in phrases) + r")\s+(.*)",
                part,
                re.DOTALL,
            )
            if heading_match is None:
                out_lines.append(part)
                continue
            heading, body = heading_match.group(1), heading_match.group(2).strip()
            if out_lines and out_lines[-1] != "":
                out_lines.append("")
            out_lines.append(heading)
            out_lines.append("")
            if body:
                out_lines.append(body)
    return "\n".join(out_lines)


def _render_block(node: Tag) -> str:
    """Walk a container and emit one line per block-level descendant,
    with list-item prefixes + blank lines around headings. Inline text
    nodes get glued to the most recent block."""
    lines: list[str] = []

    def visit(el: Tag) -> None:
        handler = _RENDER_DISPATCH.get(el.name or "", _render_default)
        handler(el, lines, visit)

    visit(node)
    return _collapse_blanks(lines)


def _render_list(el: Tag, lines: list[str], visit: Callable[[Tag], None]) -> None:
    """ul/ol — recurse into direct <li> children, ignore wrapper text."""
    for child in el.find_all("li", recursive=False):
        if isinstance(child, Tag):
            visit(child)


def _render_li(el: Tag, lines: list[str], _visit: Callable[[Tag], None]) -> None:
    txt = _clean(el.get_text(" "))
    if txt:
        lines.append(f"- {txt}")


def _render_heading(el: Tag, lines: list[str], _visit: Callable[[Tag], None]) -> None:
    txt = _clean(el.get_text(" "))
    if not txt:
        return
    if lines and lines[-1] != "":
        lines.append("")  # break from previous block
    lines.append(txt)
    lines.append("")


def _render_paragraph(el: Tag, lines: list[str], visit: Callable[[Tag], None]) -> None:
    """p/div/section/article/tr — if the block has block-level children,
    recurse; otherwise emit the joined inline text as one line."""
    children = [c for c in el.children if isinstance(c, Tag)]
    nested = ("ul", "ol")
    has_block_kids = any(c.name in _BLOCK_TAGS or c.name in nested for c in children)
    if has_block_kids:
        for c in children:
            visit(c)
        return
    txt = _clean(el.get_text(" "))
    if txt:
        lines.append(txt)


def _render_br(_el: Tag, lines: list[str], _visit: Callable[[Tag], None]) -> None:
    if lines and lines[-1] != "":
        lines.append("")


def _render_default(el: Tag, lines: list[str], _visit: Callable[[Tag], None]) -> None:
    """Fallback for unknown inline-ish tags — emit joined text."""
    txt = _clean(el.get_text(" "))
    if txt:
        lines.append(txt)


_RENDER_DISPATCH: dict[str, Callable[[Tag, list[str], Callable[[Tag], None]], None]] = {
    "ul": _render_list,
    "ol": _render_list,
    "li": _render_li,
    "h1": _render_heading,
    "h2": _render_heading,
    "h3": _render_heading,
    "h4": _render_heading,
    "h5": _render_heading,
    "h6": _render_heading,
    "p": _render_paragraph,
    "div": _render_paragraph,
    "section": _render_paragraph,
    "article": _render_paragraph,
    "tr": _render_paragraph,
    "br": _render_br,
}


def _collapse_blanks(lines: list[str]) -> str:
    """Collapse runs of >1 blank line; trim leading/trailing blanks."""
    out: list[str] = []
    for ln in lines:
        if ln == "" and out and out[-1] == "":
            continue
        out.append(ln)
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _find_full_jd_pdf(soup: BeautifulSoup, *, base_url: str) -> str | None:
    """Return the absolute URL of a 'Full Job Description' PDF link, or
    None. Operators follow this link manually until the deferred PDF
    extraction pipeline lands.

    Heuristic: link text mentions 'job description' AND either href ends
    in .pdf, or 'pdf' appears anywhere in href or in the link text.
    Catches both the literal /full.pdf form and Taleo's
    /viewRequisitionPdf?... routing form.
    """
    for link in soup.find_all("a"):
        if not isinstance(link, Tag):
            continue
        href = str(link.get("href") or "")
        text = _clean(link.get_text()).lower()
        if "job description" not in text:
            continue
        href_lower = href.lower()
        if href_lower.endswith(".pdf") or "pdf" in href_lower or "pdf" in text:
            return urljoin(base_url, href)
    return None


def _promote_listing_fields(req: TaleoRequisition) -> None:
    """Fill in listing-derived fields from the structured table when the
    listing row was missing them (handles single-rid fetches)."""
    if not req.employment_type:
        req.employment_type = req.structured_fields.get("Employment Type")
    if not req.location:
        req.location = req.structured_fields.get(
            "Location"
        ) or req.structured_fields.get("Job Location")
    if not req.department:
        req.department = req.structured_fields.get("Department")


# ---------------- helpers ----------------


def _clean(text: str | None) -> str:
    """Collapse whitespace + strip non-breaking spaces."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


# Live SFU template (May 2026) renders the listing as an accordion of
# divs, not a table. Each posting's metadata lives in this container.
_ACCORDION_INFO_CLASS = "oracletaleocwsv2-accordion-head-info"

# Anchors the employment-type field among the metadata divs (it always
# sits between department and location). Matches "Permanent Full Time",
# "Temporary Full Time", "Part Time", "Casual", "Co-op", etc. so the
# parser survives a field being added/removed rather than relying purely
# on position.
_EMPLOYMENT_HINT = re.compile(
    r"\b(full[ -]?time|part[ -]?time|casual|temporary|permanent"
    r"|contract|co-?op|seasonal|term)\b",
    re.IGNORECASE,
)


def _listing_fields(link: Tag) -> tuple[str | None, str | None, str | None]:
    """Read (location, department, employment_type) for one listing entry.

    Two live shapes are supported: the legacy ``<tr>`` table (vendored
    fixtures) and the current SFU accordion (``div`` blocks). We try the
    table first, then fall back to the accordion container.
    """
    tr = link.find_parent("tr")
    if tr is not None:
        return _row_fields(tr)
    info = link.find_parent("div", class_=_ACCORDION_INFO_CLASS)
    if info is not None:
        return _accordion_fields(info)
    return None, None, None


def _accordion_fields(info: Tag) -> tuple[str | None, str | None, str | None]:
    """Pull (location, department, employment_type) out of an accordion
    head-info block.

    The block holds the title <h4> followed by direct-child metadata
    <div>s in the order [department, employment_type, location] (verified
    against the live SIMOFRAS template). We anchor on the employment-type
    field by keyword so a missing/added field doesn't shift the others.
    """
    metas = [
        _clean(d.get_text(" "))
        for d in info.find_all("div", recursive=False)
        if isinstance(d, Tag)
    ]
    metas = [m for m in metas if m]
    if not metas:
        return None, None, None

    emp_idx = next((i for i, m in enumerate(metas) if _EMPLOYMENT_HINT.search(m)), None)
    if emp_idx is not None:
        before = metas[:emp_idx]
        after = metas[emp_idx + 1 :]
        dept = before[0] if before else None
        loc = after[0] if after else None
        return loc, dept, metas[emp_idx]

    # No recognisable employment field — fall back to positional order.
    dept = metas[0]
    emp = metas[1] if len(metas) > 1 else None
    loc = metas[2] if len(metas) > 2 else None
    return loc, dept, emp


def _row_fields(tr: Tag | None) -> tuple[str | None, str | None, str | None]:
    """Read (location, department, employment_type) from a listing row.

    Taleo orders these cells differently across templates; we fall back to
    label-based lookup using common Taleo class hints.
    """
    if tr is None:
        return None, None, None
    cells = [c for c in tr.find_all(["td", "th"]) if isinstance(c, Tag)]
    texts = [_clean(c.get_text()) for c in cells]
    # First non-empty cell is the title (the link itself). The next 3
    # cells map to location / department / employment_type on the live
    # SIMOFRAS template (verified against the May 2026 layout). If the
    # template changes order, the upsert still works (external_id is
    # the unique key); these fields just become None until reparsed.
    fields: list[str] = [t for t in texts if t][1:4]
    loc = fields[0] if len(fields) > 0 else None
    dept = fields[1] if len(fields) > 1 else None
    emp = fields[2] if len(fields) > 2 else None
    return loc, dept, emp


def _find_next_link(soup: BeautifulSoup, *, base_url: str) -> str | None:
    for link in soup.find_all("a"):
        if not isinstance(link, Tag):
            continue
        text = _clean(link.get_text()).lower()
        rel = link.get("rel")
        href = str(link.get("href") or "")
        # Match either a "next" label/title or rel="next"
        if (text == "next" or "next" in (rel or [])) and href:
            return urljoin(base_url + "/", href)
    return None
