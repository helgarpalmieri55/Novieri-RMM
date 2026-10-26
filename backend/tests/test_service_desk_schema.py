"""Tests for the Service Desk module's API schemas and typed errors
(docs/modules/service-desk/spec.md, Task 3).

Schema-boundary bounds (schema.py module docstring). Every `max_length`
here mirrors a real column width in migrations/versions/0023_service_desk.py
(or, for the three invented text bounds, is documented as a judgment call).
An unbounded string would pass Pydantic, reach asyncpg, and surface as an
unhandled DataError -- a 500, per the Phase-11/12 lesson this module's
docstring also cites.

This file covers all sixteen models the brief names, not just the four
illustrative RED-state tests it supplied as an example of shape.
"""

from datetime import datetime
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from app.modules.service_desk import schema
from app.modules.service_desk.errors import TicketNotFoundError, TicketValidationError
from app.modules.service_desk.schema import (
    ApprovalDecision,
    AttachmentOut,
    CategoryCreate,
    CategoryOut,
    CategoryUpdate,
    CommentCreate,
    CommentOut,
    QueueCreate,
    QueueOut,
    QueueUpdate,
    StatusHistoryOut,
    TicketAssign,
    TicketCreate,
    TicketOut,
    TicketStatusChange,
    TicketUpdate,
)

_UUID = uuid4()
_NOW = datetime(2026, 1, 1, 0, 0, 0)

# Every *Out model in the module, DERIVED by introspection rather than listed
# by hand. A hand-written list covers what someone remembered to add to it: the
# seventh *Out model silently escapes the invariant below, which is exactly how
# a leak gets reintroduced after the check was written to prevent it. Deriving
# it means a new response model is covered the moment it exists.
_OUT_MODELS = sorted(
    (
        obj
        for name, obj in vars(schema).items()
        if name.endswith("Out") and isinstance(obj, type) and issubclass(obj, BaseModel)
    ),
    key=lambda m: m.__name__,
)


def test_not_found_is_a_validation_error_subclass() -> None:
    """api.py maps by isinstance with the subclass checked FIRST; if this
    relationship breaks, every 404 silently becomes a 400."""
    assert issubclass(TicketNotFoundError, TicketValidationError)
    assert isinstance(TicketNotFoundError("nope"), TicketValidationError)


# ---------------------------------------------------------------------------
# Invariants (security invariant #5): no *Out model leaks org_id, and
# AttachmentOut specifically never leaks object_key. Asserted against
# model_fields so every *Out model is covered, not one at a time -- a
# per-model spot check is what lets the next model reintroduce the leak.
# ---------------------------------------------------------------------------


def test_no_out_model_carries_org_id() -> None:
    for model in _OUT_MODELS:
        assert "org_id" not in model.model_fields, model.__name__


def test_attachment_out_never_carries_object_key() -> None:
    assert "object_key" not in AttachmentOut.model_fields


# ---------------------------------------------------------------------------
# Queues (spec §2 ticket_queue, §5)
# ---------------------------------------------------------------------------


def test_queue_create_requires_key_and_name() -> None:
    with pytest.raises(ValidationError):
        QueueCreate(key="incoming")  # type: ignore[call-arg]


def test_queue_create_defaults_description_and_sort_order() -> None:
    q = QueueCreate(key="incoming", name="Incoming")
    assert q.description == ""
    assert q.sort_order == 0


def test_queue_create_rejects_over_length_key() -> None:
    with pytest.raises(ValidationError):
        QueueCreate(key="x" * 65, name="Incoming")


def test_queue_create_accepts_max_length_key() -> None:
    # ticket_queue.key is varchar(64) (migration 0023).
    q = QueueCreate(key="x" * 64, name="Incoming")
    assert len(q.key) == 64


def test_queue_create_rejects_over_length_name() -> None:
    with pytest.raises(ValidationError):
        QueueCreate(key="incoming", name="x" * 256)


def test_queue_create_accepts_max_length_name() -> None:
    # ticket_queue.name is varchar(255) (migration 0023).
    q = QueueCreate(key="incoming", name="x" * 255)
    assert len(q.name) == 255


def test_queue_create_rejects_over_length_description() -> None:
    with pytest.raises(ValidationError):
        QueueCreate(key="incoming", name="Incoming", description="x" * 1001)


def test_queue_create_accepts_max_length_description() -> None:
    q = QueueCreate(key="incoming", name="Incoming", description="x" * 1000)
    assert len(q.description) == 1000


def test_queue_create_rejects_negative_sort_order() -> None:
    with pytest.raises(ValidationError):
        QueueCreate(key="incoming", name="Incoming", sort_order=-1)


