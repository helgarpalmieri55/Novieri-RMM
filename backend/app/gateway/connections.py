"""In-process registry of live agent connections, keyed by device id.

Each gateway replica holds only its own connections; cross-replica fan-out
happens over Redis pub/sub (dispatch.py).
"""

from typing import Protocol
from uuid import UUID


class WsLike(Protocol):
    async def send_text(self, data: str) -> None: ...
    async def close(self, code: int, reason: str) -> None: ...


class ConnectionRegistry:
    def __init__(self) -> None:
        self._connections: dict[UUID, WsLike] = {}

    def add(self, device_id: UUID, ws: WsLike) -> None:
        self._connections[device_id] = ws

    def remove(self, device_id: UUID) -> None:
        self._connections.pop(device_id, None)

    def get(self, device_id: UUID) -> WsLike | None:
        return self._connections.get(device_id)

    def devices(self) -> list[UUID]:
        return list(self._connections)
