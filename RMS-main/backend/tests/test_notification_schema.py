"""Pure schema-validation tests for the notification module's Pydantic
models (docs/modules/notification/spec.md §2, §6.4, §9). No database
required -- these exercise validators only.
"""

import pytest
from pydantic import ValidationError

from app.modules.notification.schema import (
    AllowlistEntryCreate,
    AllowlistEntryOut,
    ChannelCreate,
    ChannelOut,
    ChannelUpdate,
    RuleCreate,
)


def test_webhook_channel_requires_a_url():
    with pytest.raises(ValidationError):
        ChannelCreate(kind="webhook", name="ops", config={})


def test_email_channel_requires_host_and_from_address():
    with pytest.raises(ValidationError):
        ChannelCreate(kind="email", name="mail", config={"host": "smtp.example.com"})


def test_channel_config_rejects_an_embedded_secret():
    """Spec §2.2 -- config is returned by the API. A secret placed here would
    be a credential disclosure; secrets belong in the vault."""
    with pytest.raises(ValidationError) as exc:
        ChannelCreate(
            kind="webhook",
            name="ops",
            config={"url": "https://hooks.example.com/x", "password": "hunter2"},
        )
    assert "secret" in str(exc.value).lower() or "password" in str(exc.value).lower()


def test_channel_update_config_rejects_an_embedded_secret():
    """Same guarantee on the update path -- one shared validator, not a
    second copy that could drift and stop rejecting secrets."""
    with pytest.raises(ValidationError) as exc:
        ChannelUpdate(config={"url": "https://hooks.example.com/x", "token": "abc"})
    assert "secret" in str(exc.value).lower() or "token" in str(exc.value).lower()


def test_channel_update_allows_a_clean_config():
    update = ChannelUpdate(config={"url": "https://hooks.example.com/x"})
    assert update.config == {"url": "https://hooks.example.com/x"}


# --- C1 fix: email config coherence beyond bare key presence --------------


def test_email_channel_rejects_a_non_integer_port():
    """Whole-branch review finding C1(b) -- `ChannelCreate` used to check
    only that `port` was PRESENT, not that it was a real integer.
    `_send_email` then raised an uncaught `ValueError` at send time instead
    of this being rejected at write time."""
    with pytest.raises(ValidationError):
        ChannelCreate(
            kind="email",
            name="mail",
            config={"host": "smtp.example.com", "port": "not-a-number", "from_address": "a@b.c"},
        )


@pytest.mark.parametrize("port", [0, -1, 65536, 100000])
def test_email_channel_rejects_out_of_range_port(port: int):
    with pytest.raises(ValidationError):
        ChannelCreate(
            kind="email",
            name="mail",
            config={"host": "smtp.example.com", "port": port, "from_address": "a@b.c"},
        )


def test_email_channel_rejects_a_boolean_port():
    """`bool` is a subclass of `int` in Python -- `port: true` must not slip
    through as `port=1`."""
    with pytest.raises(ValidationError):
        ChannelCreate(
            kind="email",
            name="mail",
            config={"host": "smtp.example.com", "port": True, "from_address": "a@b.c"},
        )


def test_email_channel_rejects_an_empty_host():
    with pytest.raises(ValidationError):
        ChannelCreate(
            kind="email",
            name="mail",
            config={"host": "", "port": 587, "from_address": "a@b.c"},
        )


def test_email_channel_accepts_a_well_formed_config():
    channel = ChannelCreate(
        kind="email",
        name="mail",
        config={"host": "smtp.example.com", "port": 587, "from_address": "a@b.c"},
    )
    assert channel.config["port"] == 587


def test_channel_update_all_fields_optional():
    # PATCH semantics (spec §9): every field is omittable so the service
    # layer can apply exclude_unset=True.
    update = ChannelUpdate()
    assert update.name is None
    assert update.config is None
    assert update.active is None


def test_channel_out_has_no_credential_field():
    # Structural guarantee, not a runtime filter: the response model simply
    # cannot carry a secret.
    assert "credential" not in ChannelOut.model_fields
    assert "credential_id" not in ChannelOut.model_fields
    assert "password" not in ChannelOut.model_fields
    assert "token" not in ChannelOut.model_fields


def test_rule_rejects_internal_note_to_requester():
    """Spec §6.4 -- rejected at write time, not filtered at render time."""
    with pytest.raises(ValidationError):
        RuleCreate(
            event_type="ticket.internal_note_added",
            channel_id="00000000-0000-0000-0000-000000000001",
            recipient_kind="requester",
        )


def test_rule_rejects_sla_breach_to_requester():
    """SLA spec §6 -- the breach mail is written staff-facing, and
    `recipient_kind="requester"` resolves the recipient from the TICKET rather
    than the payload, so omitting `requester_id` from the payload would not
    have stopped delivery. Refused at write time, same as §6.4's internal
    notes."""
    with pytest.raises(ValidationError):
        RuleCreate(
            event_type="ticket.sla_breached",
            channel_id="00000000-0000-0000-0000-000000000001",
            recipient_kind="requester",
        )


def test_rule_accepts_sla_breach_to_queue_members():
    rule = RuleCreate(
        event_type="ticket.sla_breached",
        channel_id="00000000-0000-0000-0000-000000000001",
        recipient_kind="queue_members",
    )
    assert rule.recipient_kind == "queue_members"


def test_rule_accepts_internal_note_to_queue_members():
    rule = RuleCreate(
        event_type="ticket.internal_note_added",
        channel_id="00000000-0000-0000-0000-000000000001",
        recipient_kind="queue_members",
    )
    assert rule.recipient_kind == "queue_members"


def test_rule_rejects_an_unregistered_event_type():
    with pytest.raises(ValidationError):
        RuleCreate(
            event_type="ticket.assinged",
            channel_id="00000000-0000-0000-0000-000000000001",
            recipient_kind="assignee",
        )


# --- Allowlist entry `port` (migration 0026 correction to the brief) -------


def test_allowlist_entry_port_defaults_to_443():
    entry = AllowlistEntryCreate(host="mattermost.internal")
    assert entry.port == 443


def test_allowlist_entry_accepts_an_explicit_port():
    entry = AllowlistEntryCreate(host="mattermost.internal", port=8443)
    assert entry.port == 8443


@pytest.mark.parametrize("port", [0, -1, 65536, 100000])
def test_allowlist_entry_rejects_out_of_range_ports(port):
    with pytest.raises(ValidationError):
        AllowlistEntryCreate(host="mattermost.internal", port=port)


@pytest.mark.parametrize("port", [1, 443, 65535])
def test_allowlist_entry_accepts_boundary_ports(port):
    entry = AllowlistEntryCreate(host="mattermost.internal", port=port)
    assert entry.port == port


def test_allowlist_entry_rejects_a_cidr_host():
    with pytest.raises(ValidationError):
        AllowlistEntryCreate(host="10.0.0.0/8")


def test_allowlist_entry_out_exposes_port():
    assert "port" in AllowlistEntryOut.model_fields
