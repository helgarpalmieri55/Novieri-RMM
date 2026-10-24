"""The install link: one shared, switchable enrollment link per SITE.

docs/superpowers/specs/2026-09-28-install-link-and-windows-push-design.md §1.

WHAT THE LINK IS. A 32-character code that, while the link is on and the
request comes from a listed network, lets a machine enrol into THIS
organisation -- as a device waiting for review (§1.5). It is the only
authorization for its own enrollment, exactly as an enrollment token is (§0,
invariant 6), and it is handled like one: generated here, hashed, stored only as
the hash, and shown once.

ONE LINK PER SITE, NOT PER ORGANISATION (MSP clients spec §1.5, plan Task 7).
`uq_install_links_site` replaced `uq_install_links_org` in migration 0231, the
link carries its site's client and site, and a machine that joins through it
lands in them. Everything here is addressed by `site_id`; a site the session
cannot see is `client_sites.SiteNotFound`, which the route answers with a 404,
and an archived site or client takes no new settings (`NotAcceptingNewRows`,
a 409) -- switching a link on would let machines join a site nobody offers.

THE ALLOWED NETWORKS ARE THE SITE'S `egress_networks` -- the public addresses
that office leaves the Internet from -- not a list of the link's own. The
settings route still takes `networks` -- optional, and absent means the site's
stay as they are -- and writes them to the site. The link's
own `install_links.networks` column stays in the table and NOTHING READS IT any
more: `save` still writes it, as a copy of what it wrote to the site, only
because migration 0170's `ck_install_links_on_needs_code_and_network` refuses a
switched-on link without one. Dropping the column and that check is on the
phase-2 cleanup list.

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
from app.core.client_scope import ClientScope
from app.core.i18n import tr
from app.core.tenancy import org_session
from app.models import Device, InstallLink, Site
from app.services.client_sites import (
    NotAcceptingNewRows,
    SiteNotFound,
    refuse_if_closed,
    site_and_client_status,
    site_for_new_rows,
)

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


@dataclass(frozen=True)
class LinkTarget:
    """Where a code enrols a machine: its link's organisation, client and site."""

    org_id: UUID
    client_id: UUID
    site_id: UUID


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


async def _site(session: AsyncSession, site_id: UUID) -> Site:
    """The site, as the session sees it, for a READ -- RLS hides another
    organisation's and a client outside the session's scope, and both are
    `SiteNotFound`. A write takes `client_sites.site_for_new_rows` instead,
    which also refuses an archived site or client."""
    site = (
        await session.execute(select(Site).where(Site.id == site_id))
    ).scalar_one_or_none()
    if site is None:
        raise SiteNotFound(site_id)
    return site


async def _row(
    session: AsyncSession, site_id: UUID, *, for_update: bool = False
) -> InstallLink | None:
    query = select(InstallLink).where(InstallLink.site_id == site_id)
    if for_update:
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


async def _locked_row(session: AsyncSession, site: Site, actor_id: UUID | None) -> InstallLink:
    """This site's row, created if missing, and locked FOR UPDATE.

    INSERT ... ON CONFLICT DO NOTHING first, so two first-ever saves racing
    each other cannot both INSERT: the second waits on the first's row and
    then locks it, instead of dying on `uq_install_links_site` with a 500.
    The row takes its site's client: `fk_install_links_site_client` holds the
    pair together.
    """
    await session.execute(
        pg_insert(InstallLink)
        .values(
            org_id=site.org_id,
            client_id=site.client_id,
            site_id=site.id,
            created_by=actor_id,
            networks=[],
        )
        .on_conflict_do_nothing(index_elements=["site_id"])
    )
    row = await _row(session, site.id, for_update=True)
    if row is None:  # pragma: no cover - the INSERT above guarantees a row
        raise RuntimeError(  # i18n-exempt: internal invariant, unhandled 500
            "install_links row missing after insert"
        )
    return row


async def _joined_since(session: AsyncSession, site_id: UUID, since: datetime) -> int:
    count = (
        await session.execute(
            select(func.count())
            .select_from(Device)
            .where(Device.site_id == site_id, Device.link_joined_at >= since)
        )
    ).scalar_one()
    return int(count)


def _networks(site: Site) -> list[str]:
    return [str(n) for n in site.egress_networks]


async def view(session: AsyncSession, site_id: UUID, now: datetime) -> LinkView:
    site = await _site(session, site_id)
    row = await _row(session, site_id)
    if row is None:
        return LinkView(False, False, _networks(site), None, DEFAULT_HOURLY_LIMIT, None, 0)
    live = is_live(row, now)
    joined = (
        await _joined_since(session, site_id, row.enabled_at)
        if live and row.enabled_at is not None
        else 0
    )
    return LinkView(
        enabled=live,
        has_code=row.code_hash is not None,
        networks=_networks(site),
        disable_at=row.disable_at,
        hourly_limit=row.hourly_limit,
        enabled_at=row.enabled_at if live else None,
        joined_since_enabled=joined,
    )


