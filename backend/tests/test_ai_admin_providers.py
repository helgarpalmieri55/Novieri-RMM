"""The provider seam, and the proof that this module is INERT WHEN UNCONFIGURED.

docs/modules/ai-admin/spec.md §0.10, §11.1, §11.2.

WHAT "INERT" HAS TO MEAN FOR A TEST TO SEE IT. "The feature is off by default"
is unobservable if the only assertion is that a config dict is empty -- a
deployment that egresses because nobody turned it off is exactly the failure
this decision exists to prevent, and it would arrive as a one-line
`DEFAULT_BASE_URL` fallback that no reviewer reads twice. So inertness is
asserted three ways here, each of which sees a different mutation:

  * BEHAVIOURAL: `load_provider_configs({})` is empty and `get_provider()`
    raises. A default-provider fallback makes the first assertion observe a
    NON-EMPTY provider map for an unconfigured deployment. **This proof carries
    the whole feature since §0.14** (the operator's decision of 2026-08-25):
    the per-org `enabled` flag that used to stand in front of it is gone, so
    server configuration is no longer the second gate, it is the only one.
  * STRUCTURAL, over string literals: no source file in the package contains a
    URL literal (other than the executor's `.invalid` loopback sentinel, which
    is never resolved), a model-name-shaped literal, or an `sk-`-shaped literal.
    A convenience default is observed AS A LITERAL, and the failure names the
    file it was added to.
  * STRUCTURAL, over egress: the package's `httpx` client construction sites are
    pinned as an EQUALITY -- the executor's in-process loopback and the single
    shared outbound client -- and the package imports neither `app.core.egress`
    nor any provider SDK. A third site is observed even if nothing calls it.

**WHAT CHANGED WHEN THE TWO PROVIDER CLIENTS LANDED, AND WHAT DID NOT.** The
first proof is unchanged and is now doing MORE work, not less: it was always
"nothing resolves", and with clients in the image it is the only thing standing
between an unconfigured deployment and the internet. The second is unchanged and
still passes, which is the point -- the client files compose their base URL and
model name from `ProviderConfig` and name neither, so a convenience default is
still a red build. Only the third moved, from "no client exists outside
`executor.py`" to "these two files construct clients and no others". The honest
claim after this build is **not** "there is no client"; it is **"a client exists
and is unreachable without server configuration"**, and the tests below say
exactly that rather than the stronger thing that stopped being true.
"""

import ast
import importlib
import pkgutil
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from app.core.ai import providers as providers_module
from app.core.ai.providers import (
    KINDS,
    Message,
    ModelProvider,
    ProviderConfigError,
    ProviderNotConfiguredError,
    TextBlock,
    ToolCallBlock,
    configured_providers,
    get_provider,
    load_provider_configs,
    reset_configured_providers,
)

PACKAGE_ROOT = Path(providers_module.__file__).resolve().parent.parent


def _full_env(key: str = "local", kind: str = "openai_compatible") -> dict[str, str]:
    upper = key.upper()
    return {
        "ITS_AI_PROVIDERS": key,
        f"ITS_AI_{upper}_KIND": kind,
        f"ITS_AI_{upper}_BASE_URL": "http://vllm.internal.test:8000/v1",
        f"ITS_AI_{upper}_MODEL": "a-model-name",
    }


# --------------------------------------------------------------------------- #
# 1. Inert when unconfigured.
# --------------------------------------------------------------------------- #


def test_an_unconfigured_deployment_has_no_providers() -> None:
    """§0.10. THE WHOLE "SHIPS OFF" DECISION IS THIS ASSERTION.

    If a later release adds a default base URL and falls back to it when
    `ITS_AI_PROVIDERS` is unset, this observes a non-empty provider map for a
    deployment that configured nothing -- which is a deployment that egresses
    because nobody turned it off.
    """
    assert load_provider_configs({}) == {}


