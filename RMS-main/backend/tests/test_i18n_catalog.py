"""The server catalogue is complete, consistent, and nothing bypasses tr().

PENDING shrinks as Tasks 14-17 sweep each area; the last task empties it and
`test_nothing_is_pending` keeps it empty.
"""

import ast
import builtins
import functools
import re
import string
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

from app.core.i18n import CATALOG_ES

APP = Path(__file__).resolve().parents[1] / "app"
PENDING: set[str] = set()
# Callables whose message argument is shown to a person.
RAISERS = re.compile(
    r"^(HTTPException|_reject\w*|_refuse\w*|\w+Error|\w+Refused|MsiRefused|ValueError)$"
)
# Exception bases that start the transitive search for a module's own error classes
# (builtin exceptions count too; see _is_exception_name).
EXCEPTION_ROOTS = frozenset({"Exception", "BaseException", "HTTPException"})
# `# i18n-exempt: <where the error goes>` -- a reason is required.
EXEMPT = re.compile(r"# i18n-exempt:\s*\S")


@functools.cache
def _read(path: Path) -> str:
    return path.read_text()


@functools.cache
def _parse(source: str) -> ast.Module:
    """Each file is parsed once per session; the tree is only ever read."""
    return ast.parse(source)


def _files() -> list[Path]:
    return [p for p in APP.rglob("*.py") if "i18n" not in p.parts]


def _rel(p: Path) -> str:
    return p.relative_to(APP).as_posix()


def _pending(p: Path) -> bool:
    return any(_rel(p).startswith(prefix + "/") or _rel(p) == prefix + ".py" for prefix in PENDING)


def _is_tr(node: ast.AST) -> bool:
    """A call to `tr(...)`, bare or as `module.tr(...)`."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    return (isinstance(f, ast.Name) and f.id == "tr") or (
        isinstance(f, ast.Attribute) and f.attr == "tr"
    )


def _is_marker(node: ast.AST) -> bool:
    """A call to `N_(...)`, the gettext marker for a message stored as data."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    return (isinstance(f, ast.Name) and f.id == "N_") or (
        isinstance(f, ast.Attribute) and f.attr == "N_"
    )


def _literal(node: ast.Call) -> str | None:
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


def _tr_calls() -> Iterator[tuple[Path, ast.Call]]:
    """(path, call node) for every tr() call under app/."""
    for p in _files():
        for node in ast.walk(_parse(_read(p))):
            if _is_tr(node):
                assert isinstance(node, ast.Call)
                yield p, node


def _marker_calls() -> Iterator[tuple[Path, ast.Call]]:
    """(path, call node) for every N_() call under app/."""
    for p in _files():
        for node in ast.walk(_parse(_read(p))):
            if _is_marker(node):
                assert isinstance(node, ast.Call)
                yield p, node


def _marker_literals() -> Iterator[tuple[Path, int, str]]:
    for p, node in _marker_calls():
        text = _literal(node)
        if text is not None:
            yield p, node.lineno, text


def _tr_literals() -> Iterator[tuple[Path, int, str]]:
    """(path, line, text) for every tr() template and every N_() message that is
    a literal. A marked message is looked up verbatim by `tr_marked`, so it is
    keyed in the catalogue exactly like a template."""
    for p, node in _tr_calls():
        text = _literal(node)
        if text is not None:
            yield p, node.lineno, text
    yield from _marker_literals()


def _fields(s: str) -> list[str]:
    """Sorted FULL field names (`a.b` stays `a.b`, so it never equals `a`)."""
    try:
        return sorted(f for _, f, _, _ in string.Formatter().parse(s) if f is not None)
    except ValueError:
        return ["<unparsable>"]


def _template_problems(s: str) -> list[str]:
    """Why `s` is not a plain-`{name}` template. Spec 3.2 moves every
    conversion and format spec into the parameter (`name=repr(x)`), and
    attribute or index access would let a catalogue value reach into an
    object it was handed, so a template may hold `{name}` fields and nothing else."""
    try:
        parsed = list(string.Formatter().parse(s))
    except ValueError as exc:
        return [f"unparsable ({exc})"]
    problems = []
    for _, field, spec, conversion in parsed:
        if field is None:
            continue
        if not field.isidentifier():
            problems.append(f"field {{{field}}} is not a plain name")
        if conversion:
            problems.append(f"conversion !{conversion} in {{{field}}}")
        if spec:
            problems.append(f"format spec :{spec} in {{{field}}}")
    return problems


