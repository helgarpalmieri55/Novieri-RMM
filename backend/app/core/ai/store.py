"""Turn persistence and the four `ai.*` audit events (spec §0.7, §0.8, §3.2,
§3.3, §5 step 6, §9).

**A TURN IS RECORDED ATOMICALLY OR NOT AT ALL.** `persist_turn` writes the user
row, the assistant row, the injection flags, the session counters and the
`ai.query` audit event through ONE session, in one transaction. The alternative
-- write the transcript, then audit -- produces a conversation with no audit
line whenever the second write fails, which is precisely the state the audit log
exists to make impossible.

**A QUESTION IS AUDITABLE; ITS TEXT IS NOT** (§0.7). The audit event carries
`question_sha256` and `question_len`; the question and the answer live in
`ai_message`, which is deletable and swept on a 30-day clock. This is not
squeamishness. The audit log is append-only and hash-chained, `UPDATE`/`DELETE`
are revoked from the app role, and anything written there is written FOREVER and
cannot be redacted -- so an administrator's questions ("is the CFO's laptop
missing patches", "which tickets mention the acquisition") would become a
permanent, un-erasable record of what people were curious about, in the one
table nobody can clean up. The hash still answers the question audit is for: was
this asked, how often, by whom, and did it move data.

THAT PROMISE COVERS `ai_session.title`, WHICH IS A COPY OF THE SAME TEXT.
`persist_turn` denormalizes the first question's first 200 characters onto the
session so a list of conversations is legible. A copy of question text is
question text, and it expires with its original: `retention.sweep_org` blanks
the title of any session whose `seq 0` message it deleted, so the title cannot
outlive the message it came from. Every future denormalization of a question or
an answer onto another row owes that same clock -- the retention promise is
about the TEXT, not about `ai_message`.

**NEVER PUT ATTACKER-CONTROLLED PROSE IN `audit_event.meta`** (§3.3). The
`ai.injection_flagged` event carries `{tool, source_type, source_id,
matched_rule}` and NO EXCERPT. Excerpts live in `ai_injection_flag`, which is
deletable. Inbound email is unauthenticated (`docs/modules/email-intake/spec.md`
§0.2: an unrecognised sender's message is quarantined but STILL STORED), so an
arbitrary internet sender can put bytes into a record an administrator will ask
about. If the excerpt reached the audit meta, that sender could write arbitrary
permanent content into the tamper-evident chain.

**ONE EVENT PER TURN, NOT PER TOOL CALL** (§0.8). `audit_service.record` takes a
per-org advisory transaction lock per append, so appends serialize; letting read
chatter multiply audit volume fivefold makes the log slower AND worse at its
actual job, which is finding the privileged actions among the noise.

**TOOL RESULTS ARE NOT STORED** (§3.2). `ai_message.tool_calls` records
`{tool, arg_hash, result_count, truncated, status}` -- enough to explain and
audit the turn, not enough to reconstitute the data. Storing results would make
this table a second, unaudited, un-RLS-reasoned shadow copy of every record the
agent ever read, and `ai_message.role`'s CHECK constraint is what makes that a
property of the schema rather than a convention of this file.
"""

import hashlib
import unicodedata
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.ai.errors import AiNotFoundError
from app.core.ai.orchestrator import TurnOutcome
from app.core.audit import audit_service
from app.core.i18n import tr
from app.jobs.canonical import canonical_bytes
from app.models.ai import AiInjectionFlag, AiMessage, AiSession

__all__ = [
    "MAX_AUDIT_SOURCES",
    "arg_hash",
    "create_session",
    "delete_session",
    "list_sessions",
    "load_session",
    "persist_turn",
    "question_sha256",
]

# §9: `sources` is capped, with a `truncated` marker. A hostile record carrying
# a hundred injection-shaped fields must not be able to write a hundred audit
# entries -- the detection heuristic caps findings at 20 for the same reason,
# and this cap is the second, independent one, because the audit log is the
# thing that cannot be cleaned up afterwards.
MAX_AUDIT_SOURCES = 20

_TITLE_CHARS = 200


def _utcnow() -> datetime:
    return datetime.now(UTC)


def question_sha256(text: str) -> str:
    """SHA-256 over the NORMALIZED question (§9).

    Unicode NFC, whitespace collapsed, lowercased -- so "was this asked before"
    survives trivial variation. Without normalization the hash answers a
    narrower question than the one audit is asked ("has anyone asked this?") and
    answers it wrongly for two spellings of the same sentence.
    """
    normalized = " ".join(unicodedata.normalize("NFC", text).split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    """`canonical_bytes` REJECTS anything it does not know how to encode, by
    design -- `app/jobs/canonical.py::_reject_unknown` raises `TypeError` rather
    than guessing, so a formatting ambiguity can never silently change signed
    bytes. A validated tool argument is very often a `UUID`, which is exactly
    such a type, so values are rendered to strings HERE, deliberately and
    visibly, rather than by relaxing the canonical encoder that the job
    signatures and the audit chain both depend on."""
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, str | int | float | bool | type(None)):
        return value
    return str(value)


