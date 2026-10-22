"""Answer-file renderer — THE security crux of provisioning
(docs/modules/provisioning/spec.md §4/§7).

This is the only place run secrets (sealed domain-join / local-admin
credentials on a `ProvisionProfile`) and the enrollment token materialize
together in plaintext. `render_answer_file` resolves each `{SEALED_MARKER:
...}` reference in `profile.post_config` via `vault.open_bytes` at render
time only, builds a plaintext `fields` dict that never leaves this module,
and hands it to a pure `_render_*` helper that produces the final answer-file
bytes. The plaintext is never written to a DB column, never placed in audit
metadata (this function records no audit event at all — the caller in Task 6
audits the render *intent* without secrets), and never logged: this module
contains no `logger`/`print` call of any kind.

Determinism is a hard requirement: the returned bytes must be a pure function
of `(profile, image, token)`. `now` is accepted only for interface symmetry
with the caller's own audit trail — it is never read, never embedded in the
output, and no wall-clock/random value of any kind appears in the rendered
bytes. Fields are also read from `post_config` via explicit named lookups
(never `dict` iteration order) so output ordering cannot drift with Python's
dict implementation.
"""

import copy
import shlex
from datetime import datetime
from typing import Any
from uuid import UUID
from xml.sax.saxutils import escape

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import tr
from app.core.vault import VaultService
from app.core.wire_words import say_list
from app.models import OsImage, ProvisionProfile
from app.modules.provisioning.repository import (
    _CREDENTIAL_PATHS,
    SEALED_MARKER,
    ProvisioningValidationError,
)

_DEFAULT_HOSTNAME = "localhost"
_LINUX_INSTALLERS = {"kickstart", "preseed", "cloud-init"}
# The installer names are format and tool names the console has no Spanish for, so
# the table is empty on purpose: `say_list` prints the list as Python did in English
# and as a plain comma-separated list in Spanish.
_NO_INSTALLER_WORDS: dict[str, str] = {}


def _dict_field(fields: dict[str, Any], key: str) -> dict[str, Any]:
    value = fields.get(key)
    return value if isinstance(value, dict) else {}


def _checked_for_control_chars(name: str, value: str) -> str:
    """Reject CR/LF/NUL and other control characters in a value that is about
    to be interpolated into a line-oriented answer-file format. A credential
    containing e.g. a newline followed by installer directives (`%post`,
    `late_command`, ...) could otherwise inject arbitrary root-executing
    steps into a kickstart/preseed/cloud-init document."""
    if any(c in value for c in "\r\n\x00") or any(ord(c) < 32 and c != "\t" for c in value):
        raise ProvisioningValidationError(
            tr("injected field {name} contains control characters", name=repr(name))
        )
    return value


async def _resolve_credentials(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    profile_id: UUID,
    post_config: dict[str, Any],
) -> dict[str, Any]:
    """Deep-copy post_config and resolve sealed markers ONLY at the two known
    credential paths (`domain_join.password`, `local_admin.password` —
    `repository._CREDENTIAL_PATHS`, the only paths `_seal_credentials` ever
    seals). A hand-planted `{"__sealed__": ...}` marker anywhere else in
    post_config is left untouched rather than silently decrypted. All other
    fields are copied through as-is."""
    fields = copy.deepcopy(post_config)
    for outer_key, inner_key in _CREDENTIAL_PATHS:
        outer = fields.get(outer_key)
        if not isinstance(outer, dict):
            continue
        value = outer.get(inner_key)
        if not isinstance(value, dict) or set(value.keys()) != {SEALED_MARKER}:
            continue
        sealed = value[SEALED_MARKER]
        plaintext = await vault.open_bytes(
            session, org_id=org_id, key_id=profile_id, sealed=sealed
        )
        outer[inner_key] = plaintext.decode()
    return fields


async def render_answer_file(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    profile: ProvisionProfile,
    image: OsImage,
    token: str,
    now: datetime,  # noqa: ARG001 - accepted for audit/interface symmetry only, never embedded
) -> bytes:
    """Resolve `profile.post_config` into plaintext `fields` (decrypting any
    sealed credential refs via the vault at this render call only), then
    dispatch to the pure, deterministic `_render_*` helper for `image.os_family`.

    Returns the rendered answer-file bytes. Writes nothing: no DB column, no
    audit event, no log line ever carries the resolved plaintext.
    """
    post_config = profile.post_config if isinstance(profile.post_config, dict) else {}
    fields = await _resolve_credentials(
        session, vault, org_id=org_id, profile_id=profile.id, post_config=post_config
    )
    fields["token"] = token
    fields.setdefault("hostname", _DEFAULT_HOSTNAME)

    if image.os_family == "windows":
        return _render_unattend(fields)
    if image.os_family == "linux":
        installer = image.params.get("installer")
        if installer not in _LINUX_INSTALLERS:
            raise ProvisioningValidationError(
                tr(
                    "linux image requires params.installer in {installers}, got {installer}",
                    installers=say_list(_NO_INSTALLER_WORDS, sorted(_LINUX_INSTALLERS)),
                    installer=repr(installer),
                )
            )
        return _render_linux(installer, fields)
    if image.os_family == "macos":
        raise ProvisioningValidationError(
            tr("macOS uses ADE/MDM enrollment, not an answer file")
        )
    raise ProvisioningValidationError(
        tr("unsupported os_family: {os_family}", os_family=repr(image.os_family))
    )


