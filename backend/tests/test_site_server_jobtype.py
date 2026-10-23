"""`site.wake`: the params, the result, and registration in BOTH processes
(spec §5.1, §5.2, §5.3, §0.9, §0.10).

**THE TWO REGISTRATION TESTS ARE SEPARATE ON PURPOSE.** One process's
registration passing says nothing whatever about the other's — that is the
entire lesson `register_module_job_types`' docstring records IPAM learning, and
one combined test would re-learn it.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.site_server.errors import SiteWakeRejected
from app.modules.site_server.handler import WAKE_TTL_S, handle_wake_result
from app.modules.site_server.schema import SiteWakeParams, SiteWakeResult

REPO_ROOT = Path(__file__).resolve().parents[2]
WIRE_DIR = REPO_ROOT / "docs" / "kernel" / "wire-protocol"


# --------------------------------------------------------------------------- #
# SiteWakeParams
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "given",
    ["A4-BB-6D-11-22-33", "a4bb.6d11.2233", "A4BB6D112233", "a4:BB:6d:11:22:33"],
)
def test_the_mac_is_canonicalised_in_the_signed_params(given: str) -> None:
    """ASSERT THE NORMALISED VALUE, not that construction succeeded.

    The params are what get signed. Two spellings of one NIC produce two
    different signatures over the same intent, and two different rows in the
    audit log describing one act.
    """
    assert SiteWakeParams(target_mac=given, broadcast="10.20.7.255").target_mac == (
        "a4:bb:6d:11:22:33"
    )


def test_an_invalid_mac_is_refused() -> None:
    with pytest.raises(ValidationError):
        SiteWakeParams(target_mac="not-a-mac", broadcast="10.20.7.255")


def test_broadcast_refuses_a_cidr_and_accepts_an_address() -> None:
    """§0.2's rule made a SCHEMA property rather than a matter of discipline.

    A CIDR here would mean the agent re-derives the broadcast address, and an
    authorization revoked in the control plane would then depend on the
    endpoint's arithmetic. Both halves in one test: a validator that refused
    everything would pass the first assertion and break every wake.
    """
    with pytest.raises(ValidationError, match="not a CIDR"):
        SiteWakeParams(target_mac="a4:bb:6d:11:22:33", broadcast="10.20.7.0/24")

    accepted = SiteWakeParams(target_mac="a4:bb:6d:11:22:33", broadcast="10.20.7.255")
    assert accepted.broadcast == "10.20.7.255"


@pytest.mark.parametrize("port", [0, 1, 8, 22, 65535])
def test_only_ports_seven_and_nine_are_accepted(port: int) -> None:
    """Not a free integer: an arbitrary destination port turns a wake capability
    into a general "send a UDP datagram of my choosing to a broadcast address"
    primitive."""
    with pytest.raises(ValidationError):
        SiteWakeParams(target_mac="a4:bb:6d:11:22:33", broadcast="10.20.7.255", port=port)


@pytest.mark.parametrize("port", [7, 9])
def test_the_two_real_wake_on_lan_ports_are_accepted(port: int) -> None:
    params = SiteWakeParams(target_mac="a4:bb:6d:11:22:33", broadcast="10.20.7.255", port=port)
    assert params.port == port


@pytest.mark.parametrize("repeat", [0, 4, -1, 100])
def test_repeat_outside_one_to_three_is_refused(repeat: int) -> None:
    with pytest.raises(ValidationError):
        SiteWakeParams(target_mac="a4:bb:6d:11:22:33", broadcast="10.20.7.255", repeat=repeat)


def test_the_defaults_are_port_nine_and_three_repeats() -> None:
    params = SiteWakeParams(target_mac="a4:bb:6d:11:22:33", broadcast="10.20.7.255")
    assert (params.port, params.repeat) == (9, 3)


def test_an_unknown_key_is_refused() -> None:
    with pytest.raises(ValidationError):
        SiteWakeParams(
            target_mac="a4:bb:6d:11:22:33", broadcast="10.20.7.255", segment="10.20.7.0/24"
        )


# --------------------------------------------------------------------------- #
# SiteWakeResult
# --------------------------------------------------------------------------- #


def test_the_result_has_no_field_that_claims_anything_woke() -> None:
    """§0.9, asserted as an exact field set rather than as restraint.

    A magic packet is unauthenticated, unacknowledged and fire-and-forget: there
    is no reply to wait for, so `woke`, a reachability probe and a retry count
    are all fields that cannot carry true information. `extra="forbid"` means an
    agent cannot add one either, and this equality means nobody adds one here
    without deciding to.
    """
    assert set(SiteWakeResult.model_fields) == {"sent", "broadcast", "sent_at"}
    with pytest.raises(ValidationError):
        SiteWakeResult(
            sent=True, broadcast="10.20.7.255", sent_at=datetime.now(UTC), woke=True
        )


# --------------------------------------------------------------------------- #
# The result handler
# --------------------------------------------------------------------------- #


class _FakeJob:
    """Just the two attributes the handler reads. The handler writes nothing, so
    there is no session to stand up — and standing one up would hide that."""

    def __init__(self, broadcast: str) -> None:
        self.id = uuid4()
        self.params = {
            "target_mac": "a4:bb:6d:11:22:33",
            "broadcast": broadcast,
            "port": 9,
            "repeat": 3,
        }


async def test_a_matching_broadcast_is_accepted_and_a_differing_one_is_rejected() -> None:
    """THE DISCRIMINATING FIXTURE IS THE PAIR: the same job, two results that
    differ in exactly one field, producing two different outcomes.

    A test that only fed the matching result would pass against a handler with
    no comparison in it at all.
    """
    job = _FakeJob("10.20.7.255")
    now = datetime.now(UTC).isoformat()

    matching = {"sent": True, "broadcast": "10.20.7.255", "sent_at": now}
    assert await handle_wake_result(None, job, matching) is None  # type: ignore[arg-type]

    differing = {"sent": True, "broadcast": "10.20.255.255", "sent_at": now}
    with pytest.raises(SiteWakeRejected) as excinfo:
        await handle_wake_result(None, job, differing)  # type: ignore[arg-type]
    # The message names BOTH addresses, because "the wake failed" is not
    # actionable and "it went to 10.20.255.255 instead of 10.20.7.255" is.
    assert "10.20.255.255" in str(excinfo.value)
    assert "10.20.7.255" in str(excinfo.value)


async def test_a_malformed_result_is_rejected_rather_than_crashing() -> None:
    job = _FakeJob("10.20.7.255")
    with pytest.raises(SiteWakeRejected, match="malformed"):
        await handle_wake_result(None, job, {"sent": "yes"})  # type: ignore[arg-type]


def test_the_ttl_is_five_minutes_and_not_the_kernel_default() -> None:
    """§0.10. The kernel's 3600s default is correct for jobs generally and
    hazardous for this one: an offline site server's queued wake is drained on
    reconnect, so a wake requested at 22:00 would broadcast at 04:00 into a
    maintenance window that ended hours ago. A LATE WAKE IS A WRONG WAKE."""
    from app.jobs.engine import DEFAULT_TTL_S

    assert (WAKE_TTL_S, DEFAULT_TTL_S) == (300, 3600)


# --------------------------------------------------------------------------- #
# Registration — TWO processes, TWO tests
# --------------------------------------------------------------------------- #


def test_the_gateway_process_registers_the_site_wake_job_type() -> None:
    """AGENT RESULTS COMPLETE IN THE GATEWAY PROCESS, and `app/gateway/main.py`
    registers each module's job types for itself rather than calling
    `register_all`.

    The result handler above is the whole reason this matters: registered only
    in `site_server.register(app)`, `handle_wake_result` never runs against a
    real agent report, every mismatched broadcast completes as `succeeded`, and
    every test in this file still passes.

    *If the gateway's registration line were removed* this observes `site.wake`
    still missing from `registered_names()` after the gateway's own bootstrap
    ran.
    """
    from app.gateway.main import register_module_job_types
    from app.jobs import registry as job_registry
    from app.jobs.registry import registered_names

    job_registry._registry.pop("site.wake", None)  # noqa: SLF001
    assert "site.wake" not in registered_names()
    register_module_job_types()
    assert "site.wake" in registered_names()


def test_the_api_process_registers_the_site_wake_job_type() -> None:
    """The API process's path: `site_server.register(app)`, called by
    `register_all`. Both dispatch sites (this module's wake route and
    provisioning's run-start) are in this process, and `JobEngine.create`
    refuses an unregistered type."""
    from fastapi import FastAPI

    from app.jobs import registry as job_registry
    from app.jobs.registry import registered_names
    from app.modules import site_server

    job_registry._registry.pop("site.wake", None)  # noqa: SLF001
    assert "site.wake" not in registered_names()
    site_server.register(FastAPI())
    assert "site.wake" in registered_names()


def test_the_worker_process_registers_the_site_wake_job_type() -> None:
    """**THE THIRD PROCESS, ASSERTED BECAUSE DESCRIBING IT ALREADY FAILED.**

    `handler.py`'s docstring said for a whole cycle that `site.wake` was NOT
    registered in the worker, while `app/worker.py::register_worker_job_types`
    registered it and carried a comment explaining why it must. Two files, one
    fact, in contradiction, and nothing was capable of noticing: neither
    sentence was load-bearing on anything that ran.

    It is not future-proofing. `register_worker_job_types` is what three
    deny-by-default partitions over the job registry use as their definition of
    "the live registry", so a type missing here cannot be classified in them —
    and because `_registry` is process-global, whether it *appears* missing
    depends on which tests imported which modules first. That is a partition
    that holds or fails by test ORDERING, which is the silence those partitions
    exist to break.

    `_registry.pop` first, exactly as the gateway and API tests do: without it
    this passes on any run where an earlier import already registered the type,
    which is the same ordering-dependence being guarded against.
    """
    from app.jobs import registry as job_registry
    from app.jobs.registry import registered_names
    from app.worker import register_worker_job_types

    job_registry._registry.pop("site.wake", None)  # noqa: SLF001
    assert "site.wake" not in registered_names()
    register_worker_job_types()
    assert "site.wake" in registered_names()


#: Ways the handler docstring could restate the claim that shipped and was
#: false. Textual, and honest about it: prose has no AST, so this can only
#: recognise the sentence that actually drifted and its obvious rewordings. It
#: is the weaker half of the guard — the strong half is
#: `test_the_worker_process_registers_the_site_wake_job_type` above, which
#: exercises the real registration.
_WORKER_DENIALS = (
    "NOT REGISTERED IN THE WORKER",
    "NOT REGISTERED IN THE WORKER PROCESS",
    "IS NOT REGISTERED IN `APP/WORKER.PY`",
    "NO WORKER REGISTRATION",
)


def test_the_handler_docstring_does_not_contradict_the_worker() -> None:
    """The docstring and the code must not be able to disagree again.

    Two directions, in one tuple, because either alone is satisfiable by a
    docstring that is wrong in the other:

    * the docstring NAMES `app/worker.py::register_worker_job_types` — a
      registration nobody documents is the one that gets deleted as dead code;
    * the docstring does not DENY it in any of the forms `_WORKER_DENIALS`
      knows.

    Narrow on purpose. This cannot check that prose is TRUE, only that this
    module does not deny a registration `app/worker.py` demonstrably performs.
    That is exactly the contradiction that shipped, and it is the one shape of
    staleness a test can see. If `register_worker_job_types` ever legitimately
    stops registering `site.wake`, the test above goes red FIRST and names the
    real change, rather than this one sending someone to edit prose.
    """
    import ast

    from app import worker as worker_module
    from app.modules.site_server import handler as handler_module

    worker_tree = ast.parse(Path(worker_module.__file__).read_text())
    registrar = next(
        node
        for node in ast.walk(worker_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "register_worker_job_types"
    )
    registers_wake = "register_site_server_job_types()" in ast.unparse(registrar)

    docstring = (handler_module.__doc__ or "").upper()
    names_the_worker = "APP/WORKER.PY::REGISTER_WORKER_JOB_TYPES" in docstring
    denies_the_worker = any(denial in docstring for denial in _WORKER_DENIALS)

    assert (registers_wake, names_the_worker, denies_the_worker) == (True, True, False)


def test_register_all_reaches_this_module() -> None:
    """`register(app)` existing is not the same as `register_all` CALLING it.

    Asserted from the AST of `app/modules/__init__.py` rather than by invoking
    `register_all(FastAPI())`: several sibling modules read `app.state.job_engine`
    at registration time, so calling it against a bare app raises
    `AttributeError: 'State' object has no attribute 'job_engine'` before it ever
    reaches this module — a failure that says nothing about whether the call is
    there. `tests/test_site_server_api.py` exercises the real path through
    `api_env`; this pins the one line that path depends on.
    """
    import ast

    from app import modules as modules_pkg

    tree = ast.parse(Path(modules_pkg.__file__).read_text())
    calls = {
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "site_server.register(app)" in calls


def test_the_result_handler_is_the_one_registered() -> None:
    """Registering the type without its handler is a silent no-op: every
    mismatched broadcast would complete as `succeeded`."""
    from app.jobs.registry import get_job_type
    from app.modules.site_server.handler import register_job_types

    register_job_types()
    job_type = get_job_type("site.wake")
    assert (job_type.params_model, job_type.result_handler) == (
        SiteWakeParams,
        handle_wake_result,
    )


# --------------------------------------------------------------------------- #
# The exported wire contract
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("model", "filename"),
    [
        (SiteWakeParams, "SiteWakeParams.schema.json"),
        (SiteWakeResult, "SiteWakeResult.schema.json"),
    ],
)
def test_the_exported_schema_matches_the_model(model: type, filename: str) -> None:
    """A model change without a re-export goes red HERE.

    The committed files are the contract a deployed agent's Go structs are built
    against, and a deployed agent is older than the control plane it talks to.
    Nothing else in this repository can tell you a change here was breaking — so
    at minimum, nothing may change without somebody seeing it.
    """
    path = WIRE_DIR / filename
    assert path.exists(), f"{filename} not exported — run scripts/export_wire_schema.py"
    assert json.loads(path.read_text()) == model.model_json_schema()


def test_the_export_script_names_both_models() -> None:
    """The regeneration test above passes vacuously if the script never writes
    these files — it would compare a stale committed file against itself
    forever. This pins that the generator itself knows about them."""
    source = (REPO_ROOT / "scripts" / "export_wire_schema.py").read_text()
    assert "SiteWakeParams" in source
    assert "SiteWakeResult" in source
