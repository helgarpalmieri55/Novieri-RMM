"""Which `/api/v1` routes the MCP server exposes, and why the rest are not
(MCP server design §4, §5).

Every table here is keyed by `(METHOD, full path template)` exactly as
`app.core.router_inspect.flatten` reports it, and every exclusion carries a
reason that is true of the route's handler. `tests/test_mcp_catalog.py`
partitions every `/api/v1` route into "tool" or "excluded, with a reason", and
fails on a stale key, so becoming AI-reachable is always a reviewed decision.

NEVER_EXPOSED -- "refused twice" (spec §5.1; invariant 1c(c)): no tool is ever
generated for these, AND the API refuses them on the MCP channel even if one
were (`app/mcp/guard.py`, and `auth_context`'s route-identity check in
`app/api/deps.py`). Each one hands a plaintext secret to its caller, or mints a
credential that would. Invariant 3 is not amended. `app/main.py` refuses to
boot if a pair stops naming a real route, because a renamed route would
otherwise silently fall out of the guard.
"""

NEVER_EXPOSED_REASONS: dict[tuple[str, str], str] = {
    # Stored credentials and license keys: the decrypted secret is the response.
    ("POST", "/api/v1/credentials/{credential_id}/checkout"): (
        "returns a stored credential's plaintext secret (invariant 3)"
    ),
    ("GET", "/api/v1/licenses/{license_id}/key"): (
        "returns a license's plaintext key (invariant 3)"
    ),
    # Enrollment material: an enrollment token or an install-link code lets a
    # machine join the fleet; the AI must not mint or rotate either.
    ("POST", "/api/v1/enrollment-tokens"): (
        "mints an enrollment token and returns its plaintext, which lets a machine join the fleet"
    ),
    ("PUT", "/api/v1/install-link"): (
        "can generate the install link and returns its code, which lets a machine join the fleet"
    ),
    ("POST", "/api/v1/install-link/regenerate"): (
        "rotates the install link and returns its new code, which lets a machine join the fleet"
    ),
    ("POST", "/api/v1/agent/releases/{release_id}/install-command"): (
        "mints an enrollment token and returns it inside an install command"
    ),
    ("POST", "/api/v1/agent/releases/{release_id}/deployment-token"): (
        "mints a deployment enrollment token and returns its plaintext"
    ),
    # Backup recovery keys: the challenge/prove exchange is the operator's
    # proof of holding the off-site recovery private key.
    ("POST", "/api/v1/backup/recovery-keys/{key_id}/challenge"): (
        "issues a recovery-key possession challenge; the exchange is the operator's alone"
    ),
    ("POST", "/api/v1/backup/recovery-keys/{key_id}/prove"): (
        "accepts the decrypted recovery-key challenge; the exchange is the operator's alone"
    ),
    # First-run claim: a one-time install code that makes the first super-admin.
    ("POST", "/api/v1/setup/claim"): (
        "consumes the one-time install code and creates the first super-admin; unauthenticated"
    ),
}

NEVER_EXPOSED: frozenset[tuple[str, str]] = frozenset(NEVER_EXPOSED_REASONS)


