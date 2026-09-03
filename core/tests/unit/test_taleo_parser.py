"""Pure-parser tests for the Taleo careers-page scraper (ADR-046).

Asserted against VENDORED fixtures (tests/fixtures/taleo/*.html), which is the
whole point: HTML scraping is the part of this feature that rots. The Taleo
template has changed before, and when it changes again these fixtures get
refreshed and this suite is what says which parser assumptions broke.

**No network anywhere in this file, or in what it imports.** The parsers are
pure HTML -> data; the fetching half lives behind ``TALEO_ENABLED`` (default
false) and is not imported here. That is why these tests can be part of the
ordinary offline gate rather than something only a live run exercises.

Ported from hris ``tests/unit/test_taleo_parser.py``; the import path is
retargeted to ``src.pipeline.sources.taleo`` and the fixture directory to this
repo's ``core/tests/fixtures/taleo``.
"""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup

from src.pipeline.sources.taleo import (
    TaleoListingRow,
    _extract_description_body,
    _split_on_section_keywords,
    parse_listing_page,
    parse_requisition_page,
)

# Mirrors SFU's live Taleo template shape: the whole posting is wrapped in
# a <header>, the nav bar ("New Search"/"Login") is a <nav>, and a
# "Posting Details" breadcrumb heading precedes the title + JD. A legit
# mid-text "Login" in the body must survive (only leading chrome strips).
_SFU_CHROME_HTML = """
<html><body><header>
  <nav><a class="fa-search">New Search</a><a class="fa-sign-in">Login</a></nav>
  <div id="main-wrapper"><section><div class="container-fluid">
    <h2>Posting Details</h2>
    <div class="row"><div class="col-md-8">
      <h1>Learning Technology Specialist - School of Medicine</h1>
      <p>Employment Duration: Permanent Full Time. Location: Surrey.</p>
      <h2>Who We Are</h2>
      <p>SFU is a leading research university. You will support faculty and
         Login to the LMS daily to publish course content.</p>
    </div></div>
  </div></section></div>
</header></body></html>
"""

# Vendored Taleo markup lives under tests/vendor/, NOT tests/fixtures/, and
# that is load-bearing rather than a naming preference: `.gitignore` carries a
# blunt `fixtures/` rule because a `git add -A` once pushed 99MB of real
# candidate PII to this PUBLIC repo. These pages are public job postings with
# no PII in them at all -- but "is this file PII?" is exactly the judgement the
# blunt rule exists to remove, so the files sit outside its namespace instead
# of earning a negation. Put them back under fixtures/ and they silently stop
# being committed: the suite passes locally on the untracked files and fails in
# CI, which is precisely how this was found.
VENDOR_DIR = Path(__file__).resolve().parents[1] / "vendor" / "taleo"
BASE_URL = "https://tre.tbe.taleo.net"


def _read(name: str) -> str:
    return (VENDOR_DIR / name).read_text(encoding="utf-8")


# ---------------- listing page ----------------


def test_listing_page1_extracts_three_rows() -> None:
    rows, _ = parse_listing_page(_read("listing_page1.html"), base_url=BASE_URL)

    assert len(rows) == 3
    rids = [r.external_id for r in rows]
    assert rids == ["7124", "7127", "7118"]


def test_listing_page1_carries_field_columns() -> None:
    rows, _ = parse_listing_page(_read("listing_page1.html"), base_url=BASE_URL)
    analyst = next(r for r in rows if r.external_id == "7124")
    assert analyst.title == "Analyst, Research Computing Systems"
    assert analyst.location == "Burnaby (Hybrid)"
    assert analyst.department == "Research Computing"
    assert analyst.employment_type == "Temporary Full Time"


def test_listing_external_url_is_absolute() -> None:
    rows, _ = parse_listing_page(_read("listing_page1.html"), base_url=BASE_URL)
    assert all(r.external_url.startswith("https://tre.tbe.taleo.net/") for r in rows)
    # HTML entity in the source (&amp;) must be normalised by the parser.
    assert "&amp;" not in rows[0].external_url
    assert "rid=7124" in rows[0].external_url


def test_listing_page1_finds_next_page_link() -> None:
    _, next_url = parse_listing_page(_read("listing_page1.html"), base_url=BASE_URL)
    assert next_url is not None
    assert "pageNo=2" in next_url
    assert next_url.startswith("https://tre.tbe.taleo.net/")


def test_listing_page2_has_no_next_link() -> None:
    rows, next_url = parse_listing_page(_read("listing_page2.html"), base_url=BASE_URL)
    assert len(rows) == 1
    assert next_url is None  # end of pagination


# ---------------- listing page (live accordion template) ----------------


