"""User lifecycle: invite, enable and disable, delete, roles, and an
administrator's factor and session acts.

`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §2, and
since own authentication phase 1 `docs/superpowers/specs/2026-11-04-autenticacion-propia-design.md`
§7: the platform holds the accounts itself. `service.py` holds every decision
worth testing, and `api.py` is a thin translation of it to HTTP. No identity
provider is called: the two Keycloak admin clients that once lived here went
in own authentication phase 4, having had no caller since phase 3.
"""
