-- Invariant #5, asserted against the real migrated schema.
--
-- "All data access is tenant-scoped. Every query is constrained by org_id,
-- enforced at the database layer via Row-Level Security, not only in
-- application code." (CLAUDE.md). This file is the only thing standing between
-- that sentence and a future migration that ships a tenant table unprotected,
-- so it is written to FAIL on the unprotected case rather than to pass on the
-- protected one.
--
-- WHY IT IS A FILE AND NOT A HEREDOC IN ci.yml: the same assertion runs in the
-- backend test suite (backend/tests/test_rls_invariant_sql.py) against the
-- schema the tests migrate, which is what makes it a falsifiable check rather
-- than a workflow step nobody can run locally. Two copies of this SQL would
-- drift, and the copy that drifts is the one that stops asserting.
--
-- THE HOLE THIS CLOSES: the predicate used to be
--
--     c.relrowsecurity AND NOT c.relforcerowsecurity
--
-- which only ever fires for a table that has RLS enabled. A tenant table
-- created with RLS entirely OFF short-circuits on the first conjunct and
-- passes -- the exact shipping accident the check exists to catch. And a table
-- with RLS enabled and forced but NO POLICY is not "protected": under FORCE,
-- policy-less means every row is invisible to app_user, which surfaces as
-- empty result sets rather than as a leak, but it equally means the tenant
-- predicate this platform relies on was never written.
--
-- AND "HAS A POLICY" IS NOT "IS PROTECTED BY A POLICY". Block 3 below closes
-- that gap. A table can be RLS-enabled, forced, and carry a policy of
-- `USING (true)` -- which permits every row to every `app_user` session and
-- mentions no tenant at all. Blocks 1 and 2 pass it. One table in this schema
-- already had exactly that shape and a route that read it with no tenant GUC
-- set, and the result was a live cross-tenant disclosure that three schema
-- guards and a 6,900-test suite could not see
-- (`docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md`, F1 and R2).
-- So block 3 asks the question the other two skip: does the predicate actually
-- name `org_id`?
--
-- WHAT DEFINES "TENANT TABLE": the presence of an `org_id` column. That is the
-- same signal the ORM, the migrations and org_session's GUC all use, and it
-- needs no maintained allowlist that a new module would have to remember to
-- edit. Tables with no `org_id` (alembic_version, permissions,
-- role_permissions) are correctly out of scope -- they hold no tenant data --
-- and the second block below still holds them to "if you turned RLS on, force
-- it".
--
-- Run: psql -v ON_ERROR_STOP=1 -f assert-rls.sql

DO $$
DECLARE
  offenders text;
BEGIN
  -- 1. Every table with an org_id column: RLS enabled, FORCED, and carrying at
  --    least one policy. Any of the three missing is a security bug.
  SELECT string_agg(
           format('%s (rls=%s forced=%s policies=%s)',
                  c.relname, c.relrowsecurity, c.relforcerowsecurity,
                  (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid)),
           ', ' ORDER BY c.relname)
    INTO offenders
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public'
     AND c.relkind = 'r'
     AND EXISTS (SELECT 1 FROM pg_attribute a
                  WHERE a.attrelid = c.oid
                    AND a.attname = 'org_id'
                    AND a.attnum > 0
                    AND NOT a.attisdropped)
     AND (NOT c.relrowsecurity
          OR NOT c.relforcerowsecurity
          OR NOT EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid));

  IF offenders IS NOT NULL THEN
    RAISE EXCEPTION
      'invariant #5: tenant table(s) not RLS-protected: %', offenders;
  END IF;

  -- 2. And nothing anywhere may have RLS enabled but not forced -- the table
  --    owner (and, in the deploy stack, `migrate`) would bypass it silently.
  SELECT string_agg(c.relname, ', ' ORDER BY c.relname)
    INTO offenders
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public'
     AND c.relkind = 'r'
     AND c.relrowsecurity
     AND NOT c.relforcerowsecurity;

  IF offenders IS NOT NULL THEN
    RAISE EXCEPTION
      'invariant #5: table(s) with RLS enabled but not FORCED: %', offenders;
  END IF;

  -- 3. And every tenant table must carry at least one PERMISSIVE policy whose
  --    USING predicate actually references `org_id`. See the header block: a
  --    policy of `USING (true)` satisfies checks 1 and 2 while enforcing
  --    nothing.
  --
  --    PERMISSIVE, not any policy, and the distinction is the whole reason
  --    this reads `polpermissive` rather than counting rows. Postgres ORs the
  --    permissive policies together and ANDs the restrictive ones on top, so a
  --    RESTRICTIVE policy can only ever narrow what a permissive one already
  --    allows. Three tables here (`audit_events`, `ticket`, `users`) carry a
  --    restrictive `portal_isolation` policy bound to `portal_user` that scopes
  --    by requester rather than by org; those intersect with `tenant_isolation`
  --    and must not be mistaken for it. Equally, `roles_insert` is an INSERT
  --    policy whose scoping lives in WITH CHECK and whose `polqual` is NULL --
  --    it is not the row-visibility rail, and `roles` passes on its
  --    `roles_select` policy instead.
  --
  --    THE EXEMPTION LIST IS NAMED, NOT A PATTERN, and it is one table:
  --
  --      platform_error_event -- platform-scoped by design (spec
  --        `docs/superpowers/specs/2026-08-15-error-monitoring-design.md` §4.1
  --        and `backend/tests/test_tenant_column_sweep.py`). Its `org_id` is a
  --        nullable CORRELATION column recording which org was in scope when an
  --        exception was captured, not a tenancy key -- a failure at process
  --        startup belongs to no org, and the grouping the module exists for is
  --        cross-org by construction. Because RLS is therefore NOT the backstop
  --        here, the whole tenant boundary is the permission code on the route,
  --        and migration 0068 moved that code to `super-admin` so no tenant
  --        administrator holds it.
  --
  --      (`platform_error_group` needs no entry: it carries no `org_id` column,
  --       so it is out of this check's scope entirely and is covered by
  --       `test_tenant_column_sweep.py` instead.)
  SELECT string_agg(c.relname, ', ' ORDER BY c.relname)
    INTO offenders
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public'
     AND c.relkind = 'r'
     AND c.relname <> ALL (ARRAY['platform_error_event'])
     AND EXISTS (SELECT 1 FROM pg_attribute a
                  WHERE a.attrelid = c.oid
                    AND a.attname = 'org_id'
                    AND a.attnum > 0
                    AND NOT a.attisdropped)
     AND NOT EXISTS (SELECT 1 FROM pg_policy p
                      WHERE p.polrelid = c.oid
                        AND p.polpermissive
                        AND pg_get_expr(p.polqual, p.polrelid) LIKE '%org_id%');

  IF offenders IS NOT NULL THEN
    RAISE EXCEPTION
      'invariant #5: tenant table(s) whose RLS policies never mention org_id: %',
      offenders;
  END IF;
END $$;
