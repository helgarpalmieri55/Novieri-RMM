"""An INDEPENDENT WinRM acceptor for the agent-winrm CI job (plan Task 9, §I5).

It is deliberately not built from the agent's Go: SPNEGO and the MS-WSMV
message encryption are pyspnego's, Kerberos is MIT's (via python-gssapi), and
the multipart framing below is re-implemented after pywinrm's
`_decrypt_response`/`_build_message`. If the agent's AP-REQ, channel binding,
key choice, RFC 4121 wrap (flags, EC, RRC, rotation) or MS-WSMV split is
wrong, acceptance or decryption fails here, loudly.

Listeners:
  --http   HOST:PORT   5985-style: Kerberos + sealed messages, never plaintext
  --https  HOST:PORT   5986-style: TLS, channel binding = its own certificate
  --https-badcb HOST:PORT
                       TLS with a channel binding for a DIFFERENT certificate:
                       an agent that sends the right binding must be REJECTED
                       here, which proves MIT enforces the binding the agent
                       sends (and so that the 5986 success is not vacuous)
  /refuse/<ntlm|basic|credssp> on any listener answers 401 with that scheme.

WS-Man is emulated just enough for the agent's fixed exchange: Create,
Command (powershell -EncodedCommand), Send (stdin), Receive, Signal, Delete.
The command's "output" is computed from the decoded script:
  Write-Output WINRM-OK        -> "WINRM-OK\r\n"
  [Console]::In.ReadLine()     -> the stdin line it was sent + "\r\n"
anything else exits 1.

--kat-out records the first sealed reply (key, header, data, plaintext) so
gsswrap_test.go's known-answer vector is an INDEPENDENT encode.

CI throwaway: no authorization beyond Kerberos, no persistence.
"""

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import ssl
import struct
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import spnego
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from spnego.channel_bindings import GssChannelBindings

FQDN = "pc1.corp.example.com"
BOUNDARY = b"--Encrypted Boundary"
PROTOCOL = b"application/HTTP-SPNEGO-session-encrypted"
ENC_CT = 'multipart/encrypted;protocol="application/HTTP-SPNEGO-session-encrypted";boundary="Encrypted Boundary"'
ACTION_BASE = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/"

KAT_LOCK = threading.Lock()


def log(*a):
    print("[acceptor]", *a, file=sys.stderr, flush=True)


def self_signed(cn):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(cn)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def tls_context(key, cert):
    d = tempfile.mkdtemp()
    kp, cp = os.path.join(d, "k.pem"), os.path.join(d, "c.pem")
    with open(kp, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with open(cp, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cp, kp)
    return ctx


def cbt_for(cert):
    # RFC 5929 tls-server-end-point, SHA-256 for an ecdsa-with-SHA256 cert.
    h = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).digest()
    return GssChannelBindings(application_data=b"tls-server-end-point:" + h)


def envelope(body):
    return (
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:rsp="http://schemas.microsoft.com/wbem/wsman/1/windows/shell">'
        "<s:Body>" + body + "</s:Body></s:Envelope>"
    ).encode()