def test_accordion_listing_extracts_one_row_per_posting() -> None:
    """The live SFU template is an accordion of <div>s, not a <tr> table.
    Each posting also carries View/Apply links with the same rid — those
    must dedup so we get one row per posting, not three."""
    rows, _ = parse_listing_page(_read("listing_accordion.html"), base_url=BASE_URL)
    assert len(rows) == 2
    assert [r.external_id for r in rows] == ["7113", "7124"]
    # The head title link wins over the "View"/"Apply" link text.
    assert rows[0].title.startswith("Administrative Coordinator")
    assert rows[1].title == "Analyst, Research Computing Systems"


def test_accordion_listing_parses_department_location_employment() -> None:
    """Regression: department (and location/employment) were always NULL
    because the table-based row reader never matched the accordion. The
    metadata divs are ordered [department, employment_type, location]."""
    rows, _ = parse_listing_page(_read("listing_accordion.html"), base_url=BASE_URL)

    admin = next(r for r in rows if r.external_id == "7113")
    assert admin.department == "School of Medicine - Operations"
    assert admin.employment_type == "Permanent Full Time"
    assert admin.location == "Surrey"

    analyst = next(r for r in rows if r.external_id == "7124")
    assert analyst.department == "Research Computing"
    assert analyst.employment_type == "Temporary Full Time"
    assert analyst.location == "Burnaby (Hybrid)"


# ---------------- requisition page ----------------


def _listing_for(rid: str = "7124") -> TaleoListingRow:
    """Listing rows feed forward into requisition parsing."""
    return TaleoListingRow(
        external_id=rid,
        title="Analyst, Research Computing Systems",
        location="Burnaby (Hybrid)",
        department="Research Computing",
        employment_type="Temporary Full Time",
        external_url=f"https://tre.tbe.taleo.net/tre01/ats/careers/v2/viewRequisition?org=SIMOFRAS&cws=37&rid={rid}",
    )


def test_requisition_extracts_structured_fields_from_dl() -> None:
    req = parse_requisition_page(_read("requisition_7124.html"), listing=_listing_for())

    assert req.structured_fields["Employment Type"] == "Temporary Full Time"
    assert req.structured_fields["Department"] == "Research Computing"
    assert req.structured_fields["Position Grade"] == "10"
    assert req.structured_fields["Closing Date"] == "June 9, 2026"
    # HTML entity collapses to the literal character.
    assert "&amp;" not in req.structured_fields["Union"]
    assert "&" in req.structured_fields["Union"]


def test_requisition_captures_inline_description() -> None:
    req = parse_requisition_page(_read("requisition_7124.html"), listing=_listing_for())
    assert "Research Computing Systems" in req.description_raw
    assert "Kubernetes" in req.description_raw
    # Inline whitespace (spaces/tabs) inside a single block still
    # collapses to one space. Cross-block newlines are PRESERVED on
    # purpose — see test_requisition_preserves_paragraph_structure for
    # the richer assertion. This fixture's body is one <div> of prose,
    # so it renders as one line with no double-spaces.
    assert "  " not in req.description_raw


def test_requisition_preserves_paragraph_structure() -> None:
    """Real Taleo pages have <h2> sections + <ul><li> bullet lists.
    The Phase-12.1 fix replaces a flat ``get_text(separator=" ")`` with
    a paragraph-aware walker so the JD body has structure:

      - Each <p>/<h2>/<li> renders on its own line.
      - List items are prefixed with ``- ``.
      - Blank lines separate headings from following content.

    This matters for two reasons: humans see a readable JD, and the
    chunker can find section boundaries so the LLM extractor's
    requirements don't blur together.
    """
    req = parse_requisition_page(
        _read("requisition_8001_structured.html"), listing=_listing_for()
    )
    lines = req.description_raw.split("\n")

    # Section headings landed on their own lines.
    assert "About the Role" in lines
    assert "Qualifications" in lines
    assert "What We Offer" in lines

    # Bullets got the "- " prefix.
    bullets = [ln for ln in lines if ln.startswith("- ")]
    assert any("REST" in b for b in bullets)
    assert any("SQL Server" in b for b in bullets)
    assert any("agile" in b for b in bullets)

    # No section runs into the next one (the flat-text bug). The
    # "Qualifications" heading is followed by its own paragraph, not
    # by the bullets on the same line.
    quals_idx = lines.index("Qualifications")
    assert lines[quals_idx + 1] == ""  # blank line after heading
    assert "Bachelor" in lines[quals_idx + 2]


