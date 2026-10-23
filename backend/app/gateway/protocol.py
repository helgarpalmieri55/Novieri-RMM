"""Agent wire protocol v1 (docs/05-agent-spec.md).

These Pydantic models ARE the contract between the control plane and the Go
agent in `agent/`. scripts/export_wire_schema.py exports them as JSON Schema to
docs/kernel/wire-protocol/, and the agent's own tests read those files to assert
its structs against them. Unknown message kinds and unknown versions are
rejected at the parse layer — nothing unrecognized reaches business logic.

**ONE REPOSITORY IS NOT ONE DEPLOYMENT.** The agent used to live in a separate
`it-suite-agent` repo, and losing that separation makes it *possible* to change
both sides in one commit and *tempting* to treat this envelope as an internal
call. It is not one. A running fleet is always mixed-version: an agent in the
field is older than the control plane it talks to and updates on its own
schedule, so every change here is a compatibility decision — additive, or
versioned, or it breaks deployed agents. A field a new control plane requires
and an old agent does not send is an outage, and this test suite will not show
it to you. See CLAUDE.md, "the agent moved in-tree".
"""

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter

from app.core.storage import _safe_object_suffix


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    v: Literal[1]


class JobPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    type: str
    device: str  # must match the receiving agent's enrolled device identity
    params: dict[str, Any]
    issued_at: str  # RFC3339
    expires_at: str  # RFC3339
    nonce: str


class JobEnvelope(_Msg):
    """server -> agent: a signed job. `sig` covers canonical_bytes(job)."""

    kind: Literal["job"]
    job: JobPayload
    sig: str


class JobStatusMsg(_Msg):
    """agent -> server: state transition (running)."""

    kind: Literal["job_status"]
    job_id: str
    state: Literal["running"]


class JobLogMsg(_Msg):
    kind: Literal["job_log"]
    job_id: str
    level: Literal["debug", "info", "warn", "error"] = "info"
    msg: str


class JobResultMsg(_Msg):
    kind: Literal["job_result"]
    job_id: str
    state: Literal["succeeded", "failed", "refused"]
    result: dict[str, Any] = Field(default_factory=dict)
    artifact_ref: str | None = None


class HeartbeatMsg(_Msg):
    kind: Literal["heartbeat"]
    ts: str


# Normalized security-event taxonomy (docs/modules/security-logs/spec.md §2).
# The agent maps OS-native events to these; the control plane accepts no others.
SecurityEventType = Literal[
    "logon_success",
    "logon_failure",
    "logoff",
    "account_lockout",
    "privilege_elevation",
    "admin_group_change",
    "policy_change",
]


class SecurityEventDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_uid: str = Field(min_length=1, max_length=64)  # agent-side idempotency key
    ts: str  # RFC3339
    event_type: SecurityEventType
    username: str = Field(default="", max_length=200)
    source: str = Field(default="", max_length=100)  # OS-native channel/id, e.g. win:4624
    outcome: Literal["success", "failure", "n_a"] = "n_a"
    raw: dict[str, Any] = Field(default_factory=dict)


class SecurityEventsMsg(_Msg):
    """agent -> server: buffered security events (at-least-once; server dedupes
    on (device, event_uid))."""

    kind: Literal["security_events"]
    events: list[SecurityEventDoc] = Field(min_length=1, max_length=500)


class HealthReportLine(BaseModel):
    """One threshold edge, or one periodic summary, for one check.

    Six scalars and nothing else. There is no sample array, no window, no
    rolling average and no "last N values" — a series on this wire is a series
    a consumer can retain, which is the boundary
    docs/modules/endpoint-health-alerting/spec.md §0.12 exists to hold.
    `value` is a STRING (§2.4): a `service.state` check observes "stopped", a
    `disk.free_pct` check observes "3.1", and typing the column numerically is
    the first step toward someone aggregating it over time.
    """

    model_config = ConfigDict(extra="forbid")
    check_id: str
    # A service name, a mount point — bounded by `health_check`'s own
    # validation server-side. Never a full file path (§5.1).
    selector: str = Field(default="", max_length=200)
    kind: Literal["breach", "recovery", "summary"]
    # `unavailable` is a real third state, distinct from both `ok` and
    # `breaching` (§0.7): absent is not healthy and is not breached.
    state: Literal["ok", "breaching", "unavailable"]
    value: str = Field(default="", max_length=64)
    observed_at: str  # RFC3339


