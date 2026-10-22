"""Module error types.

Routing at the API layer is by TYPE, never by matching exception prose -- the
convention every module since Phase 11 follows.

`NonceLeakError` is the odd one out and deliberately so: it is not a user error
and never becomes a 4xx. It means a string retrieved from the database contained
this turn's freshly minted per-turn nonce (spec §8.2), which cannot happen by
chance and means the nonce leaked into content the model or a writer could see.
The turn is aborted rather than degraded.
"""


class AiError(Exception):
    """Base for everything this module raises."""


class AiValidationError(AiError):
    """A caller-supplied value is not acceptable -- 400 at the API layer."""


class AiNotFoundError(AiError):
    """Absent, or belonging to another org. Indistinguishable on purpose: a 403
    would confirm the id exists somewhere, which is a cross-tenant existence
    oracle. 404 at the API layer."""


class AiDisabledError(AiError):
    """`ai_settings.enabled` is false, or no provider is configured (§0.10,
    §10.6). A disabled feature, not an error -- 503 with a message saying
    which."""


class AiOrgMismatchError(AiError):
    """This turn's token names a different org than `ai_session.org_id` (§0.9).
    409: a conversation is pinned to one org at creation and there is no
    migration and no merge."""


class NonceLeakError(AiError):
    """Retrieved text contained the turn's own nonce (§8.2). Abort the turn."""
