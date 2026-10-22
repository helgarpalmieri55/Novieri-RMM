from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from app.core.ca import LocalCA, MissingCAKeyError, OpenBaoPkiCA, fingerprint_sha256


def _make_csr() -> tuple[Ed25519PrivateKey, str]:
    key = Ed25519PrivateKey.generate()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "placeholder")]))
        .sign(key, None)
    )
    from cryptography.hazmat.primitives.serialization import Encoding

    return key, csr.public_bytes(Encoding.PEM).decode()


async def test_local_ca_signs_csr_with_device_identity(tmp_path: Path) -> None:
    ca = LocalCA(tmp_path)
    device_id, org_id = uuid4(), uuid4()
    _, csr_pem = _make_csr()
    issued = await ca.sign_csr(csr_pem, device_id=device_id, org_id=org_id, ttl_days=30)

    cert = x509.load_pem_x509_certificate(issued.cert_pem.encode())
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == str(device_id)
    assert cert.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)[0].value == str(org_id)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    uris = list(san.get_values_for_type(x509.UniformResourceIdentifier))
    assert uris == [f"its://org/{org_id}/device/{device_id}"]

    # ttl honored (within a day of slack)
    remaining = cert.not_valid_after_utc - datetime.now(UTC)
    assert timedelta(days=29) < remaining <= timedelta(days=30)

    # chains to the CA root
    root = x509.load_pem_x509_certificate((await ca.ca_bundle_pem()).encode())
    from cryptography.hazmat.primitives.asymmetric import ec

    root.public_key().verify(  # raises if not signed by the root
        cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm)
    )

    assert issued.fingerprint_sha256 == fingerprint_sha256(issued.cert_pem)
    assert issued.serial == format(cert.serial_number, "x")


async def test_local_ca_root_is_persistent(tmp_path: Path) -> None:
    bundle1 = await LocalCA(tmp_path).ca_bundle_pem()
    bundle2 = await LocalCA(tmp_path).ca_bundle_pem()
    assert bundle1 == bundle2


async def test_fingerprint_is_stable_and_hex(tmp_path: Path) -> None:
    ca = LocalCA(tmp_path)
    _, csr_pem = _make_csr()
    issued = await ca.sign_csr(csr_pem, device_id=uuid4(), org_id=uuid4())
    fp = fingerprint_sha256(issued.cert_pem)
    assert fp == fingerprint_sha256(issued.cert_pem)
    assert len(fp) == 64 and fp == fp.lower()
    int(fp, 16)  # valid hex


async def test_local_ca_revocation(tmp_path: Path) -> None:
    ca = LocalCA(tmp_path)
    _, csr_pem = _make_csr()
    issued = await ca.sign_csr(csr_pem, device_id=uuid4(), org_id=uuid4())
    assert not ca.is_revoked(issued.serial)
    await ca.revoke(issued.serial)
    assert ca.is_revoked(issued.serial)
    # revocation list survives reload
    assert LocalCA(tmp_path).is_revoked(issued.serial)


