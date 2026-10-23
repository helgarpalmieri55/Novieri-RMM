#!/bin/bash
# First-boot init: RLS-constrained application role + Keycloak database.
# The admin role (its_admin) carries BYPASSRLS deliberately — it is the
# migration/bootstrap role; all request traffic uses app_user.
#
# THE app_user TABLE-PRIVILEGE GAP (why the block below exists). Until now
# this script granted app_user only CONNECT. The actual table privileges
# (`GRANT USAGE ON SCHEMA public` + `GRANT SELECT, INSERT, UPDATE, DELETE ON
# ALL TABLES IN SCHEMA public`) existed ONLY in backend/tests/conftest.py's
# _run_migrations(), so every test ran against a correctly-privileged
# app_user while a real deployment got one that could CONNECT and nothing
# else — the first query after boot fails `permission denied for table ...`.
# See deploy/postgres/grant-app-privileges.sql (run once, after `docker
# compose up migrate`) and deploy/README.md for the rest of the fix,
# including the two narrowings (audit_events, permissions/role_permissions,
# security_events) that only make sense once those tables exist.
#
# This script itself runs before Alembic ever creates a table — Postgres
# only runs docker-entrypoint-initdb.d scripts once, against a brand-new,
# empty schema, before the "migrate" step (see deploy/README.md's "First
# boot" runbook) — so a plain `GRANT ... ON ALL TABLES IN SCHEMA public`
# here would grant nothing at all; it would silently apply to zero tables
# and this same permission-denied failure would resurface on the very next
# migration that adds a table. ALTER DEFAULT PRIVILEGES is the mechanism
# that survives that ordering: it tells Postgres "whenever role its_admin
# (the schema-owning role every migration runs as — see
# backend/migrations/env.py's own comment) creates a table/sequence in
# schema public from now on, give app_user these privileges automatically."
# That covers every future migration with no manual step, forever — not
# just the tables that exist today.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE app_user LOGIN PASSWORD '${APP_DB_PASSWORD:?APP_DB_PASSWORD must be set}'
        NOSUPERUSER NOBYPASSRLS;
    CREATE ROLE portal_user LOGIN PASSWORD '${PORTAL_DB_PASSWORD:?PORTAL_DB_PASSWORD must be set}'
        NOSUPERUSER NOBYPASSRLS;
    ALTER ROLE its_admin BYPASSRLS;
    GRANT CONNECT ON DATABASE itsuite TO app_user;
    GRANT CONNECT ON DATABASE itsuite TO portal_user;

    -- app_user: the RLS-constrained role every request handler runs as.
    -- Blanket read/write on the schema (narrowed for a few sensitive tables
    -- by grant-app-privileges.sql and, for audit_events/security_events, by
    -- migrations 0009/0006 themselves) is the correct default here — app_user
    -- is the one general-purpose application role, unlike portal_user below.
    GRANT USAGE ON SCHEMA public TO app_user;
    ALTER DEFAULT PRIVILEGES FOR ROLE its_admin IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
    -- No sequence is used anywhere in this schema today (every PK is a UUID
    -- via gen_random_uuid()), so this has nothing to apply to yet -- it is
    -- here so a future migration that DOES introduce one needs no manual
    -- grant step either, consistent with the tables default above.
    ALTER DEFAULT PRIVILEGES FOR ROLE its_admin IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO app_user;

    -- portal_user deliberately gets NO default-privileges/blanket grant here.
    -- Unlike app_user it is a least-privilege, row-scoped role: its table
    -- grants are specific and table-by-table, and they are already correctly
    -- provisioned in every environment (fresh or upgraded) by migrations
    -- 0028-0031 themselves, which is what actually creates portal_user's
    -- table access (this script only gives the role a LOGIN password, which
    -- a migration cannot do). Giving it ALL TABLES here would silently
    -- override that design and blow a hole through the RLS-scoped portal —
    -- a requester would gain access to every table in the schema, not just
    -- their own tickets.
    CREATE DATABASE keycloak OWNER its_admin;
EOSQL
