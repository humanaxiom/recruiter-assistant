"""External job sources (ADR-046).

Currently one: the Taleo careers-page parsers. Anything that actually reaches
the network lives behind ``TALEO_ENABLED``, which defaults to ``false`` — a
fresh checkout, a CI run and an airgapped deployment never egress for jobs.
"""

from __future__ import annotations

from src.pipeline.sources.taleo import (
    TaleoClient,
    TaleoHostNotAllowedError,
    TaleoListingRow,
    TaleoRequisition,
    parse_listing_page,
    parse_requisition_page,
)

__all__ = [
    "TaleoClient",
    "TaleoHostNotAllowedError",
    "TaleoListingRow",
    "TaleoRequisition",
    "parse_listing_page",
    "parse_requisition_page",
]
