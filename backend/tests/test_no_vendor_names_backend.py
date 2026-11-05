"""NO USER-VISIBLE STRING THE BACKEND SENDS MAY NAME THE IDENTITY VENDOR.

The backend half of the word audit in
`docs/superpowers/specs/2026-08-19-invisible-identity-layer-design.md` §1.3.
`frontend/src/lib/noVendorNames.test.ts` is the other half; it was built and
this one was not, so every offender that lives in an API `detail` -- and they
render in the buyer's browser exactly like a JSX string does -- was unguarded.

SCOPED TO WHAT A USER CAN SEE, AND NO WIDER, for the reason the frontend guard
states about itself: a guard broader than its own claim gets deleted the first
time it is inconvenient. The platform DOES integrate with Keycloak and source
that pretended otherwise would be lying about its own dependency. So module
names, imports, class names, docstrings, comments, settings keys and log
messages are all exempt and must stay accurate. What is checked is the text
that leaves the process in a response body:

  * `HTTPException(status, "...")` and any `detail=` keyword anywhere;
  * the second positional of a refusal type that carries an HTTP detail
    (`UserAdminError`), because `api.py` turns it straight into an
    `HTTPException`, and `service.py` also copies it into
    `CreatedUser.enrolment_email_error`, which is a RESPONSE FIELD;
  * Pydantic field defaults and `Field(description=...)`, which is the text
    the generated schema hands to any client that renders it;
  * the probe DISPLAY LABELS (`app.api.probes.PROBE_LABELS`), which are the
    one user-visible string no static scan of literals could ever have
    caught: `keycloak_events` reached the status page as a Python
    dict key, not as a message.

THREE THINGS THE EXTRACTOR DOES THAT A NAIVE ONE WOULD NOT, each of them a
hole somebody would otherwise walk through:

  1. f-strings. `f"Keycloak refused to {what}"` is a `JoinedStr`, not a
     `Constant`; the three live offenders this guard was written to catch were
     all f-strings.
  2. Module-level constants. `raise HTTPException(503, _NOT_CONFIGURED)`
     passes a `Name`. `setup.py` writes its two longest user-facing sentences
     that way, so a scan that only reads literals at the call site would have
     declared the file clean while it said anything at all.
  3. Implicit concatenation. The offenders are wrapped across lines, which
     `ast` folds into one `Constant` -- free, but only because the source is
     parsed rather than grepped.

`test_the_extractor_is_not_vacuous` below is why "no offenders" is a fact
about the source rather than about a broken scanner.
"""

import ast
import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"

#: Case-insensitive, and deliberately just the one word -- this is the vendor
#: the buyer must never meet. Extend it when a second one appears, not before.
FORBIDDEN = re.compile(r"keycloak", re.IGNORECASE)

#: Refusal types whose second positional argument becomes an HTTP `detail`.
#: `HTTPException` itself, plus this project's own carrier.
_DETAIL_POSITIONAL = {"HTTPException": 1, "UserAdminError": 1}


