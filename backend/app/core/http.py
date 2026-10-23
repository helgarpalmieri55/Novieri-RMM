"""Shared bounded-request-body helpers (invariant-adjacent hardening: an
unbounded read off an authenticated route is a same-origin OOM vector, not a
theoretical one).

A handful of routes across modules take a raw request body directly (no
multipart parser is installed in this project) -- budget's expense-attachment
upload and deploy's package-artifact upload both do -- and each of those
bodies is legitimately large (an invoice scan, an installer). A bare
``await request.body()`` buffers the ENTIRE body in process memory before the
handler gets to look at it, with no cap: any caller who holds the route's
permission (``expense.record``, ``deploy.package.manage`` -- an ordinary,
non-admin grant in both cases) can OOM the whole control plane with one
request.

Two checks, not one -- both are required, not redundant:

* ``reject_oversized_content_length`` is a fast-path rejection on the
  DECLARED ``Content-Length`` header, checked before any DB/object-store
  work. It is cheap and rejects an obviously-oversized request early, but
  ``Content-Length`` is client-supplied: it can be wrong, and a chunked
  request carries no ``Content-Length`` at all (``Transfer-Encoding:
  chunked``), so this check alone can be silently bypassed by any client
  that simply omits the header.
* ``read_bounded_body`` enforces the identical cap on the bytes actually
  read off the wire, via ``request.stream()`` -- this is the guard a
  hostile (or merely chunked) client cannot route around, since it counts
  real bytes rather than trusting a declared length.

Call BOTH, in that order, on every raw-body route: the header check first
(cheapest, catches the common case before touching anything else), then the
streaming read (the actual enforcement).

Originally implemented once, inline in ``app.modules.budget.api`` (P2 of the
expense-attachment-upload hardening); ``app.modules.deploy.api`` needed the
identical protection for ``POST /packages/{id}/artifact``, so both call
sites share this module rather than risk the two copies drifting apart.
Each caller supplies its OWN ``max_bytes`` -- there is no one-size-fits-all
cap across modules with very different payloads (an invoice scan vs. a
multi-hundred-MB installer).
"""

from fastapi import HTTPException, Request, status

from app.core.i18n import tr


def reject_oversized_content_length(request: Request, max_bytes: int) -> None:
    """Fail fast on a declared ``Content-Length`` before touching the DB or
    reading a byte off the wire. ``Content-Length`` is client-supplied and
    may be absent (chunked transfer) or simply wrong, so this is a fast-path
    rejection only -- the actual bytes read must ALSO be bounded via
    ``read_bounded_body``; a lying (or missing) header must never be trusted
    alone."""
    content_length = request.headers.get("content-length")
    if content_length is None:
        return
    try:
        declared = int(content_length)
    except ValueError:
        return
    if declared > max_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            tr("request body exceeds the {max_bytes}-byte limit", max_bytes=max_bytes),
        )


async def read_bounded_body(request: Request, max_bytes: int) -> bytes:
    """Enforce the size cap on the bytes actually read off the wire, not
    just the declared ``Content-Length`` (which is client-supplied and
    cannot be trusted alone -- see module docstring). Reads incrementally via
    ``request.stream()`` and aborts the instant the running total exceeds
    ``max_bytes``, so a request with no (or a lying) ``Content-Length`` can
    never buffer more than ``max_bytes`` + one chunk in memory before being
    rejected."""
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > max_bytes:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                tr("request body exceeds the {max_bytes}-byte limit", max_bytes=max_bytes),
            )
    return bytes(chunks)
