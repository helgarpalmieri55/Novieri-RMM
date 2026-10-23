"""The install code that guards first boot.

`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §3.2.

Installation ends at a URL, not at a runbook: the first browser visit shows a
setup screen, and submitting it makes you the organization's administrator. That
screen has to answer before any user exists, so it cannot be gated by RBAC --
which leaves exactly two guards, and this module is one of them.

    1. the platform has zero users        (`app/api/v1/setup.py`)
    2. a one-time code the installer printed   (here)

THE SECOND ONE IS NOT OPTIONAL. Zero-users alone means whoever reaches the URL
first owns the fleet, and a freshly installed appliance is reachable long before
its owner sits down at it. The code is what makes "first to the URL" different
from "the person who installed it".

DELIVERY IS THE GRAFANA/PORTAINER PATTERN: generated on first need, written to
the shared secrets volume mode 600, and logged ONCE by the api container at the
moment it is created. Logging a secret is a deliberate choice, not an oversight:
the log and the volume are the only two channels that exist before the product
has a user, both are already vendor-only, and the code is worthless the instant
the platform is claimed.

WHY A FILE AND NOT A DATABASE ROW. The code has to survive `docker compose up
--force-recreate` and it has to be readable by an operator who cannot sign in --
which is the whole point -- so it lives beside the audit HMAC key and the
provisioning secret on `its-secrets`, not in a table only the product can read.

WHY THE PATH IS READ FROM THE ENVIRONMENT HERE rather than from `Settings`:
`app/core/config.py` belongs to another workstream this cycle. `ITS_SETUP_CODE_PATH`
follows the same `ITS_` convention every other setting does and should be
promoted to a `Settings` field when that file is next opened.
"""

import hmac
import logging
import os
import secrets
from pathlib import Path

__all__ = [
    "DEFAULT_SETUP_CODE_PATH",
    "SETUP_CODE_PATH_ENV",
    "ensure_setup_code",
    "setup_code_path",
    "verify_setup_code",
]

logger = logging.getLogger(__name__)

SETUP_CODE_PATH_ENV = "ITS_SETUP_CODE_PATH"

#: The shared secrets volume (`its-secrets:/var/lib/its/secrets` in
#: `deploy/docker-compose.yml`), which is where the audit HMAC key and the
#: provisioning client secret already live. Anywhere inside the image instead
#: would give the appliance a new install code on every redeploy.
DEFAULT_SETUP_CODE_PATH = Path("/var/lib/its/secrets/setup-code")

#: Upper case only, and no `I`, `L`, `O`, `U`, `0` or `1`: this is read off a
#: terminal and typed into a browser by a human, and the pairs that get
#: mistyped are removed rather than apologised for. 30 symbols, 20 characters,
#: ~98 bits -- guessing is not a strategy against it, which matters because the
#: route that checks it cannot identify the caller well enough to rate-limit
#: them.
_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
_LENGTH = 20


def setup_code_path() -> Path:
    """Where the install code lives on this deployment.

    An EMPTY or whitespace-only override is read as "unset". `Path("")` is the
    process's working directory, so honouring a blank `ITS_SETUP_CODE_PATH=`
    would write the code inside the container image -- off the volume, and gone
    on the next recreate, taking the operator's only copy with it.
    """
    override = os.environ.get(SETUP_CODE_PATH_ENV, "").strip()
    return Path(override) if override else DEFAULT_SETUP_CODE_PATH


def _generate() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_LENGTH))


def _read(path: Path) -> str | None:
    """The stored code, or `None` for "there isn't one".

    A missing file, an unreadable one and an EMPTY one are all the same answer,
    and the empty case is the one worth naming: a zero-byte file is what a
    crash mid-write leaves behind, and reading it as the code `""` would mean a
    blank install-code field verifies against it.
    """
    try:
        stored = path.read_text().strip()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError):
        return None
    return stored or None


def ensure_setup_code(path: Path) -> str:
    """The install code for this deployment, generating one on first call.

    Idempotent by design: the code is printed once, when it is created, and an
    operator who reads that line must find the same code working an hour later.
    A regenerating `ensure` presents as "the install code is wrong" on a code
    that was typed correctly.
    """
    existing = _read(path)
    if existing is not None:
        return existing

    fresh = _generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # O_EXCL, so two workers starting at once cannot both believe they
        # generated the code -- one of them would have logged a code the file
        # does not hold.
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raced = _read(path)
        if raced is not None:
            # Somebody else won between the read above and here. Their code is
            # the one on disk and the one in the log; ours is discarded.
            return raced
        # The file exists and is EMPTY -- a half-finished write, which nobody
        # can ever present a matching code for. Replaced rather than left to
        # make the appliance permanently unclaimable.
        handle = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)

    with os.fdopen(handle, "w") as fh:
        fh.write(fresh + "\n")
    # The O_TRUNC branch above may have opened a file that already existed with
    # wider bits than 0o600 -- `mode` only applies on creation.
    os.chmod(path, 0o600)

    # WARNING rather than INFO, and the code itself rather than a pointer to it:
    # this line is the delivery mechanism (see the module docstring), and an
    # operator grepping a busy container log needs it to survive the default
    # level. It is written exactly once per deployment.
    logger.warning(
        "first-boot install code generated: %s -- enter it on the setup screen "
        "to claim this installation (also written to %s)",
        fresh,
        path,
    )
    return fresh


def verify_setup_code(path: Path, presented: str) -> bool:
    """Does `presented` match the stored install code?

    NEVER GENERATES. A verify that wrote a code when it found none would let
    anybody who could delete the file -- or who simply arrived before it was
    written -- verify against a code they had just caused to exist. No stored
    code means no claim, which is also the right answer for a deployment whose
    secrets volume failed to mount.

    `hmac.compare_digest` because this is the only secret guarding ownership of
    the platform, and `==` on strings returns at the first differing character.
    """
    stored = _read(path)
    if stored is None:
        return False

    # Case folded and stripped: the alphabet is upper case only, so this
    # removes no entropy, and a code pasted out of a log line arrives with
    # whitespace attached. An empty answer is refused before it can meet an
    # empty stored value -- though `_read` has already ruled that out.
    candidate = presented.strip().upper()
    if not candidate:
        return False
    # `compare_digest` raises TypeError on a non-ASCII `str`. Unhandled that is
    # a 500 on an unauthenticated route: a distinguishable response an attacker
    # can trigger at will.
    if not candidate.isascii():
        return False
    return hmac.compare_digest(stored, candidate)
