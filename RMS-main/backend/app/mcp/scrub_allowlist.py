"""The MCP scrub, and the reviewed response-field allowlist (MCP server design §5.3).

THE RULE. Every tool result is walked at every depth, and every dict KEY is put
to `app.core.secrets.looks_like_secret_key` -- the same predicate the in-app
AI's scrub and the log sink use. A hit whose `(route, path)` is in
`REVIEWED_FIELDS` is allowed; any other hit BLOCKS THE WHOLE RESULT. Nothing is
ever redacted: a partly blanked result leaves the AI reasoning over data whose
shape it cannot see (`app/core/ai/scrub.py` states the argument in full).

PATHS. A path is the dotted key path with every list index written `[]`:
`items[].nested.api_token`, a top-level list as `[].name`. That is the
notation `tests/test_mcp_scrub.py::_field_paths` produces from a response
model, so a runtime hit is looked up here directly. An entry may contain `*`
where its response model has an open dict (`dict[str, X]`): `_field_paths`
writes such a key as `.*`, and a `*` here matches exactly one key that contains
no `.`, `[` or `]` -- anything else fails to match, and blocks.

WHY AN ALLOWLIST OF FIELDS, NOT OF NAMES. A key name is reviewed per route:
`has_key` on a license is a boolean, and the same spelling on a route nobody
has read is not. `tests/test_mcp_scrub.py` walks the declared response model of
every exposed tool and fails on any secret-shaped field without an entry here,
and on any entry that no longer names a real field. A field that turns out to
CARRY a secret is not listed: its route is excluded in `app/mcp/exclusions.py`
instead (the notification channel routes are the first, for `config.url`).

WHAT ONLY THE RUNTIME SCRUB CAN POLICE. Some response fields are free-form
JSON, whose keys no model declares, so the static test cannot see into them and
nothing here allows anything inside them. Any secret-shaped key the runtime walk
finds in one blocks the result:
  * `jobs` `result` -- whatever the agent's capability handler reported;
  * docs records' `fields` (`extra="allow"`) -- whatever a person typed;
  * `audit` `metadata` -- per-action detail;
  * `security-events` `raw` -- the source system's event as received;
  * `bcp` `evidence` -- attached evidence detail;
  * `approved-actions` `params_template` -- secret VALUES are withheld by
    `redact_secret_values`, but the secret-shaped KEY stays, so such a template
    blocks its result: the safe direction, and visible to the operator.
The false-negative surface is a secret VALUE under a key that is not
secret-shaped (the webhook URL above); the answer to that is excluding the
route, never a longer word list.
"""

import re
from collections.abc import Iterator
from functools import cache
from typing import Any

from app.core.secrets import looks_like_secret_key

# --------------------------------------------------------------------------- #
# Why each kind of reviewed field carries no secret. One reason per meaning,
# not per spelling: `key` is a slug on a queue and a provider id on the AI page.
# --------------------------------------------------------------------------- #

_CREDENTIAL_ID = (
    "the id of a stored credential, a reference into the vault; its secret is returned only "
    "by the credential checkout, which is never exposed"
)
_CREDENTIAL_NAME = "a stored credential's label, which a person chose to name it; never its secret"
_CREDENTIAL_COUNT = "how many stored credentials are scoped to the group, an integer"
_HAS_CREDENTIAL = "a boolean saying whether a vault credential is attached; never the credential"
_HAS_CLIENT_SECRET = (
    "a boolean saying whether the mail OAuth app's client secret is stored; never the secret"
)
_HAS_LICENSE_KEY = (
    "a boolean saying whether a license key is stored; the key itself is returned only by "
    "GET /licenses/{license_id}/key, which is never exposed"
)
_MAILBOX_CREDENTIAL_STATE = (
    "the health of the mailbox's sign-in, a status word such as ok or needs_reconsent; "
    "never the credential"
)
_HOST_KEY_TYPE = (
    "the algorithm name of a target's SSH host key, e.g. ssh-ed25519; public by definition"
)
_RECOVERY_PUBLIC_KEY = (
    "the public half of an off-site backup recovery keypair; the server never holds the "
    "private half, and a public key is not a secret"
)
_SIGNATURE_ALGORITHM = "the constant name of the job-signing algorithm (ed25519), or null"
_SIGNATURE_DIGEST = (
    "SHA-256 over the job's signature (app/jobs/attestation.py), a one-way name for it; the "
    "signature itself, which is what an agent acts on, is never returned"
)
_APPROVED_ACTION_SLUG = (
    "the stable identifier of an approved action in the org's catalogue, a slug; not key material"
)
_REDACTED_KEY_NAMES = (
    "the names of params_template keys whose values redact_secret_values withheld; the values "
    "are never returned"
)
_AI_PROVIDER_KEY = (
    "the configured AI provider's identifier; ProviderOut carries key, kind and model only, "
    "never key material or the base URL"
)
_TOKEN_COUNT = "a count of model tokens a conversation used, an integer; not a bearer token"
_TOKEN_BUDGET = "the daily cap on model tokens, an integer or null; not a bearer token"
_CATEGORY_SLUG = "a category's identifier slug, unique in the org; not key material"
_QUEUE_SLUG = "a service-desk queue's identifier slug, unique in the org; not key material"
_BUDGET_SYSTEM_KEY = (
    "the slug of the built-in category a cost feed files projected costs under, or null"
)
_PERIOD_KEY = "the budget period a cost falls in, a date label such as 2026-10"
_STEP_SLUG = "a person-checklist step's identifier slug; not key material"
_PROBE_KEY = "the fixed identifier of a platform health probe, such as redis; not key material"
_REPORT_KEY = "the report definition's stable identifier, the last path segment of its route"
_SPEND_GROUP_LABEL = (
    "the spend row's group label -- a vendor, category or owner name -- or null; not key material"
)
_FULL_PASS = (
    "whether the membership sweep read every active device (a pass over the fleet), a boolean"
)
_LAST_FULL_PASS_AT = "when the membership sweep last read every active device, a timestamp or null"

