"""People: one identity per person (docs/modules/people/spec.md §1).

A `person` is who works here; a `users` row is who can sign in. The two are
linked optionally, so an employee who never signs in still has one record.
"""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    FetchedValue,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid7_pk

PERSON_STATUSES = ("starting", "active", "leaving", "left")
PERSON_SOURCES = ("account", "operator")
LINK_SOURCES = ("inventory", "operator")
CHECKLIST_KINDS = ("starter", "leaver")
CHECKLIST_STATUSES = ("open", "completed", "cancelled")
STEP_STATES = ("pending", "done", "skipped")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Person(Base):
    __tablename__ = "person"
    __table_args__ = (
        Index(
            "uq_person_org_lower_email",
            "org_id",
            text("lower(email)"),
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
        Index("ix_person_org_status", "org_id", "status"),
        CheckConstraint(_in("status", PERSON_STATUSES), name="ck_person_status"),
        CheckConstraint(_in("source", PERSON_SOURCES), name="ck_person_source"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    display_name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320), default=None)
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id"), unique=True, default=None
    )
    status: Mapped[str] = mapped_column(String(12), default="active")
    department: Mapped[str | None] = mapped_column(String(200), default=None)
    job_title: Mapped[str | None] = mapped_column(String(200), default=None)
    start_date: Mapped[date | None] = mapped_column(Date, default=None)
    end_date: Mapped[date | None] = mapped_column(Date, default=None)
    source: Mapped[str] = mapped_column(String(12), default="operator")
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class PersonLogin(Base):
    """A sign-in name (`corp\\alice`, lower-cased) that identifies this person
    at a computer. Written when an operator links a device's observed user."""

    __tablename__ = "person_login"
    __table_args__ = (
        UniqueConstraint("org_id", "login", name="uq_person_login_org_login"),
        Index("ix_person_login_person", "person_id"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    person_id: Mapped[UUID] = mapped_column(ForeignKey("person.id"))
    login: Mapped[str] = mapped_column(String(256))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DevicePerson(Base):
    """Who uses a device. Kept here rather than on `devices` so the kernel
    table is not forked (spec §0.3)."""

    __tablename__ = "device_person"
    __table_args__ = (
        Index("ix_device_person_org_person", "org_id", "person_id"),
        CheckConstraint(_in("source", LINK_SOURCES), name="ck_device_person_source"),
    )

    device_id: Mapped[UUID] = mapped_column(ForeignKey("devices.id"), primary_key=True)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    # MSP clients spec, Annex A (H). Set by trigger its_client_from_parent from
    # the devices row named by device_id (migration 0231): never
    # sent; the INSERT reads it back.
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("clients.id"), index=True, server_default=FetchedValue()
    )
    person_id: Mapped[UUID | None] = mapped_column(ForeignKey("person.id"), default=None)
    source: Mapped[str] = mapped_column(String(12), default="inventory")
    observed_login: Mapped[str | None] = mapped_column(String(256), default=None)
    observed_at: Mapped[datetime | None] = mapped_column(default=None)
    set_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class PersonChecklist(Base):
    __tablename__ = "person_checklist"
    __table_args__ = (
        # One open checklist per person: nobody is starting and leaving at once.
        Index(
            "uq_person_checklist_open",
            "person_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_person_checklist_org_status", "org_id", "status"),
        CheckConstraint(_in("kind", CHECKLIST_KINDS), name="ck_person_checklist_kind"),
        CheckConstraint(_in("status", CHECKLIST_STATUSES), name="ck_person_checklist_status"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    person_id: Mapped[UUID] = mapped_column(ForeignKey("person.id"))
    kind: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(12), default="open")
    started_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    closed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    closed_at: Mapped[datetime | None] = mapped_column(default=None)


class PersonChecklistStep(Base):
    __tablename__ = "person_checklist_step"
    __table_args__ = (
        UniqueConstraint("checklist_id", "key", name="uq_person_checklist_step_key"),
        CheckConstraint(_in("state", STEP_STATES), name="ck_person_checklist_step_state"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    checklist_id: Mapped[UUID] = mapped_column(ForeignKey("person_checklist.id"))
    key: Mapped[str] = mapped_column(String(40))
    position: Mapped[int] = mapped_column(SmallInteger)
    state: Mapped[str] = mapped_column(String(12), default="pending")
    note: Mapped[str] = mapped_column(String(500), default="")
    updated_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    updated_at: Mapped[datetime | None] = mapped_column(default=None)
