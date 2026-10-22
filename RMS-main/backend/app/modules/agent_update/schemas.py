"""API bodies for agent self-update rollouts (spec-self-update.md §8.3)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RingIn(_In):
    name: str = Field(min_length=1, max_length=100)
    device_group_id: UUID | None = None
    is_pilot: bool = False


class RolloutIn(_In):
    target_version: str = Field(min_length=1, max_length=64)
    is_rollback: bool = False
    reason: str = Field(default="", max_length=500)
    rings: list[RingIn] = Field(min_length=1, max_length=10)
    batch_size: int = Field(default=10, ge=1, le=100)
    failure_threshold_pct: int = Field(default=5, ge=0, le=100)
    health_window_s: int = Field(default=600, ge=120, le=3600)


class RingPreviewOut(BaseModel):
    ordinal: int
    name: str
    is_pilot: bool
    device_group_id: UUID | None
    total: int
    eligible: int
    needs_manual_update: int
    no_artifact: int
    already_current: int
    other_skipped: int


class PreviewOut(BaseModel):
    target_version: str
    is_rollback: bool
    channel: str | None
    rings: list[RingPreviewOut]


class RingOut(BaseModel):
    id: UUID
    ordinal: int
    name: str
    is_pilot: bool
    device_group_id: UUID | None
    state: str
    activated_at: datetime | None
    counts: dict[str, int]


class RolloutOut(BaseModel):
    id: UUID
    target_version: str
    is_rollback: bool
    state: str
    batch_size: int
    failure_threshold_pct: int
    health_window_s: int
    reason: str
    pause_reason: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    rings: list[RingOut] = Field(default_factory=list)


class TargetOut(BaseModel):
    id: UUID
    ring_id: UUID
    device_id: UUID
    hostname: str | None
    from_version: str
    state: str
    reason: str
    attempts: int
    job_id: UUID | None
    updated_at: datetime


class ManualUpdateDeviceOut(BaseModel):
    device_id: UUID
    hostname: str | None
    version: str
    reason: Literal["version_unknown", "no_agent_update"]


class FleetVersionsOut(BaseModel):
    total: int
    versions: dict[str, int]
    latest_stable: str | None
    update_available: int
    needs_manual_update: list[ManualUpdateDeviceOut]


class PromoteIn(_In):
    channel: Literal["pilot", "stable"] = "stable"


class PromoteOut(BaseModel):
    version: str
    channel: str
    rows: int


class ReleaseSignatureIn(_In):
    release_signature: str = Field(
        min_length=88,
        max_length=88,
        pattern=r"^[A-Za-z0-9+/]{86}==$",
        description="Base64 Ed25519 signature from `release-sign`.",
    )
