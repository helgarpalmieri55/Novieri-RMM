"""The fingerprint names the application frame, not the library that raised.

Recommendation 9 of `docs/superpowers/audits/2026-08-21-ux-remediation-closeout.md`
(finding B79). `_raise_site` walks to the traceback's LAST frame, which is
right about WHERE the exception came from and wrong about WHOSE BUG it is:
when application code calls a library and the library raises, the last frame
belongs to `httpx`/`asyncpg`/`sqlalchemy`, so every distinct failure funnelled
through one library collapses toward that library's few raise lines.

FRAMES ARE SIMULATED, NOT MOCKED. `_raise_site` reads `f_globals["__name__"]`
off a real frame, so a test that patches the site out has tested nothing. Each
`_module(...)` below compiles a source string into a namespace carrying the
`__name__` of the module it stands in for; calling into it produces a genuine
frame whose globals say `app.modules.inventory.rules` or
`httpx._transports.default`. The traceback under test is a real traceback.

THE DIRECTION IS THE EASY THING TO GET BACKWARDS, so it has its own test.
Traceback frames run OUTWARD from the raise: `tb.tb_frame` is the catch site
and each `tb_next` steps one frame closer to the raise. "Deepest application
frame" therefore means the LAST application frame in `tb_next` order -- the one
nearest the raise -- not the first one met walking from `tb`, which is the one
nearest the seam.
"""

import hashlib
from textwrap import dedent
from types import TracebackType
from typing import Any

from app.core.errors.capture import (
    FINGERPRINT_VERSION,
    CapturedError,
    capture_exception,
    drained,
)


def _module(name: str, source: str) -> dict[str, Any]:
    """A namespace that behaves, to a frame walker, like a module called
    `name`. `exec` with an explicit `__name__` is the whole trick: every
    function defined in `source` carries these globals, so `_raise_site`
    reads `name` from any frame executing it."""
    namespace: dict[str, Any] = {"__name__": name}
    exec(compile(dedent(source), f"<{name}>", "exec"), namespace)  # noqa: S102
    return namespace


# Stands in for a third-party HTTP client: not application code, and the frame
# the exception is actually raised in.
_LIBRARY = _module(
    "httpx._transports.default",
    """
    def handle_request(_url):
        raise ValueError("connection refused")

    def send(url, call):
        return call(url)
    """,
)

# Application code, two frames deep, the inner one being the caller of the
# library. `evaluate_rules` is the answer this whole change is about.
_INNER = _module(
    "app.modules.inventory.rules",
    """
    def evaluate_rules(call, url):
        return call(url)

    def evaluate_policies(call, url):
        return call(url)
    """,
)

_OUTER = _module(
    "app.api.v1.inventory",
    """
    def list_inventory(inner, call, url):
        return inner(call, url)
    """,
)

# A capture seam: application code that CATCHES. Every `capture_exception`
# call in this codebase runs at one of these.
_SEAM = _module(
    "app.api.request_context",
    """
    def correlation_middleware(call, url):
        try:
            return call(url)
        except Exception as exc:
            return exc
    """,
)


def _capture(exc: BaseException) -> CapturedError:
    captured = capture_exception(exc, where="api")
    assert captured is not None
    return captured


def _run(call: Any, *args: Any) -> tuple[CapturedError, tuple[str, int, str]]:
    """Call something that must raise, and return both the captured record and
    the site version 1 would have recorded.

    Both halves are read INSIDE the `except` block on purpose: Python deletes
    the `as exc` name when the block ends, so a test that reaches for
    `exc.__traceback__` afterwards fails on a `NameError` rather than on the
    thing it meant to assert.
    """
    try:
        call(*args)
    except ValueError as exc:
        return _capture(exc), _last_frame_site(exc.__traceback__)
    raise AssertionError("the call under test did not raise")


def _last_frame_site(tb: TracebackType | None) -> tuple[str, int, str]:
    """What version 1 would have recorded: module, lineno and qualname of the
    traceback's last frame."""
    assert tb is not None
    node = tb
    while node.tb_next is not None:
        node = node.tb_next
    return (
        str(node.tb_frame.f_globals.get("__name__", "")),
        node.tb_lineno,
        node.tb_frame.f_code.co_qualname,
    )


def teardown_function() -> None:
    """Capture enqueues; leaving records behind would make another test's
    `drained()` see them."""
    drained()


def test_a_library_raise_beneath_application_code_is_named_after_the_application() -> None:
    """THE TEST THIS CHANGE EXISTS FOR (B79).

    `app.api.v1.inventory` -> `app.modules.inventory.rules` -> `httpx`, which
    raises. Version 1 recorded `httpx._transports.default`, so the operator's
    group list was a list of library internals and the console showed a
    third-party path and line number for a bug in this codebase.
    """
    captured, version_one_site = _run(
        _OUTER["list_inventory"], _INNER["evaluate_rules"], _LIBRARY["handle_request"], "u"
    )

    assert captured.module == "app.modules.inventory.rules"
    assert captured.qualname == "evaluate_rules"
    assert version_one_site[0] == "httpx._transports.default", (
        "the traceback must really end in the library, or this test proves nothing"
    )


