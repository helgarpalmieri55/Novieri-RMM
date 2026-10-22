"""The IMAP transport, ON THE WIRE.

WHY THIS FILE EXISTS. Every other email-intake test runs against
`FakeImapClient`, so the whole of `imap.py` -- the TLS context, `.PEEK`, UID
threading, folder encoding, literal parsing -- was covered by nothing at all.
That is not a gap in coverage percentage; it is a gap in *kind*, and two real
defects lived in it: a `FETCH (RFC822)` that would have set `\\Seen` at fetch
time (voiding spec §4 rule 2 against every real server while the unit test
that names rule 2 stayed green), and an `IMAP4_SSL` built with no
`ssl_context`, which handed the vault-decrypted mailbox password to anything
that answered the socket.

WHAT IT DOES. Binds a scripted IMAP server to `127.0.0.1:0` in a daemon
thread and points the REAL `imap_client_factory` at it. No network, no mail
server, no fixture beyond a throwaway certificate. It asserts on the BYTES
THE CLIENT SENDS, which is the only place these properties are observable.

A LISTENING SOCKET HERE IS NOT A SPEC §0.1 VIOLATION. §0.1 forbids the
*product* from becoming a mail receiver -- no inbound port on the control
plane, no MX change. It constrains what ships, not what the harness does to
observe what ships. `imap.py` and `poller.py` still contain no `bind`, no
`listen`, and no server socket of any kind.
"""

import base64
import contextlib
import ipaddress
import socket
import ssl
import threading
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.modules.email_intake.imap import (
    ImapError,
    encoded_folder,
    imap_client_factory,
)

_PASSWORD = "hunter2"
_USERNAME = "helpdesk@acme.test"


# --------------------------------------------------------------------------
# a throwaway certificate
# --------------------------------------------------------------------------


