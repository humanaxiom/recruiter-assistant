"""Unit tests for the pure per-résumé cover-letter pairing (FU-3 Slice 2).

``src.services.bulk_ingest_service`` is a NEW pure, I/O-free module ported from
hris ``apps/api/src/api/services/bulk_ingest_service.py`` (the pairing half).
DEVIATION from hris: an uploaded file here is a plain ``tuple[str, bytes]`` =
``(filename, content)`` (what ``expand_zip_entries`` returns), not an
``ExpandedFile`` dataclass.

RED half of the TDD cycle — the module does not exist yet, so the whole file
fails at collection.

**Ambiguities this file locks:**

* ``_classify(filename) -> ("cover"|"resume", base)`` matches the LONGEST cover
  suffix first (``_cover_letter`` beats ``_cover``), is case-insensitive, and
  strips a trailing separator from the shared base. A stem with no known suffix
  is a résumé whose base is the whole stem.
* ``pair_applicants(files, *, manifest=None) -> PairingResult`` pairs each cover
  letter to its résumé by the filename convention, PRESERVES résumé input order,
  and DEMOTES a cover-named file with no matching résumé to a standalone résumé
  carrying a STATIC-English ``note`` (never dropped, never filename-derived).
"""

from __future__ import annotations

import json

import pytest

from src.errors import AppError
from src.services.bulk_ingest_service import (
    ApplicantFiles,
    ManifestError,
    PairingResult,
    _classify,
    basename_lower,
    pair_applicants,
    parse_pairing_manifest,
)

_PDF = b"%PDF-1.4 body"


def _f(name: str, body: bytes = _PDF) -> tuple[str, bytes]:
    return (name, body)


# ── basename_lower / _classify ───────────────────────────────────────────


def test_basename_lower_strips_folder_and_lowercases() -> None:
    assert basename_lower("Some/Folder\\Jane_Resume.PDF") == "jane_resume.pdf"


def test_classify_plain_name_is_a_resume() -> None:
    assert _classify("jane.pdf") == ("resume", "jane")


def test_classify_resume_suffix() -> None:
    assert _classify("jane_resume.pdf") == ("resume", "jane")


def test_classify_cv_suffix() -> None:
    assert _classify("jane_cv.pdf") == ("resume", "jane")


def test_classify_cover_letter_suffix() -> None:
    assert _classify("jane_cover_letter.pdf") == ("cover", "jane")


def test_classify_coverletter_variant() -> None:
    assert _classify("jane_coverletter.pdf") == ("cover", "jane")


def test_classify_cover_note_variant() -> None:
    assert _classify("jane_cover_note.pdf") == ("cover", "jane")


def test_classify_bare_cover_variant() -> None:
    assert _classify("jane_cover.pdf") == ("cover", "jane")


def test_classify_longest_cover_suffix_wins() -> None:
    # ``_cover_letter`` must win over ``_cover`` so the shared base is ``jane``.
    assert _classify("jane_cover_letter.pdf") == ("cover", "jane")


def test_classify_is_case_insensitive() -> None:
    assert _classify("Jane_Cover_Letter.PDF") == ("cover", "jane")


def test_classify_does_not_false_match_inside_a_word() -> None:
    # ``discover`` ends with ``cover`` but not ``_cover`` — it is a résumé.
    assert _classify("discover.pdf") == ("resume", "discover")


# ── pair_applicants ──────────────────────────────────────────────────────


def test_pair_resume_alone_has_no_cover() -> None:
    result = pair_applicants([_f("jane.pdf")])
    assert isinstance(result, PairingResult)
    assert len(result.pairs) == 1
    assert result.pairs[0].resume[0] == "jane.pdf"
    assert result.pairs[0].cover_letter is None
    assert result.pairs[0].note is None


def test_pair_resume_and_cover_letter_pair_up() -> None:
    result = pair_applicants([_f("jane_resume.pdf"), _f("jane_cover_letter.pdf")])
    assert len(result.pairs) == 1
    pair = result.pairs[0]
    assert pair.resume[0] == "jane_resume.pdf"
    assert pair.cover_letter is not None
    assert pair.cover_letter[0] == "jane_cover_letter.pdf"
    assert pair.note is None


def test_pair_cv_with_cover_variant() -> None:
    result = pair_applicants([_f("bob_cv.pdf"), _f("bob_cover.pdf")])
    assert len(result.pairs) == 1
    assert result.pairs[0].cover_letter is not None
    assert result.pairs[0].cover_letter[0] == "bob_cover.pdf"


def test_pair_is_case_insensitive() -> None:
    result = pair_applicants([_f("Jane_Resume.PDF"), _f("JANE_COVER_LETTER.pdf")])
    assert len(result.pairs) == 1
    assert result.pairs[0].cover_letter is not None


