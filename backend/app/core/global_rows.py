"""A refused write to a global row is refused LOUDLY (MSP clients spec §2.2, Task 6b).

A global row is an O-table row (`client_scope_catalog`, kind "O") whose
`client_id` is NULL: configuration that serves every client. Migration 0231
lets a limited scope READ it and reserves changing, deleting or creating one
to scope `all`. The database is the binding guard -- but it refuses QUIETLY:

- a DELETE that RLS filters matches zero rows, and SQLAlchemy only warns. So
  `DELETE /credentials/{id}` answered 200 and wrote a `credential.deleted`
  audit event while the row stayed -- the audit log recording a privileged act
  that did not happen (invariant 4);
- an UPDATE that RLS filters matches zero rows, and the ORM raises
  `StaleDataError`: a 500;
- an INSERT, or an UPDATE that makes a row global, is refused by WITH CHECK
  with an `InsufficientPrivilegeError`: a 500 too.

`org_session` records its `ClientScope` in `session.info[SCOPE_KEY]` and opens
its session on `ClientScopedSession`, whose `before_flush` listener below
refuses those writes BEFORE any SQL is sent, unless the scope is all:

  a. a DELETE of a global row;
  b. an UPDATE of a row whose ORIGINAL client is NULL -- which also covers
     "claim a global row into my client" -- or that leaves the row global;
  c. an INSERT of a row that will be global: no client, or a parent (the
     trigger `its_client_from_parent` copies the parent's client) that is
     itself global. `domain_events` is the one exception (below).

It raises `GlobalRowNotWritable`, so the transaction -- and the audit event in
it -- rolls back, and `app/main.py` answers 403 with the every-client refusal
of `app.api.deps.require_all_clients`.

WHAT THE LISTENER CANNOT SEE: bulk statements (`session.execute(insert(X))`,
`update(X)`, `delete(X)`) never pass through the unit of work. Each one on an
O table that a narrowed scope reaches calls `refuse_unless_every_client`
before it, or `refuse_if_still_readable` after it matched nothing.

THE DATABASE STAYS THE GUARD. When the listener cannot tell what a row's
client is or will be (a parent this session cannot read, an attribute it never
loaded and cannot find, a parent fk a relationship will fill in only at flush
time), it lets the statement through and RLS decides, as before: this module
makes refusals loud, it never makes a write possible.
"""

from collections.abc import Iterable
from typing import Any, cast
from uuid import UUID

from sqlalchemy import MetaData, Table, event, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import MANYTOONE, ONETOMANY, InstanceState, Session, UOWTransaction
from sqlalchemy.orm.base import NO_VALUE

from app.core.client_scope import ClientScope
from app.core.client_scope_catalog import CATALOG

#: Where `org_session` records the session's scope (`Session.info`).
SCOPE_KEY = "client_scope"

#: The O tables with a SECOND parent, each with its own inheritance trigger in
#: migration 0231 (`SECOND_PARENTS` there): table -> (parent, fk). A child's
#: client comes from whichever parent it names.
SECOND_PARENTS: dict[str, tuple[str, str]] = {
    "doc_versions": ("doc_records", "record_id"),
    "health_profile_assignment": ("device_groups", "device_group_id"),
}

#: O tables whose NULL rows any pinned scope may INSERT. `domain_events` only:
#: `events.publish` cannot name a client yet (phase 2 derives it), so every
#: route that publishes would fail for a narrowed technician -- migration 0231
#: allows it for the same reason. `users` is NOT here: since Task 6b its
#: `client_isolation_insert` policy reserves creating a staff row to scope
#: all, and since Task 8 its `client_isolation_update` changing one.
NULL_INSERTABLE_FROM_ANY_SCOPE = frozenset({"domain_events"})


class GlobalRowNotWritable(Exception):
    """A write to a global row (an O-table row with no client) under a scope
    that is not every client. Mapped to 403 by `app/main.py`."""

    def __init__(self, table: str) -> None:
        self.table = table
        super().__init__(  # i18n-exempt: internal text; app/main.py translates the 403 body
            f"a {table} row with no client is written only under the every-client scope"
        )