def _self_signed(tmp_path: Path, *, name: str, dns: list[str], ips: list[str]) -> tuple[Path, Path]:
    """A self-signed, CA-capable leaf certificate written to disk.

    CA-capable so the trusted-server tests can hand the SAME file to the
    client as a trust anchor via `SSL_CERT_FILE`, which is what
    `ssl.create_default_context()` reads (through OpenSSL's default verify
    paths) -- so the production code under test is exercised UNMODIFIED, with
    its own real context, rather than being handed a test context that would
    prove nothing about what production does.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    san: list[x509.GeneralName] = [x509.DNSName(entry) for entry in dns]
    san += [x509.IPAddress(ipaddress.ip_address(entry)) for entry in ips]
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / f"{name}.crt"
    key_path = tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


# --------------------------------------------------------------------------
# ~20 lines of scripted IMAP over a real socket
# --------------------------------------------------------------------------


def _readline(conn: Any) -> bytes:
    """One CRLF-terminated command line, byte at a time.

    Deliberately not `makefile()`: a buffered reader can read PAST the
    STARTTLS command into what the client will send encrypted, and then the
    upgrade test observes nothing. One byte at a time is irrelevant for
    twenty short lines and is trivially correct across the upgrade.
    """
    out = bytearray()
    while not out.endswith(b"\r\n"):
        chunk = conn.recv(1)
        if not chunk:
            break
        out += chunk
    return bytes(out)


class ScriptedImapServer:
    """An IMAP server that says just enough, and records everything heard.

    `cleartext` is the point of the whole class on the STARTTLS path: it is
    what a passive listener on the wire would have seen. A LOGIN line in
    there is a leaked credential.
    """

    def __init__(
        self,
        *,
        certfile: Path | None = None,
        keyfile: Path | None = None,
        implicit_tls: bool = True,
        offer_starttls: bool = False,
        messages: dict[str, bytes] | None = None,
        reject_login: bool = False,
        reject_select: bool = False,
        reject_store: bool = False,
        xoauth2: str | None = None,
    ) -> None:
        self.messages = dict(messages or {})
        self.implicit_tls = implicit_tls
        self.offer_starttls = offer_starttls
        self.reject_login = reject_login
        self.reject_select = reject_select
        self.reject_store = reject_store
        # None: no AUTHENTICATE support. "ok": accept. "challenge": answer the
        # way Microsoft and Google refuse -- a base64 JSON challenge, then NO
        # only once the client sends its empty continuation line.
        self.xoauth2 = xoauth2
        self.sasl_responses: list[bytes] = []
        self.saw_empty_continuation = False
        self.log: list[bytes] = []
        self.cleartext: list[bytes] = []
        self.handshake_failed = False
        self.tls_active = False

        self._ctx: ssl.SSLContext | None = None
        if certfile is not None and keyfile is not None:
            self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self._ctx.load_cert_chain(certfile, keyfile)

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(15.0)
        self.port = int(self._sock.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # -- lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        with contextlib.suppress(OSError):
            self._sock.close()
        self._thread.join(timeout=10)

    @property
    def heard(self) -> bytes:
        return b"".join(self.log)

    @property
    def heard_cleartext(self) -> bytes:
        return b"".join(self.cleartext)

    # -- the conversation --------------------------------------------------

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        try:
            if self.implicit_tls:
                assert self._ctx is not None
                try:
                    conn = self._ctx.wrap_socket(conn, server_side=True)
                except (ssl.SSLError, OSError):
                    # The client refused our certificate. This is the whole
                    # assertion of the rogue-listener test.
                    self.handshake_failed = True
                    return
                self.tls_active = True
            self._converse(conn)
        except (OSError, ssl.SSLError):
            return
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    def _converse(self, conn: Any) -> None:
        conn.sendall(b"* OK IMAP4rev1 scripted server ready\r\n")
        while True:
            line = _readline(conn)
            if not line:
                return
            self.log.append(line)
            if not self.tls_active:
                self.cleartext.append(line)

            tag, _, rest = line.rstrip(b"\r\n").partition(b" ")
            parts = rest.split()
            verb = parts[0].upper() if parts else b""
            if verb == b"UID" and len(parts) > 1:
                verb = b"UID " + parts[1].upper()
                args = parts[2:]
            else:
                args = parts[1:]

            if verb == b"CAPABILITY":
                caps = b"IMAP4rev1"
                if self.offer_starttls and not self.tls_active:
                    caps += b" STARTTLS"
                if self.xoauth2 is not None:
                    caps += b" AUTH=XOAUTH2"
                conn.sendall(b"* CAPABILITY " + caps + b"\r\n")
                conn.sendall(tag + b" OK CAPABILITY completed\r\n")
            elif verb == b"STARTTLS":
                if not self.offer_starttls:
                    conn.sendall(tag + b" BAD unknown command\r\n")
                    continue
                conn.sendall(tag + b" OK begin TLS negotiation\r\n")
                assert self._ctx is not None
                conn = self._ctx.wrap_socket(conn, server_side=True)
                self.tls_active = True
            elif verb == b"AUTHENTICATE" and self.xoauth2 is not None:
                conn.sendall(b"+ \r\n")
                response = _readline(conn)
                self.log.append(response)
                self.sasl_responses.append(response.rstrip(b"\r\n"))
                if self.xoauth2 == "ok":
                    conn.sendall(tag + b" OK AUTHENTICATE completed.\r\n")
                    continue
                challenge = base64.b64encode(
                    b'{"status":"401","schemes":"bearer","scope":"https://mail.example/"}'
                )
                conn.sendall(b"+ " + challenge + b"\r\n")
                after = _readline(conn)
                self.log.append(after)
                self.saw_empty_continuation = after == b"\r\n"
                conn.sendall(tag + b" NO AUTHENTICATE failed.\r\n")
            elif verb == b"LOGIN":
                if self.reject_login:
                    conn.sendall(tag + b' NO LOGIN failed for user "' + args[0] + b'"\r\n')
                else:
                    conn.sendall(tag + b" OK LOGIN completed\r\n")
            elif verb == b"SELECT":
                if self.reject_select:
                    conn.sendall(tag + b" NO Mailbox does not exist\r\n")
                    continue
                conn.sendall(b"* %d EXISTS\r\n" % len(self.messages))
                conn.sendall(b"* OK [UIDVALIDITY 1] UIDs valid\r\n")
                conn.sendall(tag + b" OK [READ-WRITE] SELECT completed\r\n")
            elif verb == b"UID SEARCH":
                uids = " ".join(self.messages).encode("ascii")
                conn.sendall(b"* SEARCH " + uids + b"\r\n")
                conn.sendall(tag + b" OK UID SEARCH completed\r\n")
            elif verb == b"UID FETCH":
                uid = args[0].decode("ascii")
                body = self.messages.get(uid, b"")
                conn.sendall(b"* 1 FETCH (UID %s BODY[] {%d}\r\n" % (args[0], len(body)))
                conn.sendall(body)
                conn.sendall(b")\r\n")
                conn.sendall(tag + b" OK UID FETCH completed\r\n")
            elif verb == b"UID STORE":
                if self.reject_store:
                    conn.sendall(tag + b" NO STORE rejected: mailbox is read-only\r\n")
                    continue
                conn.sendall(b"* 1 FETCH (UID " + args[0] + b" FLAGS (\\Seen))\r\n")
                conn.sendall(tag + b" OK UID STORE completed\r\n")
            elif verb == b"LOGOUT":
                conn.sendall(b"* BYE logging out\r\n")
                conn.sendall(tag + b" OK LOGOUT completed\r\n")
                return
            else:
                conn.sendall(tag + b" BAD unknown command\r\n")


@pytest.fixture()
def trusted_ca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A certificate for `127.0.0.1` that the PRODUCTION context will trust.

    `ssl.create_default_context()` calls `load_default_certs()`, which on
    POSIX honours OpenSSL's `SSL_CERT_FILE`. Setting it here means
    `imap_client_factory` builds its own real, verifying context and that
    context happens to trust our throwaway CA -- no seam, no patched code.
    """
    cert, key = _self_signed(tmp_path, name="127.0.0.1", dns=["localhost"], ips=["127.0.0.1"])
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    return cert, key