def test_an_unconfigured_deployment_cannot_resolve_any_provider(monkeypatch: Any) -> None:
    """The behavioural half of inertness: with nothing configured, resolution
    fails.

    **THE SHAPE CHANGED** (§0.14): `get_provider` took a key and this loop tried
    six of them, including the ones a default would have supplied. There is no
    key any more -- one provider per deployment -- so what a default would now
    look like is a fallback `ProviderConfig` built inside `get_provider`, and
    that is what this observes.
    """
    monkeypatch.setattr(providers_module, "_CONFIG_CACHE", {})
    with pytest.raises(ProviderNotConfiguredError, match="no model provider is configured"):
        get_provider()


def test_a_client_exists_but_is_unreachable_without_server_configuration(
    monkeypatch: Any,
) -> None:
    """**THE CLAIM THIS FILE MAKES, RESTATED HONESTLY NOW THAT CLIENTS EXIST.**

    Until the two provider clients landed, the strongest available assertion was
    "no implementation exists, so nothing can reach a network". That is no longer
    true, and a test still asserting it would be a test asserting a lie about the
    shipped code. So the property is now the one that actually protects a
    deployment: **a client exists, and configuration is the only thing that can
    reach it.**

    Both halves are asserted against VALUES, not against exception-or-not:

      * with an empty environment, resolution raises and the message names the
        missing config rather than a missing client;
      * with a complete environment, it resolves to a client whose `id` names
        the configured model, so a mutation that made resolution depend on
        something other than configuration is visible as a wrong id.
    """
    monkeypatch.setattr(providers_module, "_CONFIG_CACHE", {})
    with pytest.raises(ProviderNotConfiguredError, match="is configured on this server"):
        get_provider()

    for kind in KINDS:
        monkeypatch.setattr(
            providers_module, "_CONFIG_CACHE", load_provider_configs(_full_env(kind=kind))
        )
        provider = get_provider()
        assert provider.id == f"{kind}:a-model-name", provider.id


# --------------------------------------------------------------------------- #
# 2. Load-time validation (§11.2).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("missing", ["KIND", "BASE_URL", "MODEL"])
def test_a_missing_variable_raises_at_load_time_naming_it(missing: str) -> None:
    """FAIL AT STARTUP, NOT AT 09:00 WHEN AN ADMIN ASKS A QUESTION.

    The error names the exact variable, because "AI is broken" and
    "ITS_AI_LOCAL_BASE_URL is not set" are minutes apart for whoever is paged.
    If validation were moved to first use, this observes a SUCCESSFULLY LOADED
    config for a broken deployment.
    """
    env = _full_env()
    del env[f"ITS_AI_LOCAL_{missing}"]
    # Written as try/else rather than `pytest.raises` so the failure PRINTS THE
    # CONFIG THAT LOADED. "DID NOT RAISE" tells a reader nothing about what the
    # broken deployment came up as; the config does.
    try:
        configs = load_provider_configs(env)
    except ProviderConfigError as exc:
        assert f"ITS_AI_LOCAL_{missing}" in str(exc), str(exc)
    else:
        raise AssertionError(
            f"a deployment with no ITS_AI_LOCAL_{missing} loaded successfully as {configs!r}"
        )


def test_an_unknown_kind_raises_naming_the_two_legal_values() -> None:
    env = _full_env()
    env["ITS_AI_LOCAL_KIND"] = "bedrock"
    with pytest.raises(ProviderConfigError) as excinfo:
        load_provider_configs(env)
    message = str(excinfo.value)
    assert "bedrock" in message
    for kind in KINDS:
        assert kind in message


def test_a_second_provider_is_a_configuration_error_naming_the_variable() -> None:
    """**THE DECISION CHANGED** (spec §0.14, operator, 2026-08-25: "we will use
    the same [model] for every tenant"). This test was
    `test_several_providers_load_independently` and asserted that `local` and
    `hosted` loaded side by side, because a console dropdown chose between them
    per organization.

    **THE NEW RULE:** there is no dropdown and nothing to choose, so a second
    provider is a contradiction in the deployment's own configuration and is
    refused at read time -- which `create_app` forces at startup, so the
    operator sees it in the terminal beside the file they edited. Both keys are
    named in the message, because "one of these is wrong" is only actionable if
    the operator can see which two are in play.
    """
    env = _full_env("local") | _full_env("hosted", kind="anthropic")
    env["ITS_AI_PROVIDERS"] = "local, hosted"
    with pytest.raises(ProviderConfigError) as excinfo:
        load_provider_configs(env)
    message = str(excinfo.value)
    assert "ITS_AI_PROVIDERS" in message
    assert "local" in message and "hosted" in message


