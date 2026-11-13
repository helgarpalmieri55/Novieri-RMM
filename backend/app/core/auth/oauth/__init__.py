"""The platform's own OAuth 2.1 authorization server for MCP (autenticación propia
design §5; plan 2026-11-06).

`policy` (numbers and addresses), `errors` (RFC 6749 error bodies),
`redirects` and `pkce` (pure checks), `access` (the access token), `cimd`
(client metadata documents), `store` (the one session over these rows),
`clients`, `grants`, `authorize` and `consent`.
"""
