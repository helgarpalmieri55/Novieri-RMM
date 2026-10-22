"""Budget: settings, cost centers, categories, budgets, lines, FX rates, expenses.

docs/modules/budget/spec.md §2 (data model) and §8 (security invariants).
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    desc,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, uuid7_pk


class BudgetSettings(Base):
    """One row per org, created lazily on first read (spec §2)."""

    __tablename__ = "budget_settings"

    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    base_currency: Mapped[str] = mapped_column(String(3), default="USD")
    fiscal_year_start_month: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CostCenter(Base):
    __tablename__ = "cost_center"
    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_cost_center_org_code"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(255))
    external_ref: Mapped[str | None] = mapped_column(String(64), default=None)
    active: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ExpenseCategory(Base):
    __tablename__ = "expense_category"
    __table_args__ = (
        UniqueConstraint("org_id", "key", name="uq_expense_category_org_key"),
        Index(
            "uq_expense_category_system_key",
            "org_id",
            "system_key",
            unique=True,
            postgresql_where=text("system_key IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    system_key: Mapped[str | None] = mapped_column(String(64), default=None)
    active: Mapped[bool] = mapped_column(default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Budget(Base):
    __tablename__ = "budget"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "fiscal_year", "name", name="uq_budget_org_fiscal_year_name"
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    fiscal_year: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255))
    granularity: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(10), default="draft")
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BudgetLine(Base):
    __tablename__ = "budget_line"
    __table_args__ = (
        Index("ix_budget_line_org_budget", "org_id", "budget_id"),
        # Uniqueness needs two indexes, not one: Postgres treats NULLs as
        # distinct, so a single unique index over the nullable
        # cost_center_id would permit unlimited duplicate org-wide lines.
        Index(
            "uq_budget_line_cc",
            "org_id",
            "budget_id",
            "category_id",
            "cost_center_id",
            "period_index",
            unique=True,
            postgresql_where=text("cost_center_id IS NOT NULL"),
        ),
        Index(
            "uq_budget_line_nocc",
            "org_id",
            "budget_id",
            "category_id",
            "period_index",
            unique=True,
            postgresql_where=text("cost_center_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    budget_id: Mapped[UUID] = mapped_column(ForeignKey("budget.id"))
    category_id: Mapped[UUID] = mapped_column(ForeignKey("expense_category.id"))
    cost_center_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("cost_center.id"), default=None
    )
    period_index: Mapped[int] = mapped_column(Integer)
    planned_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class FxRate(Base):
    __tablename__ = "fx_rate"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "from_currency",
            "to_currency",
            "effective_date",
            name="uq_fx_rate_org_pair_date",
        ),
        CheckConstraint("rate > 0", name="ck_fx_rate_positive"),
        # effective_date DESC matches the migration (0020_budget.py) and the
        # only query shape that uses this index: resolve_rate's
        # "greatest effective_date <= d" lookup (ORDER BY ... DESC LIMIT 1).
        Index(
            "ix_fx_rate_org_pair_date",
            "org_id",
            "from_currency",
            "to_currency",
            desc("effective_date"),
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    from_currency: Mapped[str] = mapped_column(String(3))
    to_currency: Mapped[str] = mapped_column(String(3))
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    effective_date: Mapped[date] = mapped_column(Date)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Expense(Base):
    __tablename__ = "expense"
    __table_args__ = (
        Index("ix_expense_org_date", "org_id", "expense_date"),
        Index("ix_expense_org_status", "org_id", "status"),
        Index("ix_expense_org_category_date", "org_id", "category_id", "expense_date"),
        # Materialization idempotency guard. Voided rows stay under the
        # constraint on purpose -- a deliberately voided entry must never
        # resurrect.
        Index(
            "uq_expense_autofeed",
            "org_id",
            "source",
            "link_id",
            "period_key",
            unique=True,
            postgresql_where=text("source <> 'manual'"),
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    expense_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3))
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), default=None)
    amount_base: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), default=None)
    vendor_id: Mapped[UUID | None] = mapped_column(ForeignKey("vendor.id"), default=None)
    category_id: Mapped[UUID] = mapped_column(ForeignKey("expense_category.id"))
    cost_center_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("cost_center.id"), default=None
    )
    description: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(16), default="manual")
    link_type: Mapped[str | None] = mapped_column(String(16), default=None)
    link_id: Mapped[UUID | None] = mapped_column(default=None)
    period_key: Mapped[str | None] = mapped_column(String(16), default=None)
    invoice_ref: Mapped[str | None] = mapped_column(String(128), default=None)
    invoice_object_key: Mapped[str | None] = mapped_column(Text, default=None)
    # The sanitized, client-supplied DISPLAY name for the attachment (migration
    # 0021) -- never a path component. The object-storage key above is always
    # derived server-side and carries no client input (P1 of the post-review
    # controller fix, .git/sdd/task-12-report.md).
    invoice_filename: Mapped[str | None] = mapped_column(String(255), default=None)
    status: Mapped[str] = mapped_column(String(10), default="expected")
    entered_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    confirmed_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    confirmed_at: Mapped[datetime | None] = mapped_column(default=None)
    voided_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    voided_at: Mapped[datetime | None] = mapped_column(default=None)
