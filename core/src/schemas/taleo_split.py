"""Schemas for the Taleo combined-export split (proposal, diagnostics, slices).

Pages are 1-based everywhere. ``ProposalApplicant``/``SplitProposal`` are also
the shape the segmentation LLM is asked to emit, so they ignore extra keys.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ProposalApplicant(BaseModel):
    """One applicant in a split proposal. Pages are 1-based; order matters."""

    model_config = ConfigDict(extra="ignore")

    candidate_name: str = ""
    resume_pages: list[int] = Field(default_factory=list)
    cover_letter_pages: list[int] = Field(default_factory=list)


class SplitProposal(BaseModel):
    """A proposed segmentation of a combined export."""

    model_config = ConfigDict(extra="ignore")

    applicants: list[ProposalApplicant] = Field(default_factory=list)
    ignored_pages: list[int] = Field(default_factory=list)


class ProposalDiagnostics(BaseModel):
    """Findings from validating a proposal against the per-page text."""

    missing: list[int] = Field(default_factory=list)
    duplicated: list[int] = Field(default_factory=list)
    merged: list[int] = Field(default_factory=list)
    cover_only: list[int] = Field(default_factory=list)
    low_text: list[int] = Field(default_factory=list)
    misfiled_cover: list[int] = Field(default_factory=list)

    @property
    def blocking(self) -> bool:
        """Missing, duplicated, merged or misfiled-cover findings block accept;
        cover-only and low-text applicants are excluded and disclosed instead."""
        return bool(
            self.missing or self.duplicated or self.merged or self.misfiled_cover
        )


class ApplicantSlice(BaseModel):
    """One applicant's emitted PDFs, with name-free filenames."""

    index: int
    resume_filename: str
    cover_filename: str | None = None
    resume_pdf: bytes
    cover_pdf: bytes | None = None
