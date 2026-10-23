"""SECURITY GATE 3: the fail-closed response scanner.

docs/modules/ai-admin/spec.md §0.5, §7.3.

THIS EXISTS BECAUSE "THE UNDERLYING API DOESN'T RETURN SECRETS" IS FALSE IN THIS
CODEBASE, and the defence must not be built on it. `checkout_credential` returns
a plaintext secret and `GET /licenses/{id}/key` returns a decrypted key; both are
correct point-of-use behaviour, and both are excluded structurally by the
registry. This layer catches the OTHER case, which no registry test could see
when it was written: a module adds a field to an existing, allowlisted response
model at any time. The registry test checks that the ROUTE is allowed; nothing
checked what the route newly RETURNS.

IT FAILS CLOSED, AND IT BLOCKS THE WHOLE RESULT rather than the offending key.
Partial redaction leaves the model reasoning over data whose shape it cannot
see, and a model that cannot see what is missing will assert things about it.

---

**THE CONTROL IS THE DECLARED RESPONSE CONTRACT. THE KEY-NAME PATTERN IS DEPTH,
AND IT IS NOT THE CONTROL.** This file used to be a denylist of secret-key
SPELLINGS presented as a fail-closed gate, and it was measured: of 48 realistic
secret-bearing key names, 13 were blocked and 35 passed -- `apiKey`, `pwd`,
`session_key`, `recovery_code`, `psk`, `dsn`, and `community_string` and
`connection_string`, which are credentials the IPAM and network modules traffic
in. Every camelCase spelling missed because the pattern required `_`. A Cyrillic
homoglyph, a fullwidth `p` and a zero-width-joined `password` all walked
straight past. That is not a defect in the vocabulary; it is what a denylist IS.
A list of the spellings somebody thought of cannot be fail-closed by
construction, and adding a hundred more alternatives would move the guess, not
remove it.

So the polarity is inverted. `ReadTool.response_fields` is the closed set of key
names a tool's response may contain, at any depth, declared in `registry.py` and
compiled into the image. **A key that is not declared blocks the entire result.**
That is fail-closed by construction rather than by enumeration: the unknown case
-- a spelling nobody predicted, an alphabet nobody predicted, a field added to a
response model next year -- lands in the blocked half by default, which is the
property this docstring used to claim and this code did not have.

WHY A FROZEN DECLARATION AND NOT THE LIVE `response_model`'s FIELDS. Deriving the
set at runtime from the model would defeat exactly the threat this gate exists
for: a field added to `AlertOut` would add itself to its own allowlist and pass.
The declaration is a constant, like the registry it lives in, and
`test_ai_admin_registry.py::test_declared_response_fields_match_the_live_model`
compares it to the live model -- so a module that adds a field turns the build
red naming that field AND is blocked at runtime until someone reviews it. Those
are the two halves of "becoming an AI tool is a deliberate, reviewed act" (§0.2),
applied to what a tool RETURNS and not only to which route it calls.

WHAT THE PATTERN IS STILL FOR. Two things the allowlist cannot do:
  * a declared name that is itself secret-shaped -- someone regenerating
    `response_fields` from a model that has grown `password` would otherwise
    widen the allowlist to include it, so the pattern blocks that key even when
    it is declared, and `test_no_declared_response_field_is_secret_shaped` turns
    the build red before it ships;
  * a diagnosis. "matches the secret-material pattern" tells an operator reading
    the warning what happened; "is not declared" would send them looking for a
    schema drift that is not there.
It is therefore kept, and widened: whole-WORD matching over an NFKC-normalized,
zero-width-stripped, camelCase-split key, so `apiKey`, `PWD`, `sessionKey`, a
fullwidth spelling and a zero-width-joined one all match where they did not. It
is still a denylist and is still guessable; it is no longer what stands between
the model and a secret. A homoglyph spelling is NOT caught by the pattern and is
not claimed to be -- it is caught by the declared-contract check, because it is
not a name any response model declares.

THE FALSE-NEGATIVE SURFACE, STATED, BECAUSE THE COST THIS FILE USED TO STATE WAS
THE OTHER ONE. What can still reach the model is a value carried under a
DECLARED, non-secret-shaped key -- `observed_value` holding a credential, say.
No key-name analysis can see that; the answer is the registry (§0.3: a route
whose response model is not fully understood does not become a tool), not more
words in a regex.

THE FALSE-POSITIVE COST IS ACCEPTED AND STATED (§7.3), AND IS NOW LARGER. A
route that grows a field blocks its tool result until `registry.py` declares it;
a field named `token_expiry` or `sort_key` blocks it permanently. That is the
safe direction, it is visible (a red registry test, and a warning naming the
key), and the fix is to declare the field, rename it in the module that owns it,
or drop the route from the registry -- all visible in a diff, none reachable
from this file.

---

THERE IS NO EXEMPTION LIST AND NO EXEMPTION PARAMETER, AND THAT IS NOW TRUE OF
THE CODE AND NOT ONLY OF THIS PARAGRAPH. It was not. `scrub` consulted
`tool.sensitive_fields`, a per-tool frozenset of key names it DROPPED -- checked
BEFORE the pattern, so `sensitive_fields=frozenset({"password"})` on a registry
entry turned a fail-closed block into a silent drop, one line, no other change.
`test_scrub_takes_exactly_two_parameters` could not see it because the hatch rode
on the TOOL, not on the signature, and the paragraph this one replaces actually
RECOMMENDED reaching for it. No registered tool ever declared one. The field is
gone from `ReadTool` and the read is gone from here.

`response_fields` IS NOT THAT MECHANISM COMING BACK, AND THE DIFFERENCE IS THE
POLARITY, NOT THE WORDING: it can only ever make a result MORE likely to be
blocked. Declaring a name does not make its key pass -- the pattern still runs
over declared keys, and `test_ai_admin_scrub.py::test_a_realistic_secret_key_
name_blocks_even_when_declared` is the assertion, over all 48 measured names,
that keeps it that way. §7.3 clause 3 stands: a tool whose
legitimate response contains such a key must be FIXED OR REMOVED FROM THE
REGISTRY, not exempted.

**THIS SCANNER NEVER EDITS A PAYLOAD.** `Ok.payload` is the object that came in,
not a rebuilt copy: the only two outcomes are "all of it" and "none of it". A
walker that rebuilt dicts as it went is one `continue` away from being a
redactor again, and a redactor is the exemption mechanism with a different name.
"""