class _Unknown:
    """The client of a row the listener cannot determine: the database decides."""


UNKNOWN = _Unknown()

Client = UUID | None | _Unknown


class _Unlinked:
    """No relationship can fill a new row's unset parent fk in at flush time:
    the column default, NULL, is what it will hold."""


UNLINKED = _Unlinked()


class ClientScopedSession(Session):
    """The sync session under every `org_session`: it carries the session's
    client scope in `info[SCOPE_KEY]`, and the guard below listens on it."""


def session_scope(session: Session | AsyncSession) -> ClientScope | None:
    """The scope `org_session` recorded, or None for any other session (the
    admin session bypasses RLS; a hand-built one is a test's)."""
    scope = session.info.get(SCOPE_KEY)
    return scope if isinstance(scope, ClientScope) else None


def refuse_unless_every_client(session: Session | AsyncSession, table: str) -> None:
    """For a bulk INSERT that the listener cannot see, of a row that will be
    global: refused under any scope of `org_session` but all."""
    scope = session_scope(session)
    if scope is not None and not scope.is_all:
        raise GlobalRowNotWritable(table)


async def refuse_if_still_readable(session: AsyncSession, model: Any, *criteria: Any) -> None:
    """For a bulk UPDATE or DELETE on an O table that matched NOTHING: when the
    row it named is still readable, RLS refused to write it -- a global row
    under a limited scope -- and that is a refusal, not a "not found"."""
    scope = session_scope(session)
    if scope is None or scope.is_all:
        return
    if (await session.execute(select(model.id).where(*criteria))).first() is not None:
        raise GlobalRowNotWritable(model.__table__.name)


def _parents(table: str) -> list[tuple[str, str]]:
    entry = CATALOG[table]
    parents = [(entry.parent, entry.fk)] if entry.parent and entry.fk else []
    if table in SECOND_PARENTS:
        parents.append(SECOND_PARENTS[table])
    return parents


def _table(state: InstanceState[Any]) -> Table:
    return cast(Table, state.mapper.local_table)


def _table_of(state: InstanceState[Any]) -> str | None:
    """The row's table when it is an O table, else None."""
    name = _table(state).name
    entry = CATALOG.get(name)
    return name if entry is not None and entry.kind == "O" else None


def _client_in_database(session: Session, state: InstanceState[Any]) -> Client:
    """The row's client as this session reads it: a global row is readable
    under any scope that can have loaded it."""
    if state.identity is None:
        return UNKNOWN
    keys = zip(state.mapper.primary_key, state.identity, strict=True)
    query = select(_table(state).c.client_id).where(*[column == value for column, value in keys])
    row = session.execute(query).first()
    return UNKNOWN if row is None else cast(Client, row[0])


def _original_client(session: Session, state: InstanceState[Any]) -> Client:
    """The client the row has in the database now, before this flush."""
    if "client_id" in state.committed_state:
        before = state.committed_state["client_id"]
        if before is not NO_VALUE:
            return cast(Client, before)
    elif "client_id" in state.dict:
        return cast(Client, state.dict["client_id"])
    return _client_in_database(session, state)


def _parent_client(session: Session, parent: str, parent_id: Any, metadata: MetaData) -> Client:
    """The parent row's client: a pending one's from memory, else as this
    session reads it (UNKNOWN when it cannot: the database decides)."""
    for pending in session.new:
        other: InstanceState[Any] = inspect(pending)
        if _table(other).name == parent and other.dict.get("id") == parent_id:
            return _resulting_client(session, parent, other, pending=True)
    columns = metadata.tables[parent].c
    row = session.execute(select(columns.client_id).where(columns.id == parent_id)).first()
    return UNKNOWN if row is None else cast(Client, row[0])


