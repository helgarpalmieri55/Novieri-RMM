"""Business logic for the agent release catalogue.

Route handlers stay thin (`CLAUDE.md`: business logic lives in services), and
everything here is written against a session the caller owns.
"""

import base64
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.storage import ObjectStore, agent_release_suffix
from app.models.core import AgentRelease

#: The install command is written for ONE machine at a time and pasted by an
#: operator, so the grant only has to outlive a copy/paste and a download. A
#: long-lived URL is a durable, unauthenticated read of the artefact.
DOWNLOAD_TTL_S = 900

#: BYTE-IDENTICAL COPIES of the reviewed installer scripts, shipped inside the
#: backend so the generated command can carry them. The originals live in
#: `agent/packaging/`, which is the source of record; these copies exist ONLY
#: because the backend container image does not include the agent tree
#: (`deploy/backend.Dockerfile` copies `backend/app` and nothing else), so at
#: request time this process cannot read the packaging directory. The command
#: must reach a machine that holds no credential and no repository, so the
#: installer has to travel WITH the command.
#:
#: They are not re-derived logic. The whole point is to run the exact
#: `install.ps1` / `install.sh` that was reviewed line by line on 2026-08-26 and
#: whose brackets, ACL calls and unenrolled-start guard are checked by
#: `agent/packaging/packaging_test.go` -- rewriting the New-Service / Set-Acl /
#: systemd-unit steps into a one-liner would put the single most important
#: observation in `first-run-verification.md` (A4, the data-dir ACL) into an
#: untested string this CI cannot even parse. `tests/test_installer_scripts.py`
#: pins each copy to its `agent/packaging/` original so the two cannot drift.
_INSTALLERS_DIR = Path(__file__).parent / "installers"


@cache
def _installer_bytes(rel: str) -> bytes:
    """The raw bytes of one bundled installer file, cached.

    Read as bytes and delivered verbatim: the base64 the command carries must
    decode to the same bytes `packaging_test.go` verified, down to the trailing
    newline and the UTF-8 of the here-strings.
    """
    return (_INSTALLERS_DIR / rel).read_bytes()


async def list_releases(session: AsyncSession) -> list[AgentRelease]:
    rows = (
        await session.execute(
            select(AgentRelease).order_by(
                AgentRelease.published_at.desc().nulls_last(),
                AgentRelease.version.desc(),
            )
        )
    ).scalars()
    return list(rows)


async def get_release(session: AsyncSession, release_id: UUID) -> AgentRelease:
    release = (
        await session.execute(select(AgentRelease).where(AgentRelease.id == release_id))
    ).scalar_one_or_none()
    if release is None:
        raise NoResultFound("no such agent release")
    return release


async def create_release(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    version: str,
    os_name: str,
    arch: str,
    signed: bool,
    notes: str,
) -> AgentRelease:
    """Register a version. Validates the key BEFORE inserting anything.

    `agent_release_suffix` is called here rather than at upload time so a
    version that cannot produce a safe key is refused at the point an operator
    typed it, not later when the bytes arrive and the row already exists.
    """
    storage_ref = agent_release_suffix(version, os_name, arch)
    release = AgentRelease(
        version=version,
        os=os_name,
        arch=arch,
        storage_ref=storage_ref,
        signed=signed,
        notes=notes,
        published_at=datetime.now(UTC),
        published_by_user_id=actor_id,
    )
    session.add(release)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="agent.release_published",
        target_type="agent_release",
        target_id=str(release.id),
        metadata={"version": version, "os": os_name, "arch": arch},
    )
    return release


async def store_artifact(
    session: AsyncSession,
    store: ObjectStore,
    *,
    org_id: UUID,
    actor_id: UUID,
    release_id: UUID,
    data: bytes,
) -> AgentRelease:
    """Upload the bytes and record the SERVER's digest of them.

    The digest is computed here, over exactly the bytes that were received. A
    client-supplied digest would be the same party attesting to their own
    upload, which verifies nothing -- and the value is what an operator
    compares against their own `sha256sum` of the file they built, so it has to
    mean "what this server stored".
    """
    release = await get_release(session, release_id)
    # Re-derive rather than trusting the stored ref: a storage_ref read back
    # from the database was a request body once, and the validation in force
    # when it was written is not necessarily the validation in force now.
    key = agent_release_suffix(release.version, release.os, release.arch)
    await store.put_agent_release(key, data)
    release.storage_ref = key
    release.size_bytes = len(data)
    release.sha256 = hashlib.sha256(data).hexdigest()
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="agent.release_artifact_stored",
        target_type="agent_release",
        target_id=str(release.id),
        metadata={
            "version": release.version,
            "os": release.os,
            "arch": release.arch,
            "size_bytes": release.size_bytes,
            "sha256": release.sha256,
        },
    )
    return release


