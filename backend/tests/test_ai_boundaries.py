"""THE KERNEL AI PACKAGE'S BOUNDARIES, AS PROPERTIES.

docs/modules/ai-automation.md §0.1, §2.1, §2.3; §13 tests 19 and 20.

**WHY THIS FILE EXISTS, AND WHY IT IS NOT `test_ai_admin_boundaries.py`.** The
orchestration machinery moved from `app/modules/ai_admin/` to `app/core/ai/`,
and every guarantee the old file asserted was anchored to `PACKAGE_ROOT =
app/modules/ai_admin/`. Moving the code without moving those assertions would
have silently un-asserted the egress-site count, the tool-type guard and the
prompt-constant property -- "otherwise the first act-mode task is also the task
that quietly deleted read mode's proof", which §0.1 names in as many words. This
file scans the NEW root; the module-scoped file keeps the assertions that are
genuinely about the admin persona, and now additionally asserts that the persona
package constructs no client of its own.

TWO ASSERTIONS HERE ARE EQUALITIES RATHER THAN MEMBERSHIPS, ON PURPOSE. A third
HTTP-client construction site is a new egress path; a MISSING one means the scan
is looking at the wrong thing, which is not a clean bill of health. And the
persona package list is derived from the live `PERSONAS` registry rather than
from a literal -- a hardcoded list means the fourth persona is unscanned, which
is the same defect as a denylist and invisible in the same way.

**THE READ-TOOL ALLOWLIST MOVED HERE TOO** (§19.9), and two assertions followed
it rather than being left behind: the allowlist-is-a-module-constant scan, which
`test_ai_admin_boundaries.py` used to run against
`app/modules/ai_admin/tools/registry.py`, and the new no-cross-persona-import
rule that keeps the wrong-direction import from coming back. The module-scoped
file keeps what is genuinely about the admin persona: that ITS tool set is
GET-only and credential-free, asserted off `ADMIN_PROFILE.tools`.
"""

import ast
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

import app.core.ai as core_ai_package
from app.core.ai import PERSONAS
from app.core.ai.tools import REGISTRY
from app.core.ai.tools.registry import _FORBIDDEN_GATES, _NEVER_A_TOOL
from app.core.rbac import PERMISSIONS

PACKAGE_ROOT = Path(core_ai_package.__file__).resolve().parent


def _source_files(root: Path) -> list[Path]:
    files = sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    # A scan that found nothing would pass every assertion below. Pinned against
    # a wrong root, a renamed package and a glob that stopped matching.
    assert len(files) >= 15, f"the sweep of {root} found only {len(files)} files; it is vacuous"
    return files


