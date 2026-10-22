"""The structural sweep (docs/modules/ai-automation.md §13 tests 19, 20, 22;
§8.3, §9.1).

**THESE PROPERTIES ARE RE-ASSERTED HERE ON PURPOSE.** Two of them already have a
home in `tests/test_ai_boundaries.py`, which is organised by COMPONENT -- so
deleting that file would delete an implementation's tests and two security
guarantees in the same commit, and the deletion would read as tidying up. This
file is organised by GUARANTEE, so the guarantee outlives any one file.

**THE AUDIT TEST IS THE ONE WITH NO OTHER HOME AND IT IS SENTINEL-BASED.**
"No `ai.*` audit metadata contains `rationale`, question text or answer text" is
a claim about a metadata dict assembled in four places; a grep over call sites is
a MENTION test and proves nothing about the dict. So a real turn and a real
proposal are driven with a distinctive sentinel in the question, the answer and
the rationale, and every `audit_events` row for the org is SELECTed and searched.
"""

import ast
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.ai as core_ai_package
from app.core.ai import PERSONAS
from app.core.ai.providers import TextBlock, ToolCallBlock
from app.models import AiSettings, AuditEvent
from app.modules.ai_admin import api as api_module
from tests.ai_act_fixtures import build_world
from tests.ai_admin_fakes import ScriptedProvider

CORE_ROOT = Path(core_ai_package.__file__).resolve().parent
AI_PREFIX = "/api/v1/ai"


@pytest.fixture(scope="module")
def registered_app() -> Any:
    """A built application, so every persona package has registered.

    `PERSONAS` is populated at IMPORT time by each persona package, and those
    packages are imported by `register(app)`. A scan that read `PERSONAS`
    without building the app would find whatever an earlier test happened to
    import -- which is how a persona-derived sweep quietly becomes a
    one-persona sweep, and how the persona added next is the one nobody checked.
    """
    from app.core.config import Settings
    from app.main import create_app

    return create_app(
        Settings(
            database_url="postgresql+asyncpg://unused/unused",
            database_admin_url="postgresql+asyncpg://unused/unused",
            oidc_issuer="https://issuer.test",
            oidc_jwks_url="https://jwks.test",
            ca_dir=Path("/tmp/its-ai-structure-ca"),
        )
    )