def _render_unattend(fields: dict[str, Any]) -> bytes:
    """A minimal but valid Windows unattend.xml. Deterministic: fields are
    read by explicit key, never by iterating the dict."""
    hostname = str(fields.get("hostname", _DEFAULT_HOSTNAME))
    token = str(fields.get("token", ""))
    domain_join = _dict_field(fields, "domain_join")
    local_admin = _dict_field(fields, "local_admin")
    domain_user = str(domain_join.get("user", ""))
    domain_password = str(domain_join.get("password", ""))
    admin_password = str(local_admin.get("password", ""))

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup">
      <ComputerName>{escape(hostname)}</ComputerName>
    </component>
    <component name="Microsoft-Windows-UnattendedJoin">
      <Identification>
        <Credentials>
          <Username>{escape(domain_user)}</Username>
          <Password>{escape(domain_password)}</Password>
        </Credentials>
      </Identification>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-Shell-Setup">
      <UserAccounts>
        <AdministratorPassword>
          <Value>{escape(admin_password)}</Value>
          <PlainText>true</PlainText>
        </AdministratorPassword>
      </UserAccounts>
      <FirstLogonCommands>
        <SynchronousCommand>
          <CommandLine>agent.exe enroll --token {escape(token)}</CommandLine>
          <Order>1</Order>
        </SynchronousCommand>
      </FirstLogonCommands>
    </component>
  </settings>
</unattend>
"""
    return xml.encode("utf-8")


def _render_linux(installer: str, fields: dict[str, Any]) -> bytes:
    """A minimal but plausible kickstart / preseed / cloud-init document for
    the selected installer. Deterministic: fields are read by explicit key.

    Every credential/token value is rejected if it contains a control
    character (CR/LF/NUL) before it is interpolated anywhere — kickstart and
    preseed are line-oriented formats, so an embedded newline could otherwise
    inject arbitrary additional directives (e.g. a `%post` block that runs as
    root). Values that land inside a shell command line (the enroll `curl`
    invocation) are additionally `shlex.quote`d so spaces/shell metacharacters
    can't break the command. cloud-init is YAML: rather than hand-interpolate
    secrets into YAML text, the mapping is built as a plain dict and emitted
    with `yaml.safe_dump`, which quotes/escapes values correctly regardless of
    their content.
    """
    hostname = str(fields.get("hostname", _DEFAULT_HOSTNAME))
    token = _checked_for_control_chars("token", str(fields.get("token", "")))
    domain_join = _dict_field(fields, "domain_join")
    local_admin = _dict_field(fields, "local_admin")
    domain_user = _checked_for_control_chars("domain_join.user", str(domain_join.get("user", "")))
    domain_password = _checked_for_control_chars(
        "domain_join.password", str(domain_join.get("password", ""))
    )
    admin_password = _checked_for_control_chars(
        "local_admin.password", str(local_admin.get("password", ""))
    )
    enroll_url = "https://control-plane.local/agent/install.sh"
    quoted_token = shlex.quote(token)

    if installer == "kickstart":
        text = f"""#version=RHEL9
network --hostname={hostname}
rootpw --plaintext {admin_password}
user --name={domain_user} --password={domain_password} --plaintext

%post
curl -sSf {enroll_url} | sh -s -- --token {quoted_token}
%end
"""
    elif installer == "preseed":
        text = f"""d-i netcfg/get_hostname string {hostname}
d-i passwd/root-password password {admin_password}
d-i passwd/root-password-again password {admin_password}
d-i passwd/user-fullname string {domain_user}
d-i passwd/user-password password {domain_password}
d-i passwd/user-password-again password {domain_password}

d-i preseed/late_command string \\
    in-target curl -sSf {enroll_url} | in-target sh -s -- --token {quoted_token}
"""
    elif installer == "cloud-init":
        doc: dict[str, Any] = {
            "hostname": hostname,
            "chpasswd": {
                "list": f"root:{admin_password}\n{domain_user}:{domain_password}",
                "expire": False,
            },
            "runcmd": [f"curl -sSf {enroll_url} | sh -s -- --token {quoted_token}"],
        }
        body = yaml.safe_dump(doc, sort_keys=True, default_flow_style=False)
        text = f"#cloud-config\n{body}"
    else:  # pragma: no cover - guarded by render_answer_file before this is called
        raise ProvisioningValidationError(  # i18n-exempt: dead branch, no response or log
            f"unknown linux installer: {installer!r}"
        )

    return text.encode("utf-8")
