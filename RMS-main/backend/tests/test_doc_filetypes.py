"""Direct unit tests for the document type sniffer
(`docs/modules/documentation/spec-attachments.md` §4.3, falsifiers F3b and F4c).

WHY THESE ARE UNIT TESTS AND NOT API TESTS. §0.5: there is no multipart parser
in this project, so the client never declares a content type -- the stored
`content_type` and `viewer` are computed here, from the leading bytes, and
nowhere else. That makes `sniff()` the single point where an uploaded byte
string becomes a rendering instruction, and the property F3b pins is a property
of THIS FUNCTION over its whole input domain, not of one route response.

The load-bearing assertion in this file is
`test_sniff_can_never_emit_a_rendering_content_type`: the emitted content type
is drawn from a closed set that contains neither `text/html` nor
`image/svg+xml`. An SVG network diagram and an HTML export are both ACCEPTED --
they decode as UTF-8 and land in the text branch -- and both are stored as
`text/plain`, so the console shows their markup as characters and never asks a
browser to parse them. That is the whole of the containment §0.4 calls wall 2.
"""

import pytest

from app.modules.documentation.filetypes import (
    ACCEPTED_CONTENT_TYPES,
    MAX_TEXT_PREVIEW_BYTES,
    VIEWER_KINDS,
    UnsupportedFileType,
    sniff,
)

PDF = b"%PDF-1.7\n1 0 obj\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"
GIF87 = b"GIF87a" + b"\x00" * 16
GIF89 = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"VP8 " + b"\x00" * 16
ZIP = b"PK\x03\x04" + b"\x00" * 26
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 24


# --------------------------------------------------------------------------- #
# §4.3's table, row by row (F4c).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("data", "filename", "content_type", "viewer"),
    [
        (PDF, "contract.pdf", "application/pdf", "pdf"),
        (PNG, "rack.png", "image/png", "image"),
        (JPEG, "rack.jpg", "image/jpeg", "image"),
        (GIF87, "old.gif", "image/gif", "image"),
        (GIF89, "anim.gif", "image/gif", "image"),
        (WEBP, "diagram.webp", "image/webp", "image"),
        (
            ZIP,
            "report.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "none",
        ),
        (
            ZIP,
            "sheet.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "none",
        ),
        (
            ZIP,
            "deck.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "none",
        ),
        (ZIP, "network.vsdx", "application/vnd.ms-visio.drawing", "none"),
        (ZIP, "bundle.zip", "application/zip", "none"),
        (ZIP, "no-extension", "application/zip", "none"),
        (OLE, "legacy.doc", "application/msword", "none"),
        (OLE, "legacy.xls", "application/vnd.ms-excel", "none"),
        (OLE, "legacy.ppt", "application/vnd.ms-powerpoint", "none"),
        (OLE, "legacy.vsd", "application/vnd.visio", "none"),
        (OLE, "mystery.bin", "application/x-ole-storage", "none"),
        (b"# Runbook\n\nrestore steps", "runbook.md", "text/markdown", "markdown"),
        (b"# Runbook\n", "runbook.markdown", "text/markdown", "markdown"),
        (b"host,ip\na,10.0.0.1\n", "hosts.csv", "text/csv", "text"),
        (b"interface Gi0/1\n", "firewall.cfg", "text/plain", "text"),
        (b"plain", "notes.txt", "text/plain", "text"),
    ],
)
def test_sniff_matches_the_spec_table(
    data: bytes, filename: str, content_type: str, viewer: str
) -> None:
    assert sniff(data, filename) == (content_type, viewer)


def test_extension_case_does_not_change_the_answer() -> None:
    assert sniff(ZIP, "REPORT.DOCX")[1] == "none"
    assert sniff(b"# t", "README.MD") == ("text/markdown", "markdown")


# --------------------------------------------------------------------------- #
# F3b -- the containment property, asserted over the whole accepted corpus.
# --------------------------------------------------------------------------- #

# Every branch of §4.3's table plus the three types that would be dangerous if
# the sniffer ever named a rendering engine for them.
_CORPUS: list[tuple[bytes, str]] = [
    (PDF, "a.pdf"),
    (PNG, "a.png"),
    (JPEG, "a.jpg"),
    (GIF89, "a.gif"),
    (WEBP, "a.webp"),
    (ZIP, "a.docx"),
    (ZIP, "a.xlsx"),
    (ZIP, "a.pptx"),
    (ZIP, "a.vsdx"),
    (ZIP, "a.zip"),
    (OLE, "a.doc"),
    (OLE, "a.xls"),
    (OLE, "a.bin"),
    (b"# md", "a.md"),
    (b"a,b\n", "a.csv"),
    (b"text", "a.txt"),
    # The three that matter. Each is real text, so each is ACCEPTED.
    (b"<html><body><script>alert(1)</script></body></html>", "export.html"),
    (b"<html>", "export.htm"),
    (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "net.svg"),
    (b'<?xml version="1.0"?><root/>', "config.xml"),
    (b'<?xml version="1.0"?><mxfile/>', "diagram.drawio"),
    (b"x" * (MAX_TEXT_PREVIEW_BYTES + 1), "huge.md"),
]


