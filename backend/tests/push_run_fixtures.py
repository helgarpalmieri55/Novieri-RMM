"""Seed helpers for the subnet push run tests
(docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md §1).

Built on `tests/agent_push_fixtures.py` and on the ADMIN engine, for the same
reason that module gives: the code under test must never set up its own world.
`FakeVault` counts decrypts, so a test can say "nothing was decrypted" as a
number rather than as a hope.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.signing import LocalEd25519Signer
from app.jobs.engine import JobEngine
from tests.agent_push_fixtures import (
    PushOrg,
    seed_agent_release,
    seed_asset,
    seed_confirmed_host_key,
    seed_credential,
    seed_org,
    seed_push_authorization,
    seed_push_credential_scope,
    seed_push_site_server,
)
from tests.ipam_fixtures import admin_engine_of

CONSOLE = "https://itms.example.com"
CRED_JSON = json.dumps({"username": "svc-push", "secret": "hunter2"})


class FakeVault:
    """`checkout` returns the credential's plaintext JSON and counts calls.
    `store` is what `PUT /credentials/{id}` calls to rotate a secret: it
    returns a fresh opaque ciphertext, as the real envelope vault does (a new
    nonce on every store), and `checkout` returns the rotated plaintext."""

    def __init__(self, plaintext: str = CRED_JSON) -> None:
        self.plaintext = plaintext
        self.calls = 0
        self.stored = 0

    async def checkout(self, session: Any, *, org_id: UUID, credential_id: UUID) -> str:
        self.calls += 1
        return self.plaintext

    async def store(
        self, session: Any, *, org_id: UUID, credential_id: UUID, plaintext: str
    ) -> Any:
        import base64
        import os

        from app.core.vault import StoredSecret

        self.stored += 1
        self.plaintext = plaintext
        return StoredSecret(
            ciphertext_b64=base64.b64encode(os.urandom(48)).decode(),
            wrapped_dek=base64.b64encode(os.urandom(44)).decode(),
            key_version=1,
        )


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, int]] = []

    def presign_agent_release_get(
        self, version: str, os_name: str, arch: str, *, expires_in: int
    ) -> str:
        self.calls.append((version, os_name, arch, expires_in))
        return f"https://store.test/{version}/{os_name}/{arch}?sig=x&expires={expires_in}"


def job_engine(tmp_path: Path) -> JobEngine:
    return JobEngine(signer=LocalEd25519Signer(tmp_path / "job-signing.key"))


def make_dispatcher(
    tmp_path: Path, *, vault: FakeVault | None = None, store: FakeStore | None = None
) -> Any:
    from app.modules.agent_push.dispatch import RunDispatcher

    chosen_store = store or FakeStore()
    return RunDispatcher(
        job_engine(tmp_path),
        lambda: chosen_store,  # type: ignore[arg-type, return-value]
        vault or FakeVault(),  # type: ignore[arg-type]
        public_console_url=CONSOLE,
    )


async def grant_org_admin(env_or_engine: Any, org: PushOrg) -> None:
    """`org-admin` holds `agent.push.run` and `credentials.checkout`."""
    from app.models import Role, UserRole

    engine = admin_engine_of(env_or_engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        role = (
            await session.execute(
                select(Role).where(Role.name == "org-admin", Role.org_id.is_(None))
            )
        ).scalar_one()
        session.add(UserRole(org_id=org.org_id, user_id=org.user_id, role_id=role.id))
        await session.commit()


@dataclass(frozen=True)
class RunWorld:
    org: PushOrg
    credential_id: UUID
    prefix: str
    addresses: tuple[str, ...]
    device_ids: tuple[UUID, ...]
    site_server_device_id: UUID


async def run_world(
    env_or_engine: Any,
    tag: str,
    *,
    octet: int,
    count: int,
    confirmed_keys: bool = True,
    with_release: bool = True,
    architecture: str = "amd64",
) -> RunWorld:
    """An org in which every bound holds for `count` Linux machines on
    `10.<octet>.0.0/24`, starting at `.10`. The org's user is an org-admin."""
    engine: AsyncEngine = admin_engine_of(env_or_engine)
    org = await seed_org(engine, tag)
    await grant_org_admin(engine, org)
    credential_id = await seed_credential(engine, org)
    prefix = f"10.{octet}.0.0/24"
    await seed_push_authorization(engine, org, prefix)
    await seed_push_credential_scope(engine, org, credential_id, prefix)
    _ss, ss_device = await seed_push_site_server(engine, org.org_id, segments=[prefix])
    if with_release:
        await seed_agent_release(engine, os_name="linux", arch="amd64")
    addresses: list[str] = []
    device_ids: list[UUID] = []
    for index in range(count):
        address = f"10.{octet}.0.{10 + index}"
        device_ids.append(
            await seed_asset(
                engine, org.org_id, primary_ip=address, architecture=architecture
            )
        )
        if confirmed_keys:
            await seed_confirmed_host_key(engine, org, address=address)
        addresses.append(address)
    return RunWorld(
        org=org,
        credential_id=credential_id,
        prefix=prefix,
        addresses=tuple(addresses),
        device_ids=tuple(device_ids),
        site_server_device_id=ss_device,
    )


async def start_run(app_engine: AsyncEngine, world: RunWorld) -> UUID:
    """Expand, then confirm exactly what was shown, as a person would. Commits
    the run and dispatches NOTHING (no pacer is called here)."""
    from app.core.tenancy import org_session
    from app.modules.agent_push import run_list, runs

    async with org_session(
        world.org.org_id, engine=app_engine, clients=ClientScope.all()
    ) as session:
        listed = await run_list.expand_prefix(
            session,
            world.org.org_id,
            actor_id=world.org.user_id,
            prefix=world.prefix,
            credential_id=world.credential_id,
            public_console_url=CONSOLE,
        )
        run = await runs.create_run(
            session,
            world.org.org_id,
            actor_id=world.org.user_id,
            prefix=world.prefix,
            credential_id=world.credential_id,
            list_digest=listed.list_digest,
            public_console_url=CONSOLE,
        )
        return run.id