async def _audit(
    session: AsyncSession,
    link: InstallLink,
    actor_id: UUID | None,
    action: str,
    metadata: dict[str, Any],
) -> None:
    await audit_service.record(
        session,
        org_id=link.org_id,
        actor_type="user" if actor_id is not None else "system",
        actor_id=actor_id,
        action=action,
        target_type="install_link",
        # The SITE: one link per site. The organisation's Principal site has
        # the organisation's id (plan Global Constraints), which is what this
        # field held when there was one link per organisation.
        target_id=str(link.site_id),
        metadata=metadata,
    )


async def save(
    session: AsyncSession,
    *,
    org_id: UUID,
    site_id: UUID,
    actor_id: UUID,
    enabled: bool,
    networks: list[str] | None,
    disable_at: datetime | None,
    hourly_limit: int,
    now: datetime,
    trusted_proxies: Sequence[IPNetwork] = (),
) -> str | None:
    """Write the settings. Returns the code if this call minted one, else None.

    `networks` ARE THE SITE'S `egress_networks`, and are written there: the
    link answers requests from the networks of the office it belongs to.
    `None` leaves them as they are -- switching a link off is not an
    instruction to forget where the office is.

    Validation happens BEFORE the row is touched, so a refused save changes
    nothing and audits nothing.
    """
    # FOR NO KEY UPDATE: the networks are written to the site's own row.
    site = await site_for_new_rows(session, org_id=org_id, site_id=site_id, for_update=True)
    nets = (
        _networks(site)
        if networks is None
        else normalise_networks(networks, trusted_proxies=trusted_proxies)
    )
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
                "at least one network is required to switch on the install link of the site "
                "{site}: list the networks its computers reach the Internet from; an empty list "
                "would not mean 'anywhere'",
                site=site.name,
            )
        )

    row = await _locked_row(session, site, actor_id)
    was_on = is_live(row, now)
    before = {
        "networks": _networks(site),
        "disable_at": row.disable_at.isoformat() if row.disable_at else None,
        "hourly_limit": row.hourly_limit,
    }

    minted: str | None = None
    if enabled and row.code_hash is None:
        minted = new_code()
        row.code_hash = hash_code(minted)
    if _networks(site) != nets:
        site.egress_networks = nets
    # A copy nothing reads; 0170's CHECK requires it of a switched-on link
    # (module docstring).
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
        await _audit(session, row, actor_id, "enrollment.link_created", {})
    changed = {k: {"before": before[k], "after": after[k]} for k in after if before[k] != after[k]}
    if changed:
        await _audit(session, row, actor_id, "enrollment.link_settings_changed", changed)
    if enabled and not was_on:
        await _audit(session, row, actor_id, "enrollment.link_enabled", {"networks": nets})
    if was_on and not enabled:
        await _audit(session, row, actor_id, "enrollment.link_disabled", {"reason": "operator"})
    return minted


async def regenerate(
    session: AsyncSession, *, org_id: UUID, site_id: UUID, actor_id: UUID, now: datetime
) -> str:
    """A new code; the old one stops working the moment this commits."""
    site = await site_for_new_rows(session, org_id=org_id, site_id=site_id)
    row = await _locked_row(session, site, actor_id)
    code = new_code()
    row.code_hash = hash_code(code)
    row.updated_at = now
    await session.flush()
    await _audit(
        session, row, actor_id, "enrollment.link_regenerated", {"enabled": bool(row.enabled)}
    )
    return code


async def code_state(session: AsyncSession, site_id: UUID, *, code: str, now: datetime) -> str:
    """What `code` is to this site's link right now, for an ADMINISTRATOR's
    request (the Group Policy MSI, GPO MSI spec 2026-09-30 §2.2): `live` -- it
    is the current code and the link is on; `off` -- the link is off, or has
    no code; `other` -- the link is on and this is not its code.

    Only the hash is compared, as everywhere else: the code itself is never
    stored. The allowed networks are NOT checked here -- this answers an
    authenticated administrator in the console, not a machine joining.

    AN ARCHIVED SITE, OR A SITE OF AN ARCHIVED CLIENT, IS REFUSED
    (`NotAcceptingNewRows`, a 409) whatever its link says (MSP clients Task 9):
    an MSI built for it would carry a link that enrols nothing.
    """
    found = await site_and_client_status(session, site_id)
    if found is None:  # a site the session cannot see
        raise SiteNotFound(site_id)
    refuse_if_closed(*found)
    row = await _row(session, site_id)
    if row is None or not is_live(row, now):
        return "off"
    if not looks_like_code(code) or row.code_hash != hash_code(code):
        return "other"
    return "live"