# Files and streams are never tools (spec §5.2): the AI gets an item's details,
# not its bytes. Found by surveying every route's handler (and the helpers it
# calls) for a non-JSON response or a raw-body read -- the codebase takes
# uploads as a raw body through `app.core.http.read_bounded_body`, never as
# `UploadFile`/`File()`/`Form()`, of which there are none.
FILE_OR_STREAM: dict[tuple[str, str], str] = {
    # Downloads: a non-JSON response body.
    ("POST", "/api/v1/install-link/msi"): "returns an MSI installer file",
    ("GET", "/api/v1/export/devices.csv"): "returns a CSV file",
    ("GET", "/api/v1/export/devices.xlsx"): "returns an XLSX file",
    ("GET", "/api/v1/export/software.csv"): "returns a CSV file",
    ("GET", "/api/v1/export/software.xlsx"): "returns an XLSX file",
    ("GET", "/api/v1/ipam/export/subnets.csv"): "returns a CSV file",
    ("GET", "/api/v1/ipam/export/addresses.csv"): "returns a CSV file",
    ("GET", "/api/v1/ipam/export/drift.csv"): "returns a CSV file",
    ("GET", "/api/v1/reports/access.csv"): "returns a CSV file",
    ("GET", "/api/v1/reports/fleet-posture/export.csv"): "returns a CSV file",
    ("GET", "/api/v1/reports/patch-compliance/export.csv"): "returns a CSV file",
    ("GET", "/api/v1/reports/backup-coverage/export.csv"): "returns a CSV file",
    ("GET", "/api/v1/reports/sla-attainment/export.csv"): "returns a CSV file",
    ("GET", "/api/v1/patch/report/export"): "streams a CSV file",
    ("GET", "/api/v1/backup/report/export"): "streams a CSV file",
    ("GET", "/api/v1/budget/expenses/export"): "returns a CSV file",
    ("GET", "/api/v1/budget/expenses/export/odoo"): "returns a CSV file in Odoo's import format",
    ("GET", "/api/v1/service-desk/attachments/{attachment_id}"): "returns an attachment's bytes",
    ("GET", "/api/v1/portal/tickets/{ticket_id}/attachments/{attachment_id}"): (
        "returns an attachment's bytes"
    ),
    ("GET", "/api/v1/budget/expenses/{expense_id}/attachment"): "returns an attachment's bytes",
    ("GET", "/api/v1/remote/sessions/{session_id}/recording"): (
        "returns a session recording's bytes"
    ),
    ("GET", "/api/v1/docs/documents/{document_id}/versions/{version}/content"): (
        "returns a document version's bytes"
    ),
    ("GET", "/api/v1/ai/sessions/{session_id}/turns/{turn_id}/stream"): (
        "a server-sent-event stream of the in-app AI's turn"
    ),
    # Uploads: the request body is the file's raw bytes (no JSON schema).
    ("POST", "/api/v1/agent/releases/{release_id}/artifact"): (
        "uploads an agent binary as the raw request body"
    ),
    ("POST", "/api/v1/deploy/packages/{package_id}/artifact"): (
        "uploads a package artifact as the raw request body"
    ),
    ("POST", "/api/v1/docs/documents/{document_id}/versions"): (
        "uploads a document file as the raw request body"
    ),
    ("POST", "/api/v1/budget/expenses/{expense_id}/attachment"): (
        "uploads an attachment file as the raw request body"
    ),
    ("POST", "/api/v1/service-desk/tickets/{ticket_id}/attachments"): (
        "uploads an attachment file as the raw request body"
    ),
    ("POST", "/api/v1/portal/tickets/{ticket_id}/attachments"): (
        "uploads an attachment file as the raw request body"
    ),
    ("POST", "/api/v1/ipam/import/preview"): "uploads a CSV file as the raw request body",
    ("POST", "/api/v1/ipam/import/apply"): "uploads a CSV file as the raw request body",
}


_CIRCULAR = "an AI calling the platform's own AI is circular"
_CHANNEL_CONFIG = (
    "answers the channel's config verbatim, and a webhook channel's config.url is an "
    "incoming-webhook URL, which is a bearer secret no key-name scrub can recognise"
)

_CONNECTIONS = "connections are managed by the person in the console"

