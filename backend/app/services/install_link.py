"""The install link: one shared, switchable enrollment link per organisation.

docs/superpowers/specs/2026-09-28-install-link-and-windows-push-design.md §1.

WHAT THE LINK IS. A 32-character code that, while the link is on and the
request comes from a listed network, lets a machine enrol into THIS
organisation -- as a device waiting for review (§1.5). It is the only
authorization for its own enrollment, exactly as an enrollment token is (§0,
invariant 6), and it is handled like one: generated here, hashed, stored only as
the hash, and shown once.

WHAT BOUNDS IT (§1.6): the network list, the hourly limit, the on/off switch,
the optional automatic switch-off, regeneration -- and the review gate, which is
not here but in `app/jobs/review_gate.py`.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets as pysecrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.audit import audit_service
from app.core.client_address import IPAddress, IPNetwork, address_in
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.models import Device, InstallLink

#: `secrets.token_urlsafe(24)` is 32 characters of [A-Za-z0-9_-]: 192 bits.
CODE_BYTES = 24
CODE_LENGTH = 32
DEFAULT_HOURLY_LIMIT = 50
MIN_HOURLY_LIMIT = 1
MAX_HOURLY_LIMIT = 500
MAX_NETWORKS = 64
RATE_WINDOW = timedelta(hours=1)
#: How often the worker records automatic switch-offs. The link stops answering
#: at `disable_at` regardless (`is_live`); this only records it.
AUTO_OFF_INTERVAL_S = 60.0
_CODE_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class InstallLinkError(ValueError):
    """A setting the operator can correct. The route answers 422 with it."""


def new_code() -> str:
    code = pysecrets.token_urlsafe(CODE_BYTES)
    if len(code) != CODE_LENGTH:  # pragma: no cover - a property of token_urlsafe
        raise RuntimeError(  # i18n-exempt: internal invariant, unhandled 500
            "token_urlsafe(24) did not produce 32 characters"
        )
    return code


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def looks_like_code(value: str) -> bool:
    """The code's shape, checked before any hashing or database work."""
    return len(value) == CODE_LENGTH and all(c in _CODE_ALPHABET for c in value)


def normalise_networks(
    values: list[str], *, trusted_proxies: Sequence[IPNetwork] = ()
) -> list[str]:
    """The operator's networks, strictly parsed, de-duplicated, in order.

    TWO NETWORKS ARE REFUSED EVEN THOUGH THEY PARSE:
      * a `/0` (`0.0.0.0/0`, `::/0`) -- it is every address, which is exactly
        what the network list exists to rule out (spec §1.1: "an empty list is
        not 'anywhere'", and neither is this);
      * anything overlapping `ITS_TRUSTED_PROXY_CIDRS`. Behind Docker's
        userland proxy every client can reach the API appearing to come from
        the bridge gateway, which sits on that network; listing it would let
        any computer anywhere in.
    """
    if len(values) > MAX_NETWORKS:
        raise InstallLinkError(tr(
            "at most {max_networks} networks may be listed",
            max_networks=MAX_NETWORKS,
        ))
    out: list[str] = []
    for raw in values:
        item = raw.strip()
        try:
            network = ipaddress.ip_network(item, strict=True)
        except ValueError as exc:
            raise InstallLinkError(tr(
                "{item} is not a network ({exc})",
                item=repr(item),
                exc=exc,
            )) from exc
        if network.prefixlen == 0:
            raise InstallLinkError(
                tr(
                    "{item} is every address, not a network: list the networks the computers "
                    "you are installing are on",
                    item=item,
                )
            )
        for proxy in trusted_proxies:
            if proxy.version == network.version and network.overlaps(proxy):
                raise InstallLinkError(
                    tr(
                        "{item} overlaps {proxy}, the network ITMS's own proxy sits on "
                        "(ITS_TRUSTED_PROXY_CIDRS). Requests can appear to come from there, so "
                        "listing it would let any computer in",
                        item=item,
                        proxy=proxy,
                    )
                )
        if str(network) not in out:
            out.append(str(network))
    return out


def is_live(link: InstallLink, now: datetime) -> bool:
    """On, with a code, and not past its automatic switch-off. The beat that
    flips `enabled` at `disable_at` only RECORDS the switch-off; the link stops
    answering at that instant whether or not the beat has run."""
    return bool(
        link.enabled and link.code_hash and (link.disable_at is None or link.disable_at > now)
    )


#: A bare hostname or IP literal and an optional port -- nothing that could
#: close the single-quoted `CONSOLE=` line of `install.sh` or add a path.
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?$")