class HealthReportMsg(_Msg):
    """agent -> server: threshold edges and periodic summaries (at-least-once).

    Carries no device identity: the binding comes from the mTLS connection, and
    `handle_agent_message` resolves `device_id`/`org_id` from it and audits
    mismatches. A `device` field here would be a second, forgeable source of
    the same fact.

    Carries no sample series — see `HealthReportLine`, spec §0.12 and §5.1's
    wire rule.

    `reports` is bounded at 64 because a profile is capped at 32 checks
    (§0.11), so a full summary plus edges fits in one message; an agent with
    more to say sends more messages rather than one unbounded one.
    Re-delivery after a reconnect is expected and is not deduplicated here: a
    re-delivered health edge is not a distinct fact the way a security event
    is, so there is no `event_uid`.
    """

    kind: Literal["health_report"]
    # The policy the agent believes it is running (§2.11). A missing
    # acknowledgement reads as 0, so this can never be negative.
    policy_version: int = Field(ge=0)
    reports: list[HealthReportLine] = Field(min_length=1, max_length=64)


# --------------------------------------------------------------------------- #
# Backup dedup exchange (docs/modules/backup/spec.md §5.2)
# --------------------------------------------------------------------------- #

# A BLAKE3-256 chunk id as hex, and nothing else. THIS IS A KEY-SAFETY CONTROL:
# a chunk id is concatenated into an object key
# (`{storage_ref}/chunks/{id[:2]}/{id}`) and rides into a presigned write grant,
# so an id containing `..`, a slash or a backslash would address an object
# outside the repository on any layer between here and the bucket that
# normalises a path. Rejected at the wire, again by the `backup_chunks` CHECK,
# and again by `_safe_object_suffix` — none of the three is the only layer.
CHUNK_ID_PATTERN = r"^[0-9a-f]{64}$"

# THE BATCH BOUND, STATED RATHER THAN EMERGENT. A backup run produces six
# figures of chunks; at the pinned 1 MiB average that is a terabyte protected.
# Neither side may put that on one frame, so the agent splits its hash list into
# batches of at most this many and the server refuses a larger one at the parse
# layer. 1024 * 65 bytes is a ~66 KB request and a response no larger, which is
# an ordinary WebSocket frame; the round-trip count it implies (one per 1024
# chunks, ~1 GiB protected) is the thing this exchange exists to bound, down
# from one per chunk.
MAX_CHUNK_BATCH = 1024

# The pattern belongs to the ITEM, not the list: `Field(pattern=...)` on a
# `list[str]` constrains nothing at all, which is the quiet way this control
# would have been decoration.
ChunkId = Annotated[str, Field(pattern=CHUNK_ID_PATTERN)]