REVIEWED_FIELDS: dict[tuple[str, str], str] = {
    ("GET /api/v1/agent-push/credential-scopes", "[].credential_id"): _CREDENTIAL_ID,
    ("GET /api/v1/agent-push/credential-scopes", "[].credential_name"): _CREDENTIAL_NAME,
    ("POST /api/v1/agent-push/credential-scopes", "credential_id"): _CREDENTIAL_ID,
    ("POST /api/v1/agent-push/credential-scopes", "credential_name"): _CREDENTIAL_NAME,
    ("GET /api/v1/agent-push/host-keys", "[].observed_key_type"): _HOST_KEY_TYPE,
    ("POST /api/v1/agent-push/host-keys/confirm", "observed_key_type"): _HOST_KEY_TYPE,
    ("GET /api/v1/agent-push/runs", "[].credential_id"): _CREDENTIAL_ID,
    ("GET /api/v1/agent-push/runs", "[].credential_name"): _CREDENTIAL_NAME,
    ("POST /api/v1/agent-push/runs", "credential_id"): _CREDENTIAL_ID,
    ("POST /api/v1/agent-push/runs", "credential_name"): _CREDENTIAL_NAME,
    ("GET /api/v1/agent-push/runs/preview", "credential_id"): _CREDENTIAL_ID,
    ("GET /api/v1/agent-push/runs/preview", "credential_name"): _CREDENTIAL_NAME,
    ("GET /api/v1/agent-push/runs/{run_id}", "credential_id"): _CREDENTIAL_ID,
    ("GET /api/v1/agent-push/runs/{run_id}", "credential_name"): _CREDENTIAL_NAME,
    ("POST /api/v1/agent-push/runs/{run_id}/cancel", "credential_id"): _CREDENTIAL_ID,
    ("POST /api/v1/agent-push/runs/{run_id}/cancel", "credential_name"): _CREDENTIAL_NAME,
    ("POST /api/v1/agent-push/runs/{run_id}/pause", "credential_id"): _CREDENTIAL_ID,
    ("POST /api/v1/agent-push/runs/{run_id}/pause", "credential_name"): _CREDENTIAL_NAME,
    ("POST /api/v1/agent-push/runs/{run_id}/resume", "credential_id"): _CREDENTIAL_ID,
    ("POST /api/v1/agent-push/runs/{run_id}/resume", "credential_name"): _CREDENTIAL_NAME,
    ("GET /api/v1/ai/actions", "[].approved_action_key"): _APPROVED_ACTION_SLUG,
    ("GET /api/v1/ai/actions/{ai_action_id}", "approved_action_key"): _APPROVED_ACTION_SLUG,
    ("POST /api/v1/ai/actions/{ai_action_id}/reject", "approved_action_key"): _APPROVED_ACTION_SLUG,
    ("GET /api/v1/ai/providers", "[].key"): _AI_PROVIDER_KEY,
    ("GET /api/v1/ai/sessions", "[].total_input_tokens"): _TOKEN_COUNT,
    ("GET /api/v1/ai/sessions", "[].total_output_tokens"): _TOKEN_COUNT,
    ("GET /api/v1/ai/sessions/{session_id}", "messages[].input_tokens"): _TOKEN_COUNT,
    ("GET /api/v1/ai/sessions/{session_id}", "messages[].output_tokens"): _TOKEN_COUNT,
    ("GET /api/v1/ai/sessions/{session_id}", "session.total_input_tokens"): _TOKEN_COUNT,
    ("GET /api/v1/ai/sessions/{session_id}", "session.total_output_tokens"): _TOKEN_COUNT,
    ("GET /api/v1/ai/settings", "daily_token_budget"): _TOKEN_BUDGET,
    ("PUT /api/v1/ai/settings", "daily_token_budget"): _TOKEN_BUDGET,
    ("GET /api/v1/approved-actions", "[].key"): _APPROVED_ACTION_SLUG,
    ("GET /api/v1/approved-actions", "[].params_template_redacted_keys"): _REDACTED_KEY_NAMES,
    ("POST /api/v1/approved-actions", "key"): _APPROVED_ACTION_SLUG,
    ("POST /api/v1/approved-actions", "params_template_redacted_keys"): _REDACTED_KEY_NAMES,
    ("GET /api/v1/approved-actions/{action_id}", "key"): _APPROVED_ACTION_SLUG,
    ("GET /api/v1/approved-actions/{action_id}", "params_template_redacted_keys"): (
        _REDACTED_KEY_NAMES
    ),
    ("PATCH /api/v1/approved-actions/{action_id}", "key"): _APPROVED_ACTION_SLUG,
    ("PATCH /api/v1/approved-actions/{action_id}", "params_template_redacted_keys"): (
        _REDACTED_KEY_NAMES
    ),
    ("DELETE /api/v1/approved-actions/{action_id}/exempt-script", "key"): _APPROVED_ACTION_SLUG,
    (
        "DELETE /api/v1/approved-actions/{action_id}/exempt-script",
        "params_template_redacted_keys",
    ): _REDACTED_KEY_NAMES,
    ("POST /api/v1/approved-actions/{action_id}/exempt-script", "key"): _APPROVED_ACTION_SLUG,
    ("POST /api/v1/approved-actions/{action_id}/exempt-script", "params_template_redacted_keys"): (
        _REDACTED_KEY_NAMES
    ),
    ("GET /api/v1/backup/recovery-keys", "[].public_key"): _RECOVERY_PUBLIC_KEY,
    ("GET /api/v1/budget/categories", "[].key"): _CATEGORY_SLUG,
    ("GET /api/v1/budget/categories", "[].system_key"): _BUDGET_SYSTEM_KEY,
    ("POST /api/v1/budget/categories", "key"): _CATEGORY_SLUG,
    ("POST /api/v1/budget/categories", "system_key"): _BUDGET_SYSTEM_KEY,
    ("PATCH /api/v1/budget/categories/{category_id}", "key"): _CATEGORY_SLUG,
    ("PATCH /api/v1/budget/categories/{category_id}", "system_key"): _BUDGET_SYSTEM_KEY,
    ("GET /api/v1/budget/expenses", "[].period_key"): _PERIOD_KEY,
    ("POST /api/v1/budget/expenses", "period_key"): _PERIOD_KEY,
    ("GET /api/v1/budget/expenses/{expense_id}", "period_key"): _PERIOD_KEY,
    ("PATCH /api/v1/budget/expenses/{expense_id}", "period_key"): _PERIOD_KEY,
    ("POST /api/v1/budget/expenses/{expense_id}/confirm", "period_key"): _PERIOD_KEY,
    ("POST /api/v1/budget/expenses/{expense_id}/void", "period_key"): _PERIOD_KEY,
    ("GET /api/v1/budget/forecast", "rows[].period_key"): _PERIOD_KEY,
    ("GET /api/v1/device-groups", "evaluation.full_pass"): _FULL_PASS,
    ("GET /api/v1/device-groups", "evaluation.last_full_pass_at"): _LAST_FULL_PASS_AT,
    ("GET /api/v1/device-groups/{group_id}", "references.credentials"): _CREDENTIAL_COUNT,
    ("GET /api/v1/email-intake/mailboxes", "[].credential_state"): _MAILBOX_CREDENTIAL_STATE,
    ("GET /api/v1/email-intake/mailboxes", "[].has_credential"): _HAS_CREDENTIAL,
    ("POST /api/v1/email-intake/mailboxes", "credential_state"): _MAILBOX_CREDENTIAL_STATE,
    ("POST /api/v1/email-intake/mailboxes", "has_credential"): _HAS_CREDENTIAL,
    ("GET /api/v1/email-intake/mailboxes/{mailbox_id}", "credential_state"): (
        _MAILBOX_CREDENTIAL_STATE
    ),
    ("GET /api/v1/email-intake/mailboxes/{mailbox_id}", "has_credential"): _HAS_CREDENTIAL,
    ("PATCH /api/v1/email-intake/mailboxes/{mailbox_id}", "credential_state"): (
        _MAILBOX_CREDENTIAL_STATE
    ),
    ("PATCH /api/v1/email-intake/mailboxes/{mailbox_id}", "has_credential"): _HAS_CREDENTIAL,
    ("POST /api/v1/email-intake/mailboxes/{mailbox_id}/oauth/disconnect", "credential_state"): (
        _MAILBOX_CREDENTIAL_STATE
    ),
    ("POST /api/v1/email-intake/mailboxes/{mailbox_id}/oauth/disconnect", "has_credential"): (
        _HAS_CREDENTIAL
    ),
    ("GET /api/v1/email-intake/oauth-app", "has_secret"): _HAS_CLIENT_SECRET,
    ("PUT /api/v1/email-intake/oauth-app", "has_secret"): _HAS_CLIENT_SECRET,
    ("GET /api/v1/health/alerts/{alert_id}", "actions[].approved_action_key"): (
        _APPROVED_ACTION_SLUG
    ),
    ("POST /api/v1/health/alerts/{alert_id}/actions/{action_id}/refuse", "approved_action_key"): (
        _APPROVED_ACTION_SLUG
    ),
    ("GET /api/v1/jobs", "items[].signature_algorithm"): _SIGNATURE_ALGORITHM,
    ("GET /api/v1/jobs", "items[].signature_digest"): _SIGNATURE_DIGEST,
    ("POST /api/v1/jobs", "signature_algorithm"): _SIGNATURE_ALGORITHM,
    ("POST /api/v1/jobs", "signature_digest"): _SIGNATURE_DIGEST,
    ("GET /api/v1/jobs/{job_id}", "signature_algorithm"): _SIGNATURE_ALGORITHM,
    ("GET /api/v1/jobs/{job_id}", "signature_digest"): _SIGNATURE_DIGEST,
    ("POST /api/v1/jobs/{job_id}/cancel", "signature_algorithm"): _SIGNATURE_ALGORITHM,
    ("POST /api/v1/jobs/{job_id}/cancel", "signature_digest"): _SIGNATURE_DIGEST,
    ("POST /api/v1/jobs/{job_id}/retry", "signature_algorithm"): _SIGNATURE_ALGORITHM,
    ("POST /api/v1/jobs/{job_id}/retry", "signature_digest"): _SIGNATURE_DIGEST,
    ("GET /api/v1/licenses", "[].has_key"): _HAS_LICENSE_KEY,
    ("POST /api/v1/licenses", "has_key"): _HAS_LICENSE_KEY,
    ("GET /api/v1/licenses/{license_id}", "has_key"): _HAS_LICENSE_KEY,
    ("PATCH /api/v1/licenses/{license_id}", "has_key"): _HAS_LICENSE_KEY,
    ("GET /api/v1/people/{person_id}", "checklist.steps[].key"): _STEP_SLUG,
    ("POST /api/v1/people/{person_id}/checklists", "steps[].key"): _STEP_SLUG,
    ("POST /api/v1/people/{person_id}/checklists/{checklist_id}/cancel", "steps[].key"): _STEP_SLUG,
    ("POST /api/v1/people/{person_id}/checklists/{checklist_id}/complete", "steps[].key"): (
        _STEP_SLUG
    ),
    ("PATCH /api/v1/people/{person_id}/checklists/{checklist_id}/steps/{key}", "steps[].key"): (
        _STEP_SLUG
    ),
    ("GET /api/v1/platform/status", "probes[].key"): _PROBE_KEY,
    ("GET /api/v1/portal/categories", "[].key"): _CATEGORY_SLUG,
    ("GET /api/v1/provisioning/profiles", "[].has_domain_join_credential"): _HAS_CREDENTIAL,
    ("GET /api/v1/provisioning/profiles", "[].has_local_admin_credential"): _HAS_CREDENTIAL,
    ("POST /api/v1/provisioning/profiles", "has_domain_join_credential"): _HAS_CREDENTIAL,
    ("POST /api/v1/provisioning/profiles", "has_local_admin_credential"): _HAS_CREDENTIAL,
    ("GET /api/v1/provisioning/profiles/{profile_id}", "has_domain_join_credential"): (
        _HAS_CREDENTIAL
    ),
    ("GET /api/v1/provisioning/profiles/{profile_id}", "has_local_admin_credential"): (
        _HAS_CREDENTIAL
    ),
    ("PUT /api/v1/provisioning/profiles/{profile_id}", "has_domain_join_credential"): (
        _HAS_CREDENTIAL
    ),
    ("PUT /api/v1/provisioning/profiles/{profile_id}", "has_local_admin_credential"): (
        _HAS_CREDENTIAL
    ),
    ("POST /api/v1/remote/sessions/{session_id}/stop", "credential_id"): _CREDENTIAL_ID,
    ("GET /api/v1/reports", "reports[].key"): _REPORT_KEY,
    ("GET /api/v1/reports/backup-coverage", "meta.report_key"): _REPORT_KEY,
    ("GET /api/v1/reports/fleet-posture", "meta.report_key"): _REPORT_KEY,
    ("GET /api/v1/reports/patch-compliance", "meta.report_key"): _REPORT_KEY,
    ("GET /api/v1/reports/sla-attainment", "meta.report_key"): _REPORT_KEY,
    ("GET /api/v1/service-desk/categories", "[].key"): _CATEGORY_SLUG,
    ("POST /api/v1/service-desk/categories", "key"): _CATEGORY_SLUG,
    ("PATCH /api/v1/service-desk/categories/{category_id}", "key"): _CATEGORY_SLUG,
    ("GET /api/v1/service-desk/queues", "[].key"): _QUEUE_SLUG,
    ("POST /api/v1/service-desk/queues", "key"): _QUEUE_SLUG,
    ("PATCH /api/v1/service-desk/queues/{queue_id}", "key"): _QUEUE_SLUG,
    ("GET /api/v1/subscriptions/spend", ".*[].group_key"): _SPEND_GROUP_LABEL,
}