def test_exactly_one_provider_still_loads() -> None:
    """The other side of the rule above: one key is the supported deployment,
    and it loads with its kind and model intact."""
    configs = load_provider_configs(_full_env("hosted", kind="anthropic"))
    assert set(configs) == {"hosted"}
    assert configs["hosted"].kind == "anthropic"
    assert configs["hosted"].model == "a-model-name"


def test_configured_providers_reads_the_process_environment_once(monkeypatch: Any) -> None:
    """The single `os.environ` read in this module (§11.2's resolution). It is
    cached, so a later mutation of the environment cannot retarget a provider
    mid-process."""
    reset_configured_providers()
    monkeypatch.setattr("os.environ", _full_env(), raising=False)
    assert set(configured_providers()) == {"local"}
    monkeypatch.setattr("os.environ", {}, raising=False)
    assert set(configured_providers()) == {"local"}, "the read was not cached"
    reset_configured_providers()
    assert configured_providers() == {}


# --------------------------------------------------------------------------- #
# 3. The key never lands anywhere it can be read back.
# --------------------------------------------------------------------------- #


def test_the_api_key_is_read_from_the_file_and_never_held_on_the_config(
    tmp_path: Path,
) -> None:
    """Invariant #3's shape, matching `Settings.audit_hmac_key_path`: the config
    holds a PATH and reads the material at point of use.

    The `repr` assertion is the load-bearing one. A `ProviderConfig` that cached
    its key would put that key into every log line that ever formats a config --
    and the seeded key here is a distinctive string, so this observes it.
    """
    secret = "sk-test-DO-NOT-LOG-8f2c1a"
    key_file = tmp_path / "provider.key"
    key_file.write_text(f"{secret}\n")
    env = _full_env() | {"ITS_AI_LOCAL_API_KEY_PATH": str(key_file)}

    config = load_provider_configs(env)["local"]

    assert config.read_api_key() == secret, "the key is read from the file"
    assert secret not in repr(config)
    assert secret not in str(config)
    assert secret not in str(vars(config))


def test_a_provider_without_a_key_file_reads_no_key() -> None:
    """A self-hosted vLLM/Ollama on the customer's own network usually has no
    API key. Requiring one would push deployments toward inventing a dummy key
    file, which is worse than having none."""
    assert load_provider_configs(_full_env())["local"].read_api_key() is None


def test_provider_config_is_frozen() -> None:
    """Nothing downstream may retarget a provider's base URL at runtime."""
    config = load_provider_configs(_full_env())["local"]
    with pytest.raises(Exception):  # noqa: B017 -- pydantic/dataclass FrozenInstanceError
        config.base_url = "http://attacker.test/v1"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 4. No bundled key, no default endpoint (structural, over string literals).
# --------------------------------------------------------------------------- #


def _module_source_files() -> list[Path]:
    files = sorted(p for p in PACKAGE_ROOT.rglob("*.py") if "__pycache__" not in p.parts)
    assert len(files) >= 8, f"the literal scan found only {len(files)} source files"
    return files


def _string_literals(path: Path) -> list[str]:
    """Every string literal in `path` EXCEPT docstrings.

    Docstrings are excluded deliberately: this file's own prose talks about
    `sk-` and about base URLs, and so does the module it guards. A scanner that
    could not tell a docstring from a value would be silenced within a week by
    whoever hit that false positive -- and a silenced scanner sees nothing.
    """
    tree = ast.parse(path.read_text())
    docstring_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_nodes.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_nodes
    ]


# The executor's loopback sentinel. `ASGITransport` calls the app object
# directly, so this host is never resolved and never connected to; it exists
# only because `httpx` requires an absolute URL. Any OTHER url literal in this
# package is a default endpoint and fails the test below.
_ALLOWED_URL_LITERALS = {"http://ai-loopback.invalid"}

_MODEL_NAME_SHAPES = re.compile(
    r"gpt-[0-9]|claude-[0-9a-z]|llama-?[0-9]|mistral|mixtral|gemini-|qwen|"
    r"sonnet|opus-|haiku|deepseek",
    re.IGNORECASE,
)