def arg_hash(args: Any) -> str:
    """A stable hash of one tool call's arguments.

    Reuses `canonical_bytes` rather than inventing a second serialization: two
    encodings of "the same" object drift, and this project has exactly one
    canonical encoding for a reason.

    Arguments are hashed rather than stored because they can name records --
    device ids, ticket filters -- and §3.2's whole point is that this table
    explains a turn without reproducing the estate. The hash still answers "was
    this the same call as that one".
    """
    return hashlib.sha256(canonical_bytes(_jsonable(dict(args)))).hexdigest()


def _tool_call_rows(outcome: TurnOutcome) -> list[dict[str, Any]]:
    """§3.2's exact five keys. NOTHING ELSE, and no payload."""
    return [
        {
            "tool": record.tool,
            "arg_hash": arg_hash(record.args),
            "result_count": record.result_count,
            "truncated": record.truncated,
            "status": record.status,
        }
        for record in outcome.tool_calls
    ]


def _audit_tools(outcome: TurnOutcome) -> list[dict[str, Any]]:
    """§9's `tools`: `{tool, result_count, truncated, status}`, capped at the
    turn budget (<= 8 by construction)."""
    return [
        {
            "tool": record.tool,
            "result_count": record.result_count,
            "truncated": record.truncated,
            "status": record.status,
        }
        for record in outcome.tool_calls
    ][:MAX_AUDIT_SOURCES]


# --------------------------------------------------------------------------- #
# Sessions.
# --------------------------------------------------------------------------- #


async def create_session(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    persona: str,
    mode: str = "read",
    model_id: str = "",
    provider_key: str | None = None,
) -> AiSession:
    """A new conversation, PINNED TO ONE ORG at creation (§0.9), plus the
    `ai.session_started` audit event.

    `persona` IS A REQUIRED ARGUMENT AND HAS NO DEFAULT. This is kernel code and
    must not know a persona's name (docs/modules/ai-automation.md §0.1); the
    calling router passes its own `PersonaProfile.name`. `mode` defaults to
    `read` because that is a property of this table's own state machine rather
    than of any persona -- and it is FIXED AT CREATION (§0.8): there is no
    promotion path, because promoting a session would import a whole read-mode
    transcript, assembled under read-mode replay rules, into a context that can
    propose.
    """
    row = AiSession(
        org_id=org_id,
        user_id=user_id,
        persona=persona,
        mode=mode,
        model_id=model_id,
        created_at=_utcnow(),
        last_turn_at=_utcnow(),
    )
    session.add(row)
    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=user_id,
        action="ai.session_started",
        target_type="ai_session",
        target_id=str(row.id),
        metadata={
            "persona": row.persona,
            "mode": row.mode,
            "model_id": model_id,
            "provider_key": provider_key or "",
        },
    )
    return row


