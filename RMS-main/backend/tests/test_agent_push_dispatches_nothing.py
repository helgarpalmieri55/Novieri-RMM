"""**CYCLE B2 SIGNS AND DISPATCHES -- UNDER SIGNATURE, THROUGH ONE FILE, AND
STILL NEVER CONNECTS TO A TARGET.** This file was reshaped from the B1 guard,
not deleted, and the reshaping is the point.

`docs/modules/agent-distribution/spec-push-install.md` §14 split Part B into two
cycles. B1 "ended with a feature that refuses everything, correctly, and
dispatches nothing", and this test asserted that from the AST: no file in the
package named a signer, a vault or a transport. B2 is "the jobs and the agent":
it registers the two job types, decrypts the credential at the point of use and
signs the whole instruction. So the guard changed **exactly one thing** and
kept everything else:

* **THE UNSIGNED / CONNECTION PATH IS STILL FORBIDDEN EVERYWHERE, FOREVER.** The
  control plane never opens a connection to a target (invariant #2, 2a clause
  (a)); that connection is the AGENT'S, inside a verified job, and no file in
  this control-plane module may open a socket, an SSH session or a WinRM
  session -- not the dispatcher, not the router, not anything. `net.py`'s IPAM
  reasoning is unchanged. This is the property B2 must not weaken, and the test
  that guards it did not move.

* **THE SIGNING AND POINT-OF-USE DECRYPTION PATH IS NOW ALLOWED -- IN
  `dispatch.py` AND NOWHERE ELSE, plus `api.py` which wires it.** That is what
  B2 is. Every other file in the package -- `preflight.py`, `credential_scope.py`,
  `authorization.py`, `host_key.py`, `models.py`, `net.py`, `errors.py`,
  `schema.py`, `jobtypes.py` -- stays as pure as it was in B1: it names no
  signer, no vault and no dispatch path. The refusal ladder in particular
  (`preflight.py`) must remain a pure evaluation that cannot itself sign
  anything, which is what keeps the refusal on the near side of the signing
  boundary (spec §3).

**WHY EXEMPT `dispatch.py` RATHER THAN THE WHOLE PACKAGE.** "The signing lives
in exactly one file" is a stronger, checkable statement than "the signing lives
somewhere in the module". A second dispatch path added to `authorization.py` or
`host_key.py` would be named by a failure here, exactly as a second egress path
is in `site_server/dispatch.py`'s kernel-boundary test. The exemption is a
whitelist of two files, not a hole.
"""

import ast
from pathlib import Path

import pytest

import app.modules.agent_push as agent_push_pkg

_PACKAGE = Path(next(iter(agent_push_pkg.__path__)))
_SOURCES = sorted(p for p in _PACKAGE.glob("*.py"))

#: The files B2 permits to sign a job and decrypt a credential. `dispatch.py`
#: creates the signed jobs and checks out the credential at the point of use;
#: `api.py` wires the dispatcher to the engine, the vault and the Redis publish.
#: Every other file stays pure.
_SIGNING_EXEMPT = {"dispatch.py", "api.py"}

RESERVED = ("agent.push_install", "agent.push_probe")


def test_the_package_has_sources_to_check() -> None:
    """A sweep over an empty set reports a clean bill of health. If the module
    is ever renamed or moved, this fails instead of silently passing."""
    assert len(_SOURCES) >= 6, [p.name for p in _SOURCES]


# --------------------------------------------------------------------------- #
# 1. Both job types are now REGISTERED, and both kernel gates still refuse them
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("job_type", RESERVED)
def test_both_push_job_types_are_registered(job_type: str) -> None:
    """The B1 guard asserted these were NOT registered. B2 registers them
    (`jobtypes.register_job_types`), and that registration is what emptied
    `RESERVED_JOB_TYPES` in the same commit -- the intended handover (spec §14).
    """
    from app.jobs import registry
    from app.modules.agent_push.jobtypes import register_job_types
    from app.modules.remote.jobtypes import register_job_type as register_remote
    from app.worker import register_worker_job_types

    register_worker_job_types()
    register_remote()
    register_job_types()
    assert job_type in set(registry.registered_names())


def test_the_reserved_set_is_now_empty() -> None:
    """The handover, stated directly. Both partition tests assert
    `decided - registered == RESERVED_JOB_TYPES`; registering the two types made
    `decided - registered` empty, so the reserved set had to empty with it."""
    from app.jobs.direct_dispatch import RESERVED_JOB_TYPES

    assert RESERVED_JOB_TYPES == frozenset()


@pytest.mark.parametrize("job_type", RESERVED)
def test_both_kernel_gates_still_refuse_the_type(job_type: str) -> None:
    """Registration does NOT open the generic-dispatch or approvable-action
    door. Invariant 2a clause (e): never reachable by any automated, scheduled
    or AI-driven actor, and never mintable by a human at `POST /api/v1/jobs`.

    BOTH GATES, ASSERTED SEPARATELY, because they bound different audiences: a
    HUMAN holding `jobs.dispatch`, and a MACHINE -- an approved action, a health
    remediation, an AI persona.
    """
    from app.core.approved_actions import non_approvable_reason
    from app.jobs.direct_dispatch import (
        DIRECTLY_DISPATCHABLE_JOB_TYPES,
        MODULE_GOVERNED_REASONS,
        module_governed_reason,
    )

    assert job_type not in DIRECTLY_DISPATCHABLE_JOB_TYPES
    assert job_type in MODULE_GOVERNED_REASONS

    direct = module_governed_reason(job_type)
    assert direct is not None
    assert len(direct) > 200, "the refusal must explain what it is protecting"
    assert "/api/v1/agent-push/requests" in direct

    machine = non_approvable_reason(job_type)
    assert machine is not None
    assert len(machine) > 200
    assert "automated" in machine or "AI" in machine


