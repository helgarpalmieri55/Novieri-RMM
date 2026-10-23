"""Keycloak's credential events, forwarded into the ITMS audit chain.

`docs/modules/console-auth/spec.md` §4 and §7.1.

WHY THIS EXISTS. Passkey registration, credential removal and identity-provider
linking are credential changes on privileged accounts -- the ways an account
gains a new way in. Invariant 4 wants every privileged action in the immutable,
hash-chained audit log; without this module they live only in Keycloak's own
event log, which is neither tamper-evident nor in the same chain as job
dispatch and credential access.

WHAT IT DOES NOT CLAIM. Keycloak remains the SOURCE. A Keycloak compromised
before a sweep runs can drop an event this will never see, and no amount of
forwarding makes an absence detectable. What forwarding buys is that an event
which DID reach Keycloak, once copied here, cannot afterwards be altered
without breaking the chain.

NO ROUTES AND NO JOB TYPES. This module is a worker sweep and a mapping; it
registers nothing into the kernel's HTTP or job surfaces.
"""
