"""The two tables cycle B1 adds
(`docs/modules/agent-distribution/spec-push-install.md` §3, §6.3, §14).

**NEITHER TABLE GRANTS ANYTHING ON ITS OWN, AND THAT IS THE DESIGN.** A push
install is refused unless THREE independent authorizations hold at once
(`CLAUDE.md` invariant 2a clause (d)) -- a promoted asset row, an active
`push_authorization` covering the address, and a `push_credential_scope`
binding the chosen credential to that address. These are two of the three;
`devices` already holds the third. They are separate tables on purpose, because
each answers a different question and collapsing any two loses the question
underneath: *may this platform present administrative credentials inside this
prefix at all*, and *may THIS credential be presented at THIS address*.

**MODELLED ON `ipam_scan_authorization`, DELIBERATELY NOT REUSING IT** (spec
§3, D9). Authorizing an ICMP echo is not authorizing an administrative login.
An operator who authorized a sweep of `10.0.0.0/8` last month has not consented
to credentialed logins across sixteen million addresses, and a design that read
their scan grant that way would be putting words in their mouth. What IS
copied, column for column, is the shape that makes such a grant reviewable:

* deny by default -- no row covering an address means no push to it, ever;
* prefix-scoped, in native `cidr` so containment is `>>=` and not a string
  comparison that is wrong at every boundary (`10.200.7.5` starts with `10.20`);
* `authorized_by` / `bound_by` NOT NULL -- there is no system-authorized push
  scope, and a human put their name on every administrative login this platform
  will ever cause;
* rows are NEVER hard-deleted, so "who authorized presenting credentials on
  that segment in March" stays answerable: revocation stamps
  `revoked_at`/`revoked_by`;
* the active-uniqueness index is therefore PARTIAL on `revoked_at IS NULL`,
  which is what lets a revoked prefix be authorized again -- and which means
  every upsert against it must INFER (`index_elements=` + `index_where=`),
  because `ON CONFLICT ON CONSTRAINT` fails at runtime against a partial index
  that has no `pg_constraint` row to name.

**`expires_at` EXISTS ON THE AUTHORIZATION AND NOT ON THE CREDENTIAL SCOPE**,
and the asymmetry is deliberate rather than an omission. An authorization is an
answer to "may we do this here, for now" -- an onboarding window, a migration
weekend -- and lapsing is the safe end of it. A credential scope is an answer
to "where may this secret ever be presented", which is a property of the
secret, not of a window; a scope that lapsed silently would leave a credential
that had been dedicated to a prefix bound to nothing, and the refusal an
operator then sees would name the wrong problem.

**EVERY COLUMN HERE MUST MATCH MIGRATION 0077 EXACTLY** in type, length and
nullability, and every index declared here must be declared there:
`tests/test_schema_drift.py` has two tests and the second makes Postgres
deparse the indexes on both sides, so a predicate written differently in the
two places is drift even when it means the same thing.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    FetchedValue,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import CIDR, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, client_org_fk, uuid7_pk

__all__ = [
    "PushAuthorization",
    "PushCredentialScope",
    "PushHostKey",
    "PushBatchTarget",
    "PushRun",
    "PushRunTarget",
    "PUSH_RUN_STATES",
    "PUSH_RUN_ACTIVE_STATES",
    "PUSH_RUN_TARGET_STATES",
]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PushAuthorization(Base):
    """A human's signature on every administrative login this platform causes.

    Invariant 2a clause (d), authorization two of three. DENY BY DEFAULT: no
    active row covering an address means a push to that address is refused,
    recorded, and no signature is ever produced for it.

    **NOT IMPLIED BY AN `ipam_scan_authorization`, AND NOT SATISFIED BY ONE.**
    The two tables are read by different code for different acts. See the
    module docstring.
    """

    __tablename__ = "push_authorization"
    __table_args__ = (
        Index(
            "uq_push_authorization_active",
            "org_id",
            "prefix",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index(
            "ix_push_authorization_gist",
            "prefix",
            postgresql_using="gist",
            postgresql_ops={"prefix": "inet_ops"},
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    prefix: Mapped[str] = mapped_column(CIDR)
    note: Mapped[str] = mapped_column(String(500), server_default="", default="")
    #: NOT NULL: there is no system-authorized push scope (spec §3).
    authorized_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    authorized_at: Mapped[datetime] = mapped_column(default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)


class PushCredentialScope(Base):
    """Where one stored credential may ever be presented (spec §6.3).

    Invariant 2a clause (d), authorization three of three, and the direct
    answer to *how does one credential not get sprayed at the whole estate*:
    **the credential itself carries where it may be presented.**

    **`credentials.device_group_id` CANNOT CARRY THIS.** That column scopes a
    credential to DEVICES; a push target is not yet a managed device and may be
    in no group at all. Prefix scoping is the only expression of "this secret
    may be shown at this address" that exists before the machine is managed.

    **THE FK IS TO `credentials`, AND THE SERVICE REFUSES ANY TYPE BUT
    `push_install`** (spec §6.2). The database cannot express "a credential of
    this type only" without duplicating the type column here, which would be a
    second copy of a fact that can then disagree with the first. The refusal
    lives in `credential_scope.bind_scope`, is checked on every read path that
    resolves a credential for a push, and is tested in both directions.
    """

    __tablename__ = "push_credential_scope"
    __table_args__ = (
        Index(
            "uq_push_credential_scope_active",
            "org_id",
            "credential_id",
            "prefix",
            unique=True,
            postgresql_where=text("unbound_at IS NULL"),
        ),
        Index(
            "ix_push_credential_scope_gist",
            "prefix",
            postgresql_using="gist",
            postgresql_ops={"prefix": "inet_ops"},
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    credential_id: Mapped[UUID] = mapped_column(ForeignKey("credentials.id"))
    prefix: Mapped[str] = mapped_column(CIDR)
    note: Mapped[str] = mapped_column(String(500), server_default="", default="")
    #: NOT NULL, for the same reason `authorized_by` is: a human decided this
    #: secret may be presented here.
    bound_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    bound_at: Mapped[datetime] = mapped_column(default=_utcnow)
    #: Unbinding STAMPS; the row stays. "Which addresses was this credential
    #: bound to when it was used in March" must stay answerable after somebody
    #: narrows the scope.
    unbound_at: Mapped[datetime | None] = mapped_column(default=None)
    unbound_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)


class PushHostKey(Base):
    """A target's SSH host key, observed by a credential-free probe and confirmed
    by a human before any credential is presented (spec §4.4, §7.3).

    **THIS TABLE IS THE ONLY THING STANDING BETWEEN A PUSH AND TRUST-ON-FIRST-USE,
    AND THAT IS THE CRUX OF THE WHOLE FEATURE.** On first contact the platform
    cannot authenticate the target -- there is no prior relationship, which is
    what "unmanaged" means (spec §1.1, §7.3). So the fingerprint is fetched
    first by `agent.push_probe`, which carries no credential; the operator
    confirms it in the console; and only a CONFIRMED row here lets a push be
    dispatched, with the confirmed key signed into the install params so the
    agent's SSH client verifies against it rather than accepting blindly.

    Two consequences the spec makes requirements (§4.4, falsifier 21):

    * A push to a host with NO confirmed row is REFUSED, not prompted.
    * A push to a host whose observed key later DIFFERS from the confirmed one
      is REFUSED, not warned about. `confirmed_fingerprint` is what an install
      is verified against; a new `observed_fingerprint` that disagrees is a
      change the operator must re-confirm deliberately.

    **`address` IS `inet`, NOT `text`.** The lookup that gates an install joins
    on the target address; a string comparison would be a second, wrong answer
    to "is this the same host" at every representation boundary.
    """

    __tablename__ = "push_host_key"
    __table_args__ = (
        client_org_fk("push_host_key"),
        # One host-key record per (org, client, address): a target has one host
        # key at a time, and re-probing UPDATES the observed half rather than
        # growing rows. PER CLIENT (migration 0231, Task 8b): an address names a
        # host only within one client's network -- two clients' 10.0.0.5 are
        # two machines. NULLS NOT DISTINCT: one global row (no device) per
        # address, which may share it with a client's.
        Index(
            "uq_push_host_key_client_address",
            "org_id",
            "client_id",
            "address",
            unique=True,
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (O): NULL is every client. While
    # device_id is set, trigger its_client_from_parent copies its client
    # (migration 0231), so the value is read back after the INSERT.
    client_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    #: The promoted asset this key belongs to, resolved at probe time. Nullable
    #: because a probe records what it saw even if the asset row is later removed;
    #: an install re-resolves the asset through the refusal ladder regardless.
    device_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("devices.id"), default=None
    )
    address: Mapped[str] = mapped_column(INET)
    transport: Mapped[str] = mapped_column(String(16), default="ssh")
    #: What the most recent probe saw. Updated on every probe.
    observed_fingerprint: Mapped[str] = mapped_column(String(128))
    observed_key_type: Mapped[str] = mapped_column(String(64), default="")
    observed_at: Mapped[datetime] = mapped_column(default=_utcnow)
    observed_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("jobs.id"), default=None
    )
    #: NULL until a human confirms. An install verifies against THIS, never
    #: against `observed_fingerprint` -- the operator's confirmation is the
    #: trust anchor, and the observation alone is not one.
    confirmed_fingerprint: Mapped[str | None] = mapped_column(
        String(128), default=None
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(default=None)
    confirmed_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), default=None
    )
    #: WinRM readiness (design §2.4): {"winrm_https": bool, "winrm_http": bool,
    #: "kdc_reachable": bool} -- exactly what `dispatch.record_probe_result`
    #: writes and `HostKeyOut` exposes. NULL for ssh rows and for
    #: any host never probed over winrm. The install path does NOT gate on this
    #: (Kerberos authenticates the target); it drives the readiness panel.
    readiness: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)


class PushBatchTarget(Base):
    """One dispatched (address, job) pair, grouped by `batch_id` for display
    (spec §5.3). **HOLDS NO AGGREGATE STATE THAT COULD HIDE A FAILURE.**

    N targets are N independent jobs, each with its own `jobs` row, terminal
    state, result and audit event; this table only records which push act they
    belonged to, so the console can show "these twenty-five were one operator's
    one click" without inventing a batch-level "succeeded" that a partial
    failure would make a lie. The kernel `jobs` table cannot carry a `batch_id`
    column (it is not this module's to change), so the grouping lives here.
    """

    __tablename__ = "push_batch_target"
    __table_args__ = (
        Index("ix_push_batch_target_batch", "org_id", "batch_id"),
        Index("ix_push_batch_target_job", "job_id", unique=True),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the jobs row named by job_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of the fk lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )
    #: A uuid minted per push request. Not a FK: there is no batch-header row,
    #: because there is no batch-level state worth a row (see the docstring).
    batch_id: Mapped[UUID] = mapped_column()
    #: `probe` or `install`.
    kind: Mapped[str] = mapped_column(String(16))
    address: Mapped[str] = mapped_column(INET)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"))
    device_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("devices.id"), default=None
    )
    site_server_device_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("devices.id"), default=None
    )
    credential_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("credentials.id"), default=None
    )
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class PushCredentialRealm(Base):
    """The Kerberos realm a Windows domain push credential authenticates in
    (review I1, 2026-09-29; migration 0190).

    **THE REALM IS THE CREDENTIAL'S, NEVER THE TARGET'S.** It used to be derived
    from each target's DNS name -- a column inventory can write -- and the KDC
    that received the account's pre-authentication was then found by DNS SRV
    for that name: whoever could write a device's `fqdn` chose which "domain
    controller" a Domain-Admin-capable password was proven to. Now the realm is
    the upper-cased domain of the account's user principal name, recorded here
    when the credential is SAVED (the one moment its plaintext is in hand), so
    that dispatch can refuse a target outside it BEFORE the credential is
    decrypted.

    Not a secret: a DNS domain. One row per `windows_domain` credential,
    replaced on every rotation; the FK cascades with the credential.
    """

    __tablename__ = "push_credential_realm"

    credential_id: Mapped[UUID] = mapped_column(
        ForeignKey("credentials.id", ondelete="CASCADE"), primary_key=True
    )
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    realm: Mapped[str] = mapped_column(String(253))
    recorded_at: Mapped[datetime] = mapped_column(default=_utcnow)


#: A run's lifecycle (design §1.4a, §1.5). `running`, `paused` and
#: `stopped_credential` are ACTIVE; at most one active run per org
#: (`uq_push_run_active`). `stopped_credential` is the pacer's safety stop after
#: repeated sign-in failures, and only a person's Resume leaves it. `cancelled`
#: is final.
PUSH_RUN_STATES = ("running", "paused", "stopped_credential", "cancelled", "completed")
PUSH_RUN_ACTIVE_STATES = ("running", "paused", "stopped_credential")
#: One confirmed machine's progress. `in_flight` holds a signed job; the four
#: after it are terminal; `waiting` becomes `cancelled` only by a person's Cancel.
PUSH_RUN_TARGET_STATES = ("waiting", "in_flight", "installed", "failed", "refused", "cancelled")


class PushRun(Base):
    """One subnet push a person confirmed (design §1.3; CLAUDE.md 2a(f) as
    amended 2026-09-30).

    **THE LIST IS THE AUTHORITY, NOT THE SUBNET.** `prefix` records what built
    the list; `list_digest` is the SHA-256 of the ready `(device_id, address)`
    pairs the person was shown and confirmed (`run_list.compute_list_digest`).
    The targets are written once, in the transaction that sets `sealed_at`, and
    the `push_run_target_fixed` trigger refuses any later insert.

    `credential_id` is `ON DELETE SET NULL`: a credential that disappears
    mid-run leaves the run readable, and the pacer refuses every remaining
    target as `credential_missing`. That is the only change the database
    allows to it; `prefix`, `list_digest`, `target_count`, `created_by`,
    `org_id` and, once set, `sealed_at` never change (`push_run_fixed`,
    migration 0200). The per-state counts are computed on read, never stored.
    """

    __tablename__ = "push_run"
    __table_args__ = (
        CheckConstraint(
            "state IN ('running', 'paused', 'stopped_credential', 'cancelled', 'completed')",
            name="ck_push_run_state",
        ),
        Index(
            "uq_push_run_active",
            "org_id",
            unique=True,
            postgresql_where=text("state IN ('running', 'paused', 'stopped_credential')"),
        ),
        Index("ix_push_run_org_created", "org_id", "created_at"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    #: The person who confirmed the list. NOT NULL: no run starts without one.
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    credential_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), default=None
    )
    prefix: Mapped[str] = mapped_column(CIDR)
    list_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="running")
    target_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    sealed_at: Mapped[datetime | None] = mapped_column(default=None)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)
    #: The lockout guard's window (spec §1.4a): when the run last started or
    #: resumed. `auth_failed` targets and proven sign-ins count only when they
    #: were DISPATCHED at or after this, so an install sent before a stop that
    #: reports after the Resume counts in neither direction. `create_run` and
    #: `resume_run` set it; `resume_run` always sets it after every
    #: `dispatched_at` the run already holds.
    auth_window_started_at: Mapped[datetime] = mapped_column(default=_utcnow)
    #: When the pacer's safety stop fired. Cleared by a person's Resume.
    stopped_at: Mapped[datetime | None] = mapped_column(default=None)


class PushRunTarget(Base):
    """One confirmed machine in a run (design §1.3).

    `device_id` and `address` are what the person confirmed. The pacer refuses
    a target whose address now names another device (`target_changed`), and the
    database refuses any change to `address` or `position`, and any change to
    `device_id` but to NULL (`push_run_target_fixed`, migration 0200). `reasons` holds the
    named refusals when `state == 'refused'`; `outcome`/`detail` hold what the
    job reported once it was dispatched.
    """

    __tablename__ = "push_run_target"
    __table_args__ = (
        CheckConstraint(
            "state IN ('waiting', 'in_flight', 'installed', 'failed', 'refused', 'cancelled')",
            name="ck_push_run_target_state",
        ),
        Index("uq_push_run_target_position", "run_id", "position", unique=True),
        Index("uq_push_run_target_address", "run_id", "address", unique=True),
        Index("ix_push_run_target_state", "org_id", "run_id", "state"),
        Index("ix_push_run_target_job", "job_id"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    run_id: Mapped[UUID] = mapped_column(ForeignKey("push_run.id"))
    device_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), default=None
    )
    address: Mapped[str] = mapped_column(INET)
    position: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="waiting")
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.id"), default=None)
    outcome: Mapped[str] = mapped_column(String(32), server_default="", default="")
    detail: Mapped[str] = mapped_column(String(500), server_default="", default="")
    reasons: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), default=list
    )
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow)
    #: When the job's outcome was recorded.
    settled_at: Mapped[datetime | None] = mapped_column(default=None)
    #: When the pacer dispatched it: the lockout guard's clock (spec §1.4a).
    #: Never before the run's `auth_window_started_at` at that moment.
    dispatched_at: Mapped[datetime | None] = mapped_column(default=None)
    #: WHICH STORED VERSION OF THE SECRET the job carried: the dispatcher's
    #: opaque `credential_fingerprint` (a SHA-256 over the vault envelope, never
    #: the plaintext). The guard counts a sign-in as proof only for the version
    #: that is still current (spec §1.4a). Never returned by the API, never
    #: audited, never logged.
    credential_fingerprint: Mapped[str | None] = mapped_column(String(64), default=None)
    #: The outcome proves the credential got past sign-in on the target (R19).
    signed_in: Mapped[bool] = mapped_column(Boolean, server_default=text("false"), default=False)