def _const_strings(node: ast.AST, module_consts: dict[str, str]) -> list[str]:
    """Every string this expression can put in front of a user.

    A `Constant` is itself; a `JoinedStr` is its literal parts (the
    interpolated values are checked where they are produced, not here); a
    bare `Name` is resolved against the module's own string constants.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        parts = [
            v.value
            for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
        return ["".join(parts)] if parts else []
    if isinstance(node, ast.Name) and node.id in module_consts:
        return [module_consts[node.id]]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [
            a + b
            for a in _const_strings(node.left, module_consts)
            for b in _const_strings(node.right, module_consts)
        ]
    return []


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level `NAME = "..."` bindings, so a `detail` passed by name is
    still read. Only the module body: a local would need flow analysis, and
    none of the user-facing sentences in this codebase are locals."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        out[target.id] = node.value.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str) and isinstance(node.target, ast.Name):
                out[node.target.id] = node.value.value
    return out


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def user_visible_strings(source: str) -> list[tuple[int, str]]:
    """`(lineno, text)` for every string this module can send to a user.

    Docstrings, comments, log-call arguments, identifiers and import paths are
    never collected: none of them reaches a response body.
    """
    tree = ast.parse(source)
    consts = _module_constants(tree)
    found: list[tuple[int, str]] = []

    def take(node: ast.AST, line: int) -> None:
        for text in _const_strings(node, consts):
            found.append((line, text))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _callee(node)
            index = _DETAIL_POSITIONAL.get(name)
            if index is not None and len(node.args) > index:
                take(node.args[index], node.lineno)
            for kw in node.keywords:
                if kw.arg == "detail":
                    take(kw.value, node.lineno)
                elif name == "Field" and kw.arg in {"description", "default"}:
                    take(kw.value, node.lineno)
            if name == "Field" and node.args:
                take(node.args[0], node.lineno)
        elif isinstance(node, ast.ClassDef):
            # A response-model field default: `flavour: str = "keycloak"`.
            #
            # ANNOTATED ASSIGNMENTS ONLY. A Pydantic field is always annotated,
            # and an unannotated one in a class body is the other thing that
            # lives there: `__tablename__ = "keycloak_event_ingest"` in
            # `app/models/core.py`, which is a POSTGRES TABLE NAME. Collecting
            # it made the first run of this guard report an offender that no
            # user can see and that must never be renamed -- exactly the false
            # positive the frontend guard's docstring warns gets a guard
            # deleted.
            for stmt in node.body:
                if not isinstance(stmt, ast.AnnAssign) or stmt.value is None:
                    continue
                if isinstance(stmt.target, ast.Name) and stmt.target.id.startswith("_"):
                    continue
                take(stmt.value, stmt.lineno)
    return found


def _python_files() -> list[Path]:
    return sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_walk_reaches_the_platforms_own_sign_in() -> None:
    """Own authentication design §10.10: the sign-in's own refusals are text a
    buyer reads, so the guard must be seen to read them."""
    files = {path.relative_to(APP).as_posix() for path in _python_files()}
    assert {"api/v1/auth.py", "core/auth/sessions.py", "core/auth/loopback.py"} <= files


# --------------------------------------------------------------------------- #
# the extractor the guard is built on
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            'raise HTTPException(503, "Keycloak is not configured")',
            "Keycloak is not configured",
            id="httpexception-positional-detail",
        ),
        pytest.param(
            'raise HTTPException(status_code=503, detail="Keycloak is not configured")',
            "Keycloak is not configured",
            id="detail-keyword",
        ),
        pytest.param(
            'raise UserAdminError(502, f"Keycloak refused to {what}")',
            "Keycloak refused to ",
            id="f-string-in-the-projects-own-carrier",
        ),
        pytest.param(
            'raise HTTPException(503, "Keycloak is not " "configured")',
            "Keycloak is not configured",
            id="implicit-concatenation-across-lines",
        ),
        pytest.param(
            'MSG = "Keycloak is not configured"\nraise HTTPException(503, MSG)',
            "Keycloak is not configured",
            id="module-level-constant-passed-by-name",
        ),
        pytest.param(
            'class Out(BaseModel):\n    who: str = Field(description="Whether Keycloak sent it")',
            "Whether Keycloak sent it",
            id="field-description",
        ),
        pytest.param(
            'class Out(BaseModel):\n    who: str = "Keycloak"',
            "Keycloak",
            id="model-field-default",
        ),
    ],
)
def test_the_extractor_is_not_vacuous(source: str, expected: str) -> None:
    """Without these, "no offenders" would equally mean "the extractor
    returned nothing", and the sweep below would be decoration."""
    assert expected in [text for _line, text in user_visible_strings(source)]


