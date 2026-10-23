"""MCP server tables (the MCP server plan, task 2).

`mcp_revocation` is how an org admin cuts one AI app off, or one person's use of
one app, without waiting for an access token to expire: the MCP server checks it
on every request. `user_id` NULL revokes the app for the whole organization;
a value revokes it for that person only. Tenant-scoped under forced RLS
(`migrations/versions/0210_mcp.py`).
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column
from uuid_extensions import uuid7

from app.models.base import Base


class McpRevocation(Base):
    __tablename__ = "mcp_revocation"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    client_id: Mapped[str] = mapped_column(String(255), nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    revoked_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (Index("ix_mcp_revocation_lookup", "org_id", "client_id"),)