def test_orphan_cover_is_demoted_to_a_resume_with_a_note() -> None:
    result = pair_applicants([_f("stray_cover_letter.pdf")])
    assert len(result.pairs) == 1
    pair = result.pairs[0]
    # Never dropped — ingested as a résumé on its own...
    assert pair.resume[0] == "stray_cover_letter.pdf"
    assert pair.cover_letter is None
    # ...carrying a note.
    assert pair.note is not None


def test_orphan_cover_note_is_static_english_never_filename_derived() -> None:
    """Blind invariant: the demote note is a fixed English string with NO
    filename-derived PII in it."""
    result = pair_applicants([_f("Zzyzxqrst_Wibblesworth_cover.pdf")])
    note = result.pairs[0].note
    assert note == (
        "looked like a cover letter but had no matching résumé; " "ingested as a résumé"
    )
    assert "zzyzxqrst" not in note.lower()
    assert "wibblesworth" not in note.lower()


def test_pair_preserves_resume_input_order() -> None:
    result = pair_applicants(
        [
            _f("charlie_resume.pdf"),
            _f("alice_resume.pdf"),
            _f("alice_cover_letter.pdf"),
            _f("bob_resume.pdf"),
        ]
    )
    order = [p.resume[0] for p in result.pairs]
    assert order == ["charlie_resume.pdf", "alice_resume.pdf", "bob_resume.pdf"]


def test_plain_bulk_upload_no_covers_is_unchanged() -> None:
    files = [_f("a.pdf"), _f("b.pdf"), _f("c.pdf")]
    result = pair_applicants(files)
    assert len(result.pairs) == 3
    assert all(p.cover_letter is None for p in result.pairs)
    assert all(p.note is None for p in result.pairs)
    assert result.rejected == []


def test_carries_the_original_bytes_through() -> None:
    result = pair_applicants(
        [_f("jane_resume.pdf", b"resume-bytes"), _f("jane_cover.pdf", b"cover-bytes")]
    )
    pair = result.pairs[0]
    assert pair.resume[1] == b"resume-bytes"
    assert pair.cover_letter is not None
    assert pair.cover_letter[1] == b"cover-bytes"


