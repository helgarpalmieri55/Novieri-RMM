"""ORM models map cleanly onto migration 0020's tables."""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.core.tenancy import org_session
from app.models import Budget, Expense, ExpenseCategory, FxRate


async def test_expense_round_trips_with_decimal_money(api_env) -> None:
    org_id = await api_env.create_org("budget")
    sub = await api_env.create_user(org_id, "org-admin")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        user_id = (
            await session.execute(
                select(__import__("app.models", fromlist=["User"]).User.id).where(
                    __import__("app.models", fromlist=["User"]).User.external_subject == sub
                )
            )
        ).scalar_one()
        cat = ExpenseCategory(
            org_id=org_id, key="hardware", name="Hardware", system_key="hardware",
            created_by=user_id,
        )
        session.add(cat)
        await session.flush()
        exp = Expense(
            org_id=org_id,
            expense_date=date(2026, 3, 1),
            amount=Decimal("1234.56"),
            currency="USD",
            category_id=cat.id,
            source="manual",
            status="confirmed",
            entered_by=user_id,
        )
        session.add(exp)
        await session.flush()
        loaded = (
            await session.execute(select(Expense).where(Expense.id == exp.id))
        ).scalar_one()
    assert loaded.amount == Decimal("1234.56")
    assert isinstance(loaded.amount, Decimal)
    assert loaded.amount_base is None
    assert loaded.fx_rate is None


async def test_budget_round_trips(api_env) -> None:
    org_id = await api_env.create_org("budget")
    sub = await api_env.create_user(org_id, "org-admin")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        user_id = (
            await session.execute(
                select(__import__("app.models", fromlist=["User"]).User.id).where(
                    __import__("app.models", fromlist=["User"]).User.external_subject == sub
                )
            )
        ).scalar_one()
        budget = Budget(
            org_id=org_id,
            fiscal_year=2026,
            name="FY26 IT Budget",
            granularity="month",
            status="draft",
            created_by=user_id,
        )
        session.add(budget)
        await session.flush()
        loaded = (
            await session.execute(select(Budget).where(Budget.id == budget.id))
        ).scalar_one()
    assert loaded.fiscal_year == 2026
    assert loaded.granularity == "month"
    assert loaded.status == "draft"


async def test_fx_rate_round_trips_with_8dp_scale(api_env) -> None:
    org_id = await api_env.create_org("budget")
    sub = await api_env.create_user(org_id, "org-admin")
    async with org_session(org_id, engine=api_env.app_engine) as session:
        user_id = (
            await session.execute(
                select(__import__("app.models", fromlist=["User"]).User.id).where(
                    __import__("app.models", fromlist=["User"]).User.external_subject == sub
                )
            )
        ).scalar_one()
        rate = FxRate(
            org_id=org_id,
            from_currency="COP",
            to_currency="USD",
            rate=Decimal("4100.12345678"),
            effective_date=date(2026, 3, 1),
            created_by=user_id,
        )
        session.add(rate)
        await session.flush()
        loaded = (
            await session.execute(select(FxRate).where(FxRate.id == rate.id))
        ).scalar_one()
    assert loaded.rate == Decimal("4100.12345678")
    assert isinstance(loaded.rate, Decimal)
