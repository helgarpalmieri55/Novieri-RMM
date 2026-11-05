"""py_webauthn's half of falsifier 4's browser check (`frontend/scripts/webauthn-uv.mjs`).

Not a test module (no `test_` prefix); the `webauthn-uv` CI job runs it. Two
commands, JSON on stdout:

    python -m tests.webauthn_uv_probe options <rp_id> <required|discouraged>
    python -m tests.webauthn_uv_probe verify <origin> <rp_id>     # stdin: {"options", "credential"}

`verify` passes `require_user_verification=True`, which is what
`app.core.auth.passkeys` passes on every ceremony (D22) --
`tests/test_auth_falsifiers.py::test_falsifier_4_every_passkey_check_requires_user_verification`
holds that half.
"""

import json
import sys
from typing import Any

from webauthn import (
    base64url_to_bytes,
    generate_registration_options,
    options_to_json,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)


def _options(rp_id: str, user_verification: str) -> str:
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name="ITMS",
        user_name="uv@itms.test",
        user_display_name="UV probe",
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement(user_verification),
        ),
    )
    return options_to_json(options)


def _verify(origin: str, rp_id: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        verify_registration_response(
            credential=body["credential"],
            expected_challenge=base64url_to_bytes(body["options"]["challenge"]),
            expected_origin=origin,
            expected_rp_id=rp_id,
            require_user_verification=True,
        )
    except Exception as exc:  # noqa: BLE001 - the refusal IS the output
        return {"accepted": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"accepted": True}


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "options":
        print(_options(argv[1], argv[2]))
        return 0
    if len(argv) == 3 and argv[0] == "verify":
        print(json.dumps(_verify(argv[1], argv[2], json.load(sys.stdin))))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