def _untranslated(arg: ast.expr) -> bool:
    """A message built from English text that did not go through tr()."""
    if isinstance(arg, ast.JoinedStr):
        return True
    if isinstance(arg, ast.Constant):
        return isinstance(arg.value, str) and re.search(r"[A-Za-z]{3}", arg.value) is not None
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, (ast.Add, ast.Mod)):
        return _untranslated(arg.left) or _untranslated(arg.right)
    # "text {}".format(x)
    return (
        isinstance(arg, ast.Call)
        and isinstance(arg.func, ast.Attribute)
        and arg.func.attr == "format"
        and _untranslated(arg.func.value)
    )


def _base_name(base: ast.expr) -> str | None:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return None


def _is_exception_name(name: str) -> bool:
    obj = getattr(builtins, name, None)
    return name in EXCEPTION_ROOTS or (isinstance(obj, type) and issubclass(obj, BaseException))


def _exception_classes(sources: Iterable[str]) -> frozenset[str]:
    """Names of every class that transitively derives from an exception or
    HTTPException, wherever in `sources` it is defined (spec 3.3: "the classes
    deriving from each module's error base")."""
    bases: dict[str, set[str]] = {}
    for source in sources:
        for node in ast.walk(_parse(source)):
            if isinstance(node, ast.ClassDef):
                bases.setdefault(node.name, set()).update(
                    n for n in map(_base_name, node.bases) if n
                )
    derived: set[str] = set()
    grew = True
    while grew:
        grew = False
        for name, parents in bases.items():
            if name not in derived and any(_is_exception_name(b) or b in derived for b in parents):
                derived.add(name)
                grew = True
    return frozenset(derived)


def _app_exception_classes() -> frozenset[str]:
    return _exception_classes(_read(p) for p in _files())


def _callee(call: ast.Call) -> str:
    return getattr(call.func, "id", None) or getattr(call.func, "attr", None) or ""


