"""Capture: what a record carries, and above all what it does not.

NO DATABASE -- capture is pure; Task 2 covers the drain.
"""

import app.core.errors.capture as capture_mod
from app.core.errors.capture import (
    QUEUE_MAX,
    capture_exception,
    drained,
    dropped_count,
    fingerprint_of,  # noqa: F401 -- imported per the task brief; not called
                      # directly, `fingerprint_of` is exercised indirectly
                      # through `CapturedError.fingerprint` in the tests below.
)
from app.core.logging import org_id_var, request_id_var


class _LeakyErr(Exception):
    """Its str() is a DSN with a password -- the shape this project's two real
    leaks had."""

    def __str__(self) -> str:
        return "could not connect: postgres://its:S3CRET-PW@db.internal/itsuite"


def _raise_leaky() -> Exception:
    try:
        raise _LeakyErr()
    except _LeakyErr as exc:
        return exc


def test_a_captured_record_carries_no_exception_text() -> None:
    """THE INVARIANT (spec §0.1). An error monitor that stores tracebacks
    undoes two sub-projects of work, in a durable store, on purpose."""
    captured = capture_exception(_raise_leaky(), where="api")
    assert captured is not None
    rendered = repr(captured)
    assert "S3CRET-PW" not in rendered
    assert "postgres://" not in rendered
    assert "could not connect" not in rendered
    assert captured.exc_type == "_LeakyErr"


def test_two_raises_from_the_same_line_share_a_fingerprint() -> None:
    a = capture_exception(_raise_leaky(), where="api")
    b = capture_exception(_raise_leaky(), where="api")
    assert a is not None and b is not None
    assert a.fingerprint == b.fingerprint


def test_raises_from_different_lines_do_not_share_a_fingerprint() -> None:
    def _other() -> Exception:
        try:
            raise _LeakyErr()
        except _LeakyErr as exc:
            return exc

    a = capture_exception(_raise_leaky(), where="api")
    b = capture_exception(_other(), where="api")
    assert a is not None and b is not None
    assert a.fingerprint != b.fingerprint


def test_a_hostile_type_name_does_not_reach_the_record_verbatim() -> None:
    """Measured in sub-project 1: `type(name, (Exception,), {})` puts arbitrary
    text where a type name is expected, and the type name is the ONE string
    this design lets through."""
    Hostile = type("postgres://u:hunter2@h/db", (Exception,), {})
    try:
        raise Hostile()
    except Exception as exc:  # noqa: BLE001
        captured = capture_exception(exc, where="api")
    assert captured is not None
    assert "hunter2" not in repr(captured)


def test_correlation_ids_are_populated_from_the_contextvars() -> None:
    token_r = request_id_var.set("req-abc")
    token_o = org_id_var.set("org-1")
    try:
        captured = capture_exception(_raise_leaky(), where="api")
    finally:
        request_id_var.reset(token_r)
        org_id_var.reset(token_o)
    assert captured is not None
    assert captured.request_id == "req-abc"
    assert captured.org_id == "org-1"


def test_absent_correlation_ids_are_none_not_empty_string() -> None:
    """`""` and `None` mean different things in a filter, and only one of them
    is true outside a request."""
    captured = capture_exception(_raise_leaky(), where="worker")
    assert captured is not None
    assert captured.request_id is None
    assert captured.job_id is None


def test_capture_never_raises_even_when_recording_fails(monkeypatch) -> None:  # noqa: ANN001
    """A monitoring failure must not become the outage."""

    def _boom(_item: object) -> None:
        raise RuntimeError("queue is broken")

    monkeypatch.setattr(capture_mod, "_enqueue", _boom)
    assert capture_exception(_raise_leaky(), where="api") is None  # no raise


def test_queue_overflow_drops_and_counts() -> None:
    """A silently truncated error stream is worse than a visibly truncated
    one: the count tells an operator the numbers are floors."""
    drained()  # start empty
    before = dropped_count()
    for _ in range(QUEUE_MAX + 25):
        capture_exception(_raise_leaky(), where="api")
    assert dropped_count() - before == 25
    assert len(drained()) == QUEUE_MAX


def test_two_raise_sites_funnelled_through_ONE_catch_site_do_not_collide() -> None:
    """THE PROPERTY THE OTHER FINGERPRINT TESTS CANNOT SEE.

    They raise and catch inside the same function, so the traceback has a
    single frame and the catch site IS the raise site -- which means they pass
    whether `_raise_site` walks to the last frame or stops at the first.
    Verified: replacing the walk with `break` (stop at the catch site) left
    both of them green.

    That is not a hypothetical shape. EVERY capture call in this codebase runs
    at a seam far from the failure -- the request middleware, celery's
    task_failure, `_OrgCycleFailures.contain` -- so a fingerprint taken at the
    catch site would hash the seam and collapse every unrelated failure
    funnelled through it into one meaningless group.

    Two different helpers raise; one catch site captures both. Their
    fingerprints must differ.
    """

    def _raise_a() -> None:
        raise _LeakyErr()

    def _raise_b() -> None:
        raise _LeakyErr()

    def _one_catch_site(fn: object) -> object:
        try:
            fn()  # type: ignore[operator]
        except _LeakyErr as exc:
            return capture_exception(exc, where="api")
        raise AssertionError("unreachable")

    a = _one_catch_site(_raise_a)
    b = _one_catch_site(_raise_b)
    assert a is not None and b is not None
    assert a.fingerprint != b.fingerprint, (
        "two distinct raise sites collapsed to one fingerprint -- the site is "
        "being taken at the catch seam, not at the raise"
    )
    assert a.qualname.endswith("_raise_a")
    assert b.qualname.endswith("_raise_b")
