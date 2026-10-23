"""Managed distribution: what a GPO or Intune deployment actually consumes.

`docs/modules/agent-distribution/spec-managed-distribution.md`.

THE POINT OF THIS FILE IS A NEGATIVE: nothing it returns is a credential, and
nothing that calls it mints one. A GPO software-distribution point is read by
every machine account in the domain by construction, so a token that reached
the package would be a permanent, fleet-wide enrolment credential sitting on a
share -- which `agent/packaging/README.md` forbids in as many words ("A token
inside a `.deb` is copied to file servers, baked into golden images and cached
by apt. One image build and the whole fleet shares one token").

So the artefact half and the credential half are two routes under two
permissions, and this module is only ever the artefact half. The credential is
`enrollment.create_token` called from `api.py`, with bounds that cannot be
omitted (spec §3.3).

WHY THE OPERATOR-FACING TEXT IS SERVER-SIDE RATHER THAN IN THE CONSOLE. Two of
these strings are security claims -- "the endpoint verifies the digest before
installing" is TRUE on the manual channel and FALSE on the GPO one, and the
difference is caused by the deployment mechanism rather than by anything a
renderer knows. A claim that lives in a React component drifts from the module
that makes it true, silently, and no test in either tree is looking at both.
`tests/test_agent_managed_deployment.py` asserts these strings, which it can
only do because they are here.
"""

from dataclasses import dataclass

from app.core.i18n import N_, tr, tr_marked

#: The distribution mechanisms this module writes instructions for.
#:
#: REFUSED RATHER THAN DEFAULTED when the caller names something else, because
#: the channel decides the integrity accounting: an unknown channel silently
#: falling back to `manual` would hand a GPO administrator the statement
#: "the endpoint verifies before installing", which on their path is false.
CHANNELS = ("gpo", "intune", "manual")

#: The most machines one deployment token may enrol, and the longest it may
#: live. BOTH ARE REQUIRED OF THE CALLER -- see `schemas.DeploymentTokenRequest`
#: -- and these are only the ceilings.
#:
#: 1000 is above Toeshee's 250-300 endpoints and below "a fleet nobody is
#: counting". 336 hours is fourteen days: long enough for a GPO refresh cycle
#: plus the laptop that was on holiday, short enough that a forgotten token
#: dies rather than becoming infrastructure.
MAX_DEPLOYMENT_USES = 1000
MAX_DEPLOYMENT_HOURS = 336


@dataclass(frozen=True)
class Step:
    """One thing the administrator does, with the command if there is one."""

    title: str
    detail: str
    command: str


@dataclass(frozen=True)
class Integrity:
    """What the SHA-256 comparison becomes once the artefact leaves the console.

    `spec.md` §4.4 rests integrity on one comparison -- the digest the server
    computed against the digest of the bytes the operator built -- and on the
    one-machine path that comparison is ALSO re-run on the endpoint by
    `sha256sum -c` immediately before install.

    On a channel that pushes the file there is no such step, and no wording
    invents one. `verified_at_install` is that fact as a field rather than a
    paragraph, so the console cannot render a reassurance the path does not
    earn.
    """

    sha256: str
    verify_command: str
    verified_at_install: bool
    notes: tuple[str, ...]


@dataclass(frozen=True)
class DeploymentPackage:
    channel: str
    staging: tuple[Step, ...]
    install_command: str
    enrol_command: str
    integrity: Integrity


def _artifact_filename(os_name: str, arch: str) -> str:
    return f"its-agent-{os_name}-{arch}{'.exe' if os_name == 'windows' else ''}"


def _verify_command(os_name: str, filename: str, sha256: str) -> str:
    """Verify the file the ADMINISTRATOR now holds, on the machine they hold it.

    Not the endpoint. This is spec §4 step 2 -- the comparison that establishes
    "the file I am about to share with the whole domain is the file the
    catalogue holds" -- and it is the only one of the three checks this module
    can hand over as a paste.
    """
    if os_name == "windows":
        return (
            f"if ((Get-FileHash '{filename}' -Algorithm SHA256).Hash "
            f"-ne '{sha256.upper()}') {{ throw 'digest mismatch - do not stage this file' }}"
        )
    return f"echo '{sha256}  {filename}' | sha256sum -c -"