def test_applicant_files_is_frozen() -> None:
    import dataclasses

    import pytest

    pair = ApplicantFiles(resume=_f("a.pdf"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        pair.note = "mutated"  # type: ignore[misc]


# ── manifest (Slice-3 plumbing; the pure helper is still covered here) ────


def test_manifest_pairs_resume_to_named_cover() -> None:
    files = [_f("r1.pdf"), _f("c1.pdf")]
    result = pair_applicants(files, manifest={"r1.pdf": "c1.pdf"})
    assert len(result.pairs) == 1
    assert result.pairs[0].resume[0] == "r1.pdf"
    assert result.pairs[0].cover_letter is not None
    assert result.pairs[0].cover_letter[0] == "c1.pdf"


def test_manifest_names_a_missing_resume_becomes_a_rejected_row() -> None:
    result = pair_applicants([_f("r1.pdf")], manifest={"ghost.pdf": None})
    assert result.rejected
    name, reason = result.rejected[0]
    assert name == "ghost.pdf"
    assert reason


def test_manifest_names_a_missing_cover_adds_a_static_note() -> None:
    result = pair_applicants([_f("r1.pdf")], manifest={"r1.pdf": "ghost_cover.pdf"})
    assert len(result.pairs) == 1
    pair = result.pairs[0]
    assert pair.cover_letter is None
    assert pair.note == "a cover letter named in the manifest wasn't in the upload"


# ── parse_pairing_manifest (FU-3 Slice 3) ────────────────────────────────


def _manifest_bytes(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


def test_parse_manifest_valid_returns_resume_to_cover_map() -> None:
    blob = _manifest_bytes(
        {
            "applicants": [
                {
                    "resume_file": "jane_resume.pdf",
                    "cover_letter_file": "jane_cover.pdf",
                    "cover_letter_flag": "Yes",
                }
            ]
        }
    )
    assert parse_pairing_manifest(blob) == {"jane_resume.pdf": "jane_cover.pdf"}


def test_parse_manifest_falsy_flag_forces_no_cover_even_if_named() -> None:
    blob = _manifest_bytes(
        {
            "applicants": [
                {
                    "resume_file": "jane_resume.pdf",
                    "cover_letter_file": "jane_cover.pdf",
                    "cover_letter_flag": "No",
                }
            ]
        }
    )
    assert parse_pairing_manifest(blob) == {"jane_resume.pdf": None}


def test_parse_manifest_missing_flag_defaults_to_using_named_cover() -> None:
    blob = _manifest_bytes(
        {
            "applicants": [
                {
                    "resume_file": "jane_resume.pdf",
                    "cover_letter_file": "jane_cover.pdf",
                }
            ]
        }
    )
    assert parse_pairing_manifest(blob) == {"jane_resume.pdf": "jane_cover.pdf"}


def test_parse_manifest_applicant_without_resume_file_is_skipped() -> None:
    blob = _manifest_bytes(
        {
            "applicants": [
                {"cover_letter_file": "orphan_cover.pdf", "cover_letter_flag": "Yes"},
                {"resume_file": "  ", "cover_letter_flag": "Yes"},
                {"resume_file": "kept_resume.pdf"},
            ]
        }
    )
    assert parse_pairing_manifest(blob) == {"kept_resume.pdf": None}


def test_parse_manifest_keys_are_basename_lower() -> None:
    blob = _manifest_bytes(
        {
            "applicants": [
                {
                    "resume_file": "Some/Folder\\Jane_Resume.PDF",
                    "cover_letter_file": "Some/Folder\\Jane_Cover.PDF",
                    "cover_letter_flag": "yes",
                }
            ]
        }
    )
    assert parse_pairing_manifest(blob) == {"jane_resume.pdf": "jane_cover.pdf"}


def test_parse_manifest_malformed_json_raises_manifest_error() -> None:
    with pytest.raises(ManifestError):
        parse_pairing_manifest(b"{not valid json")


def test_parse_manifest_non_dict_raises_manifest_error() -> None:
    with pytest.raises(ManifestError):
        parse_pairing_manifest(b"[1, 2, 3]")


def test_parse_manifest_missing_applicants_array_raises_manifest_error() -> None:
    with pytest.raises(ManifestError):
        parse_pairing_manifest(_manifest_bytes({"not_applicants": []}))


def test_parse_manifest_oversize_raises_manifest_error() -> None:
    oversize = b'{"applicants": []}' + b" " * (1024 * 1024 + 1)
    with pytest.raises(ManifestError):
        parse_pairing_manifest(oversize)


def test_manifest_error_is_an_app_error_with_status_422() -> None:
    err = ManifestError("bad manifest")
    assert isinstance(err, AppError)
    assert err.status == 422


# ── pair_applicants(..., manifest=parsed) integration ────────────────────


def test_manifest_pairs_without_convention_suffix() -> None:
    """The manifest pairs ``jane_resume`` ↔ ``jane_cover`` even though the cover
    lacks the ``_cover_letter`` convention suffix."""
    files = [_f("jane_resume.pdf"), _f("jane_cover.pdf")]
    manifest = parse_pairing_manifest(
        _manifest_bytes(
            {
                "applicants": [
                    {
                        "resume_file": "jane_resume.pdf",
                        "cover_letter_file": "jane_cover.pdf",
                        "cover_letter_flag": "Yes",
                    }
                ]
            }
        )
    )
    result = pair_applicants(files, manifest=manifest)
    assert len(result.pairs) == 1
    assert result.pairs[0].resume[0] == "jane_resume.pdf"
    assert result.pairs[0].cover_letter is not None
    assert result.pairs[0].cover_letter[0] == "jane_cover.pdf"


def test_manifest_named_absent_resume_is_rejected_row() -> None:
    manifest = parse_pairing_manifest(
        _manifest_bytes(
            {"applicants": [{"resume_file": "ghost.pdf", "cover_letter_flag": "No"}]}
        )
    )
    result = pair_applicants([_f("present.pdf")], manifest=manifest)
    assert any(name == "ghost.pdf" for name, _ in result.rejected)


def test_files_not_named_in_manifest_fall_back_to_convention() -> None:
    """A manifest that only names jane still lets bob pair by convention."""
    files = [
        _f("jane_resume.pdf"),
        _f("jane_cover.pdf"),
        _f("bob_resume.pdf"),
        _f("bob_cover_letter.pdf"),
    ]
    manifest = parse_pairing_manifest(
        _manifest_bytes(
            {
                "applicants": [
                    {
                        "resume_file": "jane_resume.pdf",
                        "cover_letter_file": "jane_cover.pdf",
                        "cover_letter_flag": "Yes",
                    }
                ]
            }
        )
    )
    result = pair_applicants(files, manifest=manifest)
    by_resume = {p.resume[0]: p for p in result.pairs}
    assert by_resume["jane_resume.pdf"].cover_letter is not None
    assert by_resume["jane_resume.pdf"].cover_letter[0] == "jane_cover.pdf"
    # bob was not in the manifest → paired by the filename convention.
    assert by_resume["bob_resume.pdf"].cover_letter is not None
    assert by_resume["bob_resume.pdf"].cover_letter[0] == "bob_cover_letter.pdf"