def console_base(public_console_url: str | None, host: str | None) -> str:
    """The console's base URL, for the link shown to the admin and for the
    address baked into `install.sh`.

    A DELIBERATE, BOUNDED DEPARTURE FROM `app/core/config.py`'s rule for
    `public_console_url` ("never derived from the request's Host header").
    That rule protects addresses the platform SENDS somewhere -- an emailed
    link, an OAuth redirect URI -- where a forged Host header redirects a
    third party. Here the configured value still wins whenever it is set;
    only when it is unset does the link fall back to the Host the requester
    itself used, so a forged header misleads only the forger, and the
    install-link spec (§1.1) names this fallback. What the header can NOT do
    is inject: anything but a hostname and port collapses to `localhost`,
    because this value is written into a shell script.
    """
    configured = (public_console_url or "").strip().rstrip("/")
    if configured:
        return configured
    candidate = (host or "").strip()
    return f"https://{candidate if _HOST.match(candidate) else 'localhost'}"


def link_url(base: str, code: str) -> str:
    return f"{base.rstrip('/')}/i/{code}"


@dataclass(frozen=True)
class LinkView:
    enabled: bool
    has_code: bool
    networks: list[str]
    disable_at: datetime | None
    hourly_limit: int
    enabled_at: datetime | None
    joined_since_enabled: int


async def _row(
    session: AsyncSession, org_id: UUID, *, for_update: bool = False
) -> InstallLink | None:
    query = select(InstallLink).where(InstallLink.org_id == org_id)
    if for_update:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


async def _locked_row(session: AsyncSession, org_id: UUID, actor_id: UUID | None) -> InstallLink:
    """This org's row, created if missing, and locked FOR UPDATE.

    INSERT ... ON CONFLICT DO NOTHING first, so two first-ever saves racing
    each other cannot both INSERT: the second waits on the first's row and
    then locks it, instead of dying on `uq_install_links_org` with a 500.
    """
    await session.execute(
        pg_insert(InstallLink)
        .values(org_id=org_id, created_by=actor_id, networks=[])
        .on_conflict_do_nothing(index_elements=["org_id"])
    )
    row = await _row(session, org_id, for_update=True)
    if row is None:  # pragma: no cover - the INSERT above guarantees a row
        raise RuntimeError(  # i18n-exempt: internal invariant, unhandled 500
            "install_links row missing after insert"
        )
    return row


async def _joined_since(session: AsyncSession, org_id: UUID, since: datetime) -> int:
    count = (
        await session.execute(
            select(func.count())
            .select_from(Device)
            .where(Device.org_id == org_id, Device.link_joined_at >= since)
        )
    ).scalar_one()
    return int(count)


async def view(session: AsyncSession, org_id: UUID, now: datetime) -> LinkView:
    row = await _row(session, org_id)
    if row is None:
        return LinkView(False, False, [], None, DEFAULT_HOURLY_LIMIT, None, 0)
    live = is_live(row, now)
    joined = (
        await _joined_since(session, org_id, row.enabled_at)
        if live and row.enabled_at is not None
        else 0
    )
    return LinkView(
        enabled=live,
        has_code=row.code_hash is not None,
        networks=[str(n) for n in row.networks],
        disable_at=row.disable_at,
        hourly_limit=row.hourly_limit,
        enabled_at=row.enabled_at if live else None,
        joined_since_enabled=joined,
    )


async def _audit(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID | None,
    action: str,
    metadata: dict[str, Any],
) -> None:
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user" if actor_id is not None else "system",
        actor_id=actor_id,
        action=action,
        target_type="install_link",
        target_id=str(org_id),
        metadata=metadata,
    )


async def save(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    enabled: bool,
    networks: list[str],
    disable_at: datetime | None,
    hourly_limit: int,
    now: datetime,
    trusted_proxies: Sequence[IPNetwork] = (),
) -> str | None:
    """Write the settings. Returns the code if this call minted one, else None.

    Validation happens BEFORE the row is touched, so a refused save changes
    nothing and audits nothing.
    """
    nets = normalise_networks(networks, trusted_proxies=trusted_proxies)
    if not MIN_HOURLY_LIMIT <= hourly_limit <= MAX_HOURLY_LIMIT:
        raise InstallLinkError(
            tr(
                "the hourly limit must be between {min_hourly_limit} and {max_hourly_limit}",
                min_hourly_limit=MIN_HOURLY_LIMIT,
                max_hourly_limit=MAX_HOURLY_LIMIT,
            )
        )
    if disable_at is not None and disable_at.tzinfo is None:
        # Refused, not guessed: a zone-less time compared with the aware clock
        # is a TypeError, and assuming UTC would switch the link off hours
        # away from when the operator meant.
        raise InstallLinkError(tr("the automatic switch-off time must include a time zone"))
    if disable_at is not None and disable_at <= now:
        raise InstallLinkError(tr("the automatic switch-off must be in the future"))
    if enabled and not nets:
        raise InstallLinkError(
            tr(
                "at least one network is required to switch the link on; an empty list would "
                "not mean 'anywhere'",
            )
        )

    row = await _locked_row(session, org_id, actor_id)
    was_on = is_live(row, now)
    before = {
        "networks": [str(n) for n in row.networks],
        "disable_at": row.disable_at.isoformat() if row.disable_at else None,
        "hourly_limit": row.hourly_limit,
    }

    minted: str | None = None
    if enabled and row.code_hash is None:
        minted = new_code()
        row.code_hash = hash_code(minted)
    row.networks = nets
    row.disable_at = disable_at
    row.hourly_limit = hourly_limit
    row.enabled = enabled
    if enabled and not was_on:
        row.enabled_at = now
    if not enabled:
        row.enabled_at = None
    row.updated_at = now
    await session.flush()

    after = {
        "networks": nets,
        "disable_at": disable_at.isoformat() if disable_at else None,
        "hourly_limit": hourly_limit,
    }
    if minted is not None:
        await _audit(session, org_id, actor_id, "enrollment.link_created", {})
    changed = {k: {"before": before[k], "after": after[k]} for k in after if before[k] != after[k]}
    if changed:
        await _audit(session, org_id, actor_id, "enrollment.link_settings_changed", changed)
    if enabled and not was_on:
        await _audit(session, org_id, actor_id, "enrollment.link_enabled", {"networks": nets})
    if was_on and not enabled:
        await _audit(session, org_id, actor_id, "enrollment.link_disabled", {"reason": "operator"})
    return minted