def test_the_module_registers_its_job_types() -> None:
    """B1 asserted `register(app)` mounted routes and NOTHING else. B2 registers
    the two job types there (and from the worker and gateway). Read from the AST:
    `register` must now call `register_job_types`."""
    called = {
        getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        for node in ast.walk(ast.parse((_PACKAGE / "__init__.py").read_text()))
        if isinstance(node, ast.Call)
    }
    assert "register_job_types" in called
    assert "register" in called or "include_router" in called


# --------------------------------------------------------------------------- #
# 2. The connection path is forbidden EVERYWHERE, forever (invariant #2)
# --------------------------------------------------------------------------- #

#: Names that would mean SOME FILE opened a connection to a target. The control
#: plane never connects (2a clause (a)); this stays forbidden in every file,
#: `dispatch.py` included. B2 does not weaken this by one identifier.
CONNECTION_NAMES = {
    "socket",
    "connect",
    "asyncssh",
    "paramiko",
    "winrm",
    "SSHClient",
    "create_connection",
}

CONNECTION_MODULES = {
    "socket",
    "ssl",
    "asyncssh",
    "paramiko",
    "winrm",
    "http.client",
    "httpx",
    "requests",
}

#: Names that would mean a file signs a job or decrypts a credential. Allowed in
#: `_SIGNING_EXEMPT` (that is what B2 is); forbidden everywhere else, so the
#: refusal ladder and the authorization tables stay pure.
SIGNING_DECRYPTION_NAMES = {
    "JobEngine",
    "signed_envelope",
    "canonical_bytes",
    "publish_job",
    "LocalEd25519Signer",
    "seal_bytes",
    "open_bytes",
    "wrapped_dek",
    "ciphertext",
}

SIGNING_DECRYPTION_MODULES = {
    "app.jobs.engine",
    "app.jobs.canonical",
    "app.core.signing",
    "app.core.vault",
    "app.gateway.dispatch",
}


def _identifiers(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add((node.asname or node.name).split(".")[0])
    return found


def _imported_modules(tree: ast.AST) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


@pytest.mark.parametrize("path", _SOURCES, ids=lambda p: p.name)
def test_no_source_opens_a_connection_to_a_target(path: Path) -> None:
    """Invariant #2 / 2a clause (a), asserted over EVERY file including the
    dispatcher. The control plane never connects to a target; the agent does,
    inside a verified job."""
    tree = ast.parse(path.read_text())
    name_offenders = _identifiers(tree) & CONNECTION_NAMES
    module_offenders = _imported_modules(tree) & CONNECTION_MODULES
    assert name_offenders == set() and module_offenders == set(), (
        f"{path.name} names {sorted(name_offenders)} / imports "
        f"{sorted(module_offenders)}. NO control-plane file may connect to a "
        "target -- that connection is the agent's, inside a verified job "
        "(invariant #2, 2a clause (a))"
    )


@pytest.mark.parametrize(
    "path", [p for p in _SOURCES if p.name not in _SIGNING_EXEMPT], ids=lambda p: p.name
)
def test_only_the_dispatcher_signs_or_decrypts(path: Path) -> None:
    """The signing and point-of-use decryption path lives in `dispatch.py` (and
    `api.py`, which wires it) and NOWHERE else. A second dispatch path grown in
    the refusal ladder, the authorization tables or the host-key store is named
    by this failure -- the same structural guard `site_server/dispatch.py` has."""
    tree = ast.parse(path.read_text())
    name_offenders = _identifiers(tree) & SIGNING_DECRYPTION_NAMES
    module_offenders = _imported_modules(tree) & SIGNING_DECRYPTION_MODULES
    assert name_offenders == set() and module_offenders == set(), (
        f"{path.name} names {sorted(name_offenders)} / imports "
        f"{sorted(module_offenders)}. Only {sorted(_SIGNING_EXEMPT)} may sign a "
        "job or decrypt a credential; every other file stays pure, so the "
        "refusal ladder cannot itself produce a signature (spec §3)"
    )


# --------------------------------------------------------------------------- #
# 3. The exemption is not vacuous: the dispatcher REALLY signs and decrypts
# --------------------------------------------------------------------------- #


def test_the_dispatcher_actually_signs_and_decrypts() -> None:
    """A whitelist that exempts a file which does nothing would be a hole
    dressed as a guarantee. `dispatch.py` must genuinely name the signing engine
    and the vault checkout -- otherwise the reshaping hid the absence of the very
    thing B2 exists to add. Mirrors B1's `test_the_forbidden_sets_are_not_vacuous`.
    """
    tree = ast.parse((_PACKAGE / "dispatch.py").read_text())
    identifiers = _identifiers(tree)
    modules = _imported_modules(tree)
    assert "JobEngine" in identifiers
    assert "signed_envelope" in identifiers
    assert "checkout" in identifiers, "the credential must be decrypted at point of use"
    assert "app.jobs.engine" in modules
    assert "app.core.vault" in modules


def test_the_refusal_ladder_stays_pure() -> None:
    """The single most important line of this file. `preflight.py` is the
    refusal ladder, and it must NEVER be able to sign anything -- that is what
    keeps the refusal on the near side of the signing boundary (spec §3). It
    names no signing, decryption or connection identifier at all."""
    tree = ast.parse((_PACKAGE / "preflight.py").read_text())
    identifiers = _identifiers(tree)
    modules = _imported_modules(tree)
    forbidden_names = SIGNING_DECRYPTION_NAMES | CONNECTION_NAMES
    forbidden_modules = SIGNING_DECRYPTION_MODULES | CONNECTION_MODULES
    assert identifiers & forbidden_names == set()
    assert modules & forbidden_modules == set()
