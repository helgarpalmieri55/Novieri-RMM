"""Internal CA for agent mTLS identity (docs/03-security-model.md).

Production uses OpenBao's PKI secrets engine. Development and tests use a
local CA (ECDSA P-256 root persisted in the CA dir) with identical semantics.
Both are real CAs issuing real certificates — there is no skip-verification
mode anywhere in the platform.

Certificate identity binding: CN = device UUID, O = org UUID, and a SAN URI
`its://org/<org_id>/device/<device_id>`. The gateway maps the presented
certificate to its Agent row by SHA-256 fingerprint.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app.core.config import Settings

# How long an agent client certificate is valid for.
#
# 365 days, raised from 30 by docs/modules/agent/cert-renewal.md §0.1, in the
# same change that made rotation automatic. A 30-day certificate bought nothing
# once renewal is a routine background event, and it made every rotation defect
# fire twelve times a year instead of once.
#
# CHANGING THIS AFFECTS NEWLY ISSUED CERTIFICATES ONLY. A certificate already in
# the field keeps the NotAfter it was signed with; §0.8 explains why that needs
# no special handling (the rotation threshold is "less than a third of the TTL
# remains", and a 30-day certificate never has more than 30 days left, so every
# already-issued certificate is inside the window by definition).
AGENT_CERT_TTL_DAYS = 365


@dataclass(frozen=True)
class IssuedCert:
    cert_pem: str
    chain_pem: str
    serial: str
    fingerprint_sha256: str
    not_after: datetime


def fingerprint_sha256(cert_pem: str) -> str:
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


# Minimum key strength for an agent mTLS identity — its whole authentication
# baseline. Ed25519 (the agent default) and Ed448 are always acceptable;
# RSA must be >= 2048 bits; EC must be a NIST P-256/384/521 curve.
_MIN_RSA_BITS = 2048
_ALLOWED_EC_CURVES = {"secp256r1", "secp384r1", "secp521r1"}


def _require_strong_key(public_key: object) -> None:
    from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, rsa

    if isinstance(public_key, ed25519.Ed25519PublicKey | ed448.Ed448PublicKey):
        return
    if isinstance(public_key, rsa.RSAPublicKey):
        if public_key.key_size < _MIN_RSA_BITS:
            raise ValueError(  # i18n-exempt: CSR refusal, audited by type only
                f"weak RSA key ({public_key.key_size} bits); require >= {_MIN_RSA_BITS}"
            )
        return
    if isinstance(public_key, ec.EllipticCurvePublicKey):
        if public_key.curve.name not in _ALLOWED_EC_CURVES:
            raise ValueError(  # i18n-exempt: CSR refusal, audited by type only
                f"disallowed EC curve {public_key.curve.name!r}"
            )
        return
    raise ValueError(  # i18n-exempt: CSR refusal, audited by type only
        f"unsupported CSR key type {type(public_key).__name__}"
    )


class CAService(Protocol):
    async def sign_csr(
        self,
        csr_pem: str,
        *,
        device_id: UUID,
        org_id: UUID,
        ttl_days: int = AGENT_CERT_TTL_DAYS,
    ) -> IssuedCert: ...

    async def ca_bundle_pem(self) -> str: ...

    async def revoke(self, serial: str) -> None: ...


class MissingCAKeyError(RuntimeError):
    """Raised when the CA root key is absent and auto-generation is disallowed
    (production). A silently regenerated root would orphan every issued agent
    certificate (all mTLS admissions would fail) and break the CRL."""


class LocalCA:
    def __init__(self, ca_dir: Path, *, allow_generate: bool = True) -> None:
        self._dir = Path(ca_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._key_path = self._dir / "root.key"
        self._cert_path = self._dir / "root.crt"
        self._revoked_path = self._dir / "revoked.json"
        if self._key_path.exists():
            key = serialization.load_pem_private_key(self._key_path.read_bytes(), password=None)
            assert isinstance(key, ec.EllipticCurvePrivateKey)
            self._key = key
            self._cert = x509.load_pem_x509_certificate(self._cert_path.read_bytes())
        elif not allow_generate:
            raise MissingCAKeyError(  # i18n-exempt: startup config error, logged
                f"CA root key {self._key_path} not found and auto-generation is "
                "disabled in production; provision the CA before starting"
            )
        else:
            self._key, self._cert = self._create_root()

    def _create_root(self) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, "it-suite internal CA"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "it-suite"),
            ]
        )
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_cert_sign=True,
                    crl_sign=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )
        self._key_path.touch(mode=0o600)
        self._key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        self._key_path.chmod(0o600)
        self._cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return key, cert

    def _load_revoked(self) -> set[str]:
        if self._revoked_path.exists():
            return set(json.loads(self._revoked_path.read_text()))
        return set()

    async def sign_csr(
        self,
        csr_pem: str,
        *,
        device_id: UUID,
        org_id: UUID,
        ttl_days: int = AGENT_CERT_TTL_DAYS,
    ) -> IssuedCert:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
        if not csr.is_signature_valid:
            raise ValueError(  # i18n-exempt: CSR refusal, audited by type only
                "CSR signature invalid"
            )
        _require_strong_key(csr.public_key())
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name(
                    [
                        x509.NameAttribute(NameOID.COMMON_NAME, str(device_id)),
                        x509.NameAttribute(NameOID.ORGANIZATION_NAME, str(org_id)),
                    ]
                )
            )
            .issuer_name(self._cert.subject)
            .public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=ttl_days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.UniformResourceIdentifier(f"its://org/{org_id}/device/{device_id}")]
                ),
                critical=False,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
            )
            .sign(self._key, hashes.SHA256())
        )
        cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
        return IssuedCert(
            cert_pem=cert_pem,
            chain_pem=self._cert.public_bytes(serialization.Encoding.PEM).decode(),
            serial=format(cert.serial_number, "x"),
            fingerprint_sha256=fingerprint_sha256(cert_pem),
            not_after=cert.not_valid_after_utc,
        )

    def issue_server_cert(self, hostnames: list[str], ttl_days: int = 365) -> tuple[str, str]:
        """Issue a TLS server cert (for the gateway in dev/test). Returns
        (key_pem, cert_pem)."""
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0])]))
            .issuer_name(self._cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=ttl_days))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(h) for h in hostnames]),
                critical=False,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
            )
            .sign(self._key, hashes.SHA256())
        )
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        return key_pem, cert.public_bytes(serialization.Encoding.PEM).decode()

    async def ca_bundle_pem(self) -> str:
        return self._cert.public_bytes(serialization.Encoding.PEM).decode()

    async def revoke(self, serial: str) -> None:
        revoked = self._load_revoked()
        revoked.add(serial)
        self._revoked_path.write_text(json.dumps(sorted(revoked)))

    def is_revoked(self, serial: str) -> bool:
        return serial in self._load_revoked()


def _pem(text: str) -> str:
    """A PEM block that ENDS WITH EXACTLY ONE NEWLINE.

    OpenBao's PKI returns `certificate` / `issuing_ca` / `ca_chain` entries
    with NO trailing newline. `LocalCA` (via `cryptography`) returns them WITH
    one, so every test -- which uses `LocalCA` -- is green while the
    production CA backend is broken.

    The agent concatenates the two fields it is handed:

        chain := append(append([]byte{}, ident.CertPEM...), ident.ChainPEM...)
        cert, err := tls.X509KeyPair(chain, keyPEM)   // enroll.go

    so an unterminated `cert_pem` produces `-----END CERTIFICATE-----` butted
    straight against `-----BEGIN CERTIFICATE-----`. Go's PEM decoder finds
    nothing and enrollment dies with

        enroll: certificate and key do not form a pair:
        tls: failed to find any PEM data in certificate input

    i.e. NO DEVICE COULD EVER ENROLL against `ITS_CA_BACKEND=openbao`. Found
    by running deploy/README.md's first boot end to end for the first time.

    Normalised here, on the SERVER, rather than in the agent, deliberately:
    a fleet is always mixed-version (CLAUDE.md), and a server-side fix repairs
    every agent already in the field, including ones that will never be
    updated. Idempotent for `LocalCA`'s already-terminated output, so the two
    backends now emit byte-identical framing.
    """
    return text.strip() + "\n"


class OpenBaoPkiCA:
    def __init__(
        self,
        addr: str,
        token: str,
        *,
        role: str = "agent",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._addr = addr.rstrip("/")
        self._role = role
        self._client = client or httpx.AsyncClient(timeout=10)
        self._headers = {"X-Vault-Token": token}

    async def sign_csr(
        self,
        csr_pem: str,
        *,
        device_id: UUID,
        org_id: UUID,
        ttl_days: int = AGENT_CERT_TTL_DAYS,
    ) -> IssuedCert:
        # THE SAME TWO CHECKS `LocalCA.sign_csr` MAKES, AND FOR A STRONGER
        # REASON: this is the PRODUCTION backend.
        #
        # These were absent here until a review found it. `_require_strong_key`
        # and `is_signature_valid` were called only in `LocalCA` -- the
        # development backend -- so `ITS_CA_BACKEND=openbao`, the setting
        # `deploy/.env.example` tells every operator to use, posted the CSR
        # straight through.
        #
        # `deploy/openbao/init.sh` had made that load-bearing without knowing
        # it. The `agent` PKI role is created with `key_type=any`, which it
        # must be -- the role's default is `rsa` and the Go agent enrolls with
        # Ed25519, so a concrete `key_type` means NO DEVICE CAN EVER ENROLL --
        # and its comment justified the widening with "Key strength is enforced
        # server-side instead, by app/core/ca.py::_require_strong_key". That
        # sentence described a call that did not exist on this path, and Vault
        # performs no key validation of its own under `key_type=any`. So the
        # deployed configuration had NOTHING checking key strength at either
        # end.
        #
        # The reachable consequence: an enrollment token is deliberately
        # broadly distributable (one may enrol up to
        # `services/enrollment.py::MAX_TOKEN_USES` machines, and a managed
        # deployment hands one to a whole domain), so anyone holding one could
        # enroll with a 1024-bit RSA key and receive a certificate that is that
        # endpoint's entire mTLS identity for a year. This used to read
        # "`max_uses` is nullable"; the nullable default is gone and the
        # consequence is not.
        #
        # Checked BEFORE the POST, so a rejected CSR never reaches the PKI and
        # never consumes a serial.
        csr = x509.load_pem_x509_csr(csr_pem.encode())
        if not csr.is_signature_valid:
            raise ValueError(  # i18n-exempt: CSR refusal, audited by type only
                "CSR signature invalid"
            )
        _require_strong_key(csr.public_key())
        response = await self._client.post(
            f"{self._addr}/v1/pki/sign/{self._role}",
            headers=self._headers,
            json={
                "csr": csr_pem,
                "common_name": str(device_id),
                "uri_sans": f"its://org/{org_id}/device/{device_id}",
                "ttl": f"{ttl_days * 24}h",
            },
        )
        response.raise_for_status()
        data = response.json()["data"]
        cert_pem: str = _pem(data["certificate"])
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        return IssuedCert(
            cert_pem=cert_pem,
            chain_pem="".join(_pem(c) for c in (data.get("ca_chain") or [data["issuing_ca"]])),
            serial=str(data["serial_number"]),
            fingerprint_sha256=fingerprint_sha256(cert_pem),
            not_after=cert.not_valid_after_utc,
        )

    async def ca_bundle_pem(self) -> str:
        response = await self._client.get(f"{self._addr}/v1/pki/ca/pem", headers=self._headers)
        response.raise_for_status()
        return _pem(response.text)

    async def revoke(self, serial: str) -> None:
        response = await self._client.post(
            f"{self._addr}/v1/pki/revoke",
            headers=self._headers,
            json={"serial_number": serial},
        )
        response.raise_for_status()


def get_ca(settings: Settings) -> CAService:
    if settings.ca_backend == "local":
        return LocalCA(settings.ca_dir, allow_generate=settings.environment != "production")
    if not settings.openbao_addr or not settings.openbao_token:
        raise ValueError(  # i18n-exempt: startup config error, logged
            "openbao CA backend requires ITS_OPENBAO_ADDR and ITS_OPENBAO_TOKEN"
        )
    return OpenBaoPkiCA(settings.openbao_addr, settings.openbao_token)
