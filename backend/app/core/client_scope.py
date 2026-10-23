"""Which clients a database session may see (MSP clients spec §2.2, §2.3).

A `ClientScope` is "every client" or "exactly these ids". It travels from the
request (`AuthContext.clients`) to `org_session`, which pins it in two GUCs
that the restrictive `client_isolation` policies of migration 0231 read.

NARROWING NEVER WIDENS. `narrow()` intersects; asking a list scope for a client
outside it yields the EMPTY list, which RLS answers with zero rows -- never the
original scope (spec §2.2: the header is never ignored).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ClientScope:
    is_all: bool
    ids: frozenset[UUID]

    @classmethod
    def all(cls) -> "ClientScope":
        return cls(is_all=True, ids=frozenset())

    @classmethod
    def only(cls, ids: Iterable[UUID]) -> "ClientScope":
        return cls(is_all=False, ids=frozenset(ids))

    def narrow(self, client_id: UUID | None) -> "ClientScope":
        if client_id is None:
            return self
        if self.is_all or client_id in self.ids:
            return ClientScope.only([client_id])
        return ClientScope.only([])

    def allows(self, client_id: UUID) -> bool:
        return self.is_all or client_id in self.ids

    def guc_values(self) -> tuple[str, str]:
        """(`app.client_scope`, `app.current_clients`) for `set_config`."""
        if self.is_all:
            return "all", ""
        return "list", ",".join(sorted(str(i) for i in self.ids))


def parse_client_header(value: str | None) -> UUID | None:
    """`X-Client-Id`: absent is None; anything but one UUID raises ValueError."""
    if value is None:
        return None
    return UUID(value.strip())  # raises ValueError on "", garbage, or a list


# Files under app/ allowed to call `ClientScope.all()` (spec §2.3). Every one is
# a process acting for the whole organization, or identity resolution that
# must read the user before their scope exists. `tests/test_client_catalog.py`
# fails on any other use. A new entry is a security decision: it says why.
ALL_SCOPE_ALLOWLIST: dict[str, str] = {
    "app/api/deps.py": "resolves the user and their grants before the scope exists (spec §2.2)",
    "app/modules/portal/principal.py": "resolves the portal user before the scope exists",
    "app/core/client_scope.py": "defines it",
}
