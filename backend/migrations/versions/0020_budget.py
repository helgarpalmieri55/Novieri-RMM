"""Budget: settings, cost centers, categories, budgets, lines, FX rates, expenses.

docs/modules/budget/spec.md §2 (data model) and §8 (security invariants).

Revision ID: 0020
Revises: 0019
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = [
    ("budget.read", "budget", "read"),
    ("budget.manage", "budget", "manage"),
    ("expense.record", "expense", "record"),
    ("expense.confirm", "expense", "confirm"),
]
_ALL_BUDGET = ["budget.read", "budget.manage", "expense.record", "expense.confirm"]
# Spec §0.5: technician and read-only are deliberately absent -- budget data is
# sensitive inside IT, and this is the first module where technician gets nothing.
ROLE_GRANTS = {
    "super-admin": _ALL_BUDGET,
    "org-admin": _ALL_BUDGET,
    "auditor": ["budget.read"],
}


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (org_id = current_setting('app.current_org', true)::uuid) "
        "WITH CHECK (org_id = current_setting('app.current_org', true)::uuid)"
    )


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE budget_settings (
            org_id uuid PRIMARY KEY REFERENCES organizations(id),
            base_currency varchar(3) NOT NULL DEFAULT 'USD',
            fiscal_year_start_month integer NOT NULL DEFAULT 1,
            updated_by uuid REFERENCES users(id),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE cost_center (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            code varchar(32) NOT NULL,
            name varchar(255) NOT NULL,
            external_ref varchar(64),
            active boolean NOT NULL DEFAULT true,
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, code)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE expense_category (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            key varchar(64) NOT NULL,
            name varchar(255) NOT NULL,
            system_key varchar(64),
            active boolean NOT NULL DEFAULT true,
            sort_order integer NOT NULL DEFAULT 0,
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, key)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_expense_category_system_key "
        "ON expense_category (org_id, system_key) WHERE system_key IS NOT NULL"
    )

    op.execute(
        """
        CREATE TABLE budget (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            fiscal_year integer NOT NULL,
            name varchar(255) NOT NULL,
            granularity varchar(10) NOT NULL,
            status varchar(10) NOT NULL DEFAULT 'draft',
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, fiscal_year, name)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE budget_line (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            budget_id uuid NOT NULL REFERENCES budget(id),
            category_id uuid NOT NULL REFERENCES expense_category(id),
            cost_center_id uuid REFERENCES cost_center(id),
            period_index integer NOT NULL,
            planned_amount numeric(14, 2) NOT NULL,
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_budget_line_org_budget ON budget_line (org_id, budget_id)"
    )
    # Uniqueness needs two indexes, not one: Postgres treats NULLs as distinct,
    # so a single unique index over the nullable cost_center_id would permit
    # unlimited duplicate org-wide lines.
    op.execute(
        "CREATE UNIQUE INDEX uq_budget_line_cc "
        "ON budget_line (org_id, budget_id, category_id, cost_center_id, period_index) "
        "WHERE cost_center_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_budget_line_nocc "
        "ON budget_line (org_id, budget_id, category_id, period_index) "
        "WHERE cost_center_id IS NULL"
    )

    op.execute(
        """
        CREATE TABLE fx_rate (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            from_currency varchar(3) NOT NULL,
            to_currency varchar(3) NOT NULL,
            rate numeric(18, 8) NOT NULL,
            effective_date date NOT NULL,
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, from_currency, to_currency, effective_date)
        )
        """
    )
    op.execute("ALTER TABLE fx_rate ADD CONSTRAINT ck_fx_rate_positive CHECK (rate > 0)")
    op.execute(
        "CREATE INDEX ix_fx_rate_org_pair_date ON fx_rate "
        "(org_id, from_currency, to_currency, effective_date DESC)"
    )

    op.execute(
        """
        CREATE TABLE expense (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            expense_date date NOT NULL,
            amount numeric(14, 2) NOT NULL,
            currency varchar(3) NOT NULL,
            fx_rate numeric(18, 8),
            amount_base numeric(14, 2),
            vendor_id uuid REFERENCES vendor(id),
            category_id uuid NOT NULL REFERENCES expense_category(id),
            cost_center_id uuid REFERENCES cost_center(id),
            description text NOT NULL DEFAULT '',
            source varchar(16) NOT NULL,
            link_type varchar(16),
            link_id uuid,
            period_key varchar(16),
            invoice_ref varchar(128),
            invoice_object_key text,
            status varchar(10) NOT NULL DEFAULT 'expected',
            entered_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            confirmed_by uuid REFERENCES users(id),
            confirmed_at timestamptz,
            voided_by uuid REFERENCES users(id),
            voided_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX ix_expense_org_date ON expense (org_id, expense_date)")
    op.execute("CREATE INDEX ix_expense_org_status ON expense (org_id, status)")
    op.execute(
        "CREATE INDEX ix_expense_org_category_date "
        "ON expense (org_id, category_id, expense_date)"
    )
    # Materialization idempotency guard. Voided rows stay under the constraint
    # on purpose -- a deliberately voided entry must never resurrect.
    op.execute(
        "CREATE UNIQUE INDEX uq_expense_autofeed "
        "ON expense (org_id, source, link_id, period_key) WHERE source <> 'manual'"
    )

    for t in (
        "budget_settings",
        "cost_center",
        "expense_category",
        "budget",
        "budget_line",
        "fx_rate",
        "expense",
    ):
        _rls(t)

    for code, module, action in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', '{module}', '{action}')"
        )
    for name, codes in ROLE_GRANTS.items():
        code_list = ", ".join(f"'{c}'" for c in codes)
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            f"WHERE r.name = '{name}' AND r.org_id IS NULL AND p.code IN ({code_list})"
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE module IN ('budget', 'expense'))"
    )
    op.execute("DELETE FROM permissions WHERE module IN ('budget', 'expense')")
    for t in (
        "expense",
        "fx_rate",
        "budget_line",
        "budget",
        "expense_category",
        "cost_center",
        "budget_settings",
    ):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
