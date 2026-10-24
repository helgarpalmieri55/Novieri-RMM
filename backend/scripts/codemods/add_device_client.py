"""Tests only: give Device/DeviceGroup/EnrollmentToken/InstallLink their client.

Usage (from backend/):
    python -I scripts/codemods/add_device_client.py tests > by_hand.txt

MSP clients plan, Task 7. Every such constructor in tests/ already passes
`org_id=<expr>`. By the id convention (plan Global Constraints) the internal
client and its Principal site have that same id, so this adds
`client_id=<expr>` (and `site_id=<expr>` where the model has one). Idempotent.
Prints files it could not rewrite, which are fixed by hand:

- a constructor without `org_id=`;
- a constructor whose `org_id=` is not a plain name, attribute or subscript
  (a call, an `await`, ...): copying it would evaluate it two more times.

Formatting: CI runs `ruff check` (E501 at 100 columns), not `ruff format`, so
this keeps the file's own layout, as `add_client_scope.py` does: a multi-line
call gets each new argument on its own line, and a one-line call that would
pass 100 columns is wrapped the way the formatter would wrap it.
"""

import sys
from pathlib import Path

import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider

WITH_SITE = {"Device", "EnrollmentToken", "InstallLink"}
TARGETS = WITH_SITE | {"DeviceGroup"}
MAX_LINE = 100


def _is_plain(expr: cst.BaseExpression) -> bool:
    """A name, `a.b`, or `a[...]` over one: safe to evaluate three times."""
    if isinstance(expr, cst.Name):
        return True
    if isinstance(expr, cst.Attribute):
        return _is_plain(expr.value)
    if isinstance(expr, cst.Subscript):
        return _is_plain(expr.value)
    return False


def _nl(ws: str) -> cst.ParenthesizedWhitespace:
    return cst.ParenthesizedWhitespace(
        first_line=cst.TrailingWhitespace(),
        empty_lines=[],
        indent=False,
        last_line=cst.SimpleWhitespace(ws),
    )