def test_no_source_file_contains_a_default_endpoint_or_bundled_key() -> None:
    """§0.10. **THE MUTATION THIS EXISTS FOR IS A ONE-LINE CONVENIENCE DEFAULT.**

    `DEFAULT_BASE_URL = "https://api.anthropic.com"` plus a fallback is a change
    a reviewer reads as helpful. This observes the literal and names the file,
    so the review conversation happens.
    """
    offences: list[str] = []
    for path in _module_source_files():
        for literal in _string_literals(path):
            relative = path.relative_to(PACKAGE_ROOT.parent)
            if "://" in literal and literal not in _ALLOWED_URL_LITERALS:
                offences.append(f"{relative}: url literal {literal!r}")
            if literal.startswith("sk-") or literal.startswith("sk_live"):
                offences.append(f"{relative}: api-key-shaped literal")
            if _MODEL_NAME_SHAPES.search(literal):
                offences.append(f"{relative}: model-name literal {literal!r}")
    assert offences == [], "; ".join(offences)


# The complete set of files in this package that may construct an HTTP client,
# and what each one is for. Restated here as well as in
# `tests/test_ai_admin_egress.py` deliberately: this file's subject is "what is
# in the package", and a reader auditing inertness should not have to open a
# second file to learn that the answer is three.
#
# **IT BECAME THREE WHEN THE PORTAL EXECUTOR LANDED, AND BOTH OF THE OTHER TWO
# ARE STILL THE ONLY OUTBOUND ONE.** The count that matters to THIS file --
# "how many places could contact a model provider" -- is unchanged at ONE
# (`providers/_http.py`). The two loopbacks call the app object directly through
# `ASGITransport` and never open a socket.
_CLIENT_CONSTRUCTION_SITES = {
    # In-process only. `ASGITransport` calls the app object directly, so this
    # client never opens a socket (§0.1).
    "executor_staff.py",
    # The resolver's own in-process loopback (docs/modules/ai-automation.md
    # §0.2). A second executor rather than a principal-kind branch, and it
    # refuses any tool outside `/api/v1/portal/` before a URL exists.
    "executor_portal.py",
    # The module's ONE outbound path, shared by BOTH provider clients so that
    # adding a provider kind adds no egress site.
    "providers/_http.py",
}


def test_the_package_constructs_an_http_client_in_exactly_the_three_known_places() -> None:
    """SHIP ONE EGRESS PATH, AND KNOW WHERE IT IS.

    This assertion used to be "no client outside `executor.py`", which was the
    right claim while no provider client existed. It is now an EQUALITY over the
    set of files, which is strictly stronger than the "no offenders" form it
    replaces: a third construction site fails and names the file, and a
    construction site that DISAPPEARS also fails -- because a scan finding
    nothing where it expected something is a broken scan, not a clean bill of
    health, and that is exactly how this guard would rot if the outbound client
    were ever moved.
    """
    found: set[str] = set()
    for path in _module_source_files():
        source = path.read_text()
        # `httpx.get(url)` is a complete egress path in ONE EXPRESSION and
        # constructs no client a reader would notice, so the module-level
        # helpers are scanned alongside the constructors.
        for marker in (
            "httpx.AsyncClient(",
            "httpx.Client(",
            "httpx.get(",
            "httpx.post(",
            "httpx.stream(",
            "httpx.request(",
            "aiohttp",
            "requests.get(",
        ):
            if marker in source:
                found.add(str(path.relative_to(PACKAGE_ROOT)))
    assert found == _CLIENT_CONSTRUCTION_SITES, (
        f"client construction sites changed: {sorted(found)} != "
        f"{sorted(_CLIENT_CONSTRUCTION_SITES)}"
    )


