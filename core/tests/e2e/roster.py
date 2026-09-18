"""Synthetic Taleo "All Candidates" roster CSV generator for the stress
harness — round-trips through the REAL ``parse_candidate_csv``
(``src.services.bulk_ingest_service``), the exact parser
``candidate_roster_service`` feeds. See ``tests/unit/test_stress_roster.py``
for the pinned contract.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass


@dataclass(frozen=True)
class RosterSpec:
    """One synthetic candidate row to render into a roster CSV."""

    name: str
    email: str
    work_authorization_text: str
    apsa: bool
    cupe: bool
    sfu_id: str | None = None
    submission_date: str | None = None


_HEADERS = [
    "Name",
    "Email",
    "SFU ID",
    "Work Authorization",
    "APSA Internal",
    "CUPE Internal",
    "Submission Date",
]


def build_roster_csv(specs: list[RosterSpec]) -> str:
    """Render ``specs`` into a Taleo-shaped candidate-roster CSV (str)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_HEADERS)
    for spec in specs:
        writer.writerow(
            [
                spec.name,
                spec.email,
                spec.sfu_id or "",
                spec.work_authorization_text,
                "I" if spec.apsa else "",
                "I" if spec.cupe else "",
                spec.submission_date or "",
            ]
        )
    return buf.getvalue()