async def test_openbao_pki_ca_parses_response(tmp_path: Path) -> None:
    # produce a realistic signed cert via a LocalCA, then serve it from a mock
    reference = LocalCA(tmp_path)
    device_id, org_id = uuid4(), uuid4()
    _, csr_pem = _make_csr()
    issued = await reference.sign_csr(csr_pem, device_id=device_id, org_id=org_id)
    bundle = await reference.ca_bundle_pem()
    revoked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pki/sign/agent"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "certificate": issued.cert_pem,
                        "issuing_ca": bundle,
                        "ca_chain": [bundle],
                        "serial_number": issued.serial,
                    }
                },
            )
        if request.url.path.endswith("/pki/ca/pem"):
            return httpx.Response(200, text=bundle)
        if request.url.path.endswith("/pki/revoke"):
            revoked.append(request.read().decode())
            return httpx.Response(200, json={"data": {"revocation_time": 1}})
        return httpx.Response(404)

    ca = OpenBaoPkiCA(
        "https://bao.test",
        "token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await ca.sign_csr(csr_pem, device_id=device_id, org_id=org_id)
    assert result.cert_pem == issued.cert_pem
    assert result.fingerprint_sha256 == issued.fingerprint_sha256
    assert await ca.ca_bundle_pem() == bundle
    await ca.revoke(result.serial)
    assert revoked and result.serial in revoked[0]


async def test_openbao_pem_is_newline_terminated_so_the_agent_can_parse_the_chain(
    tmp_path: Path,
) -> None:
    """OpenBao returns PEM with NO trailing newline. The agent concatenates.

    This is the test the `_pem` normaliser in `app/core/ca.py` was missing, and
    its absence is why NO DEVICE COULD ENROLL against `ITS_CA_BACKEND=openbao`
    while the suite stayed green for an entire phase.

    The fixture is the whole point. `test_openbao_pki_ca_parses_response` above
    serves `LocalCA`'s output, which `cryptography` already terminates with a
    newline -- so its `result.cert_pem == issued.cert_pem` passes identically
    with the normaliser present or deleted. Here the trailing newline is
    STRIPPED, which is what a real OpenBao PKI actually sends, and the
    assertion is the operation the endpoint performs:

        chain := append(append([]byte{}, ident.CertPEM...), ident.ChainPEM...)
        cert, err := tls.X509KeyPair(chain, keyPEM)   // agent enroll.go

    Unterminated, that butts `-----END CERTIFICATE-----` straight against the
    next `-----BEGIN CERTIFICATE-----`; Go's PEM decoder finds nothing and
    enrollment dies with "tls: failed to find any PEM data in certificate
    input".

    **A PYTHON TEST CANNOT SEE THE DEFECT THE WAY GO DOES, and that shaped the
    assertions below.** `cryptography.x509.load_pem_x509_certificates` parses
    the glued boundary happily -- measured, not assumed: fed
    `-----END CERTIFICATE----------BEGIN CERTIFICATE-----` it returns TWO
    certificates, while Go returns none. So a "does the concatenation parse"
    assertion is exactly the decoration this project keeps catching: it passes
    with the normaliser deleted.

    What bites is asserting the FRAMING itself -- one trailing newline, and no
    glued `END`/`BEGIN` boundary anywhere in the concatenation. Verified by
    mutation: replacing `_pem`'s body with `return text` turns those two
    assertions red and leaves the parse assertion green.
    """
    reference = LocalCA(tmp_path)
    device_id, org_id = uuid4(), uuid4()
    _, csr_pem = _make_csr()
    issued = await reference.sign_csr(csr_pem, device_id=device_id, org_id=org_id)
    bundle = await reference.ca_bundle_pem()

    # Exactly what OpenBao puts on the wire: no trailing newline, anywhere.
    unterminated_cert = issued.cert_pem.rstrip("\n")
    unterminated_bundle = bundle.rstrip("\n")
    assert not unterminated_cert.endswith("\n")
    assert not unterminated_bundle.endswith("\n")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pki/sign/agent"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "certificate": unterminated_cert,
                        "issuing_ca": unterminated_bundle,
                        "ca_chain": [unterminated_bundle],
                        "serial_number": issued.serial,
                    }
                },
            )
        if request.url.path.endswith("/pki/ca/pem"):
            return httpx.Response(200, text=unterminated_bundle)
        return httpx.Response(404)

    ca = OpenBaoPkiCA(
        "https://bao.test",
        "token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await ca.sign_csr(csr_pem, device_id=device_id, org_id=org_id)

    concatenated = result.cert_pem + result.chain_pem

    # THE FALSIFIER. Go's decoder needs the newline between the blocks; this is
    # the byte sequence it chokes on, and the assertion that goes red when the
    # normaliser is removed.
    assert "-----END CERTIFICATE----------BEGIN CERTIFICATE-----" not in concatenated

    # Terminated, and not double-terminated: both backends emit identical
    # framing, so a deployment can switch between them without the endpoint
    # noticing.
    assert result.cert_pem.endswith("\n") and not result.cert_pem.endswith("\n\n")
    assert result.cert_pem == issued.cert_pem

    # Kept for shape, NOT as a falsifier -- see the docstring: Python parses the
    # glued boundary either way, so this passes with the normaliser deleted.
    assert len(x509.load_pem_x509_certificates(concatenated.encode())) == 2
    assert len(x509.load_pem_x509_certificates((await ca.ca_bundle_pem()).encode())) == 1


async def test_openbao_ca_refuses_a_weak_key_without_asking_the_pki(tmp_path: Path) -> None:
    """The PRODUCTION CA backend checks key strength. It did not, for a phase.

    `_require_strong_key` and `is_signature_valid` were called only in
    `LocalCA`, the DEVELOPMENT backend. `OpenBaoPkiCA` -- what
    `deploy/.env.example` tells every operator to run -- posted the CSR
    straight through to `pki/sign/agent`.

    Nothing else caught it. `deploy/openbao/init.sh` creates the `agent` role
    with `key_type=any`, which it must (the role default is `rsa`, the Go agent
    enrolls Ed25519, and a concrete `key_type` means no device can ever
    enroll), and Vault does no key validation of its own under that setting. So
    both ends were open, and `init.sh`'s comment justified the widening by
    citing a function the production path never called.

    The reachable consequence: an enrollment token is deliberately broadly
    distributable and `max_uses` is nullable, so a holder could enroll with a
    1024-bit RSA key and receive a year-long certificate that is that
    endpoint's entire mTLS identity.

    **WHY test_openbao_pki_ca_parses_response DID NOT CATCH THIS, and why this
    test is shaped the way it is.** That test's mock answers `pki/sign/agent`
    with a canned certificate *regardless of the CSR it was sent* -- so its
    assertions take the same value whether the guard exists or not. A fixture
    that ignores the input cannot test a check on the input.

    So the assertion here is not `pytest.raises` alone -- a bare "it raised"
    would also pass if the code raised for some unrelated reason after asking
    the PKI to sign. It is the tuple (refused, PKI never contacted). The second
    half is the one that matters: a CSR rejected before the POST never reaches
    the PKI and never consumes a serial.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import Encoding

    weak_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    weak_csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "placeholder")]))
        .sign(weak_key, hashes.SHA256())
    ).public_bytes(Encoding.PEM).decode()

    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        raise AssertionError(f"the PKI was asked to sign: {request.url.path}")

    ca = OpenBaoPkiCA(
        "https://bao.test",
        "token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ValueError) as excinfo:
        await ca.sign_csr(weak_csr, device_id=uuid4(), org_id=uuid4())

    assert (str(excinfo.value), asked) == ("weak RSA key (1024 bits); require >= 2048", [])

    # And the strong key the agent actually uses still gets through to the PKI:
    # the guard must not be a blanket refusal that happens to look correct.
    _, good_csr = _make_csr()  # Ed25519, which `key_type=any` exists to permit
    with pytest.raises(AssertionError, match="the PKI was asked to sign"):
        await ca.sign_csr(good_csr, device_id=uuid4(), org_id=uuid4())
    assert asked == ["/v1/pki/sign/agent"]


async def test_local_ca_rejects_weak_keys(tmp_path: Path) -> None:
    """Agent mTLS identity is only as strong as the CSR key — reject weak keys."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    ca = LocalCA(tmp_path)
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.serialization import Encoding

    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "weak")]))
        .sign(weak, hashes.SHA256())
    )

    with pytest.raises(ValueError, match="key"):
        await ca.sign_csr(
            csr.public_bytes(Encoding.PEM).decode(), device_id=uuid4(), org_id=uuid4()
        )


async def test_local_ca_accepts_rsa_2048_and_ed25519(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import Encoding

    ca = LocalCA(tmp_path)
    strong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ok")]))
        .sign(strong, hashes.SHA256())
    )
    issued = await ca.sign_csr(
        csr.public_bytes(Encoding.PEM).decode(), device_id=uuid4(), org_id=uuid4()
    )
    assert issued.cert_pem  # RSA-2048 accepted
    # Ed25519 (the agent default) is accepted
    _, ed_csr = _make_csr()
    assert (await ca.sign_csr(ed_csr, device_id=uuid4(), org_id=uuid4())).cert_pem


def test_production_fails_closed_on_missing_ca_key(tmp_path: Path) -> None:
    # Deferred finding C: a silently regenerated root would orphan every issued
    # agent certificate and break the CRL — production must refuse to start.
    with pytest.raises(MissingCAKeyError):
        LocalCA(tmp_path / "empty-ca", allow_generate=False)


def test_production_reuses_existing_ca_key(tmp_path: Path) -> None:
    ca_dir = tmp_path / "ca"
    LocalCA(ca_dir)  # dev provisions the root
    reopened = LocalCA(ca_dir, allow_generate=False)  # prod reuses it
    assert reopened is not None
