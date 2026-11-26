"""The backend says «Novieri RMM» where a person reads the product's name
(design 2026-11-23-vps-y-marca-novieri §1.1; operator decisions A3 and B4).

Each assertion is a value a person meets: the name the authenticator app and
the passkey prompt show (`notices.PRODUCT`), the prefix of every notification
subject in both languages, the API's own title, the name an AI app sees for
the MCP server, the CN of the internal CA every agent certificate chains to,
and the CN of the certificate the Azure portal lists for the organization's
"Sign in with Microsoft" app. `tests/test_no_old_brand_names.py` (Task 11)
refuses the OLD names everywhere; this pins the NEW ones where they live.
"""

import ast
import datetime as dt
import re
import uuid
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.x509.oid import NameOID

from app.core.auth import entra, notices
from app.core.ca import LocalCA
from app.core.i18n import tr, use_locale

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
DELIVERY = BACKEND / "app/modules/notification/delivery.py"


def _subject_prefixes() -> list[str]:
    """`[...]` at the start of every string constant in delivery.py."""
    found = []
    for node in ast.walk(ast.parse(DELIVERY.read_text())):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            match = re.match(r"\[([^\]]+)\] ", node.value)
            if match:
                found.append(match.group(1))
    return found


def test_the_product_is_novieri_rmm_in_the_authenticator_and_the_passkey_prompt() -> None:
    assert notices.PRODUCT == "Novieri RMM"


def test_every_notification_subject_starts_with_the_product() -> None:
    prefixes = _subject_prefixes()
    assert len(prefixes) >= 25, "the subjects were not found: the scan is broken"
    assert set(prefixes) == {"Novieri RMM"}, sorted(set(prefixes))


def test_a_subject_keeps_the_prefix_in_spanish() -> None:
    with use_locale("es"):
        subject = tr("[Novieri RMM] Ticket {line} resolved", line="#T-1")
    assert subject == "[Novieri RMM] Ticket #T-1 resuelto"


def test_the_api_is_titled_with_the_product(api_env: Any) -> None:
    assert api_env.app.title == "Novieri RMM API"


def test_the_mcp_server_introduces_itself_as_novieri_rmm() -> None:
    source = (BACKEND / "app/mcp/server.py").read_text()
    assert re.search(r'Server\(\n\s+"novieri-rmm",', source), "the MCP server is not novieri-rmm"


def test_the_internal_ca_is_named_for_the_product(tmp_path: Path) -> None:
    """Both of its sources: the local CA of a development backend, and the
    OpenBao PKI root a deployment's first boot generates (`openbao/init.sh`).
    Set before any agent enrols: agents pin the CA at enrollment."""
    LocalCA(tmp_path)
    root = x509.load_pem_x509_certificate((tmp_path / "root.crt").read_bytes())
    (cn,) = root.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    assert cn.value == "Novieri RMM internal CA"
    init = (REPO / "deploy/openbao/init.sh").read_text()
    assert 'common_name="Novieri RMM internal CA"' in init


def test_the_sign_in_with_microsoft_certificate_is_named_for_the_product() -> None:
    """The CN the Azure portal lists under the app's certificates (decided
    2026-11-24). Nothing reads it back: Entra matches the thumbprint."""
    app = uuid.UUID(int=7)
    made = entra.new_certificate(app_client_id=app, now=dt.datetime.now(dt.UTC))
    certificate = x509.load_pem_x509_certificate(made.certificate_pem.encode("ascii"))
    (cn,) = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    assert cn.value == f"novieri-sign-in-{app}"
