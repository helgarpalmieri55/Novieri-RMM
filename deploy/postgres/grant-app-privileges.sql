-- app_user's real, narrowed privilege set -- run once after Alembic has
-- migrated the target database to head. Every statement here is idempotent
-- (GRANT/REVOKE are safe to repeat), so re-running this file is harmless.
--
-- WHY THIS FILE EXISTS. Until this change, app_user's table privileges
-- (blanket SELECT/INSERT/UPDATE/DELETE, narrowed for three sensitive
-- tables/pairs) existed ONLY in backend/tests/conftest.py's
-- _run_migrations() -- see that function, around the `grants = (...)`
-- assignment, for the source of truth this file mirrors line for line. A
-- real deployment never ran that code, so app_user had CONNECT and nothing
-- else: the very first query after boot raised `permission denied for
-- table ...`.
--
-- WHY THIS IS SEPARATE FROM init-app-user.sh. That script runs once, before
-- Alembic has created a single table (see its own comment), so it can only
-- set up `ALTER DEFAULT PRIVILEGES` -- a standing rule that grants blanket
-- SELECT/INSERT/UPDATE/DELETE to app_user on every table `its_admin`
-- creates from then on, covering every future migration automatically with
-- no manual step. But "blanket" is deliberately too wide for a few tables:
-- an apended-only audit/security log must not be UPDATE/DELETE-able by the
-- application role, and the RBAC catalog (permissions/role_permissions)
-- must not be writable by it at all -- both would otherwise open a
-- privilege-escalation path through an application-level bug rather than
-- the intended admin-only path. Those narrowings can only be applied to a
-- table that already exists, which is why they live here, run once the
-- schema is real, rather than in init-app-user.sh.
--
-- WHEN TO RUN THIS.
--   * Fresh install: automatically, via the `postgres-grants` compose
--     service (see docker-compose.yml), which runs right after `migrate`
--     completes -- see deploy/README.md's "First boot" section.
--   * Existing deployment being upgraded to a version at or after this
--     change: by hand, once, right after `docker compose up migrate` --
--     see deploy/README.md's "Upgrading an existing deployment" section for
--     the exact command.
--
-- NOTE on audit_events / security_events. Their REVOKE UPDATE, DELETE is
-- ALSO already enforced directly inside migrations 0009 and 0006
-- respectively (grep "REVOKE UPDATE, DELETE" under
-- backend/migrations/versions/) -- those apply automatically in every
-- environment the moment app_user exists and those migrations run, with no
-- help from this file. They are repeated below anyway so this file is a
-- complete, standalone statement of app_user's intended privilege set that
-- matches conftest.py exactly, and so re-running it never leaves those two
-- tables in a different state depending on execution order.
--
-- NOTE on permissions / role_permissions. Unlike the two tables above,
-- NO migration revokes INSERT/UPDATE/DELETE on these from app_user -- this
-- file is the ONLY place that narrowing is enforced for a real deployment.
-- Without it, the blanket ALTER DEFAULT PRIVILEGES grant from
-- init-app-user.sh would leave app_user able to write directly into the
-- RBAC catalog -- a way to self-grant permissions that bypasses the API's
-- own RBAC checks entirely (invariant #6 in CLAUDE.md: authorization is
-- server-side, not just app-code-enforced).

GRANT USAGE ON SCHEMA public TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;
REVOKE UPDATE, DELETE ON audit_events FROM app_user;
REVOKE INSERT, UPDATE, DELETE ON permissions, role_permissions FROM app_user;
REVOKE UPDATE, DELETE ON security_events FROM app_user;
