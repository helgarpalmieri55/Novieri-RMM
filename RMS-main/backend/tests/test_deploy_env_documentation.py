"""`deploy/.env.example` is the operator's only complete list of what this
server reads from the environment, and for the AI admin agent it is the ONLY
list anywhere in the codebase.

`app/core/config.py` cannot help: `ITS_AI_<KEY>_KIND`, `_BASE_URL`, `_MODEL` and
`_API_KEY_PATH` are DYNAMIC names built from whatever `ITS_AI_PROVIDERS`
contains, so they are deliberately not pydantic `Settings` fields and no
introspection of `Settings` will ever mention them.
`providers.load_provider_configs(os.environ)` reads them directly. That makes
the documentation load-bearing rather than courteous, which is why it is tested.

THE SECOND TEST IS THE ONE THAT CAUGHT A REAL DEFECT. `deploy/README.md` showed
`ITS_AI_LOCAL_API_KEY_PATH=/run/secrets/ai-provider.key`, and **nothing in
`docker-compose.yml` mounts `/run/secrets` on the `api` service**. Following the
README literally gives a `FileNotFoundError` at the first hosted-provider call --
and, worse, puts key material outside the volume set `docs/07-backup-restore.md`
§2.2 backs up. A prose fix would silently rot back; a test that derives the legal
prefixes from the compose file itself cannot.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
ENV_EXAMPLE = REPO / "deploy" / ".env.example"
COMPOSE = REPO / "deploy" / "docker-compose.yml"
DEPLOY_README = REPO / "deploy" / "README.md"


def _api_container_paths() -> list[str]:
    """Container-side mount points on the `api` service. A secret path that is
    not under one of these does not survive `--force-recreate`, is not in the
    backup set, and in the `/run/secrets` case does not exist at all."""
    compose = yaml.safe_load(COMPOSE.read_text())
    mounts = []
    for volume in compose["services"]["api"].get("volumes", []):
        # "named-volume:/container/path" or "./host:/container/path:ro"
        parts = str(volume).split(":")
        if len(parts) >= 2:
            mounts.append(parts[1])
    return mounts


def test_every_ai_provider_variable_is_documented() -> None:
    """The names are built by `providers._var`, so they are derived here rather
    than typed out: renaming the suffix in code without touching `.env.example`
    fails this, which is the whole failure mode -- five settings that exist only
    in one deployment doc and one module."""
    from app.core.ai.providers import _var

    text = ENV_EXAMPLE.read_text()
    assert "ITS_AI_PROVIDERS" in text, "the provider list variable is undocumented"
    for suffix in ("KIND", "BASE_URL", "MODEL", "API_KEY_PATH"):
        name = _var("local", suffix)
        assert name in text, f"{name} is read by load_provider_configs but undocumented"


@pytest.mark.parametrize("doc", [ENV_EXAMPLE, DEPLOY_README], ids=[".env.example", "README.md"])
def test_the_one_provider_rule_is_documented_where_the_operator_edits(doc: Path) -> None:
    """`ITS_AI_PROVIDERS` naming two keys raises at startup (spec §0.14, the
    operator's decision of 2026-08-25: one model for every tenant). An operator
    meets that rule while editing `.env`, so it has to be written there and in
    the deploy README -- an error message alone teaches it only after the
    deployment has already failed to come up.

    Asserted on the SENTENCE rather than on a keyword, because "comma-separated
    provider keys" -- what this line said before the decision -- contains every
    keyword a looser check would look for and says the opposite thing.
    """
    text = doc.read_text().lower()
    assert "exactly one provider may be named" in text or "one provider key" in text, (
        f"{doc.name} does not say that ITS_AI_PROVIDERS takes exactly one key"
    )
    assert "comma-separated" not in text, (
        f"{doc.name} still describes ITS_AI_PROVIDERS as a comma-separated list, "
        f"which is the pre-2026-08-25 contract"
    )


@pytest.mark.parametrize("doc", [ENV_EXAMPLE, DEPLOY_README], ids=[".env.example", "README.md"])
def test_documented_provider_key_paths_are_actually_mounted_on_api(doc: Path) -> None:
    """Every `ITS_AI_*_API_KEY_PATH=` example must name a path inside a
    directory the `api` service mounts. `/run/secrets` -- what the README used
    to say -- is mounted nowhere in this stack."""
    mounts = _api_container_paths()
    assert "/var/lib/its/secrets" in mounts, "the api service stopped mounting its-secrets"

    paths = re.findall(r"ITS_AI_[A-Z0-9_]*API_KEY_PATH=(\S+)", doc.read_text())
    assert paths, f"{doc.name} documents no provider key path at all"
    for path in paths:
        assert any(path.startswith(f"{m.rstrip('/')}/") for m in mounts), (
            f"{doc.name} documents the provider key at {path}, which the `api` "
            f"service does not mount (it mounts {mounts}). Reading it raises "
            f"FileNotFoundError, and it sits outside the backed-up volume set."
        )


def test_the_secrets_volume_is_in_the_backup_runbook() -> None:
    """The key path is only safe because `its-secrets` is one of the volumes
    docs/07-backup-restore.md §2.2 tars. If that ever stops being true the
    guidance above becomes "put your key somewhere we do not back up"."""
    runbook = (REPO / "docs" / "07-backup-restore.md").read_text()
    assert "its-secrets" in runbook


@pytest.mark.parametrize(
    ("module", "marker"),
    [
        ("health", "--- Endpoint health & alerting ---"),
        ("ipam", "--- IPAM ---"),
        ("ai_admin", "--- AI admin agent ---"),
    ],
)
def test_every_module_has_a_section_even_when_it_adds_nothing(
    module: str, marker: str
) -> None:
    """`.env.example` carries a per-module section for every module through SLA,
    INCLUDING the ones that add no variables ("Control-plane only: NO env
    vars..."). That convention is what makes "this module needs nothing" a
    recorded finding rather than an omission indistinguishable from an
    oversight. Health and IPAM genuinely add nothing -- there is not one
    `os.environ` read under either package -- and the section says so."""
    assert marker in ENV_EXAMPLE.read_text(), f"{module} has no section in .env.example"


def test_health_and_ipam_really_do_read_no_environment() -> None:
    """The claim the two sections above make, checked rather than trusted: if a
    later change starts reading `os.environ` under either package, the section
    that says it does not becomes a lie and this fails."""
    for package in ("health", "ipam"):
        root = REPO / "backend" / "app" / "modules" / package
        offenders = [
            path.relative_to(REPO).as_posix()
            for path in root.rglob("*.py")
            if re.search(r"\bos\.(environ|getenv)\b", path.read_text())
        ]
        assert offenders == [], (
            f"{package} now reads the environment ({offenders}); .env.example says it does not"
        )


def test_the_console_address_says_every_push_install_needs_it() -> None:
    """Review Minor 8: dispatch refuses every push install -- Windows, Linux
    and macOS -- while ITS_PUBLIC_CONSOLE_URL is unset (a pushed machine would
    not know where to enroll). The comment the operator reads above the key
    must say so, in the words the refusal uses."""
    text = ENV_EXAMPLE.read_text()
    block = text[: text.index("\nITS_PUBLIC_CONSOLE_URL=")]
    block = block[block.rindex("# --- Console address") :]
    flat = " ".join(line.lstrip("# ").strip() for line in block.splitlines())
    assert "EVERY PUSH INSTALL IS REFUSED WITHOUT IT" in flat
    assert "Set ITS_PUBLIC_CONSOLE_URL so a pushed computer knows where to enroll." in flat