class _T(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, lines: list[str]) -> None:
        super().__init__()
        self.lines = lines
        self.missing = False

    def leave_Call(self, original_node: cst.Call, updated_node: cst.Call) -> cst.Call:
        f = updated_node.func
        name = f.value if isinstance(f, cst.Name) else None
        if name not in TARGETS:
            return updated_node
        kw = {a.keyword.value: a for a in updated_node.args if a.keyword is not None}
        if "org_id" not in kw or not _is_plain(kw["org_id"].value):
            self.missing = True
            return updated_node
        org = kw["org_id"].value
        add = []
        eq = cst.AssignEqual(cst.SimpleWhitespace(""), cst.SimpleWhitespace(""))
        if "client_id" not in kw:
            add.append(cst.Arg(keyword=cst.Name("client_id"), value=org, equal=eq))
        if name in WITH_SITE and "site_id" not in kw:
            add.append(cst.Arg(keyword=cst.Name("site_id"), value=org, equal=eq))
        if not add:
            return updated_node
        pos = self.get_metadata(PositionProvider, original_node)
        if pos.start.line == pos.end.line:
            return self._append_on_one_line(updated_node, add, pos.start.line)
        return self._append_multiline(updated_node, add)

    def _append_on_one_line(self, call: cst.Call, add: list[cst.Arg], line_no: int) -> cst.Call:
        args = list(call.args)
        line = self.lines[line_no - 1]
        render = cst.Module([]).code_for_node
        space = cst.SimpleWhitespace(" ")
        extra = sum(len(", ") + len(render(a)) for a in add)
        if len(line) + extra <= MAX_LINE:
            args[-1] = args[-1].with_changes(comma=cst.Comma(whitespace_after=space))
            new = [a.with_changes(comma=cst.Comma(whitespace_after=space)) for a in add[:-1]]
            return call.with_changes(args=[*args, *new, add[-1]])
        # Wrap the way the formatter would: first every argument on one
        # hanging line, then one argument per line with a trailing comma.
        indent = line[: len(line) - len(line.lstrip())]
        inner = indent + "    "
        clean = [
            a.with_changes(
                comma=cst.MaybeSentinel.DEFAULT, whitespace_after_arg=cst.SimpleWhitespace("")
            )
            for a in [*args, *add]
        ]
        flat = ", ".join(render(a) for a in clean)
        if len(inner) + len(flat) <= MAX_LINE:
            out = [a.with_changes(comma=cst.Comma(whitespace_after=space)) for a in clean[:-1]]
            out.append(clean[-1].with_changes(whitespace_after_arg=_nl(indent)))
        else:
            out = [a.with_changes(comma=cst.Comma(whitespace_after=_nl(inner))) for a in clean[:-1]]
            out.append(clean[-1].with_changes(comma=cst.Comma(whitespace_after=_nl(indent))))
        return call.with_changes(whitespace_before_args=_nl(inner), args=out)

    def _append_multiline(self, call: cst.Call, add: list[cst.Arg]) -> cst.Call:
        args = list(call.args)
        last = args[-1]
        before = call.whitespace_before_args
        hanging = isinstance(before, cst.ParenthesizedWhitespace) and not any(
            isinstance(a.comma, cst.Comma)
            and isinstance(a.comma.whitespace_after, cst.ParenthesizedWhitespace)
            for a in args[:-1]
        )
        if hanging and isinstance(before, cst.ParenthesizedWhitespace):
            # Every argument on one hanging line: keep it so while it fits,
            # else one argument per line with a trailing comma.
            render = cst.Module([]).code_for_node
            inner = before.last_line.value
            clean = [
                a.with_changes(
                    comma=cst.MaybeSentinel.DEFAULT, whitespace_after_arg=cst.SimpleWhitespace("")
                )
                for a in [*args, *add]
            ]
            if len(inner) + len(", ".join(render(a) for a in clean)) > MAX_LINE:
                closing = (
                    last.comma.whitespace_after
                    if isinstance(last.comma, cst.Comma)
                    else last.whitespace_after_arg
                )
                out = [
                    a.with_changes(comma=cst.Comma(whitespace_after=_nl(inner)))
                    for a in clean[:-1]
                ]
                out.append(clean[-1].with_changes(comma=cst.Comma(whitespace_after=closing)))
                return call.with_changes(whitespace_before_args=_nl(inner), args=out)
        if len(args) >= 2 and isinstance(args[-2].comma, cst.Comma):
            separator = args[-2].comma
        elif isinstance(call.whitespace_before_args, cst.ParenthesizedWhitespace):
            separator = cst.Comma(whitespace_after=call.whitespace_before_args)
        else:
            separator = cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))
        new = [a.with_changes(comma=separator) for a in add[:-1]]
        if isinstance(last.comma, cst.Comma):
            # Magic trailing comma: the last new argument takes over the
            # closing comma (and the line break before `)`); the old last
            # argument gets the separator its neighbours use.
            args[-1] = last.with_changes(comma=separator)
            return call.with_changes(args=[*args, *new, add[-1].with_changes(comma=last.comma)])
        # No trailing comma: the whitespace before `)` lives on the last arg.
        args[-1] = last.with_changes(comma=separator, whitespace_after_arg=cst.SimpleWhitespace(""))
        tail = add[-1].with_changes(whitespace_after_arg=last.whitespace_after_arg)
        return call.with_changes(args=[*args, *new, tail])


def main(root: str) -> None:
    for path in sorted(Path(root).rglob("*.py")):
        src = path.read_text()
        if not any(f"{t}(" in src for t in TARGETS):
            continue
        t = _T(src.splitlines())
        new = MetadataWrapper(cst.parse_module(src)).visit(t)
        if new.code != src:
            path.write_text(new.code)
        if t.missing:
            print(path)


if __name__ == "__main__":
    main(sys.argv[1])