def test_sniff_can_never_emit_a_rendering_content_type() -> None:
    """F3b. The set of content types `sniff` can produce is CLOSED, and neither
    `text/html` nor `image/svg+xml` is in it -- for any input, ever.

    Asserted two ways on purpose. The set membership is the property that
    survives a future edit (a new branch returning a new type fails here even
    if nobody thought about HTML); the by-name assertion is the property a
    reviewer can read without resolving the set. Deleting either leaves the
    other, deleting both leaves the console's render map (F3c) standing alone,
    which §0.4 says explicitly is not enough.
    """
    forbidden = {"text/html", "image/svg+xml", "application/xhtml+xml", "text/xml"}
    for data, filename in _CORPUS:
        content_type, viewer = sniff(data, filename)
        assert content_type in ACCEPTED_CONTENT_TYPES, (filename, content_type)
        assert content_type not in forbidden, (filename, content_type)
        assert viewer in VIEWER_KINDS, (filename, viewer)


@pytest.mark.parametrize(
    "filename", ["export.html", "export.htm", "net.svg", "config.xml", "diagram.drawio"]
)
def test_html_svg_and_xml_are_accepted_and_stored_as_plain_text(filename: str) -> None:
    """F3b, stated as the operator sees it: these files are USEFUL and they are
    kept. What they are not is rendered -- `text/plain` + `viewer="text"` is
    what puts their markup in a `<pre>` instead of in a parser."""
    body = b'<svg xmlns="x"><script>alert(1)</script></svg>'
    assert sniff(body, filename) == ("text/plain", "text")


# --------------------------------------------------------------------------- #
# F4a/F4b -- the extension never overrides the bytes.
# --------------------------------------------------------------------------- #


def test_a_zip_named_pdf_is_a_zip() -> None:
    assert sniff(ZIP, "report.pdf") == ("application/zip", "none")


def test_a_pdf_named_txt_is_a_pdf() -> None:
    assert sniff(PDF, "notes.txt") == ("application/pdf", "pdf")


def test_a_png_named_svg_is_a_png_not_an_svg() -> None:
    assert sniff(PNG, "diagram.svg") == ("image/png", "image")


# --------------------------------------------------------------------------- #
# The text branch's edges (F4c).
# --------------------------------------------------------------------------- #


def test_text_at_the_preview_ceiling_is_still_previewable() -> None:
    assert sniff(b"x" * MAX_TEXT_PREVIEW_BYTES, "big.txt") == ("text/plain", "text")


def test_text_over_the_preview_ceiling_is_stored_but_not_previewable() -> None:
    """The ceiling is a RENDERING limit, not an acceptance one: 25 MiB of text
    pushed into the DOM hangs the tab, so the file is kept and downloadable
    with `viewer="none"`. Note the content type collapses to `text/plain` too
    -- a `.md` this large is not a preview candidate whatever its extension."""
    assert sniff(b"x" * (MAX_TEXT_PREVIEW_BYTES + 1), "big.md") == ("text/plain", "none")


def test_utf8_bom_is_text() -> None:
    assert sniff(b"\xef\xbb\xbfhello", "bom.txt") == ("text/plain", "text")


def test_utf16_is_refused() -> None:
    """§4.3: guessing encodings is a different project. UTF-16LE is NUL-rich,
    so it is refused by the NUL rule; UTF-16BE with a BOM fails the decode."""
    with pytest.raises(UnsupportedFileType):
        sniff("hello world".encode("utf-16-le"), "notes.txt")
    with pytest.raises(UnsupportedFileType):
        sniff("hello world".encode("utf-16"), "notes.txt")


def test_a_nul_containing_file_is_not_text() -> None:
    with pytest.raises(UnsupportedFileType):
        sniff(b"looks like text\x00but is not", "notes.txt")


def test_invalid_utf8_is_refused() -> None:
    with pytest.raises(UnsupportedFileType):
        sniff(b"\xfe\xed\xfa\xce\x01\x02\x03\x04", "mystery.bin")


def test_the_refusal_names_the_accepted_list() -> None:
    """§4.3: "the refusal message names the accepted list, so the wall is
    discoverable rather than mysterious"."""
    with pytest.raises(UnsupportedFileType) as exc:
        sniff(b"\x00\x01\x02\x03binary", "installer.msi")
    message = str(exc.value)
    for word in ("PDF", "PNG", "ZIP", "text"):
        assert word in message, message


def test_an_empty_file_is_text() -> None:
    assert sniff(b"", "empty.txt") == ("text/plain", "text")


def test_the_accepted_set_and_the_viewer_kinds_are_the_ones_the_console_knows() -> None:
    """The console's render map (§5.2) has exactly one entry per viewer kind.
    If a kind is added here without a map entry the console falls through to
    download-only, which is safe -- but the count is pinned so the addition is
    a deliberate, reviewed act rather than a silent one."""
    assert VIEWER_KINDS == ("pdf", "image", "markdown", "text", "none")
    assert "text/html" not in ACCEPTED_CONTENT_TYPES
    assert "image/svg+xml" not in ACCEPTED_CONTENT_TYPES
