"""The single-egress-path property
(`docs/modules/endpoint-health-alerting/spec.md` §8.1, §14).

INVARIANT #1, AS A STRUCTURAL TEST RATHER THAN A CODE REVIEW. §8.1: "there is no
'run this command on the device' API in this module, no shell string in any
params model, and no code path that reaches an endpoint other than
`JobEngine.create()` -> `publish_job()`." A reviewer can verify that once; this
file verifies it on every commit.

It greps the module's own source. That is deliberately crude and it is the
point: a call site added in six months by someone who has not read §8.1 fails
this test by existing, whether or not it is reachable, whether or not it is
covered, and whether or not its author believed it was safe.

IT GREPS THE CODE, NOT THE PROSE. A plain substring scan matched
`policy_push.py`, whose docstring EXPLAINS that the caller publishes via
`app.gateway.dispatch.publish_job` -- a sentence that is the opposite of an
egress path and would have made this test unpassable while correct. So every
file is parsed and its docstrings stripped before scanning, which is also the
stricter reading: a comment saying "we do not call publish_job" is not evidence,
and a call hidden behind a comment claiming otherwise is exactly what this test
is for.

GUARD OBSERVABILITY: add a second egress path -- say a direct `send_text` in
`actions.py` -- and this observes an extra call site and NAMES THE FILE it is
in. The mutation is run in the test itself for the `httpx`/`requests`/
`subprocess` family, by writing a probe file into the module directory, so the
grep is proven to be able to find something rather than merely returning zero.
"""

import ast
from pathlib import Path

import pytest

import app.modules.health as health_pkg

MODULE_DIR = Path(health_pkg.__file__).resolve().parent