import logging
from dataclasses import dataclass
from typing import Any

from app.core.ai.types import ReadTool
from app.core.secrets import looks_like_secret_key

__all__ = ["Blocked", "Ok", "ScrubOutcome", "looks_like_secret_key", "scrub"]

logger = logging.getLogger(__name__)

# `looks_like_secret_key` and its word sets MOVED to `app/core/secrets.py`
# (2026-08-14) and are re-exported above. The log sink needs the same predicate
# and must not import the AI package to get it. Re-exported rather than
# relocated-and-updated-everywhere so the four test modules importing it from
# here keep working -- which is how the move is verified to be lossless.


@dataclass(frozen=True)
class Ok:
    """The payload survived. `payload` IS THE OBJECT THAT CAME IN -- this gate
    never edits, drops or rebuilds anything."""

    payload: Any


@dataclass(frozen=True)
class Blocked:
    """The whole tool result is rejected. `key_path` names where and `reason`
    names why, for the operator log -- NEITHER is returned to the model (the
    executor's model-facing message is deliberately the same for both reasons,
    so a probing model learns nothing about which rule fired), and the offending
    VALUE is never carried anywhere."""

    key_path: str
    reason: str


ScrubOutcome = Ok | Blocked


def _scan(node: Any, path: str, declared: frozenset[str] | None) -> Blocked | None:
    """Depth- and list-aware, and READ-ONLY. A dict-only walker passes a
    dict-only test and lets `{"a": [{"secret": "x"}]}` straight through, which is
    the exact shape a paged response has.

    `declared` selects the pass: `None` runs the key-name pattern, a set runs the
    declared-contract check. TWO PASSES, NOT ONE INTERLEAVED WALK, so a payload
    that trips both is always reported against the pattern -- the more specific
    diagnosis -- wherever in the tree each one sits.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if declared is None:
                if looks_like_secret_key(key):
                    return Blocked(
                        key_path=here,
                        reason="key name matches the secret-material pattern",
                    )
            elif key not in declared:
                return Blocked(
                    key_path=here,
                    reason="key is not declared by the tool's response contract",
                )
            found = _scan(value, here, declared)
            if found is not None:
                return found
        return None
    if isinstance(node, list):
        for index, value in enumerate(node):
            found = _scan(value, f"{path}[{index}]", declared)
            if found is not None:
                return found
        return None
    return None


def scrub(tool: ReadTool, payload: Any) -> ScrubOutcome:
    """Scan every key at every depth, twice: any secret-shaped key name, and any
    key the tool does not declare, rejects the ENTIRE result.

    THE SIGNATURE IS TWO PARAMETERS AND MUST STAY TWO. See the module docstring.
    """
    found = _scan(payload, "", None) or _scan(payload, "", tool.response_fields)
    if found is not None:
        # The only operator-visible signal, because the turn CONTINUES without
        # this data (§7.3). It names the tool, the key path and the rule, and
        # never the value.
        logger.warning(
            "ai_admin: blocked a tool result that may carry secret material "
            "(tool=%s, key_path=%s, reason=%s)",
            tool.name,
            found.key_path,
            found.reason,
        )
        return found
    return Ok(payload=payload)