def _relative(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def _package_root(profile: Any) -> Path:
    """The directory of a persona's own package, from the profile's declared
    `package` -- which `register_persona` refuses to accept unless the module
    registering the profile actually lives inside it."""
    module = __import__(profile.package, fromlist=["_"])
    return Path(module.__file__).resolve().parent


def _imported_modules(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def _imported_dotted_names(path: Path) -> list[str]:
    """Every dotted name an import statement can NAME, including the one an
    `ImportFrom` alias completes.

    `_imported_modules` reports `from app.modules import ai_admin` as
    `"app.modules"`, which is right for a prefix test and WRONG for a test that
    asks "did this file import that package": the package is in the alias, not
    in the module. A guard that missed that form would be satisfied by a
    one-word edit to the import it forbids.
    """
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.append(module)
            names.extend(f"{module}.{alias.name}" if module else alias.name for alias in node.names)
    return names


@pytest.fixture(scope="module")
def registered_app() -> Any:
    """A built application, so every persona package has registered.

    `PERSONAS` is populated at IMPORT time from each persona package, and those
    packages are imported by `register(app)`. A test that read `PERSONAS`
    without building the app would find whatever an earlier test happened to
    import, which is how a persona-derived scan quietly becomes a one-persona
    scan.
    """
    from app.core.config import Settings
    from app.main import create_app

    # No database is touched: this fixture needs the ROUTER and the persona
    # registrations that building the app performs, and nothing else.
    return create_app(
        Settings(
            database_url="postgresql+asyncpg://unused/unused",
            database_admin_url="postgresql+asyncpg://unused/unused",
            oidc_issuer="https://issuer.test",
            oidc_jwks_url="https://jwks.test",
            ca_dir=Path("/tmp/its-ai-boundaries-ca"),
        )
    )


# --------------------------------------------------------------------------- #
# 1. §13 test 19 -- the kernel imports no business module.
# --------------------------------------------------------------------------- #


def test_core_ai_imports_no_module_package() -> None:
    """§2.3 property 1, and the assertion that makes the relocation mean
    something.

    A kernel package that imports `app.modules.<anything>` has inverted the
    dependency direction the move exists to fix, and it does so SILENTLY: the
    application still starts, every other test still passes, and the next
    persona imports the admin module by accident.
    """
    offenders = [
        f"{_relative(path, PACKAGE_ROOT)}: import {name}"
        for path in _source_files(PACKAGE_ROOT)
        for name in _imported_modules(path)
        if name == "app.modules" or name.startswith("app.modules.")
    ]
    assert offenders == [], "; ".join(offenders)


def test_core_ai_names_no_persona_in_its_source() -> None:
    """The other half of §0.1's boundary: the kernel does not know a persona's
    NAME either.

    Matched against string LITERALS rather than against the whole file, because
    this package's docstrings necessarily discuss the admin, technician and
    end-user personas while explaining what they may not do here -- and a test
    that forbade describing the boundary would push the next reader to delete
    the description instead of respecting it.
    """
    persona_names = {"admin", "technician", "end_user"}
    offenders: list[str] = []
    for path in _source_files(PACKAGE_ROOT):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and node.value in persona_names:
                offenders.append(f"{_relative(path, PACKAGE_ROOT)}:{node.lineno}: {node.value!r}")
    assert offenders == [], (
        "app/core/ai/ names a persona as a string literal: "
        + "; ".join(offenders)
        + " -- a persona's name belongs to that persona's package (§0.1)"
    )


# --------------------------------------------------------------------------- #
# 2-3. §13 test 20 -- exactly three egress sites here, and zero in any persona.
# --------------------------------------------------------------------------- #

_CLIENT_MARKERS = (
    "httpx.AsyncClient(",
    "httpx.Client(",
    "httpx.get(",
    "httpx.post(",
    "httpx.put(",
    "httpx.delete(",
    "httpx.request(",
    "httpx.stream(",
    "aiohttp",
    "requests.get(",
)

# THE COMPLETE SET, AS AN EQUALITY. `executor_staff.py` and `executor_portal.py`
# build the two in-process loopbacks (neither leaves this process), and
# `providers/_http.py` is the one outbound seam every provider client shares. A
# fourth entry is a new egress path; a missing one means the scan broke.
#
# **THIS WENT FROM TWO TO THREE DELIBERATELY, AND THE EQUALITY IS WHY IT WAS A
# DECISION RATHER THAN A THING THAT HAPPENED.** The third is
# `executor_portal.py`, required by §0.2: the resolver serves a `PortalContext`,
# which is not an `AuthContext`, so it cannot use the staff executor -- and the
# rejected alternative, "one executor with a principal-kind branch", is how the
# staff path and the portal path end up sharing a code path only one of them was
# audited for. Two executors, two types, no branch, and one more counted egress
# site. This test going red on this commit is the mechanism working.
_CLIENT_CONSTRUCTION_SITES = {
    "executor_staff.py",
    "executor_portal.py",
    "providers/_http.py",
}


def test_the_kernel_has_exactly_three_http_client_construction_sites() -> None:
    found = {
        _relative(path, PACKAGE_ROOT)
        for path in _source_files(PACKAGE_ROOT)
        for marker in _CLIENT_MARKERS
        if marker in path.read_text()
    }
    assert found == _CLIENT_CONSTRUCTION_SITES, (
        f"client construction sites changed: {sorted(found)} != "
        f"{sorted(_CLIENT_CONSTRUCTION_SITES)}"
    )


def test_no_persona_package_constructs_an_http_client(registered_app: Any) -> None:
    """§13 test 20's "now across every persona package".

    THE PACKAGE LIST IS DERIVED FROM `PERSONAS`, NEVER WRITTEN DOWN. A literal
    list means the fourth persona is unscanned -- the same defect as a denylist,
    and invisible in exactly the same way.
    """
    assert registered_app is not None
    assert PERSONAS, "no persona registered; every assertion below would be vacuous"

    offenders: list[str] = []
    scanned = 0
    for name, profile in PERSONAS.items():
        files = [p for p in _package_root(profile).rglob("*.py") if "__pycache__" not in p.parts]
        assert files, f"persona {name}'s package has no source files; the scan is vacuous"
        scanned += len(files)
        for path in files:
            source = path.read_text()
            offenders.extend(
                f"{name}: {path.name} contains {marker}"
                for marker in _CLIENT_MARKERS
                if marker in source
            )
    assert scanned >= 5, f"only {scanned} persona source files scanned; vacuous"
    assert offenders == [], "; ".join(offenders)


def test_no_persona_package_imports_another_persona_package(registered_app: Any) -> None:
    """§0.1's boundary in the direction the relocation tests do not cover, and
    the guard that closes §19.9.

    `test_core_ai_imports_no_module_package` catches the kernel reaching DOWN
    into a module. This catches a persona reaching SIDEWAYS into another
    persona, which is what actually happened: the read-tool allowlist was
    platform capability living in `app/modules/ai_admin/tools/`, so
    `ai_copilot/tools.py` imported its `ReadTool` objects from the admin
    persona's package. Nothing failed. The application started, every test
    passed, and the admin persona had quietly become a de facto kernel that is
    not reviewed as one -- `app/core/ai/__init__.py` names that outcome as the
    reason the move happened at all.

    THE PACKAGE LIST IS DERIVED FROM `PERSONAS`, NEVER WRITTEN DOWN, for the
    same reason the client scan derives its own: a literal list means the
    persona added next is the one nobody checked.

    The observation if this test were deleted is NONE -- which is why it is
    here. The fix it protects is a moved file; re-adding
    `from app.modules.ai_admin.tools import REGISTRY` to the copilot would look
    like a one-line convenience and would restore the finding exactly.
    """
    assert registered_app is not None
    assert len(PERSONAS) >= 2, (
        f"only {len(PERSONAS)} persona registered; 'another persona's package' is empty "
        f"and this test asserts nothing"
    )

    packages = {profile.package for profile in PERSONAS.values()}
    offenders: list[str] = []
    scanned = 0
    for name, profile in PERSONAS.items():
        others = packages - {profile.package}
        files = [p for p in _package_root(profile).rglob("*.py") if "__pycache__" not in p.parts]
        assert files, f"persona {name}'s package has no source files; the scan is vacuous"
        scanned += len(files)
        for path in files:
            for imported in _imported_dotted_names(path):
                offenders.extend(
                    f"{name}: {path.name} imports {imported} (belongs to {other})"
                    for other in sorted(others)
                    if imported == other or imported.startswith(other + ".")
                )
    assert scanned >= 5, f"only {scanned} persona source files scanned; vacuous"
    assert offenders == [], (
        "a persona package imports another persona's package: "
        + "; ".join(offenders)
        + " -- shared capability belongs in app/core/ai/ (§0.1, §19.9), not in whichever "
        + "persona happened to be built first"
    )


def test_the_kernel_imports_no_egress_helper_and_no_provider_sdk() -> None:
    """§0.10: removing the SSRF primitive beats guarding it. `app/core/egress.py`
    is written for webhook delivery -- it refuses redirects, caps the body and
    never returns the body in full, all wrong for a streaming completion -- so
    its presence here would mean an arbitrary-URL feature had been reintroduced
    and then guarded."""
    forbidden = ("app.core.egress", "openai", "anthropic.", "litellm", "langchain")
    offenders = [
        f"{_relative(path, PACKAGE_ROOT)}: import {name}"
        for path in _source_files(PACKAGE_ROOT)
        for name in _imported_modules(path)
        if any(name == bad or name.startswith(bad) for bad in forbidden)
    ]
    assert offenders == [], "; ".join(offenders)


# --------------------------------------------------------------------------- #
# 4. Every persona's tool set is a frozen constant holding only GET reads.
# --------------------------------------------------------------------------- #


def test_every_persona_tool_set_holds_only_get_read_tools(registered_app: Any) -> None:
    """`ReadTool.method` is `Literal["GET"]`, not a `read_only: bool`. A flag
    defaults, gets set wrong once, and nothing fails; a type that cannot express
    a mutation cannot be set wrong. Asserted PER PERSONA so a new persona's tool
    set is covered the day it registers."""
    from app.core.ai.types import ReadTool

    assert registered_app is not None
    assert PERSONAS
    for persona, profile in PERSONAS.items():
        assert profile.tools, f"persona {persona} has an empty tool set; this check is vacuous"
        for name, tool in profile.tools.items():
            assert isinstance(tool, ReadTool), f"{persona}/{name} is a {type(tool).__name__}"
            assert tool.method == "GET", f"{persona}/{name} is a {tool.method} tool"


def test_every_persona_gate_is_a_real_permission_code(registered_app: Any) -> None:
    assert registered_app is not None
    for persona, profile in PERSONAS.items():
        for name, tool in profile.tools.items():
            assert tool.permission in PERMISSIONS, f"{persona}/{name}: {tool.permission}"


def test_no_persona_tool_is_gated_on_a_credential_permission(registered_app: Any) -> None:
    """§0.5, re-asserted across every persona rather than inside one module's
    registry suite. Gate-based exclusion catches routes that MOVE: a path can be
    renamed, but the permission a secret-revealing route is gated on is far
    stickier."""
    assert registered_app is not None
    assert _FORBIDDEN_GATES, "the gate denylist is empty; this assertion is vacuous"
    for persona, profile in PERSONAS.items():
        for name, tool in profile.tools.items():
            assert tool.permission not in _FORBIDDEN_GATES, f"{persona}/{name}"


def test_the_allowlist_and_both_denylists_are_kernel_module_constants() -> None:
    """§0.2. **A DATABASE-BACKED ALLOWLIST WOULD PUT THE AGENT'S CAPABILITIES IN
    REACH OF WHATEVER COMPROMISES THE DATABASE**, and the whole prompt-injection
    story would then rest on database integrity rather than on code.

    **RE-AIMED BY §19.9's MOVE, NOT WEAKENED.** This assertion lived in
    `test_ai_admin_boundaries.py` and parsed
    `app/modules/ai_admin/tools/registry.py`. The file is now kernel, so the
    scan is here and the property it asserts is stronger by construction: it is
    no longer one persona's allowlist being a constant, it is the platform's.
    Left where it was, it would have raised `FileNotFoundError` -- which reads
    like a stale test and gets deleted, taking the guarantee with it.

    Observation if the allowlist became a table: this test sees a query, a
    session argument or an environment read in `registry.py`.
    """
    # THE SOURCE SCAN RUNS FIRST, deliberately. The type assertions below are a
    # consequence of the file being a literal; the scan is the thing that NAMES
    # what somebody added, and an assertion ordering that reports "it is a dict"
    # before "it calls getenv in a register() function" tells a reader the less
    # useful half.
    #
    # Parsed rather than grepped: that file's own prose necessarily talks about
    # `REGISTRY[...]` and about database reads while explaining why there are
    # none, and a scanner that could not tell a comment from code would be red on
    # landing -- and a test that is red on landing gets weakened, not fixed.
    registry_path = PACKAGE_ROOT / "tools" / "registry.py"
    assert registry_path.is_file(), (
        f"{registry_path} does not exist; the allowlist moved again and this scan is "
        f"asserting nothing"
    )
    tree = ast.parse(registry_path.read_text())
    offenders: list[str] = []
    for node in ast.walk(tree):
        # No runtime construction: no function or class, so there is no
        # `register()` and nothing to call at import time but the literal.
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            offenders.append(f"defines {node.name}")
        # No assignment into a subscript: `REGISTRY[name] = tool`.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript):
                    offenders.append("assigns into a subscript")
        # No call that could read a database, an environment or a file.
        if isinstance(node, ast.Call):
            called = ast.unparse(node.func)
            for forbidden in ("select", "getenv", "environ", "Settings", "open", "load"):
                if forbidden in called:
                    offenders.append(f"calls {called}")
    assert offenders == [], "; ".join(offenders)

    assert isinstance(REGISTRY, MappingProxyType), type(REGISTRY).__name__
    assert isinstance(_NEVER_A_TOOL, frozenset)
    assert isinstance(_FORBIDDEN_GATES, frozenset)

    # And it cannot be widened AT RUNTIME through the mapping it exposes. The
    # `Mapping` annotation only stops a type checker; this is the property that
    # holds against code that is not type checked, and it did not hold until
    # this test was written -- `REGISTRY[name] = tool` used to succeed, and the
    # first run of this assertion polluted every later test in the session.
    with pytest.raises(TypeError):
        REGISTRY["credentials.list"] = None  # type: ignore[index]
    assert "credentials.list" not in REGISTRY


# --------------------------------------------------------------------------- #
# 5. Every persona's system prompt is a module-level constant it owns.
# --------------------------------------------------------------------------- #


def test_every_persona_system_prompt_is_one_constant_with_one_assignment_site(
    registered_app: Any,
) -> None:
    """§8.1. A prompt assembled per turn is a prompt an attacker can hope to
    influence one field of; a module-level constant in the persona's OWN package
    is not. Asserted per persona, off the registered profile, so a persona that
    builds its prompt from a database row fails here rather than shipping."""
    assert registered_app is not None
    assert PERSONAS
    for persona, profile in PERSONAS.items():
        assert isinstance(profile.system_prompt, str) and profile.system_prompt, persona
        assert "{" not in profile.system_prompt, persona
        assert "%" not in profile.system_prompt, persona

        root = _package_root(profile)
        assigners = [
            path.name
            for path in root.rglob("*.py")
            if "__pycache__" not in path.parts
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Assign | ast.AnnAssign)
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Name) and target.id == "SYSTEM_PROMPT"
        ]
        assert assigners == ["prompt.py"], f"{persona}: {assigners}"