async def resolve_target(admin_engine: AsyncEngine, code: str) -> LinkTarget | None:
    """Which organisation, client and site this code belongs to -- or None.

    ADMIN ENGINE, READ-ONLY, and it only picks the tenant and the client the
    session is then opened on: exactly the role `services/enrollment.py::
    enroll` gives the token id. Whether the link may be USED is decided inside
    that client's own session by `live_link_for`.
    """
    if not looks_like_code(code):
        return None
    async with AsyncSession(admin_engine) as session:
        found = (
            await session.execute(
                select(InstallLink.org_id, InstallLink.client_id, InstallLink.site_id).where(
                    InstallLink.code_hash == hash_code(code)
                )
            )
        ).one_or_none()
    if found is None:
        return None
    return LinkTarget(org_id=found.org_id, client_id=found.client_id, site_id=found.site_id)


async def live_link_for(
    session: AsyncSession,
    site_id: UUID,
    *,
    code: str,
    source: IPAddress | None,
    now: datetime,
    for_update: bool = False,
) -> InstallLink | None:
    """The link, if it may be used right now by a request from `source`: one
    of its site's egress networks.

    NOT FOR AN ARCHIVED SITE OR CLIENT (MSP clients Task 9), even a link that
    is still switched on because it predates the archive: the answer is the
    same None -- a 404 at the door -- as for a link that is off."""
    row = await _row(session, site_id, for_update=for_update)
    if row is None or row.code_hash != hash_code(code) or not is_live(row, now):
        return None
    found = await site_and_client_status(session, site_id)
    if found is None:
        return None
    site, client_status = found
    try:
        refuse_if_closed(site, client_status)
    except NotAcceptingNewRows:
        return None
    if not address_in(source, site.egress_networks):
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
    """The organisation whose live link this code is, for a request from
    `source` -- or None. The session sees the link's client alone."""
    target = await resolve_target(admin_engine, code)
    if target is None:
        return None
    async with org_session(
        target.org_id, engine=app_engine, clients=ClientScope.only([target.client_id])
    ) as session:
        row = await live_link_for(session, target.site_id, code=code, source=source, now=now)
    return target.org_id if row is not None else None


async def switch_off_due(session: AsyncSession, org_id: UUID, now: datetime) -> int:
    """The worker beat's half of the automatic switch-off: record it, for
    every link of this organisation whose time has passed. Returns how many."""
    rows = (
        (
            await session.execute(
                select(InstallLink)
                .where(
                    InstallLink.org_id == org_id,
                    InstallLink.enabled.is_(True),
                    InstallLink.disable_at.is_not(None),
                    InstallLink.disable_at <= now,
                )
                .order_by(InstallLink.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        disable_at = row.disable_at
        assert disable_at is not None  # the query's own predicate
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
            row,
            None,
            "enrollment.link_disabled",
            {"reason": "scheduled", "disable_at": disable_at.isoformat()},
        )
    return len(rows)


async def lock_links(
    session: AsyncSession,
    *,
    org_id: UUID,
    client_id: UUID | None = None,
    site_id: UUID | None = None,
) -> list[InstallLink]:
    """Every link of one client or one site, locked FOR UPDATE.

    For archiving (MSP clients Task 9). TAKEN BEFORE THE ARCHIVE COUNTS
    DEVICES (review 1, concern 5): `enroll_via_link` holds its link's row lock
    (`live_link_for(..., for_update=True)`) until it commits its device, so
    the archive either waits for that device and counts it, or holds the lock
    first and the enrollment then reads the link switched off.
    """
    if (client_id is None) == (site_id is None):
        raise ValueError(  # i18n-exempt: caller contract, unhandled 500
            "lock_links takes exactly one of client_id / site_id"
        )
    query = select(InstallLink).where(InstallLink.org_id == org_id)
    if client_id is not None:
        query = query.where(InstallLink.client_id == client_id)
    else:
        query = query.where(InstallLink.site_id == site_id)
    rows = await session.execute(query.order_by(InstallLink.id).with_for_update())
    return list(rows.scalars())


async def switch_off(
    session: AsyncSession,
    *,
    actor_id: UUID,
    reason: str,
    now: datetime,
    links: Sequence[InstallLink],
) -> int:
    """Switch off the links `lock_links` returned that are on, each audited
    `enrollment.link_disabled` with `reason`. Returns how many.

    An archived site or client enrols nothing (`live_link_for` refuses it
    regardless); switching its links off as well leaves the console saying so
    rather than showing a link "on" that no machine can use. The code is kept:
    a switched-off link is not a regenerated one.
    """
    switched = 0
    for row in links:
        if not row.enabled:
            continue
        row.enabled = False
        row.enabled_at = None
        row.updated_at = now
        await session.flush()
        await _audit(session, row, actor_id, "enrollment.link_disabled", {"reason": reason})
        switched += 1
    return switched
