"""The model-provider seam: a protocol, and configuration that comes ONLY from
server environment.

docs/modules/ai-admin/spec.md §0.10, §11.1, §11.2, §0.14.

**ONE PROVIDER PER DEPLOYMENT, AND IT IS THE SERVER'S TO CHOOSE** (§0.14, the
operator's decision of 2026-08-25: "dont allow anyone to choose an AI model, we
will use the same for every tenant and api key will be set in the .env per
tenant"). This platform deploys on-prem, one deployment per tenant, so the
provider and its key ARE the per-tenant configuration -- set once in `.env` by
whoever runs the control plane. `ITS_AI_PROVIDERS` therefore names AT MOST ONE
key: two is a configuration error raised where an operator is looking, never a
dropdown offered to an org-admin. `get_provider()` takes no argument because
there is nothing left to select.

**TWO PROVIDER CLIENTS NOW EXIST, SO THE HONEST CLAIM HAS CHANGED AND THE TESTS
SAY SO.** Until the clients landed, this file could assert the strongest
possible thing -- no implementation existed, so no code path could reach a
network at all. That is no longer true and pretending otherwise would be worse
than useless. **The claim this file now makes, and that
`tests/test_ai_admin_providers.py` checks, is: a client exists but is
UNREACHABLE WITHOUT SERVER CONFIGURATION.** `get_provider()` resolves a client
only when some operator configured one through `ITS_AI_*`; with an empty
environment there is nothing to resolve, no default to fall back to, and no
bundled key -- so an unconfigured deployment still cannot egress, and nobody has
to remember to turn anything off.

**THE SERVER CONFIGURATION IS NOW THE ONLY SWITCH** (§0.14). The per-org
`enabled` flag and the per-org provider selection are gone; a deployment that
sets `ITS_AI_*` has the assistant, and one that does not, does not. Everything
below that says "unconfigured deployments cannot egress" is therefore load
bearing in a way it was not before -- it is no longer the second of two gates,
it is the gate.

  * There is no default provider. `load_provider_configs({})` returns `{}`, and
    with no configured provider the API answers 503 "not configured" rather than
    quietly reaching the internet. A deployment that never sets `ITS_AI_*` never
    egresses, and nobody has to remember to turn anything off.
  * There is no SECOND provider either. `load_provider_configs` refuses an
    `ITS_AI_PROVIDERS` naming more than one key, because "the same model for
    every tenant" has no meaning while two are configured and something has to
    pick -- and the thing that used to pick was a console dropdown.
  * There is no bundled key, no default base URL and no default model name.
    `test_no_source_file_contains_a_default_endpoint_or_bundled_key` greps every
    source file in this package for a URL literal, a model-name-shaped literal
    or an `sk-`-shaped string, so a "convenience default" added in a later
    release is a red build naming the file it was added to. **This is why the
    two client files compose their base URL and model name from
    `ProviderConfig` and name neither.**
  * The whole outbound path is ONE `httpx` client, in `providers/_http.py`, and
    `tests/test_ai_admin_egress.py` pins the module's client-construction sites
    as an EQUALITY over file names. A second egress path is a red build naming
    the file.
  * **The base URL is a server environment variable and is NEVER org-editable**
    (§0.10). There is no `base_url` field on any request model, on `ai_settings`,
    or anywhere an org-admin can reach -- and since §0.14 there is no
    `provider_key` field either, so an org-admin now names nothing at all. This
    is deliberately NOT "guard the URL with `app/core/egress.py`": an arbitrary
    URL is not a feature here, so the correct design REMOVES THE SSRF PRIMITIVE
    rather than guarding it. This module must never import
    `app/core/egress.py`.

WHY A `Mapping[str, str]` LOADER AND NOT `Settings` FIELDS -- a spec gap closed
here and recorded in §11.2. `app/core/config.py::Settings` is
`env_prefix="ITS_"` with `extra="ignore"`. The per-provider variables of §11.2
have DYNAMIC names (`ITS_AI_<KEY>_BASE_URL`), so pydantic-settings has no field
to bind them to and would SILENTLY IGNORE every one of them -- the deployment
would look configured and behave unconfigured. `load_provider_configs` is
therefore a pure function over a `Mapping[str, str]`, exhaustively testable
without touching the process environment, and `configured_providers()` is the
ONE place in this module that reads `os.environ`.

VALIDATION IS AT LOAD TIME, NOT AT FIRST USE. A missing or misspelled variable
raises when the configuration is read, naming the variable. The alternative --
discovering it when an administrator asks a question at 09:00 -- turns a
deployment mistake into an incident.
"""