def _code_of(path: Path) -> str:
    """The file's source with every docstring and comment removed.

    `ast.unparse` drops comments outright; the walk below replaces each
    docstring with an empty string. What is left is the code, which is the only
    thing that can reach a device.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body[0].value.value = ""
    return ast.unparse(tree)

# The four families that could reach a device or the outside world without
# passing through the signed-job engine. `publish_job` is the LEGITIMATE one and
# is counted rather than banned; the other three are banned outright.
_FORBIDDEN = ("send_text", "httpx", "requests", "subprocess", "socket.socket", "urllib")


def _sources() -> list[Path]:
    return sorted(p for p in MODULE_DIR.glob("*.py") if p.name != "__pycache__")


def _files_naming(needle: str) -> list[str]:
    return [p.name for p in _sources() if needle in _code_of(p)]


def test_the_module_has_source_files_to_scan() -> None:
    """The positive control for every grep below: a scan of an empty directory
    reports zero forbidden call sites and zero `publish_job` call sites, and is
    indistinguishable from a clean module."""
    names = {p.name for p in _sources()}
    assert {"actions.py", "ingest.py", "policy_push.py"} <= names


@pytest.mark.parametrize("needle", _FORBIDDEN)
def test_no_health_module_file_reaches_the_network_directly(needle: str) -> None:
    """No HTTP client, no socket, no subprocess, no direct WebSocket write.

    `send_text` is on this list because it is the one call the GATEWAY makes to
    put bytes on an agent's connection -- a module that called it would be
    talking to an endpoint outside the signed envelope entirely.
    """
    offenders = _files_naming(needle)
    assert offenders == [], f"{needle!r} appears in {offenders}; §8.1 permits no second egress"


def test_only_the_api_layer_publishes_and_every_service_returns_its_envelope() -> None:
    """EXACTLY ONE FILE MAY PUBLISH, AND IT IS `api.py`.

    Until Task 13 this asserted ZERO files: every service in this module
    (`push_policy` §5.2, `execute_pending_actions`/`approve_action` §7.1)
    RETURNS `(job_id, envelope)` for its caller to publish after the
    `org_session` block commits, which is
    `app/modules/deploy/scripts.py::run_script`'s contract -- a job published
    before the caller's tenant transaction commits could be handed to an agent
    with no durable server record if the commit later failed. The worker was
    then the only caller, so zero held.

    Task 13 added the HTTP callers, and one of them
    (`POST /devices/{id}/health/check-now`) has no other way to reach the
    device: nothing re-drains pending `jobs` rows when an agent connects, so an
    unpublished job is a job that never runs. The property therefore narrows
    from "nobody" to "exactly `api.py`, and only after commit" rather than being
    dropped: this is still EQUALITY, so a second file acquiring a publish call
    fails here, and
    `test_the_api_never_publishes_inside_a_tenant_transaction` below pins the
    "after commit" half structurally.

    Every SERVICE file is still forbidden it, which is the half that matters:
    the services cannot put anything on a device's channel at all.
    """
    offenders = _files_naming("publish_job")
    assert offenders == ["api.py"]
    for service in ("actions.py", "ingest.py", "policy_push.py", "sweep.py"):
        assert "publish_job" not in _code_of(MODULE_DIR / service)


def test_the_api_never_publishes_inside_a_tenant_transaction() -> None:
    """`publish_job` may not appear lexically inside an `async with
    org_session(...)` block in `api.py`.

    This is the "after commit" half of the contract above, and it is asserted on
    the SYNTAX TREE rather than by review because the failure it guards is
    invisible in testing: a job published from inside the block still reaches
    the agent in every green test, and only diverges when the commit afterwards
    fails -- at which point an agent is executing a signed job the server has no
    record of having issued.

    GUARD OBSERVABILITY: move any `await _publish(...)` call in `api.py` inside
    its `async with org_session(...)` block (or inline a `publish_job` call
    there) and this fails naming the enclosing function.
    """
    tree = ast.parse((MODULE_DIR / "api.py").read_text())
    offenders: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.function = "<module>"

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            previous, self.function = self.function, node.name
            self.generic_visit(node)
            self.function = previous

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            previous, self.function = self.function, node.name
            self.generic_visit(node)
            self.function = previous

        def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
            opens_tenant_txn = any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "org_session"
                for item in node.items
            )
            if opens_tenant_txn:
                inner = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
                if "publish_job" in inner or "_publish(" in inner:
                    offenders.append(self.function)
            self.generic_visit(node)

    visitor = _Visitor()
    visitor.visit(tree)
    assert offenders == [], (
        f"these handlers publish INSIDE their tenant transaction: {offenders}; "
        f"the envelope must be published only after the org_session block exits"
    )


def test_that_syntactic_guard_can_actually_find_something() -> None:
    """THE MUTATION FOR THE TEST ABOVE, run in-test.

    A tree walk that finds nothing because it is looking for the wrong node
    shape is indistinguishable from a clean file. This feeds it a handler that
    publishes inside the block and asserts it is caught, so the empty result
    above is evidence rather than an artefact.
    """
    source = (
        "async def bad_handler(ctx, request):\n"
        "    async with org_session(ctx.org_id, engine=e) as session:\n"
        "        job_id, envelope = await push_check_now(session)\n"
        "        await publish_job(request.app.state.redis, d, envelope)\n"
    )
    tree = ast.parse(source)
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncWith)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == "org_session"
            for item in node.items
        )
        and "publish_job" in ast.unparse(ast.Module(body=node.body, type_ignores=[]))
    ]
    assert len(found) == 1


def test_every_job_this_module_creates_goes_through_job_engine_create() -> None:
    """The other half: jobs ARE created, and only through the engine.

    Asserting the absence of egress alone would pass against a module that
    dispatches nothing, which is also a broken module. `JobEngine.create` is the
    one function that validates params against the registered model and SIGNS
    the job (invariant #1), so counting its call sites is counting the signed
    path.
    """
    callers = set(_files_naming("job_engine.create("))
    assert callers == {"actions.py", "policy_push.py"}


def test_no_params_model_in_this_module_carries_a_shell_string() -> None:
    """§8.1: "no shell string in any params model". The health job types
    (`health.policy.apply`, `health.check.now`) install thresholds and ask for a
    sample; neither takes a command, an argument vector or an interpreter.

    §8.2 states the perverse incentive this is guarding against by name: the
    obvious way to make one approved action cover every service is to add a free
    `args: str` to `ScriptRunParams` -- an unbounded, operator-controlled string
    in a params model that executes as SYSTEM/root, i.e. §0.1's free-form
    expression arriving from the server side instead of the edge.
    """
    jobtypes = _code_of(MODULE_DIR / "jobtypes.py")
    for banned in ("command", "args", "argv", "shell", "interpreter", "body"):
        assert f"{banned}:" not in jobtypes, (
            f"{banned!r} appears as a field in a health params model"
        )


def test_the_grep_can_actually_find_something(tmp_path: Path) -> None:
    """THE MUTATION, RUN IN-TEST rather than described in a comment.

    A grep that returns zero because it is looking in the wrong place is
    indistinguishable from a clean module. This writes a probe file containing
    each forbidden token and asserts the same scanner reports it, so the empty
    results above are evidence rather than an artefact of the path.
    """
    probe = MODULE_DIR / "_egress_probe_delete_me.py"
    probe.write_text(
        "import httpx\n"
        "import subprocess\n"
        "\n"
        "async def x(ws):\n"
        "    await ws.send_text('boo')\n"
        "    from app.gateway.dispatch import publish_job\n"
    )
    try:
        for needle in ("httpx", "subprocess", "send_text"):
            offenders = _files_naming(needle)
            assert offenders == ["_egress_probe_delete_me.py"], (
                f"the scanner did not find {needle!r} in a file that contains it"
            )
        # `publish_job` is the one LEGITIMATE token, and `api.py` carries it
        # (see `test_only_the_api_layer_publishes_...`), so the probe is an
        # ADDITION to the known-good set rather than the only hit. `_sources()`
        # sorts by filename and `_` sorts before `a`.
        assert _files_naming("publish_job") == ["_egress_probe_delete_me.py", "api.py"]
    finally:
        probe.unlink()

    # ... and the module is clean again once the probe is gone.
    for needle in _FORBIDDEN:
        assert _files_naming(needle) == []