def test_split_on_section_keywords_breaks_flat_sfu_body() -> None:
    """Real SFU Taleo postings have NO <p>/<h2>/<li> tags — the entire
    posting is one giant text node, browser-side CSS fakes paragraphs.
    For that case the keyword splitter is the only thing that
    reintroduces structure.
    """
    flat = (
        "Application Developer Employment Duration Permanent Full Time "
        "Who We Are Simon Fraser University is a leading research university. "
        "About the Role The Application Developer analyzes and designs systems. "
        "Qualifications Bachelor's degree in Computing Science. "
        "What We Offer Hybrid-work program. Employer paid pension."
    )
    result = _split_on_section_keywords(flat)
    lines = result.split("\n")

    # Section headings landed on their own lines, in order.
    assert "Who We Are" in lines
    assert "About the Role" in lines
    assert "Qualifications" in lines
    assert "What We Offer" in lines
    # Body text immediately follows each heading.
    qual_idx = lines.index("Qualifications")
    assert lines[qual_idx + 1] == ""  # blank line after heading
    assert "Bachelor" in lines[qual_idx + 2]


def test_split_on_section_keywords_is_idempotent_when_already_structured() -> None:
    """Running the splitter on an already-sectioned document doesn't
    re-split sentences that happen to contain a keyword mid-flow."""
    structured = (
        "About the Role\n"
        "\n"
        "Manage hiring pipelines and review qualifications.\n"
        "\n"
        "Qualifications\n"
        "\n"
        "Bachelor's degree required."
    )
    result = _split_on_section_keywords(structured)
    # No duplicated headings.
    assert result.count("Qualifications") == 1
    assert result.count("About the Role") == 1


def test_split_on_section_keywords_skips_mid_sentence_matches() -> None:
    """'Qualifications' followed by a lower-case word is not a heading
    — it's the end of a sentence. Don't break there."""
    text = (
        "We will assess your qualifications carefully during the review. "
        "Qualifications Bachelor's degree in Computing Science."
    )
    result = _split_on_section_keywords(text)
    # Lowercase 'qualifications' in the first sentence stays inline.
    assert "qualifications carefully" in result
    # The capitalised "Qualifications" header DID get split out.
    assert "\nQualifications\n" in result


def test_requisition_discovers_pdf_link() -> None:
    req = parse_requisition_page(_read("requisition_7124.html"), listing=_listing_for())
    assert req.pdf_url is not None
    assert req.pdf_url.endswith("rid=7124")
    assert "viewRequisitionPdf" in req.pdf_url


def test_requisition_inherits_listing_fields_when_dl_misses() -> None:
    """When the requisition body doesn't redundantly carry a field, the
    listing-row value wins (location/department on this fixture come
    from the listing, not the dl)."""
    req = parse_requisition_page(_read("requisition_7124.html"), listing=_listing_for())
    assert req.location == "Burnaby (Hybrid)"
    assert req.department == "Research Computing"


def test_extract_description_strips_nav_and_breadcrumb_chrome() -> None:
    """SFU's template leaks 'New Search Login Page Posting Details ...' into
    the JD. The nav is decomposed and the breadcrumb preamble stripped, but
    a legitimate mid-text 'Login' in the posting survives."""
    out = _extract_description_body(BeautifulSoup(_SFU_CHROME_HTML, "lxml"))
    assert "New Search" not in out
    assert "Posting Details" not in out
    assert not out.lstrip().startswith("Login")
    # JD content kept — title, a real section, and a mid-text "Login".
    assert "Learning Technology Specialist" in out
    assert "Who We Are" in out
    assert "Login to the LMS" in out


def test_the_vendored_pages_are_actually_tracked_by_git() -> None:
    """The failure this file already caused once, made impossible to repeat.

    ``.gitignore`` carries a blunt ``fixtures/`` rule (99MB of real candidate
    PII was once pushed to this PUBLIC repo by a ``git add -A``). Vendored
    Taleo markup originally landed under ``tests/fixtures/taleo/`` and was
    therefore never committed — the suite passed locally against untracked
    files on disk and failed in CI, which is the worst shape a test-data bug
    can take: green where you are looking, red where you are not.

    Asserting the files are TRACKED, not merely present, is what closes it.
    A future tidy-up that moves them back under ``fixtures/`` fails here
    rather than in someone else's CI run.
    """
    import subprocess

    root = Path(__file__).resolve().parents[3]
    tracked = subprocess.run(
        ["git", "ls-files", "core/tests/vendor/taleo"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    on_disk = sorted(p.name for p in VENDOR_DIR.glob("*.html"))
    assert on_disk, "no vendored Taleo pages on disk"
    assert sorted(Path(t).name for t in tracked if t.endswith(".html")) == on_disk, (
        "vendored Taleo pages exist on disk but are not tracked by git — they "
        "are almost certainly under a path matching .gitignore's `fixtures/` "
        "rule, so CI will run this suite with no HTML to parse"
    )
