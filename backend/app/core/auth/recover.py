"""`python -m app.core.auth.recover --email <address>` (design §7, Q5).

For the one administrator who lost every second factor and has nobody to
reset them: it prints a one-time recovery link, valid `action_tokens.
RECOVER_TTL` (15 minutes today), to the console's `/recover` page. Since the
operator's decision of 2026-11-05 it is also the way back when an
administrator's factor reset is refused with `reset_needs_recovery`.
Following it removes the person's factors and opens a session that enrols a
new one (`flows.accept_recovery`); the password does not change unless the
person sets a new one there, which the page always offers and requires while
an emailed link set the current one (operator decisions 2026-11-05, «Pedir
contraseña nueva si hay marca» and «Ofrecerla siempre, aplicar»). Either way
it never asks for the current password.

    docker compose exec api python -m app.core.auth.recover --email <address>

NOT OVER HTTP, BY CONSTRUCTION (falsifier 9): no router, no web framework
import, and nothing in `app/` imports this module -- whoever runs it already
holds the server (`tests/test_auth_recover_command.py`). Audited as
`user.recovery_link_issued`, actor `system`, and the holder is told by mail
(design §2.7: "toda recuperación avisa al titular").

The link on stdout and nothing else. On stderr, a warning that the link is
the account -- it asks for no password, so whoever opens it before it expires
owns the account: hand it over directly, never by email (final review C-M3;
`deploy/README.md`, "Recovery link") -- or a refusal, and exit status 1.
In English: it is the server operator's terminal, like the server's own log.
"""

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import create_async_engine

from app.core.audit import audit_service, load_audit_hmac_key
from app.core.auth import action_tokens, notices
from app.core.auth.lookup import find_staff
from app.core.client_scope import ClientScope
from app.core.config import Settings, get_settings
from app.core.identity import ACTIVE_STATUS
from app.core.tenancy import org_session
from app.core.vault import get_vault
from app.models import User


class RecoveryRefused(Exception):
    """Why no link was printed."""


async def issue_recovery_link(*, settings: Settings, email: str) -> str:
    """One technician's recovery link, in `ITS_DEFAULT_ORG_SLUG`'s
    organization: the token stored as its hash only, the issue audited, the
    holder told. Any earlier recovery link of theirs stops working."""
    if not settings.default_org_slug:
        raise RecoveryRefused(  # i18n-exempt: printed to the server operator's terminal
            "ITS_DEFAULT_ORG_SLUG is not set, so there is no organization to look in"
        )
    if not settings.public_console_url:
        raise RecoveryRefused(  # i18n-exempt: printed to the server operator's terminal
            "ITS_PUBLIC_CONSOLE_URL is not set, so no link can be made"
        )
    # The same key the API chains with, or this process's record would not verify.
    audit_service.set_hmac_key(load_audit_hmac_key(settings))
    engine = create_async_engine(settings.database_url)
    try:
        found = await find_staff(engine, org_slug=settings.default_org_slug, email=email)
        if found is None or found.status != ACTIVE_STATUS:
            raise RecoveryRefused(  # i18n-exempt: printed to the server operator's terminal
                "no active technician has that address"
            )
        async with org_session(found.org_id, engine=engine, clients=ClientScope.all()) as session:
            user = await session.get(User, found.user_id)
            if user is None:
                raise RecoveryRefused(  # i18n-exempt: printed to the server operator's terminal
                    "no active technician has that address"
                )
            token = await action_tokens.issue(
                session,
                org_id=found.org_id,
                user_id=user.id,
                purpose=action_tokens.RECOVER,
                ttl=action_tokens.RECOVER_TTL,
                created_by=None,
            )
            await audit_service.record(
                session,
                org_id=found.org_id,
                actor_type="system",
                actor_id=None,
                action="user.recovery_link_issued",
                target_type="user",
                target_id=str(user.id),
                # Never the token or the link: the audit log is readable by
                # every auditor in the organization.
                metadata={
                    "email": user.email,
                    "valid_minutes": int(action_tokens.RECOVER_TTL.total_seconds()) // 60,
                },
            )
            # The holder is told (design §2.7), through the same account mail
            # as the API, with this process's own vault client.
            await notices.tell(
                session, user, "recovery_link_issued", vault=get_vault(settings), actor_id=None
            )
        return action_tokens.link(settings.public_console_url, "recover", token)
    finally:
        await engine.dispose()


def _minutes() -> int:
    """The link's lifetime, from `action_tokens.RECOVER_TTL` -- never a number
    typed into a sentence (re-review Minor 4)."""
    return int(action_tokens.RECOVER_TTL.total_seconds()) // 60


def _hand_it_over() -> str:
    # In English: printed to the server operator's terminal, like the refusals.
    return (
        "WARNING: this link is the account. Hand it to the person directly; whoever "
        f"opens it in the next {_minutes()} minutes gets the account, without a password. "
        "Never send it by email."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.core.auth.recover",
        description=f"Print a one-time recovery link for a technician, valid {_minutes()} "
        "minutes.",
    )
    parser.add_argument("--email", required=True)
    args = parser.parse_args(argv)
    try:
        link = asyncio.run(issue_recovery_link(settings=get_settings(), email=args.email))
    except RecoveryRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    # On stderr, so stdout stays the link alone (final review C-M3): the link
    # asks for no password (plan decision 10), so it IS the account.
    print(_hand_it_over(), file=sys.stderr)
    print(link)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