def test_the_persona_registry_cannot_be_widened_at_runtime(registered_app: Any) -> None:
    """The same immutability posture the tool registry takes, for the same
    reason: whatever compromises the database must not choose which personas
    exist, what they may read, or whether they may propose."""
    assert registered_app is not None
    with pytest.raises(TypeError):
        PERSONAS["intruder"] = None  # type: ignore[index]
    assert "intruder" not in PERSONAS


def test_a_second_profile_under_one_name_is_refused() -> None:
    from app.core.ai import PersonaProfile, register_persona
    from app.core.ai.executor_staff import invoke_tool
    from app.modules.ai_admin.profile import ADMIN_PROFILE

    # Re-registering the SAME object is what module reimport produces and must
    # be harmless.
    assert register_persona(ADMIN_PROFILE) is ADMIN_PROFILE

    with pytest.raises(ValueError, match="already registered"):
        register_persona(
            PersonaProfile(
                name="admin",
                package="app.modules.ai_admin",
                system_prompt="different",
                executor=invoke_tool,
                tools={},
            )
        )


def test_a_profile_that_misdeclares_its_package_is_refused() -> None:
    """The guard that keeps `_package_root` honest. A profile naming the wrong
    package would make every source scan above run against the wrong directory
    AND PASS -- a green build asserting nothing, which is the worst of the three
    possible outcomes."""
    from app.core.ai import PersonaProfile, register_persona
    from app.core.ai.executor_staff import invoke_tool

    with pytest.raises(ValueError, match="declares package"):
        register_persona(
            PersonaProfile(
                name="impostor",
                package="app.modules.ai_admin",
                system_prompt="x",
                executor=invoke_tool,
                tools={},
            )
        )
    assert "impostor" not in PERSONAS
