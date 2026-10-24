"""Shared helpers for the install-link tests (not a test module)."""

from typing import Any
from uuid import UUID

import httpx
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID

#: The compose network Traefik sits on -- the default ITS_TRUSTED_PROXY_CIDRS.
TRAEFIK = "172.19.240.2"

FACTS: dict[str, Any] = {
    "hostname": "lab-pc-07",
    "os_family": "windows",
    "os_version": "11 24H2",
    "architecture": "amd64",
}


def make_csr_pem() -> str:
    key = Ed25519PrivateKey.generate()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "enrolling-agent")]))
        .sign(key, None)
    )
    return csr.public_bytes(Encoding.PEM).decode()


def peer_client(api_env: Any, peer: str) -> httpx.AsyncClient:
    """A client whose TCP peer address is `peer` (ASGI `scope["client"]`)."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_env.app, client=(peer, 40000)),
        base_url="https://itms.example.test",
    )


def link_path(site_id: UUID, tail: str = "") -> str:
    """A site's install link route (MSP clients spec §1.5: one link per site).
    The organisation's Principal site has the organisation's id, so
    `link_path(org_id)` is the link every test before per-site links used."""
    return f"/api/v1/sites/{site_id}/install-link{tail}"


async def switch_on(api_env: Any, org_id: UUID, networks: list[str], **extra: Any) -> str:
    """Switch the Principal site's link on through the API as an org-admin;
    return the code."""
    admin = await api_env.create_user(org_id, "org-admin")
    headers = await api_env.headers(org_id, admin)
    body = {"enabled": True, "networks": networks, "hourly_limit": 50} | extra
    response = await api_env.client.put(link_path(org_id), json=body, headers=headers)
    assert response.status_code == 200, response.text
    url = response.json()["url"]
    assert url is not None
    return str(url).rsplit("/i/", 1)[1]
