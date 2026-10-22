"""Provisioning registry service (docs/modules/provisioning/spec.md §4/§7).

Owns the org-scoped `OsImage` catalog and the `ProvisionProfile` policy
templates that drive a `provision.enroll` job. A profile's `post_config` can
carry domain-join / local-admin credentials the freshly-imaged device needs
to apply post-boot; those two fields are the only ones this module knows how
to seal, and they are sealed through the vault before the row is ever
flushed to disk — the app database only ever holds a `{"__sealed__": ...}`
reference, never the plaintext (invariant #3). Mirrors the flush-before-seal
ordering in `app.modules.backup.repository.create_repository`: the profile is
flushed first so its uuid7 `id` exists and can be used as the vault
`key_id` (AAD-binding the sealed secret to this exact profile row), then the
credential fields are sealed and the JSON is overwritten before the final
flush/commit. The credential plaintext is never logged, never printed, and
never placed in audit metadata.
"""

import copy
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.vault import VaultService
from app.models import OsImage, ProvisionProfile

SEALED_MARKER = "__sealed__"

# (os_family, kind) pairs that create_image accepts; anything else is rejected.
_VALID_KIND_BY_OS_FAMILY: dict[str, str] = {
    "windows": "image",
    "linux": "build",
    "macos": "enrollment",
}

# post_config paths (as (outer_key, inner_key) tuples) that hold credential
# plaintext and must be sealed before the profile is persisted.
_CREDENTIAL_PATHS: tuple[tuple[str, str], ...] = (
    ("domain_join", "password"),
    ("local_admin", "password"),
)


class ProvisioningValidationError(Exception):
    pass


class ProvisioningPermissionError(Exception):
    """The caller holds `provisioning.run` but not something a specific option
    additionally requires (maps to HTTP 403, not 422).

    Deliberately NOT a subclass of `ProvisioningValidationError`: the route maps
    that one to 422, and "your request was malformed" is the wrong answer to
    "you may not do this". Today its only use is `wake_on_lan`, which
    additionally requires `site.wake` -- `docs/modules/site-server/spec.md` §10.2
    separates deciding-what-a-site-server-is from waking-a-machine on purpose,
    and letting `provisioning.run` confer LAN-wide wake for free would collapse
    that separation through a side door.
    """


async def create_image(
    session: AsyncSession,
    *,
    org_id: UUID,
    os_family: str,
    name: str,
    kind: str,
    params: dict[str, Any],
    artifact_ref: str | None = None,
    artifact_sha256: str | None = None,
    actor_id: UUID,
    actor_type: Literal["user", "agent", "system"] = "user",
) -> OsImage:
    expected_kind = _VALID_KIND_BY_OS_FAMILY.get(os_family)
    if expected_kind is None or kind != expected_kind:
        raise ProvisioningValidationError(
            tr(
                "invalid (os_family, kind) pairing: ({os_family}, {kind})",
                os_family=repr(os_family),
                kind=repr(kind),
            )
        )

    image = OsImage(
        org_id=org_id,
        os_family=os_family,
        name=name,
        kind=kind,
        artifact_ref=artifact_ref,
        artifact_sha256=artifact_sha256,
        params=params,
        state="active",
    )
    session.add(image)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action="provisioning.image_created",
        target_type="os_image",
        target_id=str(image.id),
        metadata={"name": name, "os_family": os_family, "kind": kind},
    )

    return image


async def list_images(session: AsyncSession, org_id: UUID) -> list[OsImage]:
    return list(
        (
            await session.execute(select(OsImage).where(OsImage.org_id == org_id))
        )
        .scalars()
        .all()
    )


async def get_image(session: AsyncSession, org_id: UUID, image_id: UUID) -> OsImage | None:
    return (
        await session.execute(
            select(OsImage).where(OsImage.org_id == org_id, OsImage.id == image_id)
        )
    ).scalar_one_or_none()


def _is_sealed_marker(value: Any) -> bool:
    return isinstance(value, dict) and set(value.keys()) == {SEALED_MARKER}