NOT_FOR_MCP: dict[tuple[str, str], str] = {
    # Lists no token value, but maps every live bootstrap credential (names,
    # lifetimes, redemptions left); the route's own docstring rules it out as an
    # AI read, and the in-app AI's surface follows that ruling.
    ("GET", "/api/v1/enrollment-tokens"): (
        "maps every live bootstrap credential that can put a machine into the fleet; "
        "deliberately not an AI read (api/v1/enrollment.py)"
    ),
    # The in-app AI's chat: opening a conversation and taking a turn call the
    # platform's model. Reading or deleting a past conversation does not, and
    # stays a tool.
    ("POST", "/api/v1/ai/sessions"): _CIRCULAR,
    ("POST", "/api/v1/ai/sessions/{session_id}/turns"): _CIRCULAR,
    ("POST", "/api/v1/service-desk/tickets/{ticket_id}/ai/sessions"): _CIRCULAR,
    ("POST", "/api/v1/service-desk/tickets/{ticket_id}/ai/sessions/{session_id}/turns"): (
        _CIRCULAR
    ),
    ("POST", "/api/v1/portal/ai/sessions"): _CIRCULAR,
    ("POST", "/api/v1/portal/ai/sessions/{session_id}/turns"): _CIRCULAR,
    # Confirming an act the in-app AI proposed (docs/modules/ai-automation.md
    # §0.4, §6, §7.2): the person's confirmation of a server-rendered preview
    # is that layer's structural bound -- every AI act needs a human click.
    # An external AI confirming it closes the AI-to-AI loop the bound exists
    # to break. Over MCP the AI acts directly as the person instead, through
    # the module's own route, where the same RBAC and audit apply.
    # Its siblings stay tools, each read before deciding:
    #   POST /ai/actions/{id}/reject -- declining creates no effect (the handler
    #     gates it on `ai.act.propose` for that reason); the safe direction.
    #   PUT /ai/autonomy -- the ceiling cannot exceed `require_approval` (a
    #     database CHECK, §4.1), so raising it only lets the in-app AI propose
    #     more; every proposal still needs this confirm, which MCP cannot reach.
    #   PUT /ai/settings -- cost and retention dials only (§0.14).
    ("POST", "/api/v1/ai/actions/{ai_action_id}/confirm"): (
        "a person's confirmation is the in-app AI's structural bound; an external AI "
        "confirming it is the AI-to-AI loop -- over MCP the AI acts directly as the "
        "person instead"
    ),
    # Unauthenticated first-boot probe (`app/api/v1/setup.py`): it answers a
    # browser that cannot sign in yet, so no signed-in person ever needs it.
    ("GET", "/api/v1/setup/status"): (
        "an unauthenticated first-boot probe for a browser that cannot sign in yet"
    ),
    # Gated by `remote/api.py::require_any_permission` (remote.control OR
    # remote.recordings.read), which `rbac.route_permission` cannot read, so a
    # tool would carry no permission for `tools/list` to filter on.
    ("GET", "/api/v1/remote/sessions"): (
        "its either-of-two-codes gate is invisible to route_permission, so tools/list "
        "could not filter it by the person's role"
    ),
    ("GET", "/api/v1/remote/sessions/{session_id}"): (
        "its either-of-two-codes gate is invisible to route_permission, so tools/list "
        "could not filter it by the person's role"
    ),
    # Connect a mailbox with Microsoft (`email_intake/api.py`, mail OAuth design
    # §4.3-§4.5): a browser sign-in round-trip the console drives. `start`
    # returns the URL a browser must open, `complete` redeems the code
    # Microsoft's redirect hands that browser, `abandon` drops the pending
    # state. None of the three can be finished without the person's browser.
    ("POST", "/api/v1/email-intake/mailboxes/{mailbox_id}/oauth/start"): (
        "starts a browser sign-in with Microsoft that only the person's browser can finish"
    ),
    ("POST", "/api/v1/email-intake/oauth/complete"): (
        "redeems the authorization code Microsoft's redirect gave the person's browser"
    ),
    ("POST", "/api/v1/email-intake/oauth/abandon"): (
        "drops the pending state of a browser sign-in with Microsoft"
    ),
    # Registering a recovery key returns the same possession challenge as
    # `.../{key_id}/challenge`, which is never exposed, and the key is only
    # usable once `.../prove` -- also never exposed -- accepts the answer. A
    # tool could only leave an unproven key behind.
    ("POST", "/api/v1/backup/recovery-keys"): (
        "returns the recovery-key possession challenge, whose exchange is never exposed"
    ),
    # Notification channels (`notification/api.py::_channel_out`): `ChannelOut.
    # config` is a free-form dict echoed verbatim, and a webhook channel's
    # `config.url` is its full destination URL. A Slack, Teams or generic
    # incoming-webhook URL IS the credential -- anyone holding it can post --
    # and its key, `url`, is not secret-shaped, so no key-name scrub can see it
    # (`app/mcp/scrub_allowlist.py`). `_reject_secret_keys` is an exact-match
    # denylist, so `config` can also carry e.g. `smtp_password`. Every route
    # answering a `ChannelOut` is therefore not a tool; deleting and test-sending
    # a channel answer no config and stay tools.
    ("GET", "/api/v1/notifications/channels"): _CHANNEL_CONFIG,
    ("POST", "/api/v1/notifications/channels"): _CHANNEL_CONFIG,
    ("GET", "/api/v1/notifications/channels/{channel_id}"): _CHANNEL_CONFIG,
    ("PATCH", "/api/v1/notifications/channels/{channel_id}"): _CHANNEL_CONFIG,
    # Settings → AI connections (`app/mcp/api.py`): which AI apps act for this
    # organization, and revoking one. An AI app listing or revoking AI apps
    # is the person's own decision made through one of the things it governs.
    ("GET", "/api/v1/mcp/connections"): _CONNECTIONS,
    ("POST", "/api/v1/mcp/connections/{client_id}/revoke"): _CONNECTIONS,
}