async def regenerate(session: AsyncSession, *, org_id: UUID, actor_id: UUID, now: datetime) -> str:
    """A new code; the old one stops working the moment this commits."""
    row = await _locked_row(session, org_id, actor_id)
    code = new_code()
    row.code_hash = hash_code(code)
    row.updated_at = now
    await session.flush()
    await _audit(
        session, org_id, actor_id, "enrollment.link_regenerated", {"enabled": bool(row.enabled)}
    )
    return code


async def code_state(session: AsyncSession, org_id: UUID, *, code: str, now: datetime) -> str:
    """What `code` is to this organisation right now, for an ADMINISTRATOR's
    request (the Group Policy MSI, GPO MSI spec 2026-09-30 §2.2): `live` -- it
    is the current code and the link is on; `off` -- the link is off, or has
    no code; `other` -- the link is on and this is not its code.

    Only the hash is compared, as everywhere else: the code itself is never
    stored. The allowed networks are NOT checked here -- this answers an
    authenticated administrator in the console, not a machine joining.
    """
    row = await _row(session, org_id)
    if row is None or not is_live(row, now):
        return "off"
    if not looks_like_code(code) or row.code_hash != hash_code(code):
        return "other"
    return "live"


async def resolve_org(admin_engine: AsyncEngine, code: str) -> UUID | None:
    """Which organisation this code belongs to -- or None.

    ADMIN ENGINE, READ-ONLY, and it only picks the tenant: exactly the role
    `services/enrollment.py::enroll` gives the token id. Whether the link may
    be USED is decided inside that tenant's own session by `live_link_for`.
    """
    if not looks_like_code(code):
        return None
    async with AsyncSession(admin_engine) as session:
        org_id: UUID | None = (
            await session.execute(
                select(InstallLink.org_id).where(InstallLink.code_hash == hash_code(code))
            )
        ).scalar_one_or_none()
    return org_id


async def live_link_for(
    session: AsyncSession,
    org_id: UUID,
    *,
    code: str,
    source: IPAddress | None,
    now: datetime,
    for_update: bool = False,
) -> InstallLink | None:
    """The link, if it may be used right now by a request from `source`."""
    row = await _row(session, org_id, for_update=for_update)
    if row is None or row.code_hash != hash_code(code) or not is_live(row, now):
        return None
    if not address_in(source, row.networks):
        return None
    return row


async def resolve_live(
    admin_engine: AsyncEngine,
    app_engine: AsyncEngine,
    *,
    code: str,
    source: IPAddress | None,
    now: datetime,
) -> UUID | None:
    org_id = await resolve_org(admin_engine, code)
    if org_id is None:
        return None
    async with org_session(org_id, engine=app_engine) as session:
        row = await live_link_for(session, org_id, code=code, source=source, now=now)
    return org_id if row is not None else None


async def switch_off_due(session: AsyncSession, org_id: UUID, now: datetime) -> bool:
    """The worker beat's half of the automatic switch-off: record it."""
    row = await _row(session, org_id, for_update=True)
    if row is None or not row.enabled or row.disable_at is None or row.disable_at > now:
        return False
    disable_at = row.disable_at
    row.enabled = False
    row.enabled_at = None
    # Cleared, not kept (final review M2): a date in the past would refuse
    # every later save that echoes it back ("must be in the future") until
    # someone emptied the field. The audit row below keeps the value.
    row.disable_at = None
    row.updated_at = now
    await session.flush()
    await _audit(
        session,
        org_id,
        None,
        "enrollment.link_disabled",
        {"reason": "scheduled", "disable_at": disable_at.isoformat()},
    )
    return True