def secret_key_paths(payload: Any) -> Iterator[str]:
    """Every secret-shaped key path in `payload`, depth-first in document order.

    A hit's value is walked too: a reviewed key never hides a secret beneath it.
    Only keys are judged; a string VALUE is never a key, whatever it says.
    """
    stack: list[tuple[Any, str]] = [(payload, "")]
    while stack:
        node, prefix = stack.pop()
        children: list[tuple[Any, str]] = []
        if isinstance(node, dict):
            for key, value in node.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                if looks_like_secret_key(str(key)):
                    yield path
                children.append((value, path))
        elif isinstance(node, list | tuple):
            children = [(item, prefix + "[]") for item in node]
        elif not isinstance(node, str | int | float | bool | type(None)):
            # Only decoded JSON is judged. A model, bytes or an undecoded string
            # of JSON would otherwise pass unseen; refusing is the safe failure.
            raise TypeError(f"not decoded JSON: {type(node).__name__}")  # i18n-exempt: internal
        stack.extend(reversed(children))


def secret_key_path(payload: Any) -> str | None:
    """The first secret-shaped key path in `payload`, or None."""
    return next(secret_key_paths(payload), None)


@cache
def _wildcards(route: str) -> tuple[re.Pattern[str], ...]:
    """This route's reviewed paths that contain an open-dict `*`, as patterns."""
    one_key = r"[^.\[\]]+"
    patterns = []
    for entry_route, path in REVIEWED_FIELDS:
        if entry_route != route or "*" not in path:
            continue
        rx = re.escape(path)
        rx = re.sub(r"^\\\.\\\*", one_key, rx)
        rx = rx.replace(r"\.\*", r"\." + one_key)
        patterns.append(re.compile(rx))
    return tuple(patterns)


def is_reviewed(route: str, path: str) -> bool:
    """Whether the hit `path` on `route` (`"METHOD /api/v1/..."`) is reviewed."""
    if (route, path) in REVIEWED_FIELDS:
        return True
    return any(p.fullmatch(path) for p in _wildcards(route))


def first_unreviewed_secret_path(route: str, payload: Any) -> str | None:
    """The runtime rule: the first hit in `payload` not reviewed for `route`, or
    None. Non-None means the whole result is blocked."""
    for path in secret_key_paths(payload):
        if not is_reviewed(route, path):
            return path
    return None