def _package_root(profile: Any) -> Path:
    module = __import__(profile.package, fromlist=["_"])
    return Path(module.__file__).resolve().parent


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _code_of(path: Path) -> str:
    """The file's source with every docstring removed.

    Docstrings in this layer NECESSARILY discuss `publish_job`, `JobEngine` and
    `app.modules.*` while explaining why they are not reached -- and a scanner
    that could not tell a comment from code would be red on landing. A test that
    is red on landing gets weakened, not fixed. `ast.unparse` drops comments
    outright; the walk below blanks each docstring.
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


# --------------------------------------------------------------------------- #
# 4. §13 test 19 -- the kernel imports no business module.
# --------------------------------------------------------------------------- #


def test_core_ai_imports_no_business_module() -> None:
    """§2.3 property 1, RE-ASSERTED so deleting `tests/test_ai_boundaries.py`
    does not delete the property.

    A kernel package importing `app.modules.<anything>` inverts the dependency
    direction the relocation exists to fix, and it does so SILENTLY: the
    application still starts, every other test still passes, and the next persona
    imports the admin module by accident.
    """
    files = _python_files(CORE_ROOT)
    assert len(files) >= 15, f"the sweep of {CORE_ROOT} found {len(files)} files; vacuous"
    offenders: list[str] = []
    for path in files:
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            offenders.extend(
                f"{path.name}: {name}"
                for name in names
                if name == "app.modules" or name.startswith("app.modules.")
            )
    assert offenders == [], "; ".join(offenders)


# --------------------------------------------------------------------------- #
# 5. §13 test 20 -- the egress-site count, across core AND every persona.
# --------------------------------------------------------------------------- #

_CLIENT_MARKERS = (
    "httpx.AsyncClient(",
    "httpx.Client(",
    "httpx.get(",
    "httpx.post(",
    "httpx.request(",
    "httpx.stream(",
    "aiohttp",
    "requests.get(",
)

#: Three, and it is an EQUALITY. A fourth is a new egress path nobody counted; a
#: missing one means the scan broke, which is not a clean bill of health.
#: `executor_staff.py` and `executor_portal.py` are the two in-process loopbacks
#: (§0.2's two executors, no branch) and `providers/_http.py` is the one outbound
#: seam every provider client shares.
_EXPECTED_CORE_SITES = {"executor_staff.py", "executor_portal.py", "providers/_http.py"}


def test_the_egress_site_count_across_core_and_every_persona(registered_app: Any) -> None:
    assert registered_app is not None
    assert len(PERSONAS) >= 3, f"only {len(PERSONAS)} personas registered; the sweep is partial"

    core_sites = {
        str(path.relative_to(CORE_ROOT))
        for path in _python_files(CORE_ROOT)
        for marker in _CLIENT_MARKERS
        if marker in path.read_text()
    }
    assert core_sites == _EXPECTED_CORE_SITES, sorted(core_sites)

    scanned = 0
    offenders: list[str] = []
    for name, profile in PERSONAS.items():
        files = _python_files(_package_root(profile))
        assert files, f"persona {name}'s package has no source files; the scan is vacuous"
        scanned += len(files)
        for path in files:
            source = path.read_text()
            offenders.extend(
                f"{name}/{path.name}: {marker}"
                for marker in _CLIENT_MARKERS
                if marker in source
            )
    assert scanned >= 15, f"only {scanned} persona source files scanned; vacuous"
    assert offenders == [], "; ".join(offenders)


# --------------------------------------------------------------------------- #
# 7. Nothing reaches a device except through `JobEngine.create()`, and nothing
#    publishes before its transaction commits.
# --------------------------------------------------------------------------- #


def _scan_roots(registered_app: Any) -> dict[str, Path]:
    assert registered_app is not None
    roots = {"app/core/ai": CORE_ROOT}
    for name, profile in PERSONAS.items():
        roots[f"persona:{name}"] = _package_root(profile)
    return roots


def test_at_most_one_file_per_package_names_publish_job(registered_app: Any) -> None:
    """§8.1's shape, applied to this layer: a signed job reaches a device through
    `JobEngine.create()` and then `publish_job`, and nothing else.

    **`app/core/ai/` MUST NAME IT ZERO TIMES.** `confirm_action` RETURNS the
    envelope and its caller publishes it -- `ConfirmOutcome`'s docstring gives the
    reason and `app/modules/deploy/scripts.py::run_script` states the contract: a
    job published before the caller's tenant transaction commits could be handed
    to an agent with no durable server record. A persona package may name it in
    exactly one file, its router.
    """
    for label, root in _scan_roots(registered_app).items():
        naming = sorted(
            path.name for path in _python_files(root) if "publish_job" in _code_of(path)
        )
        if label == "app/core/ai":
            assert naming == [], f"{label} publishes: {naming}"
        else:
            assert len(naming) <= 1, f"{label} names publish_job in {naming}"


def test_no_publish_job_call_sits_inside_a_tenant_transaction(registered_app: Any) -> None:
    """The "after commit" half, on the SYNTAX TREE rather than by review, because
    the failure it guards is invisible in testing: a job published from inside
    the block still reaches the agent in every green test, and only diverges when
    the commit afterwards fails -- at which point an agent is executing a signed
    job the server has no record of having issued.
    """
    offenders: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self, label: str, filename: str) -> None:
            self.label = label
            self.filename = filename
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
                and item.context_expr.func.id in {"org_session", "portal_session"}
                for item in node.items
            )
            if opens_tenant_txn:
                inner = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
                if "publish_job" in inner:
                    offenders.append(f"{self.label}/{self.filename}::{self.function}")
            self.generic_visit(node)

    scanned = 0
    for label, root in _scan_roots(registered_app).items():
        for path in _python_files(root):
            scanned += 1
            _Visitor(label, path.name).visit(ast.parse(path.read_text()))
    assert scanned >= 30, f"only {scanned} files walked; the visitor found nothing to visit"
    assert offenders == [], (
        f"these publish INSIDE their tenant transaction: {offenders}; the envelope "
        f"must be published only after the block exits"
    )


def test_the_layer_creates_jobs_in_exactly_one_place(registered_app: Any) -> None:
    """`JobEngine.create` is called from ONE body in this whole layer --
    `confirm_action` -- and from no persona package at all. A second call site is
    a second answer to "may this run", and only one of them went through §9.2's
    three checks."""
    creators: list[str] = []
    for label, root in _scan_roots(registered_app).items():
        for path in _python_files(root):
            code = _code_of(path)
            if "job_engine.create(" in code or "JobEngine(" in code:
                creators.append(f"{label}/{path.name}")
    assert creators == ["app/core/ai/act.py"], creators


# --------------------------------------------------------------------------- #
# 6. §13 test 22 -- no `ai.*` audit metadata carries prose.
# --------------------------------------------------------------------------- #

#: Three distinct sentinels, one per prose channel, so a failure NAMES WHICH
#: channel leaked rather than reporting "some text appeared".
QUESTION_SENTINEL = "zqxjkbquestion7742"
ANSWER_SENTINEL = "zqxjkbanswer7742"
RATIONALE_SENTINEL = "zqxjkbrationale7742"


async def test_no_ai_audit_metadata_anywhere_contains_prose(
    api_env: Any, monkeypatch: Any
) -> None:
    """**§13 TEST 22, ASSERTED OVER STORED METADATA RATHER THAN OVER A CALL
    SITE.**

    §8.3: acts log identifiers, questions log hashes, NEITHER logs prose. The
    audit chain is append-only, hash-chained and un-redactable, and it is where an
    attacker who can write a ticket body would most like their text to end up
    permanently -- so a grep over the four `audit_service.record` calls would
    prove nothing about a metadata dict assembled somewhere else.

    A real turn asks a question carrying one sentinel, a real model answers with
    a second, and a real proposal carries a third in its `rationale`. Then EVERY
    `audit_events` row for the org is read back and searched. The three
    non-vacuity assertions come first: the sentinels have to be somewhere before
    their absence somewhere else means anything.
    """
    world = await build_world(
        api_env,
        tag="sentinel",
        codes=("ai.use", "ai.act.propose", "devices.read", "jobs.dispatch"),
    )
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        row = await session.get(AiSettings, world.org_id)
        if row is None:
            row = AiSettings(org_id=world.org_id)
            session.add(row)
        await session.commit()

    provider = ScriptedProvider(
        [
            [
                ToolCallBlock(
                    id="1",
                    name="actions.propose",
                    arguments={
                        "action_key": world.action_key,
                        "device_id": str(world.device_id),
                        "rationale": f"I suggest this because {RATIONALE_SENTINEL}",
                    },
                )
            ],
            [TextBlock(text=f"I have prepared it. {ANSWER_SENTINEL}")],
        ]
    )
    monkeypatch.setattr(api_module, "_provider_for", lambda: provider)

    created = await api_env.client.post(
        f"{AI_PREFIX}/sessions", headers=world.headers, json={"mode": "act"}
    )
    assert created.status_code == 201, created.text
    turn = await api_env.client.post(
        f"{AI_PREFIX}/sessions/{created.json()['id']}/turns",
        headers=world.headers,
        json={"question": f"is anything stale? {QUESTION_SENTINEL}"},
    )
    assert turn.status_code == 200, turn.text

    # NON-VACUITY: each sentinel really is in the system, where it belongs.
    from app.models import AiAction, AiMessage

    async with AsyncSession(api_env.admin_engine) as session:
        transcript = " ".join(
            (
                await session.execute(
                    select(AiMessage.content).where(AiMessage.org_id == world.org_id)
                )
            ).scalars()
        )
        rationales = " ".join(
            (
                await session.execute(
                    select(AiAction.rationale).where(AiAction.org_id == world.org_id)
                )
            ).scalars()
        )
        events = list(
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.org_id == world.org_id)
                )
            )
            .scalars()
            .all()
        )
    assert QUESTION_SENTINEL in transcript, "the question sentinel never reached the transcript"
    assert ANSWER_SENTINEL in transcript, "the answer sentinel never reached the transcript"
    assert RATIONALE_SENTINEL in rationales, "the rationale sentinel never reached ai_action"

    ai_events = [event for event in events if event.action.startswith("ai.")]
    assert len(ai_events) >= 3, [event.action for event in ai_events]

    import json

    leaked = [
        f"{event.action}: {sentinel}"
        for event in ai_events
        for sentinel in (QUESTION_SENTINEL, ANSWER_SENTINEL, RATIONALE_SENTINEL)
        if sentinel in json.dumps(event.meta or {})
    ]
    assert leaked == [], (
        f"model-adjacent prose reached the append-only audit chain: {leaked}; §8.3 -- "
        f"acts log identifiers, questions log hashes, neither logs prose"
    )


async def test_the_query_event_carries_the_hash_of_the_question_it_refuses_to_store(
    api_env: Any, monkeypatch: Any
) -> None:
    """The positive half. Without it, "no prose in the audit log" is satisfied by
    an audit log that records nothing about the question at all -- which would
    also fail invariant #4."""
    import json

    from app.core.ai.store import question_sha256

    world = await build_world(api_env, tag="sentinel-hash", codes=("ai.use",))
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        session.add(AiSettings(org_id=world.org_id))
        await session.commit()
    monkeypatch.setattr(
        api_module, "_provider_for", lambda: ScriptedProvider([[TextBlock(text="ok")]])
    )

    question = f"which devices are stale? {QUESTION_SENTINEL}"
    created = await api_env.client.post(f"{AI_PREFIX}/sessions", headers=world.headers)
    turn = await api_env.client.post(
        f"{AI_PREFIX}/sessions/{created.json()['id']}/turns",
        headers=world.headers,
        json={"question": question},
    )
    assert turn.status_code == 200, turn.text

    async with AsyncSession(api_env.admin_engine) as session:
        (event,) = list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.org_id == world.org_id)
                    .where(AuditEvent.action == "ai.query")
                )
            )
            .scalars()
            .all()
        )
    meta = json.dumps(event.meta or {})
    assert question_sha256(question) in meta
    assert QUESTION_SENTINEL not in meta
    assert "which devices are stale" not in meta