def test_queue_create_rejects_sort_order_above_int32() -> None:
    with pytest.raises(ValidationError):
        QueueCreate(key="incoming", name="Incoming", sort_order=2_147_483_648)


def test_queue_create_accepts_sort_order_at_int32_max() -> None:
    q = QueueCreate(key="incoming", name="Incoming", sort_order=2_147_483_647)
    assert q.sort_order == 2_147_483_647


def test_queue_update_all_fields_optional() -> None:
    assert QueueUpdate().model_dump(exclude_unset=True) == {}


def test_queue_update_has_no_key_field() -> None:
    # key is the stable identifier queues are referenced by; renaming is
    # done via name only (schema.py module docstring / QueueUpdate docstring).
    assert "key" not in QueueUpdate.model_fields


def test_queue_update_distinguishes_omitted_from_explicit_null() -> None:
    omitted = QueueUpdate().model_dump(exclude_unset=True)
    assert "active" not in omitted

    cleared = QueueUpdate(active=None).model_dump(exclude_unset=True)
    assert cleared == {"active": None}

    set_active = QueueUpdate(active=False).model_dump(exclude_unset=True)
    assert set_active == {"active": False}


def test_queue_update_rejects_over_length_name() -> None:
    with pytest.raises(ValidationError):
        QueueUpdate(name="x" * 256)


def test_queue_update_rejects_negative_sort_order() -> None:
    with pytest.raises(ValidationError):
        QueueUpdate(sort_order=-1)


def test_queue_out_requires_all_fields() -> None:
    with pytest.raises(ValidationError):
        QueueOut(key="incoming", name="Incoming", description="", active=True, sort_order=0,
                  created_by=_UUID, created_at=_NOW)  # type: ignore[call-arg]  # missing id


def test_queue_out_constructs_from_a_full_row() -> None:
    q = QueueOut(id=_UUID, key="incoming", name="Incoming", description="", active=True,
                 sort_order=0, created_by=_UUID, created_at=_NOW)
    assert q.key == "incoming"


# ---------------------------------------------------------------------------
# Categories (spec §2 ticket_category, §4)
# ---------------------------------------------------------------------------


def test_category_create_requires_key_name_and_type() -> None:
    with pytest.raises(ValidationError):
        CategoryCreate(key="hw", name="Hardware")  # type: ignore[call-arg]


def test_category_create_rejects_out_of_domain_type() -> None:
    with pytest.raises(ValidationError):
        CategoryCreate(key="hw", name="Hardware", type="problem")  # type: ignore[arg-type]


def test_category_create_defaults() -> None:
    c = CategoryCreate(key="hw", name="Hardware", type="incident")
    assert c.requires_approval is False
    assert c.approver_role_id is None
    assert c.default_queue_id is None
    assert c.sort_order == 0


def test_category_create_rejects_over_length_key() -> None:
    with pytest.raises(ValidationError):
        CategoryCreate(key="x" * 65, name="Hardware", type="incident")


def test_category_create_accepts_max_length_key() -> None:
    # ticket_category.key is varchar(64) (migration 0023).
    c = CategoryCreate(key="x" * 64, name="Hardware", type="incident")
    assert len(c.key) == 64


def test_category_create_rejects_over_length_name() -> None:
    with pytest.raises(ValidationError):
        CategoryCreate(key="hw", name="x" * 256, type="incident")


def test_category_create_accepts_max_length_name() -> None:
    # ticket_category.name is varchar(255) (migration 0023).
    c = CategoryCreate(key="hw", name="x" * 255, type="incident")
    assert len(c.name) == 255


def test_category_create_rejects_negative_sort_order() -> None:
    with pytest.raises(ValidationError):
        CategoryCreate(key="hw", name="Hardware", type="incident", sort_order=-1)


def test_category_create_rejects_sort_order_above_int32() -> None:
    with pytest.raises(ValidationError):
        CategoryCreate(key="hw", name="Hardware", type="incident", sort_order=2_147_483_648)


def test_category_update_all_fields_optional() -> None:
    assert CategoryUpdate().model_dump(exclude_unset=True) == {}


def test_category_update_has_no_key_or_type_field() -> None:
    # type decides which lifecycle a ticket in this category uses (spec
    # §3.3); changing it out from under existing tickets would silently
    # invalidate them, so it -- like key -- is not patchable.
    assert "key" not in CategoryUpdate.model_fields
    assert "type" not in CategoryUpdate.model_fields