def test_the_deepest_application_frame_wins_not_the_shallowest() -> None:
    """THE DIRECTION TEST. Fails if the walk runs from `tb` outward-in and
    stops at the first application frame it meets.

    Two application frames are on the stack: `app.api.v1.inventory` (nearest
    the catch) and `app.modules.inventory.rules` (nearest the raise). The
    second is the one that called the library and the one an operator can act
    on; the first is a route handler that would name every failure under it
    the same thing.
    """
    captured, _ = _run(
        _OUTER["list_inventory"], _INNER["evaluate_rules"], _LIBRARY["handle_request"], "u"
    )

    assert captured.module != "app.api.v1.inventory", (
        "walked from the catch site inward and stopped at the first app frame; "
        "the deepest app frame is the LAST one in tb_next order"
    )
    assert captured.module == "app.modules.inventory.rules"


def test_two_failures_through_one_library_do_not_share_a_group() -> None:
    """The defect stated as an outcome rather than as a site.

    Both calls end at the same `raise` in the same library function, so
    version 1 gave both the same `(exc_type, module, lineno, qualname)` and
    therefore one group. They are two different bugs in two different
    application functions.
    """
    first, _ = _run(_INNER["evaluate_rules"], _LIBRARY["handle_request"], "u")
    second, _ = _run(_INNER["evaluate_policies"], _LIBRARY["handle_request"], "u")

    assert first.fingerprint != second.fingerprint
    assert (first.qualname, second.qualname) == ("evaluate_rules", "evaluate_policies")


def test_a_raise_inside_application_code_is_unchanged() -> None:
    """The other half of the contract: where the raise site already WAS
    application code, this records exactly what it always did -- the last
    frame."""
    raiser = _module(
        "app.services.enrollment",
        """
        def enroll():
            raise ValueError("no such device")
        """,
    )
    captured, version_one_site = _run(
        _INNER["evaluate_rules"], lambda _url: raiser["enroll"](), "u"
    )

    assert (captured.module, captured.lineno, captured.qualname) == version_one_site
    assert captured.module == "app.services.enrollment"


def test_a_failure_with_no_application_frame_falls_back_to_the_raise_site() -> None:
    """A library-scheduled callback failing entirely inside the library has no
    application frame to prefer. The fallback is the version-1 behaviour --
    the raise site -- because the alternative is recording nothing, and a
    library's own bug is still a failure this platform has to show."""
    captured, version_one_site = _run(_LIBRARY["send"], "u", _LIBRARY["handle_request"])

    assert (captured.module, captured.lineno, captured.qualname) == version_one_site
    assert captured.module == "httpx._transports.default"


def test_the_catch_seam_is_never_the_answer() -> None:
    """The rejected design, guarded.

    `app.api.request_context` is application code AND the outermost frame of
    the traceback, so a rule that simply preferred application frames would
    name every library failure caught there after the middleware -- which is
    catch-site grouping, the thing `capture`'s module docstring rejected: it
    folds every distinct failure funnelled through one handler into a single
    meaningless group (spec §2.1). A seam frame is only ever the answer when
    the exception was raised in the seam itself.
    """
    exc = _SEAM["correlation_middleware"](_LIBRARY["handle_request"], "u")
    assert isinstance(exc, ValueError)
    captured = _capture(exc)

    assert captured.module != "app.api.request_context"
    assert captured.module == "httpx._transports.default"


def test_the_record_carries_the_fingerprint_version_and_cannot_collide_with_version_one() -> None:
    """WHY THE VERSION IS IN THE HASHED INPUT rather than only in a column.

    `platform_error_group.fingerprint` is UNIQUE and the drain upserts on it.
    Two eras of the rule must therefore never produce the same digest for the
    same site, or a v1 group named after a library would absorb v2 events
    named after the application. Prefixing the input with `v2|` puts every v2
    digest in an input space a v1 digest cannot occupy, so the existing unique
    index and the existing `ON CONFLICT (fingerprint)` keep working untouched.
    """
    captured, _ = _run(_INNER["evaluate_rules"], _LIBRARY["handle_request"], "u")

    assert captured.fingerprint_version == FINGERPRINT_VERSION == 2
    version_one_digest = hashlib.sha256(
        f"{captured.exc_type}|{captured.module}|{captured.lineno}|{captured.qualname}".encode()
    ).hexdigest()[:16]
    assert captured.fingerprint != version_one_digest


def test_the_message_is_still_not_in_the_fingerprint() -> None:
    """Spec §2.1, unchanged by any of the above: two different messages from
    the same site are one group, because messages interpolate data."""
    raiser = _module(
        "app.services.enrollment",
        """
        def enroll(text):
            raise ValueError(text)
        """,
    )
    prints = [
        _run(_INNER["evaluate_rules"], lambda _url, t=text: raiser["enroll"](t), "u")[0].fingerprint
        for text in ("device 41f2 not found", "device 9ab0 not found")
    ]
    assert prints[0] == prints[1]
