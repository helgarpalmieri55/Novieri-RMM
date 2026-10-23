"""Server-side type detection for stored documents
(`docs/modules/documentation/spec-attachments.md` §0.5, §4.3).

**THE CLIENT NEVER DECLARES A TYPE.** There is no multipart parser in this
project, so an upload is a raw request body with the filename in a query
parameter -- there is no field in which a content type could arrive. That
accident is the design: "a file whose content-type lies" is not a case handled
here, it is a sentence that cannot be formed. Everything below is computed from
the leading bytes.

The filename extension is used for ONE thing and never for more: disambiguating
*within* a sniff class -- which OOXML flavour a ZIP container is, whether
decodable text is Markdown or CSV or plain. A `diagram.pdf` whose bytes are a
ZIP is a ZIP here, is download-only, and is labelled a ZIP in the console. A
sniff/extension mismatch is LABELLED, NOT REFUSED (§0.4 D4): refusing would
break the legitimate `.dat`-that-is-really-a-PDF case and buys nothing, because
the viewer keys off the sniff and never off the extension.

**THE PROPERTY THAT MATTERS, AND WHY IT LIVES HERE.** `ACCEPTED_CONTENT_TYPES`
is closed, and `text/html` and `image/svg+xml` are not in it -- for any input,
at all. HTML, SVG and XML uploads are *accepted*; they decode as UTF-8 and land
in the text branch, so they are stored as `text/plain` with `viewer="text"` and
the console shows their source characters in a `<pre>`. That is deliberate and
useful (viewing a firewall config export or a `.drawio` file as text is real
operator work), and it is safe for exactly one reason: the stored metadata can
never itself become a rendering instruction. If this function could ever emit
`text/html`, the console's `Blob` would carry that type, and a blob URL carries
its creating origin -- which is the console's own, because
`deploy/docker-compose.yml` puts MinIO on that hostname (§0.3). Wall 2 of the
three walls in §0.4 is this closed set; `tests/test_doc_filetypes.py::
test_sniff_can_never_emit_a_rendering_content_type` is what holds it.

**WHAT THE ALLOW-LIST IS NOT.** It is not a malware control and must not be
described as one. ZIP is on the list, so arbitrary bytes can be stored by
zipping them -- deliberately, as the escape hatch for what is genuinely refused
(binary with no recognised magic). What makes an uploaded file inert is
structural and lives elsewhere: no agent can resolve a docs key, no job type
references one, and no presigned URL is ever minted, so this platform never
hands an uploaded file to an endpoint (§15.10).
"""

import os

from app.core.i18n import N_, tr, tr_marked

#: The rendering ceiling for the text viewer (§4.3). Above it a text file is
#: still stored and still downloadable -- it is just not previewed, because
#: pushing 25 MiB of text into the DOM hangs the tab. A *rendering* limit, not
#: an acceptance one.
MAX_TEXT_PREVIEW_BYTES = 2 * 1024 * 1024

#: The console's render map (§5.2) has exactly one entry per kind, and an
#: unknown kind degrades to download-only rather than to guessing.
VIEWER_KINDS: tuple[str, ...] = ("pdf", "image", "markdown", "text", "none")

_OOXML_BY_EXTENSION = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".vsdx": "application/vnd.ms-visio.drawing",
}

_OLE_BY_EXTENSION = {
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".vsd": "application/vnd.visio",
}

#: Every value `sniff` can return, and nothing else. Kept as an explicit
#: frozenset rather than derived from the branches below so that adding a
#: branch which returns a NEW type fails the containment test (F3b) instead of
#: quietly widening the set it is checked against.
ACCEPTED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/zip",
        "application/x-ole-storage",
        "text/markdown",
        "text/csv",
        "text/plain",
        *_OOXML_BY_EXTENSION.values(),
        *_OLE_BY_EXTENSION.values(),
    }
)

_ACCEPTED_DESCRIPTION = N_(
    "PDF; PNG, JPEG, GIF or WebP images; ZIP and OOXML (.docx/.xlsx/.pptx/.vsdx); "
    "legacy Office (.doc/.xls/.ppt/.vsd); and UTF-8 text of any kind, including "
    "Markdown, CSV, HTML, SVG and XML (stored and shown as source text). "
    "UTF-16 text is not accepted -- re-save it as UTF-8, or zip it."
)


class UnsupportedFileType(ValueError):
    """The bytes match no accepted class. The route turns this into a 415, and
    the message names the accepted list so the wall is discoverable rather than
    mysterious (§4.3)."""


def _extension(filename: str) -> str:
    return os.path.splitext(os.path.basename(filename))[1].lower()


def _is_utf8_text(data: bytes) -> bool:
    """Strict UTF-8 and no NUL. Both conditions, and neither is redundant.

    The NUL rule is what refuses UTF-16LE, whose ASCII-range text is
    NUL-interleaved and therefore decodes as perfectly valid UTF-8; the strict
    decode is what refuses UTF-16BE-with-BOM and every other binary that
    happens to contain no NUL. Guessing encodings is a different project
    (§4.3), so neither is softened into a heuristic.
    """
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def sniff(data: bytes, filename: str) -> tuple[str, str]:
    """Return `(content_type, viewer)` for an uploaded body.

    Pure: no I/O, no DB, no state. Raises `UnsupportedFileType` for bytes that
    match no accepted class -- see the module docstring for why the escape
    hatch for those is "zip it" rather than a wider allow-list.
    """
    extension = _extension(filename)

    if data.startswith(b"%PDF-"):
        return "application/pdf", "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "image"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "image"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "image"
    # WebP is a RIFF container; the four bytes at offset 8 are what distinguish
    # it from a RIFF .wav or .avi, which are not accepted.
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", "image"
    if data.startswith(b"PK\x03\x04"):
        return _OOXML_BY_EXTENSION.get(extension, "application/zip"), "none"
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _OLE_BY_EXTENSION.get(extension, "application/x-ole-storage"), "none"

    if not _is_utf8_text(data):
        raise UnsupportedFileType(
            tr(
                "unsupported file type. This library accepts: {accepted_description}",
                accepted_description=tr_marked(_ACCEPTED_DESCRIPTION),
            )
        )

    # Text, but too large to put in the DOM. The type collapses to `text/plain`
    # as well as the viewer to `none`: a 3 MiB `.md` is not a preview candidate
    # whatever its extension says, and storing `text/markdown` beside
    # `viewer="none"` would be metadata the console has to reconcile.
    if len(data) > MAX_TEXT_PREVIEW_BYTES:
        return "text/plain", "none"

    if extension in (".md", ".markdown"):
        return "text/markdown", "markdown"
    if extension == ".csv":
        return "text/csv", "text"
    # EVERYTHING else that is text -- including .html, .svg, .xml, .drawio and
    # a vendor's config export -- is `text/plain`. This is the branch that
    # makes the closed set closed. See the module docstring.
    return "text/plain", "text"
