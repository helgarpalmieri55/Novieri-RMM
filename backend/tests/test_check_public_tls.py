"""`deploy/scripts/check-public-tls.sh` against real TLS servers (design
2026-11-23-vps-y-marca-novieri §2: "if ACME fails, the check after start says
so").

Traefik serves its own self-signed certificate, without failing, until Let's
Encrypt has issued one -- and for good when it cannot. So the check refuses
anything a browser or an agent would refuse, and says which; and a trusted
certificate with fewer than 28 days left, which only a renewal that keeps
failing produces. Each case here is a local TLS server presenting one
certificate the test made: one a trusted CA issued for the host, one for
another name, Traefik's default, an expired one, and one with 20 days left.
`openssl s_client` does the checking, as on a host.
"""

import datetime as dt
import socket
import ssl
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "deploy" / "scripts" / "check-public-tls.sh"
HOST = "rmm.novieri.com"
NOW = dt.datetime.now(dt.UTC)


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(
    subject: str,
    issuer_key: ec.EllipticCurvePrivateKey,
    issuer: str,
    key: ec.EllipticCurvePrivateKey,
    *,
    sans: list[str] | None = None,
    ca: bool = False,
    days: tuple[int, int] = (-1, 30),
) -> x509.Certificate:
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(_name(issuer))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW + dt.timedelta(days=days[0]))
        .not_valid_after(NOW + dt.timedelta(days=days[1]))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if sans:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in sans]), critical=False
        )
    return builder.sign(issuer_key, hashes.SHA256())


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("pki")
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _cert("Test Root", ca_key, "Test Root", ca_key, ca=True, days=(-1, 365))
    (out / "root.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cases = {
        "good": ("leaf", ca_key, "Test Root", [HOST], (-1, 30)),
        "other": ("leaf", ca_key, "Test Root", ["other.example"], (-1, 30)),
        "expired": ("leaf", ca_key, "Test Root", [HOST], (-60, -1)),
        "short": ("leaf", ca_key, "Test Root", [HOST], (-60, 20)),
        "default": ("TRAEFIK DEFAULT CERT", None, "TRAEFIK DEFAULT CERT", None, (-1, 30)),
    }
    for case, (cn, issuer_key, issuer, sans, days) in cases.items():
        key = ec.generate_private_key(ec.SECP256R1())
        cert = _cert(cn, issuer_key or key, issuer, key, sans=sans, days=days)
        (out / f"{case}.pem").write_bytes(
            cert.public_bytes(serialization.Encoding.PEM)
            + key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    return {p.stem: p for p in out.iterdir()}


def _serve(certfile: Path) -> Iterator[int]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile)
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    stop = threading.Event()

    def loop() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            try:
                with context.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(1)
            except (ssl.SSLError, OSError):
                pass

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    yield port
    stop.set()
    thread.join()
    listener.close()


def _check(
    tmp_path: Path, port: int, *args: str, host: str = HOST
) -> subprocess.CompletedProcess[str]:
    deploy = tmp_path / "deploy"
    (deploy / "scripts").mkdir(parents=True, exist_ok=True)
    (deploy / "scripts" / SCRIPT.name).write_bytes(SCRIPT.read_bytes())
    (deploy / ".env").write_text(f"ITS_PUBLIC_HOST={host}\nITMS_BIND_IP=0.0.0.0\n")
    return subprocess.run(
        ["bash", str(deploy / "scripts" / SCRIPT.name), "--port", str(port), "--wait", "0", *args],
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def server(request: pytest.FixtureRequest, pki: dict[str, Path]) -> Iterator[int]:
    yield from _serve(pki[request.param])


@pytest.mark.parametrize("server", ["good"], indirect=True)
def test_a_trusted_certificate_for_the_host_passes(
    tmp_path: Path, server: int, pki: dict[str, Path]
) -> None:
    run = _check(tmp_path, server, "--cafile", str(pki["root"]))
    assert run.returncode == 0, run.stdout + run.stderr
    assert f"ok      127.0.0.1:{server} presents a trusted certificate for {HOST}" in run.stdout
    assert f"DNS:{HOST}" in run.stdout


@pytest.mark.parametrize(
    ("server", "use_root", "says"),
    [
        ("good", False, "does not verify (openssl code 21"),
        ("other", True, "does not verify (openssl code 62"),
        ("expired", True, "does not verify (openssl code 10"),
        ("default", True, "Traefik is still serving its own default certificate"),
    ],
    indirect=["server"],
)
def test_what_a_client_would_refuse_is_refused(
    tmp_path: Path, server: int, pki: dict[str, Path], use_root: bool, says: str
) -> None:
    args = ("--cafile", str(pki["root"])) if use_root else ()
    run = _check(tmp_path, server, *args)
    assert run.returncode == 1, run.stdout
    assert "REFUSE" in run.stderr and says in run.stderr, run.stderr
    assert "docker compose logs traefik | grep -i acme" in run.stderr


@pytest.mark.parametrize("server", ["short"], indirect=True)
def test_a_certificate_whose_renewal_is_overdue_is_refused(
    tmp_path: Path, server: int, pki: dict[str, Path]
) -> None:
    """Traefik renews 30 days before expiry and looks once a day: a healthy
    certificate never has fewer than about 29 days left. Fewer than 28 is a
    renewal that has been failing, and nothing else says so: Let's Encrypt
    stopped mailing expiry warnings in 2025."""
    run = _check(tmp_path, server, "--cafile", str(pki["root"]))
    assert run.returncode == 1, run.stdout
    assert "REFUSE" in run.stderr and "fewer than 28 days left" in run.stderr, run.stderr
    assert "docker compose logs traefik | grep -i acme" in run.stderr


def test_nothing_listening_is_refused(tmp_path: Path) -> None:
    with socket.create_server(("127.0.0.1", 0)) as free:
        port = free.getsockname()[1]
    run = _check(tmp_path, port)
    assert run.returncode == 1
    assert f"nothing answered TLS on 127.0.0.1:{port}" in run.stderr


def test_without_a_public_host_it_refuses_before_connecting(tmp_path: Path) -> None:
    run = _check(tmp_path, 9, host="your-host")
    assert run.returncode == 1
    assert "ITS_PUBLIC_HOST is not set in .env" in run.stderr
