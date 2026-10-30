"""Budget per client (plan 2026-10-26 Task 8).

`budget_settings` had the organisation as its primary key: one base currency
and one fiscal year for every client. Its key becomes the client. The other
C tables' natural keys become per client; `budget_line` is H; a line's and an
expense's category and cost centre must be the row's client's, pinned by
`(x_id, client_id) REFERENCES <table> (id, client_id)`.

`fx_rate` stays G: one EUR->USD rate serves every client.

Existing rows go to the internal client, which holds the budget module since
0233: its settings row keeps the organisation's base currency and fiscal
year, and every category, cost centre, budget, line and expense is its, so
the composite keys hold for them all. What an expense's bare `link_id` names
(a subscription, license, device or change) is not re-checked here; the
application holds every later write to the expense's client
(`budget/expenses.py`).

Revision ID: 0238
Revises: 0237
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0238"
down_revision: str | None = "0237"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# --- Bloque común de la fase 2 (plan 2026-10-26, "Bloque común de
# migración"). Copiado igual en cada migración de familia: una migración
# publicada no importa nada que pueda cambiar después. -------------------

VISIBLE = "its_client_visible(client_id)"


def _add_c(table: str, backfill: str = "org_id") -> None:
    """Tabla C: la aplicación escribe `client_id`. `backfill` es la expresión
    SQL que da el cliente de cada fila existente; por defecto, el cliente
    interno, cuyo id es el de la organización."""
    op.execute(f"ALTER TABLE {table} ADD COLUMN client_id uuid REFERENCES clients(id)")
    op.execute(f"UPDATE {table} SET client_id = {backfill}")
    op.execute(f"ALTER TABLE {table} ALTER COLUMN client_id SET NOT NULL")
    op.execute(f"CREATE INDEX ix_{table}_client_id ON {table} (client_id)")
    # Una clave foránea ignora la RLS: sin esta, una fila de la organización A
    # podría nombrar un cliente de B bajo el alcance "todos" (0231).
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_client_org "
        "FOREIGN KEY (client_id, org_id) REFERENCES clients (id, org_id)"
    )


def _add_h(table: str, parent: str, fk: str) -> None:
    """Tabla H: el trigger de 0231 copia el cliente del padre, y el padre se
    lo propaga si cambia de cliente (spec §2.4)."""
    op.execute(f"ALTER TABLE {table} ADD COLUMN client_id uuid REFERENCES clients(id)")
    op.execute(
        f"UPDATE {table} t SET client_id = p.client_id FROM {parent} p WHERE p.id = t.{fk}"
    )
    op.execute(f"ALTER TABLE {table} ALTER COLUMN client_id SET NOT NULL")
    op.execute(f"CREATE INDEX ix_{table}_client_id ON {table} (client_id)")
    op.execute(
        f"CREATE TRIGGER its_client_from_parent BEFORE INSERT OR UPDATE OF {fk}, client_id "
        f"ON {table} FOR EACH ROW EXECUTE FUNCTION its_client_from_parent('{parent}', '{fk}')"
    )
    op.execute(
        f"CREATE TRIGGER its_client_to_{table} AFTER UPDATE ON {parent} FOR EACH ROW "
        "WHEN (OLD.client_id IS DISTINCT FROM NEW.client_id) "
        f"EXECUTE FUNCTION its_client_to_children('{table}', '{fk}')"
    )


def _isolate(table: str) -> None:
    op.execute(
        f"CREATE POLICY client_isolation ON {table} AS RESTRICTIVE FOR ALL "
        f"USING ({VISIBLE}) WITH CHECK ({VISIBLE})"
    )


def _drop_c(table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS client_isolation ON {table}")
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS fk_{table}_client_org")
    op.execute(f"ALTER TABLE {table} DROP COLUMN client_id")


def _drop_h(table: str, parent: str) -> None:
    op.execute(f"DROP TRIGGER IF EXISTS its_client_to_{table} ON {parent}")
    op.execute(f"DROP POLICY IF EXISTS client_isolation ON {table}")
    op.execute(f"DROP TRIGGER IF EXISTS its_client_from_parent ON {table}")
    op.execute(f"ALTER TABLE {table} DROP COLUMN client_id")

C = ["budget_settings", "cost_center", "expense_category", "budget", "expense"]

# (table, old constraint, old definition, new constraint, new definition).
# Old names as `pg_constraint` shows them at 0237 (confirmed: Postgres named
# the inline `UNIQUE`s of 0020).
KEYS = [
    ("cost_center", "cost_center_org_id_code_key", "UNIQUE (org_id, code)",
     "uq_cost_center_client_code", "UNIQUE (org_id, client_id, code)"),
    ("expense_category", "expense_category_org_id_key_key", "UNIQUE (org_id, key)",
     "uq_expense_category_client_key", "UNIQUE (org_id, client_id, key)"),
    ("budget", "budget_org_id_fiscal_year_name_key", "UNIQUE (org_id, fiscal_year, name)",
     "uq_budget_client_year_name", "UNIQUE (org_id, client_id, fiscal_year, name)"),
]


def upgrade() -> None:
    for table in C:
        _add_c(table)
    _add_h("budget_line", "budget", "budget_id")

    op.execute("ALTER TABLE budget_settings DROP CONSTRAINT budget_settings_pkey")
    op.execute(
        "ALTER TABLE budget_settings ADD CONSTRAINT budget_settings_pkey PRIMARY KEY (client_id)"
    )

    for table, old, _, new, definition in KEYS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {old}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {new} {definition}")
    op.execute("DROP INDEX uq_expense_category_system_key")
    op.execute(
        "CREATE UNIQUE INDEX uq_expense_category_client_system_key "
        "ON expense_category (org_id, client_id, system_key) WHERE system_key IS NOT NULL"
    )
    op.execute("DROP INDEX uq_expense_autofeed")
    op.execute(
        "CREATE UNIQUE INDEX uq_expense_client_autofeed "
        "ON expense (org_id, client_id, source, link_id, period_key) WHERE source <> 'manual'"
    )

    for parent in ("expense_category", "cost_center"):
        op.execute(
            f"ALTER TABLE {parent} ADD CONSTRAINT uq_{parent}_id_client UNIQUE (id, client_id)"
        )
    for table in ("budget_line", "expense"):
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_category_client "
            "FOREIGN KEY (category_id, client_id) REFERENCES expense_category (id, client_id)"
        )
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT fk_{table}_cost_center_client "
            "FOREIGN KEY (cost_center_id, client_id) REFERENCES cost_center (id, client_id)"
        )
    for table in [*C, "budget_line"]:
        _isolate(table)


def downgrade() -> None:
    for table in ("budget_line", "expense"):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT fk_{table}_cost_center_client")
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT fk_{table}_category_client")
    for parent in ("cost_center", "expense_category"):
        op.execute(f"ALTER TABLE {parent} DROP CONSTRAINT uq_{parent}_id_client")
    op.execute("DROP INDEX uq_expense_client_autofeed")
    op.execute(
        "CREATE UNIQUE INDEX uq_expense_autofeed "
        "ON expense (org_id, source, link_id, period_key) WHERE source <> 'manual'"
    )
    op.execute("DROP INDEX uq_expense_category_client_system_key")
    op.execute(
        "CREATE UNIQUE INDEX uq_expense_category_system_key "
        "ON expense_category (org_id, system_key) WHERE system_key IS NOT NULL"
    )
    for table, old, definition, new, _ in reversed(KEYS):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {new}")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {old} {definition}")
    op.execute("ALTER TABLE budget_settings DROP CONSTRAINT budget_settings_pkey")
    op.execute(
        "ALTER TABLE budget_settings ADD CONSTRAINT budget_settings_pkey PRIMARY KEY (org_id)"
    )
    _drop_h("budget_line", "budget")
    for table in reversed(C):
        _drop_c(table)