import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from app.core.ai.errors import AiError
from app.core.ai.types import ToolSpec
from app.core.i18n import tr

__all__ = [
    "KINDS",
    "MALFORMED_ARGUMENTS_FIELD",
    "Block",
    "Message",
    "ModelProvider",
    "ProviderConfig",
    "ProviderConfigError",
    "ProviderError",
    "ProviderNotConfiguredError",
    "ProviderRateLimitedError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "TextBlock",
    "ToolCallBlock",
    "ToolResultBlock",
    "ToolSpec",
    "UsageBlock",
    "configured_provider",
    "configured_providers",
    "get_provider",
    "load_provider_configs",
    "parse_tool_arguments",
    "reset_configured_providers",
]


class ProviderNotConfiguredError(AiError):
    """No provider by that key is configured on this server, or its kind has no
    client in this build. Becomes a 503 at the API layer (§10.6): a disabled
    feature, not an error."""


class ProviderError(AiError):
    """A provider call failed. The SUBCLASS is the retry policy (§10.4) -- the
    orchestrator branches on type, never on message prose, because a policy
    recovered by string matching is a policy that changes when someone reworded
    an error."""


class ProviderUnavailableError(ProviderError):
    """Connection refused, DNS failure, or a 5xx from the provider. **Retried
    exactly once**, then surfaced."""


class ProviderTimeoutError(ProviderError):
    """The provider did not answer in time. **NOT RETRIED, EVER** (§10.4):
    retrying a slow provider makes it slower and burns the administrator's
    deadline twice."""


class ProviderRateLimitedError(ProviderError):
    """The provider returned 429. Surfaced to the caller as a 429 with a retry
    hint; never retried server-side, because a server-side retry against a rate
    limit is what turns a rate limit into an outage."""

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class ProviderConfigError(AiError):
    """The server's `ITS_AI_*` environment is incomplete or contradictory.
    Raised at CONFIGURATION READ TIME, naming the variable, so a broken
    deployment fails where an operator is looking rather than in front of an
    administrator."""


# The two legal values of `ITS_AI_<KEY>_KIND`. Named once so the error message
# below cannot drift from the set it validates against.
KINDS: tuple[str, ...] = ("openai_compatible", "anthropic")


# --------------------------------------------------------------------------- #
# The wire types (§11.1). NO PROVIDER SDK TYPE APPEARS ABOVE THIS INTERFACE.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TextBlock:
    """A chunk of assistant prose. The orchestrator concatenates these; the API
    layer republishes each one to the SSE side-channel as it arrives."""

    text: str


@dataclass(frozen=True)
class ToolCallBlock:
    """The model asking for one tool call.

    `name` and `arguments` are MODEL OUTPUT and are therefore untrusted: `name`
    is resolved in the static registry dict (an unknown name never becomes an
    HTTP request) and `arguments` is validated against the tool's closed
    Pydantic model. Neither is ever coerced or repaired (§10.5).
    """

    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ToolResultBlock:
    """One tool's outcome, travelling back to the model in a STRUCTURALLY
    SEPARATE channel (§8.2) -- never string-formatted into a user turn.

    `content` is already scrubbed, clamped and nonce-wrapped by the time it gets
    here; this type carries it, it does not sanitize it.
    """

    tool_call_id: str
    name: str
    content: Any
    is_error: bool = False


@dataclass(frozen=True)
class UsageBlock:
    """Token accounting, for `ai.query`'s audit metadata and the per-org daily
    budget. A provider that reports nothing leaves these zero, which the budget
    check treats as "unknown", never as "free"."""

    input_tokens: int = 0
    output_tokens: int = 0


Block = TextBlock | ToolCallBlock | ToolResultBlock | UsageBlock


# The reserved argument name a provider client uses when a model's tool-call
# arguments could not be parsed as a JSON object. No `args` model declares it and
# every `args` model is `extra="forbid"`, so it becomes an `invalid_arguments`
# tool error at the executor and the turn continues.
MALFORMED_ARGUMENTS_FIELD = "__unparsed_arguments__"


def parse_tool_arguments(raw: str) -> Mapping[str, Any]:
    """A model's tool-call argument string as a mapping. **NEVER REPAIRED.**

    §10.5: arguments are never coerced, repaired or guessed. Returning `{}` for
    an unparseable string would be exactly that repair -- a tool whose arguments
    are all optional would then RUN, with defaults, on a call the model did not
    actually make. So the raw text is preserved under a reserved field name that
    no args model declares, the closed model rejects it, and the model is told
    its arguments were unreadable.
    """
    try:
        parsed = json.loads(raw.strip() or "{}")
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        return {MALFORMED_ARGUMENTS_FIELD: raw}
    return parsed