# Ungated person routes (`rbac.route_permission` is None; the dependency is
# `bound_auth_context` alone) that any signed-in person may legitimately use.
# Each is in `tests/test_route_gating.py::UNGATED_BY_DESIGN` with the same
# argument; it is restated per entry here.
OPEN_TO_ANY_SIGNED_IN: frozenset[tuple[str, str]] = frozenset(
    {
        # The caller's own identity, roles and permission list, read from their
        # own row (`app/api/v1/me.py`).
        ("GET", "/api/v1/me"),
        # The caller's own profile: display_name, phone, timezone, locale and
        # nothing else (`ProfileUpdate` is extra="forbid"); the target is
        # `ctx.user_id` from the token, never the body.
        ("PATCH", "/api/v1/me"),
        # The caller's own organization: identity, status and allowlisted
        # presentation settings (`OrgOut`), never a secret.
        ("GET", "/api/v1/orgs/current"),
        # The caller's own dashboard arrangement. Target `ctx.user_id`, no
        # `user_id` parameter; a layout grants nothing, since every widget
        # renders from a query gated on its own.
        ("GET", "/api/v1/dashboard/layout"),
        # Writes that same one row, the caller's own.
        ("PUT", "/api/v1/dashboard/layout"),
        # Resets (deletes) that same one row; idempotent on a miss.
        ("DELETE", "/api/v1/dashboard/layout"),
    }
)