class _Sites(ast.NodeVisitor):
    """Every raise site in a module: a call to a RAISERS name or to one of the
    app's own exception classes, or `__init__(...)` (usually `super().__init__`)
    inside such a class, where the class composes its message."""

    def __init__(self, classes: frozenset[str]) -> None:
        self.classes = classes
        self.class_stack: list[str] = []
        self.func_stack: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self.sites: list[tuple[ast.Call, ast.FunctionDef | ast.AsyncFunctionDef | None]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.func_stack.append(node)
        self.generic_visit(node)
        self.func_stack.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Call(self, node: ast.Call) -> None:
        name = _callee(node)
        in_error_class = bool(self.class_stack) and self.class_stack[-1] in self.classes
        if RAISERS.match(name) or name in self.classes or (in_error_class and name == "__init__"):
            self.sites.append((node, self.func_stack[-1] if self.func_stack else None))
        self.generic_visit(node)


def _raise_sites(
    source: str, classes: frozenset[str]
) -> list[tuple[ast.Call, ast.FunctionDef | ast.AsyncFunctionDef | None]]:
    visitor = _Sites(classes | _exception_classes([source]))
    visitor.visit(_parse(source))
    return visitor.sites


def _own_nodes(func: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    """The nodes of `func` itself, not of functions or classes nested in it."""
    todo: list[ast.AST] = list(ast.iter_child_nodes(func))
    while todo:
        node = todo.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            todo.extend(ast.iter_child_nodes(node))


def _english_locals(
    func: ast.FunctionDef | ast.AsyncFunctionDef | None,
) -> dict[str, ast.expr]:
    """Names assigned an untranslated string somewhere in `func`:
    `msg = f"..."; raise X(msg)`."""
    found: dict[str, ast.expr] = {}
    if func is None:
        return found
    for node in _own_nodes(func):
        if isinstance(node, ast.Assign) and _untranslated(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and isinstance(node.target, ast.Name)
            and _untranslated(node.value)
        ):
            found[node.target.id] = node.value
    return found


def _bypasses(source: str, classes: frozenset[str] = frozenset()) -> list[tuple[int, str]]:
    """(line, literal) of every user-facing message that skips tr() in `source`.
    `classes` are exception classes defined elsewhere in the app."""
    lines = source.splitlines()
    found = []
    for node, func in _raise_sites(source, classes):
        # An internal error a person never sees (caught and logged, or
        # re-raised as something else) is exempted on its own line, with
        # the reason: `# i18n-exempt: caught by _http_error, logged only`.
        if EXEMPT.search(lines[node.lineno - 1]):
            continue
        english = _english_locals(func)
        for arg in [
            *node.args,
            *(k.value for k in node.keywords if k.arg in ("detail", "message")),
        ]:
            if _untranslated(arg):
                literal = ast.get_source_segment(source, arg) or ast.unparse(arg)
            elif isinstance(arg, ast.Name) and arg.id in english:
                value = english[arg.id]
                literal = (
                    f"{arg.id} = {ast.get_source_segment(source, value) or ast.unparse(value)}"
                )
            else:
                continue
            found.append((node.lineno, literal.replace("\n", " ")[:100]))
    return found


def _findings(*, pending: bool) -> list[str]:
    """`path:line: literal` for every bypass, in pending files or in swept ones."""
    classes = _app_exception_classes()
    return [
        f"{_rel(p)}:{line}: {literal}"
        for p in sorted(_files())
        if _pending(p) is pending
        for line, literal in _bypasses(_read(p), classes)
    ]


def test_every_tr_literal_is_translated_with_the_same_placeholders():
    missing, mismatched = [], []
    for p, line, text in _tr_literals():
        where = f"{_rel(p)}:{line}: {text!r}"
        if text not in CATALOG_ES:
            missing.append(where)
        elif _fields(CATALOG_ES[text]) != _fields(text):
            mismatched.append(f"{where} -> {CATALOG_ES[text]!r}")
    assert not missing, "untranslated (add to app/core/i18n/es.py):\n" + "\n".join(missing)
    assert not mismatched, (
        "placeholder mismatch (es must use exactly the English fields):\n" + "\n".join(mismatched)
    )


def test_templates_and_catalogue_values_use_plain_named_fields_only():
    """Applies to `tr()` templates and their catalogue entries. An `N_()` message
    is EXEMPT from the rule, deliberately: `tr_marked` never calls `.format`, so
    its braces are data, and one of them (an approved-action reason) says
    `{device_id}` as literal text that must reach the reader unchanged. Doubling
    it would change the English the tests re-parse. The completeness check still
    holds the Spanish to the same fields as the English."""
    marked = {text for _, _, text in _marker_literals()}
    bad = [
        f"{_rel(p)}:{line}: {text!r}: {problem}"
        for p, line, text in _tr_literals()
        if text not in marked
        for problem in _template_problems(text)
    ] + [
        f"app/core/i18n/es.py: {key!r} -> {value!r}: {problem}"
        for key, value in CATALOG_ES.items()
        if key not in marked
        for problem in [*_template_problems(key), *_template_problems(value)]
    ]
    assert bad == [], (
        "only plain {name} fields are allowed; move !r, :, and .attr into the parameter:\n"
        + "\n".join(bad)
    )


def test_no_stale_catalogue_entries():
    used = {text for _, _, text in _tr_literals()}
    stale = sorted(set(CATALOG_ES) - used)
    assert stale == [], (
        "catalogue entries no tr() call uses (delete or fix the key):\n"
        + "\n".join(map(repr, stale))
    )


def test_n_is_only_called_with_a_literal():
    bad = [
        f"{_rel(p)}:{node.lineno}: {ast.unparse(node)[:100]}"
        for p, node in _marker_calls()
        if _literal(node) is None
    ]
    assert bad == [], "N_() needs a literal message (the catalogue is keyed by it):\n" + "\n".join(
        bad
    )


#: The module-level tables of English prose that reach a person (a 403 detail,
#: a refusal sentence, a validation error). Every value must be `N_("...")`, so
#: the guard sees it and `tr_marked` can translate it where it is used.
MARKED_TABLES = {
    "jobs/direct_dispatch.py": "MODULE_GOVERNED_REASONS",
    "jobs/dispatch_permissions.py": "NO_HUMAN_DISPATCH_PATH",
    "core/approved_actions.py": "NON_APPROVABLE_REASONS",
}


def _table_values(source: str, name: str) -> list[ast.expr]:
    for node in _parse(source).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name) and target.id == name:
                assert isinstance(node.value, ast.Dict)
                return list(node.value.values)
    raise AssertionError(f"{name} is not assigned at module level")


def _unmarked_table_values(source: str, name: str) -> list[int]:
    return [
        value.lineno
        for value in _table_values(source, name)
        if not (
            _is_marker(value)
            and isinstance(value, ast.Call)
            and _literal(value) is not None
            and len(value.args) == 1
        )
    ]


def test_every_value_in_the_prose_tables_is_a_marked_literal():
    for path, name in MARKED_TABLES.items():
        values = _table_values(_read(APP / path), name)
        assert values, f"{name} is empty or moved: the check would pass vacuously"
        assert _unmarked_table_values(_read(APP / path), name) == [], (
            f"{path}: every value of {name} must be N_('...')"
        )


def test_every_value_in_the_prose_tables_has_a_spanish_entry():
    from app.core.approved_actions import NON_APPROVABLE_REASONS
    from app.jobs.direct_dispatch import MODULE_GOVERNED_REASONS
    from app.jobs.dispatch_permissions import NO_HUMAN_DISPATCH_PATH

    tables = {
        "MODULE_GOVERNED_REASONS": MODULE_GOVERNED_REASONS,
        "NO_HUMAN_DISPATCH_PATH": NO_HUMAN_DISPATCH_PATH,
        "NON_APPROVABLE_REASONS": NON_APPROVABLE_REASONS,
    }
    missing = [
        f"{name}[{key!r}]"
        for name, table in tables.items()
        for key, value in table.items()
        if value not in CATALOG_ES
    ]
    assert len(missing) == 0, "table values with no catalogue entry:\n" + "\n".join(missing)
    assert sum(len(t) for t in tables.values()) >= 40


def test_the_table_and_marker_scans_see_what_they_are_meant_to_see():
    source = "T = {'a': N_('x y z'), 'b': 'plain text', 'c': N_(f'{q}'), 'd': (N_('ok'))}\n"
    assert _unmarked_table_values(source, "T") == [1, 1]
    assert [m for m in ast.walk(_parse(source)) if _is_marker(m)]
    assert not _is_marker(ast.parse("tr('x')").body[0].value)  # type: ignore[attr-defined]


def test_tr_is_only_called_with_a_literal():
    bad = []
    for p, node in _tr_calls():
        if _literal(node) is None:
            bad.append(f"{_rel(p)}:{node.lineno}: {ast.unparse(node)[:100]}")
    assert bad == [], "tr() needs a literal template (the catalogue is keyed by it):\n" + "\n".join(
        bad
    )


def _param_mismatches(source: str) -> list[tuple[int, str, list[str], list[str]]]:
    out = []
    for node in ast.walk(_parse(source)):
        if not _is_tr(node):
            continue
        assert isinstance(node, ast.Call)
        text = _literal(node)
        if text is None or any(k.arg is None for k in node.keywords):
            continue  # a ** splat cannot be checked statically; a literal is enforced elsewhere
        fields, given = _fields(text), sorted(k.arg for k in node.keywords if k.arg)
        given += ["<positional>"] * (len(node.args) - 1)  # tr() takes the template only
        if fields != given:
            out.append((node.lineno, text, fields, given))
    return out


def test_tr_is_called_with_exactly_the_parameters_its_template_names():
    """A forgotten parameter would raise at runtime in English; catch it here."""
    bad = [
        f"{_rel(p)}:{line}: {text!r} names {fields} but is called with {given}"
        for p in _files()
        for line, text, fields, given in _param_mismatches(_read(p))
    ]
    assert bad == [], "tr() parameters differ from the template's fields:\n" + "\n".join(bad)


def test_the_tr_guards_see_what_they_are_meant_to_see():
    """No file calls tr() yet, so prove the scans on synthetic source."""
    source = (
        "tr('Run {run_id} is gone.', run_id=1)\n"
        "tr('Run {run_id} is gone.')\n"
        "tr('Run is gone.', run_id=1)\n"
        "m.tr('Run {a} {b}.', a=1)\n"
        "tr('Run {}.', 1)\n"
        "tr('Run {x.y}.', x=1)\n"
        "tr('Run {x}.', **kw)\n"
        "tr(msg, x=1)\n"
    )
    assert [m[0] for m in _param_mismatches(source)] == [2, 3, 4, 5, 6]
    # Full names: `a.b` is not `a`.
    assert _fields("{a} {b.c} {d[0]} {} {0}") == ["", "0", "a", "b.c", "d[0]"]
    assert _fields("no fields, {{literal}}") == []
    assert _fields("a } b") == ["<unparsable>"]


def test_the_plain_field_rule_rejects_everything_but_plain_names():
    assert _template_problems("Run {run_id} on {host_2}: {{literal}}.") == []
    assert _template_problems("No fields.") == []
    for template in (
        "{a.b}",
        "{a[0]}",
        "{a!r}",
        "{a!s}",
        "{n:,}",
        "{n:>{w}}",
        "{}",
        "{0}",
        "{a-b}",
        "stray } brace",
        "open { brace",
    ):
        assert _template_problems(template), template


def test_every_exemption_names_a_reason_and_sits_on_a_raise_site():
    """An `# i18n-exempt:` that is empty, or that is no longer on a raiser call
    (code moved, comment stayed), would hide nothing and mean nothing."""
    classes = _app_exception_classes()
    bad = []
    for p in _files():
        source = _read(p)
        raiser_lines = {node.lineno for node, _ in _raise_sites(source, classes)}
        for number, text in enumerate(source.splitlines(), 1):
            if "# i18n-exempt" in text and (not EXEMPT.search(text) or number not in raiser_lines):
                bad.append(f"{_rel(p)}:{number}: {text.strip()[:100]}")
    assert bad == [], (
        "i18n-exempt needs a reason and must sit on the raiser's first line:\n" + "\n".join(bad)
    )


def test_nothing_bypasses_tr_outside_pending_areas():
    bad = _findings(pending=False)
    assert bad == [], "user-facing message not wrapped in tr():\n" + "\n".join(bad)


def test_every_pending_prefix_names_a_real_area():
    """A typo in PENDING would silently exempt nothing, or a swept prefix would linger."""
    unknown = sorted(p for p in PENDING if not ((APP / p).is_dir() or (APP / f"{p}.py").is_file()))
    assert unknown == []


def test_nothing_is_pending():
    """Every area of app/ is swept: the guards above apply to all of it, and a new
    area starts under them instead of being added to PENDING."""
    assert PENDING == set()


def test_pending_hides_something_while_it_is_not_empty():
    """If PENDING lists areas but the scan finds nothing in them, the scan is
    blind (or the areas are swept and their prefixes should go)."""
    if PENDING:
        assert _findings(pending=True)


def test_the_bypass_scan_sees_what_it_is_meant_to_see():
    """PENDING hides every real finding today, so prove the scan itself works."""
    source = (
        "raise HTTPException(409, 'Run is not paused.')\n"
        "raise HTTPException(409, detail=f'Run {x} is not paused.')\n"
        "raise _reject_state('Run is not paused.')\n"
        "raise PushRefused('Run is not paused.')\n"
        "raise ValueError('Run is not paused.')  # i18n-exempt: caught by _http_error\n"
        "raise HTTPException(409, tr('Run is not paused.'))\n"
        "raise HTTPException(404)\n"
        "raise ValueError('')\n"
        "raise HTTPException(409, 'Run ' + run_id + ' is gone.')\n"
        "raise HTTPException(409, 'Run %s is gone.' % run_id)\n"
        "raise HTTPException(409, 'Run {} is gone.'.format(run_id))\n"
        "raise ValueError('Run is gone.')  # i18n-exempt:\n"
        "raise HTTPException(409, tr('Run {id}.', id=1) + ' Retry.')\n"
        "raise HTTPException(409, str(exc))\n"
    )
    assert [line for line, _ in _bypasses(source)] == [1, 2, 3, 4, 9, 10, 11, 12, 13]
    assert _bypasses(source)[0] == (1, "'Run is not paused.'")


def test_the_bypass_scan_follows_the_apps_own_exception_classes():
    source = (
        "class SlaError(Exception):\n"
        "    pass\n"
        "class NotFound(SlaError):\n"
        "    pass\n"
        "class Teapot(HTTPException):\n"
        "    pass\n"
        "class Plain:\n"
        "    def __init__(self):\n"
        "        super().__init__('Not an error class.')\n"
        "class Composed(NotFound):\n"
        "    def __init__(self, run):\n"
        "        super().__init__(f'Run {run} is gone.')\n"
        "class Keyword(Teapot):\n"
        "    def __init__(self):\n"
        "        super().__init__(code='x', message='Short and stout.')\n"
        "class Passes(NotFound):\n"
        "    def __init__(self, *a):\n"
        "        super().__init__(*a)\n"
        "class Wrapped(NotFound):\n"
        "    def __init__(self):\n"
        "        super().__init__(tr('Run is gone.'))\n"
        "raise NotFound('Run is gone.')\n"
        "raise Teapot(detail='Short and stout.')\n"
        "raise Composed(1)\n"
        "raise Other('Defined in another file.')\n"
        "raise NotFound('Run is gone.')  # i18n-exempt: logged only\n"
    )
    assert [line for line, _ in _bypasses(source)] == [12, 15, 22, 23]
    # A class defined in ANOTHER file is known through `classes`.
    assert [line for line, _ in _bypasses(source, frozenset({"Other"}))] == [12, 15, 22, 23, 25]
    assert _exception_classes([source]) >= {"SlaError", "NotFound", "Teapot", "Composed"}
    assert "Plain" not in _exception_classes([source])


def test_exception_classes_are_found_across_files_transitively():
    a = "class AppError(Exception): pass\nclass ValueLike(ValueError): pass\n"
    b = (
        "from a import AppError\n"
        "class Gone(AppError): pass\n"
        "class Gone2(Gone): pass\n"
        "class M(Base): pass\n"
    )
    found = _exception_classes([a, b])
    assert {"AppError", "ValueLike", "Gone", "Gone2"} <= found
    assert "M" not in found


def test_the_bypass_scan_follows_a_message_built_in_the_same_function():
    source = (
        "def a(run):\n"
        "    msg = f'Run {run} is gone.'\n"
        "    raise HTTPException(409, msg)\n"
        "def b(run):\n"
        "    detail: str = 'Run is gone.'\n"
        "    raise HTTPException(409, detail=detail)\n"
        "def c(run):\n"
        "    msg = tr('Run is gone.')\n"
        "    raise HTTPException(409, msg)\n"
        "def d(run):\n"
        "    raise HTTPException(409, msg)\n"
        "def e(run):\n"
        "    msg = f'Run {run} is gone.'\n"
        "    def inner():\n"
        "        raise HTTPException(409, msg)\n"
        "async def f(run):\n"
        "    code = 'conflict'\n"
        "    raise HTTPException(409, code)\n"
        "async def g(run):\n"
        "    msg = f'Run {run} is gone.'\n"
        "    raise HTTPException(409, msg)  # i18n-exempt: logged only\n"
    )
    found = _bypasses(source)
    assert [line for line, _ in found] == [3, 6, 18]
    assert found[0][1] == "msg = f'Run {run} is gone.'"


def test_file_selection_on_a_tree_with_a_lookalike_prefix(tmp_path, monkeypatch):
    """The wiring from files to PENDING, end to end: a swept file with a bypass
    is flagged, a pending one is only counted, and `modules/sla_something/` is
    not pending just because `modules/sla` is."""
    bypass = "def f():\n    raise HTTPException(409, 'Run is gone.')\n"
    tree = {
        "core/a.py": bypass,
        "modules/sla/b.py": bypass,
        "modules/sla.py": bypass,
        "modules/sla_something/c.py": bypass,
        "modules/slate.py": bypass,
        "swept/d.py": bypass,
        "swept/clean.py": "def f():\n    raise HTTPException(409, tr('Run is gone.'))\n",
        "swept/e/errors.py": "class SweptError(Exception):\n    pass\n",
        "swept/f.py": "def f():\n    raise SweptError('Run is gone.')\n",
        "i18n/skipped.py": bypass,
        "core/i18n/skipped.py": bypass,
    }
    for rel, source in tree.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "APP", tmp_path)
    monkeypatch.setattr(module, "PENDING", {"core", "modules/sla"})

    literal = "'Run is gone.'"
    assert _findings(pending=True) == [
        f"core/a.py:2: {literal}",
        f"modules/sla/b.py:2: {literal}",
        f"modules/sla.py:2: {literal}",
    ]
    assert _findings(pending=False) == [
        f"modules/sla_something/c.py:2: {literal}",
        f"modules/slate.py:2: {literal}",
        f"swept/d.py:2: {literal}",
        f"swept/f.py:2: {literal}",
    ]
    assert (
        test_nothing_bypasses_tr_outside_pending_areas.__name__
    )  # the real guard reads the same lists
    with pytest.raises(AssertionError, match=r"swept/d\.py:2: 'Run is gone\.'"):
        test_nothing_bypasses_tr_outside_pending_areas()
    test_pending_hides_something_while_it_is_not_empty()
