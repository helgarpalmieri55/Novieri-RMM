"""The IMAP transport (spec §4).

**THIS MODULE OPENS NO LISTENING PORT AND ACCEPTS NO INBOUND CONNECTION.**
Every socket here is outbound, initiated by the control plane, to a mailbox
the operator configured (spec §0.1; the same posture invariant #2 requires of
the agent). Nothing in this module ever calls `bind`/`listen`.

**THE TLS CHANNEL IS AUTHENTICATED, ALWAYS.** `imaplib.IMAP4_SSL` with no
`ssl_context` falls back to `ssl._create_stdlib_context()`, which has
`check_hostname=False` and `verify_mode=CERT_NONE`: it will complete a
handshake against any certificate at all, including a self-signed one for a
different host, and the very next thing this module does is send
`LOGIN <username> "<password>"`. That hands the vault-decrypted mailbox
credential to whoever answered -- an ARP/DNS-spoofing neighbour on the
on-prem network this product is deployed into, a compromised resolver, a
hostile hop -- every poll interval, and lets that same party feed arbitrary
RFC822 into `process_message`. Invariant #3 is not "the password stays out of
`last_error`"; it is that the credential is only ever exposed at its
legitimate point of use. So every connection here is built with
`ssl.create_default_context()` (`check_hostname=True`, `CERT_REQUIRED`), the
same context `notification/delivery.py` uses for SMTP STARTTLS, and a
certificate that does not verify aborts BEFORE the LOGIN line is written.

**`use_ssl=False` MEANS STARTTLS, NOT PLAINTEXT.** There is no code path in
this module that sends a password over an unencrypted socket. On the
non-implicit-TLS port the client connects, reads CAPABILITY, and requires
`STARTTLS`; if the server does not advertise it, or the upgrade or its
certificate check fails, the poll FAILS CLOSED with the credential unsent.
`notification/delivery.py`'s `use_tls` path is the in-repo precedent, and
this is the same trade: an operator who genuinely has a cleartext-only relay
gets a clear `tls: starttls-unsupported` on the mailbox row rather than a
silently leaked credential.

**A `Protocol`, not a hard dependency on `imaplib`.** `poller.py` takes an
`ImapClientFactory` so its tests -- which are about the ORDER of the fetch,
the commit and the `\\Seen` flag -- run with no mail server. What that seam
CANNOT cover is this file: `.PEEK`, UID threading, folder encoding and the
TLS context are all invisible to a fake, which is exactly how the missing
`ssl_context` survived review. `tests/test_email_intake_imap.py` drives the
real factory against a scripted TLS socket on `127.0.0.1:0` and asserts on
the bytes that go out. (A listening socket in a TEST is not what spec §0.1
forbids -- §0.1 constrains the product, not the harness.)

**EVERY CALL HERE IS BLOCKING AND MUST BE DRIVEN THROUGH
`asyncio.to_thread`.** `imaplib` is synchronous socket code; this process
also serves the agent gateway's WebSocket connections on the same event
loop, so a blocking IMAP call made inline stalls every connected endpoint for
the length of someone else's network timeout. `poller.py` is the only caller
and does exactly that -- including the factory itself, which connects, logs
in and SELECTs.

**AND `asyncio.to_thread` CANNOT BE CANCELLED**, which is why `deadline_s`
exists. Abandoning the `await` abandons the coroutine, never the worker
thread, so a per-socket-operation timeout (`IMAP_TIMEOUT_S`) is not enough
to bound anything: it restarts on every byte, and a server that trickles
holds a thread out of the process's default executor for as long as it
likes. Callers on a REQUEST path must pass `deadline_s`; see
`_ConversationDeadline`.

**`BODY.PEEK[]`, NEVER `RFC822`.** Spec §4 rule 2 says `\\Seen` is set only
after the database commit, and a plain `FETCH ... (RFC822)` sets `\\Seen`
implicitly as a side effect of the fetch (RFC 3501 §6.4.5: RFC822 is
equivalent to BODY[], and any BODY[] without .PEEK sets the flag). Fetching
with `RFC822` would therefore mark the message read BEFORE any row was
written -- a crash mid-poll would lose it permanently, which is the single
failure the whole ordering exists to prevent. `.PEEK` is the form that does
not touch flags; `mark_seen` sets `\\Seen` explicitly, afterwards, and is
the only thing in this codebase that does. The fake client has no such side
effect, so this property has no unit-test guard at all -- the wire-level
assertion in `test_email_intake_imap.py` is its only guard.

**UID commands, never sequence numbers.** Message sequence numbers shift
when anything is expunged from the folder -- including by another client
between our SEARCH and our STORE -- so a sequence number captured during the
search can address a DIFFERENT message by the time `\\Seen` is set. That
would flag an unread message as read: unrecoverable, exactly the direction
rule 2 forbids. UIDs are stable for the life of the mailbox.

**NO CREDENTIAL IS RETAINED.** The password is a parameter of
`imap_client_factory`, used once for `LOGIN`, and never stored on the client
object, never logged (this module calls no logger), and never placed in an
exception message this module constructs. The LOGIN failure path chains
`from None` on purpose: an `imaplib` rejection routinely quotes the login it
refused, and that string must not survive into a traceback. `poller.py`
records only `ImapError.stage`/`.reason` -- constants written here -- in
`last_error`, never `str(exc)` (invariant #3).
"""

