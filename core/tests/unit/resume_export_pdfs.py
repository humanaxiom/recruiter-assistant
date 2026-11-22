"""Source-PDF builders shared by the round-4 export tests.

``fitz.Page.insert_text`` draws every character outside Latin-1 as a middle
dot, so a test that wants a SOURCE document holding ``2015-2019`` with a real
en dash, or ``Lukasz`` with a real ``L`` with stroke, cannot use it. These
builders write the content stream by hand:

* a character that cp1252 holds goes in as its WinAnsi byte (en dash is 0x96);
* any other BMP character is given a single-byte code through the font's
  ``/Differences`` array, named ``uniXXXX``, so text extraction returns it.

The font is the non-embedded base-14 Helvetica, like a real word-processor
export. Not a test module: pytest does not collect it.
"""

from __future__ import annotations

from typing import Any

import fitz  # type: ignore[import-untyped]

_LEFT = 72.0
_TOP = 720.0
_STEP = 16.0
_FIRST_FREE_CODE = 1
_LAST_FREE_CODE = 31


def _escape(raw: bytes) -> bytes:
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\(").replace(b")", b"\)")


def _encode(line: str, codes: dict[str, int]) -> bytes:
    out = bytearray()
    for ch in line:
        try:
            out += _escape(ch.encode("cp1252"))
        except UnicodeEncodeError:
            assert ord(ch) <= 0xFFFF, "BMP characters only"
            code = codes.setdefault(ch, _FIRST_FREE_CODE + len(codes))
            assert code <= _LAST_FREE_CODE, "too many distinct non-WinAnsi characters"
            out += b"\%03o" % code
    return bytes(out)


def build_pdf(
    lines: list[str], *, extra: bytes = b"", size: float = 11.0, pages: int = 1
) -> bytes:
    """A one-font PDF with ``lines`` top to bottom, then ``extra`` content.

    ``extra`` is raw content-stream bytes; ``/F`` stands for the font's name
    (``BT /F 22 Tf 1 0 0 1 72 650 Tm [(J) -400 (A)] TJ ET``)."""
    assert pages == 1, "one page is all the round-4 tests need"
    doc: Any = fitz.open()
    try:
        page = doc.new_page()
        page.insert_text((_LEFT, 40.0), " ", fontname="helv")
        font_xref, _ext, _kind, _base, font_name, *_rest = page.get_fonts()[0]
        codes: dict[str, int] = {}
        body = bytearray()
        for i, line in enumerate(lines):
            y = _TOP - i * _STEP
            body += (
                f"BT /{font_name} {size} Tf 1 0 0 1 {_LEFT} {y} Tm (".encode("ascii")
                + _encode(line, codes)
                + b") Tj ET\n"
            )
        body += extra.replace(b"/F ", f"/{font_name} ".encode("ascii"))
        differences = " ".join(f"{c} /uni{ord(ch):04X}" for ch, c in codes.items())
        doc.xref_set_key(
            font_xref,
            "Encoding",
            "<</Type/Encoding/BaseEncoding/WinAnsiEncoding"
            f"/Differences[{differences}]>>",
        )
        stream = page.get_contents()[0]
        doc.update_stream(stream, doc.xref_stream(stream) + b"\n" + bytes(body))
        data: bytes = doc.tobytes()
        return data
    finally:
        doc.close()


def extract(pdf: bytes) -> str:
    """All the text of ``pdf``, pages joined by a newline."""
    doc: Any = fitz.open(stream=pdf, filetype="pdf")
    try:
        return "\n".join(str(p.get_text()) for p in doc)
    finally:
        doc.close()