@dataclass(frozen=True)
class Message:
    """One conversation turn as the provider sees it.

    `role` is `user` or `assistant` and NOTHING ELSE -- the same two values
    `ai_message.role`'s CHECK constraint allows. Tool results ride inside a
    `user` message's content as `ToolResultBlock`s rather than as a third role,
    which is what keeps the transcript table and the provider view in agreement
    about what a turn is.
    """

    role: Literal["user", "assistant"]
    content: tuple[Block, ...]


@runtime_checkable
class ModelProvider(Protocol):
    """§11.1, exactly. One method, and no SDK type in its signature.

    `deadline` is a `time.monotonic()` instant for the WHOLE exchange, not a
    per-phase timeout: per-phase timeouts reset on progress and therefore never
    bound a provider that trickles bytes forever (§5, and `app/core/egress.py`'s
    own property 5 for the same reason).
    """

    id: str

    def stream_turn(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        deadline: float,
    ) -> AsyncIterator[Block]: ...


# --------------------------------------------------------------------------- #
# Configuration (§11.2).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProviderConfig:
    """One configured provider, assembled from server environment only.

    FROZEN, so nothing downstream can retarget a provider's base URL at runtime.
    `api_key_path` is a FILE PATH, matching `Settings.audit_hmac_key_path`'s
    established convention: the key material is read at point of use and is
    never held on this object, never in an environment variable this process
    passes around, and therefore never in a `repr` that reaches a log line.
    """

    key: str
    kind: Literal["openai_compatible", "anthropic"]
    base_url: str
    model: str
    # Optional: a self-hosted vLLM/Ollama/llama.cpp server on the customer's own
    # network usually has no API key at all, and REQUIRING one would push
    # deployments toward inventing a dummy key file. A hosted provider that
    # needs one fails its own request with 401 if the path is unset, which is
    # visible and correct.
    api_key_path: str | None = field(default=None)

    def read_api_key(self) -> str | None:
        """The key material, read from disk AT POINT OF USE (invariant #3).

        Never cached on this dataclass. A rotated key file therefore takes
        effect on the next call rather than at the next process restart, and a
        `repr` of this object cannot contain a secret because the object never
        holds one.
        """
        if self.api_key_path is None:
            return None
        return Path(self.api_key_path).read_text().strip()


def _var(key: str, suffix: str) -> str:
    return f"ITS_AI_{key.upper()}_{suffix}"


def load_provider_configs(env: Mapping[str, str]) -> dict[str, ProviderConfig]:
    """`{provider_key: ProviderConfig}` from a mapping of environment variables.

    PURE OVER ITS INPUT: it reads nothing else, so every branch below is
    testable without mutating the process environment. `load_provider_configs({})`
    returns `{}` -- **the module is inert when unconfigured**, which is §0.10's
    "ships off" as a checkable property rather than a promise.
    """
    raw = env.get("ITS_AI_PROVIDERS", "").strip()
    if not raw:
        return {}

    configs: dict[str, ProviderConfig] = {}
    for token in raw.split(","):
        key = token.strip().lower()
        if not key:
            continue
        if not key.replace("_", "").isalnum():
            raise ProviderConfigError(  # i18n-exempt: startup config error, logged
                f"ITS_AI_PROVIDERS names an unusable provider key {key!r}; "
                f"keys are alphanumeric with underscores"
            )
        kind = env.get(_var(key, "KIND"), "").strip()
        if not kind:
            raise ProviderConfigError(  # i18n-exempt: startup config error, logged
                f"{_var(key, 'KIND')} is not set"
            )
        if kind not in KINDS:
            raise ProviderConfigError(  # i18n-exempt: startup config error, logged
                f"{_var(key, 'KIND')} is {kind!r}; it must be one of {', '.join(KINDS)}"
            )
        base_url = env.get(_var(key, "BASE_URL"), "").strip()
        if not base_url:
            raise ProviderConfigError(  # i18n-exempt: startup config error, logged
                f"{_var(key, 'BASE_URL')} is not set"
            )
        model = env.get(_var(key, "MODEL"), "").strip()
        if not model:
            raise ProviderConfigError(  # i18n-exempt: startup config error, logged
                f"{_var(key, 'MODEL')} is not set"
            )
        api_key_path = env.get(_var(key, "API_KEY_PATH"), "").strip() or None
        configs[key] = ProviderConfig(
            key=key,
            # `kind` is validated against KINDS above, so this narrowing is real.
            kind=kind,  # type: ignore[arg-type]
            base_url=base_url,
            model=model,
            api_key_path=api_key_path,
        )
    if len(configs) > 1:
        # §0.14. "The same model for every tenant" and a list of two providers
        # cannot both be true: something would have to choose between them, and
        # the thing that used to choose was a per-org dropdown this build
        # removed. Refusing HERE -- at configuration read time, which
        # `create_app` forces at startup -- puts the contradiction in front of
        # the operator who wrote it, in the file they wrote it in. The
        # alternative, silently taking the first key, is a deployment that
        # egresses somewhere its `.env` does not obviously say.
        raise ProviderConfigError(  # i18n-exempt: startup config error, logged
            f"ITS_AI_PROVIDERS names {len(configs)} providers "
            f"({', '.join(sorted(configs))}); this platform runs ONE model for "
            f"every organization on the deployment, so exactly one key may be "
            f"named. Remove all but the provider this control plane should use."
        )
    return configs


