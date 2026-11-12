"""PKCE, S256 and nothing else (RFC 7636; design §5.1, [S17], [S30]).

- verifier: 43 to 128 characters of `[A-Z] / [a-z] / [0-9] / "-" / "." / "_"
  / "~"` (RFC 7636 §4.1);
- challenge: `BASE64URL-ENCODE(SHA256(ASCII(code_verifier)))` (§4.2), which is
  always 43 characters;
- never "plain": "Clients MUST NOT downgrade to "plain"" [S17], and MCP's
  clients "MUST use the S256 code challenge method" [S30]. §4.3 makes a
  missing method mean "plain", so the authorization request must SAY S256
  (Task 8).

Compared in constant time.
"""

import base64
import hashlib
import hmac
import re

_VERIFIER = re.compile(r"[A-Za-z0-9\-._~]{43,128}")
_CHALLENGE = re.compile(r"[A-Za-z0-9\-_]{43}")


def valid_verifier(value: str) -> bool:
    return isinstance(value, str) and _VERIFIER.fullmatch(value) is not None


def valid_challenge(value: str) -> bool:
    return isinstance(value, str) and _CHALLENGE.fullmatch(value) is not None


def s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify(verifier: str, challenge: str) -> bool:
    """RFC 7636 §4.6, S256."""
    if not valid_verifier(verifier) or not valid_challenge(challenge):
        return False
    return hmac.compare_digest(s256(verifier), challenge)
