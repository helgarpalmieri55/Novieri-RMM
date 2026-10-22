"""Nothing in the agent-distribution module may dial an endpoint.

WHY A GREP IS THE RIGHT TOOL HERE, unusually. This module exists *because* a
push install was declined: the operator asked for "deploy the agent from the UI
with credentials", and that was refused on invariant 2 --

    Agents connect outbound only, over mutual TLS. The server never opens an
    inbound connection to an endpoint. No agent listening port.

-- with the reasoning recorded in `docs/modules/agent-distribution/spec.md` §1.
The module's entire shape (publish an artefact, hand over a command, let the
operator distribute it) is the consequence of that decision.

The cheapest way that decision regresses is not a redesign. It is somebody
adding "just a quick reachability check before we show the install command", or
a helpful SSH one-liner, in a module whose name makes it sound like the natural
home for exactly that. A grep catches that; nothing subtler is warranted, and a
subtler check would be easier to satisfy without meaning it.

WHAT THIS DOES NOT PROVE. That the module is safe, that no transitive
dependency opens a socket, or that a future author cannot import a client under
an alias. It proves that the obvious ways to write a push install are not
present, and it fails loudly enough to make the deliberate ones a conversation.
"""

from pathlib import Path

MODULE = Path(__file__).resolve().parent.parent / "app" / "modules" / "agent_distribution"

#: Client libraries and calls that would mean the control plane reaching an
#: endpoint. `socket.create_connection` and `asyncio.open_connection` are the
#: stdlib routes; the rest are the libraries somebody would reach for to write
#: a remote install over SSH, WinRM or SMB.
FORBIDDEN = (
    "paramiko",
    "asyncssh",
    "fabric",
    "pywinrm",
    "winrm",
    "smbprotocol",
    "impacket",
    "pypsrp",
    "socket.create_connection",
    "asyncio.open_connection",
    "subprocess",
)


def _sources() -> dict[Path, str]:
    return {path: path.read_text() for path in sorted(MODULE.rglob("*.py"))}


def test_the_module_has_source_to_check() -> None:
    """Non-vacuity.

    Without this, renaming or moving the package makes every assertion below
    pass over an empty mapping -- a guard that cannot fail, reporting green
    about a module it is no longer reading.
    """
    sources = _sources()
    assert sources, f"no python sources found under {MODULE}"
    assert any(path.name == "api.py" for path in sources), sources.keys()


def test_nothing_here_can_reach_an_endpoint() -> None:
    offenders: list[str] = []
    for path, source in _sources().items():
        for name in FORBIDDEN:
            if name in source:
                offenders.append(f"{path.name}: {name}")
    assert not offenders, (
        "the agent-distribution module must not be able to open a connection to "
        "an endpoint -- invariant 2, and the reason this module exists in this "
        f"shape at all (spec §1). Found: {offenders}"
    )


def test_the_check_would_catch_a_planted_push_install() -> None:
    """The mutation proof, so the test above is known to be able to fail.

    A guard over source text is exactly the kind that silently stops matching --
    a renamed constant, a changed path, a `rglob` that finds nothing. This
    asserts the matcher fires on a string that contains what it is looking for,
    without editing the shipped module.
    """
    planted = "import paramiko\nclient = paramiko.SSHClient()\n"
    assert any(name in planted for name in FORBIDDEN)