async def _seal_credentials(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    profile_id: UUID,
    post_config: dict[str, Any],
) -> dict[str, Any]:
    """Return a deep copy of post_config with any plaintext credential fields
    replaced by `{SEALED_MARKER: <sealed-str>}`. Values already sealed (e.g. a
    reloaded profile being re-saved) are left untouched — no double-sealing."""
    sealed_config = copy.deepcopy(post_config)
    for outer_key, inner_key in _CREDENTIAL_PATHS:
        outer = sealed_config.get(outer_key)
        if not isinstance(outer, dict):
            continue
        value = outer.get(inner_key)
        if value is None:
            continue
        if _is_sealed_marker(value):
            continue  # already-sealed marker dict → preserve as-is (idempotent)
        if not isinstance(value, str):
            raise ProvisioningValidationError(
                tr(
                    "credential field {field} must be a plaintext string or a sealed marker",
                    field=f"{outer_key}.{inner_key}",
                )
            )
        sealed = await vault.seal_bytes(
            session, org_id=org_id, key_id=profile_id, plaintext=value.encode()
        )
        outer[inner_key] = {SEALED_MARKER: sealed}
    return sealed_config


def _preserve_unresupplied_credentials(
    *, sealed_post_config: dict[str, Any], old_post_config: dict[str, Any]
) -> None:
    """Mutate `sealed_post_config` in place so an edit that keeps a
    credential's outer container (e.g. `domain_join`) but does not supply a
    new plaintext password there carries forward the previously-sealed
    marker from `old_post_config`, instead of silently wiping it.

    If the incoming config drops the outer container entirely, that is
    treated as an intentional removal and the credential is NOT resurrected.
    """
    for outer_key, inner_key in _CREDENTIAL_PATHS:
        new_outer = sealed_post_config.get(outer_key)
        old_outer = old_post_config.get(outer_key)
        if not isinstance(new_outer, dict) or not isinstance(old_outer, dict):
            continue
        if new_outer.get(inner_key) is not None:
            continue  # operator supplied a new value (plaintext or marker) — keep it
        old_value = old_outer.get(inner_key)
        if _is_sealed_marker(old_value):
            new_outer[inner_key] = old_value


async def upsert_profile(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    name: str,
    image_id: UUID,
    post_config: dict[str, Any],
    target_scope: dict[str, Any],
    enabled: bool,
    actor_id: UUID,
    actor_type: Literal["user", "agent", "system"] = "user",
    profile_id: UUID | None = None,
) -> ProvisionProfile:
    old_post_config: dict[str, Any] | None = None
    if profile_id is not None:
        profile = (
            await session.execute(
                select(ProvisionProfile).where(
                    ProvisionProfile.org_id == org_id, ProvisionProfile.id == profile_id
                )
            )
        ).scalar_one_or_none()
        if profile is None:
            raise ProvisioningValidationError(
                tr("profile {profile_id} not found", profile_id=profile_id)
            )
        old_post_config = profile.post_config
        profile.name = name
        profile.image_id = image_id
        profile.target_scope = target_scope
        profile.enabled = enabled
    else:
        profile = ProvisionProfile(
            org_id=org_id,
            name=name,
            image_id=image_id,
            post_config={},
            target_scope=target_scope,
            enabled=enabled,
        )
        session.add(profile)
        await session.flush()  # populate profile.id (uuid7) before it is used as the seal key_id

    sealed_post_config = await _seal_credentials(
        session, vault, org_id=org_id, profile_id=profile.id, post_config=post_config
    )
    if old_post_config is not None:
        _preserve_unresupplied_credentials(
            sealed_post_config=sealed_post_config, old_post_config=old_post_config
        )
    profile.post_config = sealed_post_config
    session.add(profile)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type=actor_type,
        actor_id=actor_id,
        action="provisioning.profile_upserted",
        target_type="provision_profile",
        target_id=str(profile.id),
        metadata={"name": name, "image_id": str(image_id)},
    )

    return profile


async def list_profiles(session: AsyncSession, org_id: UUID) -> list[ProvisionProfile]:
    return list(
        (
            await session.execute(
                select(ProvisionProfile).where(ProvisionProfile.org_id == org_id)
            )
        )
        .scalars()
        .all()
    )


async def get_profile(
    session: AsyncSession, org_id: UUID, profile_id: UUID
) -> ProvisionProfile | None:
    return (
        await session.execute(
            select(ProvisionProfile).where(
                ProvisionProfile.org_id == org_id, ProvisionProfile.id == profile_id
            )
        )
    ).scalar_one_or_none()
