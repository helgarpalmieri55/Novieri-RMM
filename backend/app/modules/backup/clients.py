"""The client a backup repository or policy belongs to (MSP clients plan
2026-10-26 Task 13, C1; migration 0242).

A repository is ONE client's: its master key travels in the signed
`backup.run` of every device that writes to it, and its chunks are
deduplicated inside it, so a repository two clients shared would hand a
device of one the other's key (research-5 §E1). A policy is one client's too,
and names only a repository of that client.

- A NEW repository names its client in the request (nothing else can give
  it one), loaded like every new row's (`client_for_new_rows`): one this
  session cannot see is a 404, never a 403 (spec §3.2's existence oracle); an
  archived one a 409; "Sin asignar" a 422 (C10).
- A policy's client is its group's when it has one -- a body `client_id` that
  says otherwise is a 422 -- else the body's: that client's default policy.
  With neither, it would be a policy for every client, and such a policy can
  name no repository: a 422 (`EVERY_CLIENT_REPOSITORY`). The client is then
  held like a new row's, because an upsert creates the row the first time
  (the precedent: budget settings, `budget.clients.client_for_budget_settings`).
- The repository a policy names is its client's (`policy_repository`), or a
  422 -- checked by the write unless it only STOPS the stored policy as it
  stands (`policy.upsert_policy`).

LOCK ORDER (`app.services.client_sites`): the client, read FOR SHARE, is the
FIRST lock of the write that calls these -- before any backup row and before
the audit chain's -- which is the order an archive takes them in. The group
is read before it; the stored policy after it, without a lock; the repository
last, FOR SHARE (`policy_repository`: a retire holds it, phase 3 design §5.5).
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import N_, tr, tr_marked
from app.models import DeviceGroup
from app.models.clients import CLIENT_KIND_UNASSIGNED
from app.modules.backup.models import REPOSITORY_RETIRED, BackupRepository
from app.services.client_sites import (
    ClientNotFound,
    NotAcceptingNewRows,
    client_for_new_rows,
)

EVERY_CLIENT_REPOSITORY = N_(
    "a policy for every client cannot name a repository; give each client its own"
)


class BackupClientError(Exception):
    """A refusal about a backup row's client, with the status the API answers
    and its sentence."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def _client_taking_rows(
    session: AsyncSession, org_id: UUID, client_id: UUID, *, unassigned: str
) -> None:
    try:
        client = await client_for_new_rows(session, org_id=org_id, client_id=client_id)
    except ClientNotFound:
        raise BackupClientError(404, tr("no such client")) from None
    except NotAcceptingNewRows as exc:
        raise BackupClientError(409, exc.detail) from None
    if client.kind == CLIENT_KIND_UNASSIGNED:
        raise BackupClientError(422, unassigned)


async def client_for_new_repository(
    session: AsyncSession, *, org_id: UUID, client_id: UUID
) -> None:
    """The client a new repository is filed under (module docstring), held
    FOR SHARE until commit."""
    await _client_taking_rows(
        session, org_id, client_id,
        unassigned=tr("a backup repository cannot be filed under Unassigned"),
    )


async def client_for_policy(
    session: AsyncSession,
    *,
    org_id: UUID,
    device_group_id: UUID | None,
    client_id: UUID | None,
) -> UUID:
    """The client of the policy a `PUT /backup/policies` writes (module
    docstring), held FOR SHARE until commit. Every lookup carries `org_id`."""
    if device_group_id is not None:
        group_client = (
            await session.execute(
                select(DeviceGroup.client_id).where(
                    DeviceGroup.org_id == org_id, DeviceGroup.id == device_group_id
                )
            )
        ).scalar_one_or_none()
        if group_client is None:
            raise BackupClientError(404, tr("no such device group"))
        if client_id is not None and client_id != group_client:
            raise BackupClientError(422, tr("that device group belongs to another client"))
        client_id = group_client
    if client_id is None:
        raise BackupClientError(422, tr_marked(EVERY_CLIENT_REPOSITORY))
    await _client_taking_rows(
        session, org_id, client_id,
        unassigned=tr("a backup policy cannot be filed under Unassigned"),
    )
    return client_id


async def policy_repository(
    session: AsyncSession, *, org_id: UUID, client_id: UUID, repository_id: UUID
) -> None:
    """The repository a policy of `client_id` names is that client's, and is
    not retired: a 404 when this organisation has no such repository (or the
    session cannot see it), a 422 when it is another client's, a 409 when it
    is retired (phase 3 design §5.5: the dispatch would refuse every window
    of such a policy, so the write says so first).

    Read FOR SHARE, the policy write's last lock before the audit chain's. A
    retire holds the row FOR NO KEY UPDATE while it checks that no enabled
    policy names it and writes the state (`repository.retire_repository`), so
    the two serialise, and an enabled policy never lands on a repository that
    a concurrent retire found unused."""
    repository = (
        await session.execute(
            select(BackupRepository.client_id, BackupRepository.state)
            .where(BackupRepository.org_id == org_id, BackupRepository.id == repository_id)
            .with_for_update(read=True)
        )
    ).one_or_none()
    if repository is None:
        raise BackupClientError(404, tr("no such repository"))
    if repository.client_id != client_id:
        raise BackupClientError(422, tr("that repository belongs to another client"))
    if repository.state == REPOSITORY_RETIRED:
        raise BackupClientError(409, tr("that repository is retired"))
