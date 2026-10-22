"""What is forwarded into the audit chain, and what is refused (§4, §7.1).

THE REFUSALS MATTER MORE THAN THE ACCEPTANCES, and they are the reason this
file leads with them. Keycloak emits an event for every login, every token
refresh, every failed password attempt and every admin console click. Forwarding
all of it would make the ITMS audit log unreadable — and unreadable evidence is
not evidence, so the failure would be silent and total rather than noisy.

No database and no HTTP: the judgement in this module is a pure function, which
is exactly why it is a separate module from the sweep that calls it.
"""

from app.modules.keycloak_events.mapping import event_fingerprint, map_event


class TestWhatIsForwarded:
    def test_a_passkey_registration(self) -> None:
        assert (
            map_event(
                {
                    "type": "UPDATE_CREDENTIAL",
                    "details": {"credential_type": "webauthn-passwordless"},
                }
            )
            == "auth.passkey_registered"
        )

    def test_a_passkey_removal(self) -> None:
        assert (
            map_event(
                {
                    "type": "REMOVE_CREDENTIAL",
                    "details": {"credential_type": "webauthn-passwordless"},
                }
            )
            == "auth.passkey_removed"
        )

    def test_second_factor_webauthn_too(self) -> None:
        """Not enabled on this platform, but an operator who turns it on should
        not silently lose the audit trail for it."""
        assert (
            map_event(
                {"type": "UPDATE_CREDENTIAL", "details": {"credential_type": "webauthn"}}
            )
            == "auth.passkey_registered"
        )

    def test_identity_provider_link_and_unlink(self) -> None:
        """How an account gains a SECOND way in — the thing §4 named."""
        assert map_event({"type": "FEDERATED_IDENTITY_LINK"}) == "auth.identity_linked"
        assert (
            map_event({"type": "REMOVE_FEDERATED_IDENTITY"}) == "auth.identity_unlinked"
        )


class TestWhatIsRefused:
    def test_a_login_is_not_forwarded(self) -> None:
        """THE FIREHOSE, and the failure nobody notices until the log is
        unusable. `auth.login` already exists in this chain, written by the API
        when a token is presented; a second source would put two rows with
        different provenance behind one fact."""
        assert map_event({"type": "LOGIN"}) is None
        assert map_event({"type": "LOGIN_ERROR"}) is None
        assert map_event({"type": "REFRESH_TOKEN"}) is None
        assert map_event({"type": "CODE_TO_TOKEN"}) is None

    def test_a_password_change_is_not_forwarded(self) -> None:
        """A credential change, and still not one for this chain: Keycloak
        already enforces and records password policy, the event carries nothing
        this platform can act on, and it fires for every routine rotation in
        the realm. What belongs here is a NEW way in."""
        assert (
            map_event(
                {"type": "UPDATE_CREDENTIAL", "details": {"credential_type": "password"}}
            )
            is None
        )

    def test_a_credential_event_with_no_details_is_dropped_not_guessed(self) -> None:
        assert map_event({"type": "UPDATE_CREDENTIAL"}) is None
        assert map_event({"type": "UPDATE_CREDENTIAL", "details": "not-a-dict"}) is None

    def test_malformed_input_claims_nothing(self) -> None:
        """These dicts come off an HTTP response. A mapper that raised would
        take down a sweep for one odd row."""
        assert map_event({}) is None
        assert map_event({"type": None}) is None
        assert map_event({"type": 42}) is None


class TestFingerprint:
    def test_the_same_event_fingerprints_the_same(self) -> None:
        """Keycloak's date filters are day-granular, so every sweep re-reads an
        overlapping window. Without a stable fingerprint an auditor counting
        passkey registrations would count each one once per sweep."""
        event = {
            "time": 1_766_000_000_000,
            "type": "UPDATE_CREDENTIAL",
            "userId": "u1",
            "details": {"credential_type": "webauthn-passwordless"},
        }
        assert event_fingerprint("it-suite", event) == event_fingerprint(
            "it-suite", dict(event)
        )

    def test_different_events_fingerprint_differently(self) -> None:
        base = {"time": 1, "type": "UPDATE_CREDENTIAL", "userId": "u1"}
        assert event_fingerprint("it-suite", base) != event_fingerprint(
            "it-suite", {**base, "userId": "u2"}
        )
        assert event_fingerprint("it-suite", base) != event_fingerprint(
            "it-suite", {**base, "time": 2}
        )
        # Two realms on one Keycloak are two different events.
        assert event_fingerprint("it-suite", base) != event_fingerprint("other", base)

    def test_key_order_does_not_change_the_fingerprint(self) -> None:
        """JSON key order is not stable across httpx versions or Keycloak
        releases, and a fingerprint that moved with it would defeat dedupe
        silently on the next upgrade."""
        first = {"type": "LOGIN", "userId": "u1", "time": 5}
        second = {"time": 5, "userId": "u1", "type": "LOGIN"}
        assert event_fingerprint("r", first) == event_fingerprint("r", second)
