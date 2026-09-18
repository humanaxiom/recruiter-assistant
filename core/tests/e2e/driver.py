"""Pure, I/O-free HTML-scraping helpers shared between the smoke suite
(``tests/smoke/conftest.py``) and the stress/e2e harness (``tests/e2e/
stress.py``, driven by ``scripts/e2e.sh`` / ``scripts/stress.sh``).

Lifted verbatim (same regexes) from what used to be private duplicates in
``tests/smoke/conftest.py`` and ``tests/smoke/test_product_flows.py`` — see
``tests/unit/test_e2e_driver.py`` for the pinned contract.
"""

from __future__ import annotations

import re

#: The one place this repo's CSRF header name is spelled for the browser-
#: driving test harnesses — import this rather than re-spelling the literal.
CSRF_HEADER = "X-CSRF-Token"

_PAGE_TOKEN_RE = re.compile(r'hx-headers=\'\{"X-CSRF-Token": "([^"]+)"\}\'')
_PILL_PARSED_RE = re.compile(r"pill-parsed")
_WITHDRAW_ID_RE = re.compile(
    r"/resumes/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/withdraw"
)


def page_token(html: str) -> str | None:
    """The page-level CSRF token carried in the ``hx-headers`` attribute."""
    match = _PAGE_TOKEN_RE.search(html)
    return match.group(1) if match else None


def hidden_value(form_html: str, name: str) -> str | None:
    """The ``value`` of a ``<input type="hidden" name="...">`` in a form
    block, whichever order the ``name``/``value`` attributes appear in."""
    match = re.search(
        rf'name="{re.escape(name)}"\s+value="([^"]*)"', form_html
    ) or re.search(rf'value="([^"]*)"\s+name="{re.escape(name)}"', form_html)
    return match.group(1) if match else None


def form_for(html: str, needle: str) -> str | None:
    """The ``<form>...</form>`` block whose markup contains ``needle``."""
    idx = html.find(needle)
    if idx == -1:
        return None
    start = html.rfind("<form", 0, idx)
    if start == -1:
        return None
    end = html.find("</form>", idx)
    if end == -1:
        return None
    return html[start : end + len("</form>")]


def count_parsed_pills(html: str) -> int:
    """How many résumé cards have finished parsing."""
    return len(_PILL_PARSED_RE.findall(html))


def withdraw_ids(html: str) -> list[str]:
    """UUIDs of every ``/resumes/{id}/withdraw`` link, in document order."""
    return _WITHDRAW_ID_RE.findall(html)


def is_parsing(html: str) -> bool:
    """True while a résumé/JD fragment still carries the parsing badge."""
    return "badge-parsing" in html