def test_the_sentinels_are_distinct_and_would_not_collide() -> None:
    """A guard on the guard: three sentinels that shared a prefix would make
    "which channel leaked" unanswerable, and one that appeared in ordinary text
    would make the test flap."""
    sentinels = (QUESTION_SENTINEL, ANSWER_SENTINEL, RATIONALE_SENTINEL)
    assert len(set(sentinels)) == 3
    for one in sentinels:
        assert sum(other.startswith(one) or one.startswith(other) for other in sentinels) == 1


def test_every_persona_package_is_scanned_by_this_file(registered_app: Any) -> None:
    """The non-vacuity floor for the whole file, stated once and loudly: every
    scan above derives its roots from `PERSONAS`, so a registry that lost a
    persona would silently narrow all of them."""
    assert registered_app is not None
    assert set(PERSONAS) == {"admin", "technician", "end_user"}, sorted(PERSONAS)
    for name, profile in PERSONAS.items():
        assert _package_root(profile).exists(), name
        assert profile.package.startswith("app.modules."), (name, profile.package)


def test_the_org_id_predicate_is_present_on_every_ai_action_read() -> None:
    """§9.1: "an explicit `org_id` predicate on every query in addition to RLS".

    Asserted structurally because the behavioural version needs the admin engine
    and lives with each service; this catches a NEW read added later that leans
    on RLS alone. Every `select(AiAction)` / `update(AiAction)` in the layer must
    mention `AiAction.org_id` in the same statement.
    """
    offenders: list[str] = []
    for path in _python_files(CORE_ROOT):
        code = _code_of(path)
        for chunk in code.split("select(AiAction")[1:]:
            if "AiAction.org_id" not in chunk[:600]:
                offenders.append(f"{path.name}: a select(AiAction...) with no org_id predicate")
        for chunk in code.split("update(AiAction")[1:]:
            if "AiAction.org_id" not in chunk[:600]:
                offenders.append(f"{path.name}: an update(AiAction...) with no org_id predicate")
    assert offenders == [], "; ".join(offenders)