def test_category_update_rejects_over_length_name() -> None:
    with pytest.raises(ValidationError):
        CategoryUpdate(name="x" * 256)


def test_category_out_requires_all_fields() -> None:
    with pytest.raises(ValidationError):
        CategoryOut(key="hw", name="Hardware", type="incident", requires_approval=False,
                    approver_role_id=None, default_queue_id=None, active=True, sort_order=0,
                    created_by=_UUID, created_at=_NOW)  # type: ignore[call-arg]  # missing id


def test_category_out_type_field_is_a_plain_str_not_a_literal() -> None:
    # *Out widens the shared Literal alias to plain str (schema.py module
    # docstring, "domain literals" comment): a response model built from an
    # already-persisted, already-CHECK-constrained row is not re-validated
    # on the way out. A value outside TicketType still constructs cleanly.
    c = CategoryOut(id=_UUID, key="hw", name="Hardware", type="not-a-real-type",
                     requires_approval=False, approver_role_id=None, default_queue_id=None,
                     active=True, sort_order=0, created_by=_UUID, created_at=_NOW)
    assert c.type == "not-a-real-type"


# ---------------------------------------------------------------------------
# Tickets (spec §2 ticket, §3)
# ---------------------------------------------------------------------------


def test_ticket_create_requires_subject_and_category() -> None:
    with pytest.raises(ValidationError):
        TicketCreate(type="incident")  # type: ignore[call-arg]


def test_ticket_create_defaults_priority_to_normal() -> None:
    t = TicketCreate(type="incident", category_id=_UUID, subject="Printer on fire")
    assert t.priority == "normal"
    assert t.description == ""


def test_ticket_create_defaults() -> None:
    t = TicketCreate(type="incident", category_id=_UUID, subject="Printer on fire")
    assert t.requester_id is None
    assert t.assigned_to is None
    assert t.device_id is None


def test_ticket_create_rejects_out_of_domain_priority() -> None:
    with pytest.raises(ValidationError):
        TicketCreate(type="incident", category_id=_UUID, subject="Printer on fire",
                     priority="critical")  # type: ignore[arg-type]


def test_ticket_create_rejects_out_of_domain_type() -> None:
    with pytest.raises(ValidationError):
        TicketCreate(type="bug", category_id=_UUID, subject="Printer on fire")  # type: ignore[arg-type]


def test_ticket_create_rejects_over_length_subject() -> None:
    with pytest.raises(ValidationError):
        TicketCreate(type="incident", category_id=_UUID, subject="x" * 256)


def test_ticket_create_accepts_max_length_subject() -> None:
    # ticket.subject is varchar(255) (migration 0023).
    t = TicketCreate(type="incident", category_id=_UUID, subject="x" * 255)
    assert len(t.subject) == 255


def test_ticket_create_rejects_over_length_description() -> None:
    with pytest.raises(ValidationError):
        TicketCreate(type="incident", category_id=_UUID, subject="Printer on fire",
                     description="x" * 20_001)


def test_ticket_create_accepts_max_length_description() -> None:
    t = TicketCreate(type="incident", category_id=_UUID, subject="Printer on fire",
                      description="x" * 20_000)
    assert len(t.description) == 20_000


def test_ticket_update_distinguishes_omitted_from_explicit_null() -> None:
    """The service layer decides by key PRESENCE, so the route must be able to
    pass model_dump(exclude_unset=True) and have omitted mean 'do not touch'."""
    # Demonstrated on `device_id`: nullable, still editable through PATCH, and
    # unlike the old `assigned_to` example it is not owned by `assign_ticket`.
    omitted = TicketUpdate().model_dump(exclude_unset=True)
    assert "device_id" not in omitted

    cleared = TicketUpdate(device_id=None).model_dump(exclude_unset=True)
    assert cleared == {"device_id": None}

    set_to = uuid4()
    linked = TicketUpdate(device_id=set_to).model_dump(exclude_unset=True)
    assert linked == {"device_id": set_to}


def test_ticket_update_all_fields_optional() -> None:
    assert TicketUpdate().model_dump(exclude_unset=True) == {}


def test_ticket_update_has_no_status_type_requester_or_origin_field() -> None:
    # status moves only through the dedicated transition endpoint (spec
    # §9); type/requester_id/origin/source_alert_id are immutable after
    # creation (schema.py module docstring / TicketUpdate docstring).
    for field in ("status", "type", "requester_id", "origin", "source_alert_id"):
        assert field not in TicketUpdate.model_fields, field


def test_ticket_update_rejects_out_of_domain_priority() -> None:
    with pytest.raises(ValidationError):
        TicketUpdate(priority="critical")  # type: ignore[arg-type]