class BackupChunkQueryMsg(_Msg):
    """agent -> server: "which of these chunks does my repository already
    have, and may I write the ones it does not?"

    Carries NO repository id and NO storage prefix. `job_id` is the only thing
    the agent names, and the server resolves (org, device, repository, prefix)
    from the job IT signed — the key-is-derived-never-accepted rule that
    `app.core.storage`'s presign helpers are built on, applied to the tenancy
    question instead of the key question. An agent that named its own
    repository could name someone else's.

    `stored` is this agent reporting chunks it has successfully uploaded since
    its last query; the server records them in the index under the repository
    it resolved. `check` is the list to partition. Both are bounded by
    `MAX_CHUNK_BATCH` at the parse layer, so a malformed or hostile agent
    cannot make one frame arbitrarily large.

    Compatibility: strictly additive. An older control plane rejects this kind
    at `parse_agent_message` and logs it; the agent's run then fails on the
    unanswered query, which is no worse than the refusal it does today. An
    older agent never sends it and is never sent a `backup_chunk_grant`.
    """

    kind: Literal["backup_chunk_query"]
    job_id: str
    # Opaque agent-minted correlation token, echoed back verbatim. It exists
    # because one job issues many queries and the responses arrive on the same
    # connection as everything else.
    query_id: str = Field(min_length=1, max_length=64)
    check: list[ChunkId] = Field(default_factory=list, max_length=MAX_CHUNK_BATCH)
    stored: list[ChunkId] = Field(default_factory=list, max_length=MAX_CHUNK_BATCH)


class BackupUploadGrant(BaseModel):
    """An S3 POST policy authorising writes under one repository's prefix.

    Minted by `ObjectStore.presign_backup_write` and passed through verbatim.
    `url` + `fields` are the signed form the endpoint POSTs to; `key_prefix`
    and `max_bytes` restate the two conditions signed INTO the policy so the
    agent can check its own key before spending a round trip on a write S3
    will reject. Restating them is not enforcement — S3 enforces the signed
    document, and nothing the agent believes about these two values changes
    what it will accept.

    It is a bearer credential for its lifetime: never logged, never audited,
    never returned by an API route.
    """

    model_config = ConfigDict(extra="forbid")
    url: str
    fields: dict[str, str]
    key_prefix: str
    max_bytes: int


class BackupChunkGrantMsg(_Msg):
    """server -> agent: the dedup answer and the write grant for it.

    `new` is the subset of the query's `check` that this repository does NOT
    already hold, in the order it was asked. Everything not in `new` is
    deduplicated: the agent must not upload it and must still reference it from
    the manifest.

    The grant covers `{storage_ref}/`, which is every write the run performs —
    the new chunks AND the manifest object, whose name the agent mints. It is
    NOT scoped to the `new` keys individually: an S3 POST policy can carry a
    `starts-with $key` condition, and per-key conditions would need a separate
    signed document per chunk. `max_bytes` is what bounds the damage a
    compromised endpoint can do with it.

    There is deliberately NO error variant. Every authorization failure —
    unknown job, a job targeting another device, a job that is not a
    `backup.run`, an expired job, a run that has already finished — produces
    SILENCE plus an audit event, never a distinguishable reply. A reply that
    said "no such job" and a reply that said "not your job" would together be
    an oracle a compromised endpoint could walk.
    """

    kind: Literal["backup_chunk_grant"]
    job_id: str
    query_id: str
    new: list[ChunkId] = Field(default_factory=list, max_length=MAX_CHUNK_BATCH)
    # How many of the query's `stored` ids were newly recorded in the index.
    # A count, never the ids: this rides no further than the agent, but the
    # same message shape is what a future relay would log.
    recorded: int = Field(ge=0)
    upload: BackupUploadGrant
    expires_at: str  # RFC3339; the grant's, which is the job's or sooner


# --------------------------------------------------------------------------- #
# Backup READ exchange (docs/superpowers/specs/2026-08-13-prefix-scoped-read-
# design.md). The write half above authorizes a PREFIX with one POST policy;
# SigV4 query signing cannot express that for a READ, so reads are signed one
# object at a time and batched instead.
# --------------------------------------------------------------------------- #