def _message(subject: str = "Printer trouble") -> bytes:
    message = EmailMessage()
    message["Message-ID"] = "<wire@acme.test>"
    message["From"] = "alice@acme.test"
    message["To"] = _USERNAME
    message["Subject"] = subject
    message.set_content("The printer on 2 is jammed.")
    return message.as_bytes()


def _connect(server: ScriptedImapServer, *, use_ssl: bool = True, folder: str = "INBOX") -> Any:
    return imap_client_factory(
        host="127.0.0.1",
        port=server.port,
        use_ssl=use_ssl,
        folder=folder,
        username=_USERNAME,
        password=_PASSWORD,
    )


# --------------------------------------------------------------------------
# F1: the credential must never reach a server we could not authenticate
# --------------------------------------------------------------------------


def test_an_untrusted_certificate_is_refused_and_no_login_is_sent(tmp_path: Path) -> None:
    """THE ROGUE LISTENER (review finding F1).

    A TLS server answering on the mailbox's host/port with a self-signed
    certificate for a name that is not ours. With `imaplib`'s default
    context this handshake SUCCEEDED and the very next line on the wire was
    `LOGIN helpdesk@acme.test "hunter2"`. It must now fail during the
    handshake, before a single byte of IMAP is written.
    """
    cert, key = _self_signed(tmp_path, name="rogue", dns=["totally-not-this-host.invalid"], ips=[])
    server = ScriptedImapServer(certfile=cert, keyfile=key, messages={"101": _message()})
    try:
        with pytest.raises(ImapError) as caught:
            _connect(server)
    finally:
        server.stop()

    assert caught.value.stage == "tls"
    assert caught.value.reason == "certificate-invalid"
    assert server.log == [], "no IMAP command may be sent to an unauthenticated server"
    assert _PASSWORD.encode() not in server.heard
    assert b"LOGIN" not in server.heard