async def delete_release(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, release_id: UUID
) -> None:
    release = await get_release(session, release_id)
    version, os_name, arch = release.version, release.os, release.arch
    await session.delete(release)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="agent.release_unpublished",
        target_type="agent_release",
        target_id=str(release_id),
        metadata={"version": version, "os": os_name, "arch": arch},
    )


class ReleaseInUse(Exception):
    """An active update rollout, in some organisation, still targets it."""


@dataclass(frozen=True)
class UnpublishResult:
    releases: list[AgentRelease]
    #: Past rollout targets (completed or cancelled) whose link was cleared.
    history_cleared: int


async def unpublish(
    admin_session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    release_ids: list[UUID],
) -> UnpublishResult:
    """Remove catalogue rows, all or none, in the caller's transaction.

    ON THE ADMIN ENGINE, because the catalogue is shared by every tenant and so
    are the rollouts that point into it: RLS would hide another organisation's
    rollout, and unpublishing under it would strand its targets.

    ACTIVE ROLLOUTS REFUSE; PAST ONES LET GO. A running or paused rollout still
    needs the row, so any hit raises `ReleaseInUse` before anything changes. A
    completed or cancelled rollout only remembers it: its targets keep their
    `from_version` and the rollout its `target_version`, and their nullable
    `release_id` is cleared, which is what used to make the delete die on the
    foreign key -- after earlier platforms of the same version had already
    gone (Codex review on #87).

    The files are NOT touched here: the caller deletes them after this commits,
    so a failed commit never leaves rows pointing at bytes that are gone.
    """
    from app.modules.agent_update.models import (
        ACTIVE_ROLLOUT_STATES,
        AgentUpdateRollout,
        AgentUpdateTarget,
    )

    releases = list(
        (
            await admin_session.execute(
                select(AgentRelease)
                .where(AgentRelease.id.in_(release_ids))
                .with_for_update()
            )
        ).scalars()
    )
    if len(releases) != len(set(release_ids)):
        raise NoResultFound("no such agent release")
    in_use = (
        await admin_session.execute(
            select(AgentUpdateTarget.id)
            .join(AgentUpdateRollout, AgentUpdateRollout.id == AgentUpdateTarget.rollout_id)
            .where(
                AgentUpdateTarget.release_id.in_(release_ids),
                AgentUpdateRollout.state.in_(ACTIVE_ROLLOUT_STATES),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if in_use is not None:
        raise ReleaseInUse()

    cleared_result = await admin_session.execute(
        update(AgentUpdateTarget)
        .where(AgentUpdateTarget.release_id.in_(release_ids))
        .values(release_id=None)
    )
    cleared = int(getattr(cleared_result, "rowcount", 0) or 0)
    for release in releases:
        await admin_session.delete(release)
    await admin_session.flush()
    for release in releases:
        await audit_service.record(
            admin_session,
            org_id=org_id,
            actor_type="user",
            actor_id=actor_id,
            action="agent.release_unpublished",
            target_type="agent_release",
            target_id=str(release.id),
            metadata={
                "version": release.version,
                "os": release.os,
                "arch": release.arch,
                "history_cleared": cleared,
            },
        )
    return UnpublishResult(releases=releases, history_cleared=cleared)


def install_command(
    *,
    os_name: str,
    download_url: str,
    sha256: str,
    token: str,
    enroll_url: str,
) -> str:
    """A one-machine command that installs, registers and starts the agent.

    IT RUNS THE REAL INSTALLER, and that is the fix. The previous version wrote
    a token file and then ran `.\\install.ps1` (Windows) or placed a bare binary
    (unix) -- neither of which registers a service, and on Windows the script it
    invoked was never even downloaded. This one downloads the binary, verifies
    it, and then runs the exact `install.ps1` / `install.sh` that `agent/
    packaging/` ships: the scripts that create the service, break the
    data-directory ACL down to SYSTEM+Administrators (Windows) or write the
    hardened systemd unit (Linux), configure the restart actions, and start the
    service only once it has a token to spend. The installer travels WITH the
    command, base64-encoded, because the machine it targets has neither this
    repository nor a credential to fetch it -- see `_INSTALLERS_DIR`.

    THE TOKEN NEVER BECOMES AN ARGUMENT. `agent/packaging/README.md` names three
    places it must not end up -- argv, a service definition, and a distributable
    artefact -- and the shipped scripts enforce the first by refusing `--token`
    / `-Token`. This command keeps to the two forms they DO accept: the
    environment variable (Windows, inherited by the installer it spawns) and
    real stdin (unix, `--token-stdin`). It is never an option value, so it never
    reaches `/proc/<pid>/cmdline`, `ps`, or Windows event 4688.

    THE CHECKSUM STEP IS NOT DECORATION, AND IT NO LONGER DEPENDS ON A CMDLET
    THAT CAN BE ABSENT. The download is over the operator's own `:443`, which on
    an internal deployment is very often an internally-issued certificate; a
    digest the operator can compare against what the console shows is what makes
    the artefact verifiable independently of the TLS chain. The Windows branch
    computes it with `[Security.Cryptography.SHA256]` over the file's bytes --
    present on Windows PowerShell 5.1 and PowerShell 7 alike -- rather than
    `Get-FileHash`, which the operator hit as `not recognized` and which is
    absent on the older shells a fleet still runs.

    The token IS still a literal in the command, so it lands in the operator's
    shell history. That is a stated residual (spec §4.4), mitigated by the token
    being single-use and short-lived rather than pretended away.

    `enroll_url` is the control-plane base the agent enrolls against. Without it
    the token has nowhere to be spent -- the old command passed no URL at all,
    so even a machine that already had `install.ps1` would have staged a service
    with a token and no address. The route derives it from the request the
    operator is already authenticated on.
    """
    if os_name == "windows":
        script_b64 = base64.b64encode(_installer_bytes("windows/install.ps1")).decode(
            "ascii"
        )
        # A child PowerShell with -ExecutionPolicy Bypass runs the downloaded
        # install.ps1: it inherits ITS_AGENT_ENROLL_TOKEN from the environment
        # (never argv) and takes -Binary and -Url as ordinary arguments, which
        # the script and first-run-verification.md A3.2 both expect to be
        # visible. The finally clears the token from this shell's environment.
        return (
            "$ErrorActionPreference='Stop'; "
            "$d=Join-Path $env:TEMP 'its-agent-install'; "
            "New-Item -ItemType Directory -Force -Path $d | Out-Null; "
            "$exe=Join-Path $d 'its-agent.exe'; "
            f"(New-Object Net.WebClient).DownloadFile('{download_url}', $exe); "
            "$h=[BitConverter]::ToString("
            "[Security.Cryptography.SHA256]::Create().ComputeHash("
            "[IO.File]::ReadAllBytes($exe))).Replace('-',''); "
            f"if ($h -ne '{sha256.upper()}') {{ throw 'checksum mismatch' }}; "
            "$ps1=Join-Path $d 'install.ps1'; "
            f"[IO.File]::WriteAllBytes($ps1, [Convert]::FromBase64String('{script_b64}')); "
            f"$env:ITS_AGENT_ENROLL_TOKEN='{token}'; "
            "try { & powershell -NoProfile -ExecutionPolicy Bypass -File $ps1 "
            f"-Binary $exe -Url '{enroll_url}' }} "
            "finally { $env:ITS_AGENT_ENROLL_TOKEN=$null }"
        )

    if os_name == "darwin":
        # macOS: BSD base64 decodes with -D, and shasum -a 256 stands in for
        # sha256sum, which macOS does not ship. darwin/install.sh reads the
        # plist from its own directory, so it is written next to it under the
        # name the script looks for.
        sh_b64 = base64.b64encode(_installer_bytes("darwin/install.sh")).decode("ascii")
        plist_b64 = base64.b64encode(
            _installer_bytes("darwin/com.toeshee.itsagent.plist")
        ).decode("ascii")
        return (
            "set -e; d=$(mktemp -d); "
            f"curl -fsSL '{download_url}' -o \"$d/its-agent\"; "
            f"printf '%s  %s' '{sha256}' \"$d/its-agent\" | shasum -a 256 -c -; "
            f"printf %s '{sh_b64}' | base64 -D > \"$d/install.sh\"; "
            f"printf %s '{plist_b64}' | base64 -D > \"$d/com.toeshee.itsagent.plist\"; "
            "chmod +x \"$d/install.sh\"; "
            f"printf '%s' '{token}' | sudo bash \"$d/install.sh\" "
            f"--token-stdin --url '{enroll_url}' --binary \"$d/its-agent\""
        )

    # Linux. install.sh reads its-agent.service from its own directory and does
    # the rest: places the binary, writes the token file 0600, writes the URL
    # env file, installs the hardened unit, enables and starts the service. The
    # token reaches it on stdin via --token-stdin, never as an argument.
    sh_b64 = base64.b64encode(_installer_bytes("linux/install.sh")).decode("ascii")
    unit_b64 = base64.b64encode(_installer_bytes("linux/its-agent.service")).decode(
        "ascii"
    )
    return (
        "set -e; d=$(mktemp -d); "
        f"curl -fsSL '{download_url}' -o \"$d/its-agent\"; "
        f"printf '%s  %s' '{sha256}' \"$d/its-agent\" | sha256sum -c -; "
        f"printf %s '{sh_b64}' | base64 -d > \"$d/install.sh\"; "
        f"printf %s '{unit_b64}' | base64 -d > \"$d/its-agent.service\"; "
        "chmod +x \"$d/install.sh\"; "
        f"printf '%s' '{token}' | sudo bash \"$d/install.sh\" "
        f"--token-stdin --url '{enroll_url}' --binary \"$d/its-agent\""
    )