import base64
import contextlib
import imaplib
import socket
import ssl
import threading
from typing import Protocol

# Spec §4: "the IMAP connection is bounded by an explicit timeout". Matches
# `notification/delivery.py`'s `_SMTP_TIMEOUT_S` -- the same class of
# outbound mail conversation, from the same worker, with the same reason for
# being bounded (an unbounded one holds a worker slot forever).
#
# READ THIS BEFORE TREATING IT AS A DEADLINE. It is a PER-SOCKET-OPERATION
# timeout: `imaplib` hands it to `socket.settimeout`, which restarts on every
# byte that arrives. It bounds no exchange. A conversation is four or more
# operations (connect, greeting, LOGIN, SELECT, ...), so the honest worst
# case is a multiple of it -- and a server that sends one byte every 29
# seconds never trips it AT ALL. `_ConversationDeadline` below is the thing
# that actually bounds wall clock; this only bounds silence.
IMAP_TIMEOUT_S = 30.0


def _hard_close(sock: socket.socket) -> None:
    """Free the socket AND unblock whatever thread is parked in `recv` on it.

    `shutdown(SHUT_RDWR)` first, `close` second: closing alone does not
    reliably wake a blocked reader, while the shutdown delivers EOF to it
    immediately. Both suppressed -- this runs on a socket that may already be
    dead, and its whole job is to be the last word.
    """
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


class _ConversationDeadline:
    """A WALL-CLOCK budget for one whole IMAP conversation, enforced by
    closing the socket out from under the thread blocked on it.

    WHY A WATCHDOG AND NOT A TIMEOUT. `IMAP_TIMEOUT_S` cannot express this:
    it restarts on every byte, so a hostile or broken server that trickles
    holds the connection open forever without ever tripping it. And the
    conversation runs inside `asyncio.to_thread`, which CANNOT be cancelled
    -- abandoning the await abandons the coroutine, not the thread. The only
    thing that reliably ends a thread blocked in `recv` is closing its
    socket, which is what this does, from a daemon timer thread.

    WHAT IT DOES NOT BOUND. Name resolution: `socket.create_connection`
    resolves before any socket exists, `getaddrinfo` is not interruptible,
    and no socket is attached yet to close. A conversation's worst case is
    therefore the budget PLUS the platform resolver's own bound. Callers that
    need the request itself bounded must also bound their `await` (see
    `poller.test_mailbox`).

    `budget_s=None` means NO deadline, and that is the default everywhere
    except the operator-triggered connection test: a scheduled poll may
    legitimately spend minutes draining `MAX_MESSAGES_PER_POLL` messages off
    a slow link, and a session-wide budget would truncate the batch.
    """

    def __init__(self, budget_s: float | None) -> None:
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._expired = False
        self._timer: threading.Timer | None = None
        if budget_s is not None:
            self._timer = threading.Timer(budget_s, self._expire)
            self._timer.daemon = True
            self._timer.start()

    def attach(self, sock: socket.socket) -> None:
        """Adopt the socket the conversation is now using. Called again after
        a STARTTLS upgrade, because `wrap_socket` DETACHES the socket it
        wrapped -- closing the pre-upgrade object afterwards would close
        nothing. If the budget already expired, the new socket is closed
        immediately rather than escaping the deadline by arriving late."""
        with self._lock:
            expired = self._expired
            if not expired:
                self._sock = sock
        if expired:
            _hard_close(sock)

    def _expire(self) -> None:
        with self._lock:
            self._expired = True
            sock = self._sock
        if sock is not None:
            _hard_close(sock)

    def cancel(self) -> None:
        """Stop the watchdog: the conversation finished inside its budget.
        Idempotent, and a no-op on a timer that already fired."""
        if self._timer is not None:
            self._timer.cancel()