def test_no_persona_package_reads_ai_action_directly_except_to_list_its_own(
    registered_app: Any,
) -> None:
    """Routers may LOOK at `ai_action` (the copilot returns the ids its turn
    proposed) but must not write one: `AiAction(` construction and any
    `state =` assignment belong to `app/core/ai/act.py`, which is the only body
    that went through §6.1's seven steps."""
    offenders: list[str] = []
    for name, profile in PERSONAS.items():
        for path in _python_files(_package_root(profile)):
            code = _code_of(path)
            if "AiAction(" in code:
                offenders.append(f"{name}/{path.name}: constructs an AiAction")
    assert offenders == [], "; ".join(offenders)


def test_the_kernel_holds_the_only_definition_of_the_act_tool(registered_app: Any) -> None:
    """One `ActTool` class in the process, and it is in `app/core/ai/act.py`."""
    definitions: list[str] = []
    roots = _scan_roots(registered_app)
    for label, root in roots.items():
        for path in _python_files(root):
            for node in ast.parse(path.read_text()).body:
                if isinstance(node, ast.ClassDef) and node.name == "ActTool":
                    definitions.append(f"{label}/{path.name}")
    assert definitions == ["app/core/ai/act.py"], definitions


def test_no_module_outside_the_kernel_declares_an_ai_model(registered_app: Any) -> None:
    """The four transcript tables and the two act tables live in
    `app/models/ai.py` (§2.1, as amended). A declarative class in a persona
    package would be a second AI schema nobody reconciles."""
    from app.models.base import Base

    offenders: list[str] = []
    for name, profile in PERSONAS.items():
        module = __import__(profile.package, fromlist=["_"])
        for path in _python_files(Path(module.__file__).resolve().parent):
            for node in ast.parse(path.read_text()).body:
                if isinstance(node, ast.ClassDef) and any(
                    isinstance(base, ast.Name) and base.id == "Base" for base in node.bases
                ):
                    offenders.append(f"{name}/{path.name}: {node.name}")
    assert offenders == [], "; ".join(offenders)
    assert Base is not None


def test_no_persona_package_moves_an_ai_action_state(registered_app: Any) -> None:
    """The state machine (§5.2) is what makes a second confirm a 409 and a
    settled row un-resettable, and `app/core/ai/act.py` and `sweep.py` are the
    only bodies that move it. A persona router assigning `.state` would be a
    transition that went through none of §6.5's re-checks."""
    assert registered_app is not None
    offenders = [
        f"{name}/{path.name}"
        for name, profile in PERSONAS.items()
        for path in _python_files(_package_root(profile))
        if ".state = " in _code_of(path)
    ]
    assert offenders == [], "; ".join(offenders)
