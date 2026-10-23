"""User lifecycle against Keycloak.

`docs/superpowers/specs/2026-08-19-rbac-and-user-lifecycle-design.md` §2.

Three files, split the way the events module is split and for the same reason:
`kc_client.py` is the HTTP conversation with Keycloak's admin API and nothing
else, `service.py` holds every decision worth testing without a client, and
`api.py` is a thin translation of one to the other.
"""