def _install_command(os_name: str, filename: str) -> str:
    """Install the service and DO NOT ENROL.

    The two-step shape is the whole managed path: the package installs, and the
    channel's own secret mechanism enrols afterwards. `-NoEnroll` is not a new
    switch -- `install.ps1` has always installed without a token when none is
    present -- but it is now explicit, and the script no longer starts a service
    that has nothing to enrol with (spec §5.1).

    NOTHING HERE NAMES THE TOKEN, and a test asserts that: an install
    invocation carrying a credential would be run by every machine in the
    domain, and Windows records process command lines in Security event 4688
    where command-line auditing is on.
    """
    if os_name == "windows":
        return (
            "powershell.exe -NoProfile -ExecutionPolicy Bypass "
            "-File \\\\<dfs-or-sysvol>\\its-agent\\install.ps1 "
            f"-Binary \\\\<dfs-or-sysvol>\\its-agent\\{filename} -NoStart"
        )
    if os_name == "darwin":
        return "sudo /path/to/its-agent/packaging/darwin/install.sh --no-start"
    return "sudo apt install -y ./its-agent_<version>_<arch>.deb"


def _enrol_command(os_name: str) -> str:
    """The separate step that spends the credential, token never in argv.

    `agent/packaging/README.md` names three places a token must never end up --
    argv, a service definition, and a distributable artifact -- and the shipped
    helpers enforce the first by refusing `--token` / `-Token`. This string is
    the other side of that: what the console tells an administrator to run.
    """
    if os_name == "windows":
        return (
            "$env:ITS_AGENT_ENROLL_TOKEN = <read from your secret store>; "
            "powershell.exe -NoProfile -ExecutionPolicy Bypass "
            "-File \\\\<dfs-or-sysvol>\\its-agent\\its-agent-enroll.ps1 "
            "-Url https://<your-console>"
        )
    if os_name == "darwin":
        return (
            "sudo ITS_AGENT_ENROLL_TOKEN=<from your secret store> "
            "/path/to/its-agent/packaging/darwin/install.sh --url https://<your-console>"
        )
    return (
        "printf %s \"$TOKEN\" | sudo its-agent-enroll --token-stdin "
        "--url https://<your-console>"
    )


#: What is true of the integrity chain on a channel that PUSHES the file, said
#: once. Every clause is a fact about the mechanism, not a caveat about this
#: implementation.
_PUSHED_NOTES = (
    N_(
        "Verify the digest above once, on the file you downloaded, BEFORE you copy it "
        "to the distribution point. That comparison is the last one anybody makes."
    ),
    N_(
        "The endpoint does not verify anything. GPO software installation and Intune "
        "copy a file from a distribution point and run it; neither checks a digest, so "
        "the check that the one-machine install command performs on the endpoint does "
        "not happen on this path."
    ),
    N_(
        "What protects the file afterwards is the ACL on your distribution point. "
        "Anyone who can write to that share can replace this binary and neither this "
        "platform nor Windows will notice. Restrict write access to the administrators "
        "who manage it."
    ),
    N_(
        "This binary is unsigned. There is no Authenticode signature to fall back on "
        "(agent/packaging/README.md, \"What is still unsigned\"), so the share's ACL is "
        "not one control among several — it is the only one."
    ),
)

_MANUAL_NOTES = (
    N_(
        "A person runs this command on the machine, so the endpoint-side digest check "
        "of the one-machine install path still happens here."
    ),
    N_(
        "This binary is unsigned; the digest comparison is what stands in for a code "
        "signature (agent/packaging/README.md, \"What is still unsigned\")."
    ),
)


