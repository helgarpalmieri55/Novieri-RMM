"""OpenBao is provisioned for the sign-in's two keys, and the runtime policy
allows exactly what `app/core/auth/keys.py` calls (plan Task 2, decisions D6,
D7, D23).

- `deploy/openbao/init.sh` creates `auth-pepper` (type hmac) and
  `auth-signing` (type ecdsa-p256), each only if absent -- a re-created
  pepper would orphan every stored password -- and never exportable;
- `deploy/openbao/its-app.hcl` grants HMAC on the pepper and nothing else on
  it (no read, no rotate, no export), sign and read on the signing key;
- the OpenBao backend reaches exactly those three paths, so a new call fails
  here before it fails with a 403 in a deployment.
"""

import re
from pathlib import Path

import httpx

from app.core.auth.keys import OpenBaoAuthKeys
from tests.test_auth_keys import _Transit

REPO = Path(__file__).resolve().parents[2]
INIT = (REPO / "deploy" / "openbao" / "init.sh").read_text()
POLICY = (REPO / "deploy" / "openbao" / "its-app.hcl").read_text()

_BLOCK = re.compile(r'path\s+"([^"]+)"\s*\{\s*capabilities\s*=\s*\[([^\]]*)\]\s*\}')


def _policy() -> dict[str, set[str]]:
    return {
        path: {c.strip().strip('"') for c in capabilities.split(",") if c.strip()}
        for path, capabilities in _BLOCK.findall(POLICY)
    }


def test_init_creates_both_keys_once_and_never_exportable() -> None:
    for name, kind in (
        ("auth-pepper", "type=hmac key_size=32"),
        ("auth-signing", "type=ecdsa-p256"),
    ):
        guarded = re.search(
            rf"if ! bao read transit/keys/{name} >/dev/null 2>&1; then\s+"
            rf"bao write transit/keys/{name} {re.escape(kind)}\s+fi",
            INIT,
        )
        assert guarded, name
    assert "exportable=true" not in INIT


def test_the_policy_grants_exactly_what_the_sign_in_needs() -> None:
    auth = {path: caps for path, caps in _policy().items() if "auth-" in path}
    assert auth == {
        "transit/hmac/auth-pepper": {"update"},
        "transit/sign/auth-signing": {"update"},
        "transit/keys/auth-signing": {"read"},
    }
    assert not any(path.startswith("transit/export") for path in _policy())


async def test_the_backend_calls_only_the_policy_paths() -> None:
    transit = _Transit()
    backend = OpenBaoAuthKeys(
        "https://bao.test",
        "t",
        client=httpx.AsyncClient(transport=httpx.MockTransport(transit.handler)),
    )
    await backend.hmac(b"x", version=None)
    await backend.hmac(b"x", version=1)
    await backend.sign(b"x")
    await backend.public_keys()

    called = {r.url.path.removeprefix("/v1/") for r in transit.requests}
    assert called == {p for p in _policy() if "auth-" in p}