def test_the_package_imports_no_egress_helper_and_no_provider_sdk() -> None:
    """§0.10: removing the SSRF primitive beats guarding it.

    `app/core/egress.py` is written for webhook delivery -- it refuses
    redirects, caps the body, and never returns the body in full, all of which
    are wrong for a streaming completion. Its presence here would mean somebody
    had reintroduced an arbitrary-URL feature and then guarded it.
    """
    forbidden = ("app.core.egress", "openai", "anthropic.", "litellm", "langchain")
    offenders: list[str] = []
    for path in _module_source_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if any(name == bad or name.startswith(bad) for bad in forbidden):
                    offenders.append(f"{path.relative_to(PACKAGE_ROOT.parent)}: import {name}")
    assert offenders == [], "; ".join(offenders)


# --------------------------------------------------------------------------- #
# 5. The base URL is not reachable from any request body.
# --------------------------------------------------------------------------- #

_FORBIDDEN_MODEL_FIELDS = {"base_url", "url", "endpoint", "api_key", "api_key_path", "host"}


def _pydantic_models_in_package() -> list[type[BaseModel]]:
    """Every Pydantic model in the kernel AI package AND in the admin persona's
    own package.

    BOTH ROOTS, because the code split across two after the relocation and the
    property did not. The request models an org-admin's body reaches --
    `SettingsUpdate` above all -- stayed in `app/modules/ai_admin/schema.py`, so
    a scan of the kernel package alone finds zero models. The vacuity floor
    below is what caught exactly that.
    """
    import app.modules.ai_admin as admin_package

    # `app.models` first: `app/core/ai/store.py` imports the declarative AI
    # models, and importing the kernel package up front keeps this walk in the
    # ordinary import order every process uses rather than in whatever order
    # `walk_packages` happens to land on.
    importlib.import_module("app.models")
    admin_root = Path(admin_package.__file__).resolve().parent
    models: list[type[BaseModel]] = []
    for root, prefix in ((PACKAGE_ROOT, "app.core.ai"), (admin_root, "app.modules.ai_admin")):
        for info in pkgutil.walk_packages([str(root)], prefix=f"{prefix}."):
            module = importlib.import_module(info.name)
            for value in vars(module).values():
                if (
                    isinstance(value, type)
                    and issubclass(value, BaseModel)
                    and value is not BaseModel
                    and value.__module__.startswith(prefix)
                ):
                    models.append(value)
    return models


def test_no_request_model_in_this_module_can_carry_a_base_url() -> None:
    """§0.10: an org-admin picks a provider from a list; they never type a URL
    and never see a key. THERE IS NO FIELD FOR IT.

    The count assertion is not decoration -- an empty model list would make this
    test pass against a `schema.py` that had grown a `base_url` field and then
    failed to import.
    """
    models = _pydantic_models_in_package()
    assert len(models) >= 3, f"only {len(models)} pydantic models found; the scan is vacuous"
    for model in models:
        overlap = _FORBIDDEN_MODEL_FIELDS & set(model.model_fields)
        assert not overlap, f"{model.__module__}.{model.__name__} declares {sorted(overlap)}"


# --------------------------------------------------------------------------- #
# 6. The protocol and the fakes agree.
# --------------------------------------------------------------------------- #


def test_the_shipped_fakes_satisfy_the_protocol() -> None:
    """Both fakes are structurally `ModelProvider`s, so the orchestrator's tests
    exercise the real call shape rather than a convenient one."""
    from tests.ai_admin_fakes import RecordingProvider, ScriptedProvider

    assert isinstance(RecordingProvider(), ModelProvider)
    assert isinstance(ScriptedProvider([]), ModelProvider)


async def test_the_scripted_provider_emits_the_blocks_it_was_given() -> None:
    """The fake that simulates an OBEDIENT-TO-INJECTION model: it emits any tool
    name and any arguments, including ones no registry entry has."""
    from tests.ai_admin_fakes import ScriptedProvider

    provider = ScriptedProvider(
        [
            [ToolCallBlock(id="1", name="credentials.list", arguments={})],
            [TextBlock(text="done")],
        ]
    )
    first = [block async for block in provider.stream_turn(
        system="s", messages=[Message(role="user", content=())], tools=[], deadline=0.0
    )]
    second = [block async for block in provider.stream_turn(
        system="s", messages=[], tools=[], deadline=0.0
    )]
    assert first == [ToolCallBlock(id="1", name="credentials.list", arguments={})]
    assert second == [TextBlock(text="done")]
    assert provider.calls == 2
