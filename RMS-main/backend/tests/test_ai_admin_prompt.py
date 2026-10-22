"""The constant system prompt (spec §8.1, §0.6).

WHAT IS TESTED HERE IS THAT THE PROMPT CANNOT BE MADE INTO DATA -- not that its
wording works. Its instruction to treat retrieved content as data is a HINT, not
a control: deleting that sentence turns nothing red, and that is the honest
state of affairs rather than an oversight. The controls are the static allowlist
and the loopback gate, both of which have their own falsified tests.
"""

import ast
import re
from pathlib import Path

_AI_ADMIN = Path(__file__).resolve().parent.parent / "app" / "modules" / "ai_admin"


async def test_the_prompt_is_a_module_constant_with_no_interpolation() -> None:
    """"Compiled into the image", as a test. An f-string, a `%s`, or a
    `{placeholder}` is a slot, and a slot is where org configuration eventually
    goes."""
    from app.modules.ai_admin.prompt import SYSTEM_PROMPT

    assert isinstance(SYSTEM_PROMPT, str)
    assert SYSTEM_PROMPT
    assert "{" not in SYSTEM_PROMPT and "}" not in SYSTEM_PROMPT
    assert "%s" not in SYSTEM_PROMPT and "%(" not in SYSTEM_PROMPT


async def test_the_prompt_is_byte_identical_across_imports() -> None:
    """A prompt assembled at import time from anything mutable would differ
    between two imports in a long-lived process."""
    import importlib

    from app.modules.ai_admin import prompt as first

    second = importlib.reload(importlib.import_module("app.modules.ai_admin.prompt"))
    assert first.SYSTEM_PROMPT == second.SYSTEM_PROMPT


async def test_the_prompt_source_contains_no_fstring_and_no_concatenated_name() -> None:
    """Structural, over the AST rather than the text: the assignment must be a
    plain string literal (or implicit adjacent-literal concatenation), never a
    `JoinedStr`, a `BinOp`, a `.format(...)` or a `.join(...)`."""
    tree = ast.parse((_AI_ADMIN / "prompt.py").read_text())
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign | ast.Assign)
        and any(
            getattr(target, "id", None) == "SYSTEM_PROMPT"
            for target in (
                [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            )
        )
    ]
    assert len(assignments) == 1
    value = assignments[0].value
    assert isinstance(value, ast.Constant), f"SYSTEM_PROMPT is built from {type(value).__name__}"
    assert isinstance(value.value, str)


async def test_no_module_builds_the_system_prompt_from_anything_else() -> None:
    """GUARD OBSERVABILITY: if someone later adds `org.custom_prompt` and
    concatenates it, this test observes a SECOND construction site. Every use of
    the name outside `prompt.py` must be an import or a bare pass-through
    reference -- never an operand of `+`, `%`, `.format`, `.join`, or an
    f-string.
    """
    offenders: list[str] = []
    for path in sorted(_AI_ADMIN.rglob("*.py")):
        if path.name == "prompt.py":
            continue
        source = path.read_text()
        if "SYSTEM_PROMPT" not in source:
            continue
        for line in source.splitlines():
            if "SYSTEM_PROMPT" not in line:
                continue
            stripped = line.strip()
            if stripped.startswith(("import ", "from ", "#")):
                continue
            if re.search(r"SYSTEM_PROMPT\s*[+%]|[+%]\s*SYSTEM_PROMPT|SYSTEM_PROMPT\.", stripped):
                offenders.append(f"{path.name}: {stripped}")
            if re.search(r'f["\'].*SYSTEM_PROMPT', stripped):
                offenders.append(f"{path.name}: {stripped}")
    assert offenders == []


async def test_the_prompt_states_that_read_mode_cannot_mutate() -> None:
    """Not a control -- a hint. Pinned only so the sentence is not lost in an
    edit, and stated in the docstring above as NOT being what protects anything.
    """
    from app.modules.ai_admin.prompt import SYSTEM_PROMPT

    assert "read mode cannot" in SYSTEM_PROMPT
