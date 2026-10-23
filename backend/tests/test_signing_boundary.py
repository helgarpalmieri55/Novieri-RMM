"""There is exactly ONE place in this codebase that produces a signature.

**A REPO-LEVEL GUARD, NOT A SITE-SERVER ONE.** `CLAUDE.md` invariant #1 says
every job the agent executes is cryptographically signed by the server. The
corollary nothing asserted until now is the *uniqueness* of that path: a second
place that signs is a second place that decides what a signature MEANS, and the
two diverge on the first change to either.

`JobEngine._sign` is that place. It signs `canonical_bytes` of
`{id, type, device, params, issued_at, expires_at, nonce}` — the shape the agent
verifies — and every job in the system goes through it.

**THIS TEST WAS RED WHEN IT WAS WRITTEN, AND THAT IS WHY IT EXISTS.** Before the
commit that added it, `app/modules/provisioning/dispatch.py` contained a second
awaited signer call, over a bespoke `ProvisionPrepareEnvelope`, whose result was
**not assigned, not persisted, not returned and not published** — there was no
consumer. The discipline was real (no unsigned path was ever constructed) but
nothing consumed the output, and NOTHING WOULD HAVE GONE RED IF THE LINE WERE
DELETED. A discipline no test can see is a comment. It has been deleted; see
`docs/modules/site-server/spec.md` §8.2 and §13.2, which want that deletion
rather than its preservation.

**WHAT IS DELIBERATELY NOT MATCHED.** `app/core/ca.py` calls
`.sign(key, hashes.SHA256())` for x509 certificate issuance — that is the CA
issuing a certificate, not the control plane signing a job envelope, and it is
not awaited. `app/core/signing.py` is the signer's own implementation. This
walks `ast.Await` nodes whose value is a call to an attribute named `sign`,
which is exactly the "somebody asked the signing service to sign something"
shape and nothing else.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

#: The one and only awaited signer call in `backend/app`.
#:
#: An equality, never a subset: a subset assertion passes against the exact
#: addition it exists to forbid.
EXPECTED_AWAITED_SIGN_CALLS: dict[str, int] = {"app/jobs/engine.py": 1}


def _awaited_sign_calls(path: Path) -> int:
    tree = ast.parse(path.read_text())
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "sign"
    )


def test_only_the_job_engine_produces_a_signature() -> None:
    """*If a second awaited signer call were added anywhere under `backend/app`*
    the map gains that file and the assertion names it.

    That is not hypothetical: this test's first run named
    `app/modules/provisioning/dispatch.py` with count 1, which is what got that
    line deleted rather than documented.
    """
    found = {
        str(path.relative_to(APP.parent)): count
        for path in sorted(APP.rglob("*.py"))
        if (count := _awaited_sign_calls(path))
    }
    assert found == EXPECTED_AWAITED_SIGN_CALLS


def test_the_walker_finds_a_deliberately_planted_call() -> None:
    """POSITIVE CONTROL. The equality above is satisfied by a walker that
    matches nothing, and a guard that cannot see the thing it forbids is
    decoration. This feeds it the exact shape and requires it to be found."""
    planted = (
        "async def f(signer, envelope):\n"
        "    await signer.sign(canonical_bytes(envelope.model_dump()))\n"
    )
    tree = ast.parse(planted)
    found = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "sign"
    )
    assert found == 1


def test_the_job_engine_signs_the_canonical_payload() -> None:
    """The positive half: the one remaining call signs the canonical bytes of
    the payload the agent verifies.

    "Nobody else signs" is also true of a system that signs nothing at all, so
    the uniqueness assertion above needs this beside it.
    """
    source = (APP / "jobs" / "engine.py").read_text()
    tree = ast.parse(source)
    awaited = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "sign"
    ]
    assert len(awaited) == 1
    assert "canonical_bytes" in ast.unparse(awaited[0])
