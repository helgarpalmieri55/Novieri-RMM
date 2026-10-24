"""Client permissions; one email domain per client and one address per contact (MSP
clients spec §1.1, §1.3, §2.1, §3.3, §4).

THE THREE CODES. `clients.read`, `clients.manage` and `sites.manage` gate the
clients, sites and contacts API (Task 9, `app/api/v1/clients.py`). Seeded the
0210 way:

* `super-admin` and `org-admin` hold all three (`tests/test_rbac.py` holds
  `org-admin` to every code an organisation can hold);
* every other built-in STAFF role that reads devices -- `technician`,
  `auditor`, `read-only` (0003) -- gets `clients.read` only: a person who sees
  the fleet sees which client each machine belongs to, and the client
  selector (spec §3.1) lists exactly these rows. Managing a client or its
  sites is an admin grant. `requester` (0028) is a portal role, not staff,
  and gets nothing.

ONE EMAIL DOMAIN, ONE CLIENT. Spec §1.1: "Un dominio no puede estar en dos
clientes de la misma organización", and §4 answers a duplicate with 409. The
application checks it, but under the CALLER'S scope: a technician limited to
client X editing X's domains cannot see Y's, so that check passes a domain Y
already holds, and inbound mail from it (spec §3.3) would match two clients.
`its_client_domains_unique` is the rule where it binds: SECURITY DEFINER so it
reads every client of the organisation whatever the session's scope,
serialised per organisation by an advisory lock so two concurrent saves cannot
both pass. It raises `unique_violation` naming `uq_clients_email_domain`,
which `app/services/clients.py` answers with the same 409 as its own check.
That 409 tells a limited technician that SOME client holds the domain -- the
oracle spec §4's rule implies, and no more: neither the client nor its id is
in the message.

AN AFTER TRIGGER, NOT A BEFORE ONE (Task 9 review 1, M2). A BEFORE trigger runs
ahead of the row's RLS WITH CHECK, so a raw INSERT naming ANOTHER organisation
would answer "unique violation" or "row-level security" according to that
organisation's domains: a cross-tenant existence oracle for anyone holding
`app_user`. An AFTER ROW trigger never fires for a row RLS refused. The row it
checks is already in the table, hence `id <> NEW.id`.

ONE ADDRESS, ONE PERSON (Task 9 review 1, I1). Inbound mail files a message
under its sender's client (spec §3.3 step 1) and quarantines a sender two
users share (`app/modules/email_intake/identity.py`). So a CONTACT's address
may equal no other `users` row's in the organisation -- staff or contact, of
any client, visible to the writer or not. `its_contact_email_unique` holds it
the same way as the domains: AFTER INSERT OR UPDATE OF email, kind on `users`,
SECURITY DEFINER, an advisory lock of its own, `unique_violation` naming
`uq_users_contact_email`, the service's 409. The 409 tells a limited
technician that some user of the organisation has the address; the message
names neither the user nor the client.

IT IS ONE-DIRECTIONAL, deliberately: it checks only when the row WRITTEN is a
contact. A staff row written later with a contact's address is NOT refused,
because first-login provisioning (`app/api/deps.py::admit`) writes whatever
address the identity provider asserts, and a technician's sign-in must never
fail on a client's data. That pair is the one ambiguity left, and intake
quarantines it rather than guessing.

A CONTACT'S STATUS IS ITS CLIENT'S. `its_user_identity_guard` (0231, shipped)
refused ANY change of `users.status` outside the every-client scope, which
would stop a technician limited to client X from switching off X's own
contact (`PATCH /contacts/{id}` needs `clients.manage` on that client, Task 9
addendum decision 1). Redefined here with `CREATE OR REPLACE`: a STAFF row's
`status` stays an every-client act, and `kind` and `external_subject` stay
every-client acts on every row. The trigger (`BEFORE UPDATE OF kind,
external_subject, status`) is 0231's and is not touched; `downgrade()` puts
0231's body back verbatim.

Forward-only. `downgrade()` has never run in production (rollback is
restore-from-backup). CI runs upgrade->downgrade->upgrade.

Revision ID: 0232
Revises: 0231
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0232"
down_revision: str | None = "0231"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = [
    ("clients.read", "clients", "read"),
    ("clients.manage", "clients", "manage"),
    ("sites.manage", "sites", "manage"),
]
_ALL = [code for code, _, _ in PERMISSIONS]
ROLE_GRANTS: dict[str, list[str]] = {
    "super-admin": _ALL,
    "org-admin": _ALL,
    "technician": ["clients.read"],
    "auditor": ["clients.read"],
    "read-only": ["clients.read"],
}


def upgrade() -> None:
    for code, module, action in PERMISSIONS:
        op.execute(
            "INSERT INTO permissions (id, code, module, action) "
            f"VALUES (gen_random_uuid(), '{code}', '{module}', '{action}')"
        )
    for role, codes in ROLE_GRANTS.items():
        code_list = ", ".join(f"'{c}'" for c in codes)
        op.execute(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            f"WHERE r.name = '{role}' AND r.org_id IS NULL AND p.code IN ({code_list})"
        )

    # AFTER, so RLS has accepted the row first (module docstring, M2). The
    # return value of an AFTER ROW trigger is ignored.
    op.execute(
        """
        CREATE FUNCTION its_client_domains_unique() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        BEGIN
            IF cardinality(NEW.email_domains) = 0 THEN
                RETURN NULL;
            END IF;
            -- The two-key form: a key space apart from the audit chain's
            -- one-key `pg_advisory_xact_lock(hashtext('audit:' || org))`.
            PERFORM pg_advisory_xact_lock(
                hashtext('its_client_domains'), hashtext(NEW.org_id::text)
            );
            IF EXISTS (
                SELECT 1 FROM clients
                WHERE org_id = NEW.org_id AND id <> NEW.id
                  AND email_domains && NEW.email_domains
            ) THEN
                RAISE EXCEPTION 'an email domain of client % belongs to another client', NEW.id
                    USING ERRCODE = 'unique_violation', CONSTRAINT = 'uq_clients_email_domain';
            END IF;
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER its_client_domains_unique AFTER INSERT OR UPDATE OF email_domains "
        "ON clients FOR EACH ROW EXECUTE FUNCTION its_client_domains_unique()"
    )

    # A contact's address is no other user's (module docstring, I1). Staff
    # rows are not checked: first-login provisioning must never fail on a
    # contact's address. Case-free, like `ix_users_org_lower_email` and the
    # sender match in `email_intake/identity.py`.
    op.execute(
        """
        CREATE FUNCTION its_contact_email_unique() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        BEGIN
            IF NEW.kind IS DISTINCT FROM 'contact' THEN
                RETURN NULL;
            END IF;
            PERFORM pg_advisory_xact_lock(
                hashtext('its_contact_email'), hashtext(NEW.org_id::text)
            );
            IF EXISTS (
                SELECT 1 FROM users
                WHERE org_id = NEW.org_id AND id <> NEW.id
                  AND lower(email) = lower(NEW.email)
            ) THEN
                RAISE EXCEPTION 'the address of contact % belongs to another user', NEW.id
                    USING ERRCODE = 'unique_violation', CONSTRAINT = 'uq_users_contact_email';
            END IF;
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER its_contact_email_unique AFTER INSERT OR UPDATE OF email, kind "
        "ON users FOR EACH ROW EXECUTE FUNCTION its_contact_email_unique()"
    )

    # What a `users` row IS -- staff or contact (and with it, through
    # `ck_users_kind_client`, its client) and the identity it logs in as --
    # changes only under scope all, on every row: the auth door and the
    # every-client user administration. So does whether a TECHNICIAN may log
    # in (`status` on a staff row, the admission gate, spec §3: `users.manage`
    # disables, enables and deletes, every-client). A narrowed scope reaches
    # no staff row at all (`client_isolation_update`, Task 8), so what this
    # holds there is a CONTACT of a client in the scope, whose other columns
    # that scope may write.
    #
    # A CONTACT'S `status` IS ITS CLIENT'S (Task 9, addendum decision 1):
    # `PATCH /contacts/{id}` needs `clients.manage` on the contact's client,
    # so a technician limited to X switches X's contacts on and off -- RLS
    # already confines the UPDATE to a contact of a client in the scope. Its
    # `kind` and `external_subject` stay every-client acts. `OLD.client_id`,
    # not NEW: the row as it stands decides, and a change of kind is refused
    # above on its own. A role that bypasses RLS (the admin session) is not
    # held to a client scope here either.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION its_user_identity_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.kind IS DISTINCT FROM OLD.kind
                OR NEW.external_subject IS DISTINCT FROM OLD.external_subject
                OR (NEW.status IS DISTINCT FROM OLD.status AND OLD.client_id IS NULL))
               AND current_setting('app.client_scope', true) IS DISTINCT FROM 'all'
               AND NOT (SELECT rolbypassrls OR rolsuper FROM pg_roles
                        WHERE rolname = current_user) THEN
                RAISE EXCEPTION
                    'users.kind and users.external_subject, and a staff row''s users.status, '
                    'change only under the every-client scope'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
        """
    )


def downgrade() -> None:
    # 0231's body, verbatim: every change of `users.status` outside scope all
    # refused again, a contact's included.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION its_user_identity_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (NEW.kind IS DISTINCT FROM OLD.kind
                OR NEW.external_subject IS DISTINCT FROM OLD.external_subject
                OR NEW.status IS DISTINCT FROM OLD.status)
               AND current_setting('app.client_scope', true) IS DISTINCT FROM 'all'
               AND NOT (SELECT rolbypassrls OR rolsuper FROM pg_roles
                        WHERE rolname = current_user) THEN
                RAISE EXCEPTION
                    'users.kind, users.external_subject and users.status change only '
                    'under the every-client scope'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute("DROP TRIGGER its_contact_email_unique ON users")
    op.execute("DROP FUNCTION its_contact_email_unique()")
    op.execute("DROP TRIGGER its_client_domains_unique ON clients")
    op.execute("DROP FUNCTION its_client_domains_unique()")
    code_list = ", ".join(f"'{c}'" for c in _ALL)
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        f"(SELECT id FROM permissions WHERE code IN ({code_list}))"
    )
    op.execute(f"DELETE FROM permissions WHERE code IN ({code_list})")
