"""Own authentication: a person's credentials, sessions and their bookkeeping,
and an organization's session and account-mail settings (migration 0245; design
`docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md` §6.1).

NOTHING HERE HOLDS A SECRET IN CLEAR. `AuthPassword.hash` is argon2id over an
HMAC with the OpenBao pepper (`app.core.auth.passwords`); `AuthTotp.
secret_sealed` is sealed by the vault, bound to the user id
(`app.core.vault.seal_bytes`); the account-mail relay's password is the
vault secret of `AccountMailSettings.credential_id`, never a column; every
`token_hash` is a 32-byte digest (SHA-256, or HMAC-SHA256 with the pepper) of
a random value the person holds, and a recovery code's `code_hash` is its
salted argon2id hash in the encoded form (NIST SP 800-63B-4 §3.1.2.2) --
CHECKs in 0245 hold both lengths.

A PERSON'S ROWS FOLLOW THEIR `users` ROW (catalog kind O, parent `users`
through `user_id`): `client_id` is copied by the `its_client_from_parent`
trigger -- NULL for a technician, the contact's client for a contact -- so it
is never sent and the INSERT reads it back. A technician's rows are therefore
GLOBAL rows: read under any pinned scope, written only under scope all
(`app.core.global_rows`). `AuthOrgSettings` and `AccountMailSettings` are an
organization's configuration and carry no client (G).

Before the organization is known nothing here can be read at all (forced
RLS); the two SECURITY DEFINER functions of 0245, `its_auth_principal` and
`its_auth_session`, are the only way in.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    FetchedValue,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, client_org_fk, uuid7_pk

#: `auth_sessions.kind`: who the session admits (design §3: a contact never
#: passes the console's door).
SESSION_KINDS = ("staff", "contact")
#: `auth_failures.factor`: counted per factor, never only per address (§2.3).
FACTORS = ("password", "totp", "passkey", "recovery")
#: `auth_action_tokens.purpose`: the three single-use links (§2.8, §7).
ACTION_PURPOSES = ("invite", "reset", "recover")
#: `auth_webauthn_challenges.purpose`.
CHALLENGE_PURPOSES = ("register", "authenticate", "step_up")


def _client_id() -> Mapped[UUID | None]:
    """The trigger's: copied from `users.client_id`, never sent (module docstring)."""
    return mapped_column(
        ForeignKey("clients.id"),
        index=True,
        server_default=FetchedValue(),
        # An UPDATE of `user_id` lets the trigger change it: expired, not stale.
        server_onupdate=FetchedValue(),
    )


class AuthPassword(Base):
    """A person's password verifier: argon2id over HMAC(pepper, password)."""

    __tablename__ = "auth_passwords"
    __table_args__ = (
        client_org_fk("auth_passwords"),
        CheckConstraint("pepper_version >= 1", name="ck_auth_passwords_pepper_version"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID | None] = _client_id()
    #: The argon2id encoded hash (`$argon2id$v=19$m=...`): parameters and salt
    #: travel inside it.
    hash: Mapped[str] = mapped_column(Text)
    #: The transit key version of the pepper the HMAC used.
    pepper_version: Mapped[int] = mapped_column(Integer)
    changed_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AuthTotp(Base):
    """A person's TOTP secret, sealed by the vault, and the last step accepted."""

    __tablename__ = "auth_totp"
    __table_args__ = (client_org_fk("auth_totp"),)

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID | None] = _client_id()
    #: `vault.seal_bytes(org_id=..., key_id=user_id, plaintext=secret)`, the
    #: self-contained ASCII string it returns, stored as its bytes
    #: (`.encode("ascii")`; `open_bytes` takes it back `.decode("ascii")`).
    secret_sealed: Mapped[bytes] = mapped_column(LargeBinary)
    #: The last 30-second step accepted; a code for a step at or below it is
    #: refused (design §2.2: pyotp does not do this itself).
    last_step: Mapped[int | None] = mapped_column(BigInteger, default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AuthPasskey(Base):
    """A WebAuthn credential (design §2.2): several per person."""

    __tablename__ = "auth_passkeys"
    __table_args__ = (
        client_org_fk("auth_passkeys"),
        # W3C WebAuthn §7.1: a credential id registered to anyone is refused.
        UniqueConstraint("credential_id", name="uq_auth_passkeys_credential_id"),
        CheckConstraint("sign_count >= 0", name="ck_auth_passkeys_sign_count"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="ck_auth_passkeys_name"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID | None] = _client_id()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    credential_id: Mapped[bytes] = mapped_column(LargeBinary)
    #: The COSE public key, as py_webauthn returns it.
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    transports: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'")
    )
    backup_eligible: Mapped[bool] = mapped_column()
    backup_state: Mapped[bool] = mapped_column()
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)


