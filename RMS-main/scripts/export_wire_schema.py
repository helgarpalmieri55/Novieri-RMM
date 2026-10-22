#!/usr/bin/env python3
"""Export the agent wire-protocol Pydantic models as JSON Schema.

Run from repo root with the backend venv:
    backend/.venv/bin/python scripts/export_wire_schema.py

Output lands in docs/kernel/wire-protocol/ — the contract the Go agent under
`agent/` builds its structs against.

ONE REPOSITORY DOES NOT MAKE THIS AN INTERNAL DETAIL. The agent moved in-tree in
`CLAUDE.md` commit `a32cead`, so both sides can now change in one commit; a
fleet is still always mixed-version, so every change here is still additive, or
versioned, or it breaks agents in the field — and no test in this repository
will tell you which. See docs/kernel/wire-protocol/README.md.
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.core.wire import InstallOutcome  # noqa: E402
from app.jobs.registry import (  # noqa: E402
    AgentUninstallParams,
    AgentUninstallResult,
    AgentCertInstallParams,
    AgentCertInstallResult,
    AgentCertRenewParams,
    AgentCertRenewResult,
    JobCancelParams,
    JobCancelResult,
)
from app.gateway.protocol import (  # noqa: E402
    BackupChunkGrantMsg,
    BackupChunkQueryMsg,
    HealthReportMsg,
    HeartbeatMsg,
    JobEnvelope,
    JobLogMsg,
    JobResultMsg,
    JobStatusMsg,
    SecurityEventsMsg,
)
from app.modules.backup.schema import (  # noqa: E402
    BackupManifest,
    BackupRestoreParams,
    BackupRestoreResult,
    BackupRunParams,
    BackupRunResult,
    FiletreeStream,
    ManifestEntry,
)
from app.modules.deploy.schema import (  # noqa: E402
    AppDeployParams,
    AppDeployResult,
    ScriptRunParams,
    ScriptRunResult,
)
from app.modules.health.jobtypes import (  # noqa: E402
    HealthCheckNowParams,
    HealthPolicyApplyParams,
)
from app.modules.inventory.schema import InventoryDocument  # noqa: E402
from app.modules.ipam.schema import NetSweepDocument, NetSweepParams  # noqa: E402
from app.modules.patch.schema import (  # noqa: E402
    PatchApplyParams,
    PatchApplyResult,
    PatchRebootParams,
    PatchRebootResult,
    PatchScanDocument,
    PatchScanParams,
)
from app.modules.provisioning.schema import (  # noqa: E402
    ProvisionEnrollParams,
    ProvisionEnrollResult,
    ProvisionPrepareEnvelope,
)
from app.modules.agent_update.jobtypes import (  # noqa: E402
    AgentUpdateParams,
    AgentUpdateResult,
)
from app.modules.agent_push.jobtypes import (  # noqa: E402
    AgentPushInstallParams,
    AgentPushInstallResult,
    AgentPushProbeParams,
    AgentPushProbeResult,
)
from app.modules.remote.jobtypes import (  # noqa: E402
    RemoteScreenOpenParams,
    RemoteScreenOpenResult,
    RemoteSessionOpenParams,
)
from app.modules.site_server.schema import SiteWakeParams, SiteWakeResult  # noqa: E402

OUT_DIR = REPO_ROOT / "docs" / "kernel" / "wire-protocol"


def export(out_dir: Path | None = None, *, quiet: bool = False) -> Path:
    """Write every wire model's JSON Schema to `out_dir`, returning it.

    Parameterised so `backend/tests/test_wire_schema_export.py` can regenerate
    into a temp dir and compare bytes with the committed files. Without that,
    the only way to notice a stale export is for a human to re-run the script
    and read the diff -- and the Go agent builds its structs from the committed
    files, so a stale export means the agent is built against a contract the
    server no longer speaks.
    """
    out_dir = Path(out_dir) if out_dir is not None else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    for model in (
        JobEnvelope,
        JobStatusMsg,
        JobLogMsg,
        JobResultMsg,
        HeartbeatMsg,
        SecurityEventsMsg,
        # HealthReportLine is nested in HealthReportMsg's $defs and is not
        # exported separately, matching SecurityEventDoc inside SecurityEventsMsg.
        HealthReportMsg,
        # HealthCheckSpec is nested in HealthPolicyApplyParams's $defs and is
        # not exported separately, matching HealthReportLine above.
        HealthPolicyApplyParams,
        HealthCheckNowParams,
        InventoryDocument,
        # `SweepHost` is nested in NetSweepDocument's $defs and is not exported
        # separately, matching HealthReportLine and SecurityEventDoc above.
        NetSweepParams,
        NetSweepDocument,
        RemoteSessionOpenParams,
        PatchScanDocument,
        PatchApplyResult,
        PatchScanParams,
        PatchApplyParams,
        # A new job type: an agent without it refuses `unknown_type`.
        PatchRebootParams,
        PatchRebootResult,
        InstallOutcome,
        AppDeployParams,
        AppDeployResult,
        ScriptRunParams,
        ScriptRunResult,
        BackupRunParams,
        BackupRunResult,
        BackupRestoreParams,
        BackupRestoreResult,
        ManifestEntry,
        FiletreeStream,
        BackupManifest,
        # The server-side dedup exchange (docs/modules/backup/spec.md §5.2).
        # Additive in both directions: an older control plane rejects
        # `backup_chunk_query` at `parse_agent_message`, and an older agent
        # never sends one so is never sent a `backup_chunk_grant`.
        # `BackupUploadGrant` is nested in `BackupChunkGrantMsg`'s $defs and is
        # not exported separately, matching `HealthReportLine` above.
        BackupChunkQueryMsg,
        BackupChunkGrantMsg,
        ProvisionEnrollParams,
        ProvisionEnrollResult,
        ProvisionPrepareEnvelope,
        # Automatic certificate renewal (docs/modules/agent/cert-renewal.md §1).
        # Additive in both directions: an old agent refuses the job type
        # unknown_type before anything runs, and an old control plane never
        # dispatches either leg.
        AgentCertRenewParams,
        AgentCertRenewResult,
        AgentCertInstallParams,
        AgentCertInstallResult,
        # Interruption (2026-08-22-job-lifecycle-design.md §3.1). Additive in
        # both directions, and the analysis is in this directory's README:
        # `job.cancel` is a new JOB TYPE, not a new wire `kind`, so an agent
        # that predates it refuses `unknown_type` LOUDLY -- an unknown kind
        # would be dropped in silence, which is the worst property a cancel
        # could have. Nothing an old agent sends is rejected and no field is
        # required of it.
        JobCancelParams,
        JobCancelResult,
        # Site servers (docs/modules/site-server/spec.md §5.2, §5.3). NO GO
        # CAPABILITY EXISTS for this job type yet -- the export IS the
        # deliverable, and it is a compatibility contract rather than an
        # internal detail even though both sides now live in one tree.
        SiteWakeParams,
        SiteWakeResult,
        # Agent push install, cycle B2 (docs/modules/agent-distribution/
        # spec-push-install.md §4). Additive in both directions and the same
        # compatibility decision as `job.cancel`: these are new JOB TYPES, not
        # new wire kinds, so an agent that predates the `pushinstall` capability
        # refuses the type `unknown_type` LOUDLY before anything runs -- an
        # unknown kind would be dropped in silence. `AgentPushInstallParams`
        # carries the decrypted credential and the confirmed host key; that is
        # the signed contract the pushing site server's capability unmarshals
        # (invariant #3's residual is bounded by the redactor and the terminal
        # scrubber on the SERVER side, §6.4 -- the wire schema documents the
        # shape, it does not weaken it). `PushCredential` and `PushRelease` are
        # nested in `$defs` and not exported separately, matching
        # `HealthReportLine` above.
        AgentPushProbeParams,
        AgentPushProbeResult,
        AgentPushInstallParams,
        AgentPushInstallResult,
        # Screen share (docs/modules/remote-control/spec-screen-share.md §6).
        # A new job type, so an agent without the capability refuses it
        # `unknown_type`. The media itself is rsp/1, which is not JSON: see
        # remote-screen-protocol.md in this directory. `ScreenConsent` is
        # nested in `$defs` and not exported separately.
        RemoteScreenOpenParams,
        RemoteScreenOpenResult,
        # Signed self-update (spec-self-update.md §3). A new job TYPE: an old
        # agent refuses it `unknown_type` before anything runs, and an old
        # control plane never dispatches it.
        AgentUpdateParams,
        AgentUpdateResult,
        # Reject (install-link spec 2026-09-28 §1.5, §4). A new job TYPE: an
        # agent older than 1.6.0 refuses it `unknown_type` before anything
        # runs, and the control plane never sends it one -- `device_review`
        # dispatches only to an agent that advertised the capability.
        AgentUninstallParams,
        AgentUninstallResult,
    ):
        path = out_dir / f"{model.__name__}.schema.json"
        path.write_text(json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n")
        if not quiet:
            print(f"wrote {path}")
    return out_dir


def main() -> None:
    export(Path(sys.argv[1]) if len(sys.argv) > 1 else None)


if __name__ == "__main__":
    main()
