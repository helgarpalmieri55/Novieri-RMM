"""Kernel AI models (docs/modules/ai-automation.md §4.1, §5.1).

WHY THESE LIVE HERE AND NOT UNDER `app/core/ai/`. The same forced split
`app/models/approved_actions.py` documents: a declarative model defined under
`app/core/` cannot be registered in `app/models/__init__.py` without a genuine
import cycle, because `app/core/...` needs `app.models.base.Base`, importing
which executes `app/models/__init__.py`. So the behaviour lives in
`app/core/ai/` and the declarative classes sit beside the other kernel models.

`ai_autonomy_ceiling` IS THE DIAL, AND ITS VOCABULARY STOPS ONE SHORT.
`level IN ('off','suggest','require_approval')` -- there is NO `'autonomous'`
value at all (§0.4). That is a database CHECK rather than application logic
precisely so widening it is a migration a reviewer sees, which is the same
argument `ck_approved_action_autonomous_is_bounded` makes for its own gate. A
MISSING ROW MEANS `off`: deny by default, and the layer ships with no rows, the
way `ai_settings.enabled` ships `false`.

THE FOURTH CHECK IS A RECORDED SPEC GAP, CLOSED HERE. §3.1 says the `end_user`
persona may never "be raised above `off` ... for any risk class", and §4.1's
table gives no constraint for it -- so it would have been application code,
which is exactly what §0.4 refuses for `autonomous`.
`ck_ai_autonomy_ceiling_end_user_is_off` makes it the database's answer.
Recorded back into spec §4.1.

THE FOUR TRANSCRIPT TABLES MOVED HERE from `app/modules/ai_admin/models.py`
(§0.1, §2.1). Table names did not change, so there is no migration. They had to
move: `app/core/ai/store.py` reads them, and a kernel module importing the old
`app.modules.ai_admin.models` path would fail §2.3 property 1 -- the assertion
that makes the relocation mean anything. Their own three non-ordinary-DDL notes
survive on the classes below.

Every column here must match the migration that created it -- `0045_ai_admin.py`
for the four transcript tables, `0048_ai_autonomy_ceiling.py` for the dial --
exactly in type, length and nullability, and every index must be declared in
`__table_args__` as well as in the migration:
`tests/test_schema_drift.py::test_metadata_indexes_match_live_schema` compares
both directions, including partial-index predicates.
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
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.models.base import Base, uuid7_pk

#: The dial's settable vocabulary, in the order the CHECK states it. `autonomous`
#: is deliberately absent -- see the module docstring and §0.4. The RESOLVER's
#: ordered vocabulary (`app/core/ai/autonomy.py::LEVELS`) is one longer, because
#: `approved_action.autonomy` may be `autonomous` and the resolver's job is to
#: reduce it away.
CEILING_LEVELS = ("off", "suggest", "require_approval")

#: The personas this layer knows. `'requester'` is deliberately not one of them:
#: the portal principal's persona name is `end_user`, and two names for one
#: persona is how a ceiling gets set on a row nothing reads.
AI_PERSONAS = ("end_user", "technician", "admin")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AiAutonomyCeiling(Base):
    """One org's ceiling on one persona's reach into one risk class (§4.1).

    Composes with `approved_action.autonomy` by taking the MINIMUM, so the dial
    can only ever LOWER what the catalogue row already permits. The catalogue
    row is the ceiling on the action; this is the ceiling on the persona.
    """

    __tablename__ = "ai_autonomy_ceiling"
    __table_args__ = (
        CheckConstraint(
            "persona IN ('end_user', 'technician', 'admin')",
            name="ck_ai_autonomy_ceiling_persona",
        ),
        CheckConstraint(
            "risk_class IN ('low', 'medium', 'high')",
            name="ck_ai_autonomy_ceiling_risk_class",
        ),
        # §0.4: the AI personas' vocabulary stops one short of the catalogue's.
        CheckConstraint(
            "level IN ('off', 'suggest', 'require_approval')",
            name="ck_ai_autonomy_ceiling_level",
        ),
        # §3.1, recorded into §4.1 by this build: the resolver reads the most
        # hostile text on the platform and proposes nothing, at any position.
        CheckConstraint(
            "NOT (persona = 'end_user' AND level <> 'off')",
            name="ck_ai_autonomy_ceiling_end_user_is_off",
        ),
    )

    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    persona: Mapped[str] = mapped_column(String(16), primary_key=True)
    risk_class: Mapped[str] = mapped_column(String(8), primary_key=True)
    level: Mapped[str] = mapped_column(String(20))
    updated_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class AiSession(Base):
    """One conversation, pinned to one org at creation (§3.1, §0.9).

    `persona` and `mode` exist from day one, and both CHECKs were widened by a
    REVIEWED migration (0048) exactly as §12.2 anticipated -- a widening you
    have to write a migration for is a widening someone reads. The value that
    shipped for act mode is `'act'`, not the `'read_act'` this docstring used
    to predict: `docs/modules/ai-automation.md` §0.8 fixes `mode` at creation
    and gives it no promotion path, so a combined value would describe a
    session that cannot exist.

    `title` is generated from the first question by truncation, never by a
    second model call -- naming a conversation is cost with no user value.
    """

    __tablename__ = "ai_session"
    __table_args__ = (
        # Widened by 0048 (docs/modules/ai-automation.md §5.3, §10.3). Still
        # refused: `'requester'` is not a persona of this layer, and
        # `'read_act'` -- the value this file's own docstring anticipated -- is
        # not the value that shipped, because §0.8 fixes `mode` AT CREATION and
        # a session that could be promoted would import a read-mode transcript
        # into a context that can propose.
        CheckConstraint(
            "persona IN ('admin', 'technician', 'end_user')", name="ck_ai_session_persona"
        ),
        CheckConstraint("mode IN ('read', 'act')", name="ck_ai_session_mode"),
        Index(
            "ix_ai_session_org_user_recent",
            "org_id",
            "user_id",
            text("last_turn_at DESC"),
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    persona: Mapped[str] = mapped_column(String(16), server_default="admin", default="admin")
    mode: Mapped[str] = mapped_column(String(8), server_default="read", default="read")

    @validates("mode", "persona")
    def _fixed_at_creation(self, key: str, value: str) -> str:
        """**NEITHER `mode` NOR `persona` MAY BE CHANGED ONCE THE ROW EXISTS**
        (docs/modules/ai-automation.md §0.8).

        There is no route that accepts a mode change, and there is no service
        function that writes one -- but "no caller does this" is a property of
        every caller written so far, and the whole point of §0.8 is that the
        cheapest possible promotion path is one line in a future handler.
        Promoting a session imports a whole read-mode transcript, ASSEMBLED UNDER
        READ MODE'S REPLAY RULES (where a laundered instruction was flagged and
        replayed verbatim, because a read turn could not act on it), into a
        context that can now propose an act. `persona` is here for the same
        reason one field over: it is the key the autonomy dial, the audit events
        and every `ai_action` row are resolved against, and a session that
        changed persona would make every one of those describe a conversation
        that no longer exists.

        **THIS FIRES ONLY ON APPLICATION WRITES, NOT ON LOADS.** SQLAlchemy's
        `validates` hooks the `set` event; populating an instance from a query
        goes through the loader and does not call it, so re-reading an `act`
        session is not a promotion. What it catches is `row.mode = "act"`, which
        is the thing that would ship.
        """
        current = getattr(self, key, None)
        if current is not None and current != value:
            raise ValueError(  # i18n-exempt: ORM guard, never in a response
                f"ai_session.{key} is fixed at creation and cannot be changed "
                f"({current!r} -> {value!r}); docs/modules/ai-automation.md §0.8 gives "
                f"a session no promotion path, because promoting one imports a "
                f"read-mode transcript into a context that can propose an act"
            )
        return value
    title: Mapped[str] = mapped_column(String(200), server_default="", default="")
    # Which model served this conversation, so "what produced this answer" is
    # answerable after a provider change (§3.1).
    model_id: Mapped[str] = mapped_column(String(100), server_default="", default="")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    last_turn_at: Mapped[datetime] = mapped_column(default=_utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(default=None)
    message_count: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    total_input_tokens: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, server_default="0", default=0)


class AiMessage(Base):
    """The transcript, retained on a clock (§3.2, §0.7).

    Question and answer text live here and NOT in the audit log, because the
    audit log is append-only and hash-chained: anything written there is written
    forever and cannot be redacted. These rows are deletable BECAUSE they are
    not audit, and a retention sweep hard-deletes them.

    `tool_calls` records `[{tool, arg_hash, result_count, truncated, status}]` --
    enough to explain and audit the turn, not enough to reconstitute the data.
    """

    __tablename__ = "ai_message"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_ai_message_session_seq"),
        CheckConstraint("role IN ('user', 'assistant')", name="ck_ai_message_role"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("ai_session.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(12))
    content: Mapped[str] = mapped_column(Text, server_default="", default="")
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, default=None)
    flags: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default="{}", default=dict
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    output_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class AiInjectionFlag(Base):
    """Flagged retrieved content, for admin review (§3.3).

    One row per flagged field, so an admin can see WHICH RECORD carried
    injection-shaped text and go look at it.

    `excerpt` is the one place retrieved text is deliberately stored, and it is
    bounded by the COLUMN (`String(400)`) rather than by the caller -- the text
    is attacker-controlled and arrives through unauthenticated email intake.
    This is also why the flag is not written to the audit log: an attacker who
    can write a ticket body could otherwise write arbitrary PERMANENT content
    into the tamper-evident chain. The FACT that a flag fired is audited; the
    text is not.
    """

    __tablename__ = "ai_injection_flag"

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("ai_session.id", ondelete="CASCADE")
    )
    message_seq: Mapped[int] = mapped_column(Integer)
    source_tool: Mapped[str] = mapped_column(String(100))
    source_type: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[str] = mapped_column(String(100), server_default="", default="")
    matched_rule: Mapped[str] = mapped_column(String(40))
    excerpt: Mapped[str] = mapped_column(String(400), server_default="", default="")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)


class AiSettings(Base):
    """One row per org (§3.4). Every write requires `ai.admin` and is audited.

    **EVERY COLUMN HERE IS A COST OR RETENTION DIAL. NONE OF THEM DECIDES
    WHETHER THE ASSISTANT RUNS OR WHERE IT SENDS** (§0.14, operator decision of
    2026-08-25; migration `0074`). `enabled` and `provider_key` used to live
    here and were dropped: the deployment's `ITS_AI_*` environment is the single
    switch and the single destination, so a per-org copy of either was a value
    the console could contradict and an org-admin could point somewhere.
    """

    __tablename__ = "ai_settings"
    __table_args__ = (
        CheckConstraint(
            "transcript_retention_days BETWEEN 1 AND 365",
            name="ck_ai_settings_retention_days",
        ),
        CheckConstraint(
            "max_turns_per_user_per_minute BETWEEN 1 AND 60",
            name="ck_ai_settings_turns_per_user",
        ),
        CheckConstraint(
            "max_turns_per_org_per_hour BETWEEN 1 AND 5000",
            name="ck_ai_settings_turns_per_org",
        ),
        CheckConstraint(
            "daily_token_budget IS NULL OR daily_token_budget > 0",
            name="ck_ai_settings_token_budget",
        ),
        # §6.4 and §6.5, added by 0049. RECORDED SPEC GAP: both sections say
        # "org-configurable, CHECK-bounded" without saying where the value
        # lives; they live here, beside the four tunables that already carry
        # ceilings on this table, because every write to it is already
        # `ai.admin` and already audited.
        CheckConstraint(
            "action_expiry_s BETWEEN 60 AND 86400",
            name="ck_ai_settings_action_expiry",
        ),
        CheckConstraint(
            "max_proposals_per_turn BETWEEN 1 AND 10",
            name="ck_ai_settings_max_proposals",
        ),
    )

    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    transcript_retention_days: Mapped[int] = mapped_column(
        Integer, server_default="30", default=30
    )
    max_turns_per_user_per_minute: Mapped[int] = mapped_column(
        Integer, server_default="6", default=6
    )
    max_turns_per_org_per_hour: Mapped[int] = mapped_column(
        Integer, server_default="200", default=200
    )
    daily_token_budget: Mapped[int | None] = mapped_column(Integer, default=None)
    # §6.4: an unbounded expiry is a confirmation token that outlives the
    # situation that justified it. Health's `approval_ttl_s` is bounded for the
    # same reason.
    action_expiry_s: Mapped[int] = mapped_column(Integer, server_default="900", default=900)
    # §6.5: the bound §0.12 adds at the other end from `max_target_scope` --
    # one device per row is a claim about ONE dispatch, this is the claim about
    # HOW MANY.
    max_proposals_per_turn: Mapped[int] = mapped_column(Integer, server_default="3", default=3)
    updated_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


#: The states an `ai_action` may be in (§5.2). Transitions are one-way: a
#: confirmed row can never return to `previewed` and be re-confirmed, and a
#: second confirm is a 409 BY THE STATE MACHINE rather than by an idempotency
#: key.
AI_ACTION_STATES = (
    "proposed",
    "previewed",
    "confirmed",
    "dispatched",
    "settled",
    "rejected",
    "expired",
    "failed",
)

#: What `effective_autonomy` may be on a STORED row. `off` is excluded because a
#: row that resolved to `off` should never have been minted at all, and
#: `autonomous` because it is unreachable by an AI persona in v1 (§0.4).
AI_ACTION_AUTONOMY = ("suggest", "require_approval")

#: Why a human declined, as a BOUNDED ENUM rather than free text (§8.2). Model-
#: adjacent free text on a durable row is what §8.3 spends a paragraph refusing:
#: the rejecting human is reading the model's rationale at the moment they
#: click, and a free-text box beside it is where that prose gets copied.
AI_ACTION_REJECTION_REASONS = (
    "not_needed",
    "wrong_device",
    "wrong_action",
    "needs_change_request",
    "unsafe_now",
    "other",
)


class AiAction(Base):
    """One PROPOSED OCCURRENCE of an `approved_action` catalogue entry (§5.1).

    `approved_action` is org policy -- *this thing may be run, at this risk, by
    these personas, with these fuses* -- and is long-lived. This row is one
    proposal of it: this action, on this device, in this turn of this session,
    with these rendered params, previewed at this digest, confirmed (or not) by
    this human, dispatched as this job.

    **`ON DELETE RESTRICT` ON `session_id`, DELIBERATELY, AND IT IS THE ONLY
    ASYMMETRY IN THIS FILE.** `ai_message` CASCADEs from `ai_session` because a
    transcript is deletable BECAUSE IT IS NOT AUDIT. An `ai_action` that reached
    `dispatched` is not deletable on those terms: it names a signed job that ran
    on somebody's machine. The retention sweep therefore blanks `rationale` and
    nothing else on this table (§8.3), and a session with a surviving action
    cannot be hard-deleted out from under it. RESTRICT IS NOT CONDITIONAL -- a
    session whose actions are all `rejected` cannot be deleted either. That is
    the honest cost, it is pinned by a test, and the existing transcript
    retention sweep has to handle it rather than discover it in production.

    **NO UNIQUE CONSTRAINT AND NO IDEMPOTENCY KEY, DELIBERATELY** (§0.5).
    `health_alert_action`'s `UNIQUE (alert_id, kind)` is the right idempotency
    for a threshold breach and the wrong one for a conversation: an AI proposal
    has no alert, and two proposals of the same action on the same device in one
    session are two decisions a human may answer differently. A builder reaching
    for one "for dedupe" will reach for a PARTIAL index over nullable columns,
    and two things then go wrong at once: NULLs do not collide in a Postgres
    unique constraint, so it will not dedupe what they think it does; and
    `ON CONFLICT ON CONSTRAINT <partial index>` fails at RUNTIME with
    `InvalidObjectDefinition`, because a partial index is not a named
    constraint. The state machine is what makes a second confirm a 409.

    `params_template` is NOT copied here. `rendered_params` is the rendered
    result; the template lives on the catalogue row, where `redact_secret_values`
    already governs its read path.

    `target_device_id` is a single NOT NULL uuid (§0.12): no group column, no
    target array, no selector union. A campaign is N rows, each with its own
    preview, its own digest and its own confirmation.
    """

    __tablename__ = "ai_action"
    __table_args__ = (
        CheckConstraint(
            "persona IN ('end_user', 'technician', 'admin')",
            name="ck_ai_action_persona",
        ),
        CheckConstraint(
            "risk_class IN ('low', 'medium', 'high')",
            name="ck_ai_action_risk_class",
        ),
        CheckConstraint(
            "state IN ('proposed', 'previewed', 'confirmed', 'dispatched', 'settled', "
            "'rejected', 'expired', 'failed')",
            name="ck_ai_action_state",
        ),
        # A stored `off` would be a row that should never have been minted, and
        # `autonomous` is unreachable in v1 (§0.4). Both refused by the database
        # rather than by whoever writes the next insert path.
        CheckConstraint(
            "effective_autonomy IN ('suggest', 'require_approval')",
            name="ck_ai_action_effective_autonomy",
        ),
        Index("ix_ai_action_org_state_expiry", "org_id", "state", "expires_at"),
        Index("ix_ai_action_org_session", "org_id", "session_id"),
        Index("ix_ai_action_org_device", "org_id", "target_device_id"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by target_device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"), index=True, server_default=FetchedValue()
    )
    session_id: Mapped[UUID] = mapped_column(ForeignKey("ai_session.id", ondelete="RESTRICT"))
    message_seq: Mapped[int] = mapped_column(Integer)
    persona: Mapped[str] = mapped_column(String(16))
    approved_action_id: Mapped[UUID] = mapped_column(ForeignKey("approved_action.id"))
    # Denormalized so the record survives a catalogue delete: "what did the AI
    # dispatch last March" must stay answerable after the row it named is gone.
    approved_action_key: Mapped[str] = mapped_column(String(100))
    job_type: Mapped[str] = mapped_column(String(100))
    target_device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"))
    # From `render_params_template`, NEVER from a request body (§0.6).
    rendered_params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    script_id: Mapped[UUID | None] = mapped_column(ForeignKey("scripts.id"), default=None)
    script_version: Mapped[int | None] = mapped_column(Integer, default=None)
    risk_class: Mapped[str] = mapped_column(String(8), default="low")
    # Re-derived by `is_destructive` FROM THE PIN, never accepted from a caller.
    destructive: Mapped[bool] = mapped_column(Boolean, default=False)
    effective_autonomy: Mapped[str] = mapped_column(String(20), default="require_approval")
    tainted: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)
    preview_digest: Mapped[str] = mapped_column(CHAR(64), server_default="", default="")
    # MODEL PROSE, on the transcript's clock (§8.3). Stored and displayed, never
    # executed, and NEVER written to the audit log: the chain is permanent and
    # un-redactable, and this text is downstream of whatever the model read.
    rationale: Mapped[str] = mapped_column(Text, server_default="", default="")
    state: Mapped[str] = mapped_column(String(16), default="proposed")
    expires_at: Mapped[datetime] = mapped_column()
    confirmed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    confirmed_at: Mapped[datetime | None] = mapped_column(default=None)
    rejected_reason: Mapped[str | None] = mapped_column(String(200), default=None)
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey("jobs.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