def test_a_trusted_certificate_for_the_wrong_host_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of F1: verification is not just chain-of-trust.

    Here the certificate IS in the client's trust store, so the only thing
    standing between the credential and the wrong server is
    `check_hostname` -- which `imaplib`'s default context also disables.
    """
    cert, key = _self_signed(tmp_path, name="elsewhere", dns=["mail.somewhere-else.test"], ips=[])
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    server = ScriptedImapServer(certfile=cert, keyfile=key, messages={"101": _message()})
    try:
        with pytest.raises(ImapError) as caught:
            _connect(server)
    finally:
        server.stop()

    assert caught.value.reason == "certificate-invalid"
    assert _PASSWORD.encode() not in server.heard


# --------------------------------------------------------------------------
# F1b: the cleartext port upgrades or fails; it never logs in unencrypted
# --------------------------------------------------------------------------


def test_the_cleartext_port_upgrades_with_starttls_before_logging_in(trusted_ca: Any) -> None:
    """`use_ssl=False` is STARTTLS, not plaintext (review finding F1b).

    `cleartext` is exactly what a passive tap on the wire would have
    recorded. It must contain the CAPABILITY probe and the STARTTLS command
    and nothing else -- in particular not LOGIN.
    """
    cert, key = trusted_ca
    server = ScriptedImapServer(
        certfile=cert,
        keyfile=key,
        implicit_tls=False,
        offer_starttls=True,
        messages={"101": _message()},
    )
    try:
        client = _connect(server, use_ssl=False)
        assert client.fetch_unseen(10)
        client.close()
    finally:
        server.stop()

    assert b"STARTTLS" in server.heard_cleartext
    assert b"LOGIN" not in server.heard_cleartext, "the credential went out in the clear"
    assert _PASSWORD.encode() not in server.heard_cleartext
    assert b"LOGIN" in server.heard, "the login must still have happened, encrypted"


def test_a_server_without_starttls_fails_closed_with_the_password_unsent(
    trusted_ca: Any,
) -> None:
    """No fallback to plaintext, ever. An operator whose relay genuinely
    cannot do TLS gets `tls failed: starttls-unsupported` on the mailbox
    row -- a diagnosable configuration error rather than a silently leaked
    credential."""
    cert, key = trusted_ca
    server = ScriptedImapServer(
        certfile=cert, keyfile=key, implicit_tls=False, offer_starttls=False
    )
    try:
        with pytest.raises(ImapError) as caught:
            _connect(server, use_ssl=False)
    finally:
        server.stop()

    assert caught.value.stage == "tls"
    assert caught.value.reason == "starttls-unsupported"
    assert b"LOGIN" not in server.heard
    assert _PASSWORD.encode() not in server.heard


# --------------------------------------------------------------------------
# the wire form of the fetch: spec §4 rule 2's only transport-level guard
# --------------------------------------------------------------------------


def test_the_fetch_on_the_wire_is_body_peek_and_never_rfc822(trusted_ca: Any) -> None:
    """SPEC §4 RULE 2, AT THE TRANSPORT.

    `FETCH (RFC822)` is functionally `BODY[]` (RFC 3501 §6.4.5) and a
    `BODY[...]` fetch without `.PEEK` sets `\\Seen` AS A SIDE EFFECT OF THE
    FETCH -- before `_begin_row` has written anything, so a crash mid-poll
    loses the message permanently. `FakeImapClient` has no such side effect,
    so `test_seen_is_set_only_after_the_commit` stays green either way: this
    assertion is the only thing in the suite that can see the difference.
    """
    cert, key = trusted_ca
    raw = _message()
    server = ScriptedImapServer(certfile=cert, keyfile=key, messages={"101": raw})
    try:
        client = _connect(server)
        messages = client.fetch_unseen(10)
        client.close()
    finally:
        server.stop()

    assert messages == [("101", raw)], "the literal must round-trip byte for byte"
    assert b"BODY.PEEK[]" in server.heard
    assert b"RFC822" not in server.heard, "RFC822 would set \\Seen at fetch time"


def test_the_uid_from_search_is_the_uid_fetched_and_flagged(trusted_ca: Any) -> None:
    """UID commands, never sequence numbers (declared deviation D3).

    Sequence numbers are renumbered by EXPUNGE, so a number captured at
    SEARCH can address a DIFFERENT message by STORE time -- flagging an
    unread message as read, the unrecoverable direction. This asserts the
    identifier threaded from SEARCH through FETCH to STORE is the UID the
    server gave, and that every command carrying it is a `UID` command.
    """
    cert, key = trusted_ca
    server = ScriptedImapServer(
        certfile=cert,
        keyfile=key,
        messages={"4021": _message("first"), "4022": _message("second")},
    )
    try:
        client = _connect(server)
        messages = client.fetch_unseen(10)
        client.mark_seen(messages[1][0])
        client.close()
    finally:
        server.stop()

    assert [uid for uid, _ in messages] == ["4021", "4022"]
    heard = server.heard
    assert b"UID SEARCH UNSEEN" in heard
    assert b"UID FETCH 4021 (BODY.PEEK[])" in heard
    assert b"UID FETCH 4022 (BODY.PEEK[])" in heard
    assert b"UID STORE 4022 +FLAGS (\\Seen)" in heard
    # No bare (sequence-number) FETCH or STORE anywhere.
    for line in server.log:
        command = line.split(b" ", 1)[1] if b" " in line else b""
        assert not command.startswith((b"FETCH", b"STORE", b"SEARCH"))


def test_the_fetch_is_capped_at_the_limit(trusted_ca: Any) -> None:
    """`MAX_MESSAGES_PER_POLL` must bound ROUND TRIPS, not just the returned
    list -- capping after the fetch loop would still pay one `UID FETCH` per
    message in a five-figure backlog, which is the cost the bound exists to
    avoid (review finding F7)."""
    cert, key = trusted_ca
    server = ScriptedImapServer(
        certfile=cert,
        keyfile=key,
        messages={str(uid): _message(f"m{uid}") for uid in range(1, 6)},
    )
    try:
        client = _connect(server)
        messages = client.fetch_unseen(2)
        client.close()
    finally:
        server.stop()

    assert [uid for uid, _ in messages] == ["1", "2"]
    assert server.heard.count(b"UID FETCH") == 2, "the cap must stop the round trips too"


# --------------------------------------------------------------------------
# folder names, and the failure classifications the admin surface renders
# --------------------------------------------------------------------------


def test_a_non_ascii_folder_is_sent_as_modified_utf7(trusted_ca: Any) -> None:
    """RFC 3501 §5.1.3 (review finding F10). Raw UTF-8 is rejected by a
    conforming server, so `Boîte de réception` would never poll at all."""
    assert encoded_folder("INBOX") == '"INBOX"'
    assert encoded_folder("Sent Items") == '"Sent Items"'
    assert encoded_folder("Boîte") == '"Bo&AO4-te"'
    assert encoded_folder("R&D") == '"R&-D"'
    assert encoded_folder("受信トレイ") == '"&U9dP4TDIMOwwpA-"'

    cert, key = trusted_ca
    server = ScriptedImapServer(certfile=cert, keyfile=key)
    try:
        client = _connect(server, folder="Boîte")
        client.close()
    finally:
        server.stop()

    assert b'SELECT "Bo&AO4-te"' in server.heard


@pytest.mark.parametrize(
    ("kwargs", "stage", "reason"),
    [
        ({"reject_login": True}, "login", "rejected"),
        ({"reject_select": True}, "select", "rejected"),
    ],
)
def test_a_refusal_is_classified_by_stage(
    trusted_ca: Any, kwargs: dict[str, bool], stage: str, reason: str
) -> None:
    """Review finding F3, at the transport. Every one of these used to reach
    the poller as `imaplib.IMAP4.error`, whose `__name__` is `error`, so the
    mailbox row said `poll failed: error` for all of them. Note the scripted
    server echoes the username back in its LOGIN refusal, the way a real one
    does -- and none of it survives into `reason`."""
    cert, key = trusted_ca
    server = ScriptedImapServer(certfile=cert, keyfile=key, **kwargs)
    try:
        with pytest.raises(ImapError) as caught:
            _connect(server)
    finally:
        server.stop()

    assert caught.value.stage == stage
    assert caught.value.reason == reason
    assert _PASSWORD not in caught.value.reason
    assert _USERNAME not in caught.value.reason


def test_a_refused_store_is_classified_as_a_flag_rejection(trusted_ca: Any) -> None:
    """What `poller.py` turns into `flag failed: rejected` and, crucially,
    does NOT let abort the batch (review finding F2)."""
    cert, key = trusted_ca
    server = ScriptedImapServer(
        certfile=cert, keyfile=key, reject_store=True, messages={"7": _message()}
    )
    try:
        client = _connect(server)
        client.fetch_unseen(10)
        with pytest.raises(ImapError) as caught:
            client.mark_seen("7")
        client.close()
    finally:
        server.stop()

    assert caught.value.stage == "flag"
    assert caught.value.reason == "rejected"


def test_a_refused_connection_is_classified_without_touching_the_credential() -> None:
    """`connect failed: connection-refused`, from a port with nothing on it.
    Distinguishable at a glance from `tls failed: certificate-invalid` and
    `login failed: rejected`, which is the entire point of F3."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = int(probe.getsockname()[1])
    probe.close()

    with pytest.raises(ImapError) as caught:
        imap_client_factory(
            host="127.0.0.1",
            port=port,
            use_ssl=True,
            folder="INBOX",
            username=_USERNAME,
            password=_PASSWORD,
        )
    assert caught.value.stage == "connect"
    assert caught.value.reason in {"connection-refused", "network"}