def test_ticket_update_rejects_over_length_subject() -> None:
    with pytest.raises(ValidationError):
        TicketUpdate(subject="x" * 256)


def test_ticket_update_rejects_over_length_description() -> None:
    with pytest.raises(ValidationError):
        TicketUpdate(description="x" * 20_001)


def _full_ticket_out_kwargs() -> dict[str, object]:
    return dict(
        id=_UUID, client_id=_UUID, number="INC-0001", type="incident", category_id=_UUID,
        queue_id=_UUID,
        status="open", priority="normal", subject="Printer on fire", description="",
        requester_id=_UUID, assigned_to=None, device_id=None, origin="technician",
        source_alert_id=None, resolved_at=None, closed_at=None, has_attachments=False,
        created_by=_UUID, created_at=_NOW, updated_at=_NOW,
    )


def test_ticket_out_requires_all_fields() -> None:
    kwargs = _full_ticket_out_kwargs()
    del kwargs["id"]
    with pytest.raises(ValidationError):
        TicketOut(**kwargs)  # type: ignore[arg-type]


def test_ticket_out_constructs_from_a_full_row() -> None:
    t = TicketOut(**_full_ticket_out_kwargs())  # type: ignore[arg-type]
    assert t.has_attachments is False


def test_ticket_out_status_type_priority_origin_are_plain_str() -> None:
    # Same widening as CategoryOut.type: an *Out model built from a row a
    # DB CHECK constraint has already validated is not re-validated here.
    kwargs = _full_ticket_out_kwargs()
    kwargs["status"] = "some_future_status"
    kwargs["priority"] = "extreme"
    kwargs["origin"] = "sms"
    kwargs["type"] = "alert"
    t = TicketOut(**kwargs)  # type: ignore[arg-type]
    assert t.status == "some_future_status"


def test_ticket_out_has_no_attachment_object_key_or_list() -> None:
    # spec: a count or list of attachment objects would either leak
    # object_key indirectly or force a second query per row.
    assert "has_attachments" in TicketOut.model_fields
    assert "object_key" not in TicketOut.model_fields
    assert "attachments" not in TicketOut.model_fields


def test_ticket_status_change_requires_to_status() -> None:
    with pytest.raises(ValidationError):
        TicketStatusChange()  # type: ignore[call-arg]


def test_ticket_status_change_defaults_reason_to_empty() -> None:
    s = TicketStatusChange(to_status="resolved")
    assert s.reason == ""


def test_ticket_status_change_rejects_over_length_to_status() -> None:
    # ticket.status / ticket_status_history.to_status are varchar(32)
    # (migration 0023).
    with pytest.raises(ValidationError):
        TicketStatusChange(to_status="x" * 33)


def test_ticket_status_change_accepts_max_length_to_status() -> None:
    s = TicketStatusChange(to_status="x" * 32)
    assert len(s.to_status) == 32


def test_ticket_status_change_rejects_over_length_reason() -> None:
    with pytest.raises(ValidationError):
        TicketStatusChange(to_status="resolved", reason="x" * 2001)


def test_ticket_status_change_accepts_max_length_reason() -> None:
    s = TicketStatusChange(to_status="resolved", reason="x" * 2000)
    assert len(s.reason) == 2000


def test_ticket_status_change_to_status_is_not_a_literal() -> None:
    # Deliberate: lifecycle.py (Task 4) is the sole authority on the valid
    # status vocabulary and transition table per type (spec §3.3) --
    # "there is no second copy." A Literal here would be that second copy,
    # so any string within the column's length is accepted at this layer;
    # lifecycle.py rejects an out-of-vocabulary or illegal-transition value.
    s = TicketStatusChange(to_status="not_a_real_status")
    assert s.to_status == "not_a_real_status"


def test_ticket_assign_both_fields_default_none() -> None:
    a = TicketAssign()
    assert a.assigned_to is None
    assert a.queue_id is None


def test_ticket_assign_distinguishes_omitted_from_explicit_null() -> None:
    omitted = TicketAssign().model_dump(exclude_unset=True)
    assert omitted == {}

    unassign = TicketAssign(assigned_to=None).model_dump(exclude_unset=True)
    assert unassign == {"assigned_to": None}

    move_queue = TicketAssign(queue_id=_UUID).model_dump(exclude_unset=True)
    assert move_queue == {"queue_id": _UUID}


def test_approval_decision_reason_defaults_to_empty() -> None:
    assert ApprovalDecision().reason == ""