def fault(text):
    return envelope("<s:Fault><s:Reason><s:Text>" + text + "</s:Text></s:Reason></s:Fault>")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pyspnego-winrm-acceptor"

    # Per-connection state: BaseHTTPRequestHandler serves every request on one
    # TCP connection from one instance, which is exactly WinRM's binding of the
    # security context to the connection.
    def setup(self):
        super().setup()
        self.ctx = None
        self.script = None
        self.stdin = b""

    def log_message(self, fmt, *args):
        log(self.server.label, fmt % args)

    def send(self, status, body=b"", headers=None):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        m = re.match(r"^/refuse/(ntlm|basic|credssp)$", self.path)
        if m:
            scheme = {"ntlm": "NTLM", "basic": 'Basic realm="WSMAN"', "credssp": "CredSSP"}[m.group(1)]
            self.send(401, headers={"WWW-Authenticate": scheme})
            return

        auth = self.headers.get("Authorization")
        if auth:
            if not auth.startswith("Negotiate "):
                self.server.violations.append("non-Negotiate Authorization: " + auth.split(" ")[0])
                self.send(401, headers={"WWW-Authenticate": "Negotiate"})
                return
            ctx = spnego.server(
                hostname=FQDN, service="HTTP", protocol="negotiate", channel_bindings=self.server.cbt
            )
            try:
                out = ctx.step(base64.b64decode(auth[len("Negotiate "):]))
            except Exception as e:  # noqa: BLE001 -- any rejection is a 401
                # pyspnego can surface a GSS failure (e.g. BAD_BINDINGS) as a
                # secondary error; log the whole chain so the cause is legible.
                chain, cur = [], e
                while cur is not None and len(chain) < 5:
                    chain.append(repr(cur))
                    cur = cur.__cause__ or cur.__context__
                log(self.server.label, "REJECTED:", " <- ".join(chain))
                self.server.rejections += 1
                self.send(401, headers={"WWW-Authenticate": "Negotiate"})
                return
            if not ctx.complete:
                self.server.violations.append("context not complete after one leg")
                self.send(401, headers={"WWW-Authenticate": "Negotiate"})
                return
            log(self.server.label, "accepted", ctx.client_principal, "session key", len(ctx.session_key), "bytes")
            self.ctx = ctx
            self.server.accepts += 1
            hdrs = {"WWW-Authenticate": "Negotiate " + base64.b64encode(out).decode()} if out else {}
            self.send(200, headers=hdrs)
            if body:
                self.server.violations.append("body on the authentication request")
            return

        if self.ctx is None:
            self.server.violations.append("request before authentication")
            self.send(401, headers={"WWW-Authenticate": "Negotiate"})
            return

        if self.server.sealed:
            ct = self.headers.get("Content-Type", "")
            if not ct.startswith("multipart/encrypted"):
                self.server.violations.append("PLAINTEXT request on the sealed listener")
                self.send(400)
                return
            soap = self.decrypt(body)
        else:
            soap = body

        reply = self.wsman(soap)

        if self.server.sealed:
            out = self.encrypt(reply)
            self.send(200, out, {"Content-Type": ENC_CT})
        else:
            self.send(200, reply, {"Content-Type": "application/soap+xml;charset=UTF-8"})

    # --- MS-WSMV framing, after pywinrm (not the agent's Go) -----------------

    def decrypt(self, content):
        parts = [p for p in content.split(BOUNDARY + b"\r\n") if p]
        if len(parts) != 2:
            raise ValueError("expected 2 MIME parts, got %d" % len(parts))
        header, payload = parts[0].strip(), parts[1]
        if PROTOCOL not in header:
            raise ValueError("first part is not " + PROTOCOL.decode())
        expected = int(re.search(rb"Length=(\d+)", header).group(1))
        if not payload.endswith(BOUNDARY + b"--\r\n"):
            raise ValueError("no closing boundary")
        payload = payload[: -len(BOUNDARY + b"--\r\n")]
        prefix = b"\tContent-Type: application/octet-stream\r\n"
        if not payload.startswith(prefix):
            raise ValueError("second part is not application/octet-stream")
        enc = payload[len(prefix):]
        siglen = struct.unpack("<I", enc[:4])[0]
        sig, data = enc[4 : 4 + siglen], enc[4 + siglen :]
        # MIT's IOV unwrap takes the header buffer from this length: a wrong
        # split fails here, not just a wrong key.
        msg = self.ctx.unwrap_winrm(sig, data)
        if len(msg) != expected:
            raise ValueError("Length=%d but decrypted %d" % (expected, len(msg)))
        self.server.sealed_in += 1
        if self.server.kat_in is None:
            self.server.kat_in = {"header": sig.hex(), "data": data.hex(), "plaintext": msg.decode("utf-8", "replace")}
        return msg

    def encrypt(self, msg):
        res = self.ctx.wrap_winrm(msg)
        header, data = res.header, res.data
        self.record_kat(header, data, msg)
        return (
            BOUNDARY + b"\r\n"
            + b"\tContent-Type: " + PROTOCOL + b"\r\n"
            + b"\tOriginalContent: type=application/soap+xml;charset=UTF-8;Length=" + str(len(msg)).encode() + b"\r\n"
            + BOUNDARY + b"\r\n"
            + b"\tContent-Type: application/octet-stream\r\n"
            + struct.pack("<I", len(header)) + header + data
            + BOUNDARY + b"--\r\n"
        )

    def record_kat(self, header, data, msg):
        path = self.server.kat_out
        if not path:
            return
        with KAT_LOCK:
            if os.path.exists(path):
                return
            key = self.ctx.session_key
            kat = {
                "source": "pyspnego %s wrap_winrm (MIT krb5 via python-gssapi), acceptor side" % spnego.__version__
                if hasattr(spnego, "__version__") else "pyspnego wrap_winrm (MIT krb5), acceptor side",
                "key_type": {32: 18, 16: 17}.get(len(key), 0),
                "key": key.hex(),
                "header": header.hex(),
                "data": data.hex(),
                "plaintext": msg.decode(),
                "initiator_message_mit_decrypted": self.server.kat_in,
            }
            with open(path, "w") as f:
                json.dump(kat, f, indent=2)
            log(self.server.label, "KAT written to", path)

    # --- WS-Man, just enough -------------------------------------------------

    def wsman(self, soap):
        text = soap.decode("utf-8")
        am = re.search(r"<a:Action[^>]*>([^<]+)</a:Action>", text)
        action = am.group(1) if am else ""
        self.server.actions.append(action.rsplit("/", 1)[-1])
        if action.endswith("/transfer/Create"):
            return envelope("<rsp:Shell><rsp:ShellId>ACCEPTOR-SHELL</rsp:ShellId></rsp:Shell>")
        if action == ACTION_BASE + "Command":
            args = re.search(r"<rsp:Arguments>([^<]*)</rsp:Arguments>", text)
            cmd = re.search(r"<rsp:Command>([^<]*)</rsp:Command>", text)
            if not args or not cmd or cmd.group(1) != "powershell.exe":
                return fault("unexpected command")
            m = re.search(r"-EncodedCommand ([A-Za-z0-9+/=]+)$", args.group(1))
            if not m or not args.group(1).startswith("-NoProfile -NonInteractive "):
                return fault("unexpected arguments")
            self.script = base64.b64decode(m.group(1)).decode("utf-16-le")
            return envelope("<rsp:CommandResponse><rsp:CommandId>ACCEPTOR-CMD</rsp:CommandId></rsp:CommandResponse>")
        if action == ACTION_BASE + "Send":
            sm = re.search(r'<rsp:Stream Name="stdin"[^>]*>([^<]*)</rsp:Stream>', text)
            self.stdin += base64.b64decode(sm.group(1)) if sm else b""
            return envelope("<rsp:SendResponse/>")
        if action == ACTION_BASE + "Receive":
            if self.script == "Write-Output WINRM-OK":
                out, code = b"WINRM-OK\r\n", 0
            elif self.script == "[Console]::In.ReadLine()":
                out, code = self.stdin.split(b"\n", 1)[0] + b"\r\n", 0
            else:
                out, code = b"", 1
            return envelope(
                '<rsp:ReceiveResponse><rsp:Stream Name="stdout" CommandId="ACCEPTOR-CMD" End="true">'
                + base64.b64encode(out).decode()
                + '</rsp:Stream><rsp:CommandState CommandId="ACCEPTOR-CMD" '
                'State="http://schemas.microsoft.com/wbem/wsman/1/windows/shell/CommandState/Done">'
                "<rsp:ExitCode>" + str(code) + "</rsp:ExitCode></rsp:CommandState></rsp:ReceiveResponse>"
            )
        if action == ACTION_BASE + "Signal" or action.endswith("/transfer/Delete"):
            return envelope("")
        return fault("unknown action " + action)