# --------------------------------------------------------------------------
# XOAUTH2 (Microsoft 365 sign-in; mail-oauth design §4.5, §6.1 items 1-2)
# --------------------------------------------------------------------------


def _connect_with_token(server: ScriptedImapServer, *, password: str = "") -> Any:
    return imap_client_factory(
        host="127.0.0.1",
        port=server.port,
        use_ssl=True,
        folder="INBOX",
        username="support@toeshee.com",
        password=password,
        access_token="ACCESS-TOKEN",
        deadline_s=5.0,
    )


def test_xoauth2_sends_the_exact_sasl_string(trusted_ca: Any) -> None:
    """The protocol itself, as a passive listener would have seen it."""
    cert, key = trusted_ca
    server = ScriptedImapServer(certfile=cert, keyfile=key, xoauth2="ok")
    try:
        _connect_with_token(server).close()
    finally:
        server.stop()

    assert b"AUTHENTICATE XOAUTH2" in server.heard
    assert b"LOGIN" not in server.heard
    (response,) = server.sasl_responses
    assert base64.b64decode(response) == (
        b"user=support@toeshee.com\x01auth=Bearer ACCESS-TOKEN\x01\x01"
    )
    assert b"SELECT" in server.heard


def test_a_refused_token_sends_the_empty_continuation_and_does_not_hang(
    trusted_ca: Any,
) -> None:
    """A refusing server sends a JSON challenge and WAITS; without the empty
    reply the connection would sit until the deadline on every poll."""
    cert, key = trusted_ca
    server = ScriptedImapServer(certfile=cert, keyfile=key, xoauth2="challenge")
    try:
        with pytest.raises(ImapError) as caught:
            _connect_with_token(server)
    finally:
        server.stop()

    assert server.saw_empty_continuation
    assert caught.value.stage == "login"
    assert caught.value.reason == "rejected"
    assert "{" not in str(caught.value) and "scope" not in str(caught.value)


def test_a_token_is_never_followed_by_a_password(trusted_ca: Any) -> None:
    """No fallback: a second credential to a server that refused the first."""
    cert, key = trusted_ca
    server = ScriptedImapServer(certfile=cert, keyfile=key, xoauth2="challenge")
    try:
        with pytest.raises(ImapError):
            _connect_with_token(server, password=_PASSWORD)
    finally:
        server.stop()

    assert _PASSWORD.encode() not in server.heard
    assert b"LOGIN" not in server.heard


def test_the_password_path_is_unchanged(trusted_ca: Any) -> None:
    cert, key = trusted_ca
    server = ScriptedImapServer(certfile=cert, keyfile=key, xoauth2="ok")
    try:
        _connect(server).close()
    finally:
        server.stop()

    assert b"LOGIN" in server.heard
    assert b"XOAUTH2" not in server.heard
