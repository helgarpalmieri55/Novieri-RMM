"""Add `clients=` to every org_session(...) / portal_session(...) call.

Usage (from backend/):
    python -I scripts/codemods/add_client_scope.py <path>... > REVIEW_REQUIRED.txt

MSP clients plan, Task 3 (spec §2.3). One-shot: it exists so the rewrite of
~3,000 call sites is reproducible and reviewable as a program, not as a diff.

- A call that already passes `clients=` is left alone (idempotent).
- Under `tests/` the value is always `ClientScope.all()`: a test opens a session
  as the whole organization unless it is testing the scope itself.
- Elsewhere, if the enclosing function (or a function it is nested in) holds
  the request's principal, the value is `<name>.clients`. The principal is:
    * a parameter annotated `AuthContext` / `PortalContext` (bare, quoted, or
      as the first argument of `Annotated[...]`), any name;
    * an UNannotated parameter named `ctx`;
    * a local `ctx = await auth_context(...)` (or `resolve_portal_principal`,
      `bound_auth_context`), or `ctx: AuthContext = ...`.
  A parameter that shadows the inherited name (e.g. an inner `ctx: TurnContext`)
  breaks the inheritance -- `ctx.clients` would then name the wrong object.
- Otherwise `ClientScope.all()`, and for files under app/ the path is printed
  so a person reviews it (plan Task 3 Step 4). `ClientScope.all()` is never the
  final word for app/ code: `tests/test_client_catalog.py` refuses any file
  that is not on the reviewed `ALL_SCOPE_ALLOWLIST`.
- `from app.core.client_scope import ClientScope` is added where needed.

Formatting: CI runs `ruff check` (E501 at 100 columns), not `ruff format`, so
this keeps the file's own layout: a magic-trailing-comma call gets the new
argument on its own line, and a one-line call that would pass 100 columns is
wrapped the way the formatter would wrap it.

NOT HANDLED (fix by hand; the script cannot see them as calls):
`functools.partial(org_session, ...)` and session factories passed by name.
"""

import sys
from pathlib import Path

import libcst as cst
from libcst.codemod import CodemodContext
from libcst.codemod.visitors import AddImportsVisitor
from libcst.metadata import MetadataWrapper, PositionProvider

TARGETS = {"org_session", "portal_session"}
PRINCIPAL_TYPES = {"AuthContext", "PortalContext"}
RESOLVERS = {"auth_context", "bound_auth_context", "resolve_portal_principal"}
MAX_LINE = 100


def _annotation_names_principal(ann: cst.Annotation | None) -> bool:
    if ann is None:
        return False
    node = ann.annotation
    if isinstance(node, cst.SimpleString):  # "AuthContext"
        return node.evaluated_value in PRINCIPAL_TYPES
    if isinstance(node, cst.Subscript) and isinstance(node.value, cst.Name):
        if node.value.value == "Annotated" and node.slice:
            first = node.slice[0].slice
            if isinstance(first, cst.Index):
                node = first.value
    return isinstance(node, cst.Name) and node.value in PRINCIPAL_TYPES


def _is_resolver_call(value: cst.BaseExpression) -> bool:
    if isinstance(value, cst.Await):
        value = value.expression
    return (
        isinstance(value, cst.Call)
        and isinstance(value.func, cst.Name)
        and value.func.value in RESOLVERS
    )