# A repository-RELATIVE object suffix, and nothing else. THIS IS A KEY-SAFETY
# CONTROL: a suffix is joined onto the prefix the server derives from the job,
# so a `..` segment, a leading slash, a backslash, an empty segment or a control
# character would address an object outside the repository on any layer between
# here and the bucket that normalises a path.
#
# THE VALIDATOR IS `_safe_object_suffix` ITSELF, not a regex restating it.
# `Field(pattern=...)` would be the obvious move and is the wrong one twice
# over: pydantic-core's default regex engine has no lookahead, so the `..` and
# empty-segment rules cannot be expressed as one pattern at all; and a second
# statement of the alphabet is a second thing to drift. `dispatch.py` records
# what drift costs here -- a hand-rolled equivalent silently omitted the
# control-character check, so `manifests/x\ty` would have reached an S3 key.
#
# Applied at the wire is the FIRST of three layers, not the only one:
# `presign_backup_read` runs the same validator again before signing, and the
# object store's own key handling is the third.
def _wire_safe_suffix(value: str) -> str:
    _safe_object_suffix(value)
    return value


# THE READ BATCH BOUND, and deliberately NOT the write side's 1024. The write
# exchange moves no bytes, so its batch size is free; here every grant is ~1 MiB
# to fetch, and 1024 keys is ~1 GiB per batch -- long enough on a slow link that
# the URLs expire mid-batch and the restore thrashes. 256 keys is ~256 MiB,
# about 3.5 minutes on a 10 Mbps link inside a 15-minute window. Erring small is
# safe because re-requesting is cheap; erring large strands work.
READ_BATCH_MAX = 256

ObjectSuffix = Annotated[str, Field(max_length=512), AfterValidator(_wire_safe_suffix)]


class BackupReadQueryMsg(_Msg):
    """agent -> server: "sign me a GET for each of these objects in my
    repository."

    Carries NO repository id, NO storage prefix and NO keys -- only SUFFIXES.
    The server joins each onto the prefix it derives from the job IT signed, so
    an agent cannot express a location outside its own repository: the boundary
    is a property of the construction, not of remembering to validate.

    Compatibility: strictly additive. An older control plane rejects this kind
    at `parse_agent_message`; the agent's restore then fails on the unanswered
    query with a stated reason, which is no worse than the refusal `Get` and
    `GetRef` return today.
    """

    kind: Literal["backup_read_query"]
    job_id: str
    # Opaque agent-minted correlation token, echoed back verbatim. One restore
    # issues many batches, and the replies arrive on the same connection as
    # everything else.
    query_id: str = Field(min_length=1, max_length=64)
    suffixes: list[ObjectSuffix] = Field(min_length=1, max_length=READ_BATCH_MAX)


class BackupReadGrantItem(BaseModel):
    """One signed GET. A bearer credential for its lifetime: never logged,
    never audited, never returned by an API route."""

    model_config = ConfigDict(extra="forbid")
    suffix: str
    url: str


class BackupReadGrantMsg(_Msg):
    """server -> agent: one short-lived signed GET per requested suffix, in the
    order asked.

    There is deliberately NO error variant, for the reason
    `BackupChunkGrantMsg` states: a reply that distinguished "no such job" from
    "not your job" would be an oracle a compromised endpoint could walk. Every
    authorization refusal is silence plus an audit record. A suffix or batch
    size the agent should never have sent is refused a layer earlier still, by
    `BackupReadQueryMsg`'s own validation, so it reaches no handler.
    """

    kind: Literal["backup_read_grant"]
    job_id: str
    query_id: str
    grants: list[BackupReadGrantItem] = Field(default_factory=list, max_length=READ_BATCH_MAX)
    expires_at: str  # RFC3339; the grant's, which is the job's or sooner


AgentMessage = Annotated[
    JobStatusMsg
    | JobLogMsg
    | JobResultMsg
    | HeartbeatMsg
    | SecurityEventsMsg
    | HealthReportMsg
    | BackupChunkQueryMsg
    | BackupReadQueryMsg,
    Field(discriminator="kind"),
]

_agent_message_adapter: TypeAdapter[AgentMessage] = TypeAdapter(AgentMessage)


def parse_agent_message(raw: str | bytes) -> AgentMessage:
    return _agent_message_adapter.validate_json(raw)