class AuthRecoveryCode(Base):
    """One single-use recovery code, stored as its hash (design §2.2)."""

    __tablename__ = "auth_recovery_codes"
    __table_args__ = (
        client_org_fk("auth_recovery_codes"),
        CheckConstraint(
            "octet_length(code_hash) BETWEEN 32 AND 256", name="ck_auth_recovery_codes_code_hash"
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID | None] = _client_id()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    code_hash: Mapped[bytes] = mapped_column(LargeBinary)
    used_at: Mapped[datetime | None] = mapped_column(default=None)


class AuthTrustedDevice(Base):
    """A remembered device (design §2.6): the `__Host-itms_device` cookie's hash."""

    __tablename__ = "auth_trusted_devices"
    __table_args__ = (
        client_org_fk("auth_trusted_devices"),
        UniqueConstraint("token_hash", name="uq_auth_trusted_devices_token_hash"),
        UniqueConstraint("id", "user_id", name="uq_auth_trusted_devices_id_user"),
        CheckConstraint(
            "octet_length(token_hash) = 32", name="ck_auth_trusted_devices_token_hash"
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID | None] = _client_id()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    label: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column()
    last_used_at: Mapped[datetime | None] = mapped_column(default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)


class AuthSession(Base):
    """A signed-in browser (design §2.4, §6.4): the `__Host-itms_session`
    cookie's hash. `factor_at` NULL is a session that has passed the password
    and not yet the second factor."""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        client_org_fk("auth_sessions"),
        UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
        # A session's remembered device is its own person's.
        ForeignKeyConstraint(
            ["trusted_device_id", "user_id"],
            ["auth_trusted_devices.id", "auth_trusted_devices.user_id"],
            name="fk_auth_sessions_device_user",
        ),
        CheckConstraint("kind IN ('staff', 'contact')", name="ck_auth_sessions_kind"),
        CheckConstraint("octet_length(token_hash) = 32", name="ck_auth_sessions_token_hash"),
        CheckConstraint(
            "(ended_at IS NULL) = (ended_reason IS NULL)", name="ck_auth_sessions_ended"
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID | None] = _client_id()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    kind: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column()
    factor_at: Mapped[datetime | None] = mapped_column(default=None)
    step_up_at: Mapped[datetime | None] = mapped_column(default=None)
    trusted_device_id: Mapped[UUID | None] = mapped_column(default=None)
    user_agent: Mapped[str] = mapped_column(Text, default="", server_default="")
    ip: Mapped[str] = mapped_column(Text, default="", server_default="")
    ended_at: Mapped[datetime | None] = mapped_column(default=None)
    ended_reason: Mapped[str | None] = mapped_column(Text, default=None)


class AuthFailure(Base):
    """Consecutive failures of one factor of one person (design §2.3)."""

    __tablename__ = "auth_failures"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "factor", name="auth_failures_pkey"),
        client_org_fk("auth_failures"),
        CheckConstraint(
            "factor IN ('password', 'totp', 'passkey', 'recovery')",
            name="ck_auth_failures_factor",
        ),
        CheckConstraint("consecutive >= 0", name="ck_auth_failures_consecutive"),
    )

    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID | None] = _client_id()
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    factor: Mapped[str] = mapped_column(Text)
    consecutive: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_failed_at: Mapped[datetime] = mapped_column()
    #: Set at `DISABLE_AT` consecutive failures; only an administrator's reset
    #: clears it (NIST SP 800-63B-4 §3.2.2).
    disabled_at: Mapped[datetime | None] = mapped_column(default=None)


class AuthActionToken(Base):
    """A single-use link: invitation (72 h), password reset (30 min) or the
    server command's recovery link (15 min). Stored as its hash."""

    __tablename__ = "auth_action_tokens"
    __table_args__ = (
        client_org_fk("auth_action_tokens"),
        UniqueConstraint("token_hash", name="uq_auth_action_tokens_token_hash"),
        CheckConstraint(
            "purpose IN ('invite', 'reset', 'recover')", name="ck_auth_action_tokens_purpose"
        ),
        CheckConstraint(
            "octet_length(token_hash) = 32", name="ck_auth_action_tokens_token_hash"
        ),
        CheckConstraint("expires_at > created_at", name="ck_auth_action_tokens_expiry"),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID | None] = _client_id()
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column()
    used_at: Mapped[datetime | None] = mapped_column(default=None)
    created_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )


class AuthWebauthnChallenge(Base):
    """A WebAuthn challenge: random, single use, with an expiry (design §2.2).
    `user_id` NULL only when signing in, before the person is known."""

    __tablename__ = "auth_webauthn_challenges"
    __table_args__ = (
        client_org_fk("auth_webauthn_challenges"),
        CheckConstraint(
            "purpose IN ('register', 'authenticate', 'step_up')",
            name="ck_auth_webauthn_challenges_purpose",
        ),
        CheckConstraint(
            "octet_length(challenge) >= 16", name="ck_auth_webauthn_challenges_challenge"
        ),
        CheckConstraint(
            "purpose = 'authenticate' OR user_id IS NOT NULL",
            name="ck_auth_webauthn_challenges_person",
        ),
    )

    id: Mapped[UUID] = uuid7_pk()
    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"))
    client_id: Mapped[UUID | None] = _client_id()
    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), default=None
    )
    purpose: Mapped[str] = mapped_column(Text)
    challenge: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column()
    used_at: Mapped[datetime | None] = mapped_column(default=None)


class AuthOrgSettings(Base):
    """Settings → Security → Sessions (design §2.4, §3; plan decision D10): one
    optional row per organization. No row is the defaults below, which are
    also the column defaults; every bound is a CHECK, so a direct SQL fixup is
    held to it as well."""

    __tablename__ = "auth_org_settings"
    __table_args__ = (
        CheckConstraint(
            "staff_idle_minutes BETWEEN 5 AND 480", name="ck_auth_org_settings_staff_idle"
        ),
        CheckConstraint("staff_max_hours BETWEEN 1 AND 24", name="ck_auth_org_settings_staff_max"),
        CheckConstraint(
            "remember_device_days BETWEEN 1 AND 30", name="ck_auth_org_settings_remember_days"
        ),
        CheckConstraint(
            "remembered_max_hours BETWEEN 1 AND 720", name="ck_auth_org_settings_remembered_max"
        ),
        CheckConstraint(
            "contact_idle_minutes BETWEEN 5 AND 480", name="ck_auth_org_settings_contact_idle"
        ),
        CheckConstraint(
            "contact_max_days BETWEEN 1 AND 30", name="ck_auth_org_settings_contact_max"
        ),
        CheckConstraint(
            "staff_idle_minutes <= staff_max_hours * 60",
            name="ck_auth_org_settings_staff_idle_within_max",
        ),
    )

    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    staff_idle_minutes: Mapped[int] = mapped_column(Integer, default=30, server_default="30")
    staff_max_hours: Mapped[int] = mapped_column(Integer, default=12, server_default="12")
    remember_device_enabled: Mapped[bool] = mapped_column(default=True, server_default=true())
    remember_device_days: Mapped[int] = mapped_column(Integer, default=15, server_default="15")
    remembered_max_hours: Mapped[int] = mapped_column(Integer, default=168, server_default="168")
    contact_idle_minutes: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    contact_max_days: Mapped[int] = mapped_column(Integer, default=7, server_default="7")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )


class AccountMailSettings(Base):
    """Settings -> Email for one organization (`app/core/mail.py`, design §7).

    The relay's password or Microsoft client secret is NOT a column here: it is
    the vault secret of `credential_id` (invariant 3)."""

    __tablename__ = "account_mail_settings"
    __table_args__ = (
        CheckConstraint("length(host) BETWEEN 1 AND 253", name="ck_account_mail_host"),
        CheckConstraint("port BETWEEN 1 AND 65535", name="ck_account_mail_port"),
        CheckConstraint(
            "encryption IN ('none', 'starttls', 'ssl')", name="ck_account_mail_encryption"
        ),
        CheckConstraint("auth_type IN ('password', 'token')", name="ck_account_mail_auth_type"),
    )

    org_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    host: Mapped[str] = mapped_column(Text)
    port: Mapped[int] = mapped_column(Integer)
    from_address: Mapped[str] = mapped_column(Text)
    from_display_name: Mapped[str] = mapped_column(Text, default="", server_default="")
    reply_to: Mapped[str] = mapped_column(Text, default="", server_default="")
    encryption: Mapped[str] = mapped_column(Text)
    username: Mapped[str] = mapped_column(Text, default="", server_default="")
    auth_type: Mapped[str] = mapped_column(Text, default="password", server_default="password")
    token_url: Mapped[str] = mapped_column(Text, default="", server_default="")
    token_scope: Mapped[str] = mapped_column(Text, default="", server_default="")
    token_client_id: Mapped[str] = mapped_column(Text, default="", server_default="")
    credential_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), default=None
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )


__all__ = [
    "ACTION_PURPOSES",
    "CHALLENGE_PURPOSES",
    "FACTORS",
    "SESSION_KINDS",
    "AccountMailSettings",
    "AuthActionToken",
    "AuthFailure",
    "AuthOrgSettings",
    "AuthPasskey",
    "AuthPassword",
    "AuthRecoveryCode",
    "AuthSession",
    "AuthTotp",
    "AuthTrustedDevice",
    "AuthWebauthnChallenge",
]
