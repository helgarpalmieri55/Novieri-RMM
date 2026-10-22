"""Keycloak event -> ITMS audit action, and everything this refuses to forward.

THE CLOSED LIST IS THE DESIGN. Keycloak emits an event for every login, every
token refresh, every failed password attempt and every admin console click. All
of that in the ITMS audit log would make the log unreadable — and unreadable
evidence is not evidence. The four events below are the ones
`docs/modules/console-auth/spec.md` §4 names: the ways an account gains or
loses a way in.

Logins are deliberately absent: `auth.login` already exists in this chain,
written by the API when a token is presented, and duplicating it from a second
source would put two rows with different provenance behind one fact.

PURE, and that is why it is a separate module from the sweep. This function is
where the judgement lives; everything around it is plumbing that a test can
only reach through a database and an HTTP client.
"""

from typing import Any

#: WebAuthn credential types Keycloak reports. `webauthn-passwordless` is the
#: first-factor passkey this platform enables (console-auth §3.2);
#: `webauthn` is the second-factor form, which is not enabled here but is
#: mapped anyway -- an operator who turns it on should not silently lose the
#: audit trail for it.
_PASSKEY_TYPES = frozenset({"webauthn-passwordless", "webauthn"})

#: Keycloak type -> ITMS action, for events that need no detail inspection.
_DIRECT: dict[str, str] = {
    "FEDERATED_IDENTITY_LINK": "auth.identity_linked",
    "REMOVE_FEDERATED_IDENTITY": "auth.identity_unlinked",
}


def map_event(event: dict[str, Any]) -> str | None:
    """The ITMS audit action for one Keycloak event, or `None` to drop it.

    `None` is the common answer and the correct one: see the module docstring.
    """
    kind = event.get("type")
    if not isinstance(kind, str):
        return None

    if kind in _DIRECT:
        return _DIRECT[kind]

    if kind in ("UPDATE_CREDENTIAL", "REMOVE_CREDENTIAL"):
        details = event.get("details")
        credential_type = (
            details.get("credential_type") if isinstance(details, dict) else None
        )
        # A PASSWORD change is a credential change too, and it is deliberately
        # NOT forwarded: Keycloak already enforces and records password policy,
        # the event carries nothing this platform can act on, and it would fire
        # for every routine rotation across the whole realm. The events worth
        # having in this chain are the ones that add a NEW way in.
        if credential_type in _PASSKEY_TYPES:
            return (
                "auth.passkey_registered"
                if kind == "UPDATE_CREDENTIAL"
                else "auth.passkey_removed"
            )
        return None

    return None


def event_fingerprint(realm: str, event: dict[str, Any]) -> str:
    """A stable identity for one Keycloak event, for deduplication.

    KEYCLOAK'S DATE FILTERS ARE DAY-GRANULAR, so every sweep re-reads an
    overlapping window and sees the same events again. A duplicate does not
    corrupt the hash chain -- it pollutes it, which for evidence is nearly as
    bad: an auditor counting passkey registrations would count each one twice.

    Built from the fields Keycloak itself sets, never from anything this
    process computes, so the same event fingerprints identically on every run
    and across every replica of the worker.
    """
    import hashlib
    import json

    material = json.dumps(
        {
            "realm": realm,
            "time": event.get("time"),
            "type": event.get("type"),
            "userId": event.get("userId"),
            "sessionId": event.get("sessionId"),
            "details": event.get("details"),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode()).hexdigest()