# The single cached read of `os.environ` in this module. `None` means "not read
# yet"; an empty dict means "read, and this deployment configured nothing".
_CONFIG_CACHE: dict[str, ProviderConfig] | None = None


def configured_providers() -> Mapping[str, ProviderConfig]:
    """This server's configured providers -- AT MOST ONE since §0.14. The ONLY
    place this module reads `os.environ` (§11.2's resolution).

    Still a mapping rather than an optional single value, because that is what
    `GET /ai/providers` publishes and because the one-provider rule is enforced
    where it is decided (`load_provider_configs`) rather than restated by every
    caller. `configured_provider()` below is the singular read.
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        _CONFIG_CACHE = load_provider_configs(os.environ)
    return _CONFIG_CACHE


def configured_provider() -> ProviderConfig | None:
    """THE provider this deployment uses, or `None` when none is configured.

    §0.14: there is no per-org selection to consult, so "which provider" is a
    question with exactly one answer for the whole control plane.
    """
    return next(iter(configured_providers().values()), None)


def reset_configured_providers() -> None:
    """Drop the cache. For tests and for a process that re-reads configuration;
    never called from a request path."""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None


# --------------------------------------------------------------------------- #
# Resolution.
# --------------------------------------------------------------------------- #

# Kind -> client factory, one entry per member of `KINDS`.
#
# **A CLIENT EXISTING IS NOT A CLIENT BEING REACHABLE.** Nothing below is
# constructed until `configured_provider()` answers with something, and that is
# `load_provider_configs(os.environ)` -- which returns `{}` for a deployment that
# set no `ITS_AI_*` variable. There is no default key, no default kind and no
# fallback branch; a reader tempted to add one "so it works out of the box"
# should read this file's header first. Since §0.14 this is the ONLY gate left
# in front of egress, so a default here would not be a convenience, it would be
# the whole feature switching itself on.
#
# The imports are resolved on first use rather than at module import, because
# both client modules import the types defined above them in this file. That is
# a genuine cycle at import time and a function-local import is the ordinary way
# out of it; it has no other purpose and grants no other behaviour.
_CLIENT_FACTORIES: dict[str, Any] = {}


def _client_factories() -> dict[str, Any]:
    global _CLIENT_FACTORIES
    if not _CLIENT_FACTORIES:
        from app.core.ai.providers.anthropic import AnthropicProvider
        from app.core.ai.providers.openai_compatible import (
            OpenAICompatibleProvider,
        )

        _CLIENT_FACTORIES = {
            "openai_compatible": OpenAICompatibleProvider,
            "anthropic": AnthropicProvider,
        }
    return _CLIENT_FACTORIES


def get_provider() -> ModelProvider:
    """This deployment's `ModelProvider`, or `ProviderNotConfiguredError`.

    **NO ARGUMENT, BECAUSE THERE IS NOTHING LEFT TO SELECT** (§0.14). It used to
    take the org's `ai_settings.provider_key`; that column is gone, and with it
    the last way an org-admin could name a destination.

    Both remaining failure modes are the same exception on purpose: "this server
    configured no provider" and "that kind has no client in this build" are both
    "the AI surface is not available here", and the API layer turns each into the
    same 503 with a message naming the cause.
    """
    config = configured_provider()
    if config is None:
        raise ProviderNotConfiguredError(tr("no model provider is configured on this server"))
    factory = _client_factories().get(config.kind)
    if factory is None:  # pragma: no cover - `KINDS` and the factory map agree
        raise ProviderNotConfiguredError(
            tr(
                "provider {key} is configured with kind {kind}, which has no client in this "
                "build",
                key=repr(config.key),
                kind=repr(config.kind),
            )
        )
    provider: ModelProvider = factory(config)
    return provider