class _Transformer(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, lines: list[str], force_all: bool) -> None:
        super().__init__()
        self.lines = lines
        self.force_all = force_all
        self.ctx_stack: list[str | None] = []
        self.used_all = False
        self.changed = False

    # -- which name holds the principal in this frame ---------------------- #

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        params = [
            *node.params.posonly_params,
            *node.params.params,
            *node.params.kwonly_params,
        ]
        name: str | None = None
        for p in params:
            if _annotation_names_principal(p.annotation):
                name = p.name.value
                break
            if p.name.value == "ctx" and p.annotation is None:
                name = "ctx"
        if name is None and isinstance(node.body, cst.IndentedBlock):
            for stmt in node.body.body:
                if not isinstance(stmt, cst.SimpleStatementLine):
                    continue
                for s in stmt.body:
                    if isinstance(s, cst.Assign) and _is_resolver_call(s.value):
                        if any(
                            isinstance(t.target, cst.Name) and t.target.value == "ctx"
                            for t in s.targets
                        ):
                            name = "ctx"
                    if (
                        isinstance(s, cst.AnnAssign)
                        and isinstance(s.target, cst.Name)
                        and s.target.value == "ctx"
                        and _annotation_names_principal(s.annotation)
                    ):
                        name = "ctx"
        if name is None:
            inherited = self.ctx_stack[-1] if self.ctx_stack else None
            shadowed = any(p.name.value == inherited for p in params)
            name = None if shadowed else inherited
        self.ctx_stack.append(name)

    def leave_FunctionDef(
        self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef
    ) -> cst.FunctionDef:
        self.ctx_stack.pop()
        return updated_node

    # -- the rewrite -------------------------------------------------------- #

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.Call:
        func = updated_node.func
        fname = (
            func.value
            if isinstance(func, cst.Name)
            else (func.attr.value if isinstance(func, cst.Attribute) else None)
        )
        if fname not in TARGETS:
            return updated_node
        if any(a.keyword is not None and a.keyword.value == "clients" for a in updated_node.args):
            return updated_node
        ctx_name = None if self.force_all else (self.ctx_stack[-1] if self.ctx_stack else None)
        value: cst.BaseExpression
        if ctx_name is not None:
            value = cst.Attribute(cst.Name(ctx_name), cst.Name("clients"))
        else:
            value = cst.Call(cst.Attribute(cst.Name("ClientScope"), cst.Name("all")))
            self.used_all = True
        self.changed = True
        new_arg = cst.Arg(
            keyword=cst.Name("clients"),
            value=value,
            equal=cst.AssignEqual(cst.SimpleWhitespace(""), cst.SimpleWhitespace("")),
        )
        pos = self.get_metadata(PositionProvider, original_node)
        if pos.start.line == pos.end.line:
            return self._append_on_one_line(updated_node, new_arg, pos.start.line)
        return self._append_multiline(updated_node, new_arg)

    def _append_on_one_line(self, call: cst.Call, new_arg: cst.Arg, line_no: int) -> cst.Call:
        args = list(call.args)
        line = self.lines[line_no - 1]
        render = cst.Module([]).code_for_node
        space = cst.SimpleWhitespace(" ")
        if len(line) + len(", ") + len(render(new_arg)) <= MAX_LINE:
            args[-1] = args[-1].with_changes(comma=cst.Comma(whitespace_after=space))
            return call.with_changes(args=[*args, new_arg])
        # Wrap the way the formatter would: first try every argument on one
        # hanging line, then one argument per line with a trailing comma.
        indent = line[: len(line) - len(line.lstrip())]
        inner = indent + "    "

        def nl(ws: str) -> cst.ParenthesizedWhitespace:
            return cst.ParenthesizedWhitespace(
                first_line=cst.TrailingWhitespace(),
                empty_lines=[],
                indent=False,
                last_line=cst.SimpleWhitespace(ws),
            )

        clean = [
            a.with_changes(
                comma=cst.MaybeSentinel.DEFAULT, whitespace_after_arg=cst.SimpleWhitespace("")
            )
            for a in [*args, new_arg]
        ]
        flat = ", ".join(render(a) for a in clean)
        if len(inner) + len(flat) <= MAX_LINE:
            out = [a.with_changes(comma=cst.Comma(whitespace_after=space)) for a in clean[:-1]]
            out.append(clean[-1].with_changes(whitespace_after_arg=nl(indent)))
        else:
            out = [a.with_changes(comma=cst.Comma(whitespace_after=nl(inner))) for a in clean[:-1]]
            out.append(clean[-1].with_changes(comma=cst.Comma(whitespace_after=nl(indent))))
        return call.with_changes(whitespace_before_args=nl(inner), args=out)

    def _append_multiline(self, call: cst.Call, new_arg: cst.Arg) -> cst.Call:
        args = list(call.args)
        last = args[-1]
        if len(args) >= 2 and isinstance(args[-2].comma, cst.Comma):
            separator = args[-2].comma
        elif isinstance(call.whitespace_before_args, cst.ParenthesizedWhitespace):
            separator = cst.Comma(whitespace_after=call.whitespace_before_args)
        else:
            separator = cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))
        if isinstance(last.comma, cst.Comma):
            # Magic trailing comma: the new argument takes over the closing
            # comma (and the line break before `)`); the old last argument gets
            # the separator its neighbours use.
            args[-1] = last.with_changes(comma=separator)
            return call.with_changes(args=[*args, new_arg.with_changes(comma=last.comma)])
        # No trailing comma: the whitespace before `)` lives on the last arg.
        args[-1] = last.with_changes(
            comma=separator, whitespace_after_arg=cst.SimpleWhitespace("")
        )
        new_arg = new_arg.with_changes(whitespace_after_arg=last.whitespace_after_arg)
        return call.with_changes(args=[*args, new_arg])


def rewrite(path: Path) -> bool:
    """Rewrite one file in place. True when it received `ClientScope.all()`."""
    src = path.read_text()
    module = cst.parse_module(src)
    t = _Transformer(src.splitlines(), force_all="tests" in path.parts)
    new = MetadataWrapper(module).visit(t)
    if not t.changed:
        return False
    if t.used_all:
        context = CodemodContext()
        AddImportsVisitor.add_needed_import(context, "app.core.client_scope", "ClientScope")
        new = new.visit(AddImportsVisitor(context))
    path.write_text(new.code)
    return t.used_all


def main(paths: list[str]) -> None:
    for root in paths:
        base = Path(root)
        files = [base] if base.is_file() else sorted(base.rglob("*.py"))
        for path in files:
            text = path.read_text()
            if "org_session(" not in text and "portal_session(" not in text:
                continue
            if rewrite(path) and "app" in path.parts and "tests" not in path.parts:
                print(path)


if __name__ == "__main__":
    main(sys.argv[1:])
