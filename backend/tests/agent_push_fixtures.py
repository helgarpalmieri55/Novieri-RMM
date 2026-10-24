"""Seed helpers shared by every `test_agent_push_*.py` module.

Deliberately THIN, and on the ADMIN (RLS-bypassing) engine: a test that
exercises `authorization.create_authorization` must never be set up by
`authorization.create_authorization`, or the fixture and the code under test
agree with each other and with nothing else. Same shape and same reasoning as
`tests/ipam_fixtures.py` and `tests/site_server_fixtures.py`, whose helpers
this module reuses rather than duplicating.

**THE CREDENTIAL SEED WRITES NO SECRET, DELIBERATELY.** Cycle B1 stores,
scopes and refuses; it never decrypts, because there is nothing yet for a
plaintext to be decrypted FOR (spec §14). A fixture that stashed a plaintext
here would be inventing the exposure B2 has to justify, one cycle early, in a
file no reviewer reads as security-relevant. `ciphertext` is left NULL, which
is the credential vault's own "metadata-only placeholder" state.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from tests.ipam_fixtures import admin_engine_of, utcnow

__all__ = [
    "PUSH_CREDENTIAL_TYPE",
    "PushOrg",
    "seed_agent_release",
    "CURRENT_PUSH_CAPABILITIES",
    "seed_push_site_server",
    "seed_asset",
    "seed_client",
    "seed_confirmed_host_key",
    "seed_credential",
    "seed_org",
    "seed_push_authorization",
    "seed_push_credential_scope",
]

PUSH_CREDENTIAL_TYPE = "push_install"

#: What a site server's agent from this release advertises for push install:
#: both job types, the WinRM transport and the enroll_url hand-off (review
#: Minor 1). A site server seeded without these is an OLDER agent.
CURRENT_PUSH_CAPABILITIES: dict[str, bool] = {
    "agent.push_probe": True,
    "agent.push_install": True,
    "agent.push_install.winrm": True,
    "agent.push_install.enroll_url": True,
}


@dataclass(frozen=True)
class PushOrg:
    """One org with the kernel rows every push test needs."""

    org_id: UUID
    user_id: UUID


async def seed_client(
    env_or_engine: Any, org_id: UUID, name: str | None = None
) -> tuple[UUID, UUID]:
    """A customer client and its one site, on the admin engine. Returns
    (client_id, site_id) -- for a test that needs a second client's devices."""
    from app.models import Client, Site

    engine: AsyncEngine = admin_engine_of(env_or_engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        client = Client(
            org_id=org_id, name=name or f"Cliente {uuid4().hex[:6]}", kind="customer"
        )
        session.add(client)
        await session.flush()
        site = Site(org_id=org_id, client_id=client.id, name="Principal")
        session.add(site)
        await session.commit()
        return client.id, site.id


async def seed_org(env_or_engine: Any, tag: str = "push") -> PushOrg:
    from app.models import Organization, User

    engine: AsyncEngine = admin_engine_of(env_or_engine)
    slug = f"{tag}-{uuid4().hex[:8]}"
    async with AsyncSession(engine, expire_on_commit=False) as session:
        org = Organization(name=slug, slug=slug)
        session.add(org)
        await session.flush()
        user = User(
            org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@test.local"
        )
        session.add(user)
        await session.commit()
        return PushOrg(org_id=org.id, user_id=user.id)


async def seed_asset(
    env_or_engine: Any,
    org_id: UUID,
    *,
    primary_ip: str | None,
    hostname: str | None = None,
    os_family: str = "linux",
    architecture: str = "amd64",
    source: str = "discovery",
    provenance: str = "promoted",
    fqdn: str | None = None,
    review_state: str = "approved",
    client_id: UUID | None = None,
    site_id: UUID | None = None,
) -> UUID:
    """A `devices` row (spec §3 condition 1).

    `primary_ip` is what makes it a push target at all, and it is passed
    explicitly rather than defaulted so a test can seed the case that matters
    most: an asset in this org with a DIFFERENT address, which is what
    distinguishes "the target is vouched for" from "the org has any asset at
    all".

    **`source` AND `provenance` ARE PARAMETERS, AND `provenance` IS THE ONE
    THAT MATTERS.** Since 2026-08-26 a completed `net.sweep` writes a `devices`
    row for every host it reported, with nobody selecting anything
    (`docs/modules/asset-inventory/spec.md` §3) -- so a fixture that could only
    produce vouched-for rows could not seed the case the first bound now exists
    to refuse. The pair must satisfy `ck_devices_source_provenance`, which is
    why both are settable and why the default pair is a valid one.

    `fqdn` is the DNS name a WinRM push authenticates to (`HTTP/<fqdn>`, design
    §2.3); `review_state` is the install-link review gate (PR 1 D9) the first
    bound also requires to be `approved`.

    `client_id`/`site_id` default to the internal client and its Principal
    site (id = org id); `seed_client` makes another client to name instead.
    """
    from app.models import Device

    engine = admin_engine_of(env_or_engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        device = Device(
            org_id=org_id,
            hostname=hostname or f"asset-{uuid4().hex[:8]}",
            os_family=os_family,
            architecture=architecture,
            primary_ip=primary_ip,
            source=source,
            provenance=provenance,
            fqdn=fqdn,
            review_state=review_state,
            client_id=org_id if client_id is None else client_id,
            site_id=org_id if site_id is None else site_id,
        )
        session.add(device)
        await session.commit()
        return device.id


async def seed_agent_release(
    env_or_engine: Any,
    *,
    os_name: str = "linux",
    arch: str = "amd64",
    version: str | None = None,
    sha256: str | None = None,
) -> UUID:
    """One published, digest-bearing `agent_releases` row (GLOBAL, no org_id).

    B2's install path selects the newest published release for the target's
    OS/arch server-side (spec §4.2); a push has nothing to install until one
    exists.
    """
    from app.models.core import AgentRelease

    engine = admin_engine_of(env_or_engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        # `agent_releases` is GLOBAL (no org_id) and committed, so rows persist
        # across tests; a unique version avoids the (version, os, arch) unique
        # constraint colliding with another test's release.
        release = AgentRelease(
            version=version or f"1.0.0-{uuid4().hex[:8]}",
            os=os_name,
            arch=arch,
            storage_ref=f"releases/{version}/{os_name}/{arch}",
            sha256=sha256 or ("a" * 64),
            signed=True,
            published_at=utcnow(),
        )
        session.add(release)
        await session.commit()
        return release.id


async def seed_confirmed_host_key(
    env_or_engine: Any,
    org: PushOrg,
    *,
    address: str,
    fingerprint: str = "SHA256:AAAAtestkeyfingerprintAAAA",
    device_id: UUID | None = None,
) -> None:
    """A `push_host_key` row observed AND confirmed for `address` -- the state an
    install requires (spec §7.3). Written on the admin engine so the test that
    exercises confirmation is never set up by confirmation."""
    from app.modules.agent_push.models import PushHostKey

    engine = admin_engine_of(env_or_engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(
            PushHostKey(
                org_id=org.org_id,
                device_id=device_id,
                address=address,
                transport="ssh",
                observed_fingerprint=fingerprint,
                observed_key_type="ssh-ed25519",
                observed_at=utcnow(),
                confirmed_fingerprint=fingerprint,
                confirmed_at=utcnow(),
                confirmed_by=org.user_id,
            )
        )
        await session.commit()


async def seed_credential(
    env_or_engine: Any,
    org: PushOrg,
    *,
    credential_type: str = PUSH_CREDENTIAL_TYPE,
    name: str | None = None,
    realm: str | None = "CORP.EXAMPLE.COM",
) -> UUID:
    """Credential METADATA only. See the module docstring on why no secret.

    A `windows_domain` credential also gets the realm its save would have
    recorded (review I1) unless `realm=None` -- one saved before realms were.
    """
    from app.models import Credential
    from app.modules.agent_push.models import PushCredentialRealm

    engine = admin_engine_of(env_or_engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        credential = Credential(
            org_id=org.org_id,
            name=name or f"push-{uuid4().hex[:8]}",
            type=credential_type,
            vault_ref="",
            created_by=org.user_id,
        )
        session.add(credential)
        await session.flush()
        if credential_type == "windows_domain" and realm is not None:
            session.add(
                PushCredentialRealm(
                    org_id=org.org_id, credential_id=credential.id, realm=realm
                )
            )
        await session.commit()
        return credential.id


async def seed_push_authorization(
    env_or_engine: Any,
    org: PushOrg,
    prefix: str,
    *,
    revoked: bool = False,
    expires_at: Any = None,
) -> UUID:
    """One `push_authorization`, written straight through the admin engine.

    `revoked` and `expires_at` exist because the refusal tests need a fixture
    in which the guarded and the unguarded path produce DIFFERENT observable
    values: a test that seeds nothing and asserts a refusal passes against a
    function that refuses everything and against one that never looks.

    **THE STAMPS ARE COMPUTED IN PYTHON, NOT IN A SQL `CASE`.** asyncpg deduces
    each parameter's type from every use site, and a bind used both as a plain
    `uuid` and inside a `CASE ... ELSE NULL` deduces `text` and fails at
    execute time against a `uuid` column. Two literals here beat one clever
    statement.
    """
    engine = admin_engine_of(env_or_engine)
    row_id = uuid4()
    stamp = utcnow() if revoked else None
    async with AsyncSession(engine) as session:
        await session.execute(
            text(
                "INSERT INTO push_authorization"
                " (id, org_id, prefix, note, authorized_by, authorized_at,"
                "  expires_at, revoked_at, revoked_by)"
                " VALUES (:id, :org, CAST(:prefix AS cidr), '', :actor, now(),"
                "         :expires, :revoked_at, :revoked_by)"
            ),
            {
                "id": row_id,
                "org": org.org_id,
                "prefix": prefix,
                "actor": org.user_id,
                "expires": expires_at,
                "revoked_at": stamp,
                "revoked_by": org.user_id if revoked else None,
            },
        )
        await session.commit()
    return row_id


async def seed_push_credential_scope(
    env_or_engine: Any,
    org: PushOrg,
    credential_id: UUID,
    prefix: str,
    *,
    unbound: bool = False,
) -> UUID:
    """One `push_credential_scope`. See `seed_push_authorization` on the
    stamps."""
    engine = admin_engine_of(env_or_engine)
    row_id = uuid4()
    stamp = utcnow() if unbound else None
    async with AsyncSession(engine) as session:
        await session.execute(
            text(
                "INSERT INTO push_credential_scope"
                " (id, org_id, credential_id, prefix, note, bound_by, bound_at,"
                "  unbound_at, unbound_by)"
                " VALUES (:id, :org, :cred, CAST(:prefix AS cidr), '', :actor, now(),"
                "         :unbound_at, :unbound_by)"
            ),
            {
                "id": row_id,
                "org": org.org_id,
                "cred": credential_id,
                "prefix": prefix,
                "actor": org.user_id,
                "unbound_at": stamp,
                "unbound_by": org.user_id if unbound else None,
            },
        )
        await session.commit()
    return row_id


async def seed_push_site_server(
    env_or_engine: Any,
    org_id: UUID,
    *,
    segments: list[str],
    capabilities: dict[str, bool] | None = None,
    enabled: bool = True,
    client_id: UUID | None = None,
    site_id: UUID | None = None,
) -> tuple[UUID, UUID]:
    """A site-server device, its agent advertising `capabilities` (the current
    release's by default), and the `site_server` row. Returns (site_server_id,
    device_id). The device is in the internal client unless `client_id` and
    `site_id` name another (the row inherits its client from the device)."""
    from tests.ipam_fixtures import seed_agent, seed_device
    from tests.site_server_fixtures import seed_site_server

    placed: dict[str, UUID] = {}
    if client_id is not None:
        placed = {"client_id": client_id, "site_id": site_id or client_id}
    device_id = await seed_device(env_or_engine, org_id, **placed)
    await seed_agent(
        env_or_engine,
        org_id,
        device_id,
        capabilities=CURRENT_PUSH_CAPABILITIES if capabilities is None else capabilities,
    )
    site_server_id = await seed_site_server(
        env_or_engine, org_id, device_id=device_id, segments=segments, enabled=enabled
    )
    return site_server_id, device_id