class _DeadlineIMAP4(imaplib.IMAP4):
    """`imaplib.IMAP4` that hands its socket to a `_ConversationDeadline` the
    moment it exists -- which is inside `IMAP4.__init__`, before the greeting
    is read, so a server that trickles its greeting is bounded too."""

    def __init__(
        self, host: str, port: int, *, timeout: float, deadline: _ConversationDeadline
    ) -> None:
        self._deadline = deadline
        super().__init__(host, port, timeout=timeout)

    # `_create_socket` is `imaplib`'s own hook (`IMAP4.open` calls it to
    # build `self.sock`) but is private, so typeshed does not declare it --
    # hence the ignores. `tests/test_email_intake_imap.py` drives the real
    # factory against a real socket, so a stdlib change that renamed this
    # hook fails there rather than silently disarming the deadline.
    def _create_socket(self, timeout: float | None) -> socket.socket:
        sock: socket.socket = super()._create_socket(timeout)  # type: ignore[misc]
        self._deadline.attach(sock)
        return sock


class _DeadlineIMAP4SSL(imaplib.IMAP4_SSL):
    """As `_DeadlineIMAP4`, but attaching the RAW socket before the TLS
    handshake as well as the wrapped one after it.

    `IMAP4_SSL._create_socket` connects and then hands the socket to
    `wrap_socket`; the handshake itself is a network exchange with a hostile
    party, so it must be inside the budget, and at that point the only thing
    that can be closed is the raw socket. The re-attach afterwards is not
    optional -- `wrap_socket` detaches its argument.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        ssl_context: ssl.SSLContext,
        timeout: float,
        deadline: _ConversationDeadline,
    ) -> None:
        self._deadline = deadline
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def _create_socket(self, timeout: float | None) -> socket.socket:
        # `IMAP4._create_socket`, not `super()`: `IMAP4_SSL`'s own override
        # is exactly "connect, then wrap", and this replaces it so the raw
        # socket can be adopted BETWEEN those two steps.
        raw: socket.socket = imaplib.IMAP4._create_socket(self, timeout)  # type: ignore[attr-defined]
        self._deadline.attach(raw)
        wrapped: socket.socket = self.ssl_context.wrap_socket(raw, server_hostname=self.host)
        self._deadline.attach(wrapped)
        return wrapped


class ImapError(Exception):
    """An IMAP conversation failed, tagged with WHERE and WHY.

    Carries only text THIS MODULE constructs -- never the server's response
    line, which on the LOGIN path routinely echoes the username and on any
    path is attacker-influenced.

    `stage` and `reason` are both drawn from small fixed vocabularies written
    in this file. They are what `poller.py` puts in `email_mailbox.last_error`
    for §10's admin surface, which is the entire reason they exist: before
    them every protocol failure -- a wrong password, a missing folder, a DNS
    failure, a refused certificate -- recorded the identical six characters
    (`error`, the unqualified name of `imaplib.IMAP4.error`), and an operator
    could not tell them apart.
    """

    def __init__(self, message: str, *, stage: str = "imap", reason: str = "protocol") -> None:
        super().__init__(message)
        self.stage = stage
        self.reason = reason


def failure_reason(exc: BaseException) -> str:
    """A short, SAFE, operator-facing classification of a poll failure.

    Every value returned is either a constant written in this file or a
    class NAME -- never `str(exc)`, which for a LOGIN rejection embeds the
    credential (invariant #3, spec §11.12). `__qualname__` rather than
    `__name__` for the fallback, because the unqualified names of the
    `imaplib` exceptions are all useless (`error`, `abort`, `readonly`).
    """
    if isinstance(exc, ImapError):
        return exc.reason
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "certificate-invalid"
    if isinstance(exc, ssl.SSLError):
        return "handshake-failed"
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, ConnectionRefusedError):
        return "connection-refused"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, imaplib.IMAP4.abort):
        return "protocol-abort"
    if isinstance(exc, imaplib.IMAP4.error):
        return "protocol"
    if isinstance(exc, OSError):
        return "network"
    return type(exc).__qualname__


def failure_stage(exc: BaseException, default: str) -> str:
    """Where the poll failed. An `ImapError` knows better than the caller's
    coarse guess -- the factory is one `await` to `poller.py` but four
    distinct stages (connect, tls, login, select) to an operator."""
    if isinstance(exc, ImapError):
        return exc.stage
    return default


class ImapClient(Protocol):
    """One connected, authenticated, folder-selected IMAP session.

    Deliberately three methods wide. The poller needs "what is unread",
    "mark this one read", and "hang up"; anything more (delete, move, copy)
    would be a destructive capability on a customer mailbox that no part of
    this module has a reason to hold.
    """

    def fetch_unseen(self, limit: int) -> list[tuple[str, bytes]]:
        """`(uid, raw RFC822 bytes)` for at most `limit` UNSEEN messages,
        flags untouched. The bound is the caller's; see
        `poller.MAX_MESSAGES_PER_POLL` for why one exists."""
        ...

    def mark_seen(self, uid: str) -> None:
        """Set `\\Seen` on one message. Called ONLY after the database
        commit (spec §4 rule 2)."""
        ...

    def close(self) -> None:
        """Release the connection. Must not raise."""
        ...


class ImapClientFactory(Protocol):
    """Builds one connected session.

    `deadline_s` is a WALL-CLOCK budget for the whole session -- connect
    through LOGOUT -- not a per-operation timeout, and `None` (the default,
    and what `poll_mailbox` uses) means none. Only the operator-triggered
    connection test sets it; see `_ConversationDeadline`.
    """

    def __call__(
        self,
        *,
        host: str,
        port: int,
        use_ssl: bool,
        folder: str,
        username: str,
        password: str,
        deadline_s: float | None = None,
        access_token: str | None = None,
    ) -> ImapClient: ...


def _modified_utf7(folder: str) -> str:
    """RFC 3501 §5.1.3 modified UTF-7, the ONLY folder-name encoding IMAP
    accepts.

    A folder called `Boîte de réception` or `受信トレイ` sent as raw UTF-8 is
    rejected by a conforming server, so the mailbox never polls and the
    operator sees `select: rejected` with no idea why. Printable US-ASCII
    passes through unchanged (so `INBOX` is still `INBOX`); everything else
    is UTF-16BE, base64'd with `/` spelled `,` and the padding dropped,
    wrapped in `&`...`-`. A literal `&` becomes `&-`.
    """
    out: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if not pending:
            return
        raw = "".join(pending).encode("utf-16-be")
        encoded = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        out.append(f"&{encoded}-")
        pending.clear()

    for char in folder:
        if char == "&":
            flush()
            out.append("&-")
        elif "\x20" <= char <= "\x7e":
            flush()
            out.append(char)
        else:
            pending.append(char)
    flush()
    return "".join(out)


def _quoted(folder: str) -> str:
    """IMAP quoted-string form of a folder name.

    `imaplib.IMAP4.select` interpolates the mailbox name into the command
    line verbatim, so an ordinary folder containing a space (`Sent Items`,
    `Shared/Help Desk`) would be sent as two atoms and rejected. Quoting is
    valid for every folder name, including `INBOX`.
    """
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def encoded_folder(folder: str) -> str:
    """What actually goes on the wire for `folder`: modified UTF-7, quoted."""
    return _quoted(_modified_utf7(folder))


class _ImapLibClient:
    """`ImapClient` over `imaplib`. Every method blocks."""

    def __init__(self, conn: imaplib.IMAP4, deadline: _ConversationDeadline) -> None:
        self._conn = conn
        self._deadline = deadline

    def fetch_unseen(self, limit: int) -> list[tuple[str, bytes]]:
        try:
            typ, data = self._conn.uid("SEARCH", "UNSEEN")
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
                "UID SEARCH UNSEEN failed", stage="fetch", reason=failure_reason(exc)
            ) from exc
        if typ != "OK":
            raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
                "UID SEARCH UNSEEN was refused", stage="fetch", reason="rejected"
            )
        first = data[0] if data else None
        uids = first.split() if isinstance(first, bytes) else []

        messages: list[tuple[str, bytes]] = []
        # The cap is applied to the UID LIST, before any FETCH round trip:
        # capping the returned list instead would still pay one round trip
        # per message in the backlog, which is the cost the bound exists to
        # avoid (see `poller.MAX_MESSAGES_PER_POLL`).
        for raw_uid in uids[:limit]:
            uid = raw_uid.decode("ascii", "replace")
            # `.PEEK` is load-bearing: without it the fetch itself sets
            # `\Seen` and spec §4 rule 2 is void (module docstring).
            try:
                typ, parts = self._conn.uid("FETCH", uid, "(BODY.PEEK[])")
            except (imaplib.IMAP4.error, OSError) as exc:
                raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
                    "UID FETCH failed", stage="fetch", reason=failure_reason(exc)
                ) from exc
            if typ != "OK":
                # One unreadable message must not abort the batch -- the rest
                # of the mailbox is still deliverable (spec §11.10). It stays
                # UNSEEN, so the next poll retries it.
                continue
            body = _literal(parts)
            if body is None:
                continue
            messages.append((uid, body))
        return messages

    def mark_seen(self, uid: str) -> None:
        try:
            typ, _ = self._conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
                "UID STORE +FLAGS (\\Seen) failed", stage="flag", reason=failure_reason(exc)
            ) from exc
        if typ != "OK":
            raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
                "UID STORE +FLAGS (\\Seen) was refused", stage="flag", reason="rejected"
            )

    def close(self) -> None:
        """LOGOUT only -- deliberately NOT `IMAP4.close()`, which issues
        CLOSE and thereby EXPUNGES anything flagged `\\Deleted` in the
        folder. Nothing here ever sets `\\Deleted`, but a poller that can
        permanently destroy mail in a customer's mailbox as a side effect of
        hanging up is a capability this module has no reason to hold.

        Never raises: this runs in the `finally` of the poll, where an error
        would mask the real failure.

        The conversation deadline is cancelled LAST and unconditionally: the
        LOGOUT itself is a network round trip and must stay inside the
        budget, and a watchdog left armed on a finished session would close a
        socket some later connection might have inherited the fd of.
        """
        try:
            with contextlib.suppress(Exception):
                self._conn.logout()
        finally:
            self._deadline.cancel()


def _literal(parts: list[object]) -> bytes | None:
    """The message body out of an `imaplib` FETCH response.

    `imaplib` returns a list mixing bare bytes (`b')'`) with
    `(header, literal)` tuples; the body is the literal of the first tuple.
    """
    for part in parts:
        if isinstance(part, tuple) and len(part) > 1:
            payload = part[1]
            if isinstance(payload, bytes | bytearray):
                return bytes(payload)
    return None


def _client_ssl_context() -> ssl.SSLContext:
    """A VERIFYING TLS context: `check_hostname=True`, `CERT_REQUIRED`.

    Same call `notification/delivery.py` makes for SMTP STARTTLS. Never
    replace this with `imaplib`'s default -- see the module docstring.
    """
    return ssl.create_default_context()


def _open(host: str, port: int, use_ssl: bool, deadline: _ConversationDeadline) -> imaplib.IMAP4:
    """Connect, and get the channel encrypted, BEFORE anything is sent.

    Every failure is classified into a `stage`/`reason` pair the operator can
    act on: `connect: dns` and `tls: certificate-invalid` are different
    problems with different owners, and both used to record the string
    `poll failed: error`.

    A `deadline` that expires mid-connect closes the socket, which surfaces
    here as an ordinary `OSError` and is classified `connect: network` (or
    `tls: handshake-failed` if it lands during the handshake) -- deliberately
    not a distinct vocabulary entry, because from the operator's chair "the
    server stopped talking" is what happened.
    """
    conn: imaplib.IMAP4
    try:
        if use_ssl:
            conn = _DeadlineIMAP4SSL(
                host,
                port,
                ssl_context=_client_ssl_context(),
                timeout=IMAP_TIMEOUT_S,
                deadline=deadline,
            )
        else:
            conn = _DeadlineIMAP4(host, port, timeout=IMAP_TIMEOUT_S, deadline=deadline)
    except ssl.SSLCertVerificationError as exc:
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            "the server's TLS certificate was not accepted",
            stage="tls",
            reason="certificate-invalid",
        ) from exc
    except ssl.SSLError as exc:
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            "the TLS handshake failed", stage="tls", reason="handshake-failed"
        ) from exc
    except socket.gaierror as exc:
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            "the host name did not resolve", stage="connect", reason="dns"
        ) from exc
    except ConnectionRefusedError as exc:
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            "the connection was refused", stage="connect", reason="connection-refused"
        ) from exc
    except TimeoutError as exc:
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            "the connection timed out", stage="connect", reason="timeout"
        ) from exc
    except OSError as exc:
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            "the connection failed", stage="connect", reason="network"
        ) from exc
    except imaplib.IMAP4.error as exc:
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            "the server's greeting was not understood", stage="connect", reason="protocol"
        ) from exc

    if not use_ssl:
        try:
            _starttls(conn)
        except BaseException:
            with contextlib.suppress(Exception):
                conn.shutdown()
            raise
    return conn


def _starttls(conn: imaplib.IMAP4) -> None:
    """Upgrade a cleartext connection, or FAIL. Never falls back.

    Reached only when the mailbox row says `use_ssl=false`. The password has
    not been sent at this point and will not be sent if this raises -- that
    ordering is the whole point (module docstring).
    """
    if "STARTTLS" not in getattr(conn, "capabilities", ()):
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            "the server does not offer STARTTLS on this port",
            stage="tls",
            reason="starttls-unsupported",
        )
    try:
        conn.starttls(_client_ssl_context())
        # `wrap_socket` DETACHED the socket the deadline was holding; hand it
        # the upgraded one or the budget silently stops applying.
        deadline = getattr(conn, "_deadline", None)
        if deadline is not None:
            deadline.attach(conn.sock)
    except ssl.SSLCertVerificationError as exc:
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            "the server's TLS certificate was not accepted",
            stage="tls",
            reason="certificate-invalid",
        ) from exc
    except ssl.SSLError as exc:
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            "the TLS handshake failed", stage="tls", reason="handshake-failed"
        ) from exc
    except imaplib.IMAP4.error as exc:
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            "STARTTLS was refused", stage="tls", reason="starttls-refused"
        ) from exc
    except OSError as exc:
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            "the connection failed during STARTTLS", stage="tls", reason="network"
        ) from exc


def _login(conn: imaplib.IMAP4, username: str, password: str) -> None:
    """`from None`, not `from exc`: an `imaplib` LOGIN rejection routinely
    quotes the credentials it refused, and chaining it would put the password
    in any traceback anything ever prints (invariant #3). The classification
    below is all an operator needs and all that is safe to keep."""
    try:
        conn.login(username, password)
    except imaplib.IMAP4.error:
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            "LOGIN was refused", stage="login", reason="rejected"
        ) from None
    except OSError:
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            "the connection failed during LOGIN", stage="login", reason="network"
        ) from None


def _xoauth2(conn: imaplib.IMAP4, username: str, access_token: str) -> None:
    """SASL XOAUTH2, as Microsoft 365 (and Google) expect it.

    A refusing server answers the credential with a base64 JSON challenge and
    WAITS for a reply before sending NO; answering it with an empty line is
    what ends the exchange instead of leaving the connection hanging until the
    deadline on every poll. The challenge is discarded, never surfaced: it is
    remote-controlled text. `from None` for the same reason `_login` uses it.
    """
    sasl = f"user={username}\x01auth=Bearer {access_token}\x01\x01".encode()
    sent = False

    def respond(challenge: bytes) -> bytes:
        nonlocal sent
        if sent:
            return b""
        sent = True
        return sasl

    try:
        conn.authenticate("XOAUTH2", respond)
    except imaplib.IMAP4.error:
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            "the Microsoft sign-in was refused", stage="login", reason="rejected"
        ) from None
    except OSError:
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            "the connection failed during sign-in", stage="login", reason="network"
        ) from None


def _select(conn: imaplib.IMAP4, folder: str) -> None:
    try:
        typ, _ = conn.select(encoded_folder(folder))
    except imaplib.IMAP4.error as exc:
        # The folder name is the operator's own configuration, not attacker
        # input, so naming it is safe and is the single most useful thing to
        # say about this failure.
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            f"SELECT {folder!r} failed", stage="select", reason="rejected"
        ) from exc
    except OSError as exc:
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            f"SELECT {folder!r} failed", stage="select", reason=failure_reason(exc)
        ) from exc
    if typ != "OK":
        raise ImapError(  # i18n-exempt: poller stores stage and reason, never this text
            f"SELECT {folder!r} was refused", stage="select", reason="rejected"
        )


def imap_client_factory(
    *,
    host: str,
    port: int,
    use_ssl: bool,
    folder: str,
    username: str,
    password: str,
    deadline_s: float | None = None,
    access_token: str | None = None,
) -> ImapClient:
    """Connect over a VERIFIED TLS channel, authenticate and SELECT `folder`.
    BLOCKING -- call it through `asyncio.to_thread` (module docstring).

    `deadline_s` bounds the WHOLE session in wall clock (connect through the
    `close()` that ends it), enforced by closing the socket -- the only thing
    that ends an uncancellable thread blocked on a server that trickles. See
    `_ConversationDeadline` for what it does not cover. `None` means no
    deadline and leaves the per-operation `IMAP_TIMEOUT_S` as the only bound,
    which is what a scheduled poll wants and what a request-facing caller
    must not rely on.

    `use_ssl` selects implicit TLS (the column default, port 993) versus
    STARTTLS on a cleartext port (143), mirroring the shape
    `notification/delivery.py` already accepts for SMTP. It does NOT select
    "encrypted" versus "not": both paths verify the certificate and both
    refuse to send the credential if they cannot.

    A failure at any step closes the socket before propagating, so a refused
    LOGIN or a missing folder cannot leak a half-open connection per poll
    interval.

    `access_token`, when given, selects SASL XOAUTH2 and `password` is not
    consulted: there is no fallback that offers a second credential to a
    server that refused the first. Both paths refuse to send a credential
    over a channel they could not verify.
    """
    deadline = _ConversationDeadline(deadline_s)
    try:
        conn = _open(host, port, use_ssl, deadline)
    except BaseException:
        deadline.cancel()
        raise
    try:
        if access_token is not None:
            _xoauth2(conn, username, access_token)
        else:
            _login(conn, username, password)
        _select(conn, folder)
    except BaseException:
        with contextlib.suppress(Exception):
            conn.logout()
        deadline.cancel()
        raise
    return _ImapLibClient(conn, deadline)