# `destructiveHint` beyond every DELETE (spec §4.1's named set). Hints set the
# AI client's defaults; the gate is still the person's per-tool setting.
#
# DOCUMENTARY FOR A SPEC-COMPLIANT CLIENT, AND KEPT ANYWAY. Per the MCP spec a
# missing `destructiveHint` defaults to true whenever `readOnlyHint` is false,
# so every non-GET tool already reads as destructive to such a client. This set
# is kept as the reviewed list of acts that change endpoints or lose data. It
# must never be "fixed" by emitting `destructiveHint: False` on the other
# tools: that would tell clients that everything not listed here is safe.
DESTRUCTIVE: frozenset[tuple[str, str]] = frozenset(
    {
        # Push install and push runs: install the agent on machines that are
        # not yet enrolled, presenting an administrative credential.
        ("POST", "/api/v1/agent-push/requests"),
        ("POST", "/api/v1/agent-push/runs"),
        ("POST", "/api/v1/agent-push/runs/{run_id}/resume"),
        # Agent update rollouts: create, and every act that dispatches a ring.
        ("POST", "/api/v1/agent/update-rollouts"),
        ("POST", "/api/v1/agent/update-rollouts/{rollout_id}/start"),
        ("POST", "/api/v1/agent/update-rollouts/{rollout_id}/resume"),
        ("POST", "/api/v1/agent/update-rollouts/{rollout_id}/advance"),
        # Rejecting a device uninstalls its agent.
        ("POST", "/api/v1/devices/{device_id}/review/reject"),
        # A remote session onto an endpoint.
        ("POST", "/api/v1/remote/sessions"),
        # Patch apply installs updates now.
        ("POST", "/api/v1/patch/apply"),
        # Approving a patch for a ring makes the platform's sweep dispatch signed installs
        # (which may reboot per policy) to that ring's devices inside their windows.
        ("POST", "/api/v1/patch/approvals"),
        ("POST", "/api/v1/patch/approvals/bulk"),
        # Script run executes a script on endpoints.
        ("POST", "/api/v1/deploy/scripts/{script_id}/run"),
        # Backup restore writes restored files onto an endpoint.
        ("POST", "/api/v1/backup/points/{point_id}/restore"),
        # App deploy: creating a deployment, and executing it (the dispatch).
        ("POST", "/api/v1/deploy/deployments"),
        ("POST", "/api/v1/deploy/deployments/{deployment_id}/execute"),
        # Beyond the spec's named set, same character: each makes endpoints act,
        # cuts one off, or loses data.
        ("POST", "/api/v1/jobs"),  # dispatches a signed job of any registered type
        ("POST", "/api/v1/jobs/{job_id}/retry"),  # re-dispatches a job as a new signed job
        ("POST", "/api/v1/agents/{device_id}/revoke"),  # revokes a device's agent identity
        ("POST", "/api/v1/patch/devices/{device_id}/restart"),  # books an endpoint restart
        ("POST", "/api/v1/provisioning/profiles/{profile_id}/run"),  # provisions a machine
        # Destroys a document version's bytes; irreversible (docs.document.purge).
        ("POST", "/api/v1/docs/documents/{document_id}/versions/{version}/purge"),
    }
)


# Tool names that the derived name gets wrong: only where two routes would
# collide or a name would exceed 64 characters. One short reason each.
NAME_OVERRIDES: dict[tuple[str, str], str] = {
    # A list whose page is keyed `groups` -- neither `items` nor its own name
    # (`errors`) -- so it derives `platform_errors_get`, the same name as
    # `GET /platform/errors/{group_id}`.
    ("GET", "/api/v1/platform/errors"): "platform_errors_list",
    # Actions whose route answers 201 (they create something as a side effect)
    # or that sit on a collection; the verb in the path already says what they
    # do, so a `_create` suffix would only mislead.
    ("POST", "/api/v1/jobs/{job_id}/retry"): "jobs_retry",
    ("POST", "/api/v1/ipam/subnets/{subnet_id}/allocate"): "ipam_subnets_allocate",
    ("POST", "/api/v1/health/profiles/{profile_id}/clone"): "health_profiles_clone",
    ("POST", "/api/v1/agent-push/host-keys/confirm"): "agent_push_host_keys_confirm",
    ("POST", "/api/v1/licenses/review-queue/ignore"): "licenses_review_queue_ignore",
    ("POST", "/api/v1/licenses/review-queue/map"): "licenses_review_queue_map",
    # One address, not a list.
    ("GET", "/api/v1/ipam/subnets/{subnet_id}/next-free"): "ipam_subnets_next_free_get",
}
