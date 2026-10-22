#!/usr/bin/env python3
"""Who would be locked out if ITS_CONSOLE_EMAIL_DOMAIN were set to X.

Run from repo root with the backend venv:

    ITS_DATABASE_ADMIN_URL=postgresql+asyncpg://its_admin:...@localhost/itsuite \\
        backend/.venv/bin/python scripts/preflight_email_domain.py toeshee.com

WHY THIS EXISTS AND WHY IT IS A REQUIRED STEP, NOT A SUGGESTION
==============================================================

`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §3.3.
Setting the allowlist is a one-line config change with a fleet-sized blast
radius: from the next restart, every operator whose email claim does not match
is refused at all three doors — including, very plausibly, the person who set
it. There is no in-console recovery, because the console is the thing that
just stopped admitting them; the break-glass is to unset the variable and
restart the api.

So the only way to learn the cost BEFORE paying it is to ask the database who
is currently on the wrong side of the rule. That is all this does.

WHAT IT READS, AND WHY WITH THE ADMIN ENGINE
============================================

Every `users` row in every organization, joined to its org slug. That is a
deliberate cross-tenant read, which is exactly why it uses the BYPASSRLS admin
engine (`ITS_DATABASE_ADMIN_URL`) rather than a tenant session: the question
"who, anywhere in this deployment, is about to lose access" has no single
tenant to scope to, and an answer that quietly omitted a tenant would be worse
than no answer. This is an operator tool run by hand at the console, not a
request path — invariant 5 governs application queries, and none of those
change here.

IT CALLS THE SAME RULE THE DOORS CALL
=====================================

`app.core.identity.email_domain.is_allowed_domain`, imported, never
re-expressed as a SQL `LIKE`. A preflight that reimplements the predicate is a
preflight that can disagree with the gate, and it would disagree in the
direction that matters: `LIKE '%toeshee.com'` says `eviltoeshee.com` is fine
and the gate says it is not, so the operator is told nobody is affected and
locks out a user anyway.

EXIT CODE
=========

0 — no ACTIVE user would be refused; the change is safe to make.
1 — at least one active user would be refused. Their addresses are listed.
2 — could not run (no admin URL, no domain, connection failed).

Non-active rows (`disabled`, `deleted`) are listed for completeness but do not
set the exit code: those accounts are already refused by the status half of
the same gate, so the allowlist does not change anything for them.

WHAT IT CANNOT TELL YOU
=======================

It reports who would be refused AT THE MOMENT IT RUNS. An account created
between this run and the restart is not in the answer, and an operator whose
IdP email claim differs from the `users.email` projection stored here (they
are written from the same claim, but a row can predate a rename) is judged on
the stored value. Treat a clean run as evidence, not as a guarantee.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.core.identity import ACTIVE_STATUS  # noqa: E402
from app.core.identity.email_domain import is_allowed_domain  # noqa: E402
from app.models import Organization, User  # noqa: E402


@dataclass(frozen=True)
class Report:
    examined: int
    refused: list[tuple[str, str, str]]
    blocking: list[tuple[str, str, str]]


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument(
        "domain",
        nargs="?",
        default=os.environ.get("ITS_CONSOLE_EMAIL_DOMAIN", ""),
        help=(
            "the base domain you are about to configure, e.g. toeshee.com. "
            "Defaults to $ITS_CONSOLE_EMAIL_DOMAIN so a value already staged "
            "in the environment can be checked without retyping it."
        ),
    )
    parser.add_argument(
        "--admin-database-url",
        default=os.environ.get("ITS_DATABASE_ADMIN_URL", ""),
        help="BYPASSRLS connection URL (default: $ITS_DATABASE_ADMIN_URL).",
    )
    return parser.parse_args(argv)


def partition(rows: Sequence[tuple[str, str, str]], domain: str) -> Report:
    """Split (org_slug, email, status) rows into "would be refused" and "and
    that refusal actually costs someone their access".

    Separated from the I/O so both branches are testable without arranging a
    whole database into the required state -- the exit-0 branch in particular
    is not reachable by seeding, because it is a claim about EVERY row in the
    deployment.
    """
    refused = [
        (slug, email, status)
        for slug, email, status in rows
        if not is_allowed_domain(email, domain)
    ]
    return Report(
        examined=len(rows),
        refused=refused,
        blocking=[row for row in refused if row[2] == ACTIVE_STATUS],
    )


def render(report: Report, domain: str) -> int:
    """Print the report and return the process exit code."""
    refused = report.refused
    blocking = report.blocking
    rows_examined = report.examined

    print(f"base domain under test: {domain}")
    print(f"users examined:         {rows_examined}")
    print(f"would be refused:       {len(refused)} ({len(blocking)} of them active)")
    if refused:
        print()
        width = max(len(slug) for slug, _, _ in refused)
        for slug, email, status in refused:
            marker = "LOCKED OUT" if status == ACTIVE_STATUS else f"already {status}"
            print(f"  {slug:<{width}}  {email}  [{marker}]")
    print()
    if blocking:
        print(
            f"REFUSING TO CALL THIS SAFE: {len(blocking)} active "
            f"{'user' if len(blocking) == 1 else 'users'} would lose access on the "
            "next restart. Check whether one of them is you."
        )
        return 1
    print("No active user would be refused. Safe to set ITS_CONSOLE_EMAIL_DOMAIN.")
    return 0


async def run(admin_url: str, domain: str) -> int:
    engine = create_async_engine(admin_url)
    try:
        async with AsyncSession(engine) as session:
            rows = [
                (slug, email, status)
                for slug, email, status in (
                    await session.execute(
                        select(Organization.slug, User.email, User.status)
                        .join(Organization, Organization.id == User.org_id)
                        .order_by(Organization.slug, User.email)
                    )
                ).all()
            ]
    finally:
        await engine.dispose()
    return render(partition(rows, domain), domain)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if not args.domain.strip():
        print(
            "no base domain given: pass one as an argument or set "
            "ITS_CONSOLE_EMAIL_DOMAIN",
            file=sys.stderr,
        )
        return 2
    if not args.admin_database_url.strip():
        print(
            "no admin database URL: pass --admin-database-url or set "
            "ITS_DATABASE_ADMIN_URL. This must be the BYPASSRLS role -- the "
            "application role sees one tenant at a time and would silently "
            "under-report.",
            file=sys.stderr,
        )
        return 2
    try:
        return asyncio.run(run(args.admin_database_url, args.domain))
    except Exception as exc:  # noqa: BLE001 - an operator tool reports, never traces
        print(f"preflight could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
