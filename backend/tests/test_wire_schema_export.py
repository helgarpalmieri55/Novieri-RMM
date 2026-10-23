"""The exported wire schemas must match the models they claim to export.

`docs/kernel/wire-protocol/*.schema.json` is generated from the Pydantic models
in `app/gateway/protocol.py` and the module schema packages by
`scripts/export_wire_schema.py`. Since `CLAUDE.md` commit `a32cead` the Go agent
lives under `agent/` in this repository and builds its wire structs from those
committed files -- `agent/internal/wire/schema_test.go` reads them directly and
fails if a schema is neither implemented nor explicitly excused.

That makes a stale export a real defect and not a documentation nit: the agent
would be built against a contract the server no longer speaks, and BOTH sides
would stay green. The Python suite tests the models against themselves; the Go
suite tests the structs against the files; nothing until now tested the files
against the models. This closes that link.

It asserts on file CONTENT, not on the export script exiting zero -- a script
that writes nothing exits zero too.

The related guard for the agent-facing side is `test_canonical_corpus.py`.
"""

import filecmp
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMITTED_DIR = REPO_ROOT / "docs" / "kernel" / "wire-protocol"


def _load_exporter():
    """Import scripts/export_wire_schema.py by path.

    `scripts/` is not a package and is not on sys.path; importing by file
    location keeps the script the single source of the export rather than
    duplicating its model list here, which would drift the moment somebody
    exported a new model.
    """
    path = REPO_ROOT / "scripts" / "export_wire_schema.py"
    spec = importlib.util.spec_from_file_location("_export_wire_schema", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_exported_wire_schemas_match_the_models(tmp_path: Path) -> None:
    """Regenerating the export must produce no diff.

    If this guard were deleted, the observation is a
    `docs/kernel/wire-protocol/` file whose JSON differs from the model it
    claims to export -- and since the agent builds its Go structs from those
    files, the agent would be built against a contract the server no longer
    speaks, with every test in both languages still green.
    """
    _load_exporter().export(tmp_path, quiet=True)

    regenerated = {p.name for p in tmp_path.glob("*.schema.json")}
    committed = {p.name for p in COMMITTED_DIR.glob("*.schema.json")}
    assert regenerated, "the exporter wrote nothing"
    assert regenerated == committed, (
        "the set of exported schemas differs from what is committed under "
        f"docs/kernel/wire-protocol/. Only in the export: "
        f"{sorted(regenerated - committed)}. Only committed: "
        f"{sorted(committed - regenerated)}. Re-run "
        "`backend/.venv/bin/python scripts/export_wire_schema.py`."
    )

    stale = [
        name
        for name in sorted(regenerated)
        if not filecmp.cmp(tmp_path / name, COMMITTED_DIR / name, shallow=False)
    ]
    assert not stale, (
        "these committed schemas no longer match their Pydantic models: "
        f"{stale}. The Go agent builds its wire structs from these files "
        "(agent/internal/wire), so it is now built against a contract the "
        "server does not speak. Re-run "
        "`backend/.venv/bin/python scripts/export_wire_schema.py`."
    )


def test_every_committed_schema_is_valid_json_with_a_title() -> None:
    """The Go side keys its implemented/notImplemented maps on `title`.

    A schema file with no title, or a duplicated title, would make
    `agent/internal/wire/schema_test.go` silently collapse two contracts into
    one entry rather than fail.
    """
    titles: dict[str, str] = {}
    for path in sorted(COMMITTED_DIR.glob("*.schema.json")):
        data = json.loads(path.read_text())
        title = data.get("title")
        assert title, f"{path.name} has no title"
        assert title not in titles, f"{path.name} and {titles[title]} share the title {title!r}"
        titles[title] = path.name
    assert len(titles) >= 20, f"only {len(titles)} schemas found; wrong directory?"


@pytest.mark.parametrize(
    "kind",
    [
        "job",
        "job_status",
        "job_log",
        "job_result",
        "heartbeat",
        "security_events",
        "health_report",
    ],
)
def test_every_protocol_message_kind_has_an_exported_schema(kind: str) -> None:
    """A message kind the gateway speaks but never exports is a contract the
    agent cannot be built against, and it would go unnoticed: the Python suite
    would exercise it end to end against itself.
    """
    exported = set()
    for path in COMMITTED_DIR.glob("*.schema.json"):
        # `.get("const")`, not `["const"]`: a `kind` property that is an enum
        # rather than a const belongs to a document type (a NetSweepDocument
        # host, a health report line), not to a framed message.
        const = json.loads(path.read_text()).get("properties", {}).get("kind", {}).get("const")
        if const is not None:
            exported.add(const)
    assert kind in exported, f"message kind {kind!r} has no exported schema"