@pytest.mark.parametrize(
    ("source", "why"),
    [
        pytest.param(
            '"""Talking to Keycloak\'s admin API."""\nX = 1',
            "a module docstring",
            id="docstring",
        ),
        pytest.param(
            "# Keycloak refuses this, and the comment must say so\nX = 1",
            "a comment",
            id="comment",
        ),
        pytest.param(
            "from app.modules.keycloak_events.client import KeycloakEventsConfig",
            "an import path and a class name",
            id="import",
        ),
        pytest.param(
            'logger.warning("keycloak sweep failed", extra={"probe": "keycloak_events"})',
            "a log message",
            id="log-message",
        ),
        pytest.param(
            'settings = {"keycloak_admin_url": None}',
            "a settings key",
            id="settings-key",
        ),
        pytest.param(
            'class KeycloakEventIngest(Base):\n    __tablename__ = "keycloak_event_ingest"',
            "a database table name",
            id="tablename",
        ),
        pytest.param(
            'def f() -> None:\n    """Ask Keycloak to send one action mail."""',
            "a function docstring",
            id="function-docstring",
        ),
    ],
)
def test_the_extractor_stays_inside_its_claim(source: str, why: str) -> None:
    """Exempt, and it has to be: the platform genuinely integrates with
    Keycloak. A guard that forced these to lie would be deleted."""
    texts = " | ".join(text for _line, text in user_visible_strings(source))
    assert not FORBIDDEN.search(texts), f"{why} was collected as user-visible"


# --------------------------------------------------------------------------- #
# the guard
# --------------------------------------------------------------------------- #


def test_no_response_string_names_the_identity_vendor() -> None:
    offenders: list[str] = []
    scanned = 0
    for path in _python_files():
        scanned += 1
        for line, text in user_visible_strings(path.read_text()):
            if FORBIDDEN.search(text):
                offenders.append(f"{path.relative_to(APP.parent)}:{line}: {text[:90]!r}")
    # A walk that found no files would also find no offenders.
    assert scanned > 100, f"only {scanned} modules scanned -- the walk is broken"
    assert offenders == [], "user-visible strings naming the identity vendor:\n" + "\n".join(
        offenders
    )


def test_every_probe_has_a_display_label_and_none_names_the_vendor() -> None:
    """The case no scan of string literals could ever have caught.

    `PlatformStatusPage` prints the probe name the status API sends, and the
    probe was called `keycloak_events` -- a dict KEY, in the position of a
    heading, on an operator's screen. The key stays (history rows in
    `platform_health_sample.probe` carry it, `/readyz` reports it, and
    `test_platform_health_sampler.py` pins the sampled set); the label is the
    separate, user-facing string, and this is what keeps it clean.
    """
    from app.api.probes import _ALL_PROBES, PROBE_LABELS, probe_label

    assert set(PROBE_LABELS) == set(_ALL_PROBES), (
        "every registered probe needs a label, and a label with no probe is dead text"
    )
    offenders = [key for key in _ALL_PROBES if FORBIDDEN.search(probe_label(key))]
    assert offenders == [], f"probe labels naming the identity vendor: {offenders}"


# --------------------------------------------------------------------------- #
# the string this file's static half cannot see
# --------------------------------------------------------------------------- #


def test_the_user_lifecycle_has_no_identity_provider_wording_to_forward() -> None:
    """`_translating` used to turn the admin client's failures into refusals,
    and its first version interpolated the upstream exception -- "Keycloak
    accepted the user but returned no Location header" -- into a 502 and a 201
    body, assembled at runtime where no scan of literals could see it.

    Since P2 phase 1 Task 8 the lifecycle calls no identity provider at all,
    so there is no upstream wording left to forward: the service imports
    neither the admin client nor an HTTP client. Should either come back, the
    rule above comes back with it, and so must a test like the old one."""
    source = (Path(__file__).resolve().parents[1] / "app/modules/user_admin/service.py").read_text()
    assert "kc_client" not in source
    assert "import httpx" not in source