def _client_of(session: Session, parent: str, obj: Any) -> Client:
    """The client a parent OBJECT has, or will have once this flush writes it."""
    other: InstanceState[Any] = inspect(obj)
    if _table(other).name != parent:
        return UNKNOWN
    if other.identity is None:  # new itself, in this flush
        return _resulting_client(session, parent, other, pending=True)
    if other.modified:  # changed in this flush too
        return _resulting_client(session, parent, other, pending=False)
    return _original_client(session, other)


def _linked_client(
    session: Session, state: InstanceState[Any], parent: str, fk: str
) -> Client | _Unlinked:
    """The parent of a NEW row whose `fk` is unset, when a relationship names it.

    `DocLink(article=a)` leaves `article_id` out of `state.dict`: the flush
    copies it from `a` only after `before_flush` has run, so reading the
    missing fk as NULL called a link of X's article global and refused it
    (Task 6b review). So: the related object's client when a many-to-one over
    `fk` holds one; UNKNOWN -- the database decides -- when a relationship
    over `fk` exists but holds no object here (a parent's collection may hold
    this row, or the many-to-one may be set later); UNLINKED when no
    relationship touches `fk`, and the unset fk really is NULL. The
    application maps no `relationship()` today: this keeps the guard right
    the day one appears."""
    column = _table(state).c[fk]
    reachable = False
    for mapper in state.mapper.registry.mappers:
        for rel in mapper.relationships:
            if rel.direction is MANYTOONE and column in rel.local_columns:
                if not state.mapper.isa(rel.parent):
                    continue
                reachable = True
                related = state.dict.get(rel.key)
                if related is not None:
                    return _client_of(session, parent, related)
            elif rel.direction is ONETOMANY and column in rel.remote_side:
                reachable = True
    return UNKNOWN if reachable else UNLINKED


def _resulting_client(
    session: Session, table: str, state: InstanceState[Any], *, pending: bool
) -> Client:
    """The client the row will carry after the write: its parent's while it
    names one (the trigger overwrites anything else), else its own."""
    metadata = _table(state).metadata
    for parent, fk in _parents(table) if table in CATALOG else []:
        if fk in state.dict:
            parent_id = state.dict[fk]
        elif not pending:
            return UNKNOWN
        else:
            linked = _linked_client(session, state, parent, fk)
            if not isinstance(linked, _Unlinked):
                return linked
            parent_id = None  # unset on a new row, and nothing will set it: NULL
        if parent_id is not None:
            return _parent_client(session, parent, parent_id, metadata)
    if "client_id" in state.dict:
        return cast(Client, state.dict["client_id"])
    # Unset on a new row: no default but the trigger's, so NULL.
    return None if pending else UNKNOWN


def _changes_client(table: str, state: InstanceState[Any]) -> bool:
    keys = {"client_id", *(fk for _, fk in _parents(table))}
    return bool(keys & set(state.committed_state))


def _refusals(session: Session) -> Iterable[str]:
    for obj in session.deleted:
        state = inspect(obj)
        table = _table_of(state)
        if table is not None and _original_client(session, state) is None:
            yield table
    for obj in session.dirty:
        state = inspect(obj)
        table = _table_of(state)
        if table is None or not session.is_modified(obj, include_collections=False):
            continue
        if _original_client(session, state) is None:
            yield table
        elif (
            table not in NULL_INSERTABLE_FROM_ANY_SCOPE
            and table != "users"
            and _changes_client(table, state)
            and _resulting_client(session, table, state, pending=False) is None
        ):
            # Released to every client. (`users`: `ck_users_kind_client` and
            # `its_user_identity_guard` hold a contact to its client.)
            yield table
    for obj in session.new:
        state = inspect(obj)
        table = _table_of(state)
        if (
            table is not None
            and table not in NULL_INSERTABLE_FROM_ANY_SCOPE
            and _resulting_client(session, table, state, pending=True) is None
        ):
            yield table


@event.listens_for(ClientScopedSession, "before_flush")
def _refuse_global_writes(session: Session, flush_context: UOWTransaction, instances: Any) -> None:
    scope = session_scope(session)
    if scope is None or scope.is_all:
        return
    for table in _refusals(session):
        raise GlobalRowNotWritable(table)
