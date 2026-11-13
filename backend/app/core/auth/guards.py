"""What each sign-in step needs from the session it is called with.

- pending: the password is proved and the second factor is not (a session the
  door refuses; `sessions.resolve` returns it with `factor_done=False`);
- full: the factor is proved (what the door admits);
- fresh: full, and a factor verified in the last ten minutes (design §2.5).

And the WebAuthn relying party: the console's host and origin
(`csrf.console_host` / `csrf.console_origin`, Task 6), from one setting and
never the request's Host header.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.csrf import console_host, console_origin
from app.core.auth.keys import AuthKeysUnavailable
from app.core.auth.refusals import AUTH_REQUIRED, STEP_UP_REQUIRED, AuthRefusal
from app.core.auth.sessions import SessionPrincipal, resolve, step_up_fresh
from app.core.config import Settings
from app.core.i18n import tr
from app.core.identity import ACTIVE_STATUS, CONTACT_KIND, STAFF_KIND
from app.models import User
from app.models.auth import AuthSession


async def keys_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    """OpenBao did not answer (Task 2's `AuthKeysUnavailable`): 503, one body
    whatever was being done (plan decision 13). Nothing about the account,
    the request or the transport goes back."""
    assert isinstance(exc, AuthKeysUnavailable)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "code": "sign_in_unavailable",
            "detail": tr("signing in is not available right now; try again in a moment"),
        },
    )


def relying_party(settings: Settings) -> tuple[str, str]:
    """(rp_id, expected origin); 503 without `ITS_PUBLIC_CONSOLE_URL`."""
    host, origin = console_host(settings), console_origin(settings)
    if host is None or origin is None:
        raise AuthRefusal(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            code="sign_in_unavailable",
            detail=tr(
                "signing in is not set up on this installation: its console address is not "
                "configured"
            ),
        )
    return host, origin


def sign_in_again() -> AuthRefusal:
    """401 `auth_required`: the session a step needs no longer exists."""
    return AuthRefusal(
        status.HTTP_401_UNAUTHORIZED, code=AUTH_REQUIRED, detail=tr("sign in to do this")
    )


async def session_of(request: Request) -> SessionPrincipal:
    principal = await resolve(request)
    if principal is None:
        raise sign_in_again()
    return principal


def require_pending(principal: SessionPrincipal) -> None:
    if principal.factor_done:
        raise AuthRefusal(
            status.HTTP_409_CONFLICT,
            code="already_signed_in",
            detail=tr("you are already signed in"),
        )


def require_full(principal: SessionPrincipal) -> None:
    if not principal.factor_done:
        raise AuthRefusal(
            status.HTTP_403_FORBIDDEN,
            code="factor_required",
            detail=tr("confirm your second factor first"),
        )


def require_fresh(principal: SessionPrincipal) -> None:
    """Task 6's rule (`deps.require_step_up`) for a step that holds a
    principal and no `AuthContext`: an enrolment from a full session."""
    require_full(principal)
    if not step_up_fresh(principal, datetime.now(UTC)):
        raise AuthRefusal(
            status.HTTP_403_FORBIDDEN,
            code=STEP_UP_REQUIRED,
            detail=tr("confirm your second factor again to do this"),
        )


def _admitted(user: User | None, principal: SessionPrincipal) -> User:
    if (
        user is None
        or user.kind != principal.kind
        or user.kind not in (STAFF_KIND, CONTACT_KIND)
        or user.status != ACTIVE_STATUS
    ):
        raise sign_in_again()
    return user


async def signed_user(session: AsyncSession, principal: SessionPrincipal) -> User:
    """The session's person, still active and of the session's own kind: a
    technician or, since own authentication phase 2, a client's contact (plan
    decision D25). `its_auth_session` already refuses a session whose kind is
    not its person's; this reads the same rule again in the caller's
    transaction, where a disabled person or a kind changed since is caught.

    A READ: a step that writes the person's rows takes `locked_user` instead."""
    user = (
        await session.execute(
            select(User).where(User.id == principal.user_id, User.org_id == principal.org_id)
        )
    ).scalar_one_or_none()
    return _admitted(user, principal)


