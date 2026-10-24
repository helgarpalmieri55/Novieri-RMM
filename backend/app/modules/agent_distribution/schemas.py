"""Request and response models for the agent release catalogue."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.storage import AGENT_RELEASE_ARCH, AGENT_RELEASE_OS
from app.modules.agent_distribution.managed import (
    MAX_DEPLOYMENT_HOURS,
    MAX_DEPLOYMENT_USES,
)


class ReleaseCreateRequest(BaseModel):
    """Register a version before its bytes are uploaded.

    `extra="forbid"` so a caller who sends `sha256` gets a 422 rather than a
    silent no-op that reads like success. The digest is computed by the server
    from the bytes it receives (`store_artifact`) and is deliberately not
    settable here: a digest supplied alongside the bytes by the same party
    verifies nothing.
    """

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=64)
    os: str = Field(pattern="|".join(sorted(AGENT_RELEASE_OS)))
    arch: str = Field(pattern="|".join(sorted(AGENT_RELEASE_ARCH)))
    signed: bool = False
    notes: str = Field(default="", max_length=500)


class ReleaseOut(BaseModel):
    """One published agent artefact: a version, for one OS and architecture.

    `sha256` is computed by the SERVER over the bytes it received and is what an
    operator compares against their own `sha256sum` of the build. `signed` is a
    claim the publisher made, not something this platform verified.
    """

    id: UUID
    version: str
    os: str
    arch: str
    size_bytes: int
    sha256: str
    signed: bool
    notes: str
    published_at: datetime | None
    # Self-update (spec-self-update.md §6.1): `pilot` reaches pilot rings only.
    channel: str = "pilot"
    # Whether the offline release signature has been recorded for this row.
    release_signed: bool = False
    #: False until the bytes land. The console renders these rows differently:
    #: a release with no artefact cannot produce an install command.
    has_artifact: bool


class InstallCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Hours, not days: this mints a credential that enrols a device into the
    #: tenant, and the generated command puts it in an operator's shell
    #: history. A short life is the mitigation -- see the spec §4.4.
    expires_in_hours: int = Field(default=4, ge=1, le=72)
    #: The site the machine lands in, and with it its client (MSP clients spec
    #: §1.5): the token this mints is that site's, like every enrollment token.
    site_id: UUID = Field(
        description="The site the machine lands in; its client is the site's client"
    )


class InstallCommandOut(BaseModel):
    release: ReleaseOut
    #: Shown exactly once, like every other enrollment token in this platform.
    token: str
    token_expires_at: datetime
    download_url: str
    download_expires_in_s: int
    sha256: str
    #: A ready-to-paste command for ONE machine. It passes the token on stdin,
    #: never in argv.
    command: str


# --------------------------------------------------------------------------
# Managed distribution (spec-managed-distribution.md §2)
# --------------------------------------------------------------------------


class DeploymentStepOut(BaseModel):
    """One thing the administrator does. `command` is "" when there is nothing
    to paste -- an empty string rather than a null so the console renders the
    absence the same way every time."""

    title: str
    detail: str
    command: str


class DeploymentIntegrityOut(BaseModel):
    """What `spec.md` §4.4's digest comparison becomes on a pushed package.

    `verified_at_install` IS A FIELD RATHER THAN PROSE precisely because it is
    False on the channels an operator is most likely to use. GPO and Intune copy
    a file from a distribution point and run it; neither checks a digest. A
    console that rendered the same reassurance for every channel would be making
    a claim the mechanism does not support.
    """

    sha256: str
    #: Run by the ADMINISTRATOR on the file they downloaded, before it reaches a
    #: share. Not by the endpoint.
    verify_command: str
    verified_at_install: bool
    notes: list[str]


class DeploymentPackageOut(BaseModel):
    """Everything a managed deployment consumes.

    THERE IS NO TOKEN FIELD AND THERE MUST NEVER BE ONE. The credential is a
    separate route under a separate permission
    (`POST .../deployment-token`), and the reason is the reason this whole
    module exists: a GPO distribution point is read by every machine account in
    the domain, so a package carrying a credential IS a fleet-wide standing
    secret. `tests/test_agent_managed_deployment.py` asserts the negative from
    both sides -- no token in the body, and no `enrollment_tokens` row created.
    """

    release: ReleaseOut
    channel: str
    download_url: str
    download_expires_in_s: int
    sha256: str
    staging: list[DeploymentStepOut]
    #: Installs the service and does NOT enrol. Names no token.
    install_command: str
    #: Spends the credential, in a separate step, with the token from the
    #: environment or stdin and never from argv.
    enrol_command: str
    integrity: DeploymentIntegrityOut


class DeploymentTokenRequest(BaseModel):
    """The bounds ARE the blast radius, so neither is optional and neither is
    defaulted.

    `EnrollmentToken.max_uses` is nullable and null means UNLIMITED, and
    `POST /api/v1/enrollment/tokens` defaults it to null -- so on every other
    surface of this platform "forgot the argument" is the unbounded case. This
    request cannot express it: `max_uses` has no default, refuses null, and is
    capped; the lifetime is capped at fourteen days. See spec §3.3, and §3.5 for
    why the kernel route's default was not changed instead.
    """

    model_config = ConfigDict(extra="forbid")

    #: Size it to the machines being deployed to, not to the ceiling. The use
    #: count on the Enrollment page is the detection mechanism, and slack in
    #: this number is exactly what it cannot detect.
    max_uses: int = Field(ge=1, le=MAX_DEPLOYMENT_USES)
    #: Long enough for a GPO refresh cycle plus a laptop that was away; short
    #: enough that a forgotten token dies instead of becoming infrastructure.
    expires_in_hours: int = Field(ge=1, le=MAX_DEPLOYMENT_HOURS)
    channel: str = Field(default="gpo")
    note: str = Field(default="", max_length=200)
    #: The site the machines land in, and with it their client (MSP clients
    #: spec §1.5): the token this mints is that site's, like every enrollment
    #: token.
    site_id: UUID = Field(
        description="The site the machines land in; their client is the site's client"
    )


class DeploymentTokenOut(BaseModel):
    """One bounded, multi-use enrolment credential, shown exactly once."""

    token: str
    token_id: UUID
    token_expires_at: datetime
    max_uses: int
    channel: str
    #: What the holder gains if the distribution channel leaks it. Worded
    #: server-side and per channel: "who can read this" differs by an order of
    #: magnitude between GPO and Intune, and that difference is the operator's
    #: whole decision.
    blast_radius: list[str]


class VersionUnpublishedOut(BaseModel):
    """What removing a whole version did."""

    version: str
    #: Catalogue rows removed, one per platform.
    removed: int
    #: Completed or cancelled rollout targets whose link to a removed row was
    #: cleared; their history keeps the version numbers.
    history_cleared: int
    #: Stored files that could not be deleted after the rows were. The rows are
    #: gone either way; these are bytes left in object storage to clear by hand.
    files_not_removed: list[str]

