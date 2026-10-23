"""The install code: the only thing standing between a stranger and the platform.

`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §3.2.

First boot is a SCREEN, not a runbook -- which means the claim route answers
before anybody can sign in, so it cannot be gated by RBAC. Two things guard it:
the platform having zero users, and this code, generated at first start and
readable only by whoever can read the api container's log or the secrets
volume.

ZERO-USERS ALONE IS NOT ENOUGH, which is why this file exists. A stranger who
reaches the URL of a freshly installed appliance before its owner does would
otherwise claim the box and become its administrator. The code is what makes
"first to the URL" not the same thing as "owner".

WHAT THIS FILE DOES NOT TEST: that the code reaches a human. It is written to
the shared secrets volume and logged once by the api container (the Grafana and
Portainer pattern); whether the operator can read either is a deployment
question, exercised on the operator's own install like every other surface in
this cycle.
"""

import hmac
import os
import stat
from pathlib import Path

import pytest

from app.core.setup_code import (
    DEFAULT_SETUP_CODE_PATH,
    SETUP_CODE_PATH_ENV,
    ensure_setup_code,
    setup_code_path,
    verify_setup_code,
)


def test_the_code_is_generated_once_and_then_stays_put(tmp_path: Path) -> None:
    """A code that changed on the second call is a code nobody can ever use.

    The operator reads it from the log line written when it was created. If a
    later call regenerated, the printed code would stop working at some moment
    nobody can observe, and the failure would present as "the install code is
    wrong" on a correctly typed code.
    """
    path = tmp_path / "setup-code"
    first = ensure_setup_code(path)
    second = ensure_setup_code(path)

    assert first == second
    assert path.read_text().strip() == first


def test_two_installs_do_not_share_a_code(tmp_path: Path) -> None:
    """Non-vacuity for everything above and below.

    A `return "SETUP"` constant would satisfy stability, verification and the
    constant-time comparison, and would hand every deployment of this product
    the same install code -- one leaked screenshot and every unclaimed appliance
    in the field belongs to whoever saw it.
    """
    one = ensure_setup_code(tmp_path / "a" / "setup-code")
    two = ensure_setup_code(tmp_path / "b" / "setup-code")

    assert one != two


def test_the_code_carries_enough_entropy_to_be_unguessable(tmp_path: Path) -> None:
    """The claim route cannot rate-limit an anonymous caller usefully, so the
    code has to be wide enough that guessing is not a strategy. 20 characters
    over a 30-character alphabet is ~98 bits."""
    code = ensure_setup_code(tmp_path / "setup-code")

    assert len(code) >= 20
    assert code.isascii() and code.isalnum() and code == code.upper()


