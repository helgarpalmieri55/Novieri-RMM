"""Account mail: the notices of design §2.7 and the reset link of §2.8.

THE ACCOUNT MAIL IS TASK 3's (`app.core.mail.send_account_mail`). This module
says what each mail says and survives its absence: with no account mail
configured, or the relay down, `deliver` answers False and nothing fails -- a
notice never undoes the act it reports. The caller that must know (the
invitation, Task 8) shows its link instead. The failure is caught INSIDE the
caller's session (Task 3's contract): the vault checkout of the relay's
password is audited in that session, and a raise would roll that record back.

IN THE PERSON'S LANGUAGE. A notice is about the person's own account, so it is
composed under `users.locale` when they saved one, and the request's language
otherwise.

NO SECRET IN A NOTICE. A notice names what happened and when, never a code, a
token or a password; the reset link (and Task 8's invitation link) is the one
secret a mail carries, and it is the reason the mail exists.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import locale_var, tr, use_locale
from app.core.mail import MailNotConfigured, MailSendFailed, send_account_mail
from app.core.vault import VaultService
from app.models import User

#: The product's name, as the mails, the authenticator app and the passkey
#: prompt say it (design §2.1 names it among the context words).
PRODUCT = "Novieri"

Notice = Literal[
    "password_changed",
    "factor_added",
    "factor_removed",
    "factor_disabled",
    "device_remembered",
    "recovery_code_used",
    "factors_reset",
    "recovery_link_issued",
]


def _when() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _factor_name(factor: str | None) -> str:
    if factor == "passkey":
        return tr("a passkey")
    if factor == "totp":
        return tr("an authenticator app")
    if factor == "recovery_codes":
        return tr("a new set of recovery codes")
    if factor == "recovery":
        return tr("your recovery codes")
    return tr("your password")


def _compose(
    kind: Notice, *, factor: str | None, days: int | None, left: int | None
) -> tuple[str, str]:
    when = _when()
    if kind == "password_changed":
        return (
            tr("Your {product} password was changed", product=PRODUCT),
            tr(
                "The password of your {product} account was changed on {when}.\n\n"
                "If it was not you, tell your administrator now.",
                product=PRODUCT,
                when=when,
            ),
        )
    if kind == "factor_added":
        return (
            tr("A sign-in method was added to your {product} account", product=PRODUCT),
            tr(
                "Added on {when}: {factor}.\n\nIf it was not you, tell your administrator now.",
                when=when,
                factor=_factor_name(factor),
            ),
        )
    if kind == "factor_removed":
        return (
            tr("A sign-in method was removed from your {product} account", product=PRODUCT),
            tr(
                "Removed on {when}: {factor}.\n\nIf it was not you, tell your administrator now.",
                when=when,
                factor=_factor_name(factor),
            ),
        )
    if kind == "factor_disabled":
        return (
            tr("A sign-in method on your {product} account was locked", product=PRODUCT),
            tr(
                "After too many failed attempts, {factor} was locked on {when}. An administrator "
                "can reset your sign-in methods.",
                factor=_factor_name(factor),
                when=when,
            ),
        )
    if kind == "device_remembered":
        return (
            tr("A device will be remembered for your {product} account", product=PRODUCT),
            tr(
                "On {when} a browser was remembered: for {days} days it will not ask for your "
                "second factor.\n\nIf it was not you, remove it under My security and tell your "
                "administrator.",
                when=when,
                days=days,
            ),
        )
    if kind == "recovery_code_used":
        return (
            tr("A recovery code was used on your {product} account", product=PRODUCT),
            tr(
                "A recovery code was used on {when}. {left} codes remain.\n\n"
                "If it was not you, tell your administrator now.",
                when=when,
                left=left,
            ),
        )
    if kind == "recovery_link_issued":
        return (
            tr("A recovery link was issued for your {product} account", product=PRODUCT),
            tr(
                "On {when} someone with access to the server issued a one-time recovery link for "
                "your account. It works for 15 minutes.\n\n"
                "If you did not ask for this, tell whoever runs this installation now.",
                when=when,
            ),
        )
    return (
        tr("Your {product} sign-in methods were reset", product=PRODUCT),
        tr(
            "Your passkeys, authenticator app and recovery codes were removed on {when}. You "
            "will set up a new second factor the next time you sign in.\n\n"
            "If you did not ask for this, tell your administrator now.",
            when=when,
        ),
    )


def reset_mail(link: str) -> tuple[str, str]:
    return (
        tr("Reset your {product} password", product=PRODUCT),
        tr(
            "Someone asked to reset the password of your {product} account.\n\n"
            "Open this link within 30 minutes to choose a new one. You will still need your "
            "second factor to sign in.\n{link}\n\n"
            "If it was not you, ignore this message: your password has not changed.",
            product=PRODUCT,
            link=link,
        ),
    )


def invitation_mail(link: str, *, inviter: str) -> tuple[str, str]:
    """The invitation (Task 8, design §7): the one-time link to choose a
    password, the second secret a mail carries besides the reset link."""
    return (
        tr("You are invited to {product}", product=PRODUCT),
        tr(
            "{inviter} invited you to {product}.\n\n"
            "Open this link within 72 hours to choose a password and set up your second "
            "factor:\n{link}\n\n"
            "If you did not expect this, ignore this message.",
            inviter=inviter,
            product=PRODUCT,
            link=link,
        ),
    )


async def deliver(
    session: AsyncSession,
    *,
    org_id: UUID,
    to: str,
    subject: str,
    text: str,
    vault: VaultService,
    actor_id: UUID | None,
) -> bool:
    """True when the relay took it. Never raises for delivery: no configured
    relay, or a relay that refused (`MailSendFailed`, whose `reason` is from a
    fixed vocabulary and already audited by Task 3), is False -- and so is a
    vault that did not open the relay's sign-in (`SIGN_IN_UNAVAILABLE`, fix 3
    review Minor 2), so a notice that closes a transaction after another one
    cannot roll back the first. A database error still raises: the
    transaction it happened in is already aborted."""
    try:
        await send_account_mail(
            session,
            org_id=org_id,
            to=to,
            subject=subject,
            text=text,
            vault=vault,
            actor_id=actor_id,
        )
    except (MailNotConfigured, MailSendFailed):
        return False
    return True


async def tell(
    session: AsyncSession,
    user: User,
    kind: Notice,
    *,
    vault: VaultService,
    actor_id: UUID | None,
    factor: str | None = None,
    days: int | None = None,
    left: int | None = None,
) -> bool:
    with use_locale(user.locale or locale_var.get()):
        subject, text = _compose(kind, factor=factor, days=days, left=left)
    return await deliver(
        session,
        org_id=user.org_id,
        to=user.email,
        subject=subject,
        text=text,
        vault=vault,
        actor_id=actor_id,
    )
