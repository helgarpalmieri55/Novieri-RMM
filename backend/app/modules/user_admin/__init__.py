"""User lifecycle: invite, enable and disable, delete, roles, and an
administrator's factor and session acts.

`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §2, and
since own authentication phase 1 `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md`
§7: the platform holds the accounts itself. `service.py` holds every decision
worth testing, and `api.py` is a thin translation of it to HTTP. The two
Keycloak clients stay only until phase 4 and are not part of the lifecycle:
neither has a caller since phase 3 (revoking an AI app no longer asks
Keycloak). `realm_config.py` imports `kc_client.py`'s types, and nothing
imports `realm_config.py`.
"""