async def lock_user_row(session: AsyncSession, user_id: UUID) -> User | None:
    """THE FIRST LOCK OF EVERY TRANSACTION THAT WRITES A PERSON'S ROWS (own
    authentication phase 2, Task 9b): their `users` row FOR NO KEY UPDATE,
    re-read (`populate_existing`), so what a writer committed while this
    waited -- a switch-off, a deletion -- is what the caller then checks.

    THE LOCK ORDER of a person's account: THE PERSON'S ROW; then, when the
    transaction takes it, their advisory lock (`factors.lock_person`); then
    their links, sessions, devices and credentials -- `users.last_login_at`
    among them, already held -- and LAST the organization's audit chain
    (`audit_service`'s advisory lock). BEWARE THE CHAIN'S STATEMENT:
    `audit_service.record` takes it with a `text()` execute, which does NOT
    autoflush, so a `User` attribute assigned before it is written only
    after it, at the record's next ORM read -- an unlocked row assigned and
    then audited is the chain before the row. Kept by:
    - the administrators' acts on a contact (`contact_access._contact`, the
      archive's `_lock_contacts`, `update_contact`, whose UPDATE is flushed
      before its links and its audit);
    - every act of `user_admin.service` on a technician
      (`_target(lock=True)`: `resend_invite`, `cancel_invite`,
      `assign_role`, `remove_role`, `set_enabled`, `reset_factors`,
      `end_sessions`, `delete_user`). `set_enabled`, `assign_role`,
      `remove_role` and `resend_invite` take it since the final review of
      phase 2 (items 1-2);
    - every use of their links (`flows._consumed`) and the mailing of a
      reset link (`flows.send_reset_link`);
    - their own steps: sign-in, step-up and sign-out (`locked_user`, here),
      My security (`account`'s session) and Microsoft's (`entra_flows`);
    - their own profile, `PATCH /me` (`own_profile.update_own_profile`,
      the same locked statement written out there), since the final
      review's round 2: it assigned its fields to an unlocked row and then
      audited -- the chain before the row;
    - an AI app's token requests (own authentication phase 3, Task 7;
      operator decision D2): `oauth.grants._exchange` and `_refresh` read
      the code, or the refresh token, unlocked only to learn whose it is;
      then the row; then the app's (`clients.hold`, FOR KEY SHARE); then the
      code, or the token and its family, FOR UPDATE, re-read; the grant
      rows, the access token's signature, the app's use (`clients.touch`,
      flushed), the chain last. A code exchanged while an act on the person
      is in flight waits here and reads the act's outcome, so it opens no
      grant the act's revocation could miss (`tests/test_oauth_token.py`);
    - a person's approval of an AI app (own authentication phase 3, Task 8;
      operator decision D2): `oauth.consent.decide` resolves the app first,
      outside its transaction (`clients.resolve_client`, whose purge of an
      idle document gives way on a lock wait); then, in one transaction, the
      row with the session still live under it (`locked_user`, here); the
      app's (`clients.hold`, FOR KEY SHARE); the code (`grants.issue_code`,
      no lock of its own); the app's use (`clients.touch`, flushed); the
      chain; and the "You connected" notice last. An approval that waited
      on an act which ended the session -- signing out everywhere, a
      disable -- is refused and issues nothing
      (`tests/test_oauth_consent.py`).
    THESE WRITERS take the chain last, so none of them holds it while
    waiting on a person's row; it is not a claim about every transaction
    that audits. Two of them that both take the row wait for each other
    here, before either holds anything the other needs: no 40P01
    (`tests/test_auth_sign_in_races.py`, `tests/test_user_admin_races.py`).

    The writers that take NO person's row, each with the chain last or not
    at all, so none of them can close a cycle with the above:
    - the retention sweep (`retention.py`). Its phase 2 steps delete only
      rows long dead that none of the above reads. Its OAuth steps (own
      authentication phase 3, Task 13), in the same transaction, delete rows
      long dead that a writer above may still lock -- a refresh or an
      exchange presenting an old secret, a revocation's UPDATE reaching a
      grant that lapsed unrevoked -- so they WAIT ON NO ROW. Each statement
      locks what it deletes FOR UPDATE SKIP LOCKED, the ended condition
      inside it, leaves first: `oauth_codes` expired more than ten minutes
      ago; then the refresh tokens of the grants ended more than a day ago;
      then those grants (`oauth_refresh_families`) that no token and no code
      still names, so their cascade reaches no row they did not lock. A row
      another transaction holds is left to the next day's sweep. They hold no
      person's row and no app row, and the chain comes last: a writer above
      may wait on them and then find the row gone, never the other way round
      (`tests/test_oauth_retention.py`). NOT an idle AI app, whose live
      grants go with it: the sweep's `oauth_apps` step is the purge, below
      (`clients.purge_idle`), run before the sweep's transaction and outside
      it;
    - the purge of an idle AI app (own authentication phase 3: Task 6's
      `clients.purge_idle`, and Task 13's retention step for apps, which
      must keep the same shape), which deletes a person's grant rows --
      families, refresh tokens, codes -- by cascade from `oauth_clients`,
      holding no person's row. Its shape, as `clients.purge_idle` builds it:
      the idle apps' ids read first, holding nothing; then, for each, in A
      TRANSACTION OF ITS OWN (`clients._purge_one`), `DELETE FROM
      oauth_clients WHERE id = ... AND last_used_at <= now - CLIENT_IDLE`
      (`clients.idle_at`, `clients.is_idle` in SQL), the idle condition
      INSIDE the statement, waiting at most half the server's
      `deadlock_timeout` (`clients.bound_lock_wait`: read from `pg_settings`
      in that transaction, at most 500 ms) and giving way on a timeout or a
      40P01; no app started after `clients.PURGE_BUDGET_S`; nothing after
      the cascade, not even the chain (a registration is not audited).
      `clients.resolve_client` removes a document's idle row by the same
      step before it stores a new copy. A writer of grants holds the app row
      FOR KEY SHARE before any grant row (`clients.hold`, `oauth.store`'s
      lock order, step 3), so the DELETE waits for it on the app row; that
      writer's late use (`clients.touch`, step 5) does not queue behind the
      waiting DELETE, and once it commits Postgres re-checks the condition
      on the row as it now is, finds it false, and the app survives. A
      revocation -- one UPDATE of families and then only the chain
      (`grants.revoke_family`, `revoke_all_for_user`, `revoke_app`) -- takes
      no app row (`oauth.store`'s carve-out), so the DELETE may instead wait
      IN ITS CASCADE for that writer's family rows. That UPDATE takes a
      person's families in no app's order, which is why ONE APP PER
      TRANSACTION and never several, even in id order: a purge holding one
      app's cascade while waiting in another's would have the UPDATE wait on
      it in turn -- a cycle. And with the person's two families on the ONE
      app, the cascade may hold the first while waiting on the second that
      the UPDATE took first, and the UPDATE then reach the first: a cycle
      inside one app, which is why the purge's wait is bounded by half the
      server's `deadlock_timeout`, whatever it is set to. Its wait began first, so
      it gives way before either side's deadlock check runs, and the
      person's act completes (`tests/test_oauth_schema.py`,
      `tests/test_oauth_registration.py`);
    - an AI app's RFC 7009 revocation (`oauth.grants.revoke_presented`,
      own authentication phase 3, Task 7), under that carve-out: the grant
      read unlocked, one UPDATE of its family (`grants.revoke_family`), then
      only the chain -- no person's row and no app row
      (`tests/test_oauth_token.py`);
    - the server's recovery command (`recover.py`), which issues its link
      and then audits -- link before chain -- and whose link is checked
      against the locked row when it is used (`flows._consumed`);
    - the session door's renewal (`sessions.resolve`): one UPDATE of
      `last_seen_at`, in a transaction of its own that holds nothing else
      and audits nothing;
    - the first edit of a global role (`rbac_admin._clone_into_org`), which
      repoints the organization's `user_roles` to the copy and then audits;
    - a new session's fixation sweep (`sessions.start_session`), which ends
      the session the request's cookie names -- somebody else's, when a
      cookie was planted -- in one UPDATE, under the row of the person
      signing in and before their chain.

    NOT A WRITER AT ALL since own authentication phase 3 (Task 9): the MCP
    door (`deps.admit`). Until then it refreshed the person's address from
    the token's claim -- taking the row first since the final review of
    phase 2 (item 3), the chain before the row until then -- and wrote
    `last_login_at` on every call. The platform's own token carries no
    address: the door reads the person's row and their grant
    (`grants.family_is_live`) unlocked and writes nothing, so it takes no
    lock and cannot close a cycle with any of the above
    (`tests/test_user_admin_races.py`).

    NO KEY UPDATE, not UPDATE: a row inserted meanwhile that refers to the
    person (a session, a link) takes KEY SHARE on it, which this does not
    block. None when the organization has no such row."""
    return (
        await session.execute(
            select(User)
            .where(User.id == user_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def locked_user(session: AsyncSession, principal: SessionPrincipal) -> User:
    """`signed_user` for a step that writes: the person's row locked first
    (`lock_user_row`), then the same checks -- and the session still live.
    A writer that ended it (a reset of the person's access, a deletion, a
    sign-out everywhere) took the row before ending it, so it has committed
    by now: a step that waited for it must not go on as if signed in. 401
    `auth_required`, as a session ended at `mark_factor` or `mark_step_up`
    already answers."""
    user = await lock_user_row(session, principal.user_id)
    if user is not None and user.org_id != principal.org_id:
        user = None
    user = _admitted(user, principal)
    await require_live(session, principal)
    return user


async def require_live(session: AsyncSession, principal: SessionPrincipal) -> None:
    """The caller's session has not ended since the door (or `resolve`)
    admitted it: 401 `auth_required` otherwise. Read under whatever lock
    the caller holds, so a writer that ended it under the same lock and
    committed is seen (plan 2026-11-06, A2-38; Task 9b)."""
    live = await session.scalar(
        select(AuthSession.id).where(
            AuthSession.id == principal.session_id,
            AuthSession.user_id == principal.user_id,
            AuthSession.ended_at.is_(None),
        )
    )
    if live is None:
        raise sign_in_again()