def serve(label, addr, sealed, tls=None, cbt=None, kat_out=None):
    host, port = addr.rsplit(":", 1)
    srv = ThreadingHTTPServer((host or "0.0.0.0", int(port)), Handler)
    srv.daemon_threads = True
    srv.label, srv.sealed, srv.cbt, srv.kat_out = label, sealed, cbt, kat_out
    srv.accepts = srv.rejections = srv.sealed_in = 0
    srv.actions, srv.violations, srv.kat_in = [], [], None
    if tls is not None:
        srv.socket = tls.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(label, "listening on", addr)
    return srv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", required=True)
    ap.add_argument("--https", required=True)
    ap.add_argument("--https-badcb", required=True)
    ap.add_argument("--ca-out", required=True, help="PEM of the TLS certificate (the agent's CA bundle)")
    ap.add_argument("--kat-out", required=True)
    a = ap.parse_args()
    if not os.environ.get("KRB5_KTNAME"):
        sys.exit("KRB5_KTNAME must name the HTTP/%s keytab" % FQDN)

    key, cert = self_signed(FQDN)
    _, other = self_signed(FQDN)  # a certificate the agent never sees
    tls = tls_context(key, cert)

    serve("http", a.http, sealed=True, kat_out=a.kat_out)
    serve("https", a.https, sealed=False, tls=tls, cbt=cbt_for(cert))
    serve("https-badcb", a.https_badcb, sealed=False, tls=tls, cbt=cbt_for(other))

    # Written last: its existence is the readiness signal.
    with open(a.ca_out + ".tmp", "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    os.replace(a.ca_out + ".tmp", a.ca_out)
    log("ready; CA at", a.ca_out)
    threading.Event().wait()


if __name__ == "__main__":
    main()
