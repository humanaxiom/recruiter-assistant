"""doctor: a CAS-on deployment with no localhost CAS origin is a failure.

Without ``CAS_LOCAL_SERVICE_BASE_URL`` the box's own operator, on
http://localhost:29500, is redirected to the PUBLIC CAS origin (sfuai.ca), which
hairpin NAT makes unreachable from inside the LAN: ERR_CONNECTION_TIMED_OUT on
the login page. It regressed silently on 2026-11-24 because the setting lived
only in an overlay file a deploy could omit. A state defect, so doctor owns it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from src import doctor


def _acm(value: Any) -> Any:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=value)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _pg() -> MagicMock:
    conn = MagicMock(name="pg")
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetch = AsyncMock(return_value=[])
    pool = MagicMock(name="pool")
    pool.acquire = MagicMock(return_value=_acm(conn))
    return pool


def _neo() -> MagicMock:
    result = MagicMock()
    result.single = AsyncMock(return_value={"n": 0})
    session = MagicMock()
    session.run = AsyncMock(return_value=result)
    driver = MagicMock()
    driver.session = MagicMock(return_value=_acm(session))
    return driver


def _settings(*, cas: bool, local: str) -> MagicMock:
    return MagicMock(
        cas_enabled=cas,
        auth_enabled=cas,
        cas_local_service_base_url=local,
        cas_local_frontend_base_url=local,
    )


_CHECK = "deploy.cas_local_origin_unset"


async def test_cas_on_without_a_localhost_origin_fails() -> None:
    findings = await doctor.run_checks(
        pg=_pg(), neo4j=_neo(), settings=_settings(cas=True, local="")
    )
    hit = [f for f in findings if f.check == _CHECK]
    assert len(hit) == 1
    assert hit[0].severity == "fail"
    assert "compose.cas.yml" in hit[0].remedy or "docker-compose.yml" in hit[0].remedy


async def test_cas_on_with_a_localhost_origin_is_clean() -> None:
    findings = await doctor.run_checks(
        pg=_pg(),
        neo4j=_neo(),
        settings=_settings(cas=True, local="http://localhost:29800"),
    )
    assert not [f for f in findings if f.check == _CHECK]


async def test_cas_off_is_not_this_checks_business() -> None:
    findings = await doctor.run_checks(
        pg=_pg(), neo4j=_neo(), settings=_settings(cas=False, local="")
    )
    assert not [f for f in findings if f.check == _CHECK]
