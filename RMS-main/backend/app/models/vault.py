"""Credential vault key material (docs/kernel/vault.md §5).

One wrapped data-encryption key per org: the plaintext DEK exists only
transiently in server memory during store/checkout and is never persisted.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OrgVaultKey(Base):
    __tablename__ = "org_vault_keys"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    wrapped_dek: Mapped[str] = mapped_column(String)  # DEK wrapped by the vault root
    key_version: Mapped[int] = mapped_column()
    created_at: Mapped[datetime | None] = mapped_column(server_default="now()")
