"""RED -- round 4: two things an operator reads.

1. A job that grew past ``resume_export_max_applicants`` between the click and
   the build is exported TRUNCATED (the worker builds the first ``cap``). The
   manifest must say so, or the recruiter believes they hold everyone.

   Name the coder must follow in ``src.services.resume_export_service``::

       build_export(applicants, *, blind_review, max_pages_per_file=None,
                    not_exported: int = 0) -> ExportResult

   When ``not_exported > 0`` MANIFEST.txt holds ONE line containing the count
   and the exact phrase ``applicants not exported (over the cap)`` -- e.g.
   ``3 applicants not exported (over the cap)``. With ``0`` no such line.
   The worker passes ``live applicants - cap``; that half is pinned against a
   real Postgres in ``tests/integration/test_resume_export_round4_pg.py``.

2. The ``pg.export_ready_past_expiry`` remedy said expired ZIPs "stay
   downloadable". They are not offered for download once expired. What
   stays is the file on disk, so the remedy says the ZIPs are "retained".
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src import doctor
from tests.unit.resume_export_pdfs import build_pdf

PHRASE = "applicants not exported (over the cap)"


def _applicant(name: str = "Jane Doe") -> Any:
    from src.services.resume_export_service import ExportApplicant, collect_terms

    return ExportApplicant(
        resume_pdf=build_pdf([name, "Analyst with SQL skills"]),
        cover_pdf=None,
        terms=collect_terms(name, None),
    )


def _manifest(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read("MANIFEST.txt").decode("utf-8").splitlines()


def _export(not_exported: int | None, *, blind: bool = False) -> list[str]:
    from src.services.resume_export_service import build_export

    kwargs: dict[str, Any] = {}
    if not_exported is not None:
        kwargs["not_exported"] = not_exported
    result = build_export([_applicant()], blind_review=blind, **kwargs)
    return _manifest(result.data)


# --------------------------------------------------------------- the manifest


@pytest.mark.parametrize("blind", [False, True])
@pytest.mark.parametrize("left_out", [1, 3, 250])
def test_the_manifest_announces_how_many_applicants_were_left_out(
    left_out: int, blind: bool
) -> None:
    lines = [ln for ln in _export(left_out, blind=blind) if PHRASE in ln]

    assert len(lines) == 1, lines
    assert re.search(rf"\b{left_out}\b", lines[0]), lines[0]


@pytest.mark.parametrize("left_out", [0, None])
def test_a_complete_export_says_nothing_about_a_cap(left_out: int | None) -> None:
    lines = _export(left_out)

    assert not [ln for ln in lines if "over the cap" in ln or "not exported" in ln]


def test_the_cap_line_does_not_disturb_the_per_applicant_lines() -> None:
    lines = _export(2)

    assert "applicant-001: anonymized" in lines
    assert len([ln for ln in lines if ln.startswith("applicant-")]) == 1


def test_the_cap_line_carries_no_name() -> None:
    assert not [ln for ln in _export(4) if "Jane" in ln or "Doe" in ln]


def test_the_not_exported_count_is_keyword_only_with_a_zero_default() -> None:
    import inspect

    from src.services.resume_export_service import build_export

    param = inspect.signature(build_export).parameters["not_exported"]

    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default == 0


# ----------------------------------------------------------- doctor wording

_CHECK = "pg.export_ready_past_expiry"


def _acm(value: Any) -> Any:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _pg(overdue: list[dict[str, Any]]) -> MagicMock:
    conn = MagicMock(name="pg")
    conn.fetchval = AsyncMock(side_effect=[0, 0, 0, 0, 0])

    async def _fetch(sql: str, *_a: Any) -> list[dict[str, Any]]:
        lowered = sql.lower()
        if "resume_export_requests" in lowered and "'ready'" in lowered:
            return overdue
        return []

    conn.fetch = AsyncMock(side_effect=_fetch)
    pool = MagicMock(name="pool")
    pool.acquire = MagicMock(return_value=_acm(conn))
    return pool


def _neo() -> MagicMock:
    session = MagicMock(name="session")

    async def _run(*_a: Any, **_k: Any) -> Any:
        result = MagicMock()
        result.single = AsyncMock(return_value={"n": 0})
        return result

    session.run = AsyncMock(side_effect=_run)
    driver = MagicMock(name="driver")
    driver.session = MagicMock(return_value=_acm(session))
    return driver


async def _finding() -> Any:
    findings = await doctor.run_checks(
        pg=_pg([{"id": uuid4()}, {"id": uuid4()}]), neo4j=_neo()
    )
    return next(f for f in findings if f.check == _CHECK)


@pytest.mark.asyncio
async def test_the_overdue_remedy_does_not_claim_a_zip_is_downloadable() -> None:
    f = await _finding()

    assert "downloadable" not in f.remedy.lower()
    assert "downloadable" not in f.detail.lower()


@pytest.mark.asyncio
async def test_the_overdue_remedy_says_the_zips_are_retained() -> None:
    f = await _finding()

    assert "retained" in f.remedy.lower()


@pytest.mark.asyncio
async def test_the_overdue_remedy_still_names_the_sweep() -> None:
    f = await _finding()

    assert "expire_resume_exports" in f.remedy
    assert f.severity == "fail"
    assert f.count == 2