def test_approval_decision_rejects_over_length_reason() -> None:
    with pytest.raises(ValidationError):
        ApprovalDecision(reason="x" * 2001)


def test_approval_decision_accepts_max_length_reason() -> None:
    d = ApprovalDecision(reason="x" * 2000)
    assert len(d.reason) == 2000


# ---------------------------------------------------------------------------
# Comments & attachments (spec §6)
# ---------------------------------------------------------------------------


def test_comment_create_requires_nonempty_body() -> None:
    with pytest.raises(ValidationError):
        CommentCreate(body="")


def test_comment_create_defaults_visibility_to_public() -> None:
    c = CommentCreate(body="Called the vendor.")
    assert c.visibility == "public"


def test_comment_create_rejects_out_of_domain_visibility() -> None:
    with pytest.raises(ValidationError):
        CommentCreate(body="Called the vendor.", visibility="secret")  # type: ignore[arg-type]


def test_comment_create_rejects_over_length_body() -> None:
    with pytest.raises(ValidationError):
        CommentCreate(body="x" * 20_001)


def test_comment_create_accepts_max_length_body() -> None:
    c = CommentCreate(body="x" * 20_000)
    assert len(c.body) == 20_000


def test_comment_out_requires_all_fields() -> None:
    with pytest.raises(ValidationError):
        CommentOut(ticket_id=_UUID, author_id=_UUID, author_type="user", body="hi",
                   visibility="public", created_at=_NOW)  # type: ignore[call-arg]  # missing id


def test_comment_out_visibility_is_a_plain_str_not_a_literal() -> None:
    c = CommentOut(id=_UUID, ticket_id=_UUID, author_id=None, author_type="system",
                    body="Auto-closed after 30 days idle.", visibility="whatever-value",
                    created_at=_NOW)
    assert c.visibility == "whatever-value"


def test_attachment_out_requires_all_fields() -> None:
    with pytest.raises(ValidationError):
        AttachmentOut(ticket_id=_UUID, filename="log.txt", byte_size=10,
                      uploaded_by=_UUID, created_at=_NOW)  # type: ignore[call-arg]  # missing id


def test_attachment_out_constructs_from_a_full_row() -> None:
    a = AttachmentOut(id=_UUID, ticket_id=_UUID, filename="log.txt", byte_size=10,
                       uploaded_by=_UUID, created_at=_NOW)
    assert a.filename == "log.txt"


def test_status_history_out_requires_all_fields() -> None:
    with pytest.raises(ValidationError):
        StatusHistoryOut(ticket_id=_UUID, from_status="new", to_status="open",
                          actor_type="user", actor_id=_UUID, reason="",
                          created_at=_NOW)  # type: ignore[call-arg]  # missing id


def test_status_history_out_allows_null_from_status_for_creation_row() -> None:
    # The first history row for a ticket has no prior status.
    h = StatusHistoryOut(id=_UUID, ticket_id=_UUID, from_status=None, to_status="new",
                          actor_type="user", actor_id=_UUID, reason="", created_at=_NOW)
    assert h.from_status is None


def test_uuid_type_annotations_are_real_uuids() -> None:
    assert isinstance(_UUID, UUID)


def test_ticket_create_has_no_origin_field() -> None:
    """`origin` must not be settable from a request body.

    `ck_ticket_human_attribution` permits `requester_id`/`created_by` to be
    NULL exactly when `origin == "alert"`. So if a client could set `origin`,
    POSTing {"origin": "alert"} would mint a ticket with no human on it —
    a forged system-attributed record in an immutable audit trail.

    `create_ticket` takes `origin` as a caller-supplied keyword (the route
    passes "technician", Phase 15 passes "alert") and never reads it from the
    payload. This asserts the field is absent rather than merely ignored: an
    ignored field is one a future route author forwards.
    """
    assert "origin" not in TicketCreate.model_fields
    assert "source_alert_id" not in TicketCreate.model_fields


def test_ticket_update_has_no_assignment_fields() -> None:
    """`queue_id` and `assigned_to` belong to `assign_ticket`, which spec §5
    gates on `ticket.assign` and §8.3 audits as `ticket.assigned`.

    Declared here they would make `PATCH {"queue_id": ...}` a silent no-op
    returning 200 — the field validates, the service ignores it, the caller
    believes the ticket moved. A silent no-op is worse than an error, because
    it is an error the client cannot see. Same reasoning that removed `origin`
    from TicketCreate: a field that exists is a field someone forwards.
    """
    assert "queue_id" not in TicketUpdate.model_fields
    assert "assigned_to" not in TicketUpdate.model_fields