def _staging(channel: str, *, filename: str, url: str, verify: str) -> tuple[Step, ...]:
    download = Step(
        title=tr("Download the artefact once"),
        detail=tr(
            "This link is short-lived and authenticated by the grant that minted it. "
            "Download it here, on your own workstation — the endpoints never call it."
        ),
        command=f"curl -fL -o {filename} '{url}'",
    )
    verify_step = Step(
        title=tr("Verify what you downloaded"),
        detail=tr(
            "Against the SHA-256 this console computed over the bytes it stored. "
            "Do this before the file reaches a share, not after."
        ),
        command=verify,
    )
    if channel == "gpo":
        return (
            download,
            verify_step,
            Step(
                title=tr("Stage it on a distribution point"),
                detail=tr(
                    "A DFS or SYSVOL path every target machine account can read, and "
                    "that only your administrators can write. Copy install.ps1 and "
                    "its-agent-enroll.ps1 from agent/packaging/windows/ alongside it."
                ),
                command="",
            ),
            Step(
                title=tr("Assign it as a computer startup script"),
                detail=tr(
                    "Computer Configuration > Policies > Windows Settings > Scripts "
                    "(Startup). Startup scripts run as SYSTEM, which is what the "
                    "install needs. There is no MSI, so GPO's Software Installation "
                    "policy is not the mechanism here — see the spec §5.3."
                ),
                command="",
            ),
            Step(
                title=tr("Deliver the token separately"),
                detail=tr(
                    "Mint a deployment token below and have the startup script read it "
                    "from a location only machine accounts can read. It must never be "
                    "in the script's arguments, and never in the package."
                ),
                command="",
            ),
        )
    if channel == "intune":
        return (
            download,
            verify_step,
            Step(
                title=tr("Wrap it as a Win32 app"),
                detail=tr(
                    "IntuneWinAppUtil over a folder holding the binary, install.ps1 and "
                    "its-agent-enroll.ps1. Intune encrypts the payload at rest and "
                    "delivers it to SYSTEM on the assigned device."
                ),
                command="IntuneWinAppUtil.exe -c .\\its-agent -s install.ps1 -o .\\out",
            ),
            Step(
                title=tr("Set the detection rule"),
                detail=tr(
                    "File exists: %ProgramFiles%\\ITSuite\\its-agent.exe. Do not detect "
                    "on the enrolled state — an installed-but-unenrolled agent is a "
                    "normal intermediate state on this path."
                ),
                command="",
            ),
            Step(
                title=tr("Deliver the token separately"),
                detail=tr(
                    "Mint a deployment token below and put it in the app's own script "
                    "content or a linked key vault. It must never be in the package."
                ),
                command="",
            ),
        )
    return (
        download,
        verify_step,
        Step(
            title=tr("Distribute with your own tooling"),
            detail=tr(
                "Ansible, Salt, a kickstart, or a technician at the desk. Supply the "
                "token from your secret store (an Ansible no_log task, a vault lookup) "
                "and never from the artefact."
            ),
            command="",
        ),
    )


def deployment_package(
    *,
    channel: str,
    os_name: str,
    arch: str,
    sha256: str,
    download_url: str,
) -> DeploymentPackage:
    """Everything a managed deployment consumes, and no credential.

    Raises ValueError on an unknown channel -- refused rather than defaulted,
    because the channel decides the integrity statement.
    """
    if channel not in CHANNELS:
        raise ValueError(
            tr(
                "unknown distribution channel {channel}; expected one of {channels}",
                channel=repr(channel),
                channels=list(CHANNELS),
            )
        )
    filename = _artifact_filename(os_name, arch)
    verify = _verify_command(os_name, filename, sha256)
    pushed = channel in ("gpo", "intune")
    return DeploymentPackage(
        channel=channel,
        staging=_staging(channel, filename=filename, url=download_url, verify=verify),
        install_command=_install_command(os_name, filename),
        enrol_command=_enrol_command(os_name),
        integrity=Integrity(
            sha256=sha256,
            verify_command=verify,
            verified_at_install=not pushed,
            notes=tuple(tr_marked(note) for note in (_PUSHED_NOTES if pushed else _MANUAL_NOTES)),
        ),
    )


def blast_radius(*, channel: str, max_uses: int, hours: int) -> tuple[str, ...]:
    """What the holder of this token gains if the distribution channel leaks it.

    SERVER-SIDE, AND WORDED PER CHANNEL, because the answer to "who can read
    it" differs by an order of magnitude between them and that difference is
    the operator's whole decision. Spec §3.4 does the accounting; this is the
    part that has to be on the screen at the moment somebody presses the
    button.
    """
    who = {
        "gpo": (
            tr(
                "Anyone who can run code as SYSTEM on any domain-joined machine in scope "
                "can read this token — which in practice means any local administrator of "
                "any machine in the deployment."
            ),
        ),
        "intune": (
            tr(
                "Intune encrypts the payload at rest and runs it as SYSTEM on the assigned "
                "device, so reading this token needs local administrator on an assigned "
                "device, or Intune administrator."
            ),
        ),
        "manual": (tr("Whoever can read the secret store you put this in can read this token."),),
    }[channel]
    return who + (
        tr(
            "A holder can enrol up to {max_uses} machines into this organization, for the "
            "next {hours} hours, until you revoke it.",
            max_uses=max_uses,
            hours=hours,
        ),
        tr(
            "Each enrolment produces a device record and a device certificate: it appears "
            "in the fleet list and submits inventory this console will believe."
        ),
        tr(
            "A rogue device is a device somebody may later target. Any job whose signed "
            "parameters carry a secret delivers that secret to whatever answers as that "
            "device — this is the largest item on this list."
        ),
        tr(
            "It cannot forge a job, dispatch anything, read another device's data, promote "
            "itself to a site server, or mint another token."
        ),
        tr(
            "Watch the use count on the Enrollment page: {max_uses} uses for a smaller "
            "rollout hides the difference. Size this number to the machines you are "
            "actually deploying to.",
            max_uses=max_uses,
        ),
        tr("Revoking the token on the Enrollment page kills every remaining use at once."),
    )
