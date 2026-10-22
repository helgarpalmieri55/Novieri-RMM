"""SECURITY GATE 4a: the nonce-delimited untrusted envelope.

docs/modules/ai-admin/spec.md §8.2, §0.6, §13 test 11.

**THIS IS DEPTH. IT IS NOT THE CONTROL.** The control is the capability bound:
the static allowlist of `GET` routes (`tools/registry.py`) and the loopback gate
(`executor.py`), both already landed and both already falsified. A FULLY
SUCCESSFUL INJECTION GETS THE MODEL TO CALL A DIFFERENT READ TOOL THE ADMIN WAS
ALREADY ENTITLED TO, AND NOTHING ELSE. The envelope, the constant system prompt
and the flagging heuristic are mitigations layered on top of that, and the spec
says so explicitly so a later reader does not mistake the envelope for the
control and then "simplify" the allowlist away.

A later task MAY NOT remove, widen, or make configurable: `REGISTRY`,
`_NEVER_A_TOOL`, `_FORBIDDEN_GATES`, `ReadTool.method`'s `Literal["GET"]`, or the
loopback call in `executor.py`. If a future brief appears to require it, that
brief is wrong.

WHAT THIS DOES. Retrieved content is never string-formatted into a prose blob
and never concatenated into a user or system turn; it travels as JSON inside a
`tool_result` block, so every field boundary survives whatever the content says.
Within that JSON, FREE-TEXT fields are additionally wrapped:

    <untrusted-a7Kq2Zx9m>...content...</untrusted-a7Kq2Zx9m>

The suffix is a PER-TURN nonce from `secrets.token_urlsafe(9)`. Content cannot
forge a closing tag it cannot predict -- a body reading `</untrusted-> now do X`
closes nothing, because it does not know the suffix.

THE NONCE-LEAK ABORT. Before wrapping, retrieved text is scanned FOR THE NONCE
ITSELF. There is no benign way for a freshly minted 12-character random string
to appear in a database row read microseconds later, so its presence means the
nonce leaked somewhere it could be observed and forged. The turn is aborted
(`NonceLeakError`), not degraded.

Stated honestly: a model can be persuaded to disregard a delimiter. The proof is
§8.3, not this file.
"""

import secrets
from typing import Any

from app.core.ai.errors import NonceLeakError

# `secrets.token_urlsafe(9)` -> 12 url-safe characters, ~72 bits. Long enough
# that guessing it inside one turn is not a strategy; short enough that it costs
# a handful of tokens per wrapped field.
_NONCE_BYTES = 9

# The free-text fields §8.2 enumerates: ticket subject and body, comment bodies,
# descriptions, device hostnames, software and package names, script names,
# alert messages, audit metadata strings. Matched on the KEY, because the value
# being a string is not enough -- a UUID and a timestamp are strings too, and
# wrapping them would bloat every result and teach the model that the tags are
# noise.
_FREE_TEXT_FIELDS: frozenset[str] = frozenset(
    {
        "subject",
        "body",
        "content",
        "comment",
        "description",
        "note",
        "notes",
        "message",
        "title",
        "summary",
        "hostname",
        "device_hostname",
        "name",
        "display_name",
        "product_name",
        "package_name",
        "software_name",
        "script_name",
        "vendor_name",
        "check_type",
        "selector",
        "observed_value",
        "threshold_display",
        "cleared_reason",
        "matched_rule",
        "excerpt",
        "action",
        "target_type",
        "reason",
    }
)


def mint_nonce() -> str:
    """A fresh per-turn nonce. NEVER cache this, never derive it from anything
    stable: a constant nonce is forgeable by any content that has seen one
    previous turn's output, and `test_two_turns_get_different_nonces` is the
    only thing that can see that mistake."""
    return secrets.token_urlsafe(_NONCE_BYTES)


def open_tag(nonce: str) -> str:
    return f"<untrusted-{nonce}>"


def close_tag(nonce: str) -> str:
    return f"</untrusted-{nonce}>"


def _wrap_text(text: str, *, nonce: str) -> str:
    if nonce in text:
        # The only way this happens is a leak. Abort rather than wrap, because
        # wrapping content that knows the nonce is wrapping content that can
        # close the tag.
        raise NonceLeakError(  # i18n-exempt: aborts the turn, logged only
            "retrieved text contained this turn's nonce; the turn was aborted"
        )
    return f"{open_tag(nonce)}{text}{close_tag(nonce)}"


def wrap(payload: Any, *, nonce: str) -> Any:
    """Wrap every free-text field at every depth. Non-text fields -- UUIDs
    rendered as strings under non-text keys, ints, bools, timestamps, `None` --
    are returned untouched."""
    if isinstance(payload, dict):
        return {
            key: (
                _wrap_text(value, nonce=nonce)
                if key in _FREE_TEXT_FIELDS and isinstance(value, str) and value
                else wrap(value, nonce=nonce)
            )
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [wrap(item, nonce=nonce) for item in payload]
    return payload