def test_the_file_is_readable_only_by_its_owner(tmp_path: Path) -> None:
    """It shares a volume with the audit HMAC key and the provisioning secret.

    A world-readable install code on an unclaimed appliance is the same defect
    as no code at all for anybody with a shell on the host.
    """
    path = tmp_path / "setup-code"
    ensure_setup_code(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_right_code_verifies_and_a_wrong_one_does_not(tmp_path: Path) -> None:
    path = tmp_path / "setup-code"
    code = ensure_setup_code(path)

    assert verify_setup_code(path, code) is True
    assert verify_setup_code(path, code[:-1] + ("A" if code[-1] != "A" else "B")) is False
    assert verify_setup_code(path, "TOTALLY-WRONG") is False


def test_a_code_typed_in_the_other_case_still_works(tmp_path: Path) -> None:
    """Deliberate, and it costs nothing: the alphabet is uppercase only, so
    folding case removes no entropy. Surrounding whitespace goes the same way --
    a code pasted out of a log line arrives with a newline attached."""
    path = tmp_path / "setup-code"
    code = ensure_setup_code(path)

    assert verify_setup_code(path, f"  {code.lower()}\n") is True


def test_the_comparison_is_constant_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`hmac.compare_digest`, asserted at the CALL SITE.

    A timing measurement would be flaky in CI and would prove less: what is
    being defended is that the comparison does not return early on the first
    wrong character, and the achievable proof of that is that the stdlib
    function which guarantees it is the one doing the work.

    The spy DELEGATES rather than stubbing, so this test still asserts the real
    answer as well -- a spy that returned True would pass a test that only
    counted calls.
    """
    calls: list[tuple[str, str]] = []
    real = hmac.compare_digest

    def spy(a: object, b: object) -> bool:
        calls.append((str(a), str(b)))
        return bool(real(a, b))  # type: ignore[arg-type]

    path = tmp_path / "setup-code"
    code = ensure_setup_code(path)
    monkeypatch.setattr(hmac, "compare_digest", spy)

    assert verify_setup_code(path, code) is True
    assert calls, "the code was compared with something other than hmac.compare_digest"


def test_no_code_file_means_no_claim(tmp_path: Path) -> None:
    """A missing file must REFUSE, not generate.

    If `verify` created a code when it found none, an attacker who could delete
    the file (or who arrived before the api container ever wrote one) would be
    verifying against a code they had just caused to exist. It also must not
    silently pass: a deployment whose secrets volume failed to mount has no
    install code, and the correct behaviour there is that nobody can claim it.
    """
    missing = tmp_path / "not-written-yet"

    assert verify_setup_code(missing, "ANYTHING") is False
    assert not missing.exists()


def test_an_empty_file_and_an_empty_answer_never_agree(tmp_path: Path) -> None:
    """The empty-string trap, from both ends.

    A half-finished write leaves a zero-byte file. Comparing "" with "" is
    `True` for every comparison function there is, so an empty stored code plus
    an empty presented one would let anyone claim the platform by submitting a
    blank field.
    """
    path = tmp_path / "setup-code"
    path.write_text("")

    assert verify_setup_code(path, "") is False
    assert verify_setup_code(path, "   ") is False
    assert verify_setup_code(path, "ANYTHING") is False


def test_a_non_ascii_answer_is_refused_rather_than_raising(tmp_path: Path) -> None:
    """`hmac.compare_digest` raises TypeError on non-ASCII `str`.

    Unhandled, that is a 500 on an unauthenticated route -- a shape an attacker
    can trigger at will, and one that distinguishes itself from the 403 every
    other wrong answer gets.
    """
    path = tmp_path / "setup-code"
    ensure_setup_code(path)

    assert verify_setup_code(path, "CODE-WITH-é") is False


def test_an_empty_file_is_replaced_rather_than_left_unusable(tmp_path: Path) -> None:
    """The other half of the zero-byte case: nobody can present an empty code,
    so a deployment that crashed mid-write would be permanently unclaimable
    with no way to fix it from inside the product."""
    path = tmp_path / "setup-code"
    path.write_text("")

    code = ensure_setup_code(path)

    assert code and verify_setup_code(path, code) is True


def test_the_path_is_the_shared_secrets_volume_unless_told_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And an EMPTY override is read as "unset", deliberately.

    `Path("")` is the current working directory, so a blank
    `ITS_SETUP_CODE_PATH=` in a compose file would write the install code into
    whatever directory the api process happened to start in -- inside the image,
    not on the volume, and gone on the next `docker compose up`.
    """
    monkeypatch.delenv(SETUP_CODE_PATH_ENV, raising=False)
    assert setup_code_path() == DEFAULT_SETUP_CODE_PATH

    monkeypatch.setenv(SETUP_CODE_PATH_ENV, "  ")
    assert setup_code_path() == DEFAULT_SETUP_CODE_PATH

    monkeypatch.setenv(SETUP_CODE_PATH_ENV, "/tmp/elsewhere/setup-code")
    assert setup_code_path() == Path("/tmp/elsewhere/setup-code")


def test_the_default_lives_on_the_volume_the_other_secrets_live_on() -> None:
    """A path inside the image is a code that changes on every redeploy."""
    assert str(DEFAULT_SETUP_CODE_PATH).startswith("/var/lib/its/secrets/")


def test_a_concurrent_first_start_does_not_hand_out_two_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two workers reaching `ensure` at once must agree.

    Simulated by making the exclusive create lose the race: the file appears,
    holding somebody else's code, between the read and the open. The loser must
    return the code that is ON DISK -- the one that was logged -- not the one it
    generated and failed to write.
    """
    path = tmp_path / "setup-code"
    real_open = os.open

    def racing_open(file: object, flags: int, mode: int = 0o777) -> int:
        if flags & os.O_EXCL:
            Path(str(file)).write_text("WINNERSCODEWINNERSCODE\n")
        return real_open(file, flags, mode)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", racing_open)

    assert ensure_setup_code(path) == "WINNERSCODEWINNERSCODE"