async def load_session(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, session_id: UUID
) -> tuple[AiSession, list[AiMessage]]:
    """One conversation and its transcript, in `seq` order.

    THE `org_id` AND `user_id` PREDICATES ARE EXPLICIT AND IN ADDITION TO RLS.
    RLS rejects a cross-org read first, so a cross-org test driven through
    `org_session` passes even when this predicate has been deleted -- the
    admin-engine tests are what pin it. RLS says nothing at all about the
    `user_id`: a conversation belongs to the administrator who started it, and
    another administrator in the same org gets a 404, never a transcript.
    """
    row = (
        await session.execute(
            select(AiSession).where(
                AiSession.id == session_id,
                AiSession.org_id == org_id,
                AiSession.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AiNotFoundError(tr("no ai session {session_id}", session_id=session_id))
    messages = list(
        (
            await session.execute(
                select(AiMessage)
                .where(AiMessage.session_id == session_id, AiMessage.org_id == org_id)
                .order_by(AiMessage.seq)
            )
        )
        .scalars()
        .all()
    )
    return row, messages


async def list_sessions(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, limit: int = 50
) -> list[AiSession]:
    """THE CALLER'S OWN conversations, most recent first."""
    return list(
        (
            await session.execute(
                select(AiSession)
                .where(AiSession.org_id == org_id, AiSession.user_id == user_id)
                .order_by(AiSession.last_turn_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def delete_session(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, session_id: UUID
) -> None:
    """Hard-delete one conversation. `ON DELETE CASCADE` removes its messages
    and its injection flags; the audit events it produced remain, because they
    are the record that it happened."""
    row = (
        await session.execute(
            select(AiSession).where(
                AiSession.id == session_id,
                AiSession.org_id == org_id,
                AiSession.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AiNotFoundError(tr("no ai session {session_id}", session_id=session_id))
    await session.delete(row)


# --------------------------------------------------------------------------- #
# The one atomic turn write (§5 step 6).
# --------------------------------------------------------------------------- #


async def persist_turn(
    session: AsyncSession,
    *,
    org_id: UUID,
    user_id: UUID,
    ai_session: AiSession,
    question: str,
    outcome: TurnOutcome,
    turn_id: UUID | None = None,
) -> tuple[AiMessage, AiMessage]:
    """Write one completed turn. Caller owns the transaction boundary.

    `turn_id` is the id the USER row will take. The API layer allocates it
    BEFORE the turn runs, so the SSE side-channel has a name to publish on --
    and the row itself is still written here, once, inside the same transaction
    as the assistant row and the audit event. Creating the user row up front
    instead would put a question in the transcript for a turn that later failed,
    with no audit line to explain it.
    """
    next_seq = (
        await session.execute(
            select(func.coalesce(func.max(AiMessage.seq), -1)).where(
                AiMessage.session_id == ai_session.id, AiMessage.org_id == org_id
            )
        )
    ).scalar_one() + 1

    user_row = AiMessage(
        id=turn_id or uuid7(),
        org_id=org_id,
        session_id=ai_session.id,
        seq=next_seq,
        role="user",
        content=question,
        created_at=_utcnow(),
    )
    assistant_row = AiMessage(
        org_id=org_id,
        session_id=ai_session.id,
        seq=next_seq + 1,
        role="assistant",
        content=outcome.answer,
        tool_calls=_tool_call_rows(outcome),
        flags={"stop_reason": outcome.stop_reason, "partial": outcome.partial},
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
        created_at=_utcnow(),
    )
    session.add(user_row)
    session.add(assistant_row)

    for finding in outcome.findings[:MAX_AUDIT_SOURCES]:
        session.add(
            AiInjectionFlag(
                org_id=org_id,
                session_id=ai_session.id,
                message_seq=assistant_row.seq,
                source_tool=finding.tool,
                source_type=finding.source_type,
                source_id=finding.source_id,
                matched_rule=finding.matched_rule,
                # THE ONE PLACE RETRIEVED TEXT IS DELIBERATELY STORED (§3.3),
                # bounded by the column (`String(400)`) as well as by `detect`.
                excerpt=finding.excerpt[:400],
            )
        )

    # A COPY OF QUESTION TEXT, ON THE QUESTION'S RETENTION CLOCK. `next_seq == 0`
    # is part of the retention contract, not a tidiness check: the title may only
    # ever come from the FIRST turn, whose user row is `seq 0`, because that is
    # what lets `retention.sweep_org` decide -- exactly, not approximately --
    # when this copy has outlived the message it was copied from. Falling back to
    # a later turn (which `if not ai_session.title` alone would do after a
    # whitespace-only first question) would leave a title anchored to a `seq 0`
    # that never carried it, and it would be blanked early. An untitled
    # conversation is the cheaper defect.
    if not ai_session.title and next_seq == 0:
        ai_session.title = question.strip()[:_TITLE_CHARS]
    ai_session.message_count += 2
    ai_session.total_input_tokens += outcome.input_tokens
    ai_session.total_output_tokens += outcome.output_tokens
    ai_session.last_turn_at = _utcnow()
    if outcome.model_id:
        ai_session.model_id = outcome.model_id
    session.add(ai_session)

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=user_id,
        action="ai.query",
        target_type="ai_session",
        target_id=str(ai_session.id),
        metadata={
            "session_id": str(ai_session.id),
            # THE HASH, NEVER THE TEXT (§0.7).
            "question_sha256": question_sha256(question),
            "question_len": len(question),
            "tools": _audit_tools(outcome),
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "duration_ms": outcome.duration_ms,
            "model_id": outcome.model_id,
            "stop_reason": outcome.stop_reason,
        },
    )

    if outcome.findings:
        sources = [
            {
                "tool": finding.tool,
                "source_type": finding.source_type,
                "source_id": finding.source_id,
                "matched_rule": finding.matched_rule,
            }
            for finding in outcome.findings[:MAX_AUDIT_SOURCES]
        ]
        metadata: dict[str, Any] = {
            "session_id": str(ai_session.id),
            "sources": sources,
            "count": len(outcome.findings),
        }
        if len(outcome.findings) > MAX_AUDIT_SOURCES:
            metadata["truncated"] = True
        # NO EXCERPT, NO PROSE. See this module's header.
        await audit_service.record(
            session,
            org_id=org_id,
            actor_type="user",
            actor_id=user_id,
            action="ai.injection_flagged",
            target_type="ai_session",
            target_id=str(ai_session.id),
            metadata=metadata,
        )

    return user_row, assistant_row
