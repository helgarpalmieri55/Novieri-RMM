# Microsoft OAuth for mail: what was established before building anything

Research notes, 2026-09-10. **Not a spec.** The operator asked to enable
Microsoft OAuth for the email account and sequenced it after the settings
rework; this records what was verified so the next cycle does not re-derive it.

## There are two mail paths, and they are unrelated

| | outgoing | incoming |
| --- | --- | --- |
| what | invitations, password resets | the support mailbox that becomes tickets |
| who sends | **Keycloak**, from its realm `smtpServer` block | **ITMS**, `email_intake/imap.py` |
| configured at | Settings → Email, written through the Keycloak admin API (`user_admin/realm_config.py`) | Email Intake → Mailboxes |
| credential today | goes straight to Keycloak; ITMS never stores it | vault, via `mailboxes.credential_id` |

A change to one buys nothing for the other.

## Outgoing: Keycloak can do this, but not at the pinned version

**Keycloak 26.2 added token-based SMTP auth (XOAUTH2)**, fetching the token
with a **client-credentials grant**. Microsoft Azure / Office 365 is explicitly
supported. Google is explicitly NOT — it requires a JWT assertion rather than a
client secret, which 26.2 does not do.

Sources:
- https://www.keycloak.org/2025/05/send-mails-xoauth-26-2
- https://www.keycloak.org/docs/latest/server_admin/index.html

The realm fields, as documented: **Authentication Type** (`password` | `token`),
**Auth Token URL**, **Auth Token Scope**, **Auth Token ClientId**, **Auth Token
Client Secret**.

**`deploy/docker-compose.yml` pins `quay.io/keycloak/keycloak:26.0`.** So the
feature needs an upgrade to 26.2+, which the operator agreed to take as part of
that work rather than separately. Without the upgrade the only route is a
custom Java `EmailSenderProvider` SPI baked into the Keycloak image — rejected:
it puts this platform in the business of shipping Java into a vendor container
to obtain a feature the vendor now ships.

ITMS's side is small: `SMTP_FIELDS` in `realm_config.py` is an allowlist of ten
keys and needs the five above; the Email tab needs an auth-type switch. The
client secret follows the password's existing rule — passes through to
Keycloak, never stored, never returned (invariant 3).

**Upgrade risk to check before it lands:** realm import compatibility, the ITMS
login theme (a bind mount, `deploy/keycloak/themes/itms/`), and the admin REST
contract `realm_config.py` depends on.

## Incoming: delegated sign-in, by the operator's ruling

`email_intake/imap.py` authenticates with `LOGIN <username> "<password>"`.
Microsoft has disabled basic auth for IMAP in Exchange Online, so this path
cannot work against a Microsoft 365 mailbox today.

Two grants were put to the operator. **Delegated ("Connect with Microsoft") was
chosen** over app-only.

The rejected alternative and why it matters: app-only needs
`IMAP.AccessAsApp`, which reads **every mailbox in the tenant** unless an admin
scopes it with `New-ApplicationAccessPolicy`. Delegated access is naturally
bounded to the mailbox the person signed in as, which is why it wins here even
though it costs a stored refresh token.

What that implies, to be designed properly in its own cycle:

- Authorization-code + PKCE, scopes
  `https://outlook.office.com/IMAP.AccessAsUser.All offline_access`.
- A browser-reachable redirect URI. Each on-prem install has its own hostname,
  so **each install registers its own Azure app** — this platform cannot ship a
  client secret, and the same app can serve the outgoing half.
- The refresh token becomes a **new credential type in the existing vault**,
  reached through `mailboxes.credential_id`, so invariant 3's
  encrypt-at-rest/decrypt-at-point-of-use machinery is reused rather than
  reinvented. A migration adds the type.
- An access token is fetched per poll and never stored.
- **Revocation is a first-class state.** A refresh token dies — revoked,
  expired, password changed — and the mailbox stops polling. The console must
  say so and offer Reconnect, rather than reporting a login failure.
