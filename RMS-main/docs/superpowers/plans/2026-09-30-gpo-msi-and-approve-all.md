# GPO MSI and Approve all — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An administrator downloads one MSI per organisation and install link, assigns it through Group Policy software installation, every computer installs the agent at startup and joins *Waiting for review* through the install link, and one **Approve all** approves the list they are shown.

**Architecture:** The console POSTs the link code and an architecture to `POST /api/v1/install-link/msi` (`enrollment.link.manage`). `app/modules/agent_distribution/msi.py` checks the code against the link's stored hash, picks the stable release-signed Windows agent (≥ 1.7.0), reads its bytes from object storage, re-hashes them against `agent_releases.sha256`, verifies the offline release signature against the new `ITS_AGENT_RELEASE_PUBLIC_KEY`, and hands them to `app/services/msi_build.py`, which writes a fixed WiX source into a private temporary directory, runs `wixl` (and `msibuild` for ARM64), reads the result back with `msiinfo` and refuses anything but what it wrote. The MSI is streamed to the administrator, audited as `install_link.msi_downloaded` (never the code) and never stored. On the computer, Windows Installer runs `its-agent.exe --msi-install` as SYSTEM, which reads `msi-enroll.json`, runs the link installer's own directory/token/service steps and deletes the file; removal runs `its-agent.exe --msi-uninstall`, the `agent.uninstall` removal without the binary. `POST /api/v1/devices/review/approve-all` (`devices.review`) approves the listed ids that are still pending, one audit record each under a shared `bulk_id`.

**Tech Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.x · Pydantic v2 · `cryptography` (Ed25519, already a dependency) · msitools (`wixl`, `msibuild`, `msiinfo`; Debian/Ubuntu package `wixl` + `msitools`, MSYS2 `mingw-w64-ucrt-x86_64-msitools` on the Windows runner) · Go 1.24 (`golang.org/x/sys/windows`, `svc`, `svc/mgr`, `registry` — already required) · React + TypeScript (strict) · TanStack Query · Vitest.

**Spec:** `docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md` — **Part 2 only**: §2.1–§2.6 and the Part 2 half of *Testing*. Part 1 (subnet push, `push_run`, migration `0200`) is planned separately and is not in this plan.

**Migrations: none.** Part 2 adds no table and no column. Part 1 plans `0200`; if a later revision of *this* plan ever needs one, it is `0210` with `down_revision = "0200"`.

**Merge order with Part 1** (`docs/superpowers/plans/2026-09-30-subnet-push-runs.md`). Both parts touch `frontend/src/lib/api.ts` (Part 1 adds methods after `requestPush`; this plan after `regenerateInstallLink` and before `listAssetCandidates`) and `frontend/src/modules/devices/DevicesPage.tsx` (Part 1: imports, page-head actions, the push flow mount; this plan: one import after `AddAssetDialog`, one state, the Approve all bar before `{reviewNotice ? (`). The edits are additive at different anchors; whichever merges second rebases, resolves any adjacent-import conflict by keeping both lines, and re-runs Task 11.

## Global Constraints

- **CLAUDE.md invariants hold unchanged; Part 2 changes no security rule (spec §0: "Part 2 … has no security-rule change").** 1: nothing here creates, signs or runs a job; the MSI's custom actions are Windows Installer running the platform's own published binary on the machine it was installed on. 2: the agent still connects outbound only; the control plane opens no connection to any computer. 3: no credential is stored; the link code stays hashed server-side. 4: every MSI download (`install_link.msi_downloaded`) and every approval (`device.review_approved`) is audited. 5: every query is `org_session`-scoped with an explicit `org_id` predicate. 6: `enrollment.link.manage` and `devices.review` are checked server-side; the console only hides.
- **The link code is never in argv and never logged.** It reaches the server in a POST **body** (never a URL); reaches `msi_build.py` as a function argument and leaves it only as the contents of `msi-enroll.json` in a 0700 temporary directory; no tool (`wixl`, `msibuild`, `msiinfo`) is given it as an argument or in its environment; the MSI's custom action command lines are the constants `--msi-install` and `--msi-uninstall`; no exception message, log line or audit record contains it.
- **The MSI is built per request and never stored.** Built in `tempfile.TemporaryDirectory(prefix="itms-msi-")`, removed on the way out, bounded to **60 s** (`BUILD_TIMEOUT_S`), at most **2** concurrent builds per process (`MAX_CONCURRENT_BUILDS`). No object-store write, no cache, `Cache-Control: no-store`.
- **It wraps only the published binary, verified.** The bytes are read from object storage by `(version, "windows", arch)`; their SHA-256 must equal `agent_releases.sha256`; the release must carry a valid offline release signature over `its-agent-release/v1\n<version>\nwindows\n<arch>\n<sha256>\n` under `ITS_AGENT_RELEASE_PUBLIC_KEY`. Unset or malformed key: **503**, nothing built. Wrong bytes or signature: **409**, nothing built, nothing audited.
- **The audit record never contains the code.** `install_link.msi_downloaded`, target `install_link` / `str(org_id)`, metadata exactly `{"arch", "version", "msi_sha256", "agent_sha256"}`.
- **Exact names.** Route `POST /api/v1/install-link/msi`, body `{"code": str, "arch": "amd64" | "arm64"}`, response `application/x-msi` with `Content-Disposition: attachment; filename="ITMS-Agent-<host>[@<port>]-<arch>.msi"` and `X-Content-SHA256: <hex>`. Route `POST /api/v1/devices/review/approve-all`, body `{"device_ids": [uuid], "list_digest": "<64 hex>"}`, response `{"bulk_id", "approved", "not_pending", "still_waiting"}`. Agent arguments `--msi-install`, `--msi-uninstall`. Settings file `msi-enroll.json` = `{"console":"https://<host>[:<port>]","code":"<32 URL-safe>"}`. Setting `ITS_AGENT_RELEASE_PUBLIC_KEY` (`Settings.agent_release_public_key`).
- **MSI shape (verified with wixl 0.103; production image gets 0.106).** `ProgramFiles64Folder\ITSuite\{its-agent.exe, msi-enroll.json}` in one component (KeyPath `its-agent.exe`); `ItsMsiInstall` type **3090** (18 + 1024 deferred + 2048 no-impersonate), sequence **4010**, condition `NOT REMOVE~="ALL"`; `ItsMsiUninstall` type **3154** (3090 + 64 ignore exit code), sequence **3490**, condition `REMOVE~="ALL" AND NOT UPGRADINGPRODUCTCODE`; ServiceControl `its-agent` Stop on install and removal (event **34**); `MajorUpgrade AllowSameVersionUpgrades="yes"`; `ARPNOMODIFY` and `ARPNOREPAIR`; `UpgradeCode = uuid5(22ad0fe1-20ac-49a3-938c-25347fa54ecb, str(org_id))`; `ProductVersion = MAJOR.MINOR.PATCH` of the agent; Summary Template `x64;1033` (amd64) or `Arm64;1033` (arm64). Not Authenticode-signed.
- **Versions.** The MSI wraps only a stable, release-signed Windows agent **≥ 1.7.0** (`MSI_MIN_AGENT_VERSION`). The link page's own floor (`LINK_MIN_AGENT_VERSION = 1.6.0`) is unchanged.
- **Approve all.** At most **1,000** ids per call (`APPROVE_ALL_MAX`); approves only listed ids still `pending`; one `device.review_approved` per machine with metadata `bulk_id`; `list_digest = sha256("\n".join(sorted(set(str(id)))))`, hex; mismatch **422**, nothing changes.
- **Agent idle budget.** `--msi-install` and `--msi-uninstall` add nothing to the idle service: `cmd/agent` routes them by comparing constants; `internal/install` and `capabilities/uninstall` gain **no import** (their pinned import lists, `imports_test.go` in each, are unchanged — that unchanged list *is* the proof) and no package-level value initialised by a call (`dormant_test.go`, which now names the three new files).
- **`backend/app/modules/agent_distribution/**` must not contain** `paramiko`, `asyncssh`, `fabric`, `pywinrm`, `winrm`, `smbprotocol`, `impacket`, `pypsrp`, `socket.create_connection`, `asyncio.open_connection`, `subprocess` — anywhere, docstrings included (`tests/test_agent_distribution_opens_no_connection.py` is a case-sensitive substring match, so ordinary words count too: "fabricated" contains `fabric`, and a lowercase "winrm" anywhere fails it).
- **`backend/app/services/msi_build.py` imports only the standard library** and runs as a script (`python backend/app/services/msi_build.py …`), because the Windows e2e runs it that way.
- **Tests first.** Every task writes its failing test, watches it fail, then implements. Every commit message ends with exactly these two lines:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89`
- **Frontend:** strict TypeScript, no `any`; no wire name in primary copy (`src/lib/noWireNames.test.ts`); every `ApiClient` method a routed page calls has a fixture (harmony `--strict-fixtures`).
- **Verification commands** (the relevant ones at the end of each task, all of them in Task 11):
  - backend, from `backend/`: `.venv/bin/ruff check app tests`, `.venv/bin/mypy app`, `ITS_TEST_REQUIRE_MSITOOLS=1 .venv/bin/python -m pytest -q`
  - agent, from `agent/`: `test -z "$(gofmt -l .)"`, `go vet ./...`, `GOOS=windows GOARCH=amd64 go vet ./...`, `GOOS=windows GOARCH=arm64 go vet ./...`, `GOOS=darwin go vet ./...`, `go test ./... -count=1`, `"$(go env GOPATH)/bin/staticcheck" ./...` (staticcheck **2025.1.1**: `go install honnef.co/go/tools/cmd/staticcheck@2025.1.1`), and ci.yml's idle-budget step (Task 6 Step 8 gives it verbatim)
  - frontend, from `frontend/`: `npx tsc --noEmit`, `npx vitest run`, `npm run build`, `node scripts/harmony.mjs --routes /agent-packages,/devices --strict-fixtures`

## What was settled before this plan was written (the spec's §2.2 risk)

msitools was installed on the planning host (`sudo apt-get install -y msitools wixl`, Ubuntu 24.04, **msitools/wixl 0.103**) and the exact source in Task 1 was built and read back with `msiinfo export`. Findings, all reflected in Task 1:

1. **wixl expresses the deferred, no-impersonate `ExeCommand` custom action.** `<CustomAction FileKey="its_agent_exe" ExeCommand="--msi-install" Execute="deferred" Impersonate="no" Return="check"/>` comes out as `ItsMsiInstall	3090	its_agent_exe	--msi-install` (18 + 1024 + 2048), and `Return="ignore"` as 3154. **No `msibuild` fallback is needed for the custom action.** Upstream NEWS lists "Support all CustomAction Execute types properly" in 0.102, so 0.101 (Debian bookworm) would not do; `python:3.12-slim` is Debian **trixie** today (its digest equals `3.12-slim-trixie`), which ships **0.106**.
2. **wixl's `Before=`/`After=` scheduling is not reliable.** `Before="RemoveFiles"` produced sequence **1402** — *before* `InstallInitialize` (1500), where a deferred action fails with error 2762 (an earlier build of a slightly different source put it at 2001). Explicit `Sequence="3490"` / `Sequence="4010"` land exactly, and `verify_msi` re-checks the placement on every build.
3. **wixl 0.103–0.106 has no `arm64` architecture** (`-a arm64` → "arch of type 'arm64' is not supported"; 0.106's `builder.vala` knows intel/ia64/x64 only). The ARM64 MSI is built as x64 and its Summary Template rewritten with `msibuild agent.msi -s "ITMS Agent" Toeshee "Arm64;1033"`, read back as `Template: Arm64;1033`. **ARM64 installation is not exercised by any CI job** (the e2e runner is `windows-latest`, x64); it is proven only by the table read-back.
4. **Debian and Ubuntu package `wixl` separately from `msitools`.** `apt-get install msitools` alone gives `msibuild`/`msiinfo` and no `wixl`. The Dockerfile and CI install both.
5. `msibuild <msi> -q "UPDATE …"` works, which is how Task 1's negative test tampers with a built MSI.
6. MSYS2 ships `mingw-w64-ucrt-x86_64-msitools` **0.106-4** with `wixl.exe`, `msibuild.exe`, `msiinfo.exe` — the Windows e2e builds with that (Task 7).

## Decisions this plan takes where the spec is silent or disagrees with the code

Recorded so a reviewer can reject one without re-reading the plan.

- **D1 — The server does not have the link code; the administrator supplies it.** Spec §2.2 has `build_msi(release, console, code)` served under `enrollment.link.manage` as if the server could produce the code. It cannot: `app/services/install_link.py` stores only `sha256(code)`, and the full link is returned once (`InstallLinkSaved.url`). So the route is **`POST /api/v1/install-link/msi` with the code in the body**; the server checks it against the stored hash (`install_link.code_state`) and refuses a code that is not the current one. The console pre-fills it from the link on screen right after it was generated, and otherwise asks the administrator to paste the link they saved. POST, not GET: a code in a URL lands in access logs.
- **D2 — The wixl-running code lives in `app/services/msi_build.py`.** `tests/test_agent_distribution_opens_no_connection.py` forbids the substring `subprocess` anywhere under `app/modules/agent_distribution/`. Running a local tool opens no connection, but the guard is a text match and stays exact; `agent_distribution/msi.py` keeps the spec's `build_msi` and all the checks, and calls the service.
- **D3 — The MSI's agent floor is 1.7.0, not 1.6.0.** `agent-v1.6.0` is already tagged (`git ls-remote --tags origin 'agent-v*'` → `fe705b0… refs/tags/agent-v1.6.0`) and has no `--msi-install`: its custom action would exit 2 ("flag provided but not defined") and every install would roll back. `MSI_MIN_AGENT_VERSION = 1.7.0`; Appendix B cuts 1.7.0. If a 1.7.x tag exists by the time this merges, raise the constant to the first version built from this branch and change the tests that name `1.7.0` with it.
- **D4 — The server gains the release public key.** Spec §2.2 says the builder "verifies its release signature"; the control plane has never held the release key (only agents have it, compiled in). New setting `ITS_AGENT_RELEASE_PUBLIC_KEY` (not a secret: `release-agent.yml` publishes it as `release-signing-key.pub` and in every release's notes). Unset → 503. A shared vector, `docs/kernel/wire-protocol/testdata/release-signature.json`, is verified by both the Go verifier and the Python one so the message format cannot drift apart.
- **D5 — "Already enrolled changes nothing and exits 0" is refined.** The MSI stops the service before touching files (D7), so on an upgrade or re-assignment `--msi-install` must start it again; and `msi-enroll.json` holds the code and must not stay in Program Files. So on an enrolled machine it deletes `msi-enroll.json`, starts the service if it is stopped, and changes nothing else (identity, token, directories and service configuration untouched). "Enrolled" means `identity.json` present **and** the service registered: an identity alone (a leftover, or one a standard user planted under an `ITSuite` they created) takes the full path, whose fresh-install ownership checks refuse a planted directory.
- **D6 — `--msi-install` does not stage or verify the binary.** The link installer stages a copy and verifies it against `/i/<code>/windows.sig`; here the MSI has already placed the binary at `%ProgramFiles%\ITSuite\its-agent.exe`, which is this very process (it cannot be renamed over), and a binary checking its own bytes proves nothing. The server verified the bytes before wrapping them (D4). `apply()` is split into `writeToken` and `registerAndStart`, shared by `apply` and the new `applyInPlace`, so the directories-parent-first, `O_EXCL` token and service steps are literally the same code.
- **D7 — The MSI stops the service on install and on removal** (`ServiceControl Stop="both"`), so an upgrade or removal never writes over a running image. Spec silent.
- **D8 — The removal custom action runs only on a real removal and never fails the removal.** Condition `REMOVE~="ALL" AND NOT UPGRADINGPRODUCTCODE`: a major upgrade removes the older product first, and running `agent.uninstall` then would un-enrol every machine on every MSI refresh. `Return="ignore"`: a removal action that can fail makes the product impossible to remove through Group Policy. It does not delete the binary or `%ProgramFiles%\ITSuite` — Windows Installer owns those and removes them right after.
- **D9 — Audit names.** The MSI download uses the spec's `install_link.msi_downloaded`. Approve all writes the **existing** action `device.review_approved` (spec §2.5 wrote `device.approved`; the code and its tests have used `device.review_approved` since 2026-09-28), adding `bulk_id` to its metadata.
- **D10 — Approve all reads its list through the existing *Waiting for review* query**, `GET /api/v1/devices?state=review` (500 per page, pending rows only, at most 1,000), rather than a new GET route (which would also move `tests/test_ai_tool_surface.py`'s route counts that Part 1 is changing too). The digest is computed in the browser (`crypto.subtle`) over the ids it shows, and recomputed by the server over the ids it receives.
- **D11 — The SHA-256 is shown after the download, not beside the button before it.** Each build has a fresh ProductCode and package code, so every download is a different file; the console shows the `X-Content-SHA256` of the file just saved, which is also the value in the audit record.
- **D12 — The Windows e2e builds the MSI with the server's own builder**, run as a script (`msi_build.py` is stdlib-only) with MSYS2's `wixl.exe`, then installs it with `msiexec` as SYSTEM from a scheduled task. `agent-update-e2e.yml`'s path filter gains the builder and the distribution module.
- **D13 — The rule the MSI overturns is amended where it lives.** `agent/packaging/README.md` row 3 ("A distributable artifact … The install link is the one recorded exception, and it puts a code in a file's NAME, never in its bytes") and its "### No MSI" section, and the distribution spec's §2 and §7, are rewritten to record the MSI as the second exception with the §2.6 residuals — the same treatment the 2026-09-28 decision got (`tests/test_install_link_docs.py`).
- **D14 — `build_msi` takes three more keyword arguments than the spec's signature**: `exe` (the bytes, read by the async caller), `org_id` (for the UpgradeCode) and `key` (the parsed public key). The spec's three positional parameters are unchanged.

## File map

**Backend — create**
- `backend/app/services/msi_build.py` — WiX source, wixl/msibuild/msiinfo, read-back verification, script entry point (stdlib only).
- `backend/app/modules/agent_distribution/msi.py` — release-signature check, `build_msi`, `msi_for_link` (link, console, release, key, bytes, audit).
- `backend/tests/test_msi_build.py`, `backend/tests/msi_fixtures.py`, `backend/tests/test_release_signature_vector.py`, `backend/tests/test_install_link_msi.py`, `backend/tests/test_device_review_approve_all.py`.
- `docs/kernel/wire-protocol/testdata/release-signature.json`.

**Backend — modify**
- `backend/app/core/config.py` (`agent_release_public_key`), `backend/app/core/storage.py` (`get_agent_release`), `backend/app/services/install_link.py` (`code_state`), `backend/app/api/v1/install_link.py` (`POST /install-link/msi`), `backend/app/services/device_review.py` (`_approve_row`, `review_list_digest`, `approve_all`), `backend/app/api/v1/devices.py` (`POST /devices/review/approve-all`), `backend/tests/test_storage.py`, `backend/tests/test_install_link_docs.py`.
- `deploy/backend.Dockerfile`, `deploy/.env.example`, `.github/workflows/ci.yml`.

**Agent — create**
- `agent/internal/install/{msi.go,msi_windows.go,msi_other.go,msi_test.go,msi_windows_test.go}`.
- `agent/internal/capabilities/uninstall/{msi.go,msi_windows.go,msi_other.go,msi_test.go}`.
- `agent/internal/update/releasesig_vector_test.go`.
- `agent/internal/update/e2e/msi_install_windows_test.go`.

**Agent — modify**
- `agent/internal/install/install_windows.go` (`writeToken`, `registerAndStart`, `applyInPlace`), `agent/internal/install/dormant_test.go`, `agent/internal/capabilities/uninstall/helper.go` (`removeServiceAndData`), `agent/cmd/agent/main.go`, `agent/cmd/agent/main_test.go`, `.github/workflows/agent-update-e2e.yml`.

**Frontend — create**
- `frontend/src/modules/agent-releases/MsiDownload.tsx` + `.test.tsx`.
- `frontend/src/modules/devices/ApproveAllDialog.tsx`, `frontend/src/modules/devices/DevicesApproveAll.test.tsx`.

**Frontend — modify**
- `frontend/src/lib/api.ts`, `frontend/src/lib/api.test.ts`, `frontend/src/modules/agent-releases/InstallLinkPanel.tsx`, `frontend/src/modules/agent-releases/fixtures.ts`, `frontend/src/modules/devices/DevicesPage.tsx`, `frontend/src/fixtures/core.ts`.

**Docs — modify**
- `agent/packaging/README.md`, `docs/modules/agent-distribution/spec.md` (§2, §7), `deploy/WINDOWS-PUSH.md` (new §7).

---

### Task 1: The MSI builder, and the proof that wixl can express the custom action

This is the spec's "risk to settle first". It was settled while planning (see *What was settled*); this task turns that into a test that fails if a future wixl disagrees, and the builder the rest of the plan calls.

**Files:**
- Create: `backend/app/services/msi_build.py`
- Create: `backend/tests/test_msi_build.py`
- Modify: `deploy/backend.Dockerfile` (after `ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1`)
- Modify: `.github/workflows/ci.yml` (job `backend`: a step before `pytest`, and the `pytest` step's `env`)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces (all in `app.services.msi_build`):
  - `class MsiBuildError(RuntimeError)` — message safe to show, never contains the code.
  - `build_agent_msi(*, exe: bytes, console: str, code: str, org_id: uuid.UUID, version: str, arch: str, timeout_s: float = BUILD_TIMEOUT_S) -> bytes`
  - `verify_msi(msi: Path, *, arch: str, deadline: float | None = None) -> None`
  - `product_version(version: str) -> str`, `settings_json(console: str, code: str) -> bytes`, `render_wxs(*, upgrade: uuid.UUID, version: str) -> str`, `upgrade_code(org_id: uuid.UUID) -> uuid.UUID`
  - constants `BUILD_TIMEOUT_S = 60.0`, `TEMPLATES = {"amd64": "x64;1033", "arm64": "Arm64;1033"}`, `UPGRADE_NAMESPACE = uuid.UUID("22ad0fe1-20ac-49a3-938c-25347fa54ecb")`
  - script: `python backend/app/services/msi_build.py --exe F --console URL --code-file F --org UUID --version V --arch amd64|arm64 --out F` → prints the MSI's SHA-256, exit 0; `msi_build: <reason>` on stderr, exit 1.

- [ ] **Step 1: Install msitools on the development host**

Run: `sudo apt-get update && sudo apt-get install -y wixl msitools && wixl --version && command -v msibuild msiinfo msiextract`
Expected: a version of `0.103` or later, then three paths. (`msitools` alone has no `wixl`; both packages are needed.)

- [ ] **Step 2: Write the failing tests**

`backend/tests/test_msi_build.py`:

```python
"""The Group Policy MSI builder (GPO MSI spec 2026-09-30 §2.2) -- and the proof
the spec asked for first: wixl CAN express a deferred, no-impersonate
custom action, read back from the built file with msiinfo.

In CI, ITS_TEST_REQUIRE_MSITOOLS=1 turns a missing tool into a failure; on a
developer machine without msitools the tool-dependent tests skip."""

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.services import msi_build
from app.services.msi_build import MsiBuildError, build_agent_msi, product_version, settings_json

ORG = uuid.UUID("7d0c4c2e-5b1f-4a8e-9f62-3c1d2e4b5a69")
CODE = "AbCdEfGhIjKlMnOpQrStUvWxYz012_-9"
CONSOLE = "https://itms.example.com"
EXE = b"MZ" + bytes(range(256)) * 64
REPO = Path(__file__).resolve().parents[2]


def _need_msitools() -> None:
    missing = [t for t in ("wixl", "msibuild", "msiinfo", "msiextract") if shutil.which(t) is None]
    if missing:
        if os.environ.get("ITS_TEST_REQUIRE_MSITOOLS") == "1":
            pytest.fail(f"ITS_TEST_REQUIRE_MSITOOLS=1 but {missing} are not on PATH")
        pytest.skip(f"msitools is not installed ({missing})")


def _build(
    tmp_path: Path, *, arch: str = "amd64", org: uuid.UUID = ORG, name: str = "agent"
) -> Path:
    _need_msitools()
    data = build_agent_msi(
        exe=EXE, console=CONSOLE, code=CODE, org_id=org, version="1.7.0", arch=arch
    )
    out = tmp_path / f"{name}-{arch}.msi"
    out.write_bytes(data)
    return out


def _table(msi: Path, table: str) -> list[list[str]]:
    out = subprocess.run(
        ["msiinfo", "export", str(msi), table], capture_output=True, text=True, check=True
    ).stdout
    return [line.split("\t") for line in out.splitlines()[3:] if line]


def _template(msi: Path) -> str:
    out = subprocess.run(
        ["msiinfo", "suminfo", str(msi)], capture_output=True, text=True, check=True
    ).stdout
    return next(
        line.split(":", 1)[1].strip() for line in out.splitlines() if line.startswith("Template:")
    )


def test_wixl_expresses_a_deferred_no_impersonate_custom_action(tmp_path: Path) -> None:
    rows = {tuple(row[:4]) for row in _table(_build(tmp_path), "CustomAction")}
    assert rows == {
        ("ItsMsiInstall", "3090", "its_agent_exe", "--msi-install"),
        ("ItsMsiUninstall", "3154", "its_agent_exe", "--msi-uninstall"),
    }
    # 18: run an installed file. 1024: deferred. 2048: no impersonation (SYSTEM).
    # 64: exit code ignored -- on the removal action only.
    assert 3090 & 0x3F == 18 and 3090 & 1024 and 3090 & 2048 and not 3090 & 64
    assert 3154 & 0x3F == 18 and 3154 & 1024 and 3154 & 2048 and 3154 & 64


def test_both_actions_run_inside_the_install_script_where_they_must(tmp_path: Path) -> None:
    seq = {
        row[0]: (row[1], int(row[2])) for row in _table(_build(tmp_path), "InstallExecuteSequence")
    }
    assert seq["ItsMsiInstall"] == ('NOT REMOVE~="ALL"', 4010)
    assert seq["ItsMsiUninstall"] == ('REMOVE~="ALL" AND NOT UPGRADINGPRODUCTCODE', 3490)
    assert (
        seq["InstallInitialize"][1]
        < 3490
        < seq["RemoveFiles"][1]
        < seq["InstallFiles"][1]
        < 4010
        < seq["InstallFinalize"][1]
    )
    assert seq["StopServices"][1] < 3490


def test_it_holds_exactly_the_agent_and_its_settings_file(tmp_path: Path) -> None:
    msi = _build(tmp_path)
    assert sorted(row[2] for row in _table(msi, "File")) == ["its-agent.exe", "msi-enroll.json"]
    out = tmp_path / "extracted"
    subprocess.run(["msiextract", "-C", str(out), str(msi)], check=True, capture_output=True)
    assert (out / "ITSuite" / "its-agent.exe").read_bytes() == EXE
    assert json.loads((out / "ITSuite" / "msi-enroll.json").read_text()) == {
        "console": CONSOLE,
        "code": CODE,
    }
    dirs = {row[0]: (row[1], row[2]) for row in _table(msi, "Directory")}
    assert dirs["INSTALLFOLDER"] == ("ProgramFiles64Folder", "ITSuite")
    assert _table(msi, "ServiceControl") == [["StopAgent", "its-agent", "34", "", "1", "Agent"]]


def test_the_upgrade_code_is_the_organisations_and_the_version_the_agents(tmp_path: Path) -> None:
    first = {row[0]: row[1] for row in _table(_build(tmp_path, name="a"), "Property")}
    again = {row[0]: row[1] for row in _table(_build(tmp_path, name="b"), "Property")}
    other = {
        row[0]: row[1] for row in _table(_build(tmp_path, name="c", org=uuid.uuid4()), "Property")
    }
    expected = "{" + str(uuid.uuid5(msi_build.UPGRADE_NAMESPACE, str(ORG))).upper() + "}"
    assert first["UpgradeCode"] == again["UpgradeCode"] == expected
    assert other["UpgradeCode"] != expected
    assert first["ProductCode"] != again["ProductCode"]
    assert first["ProductVersion"] == "1.7.0"


def test_the_arm64_msi_is_marked_arm64(tmp_path: Path) -> None:
    assert _template(_build(tmp_path, arch="amd64")) == "x64;1033"
    assert _template(_build(tmp_path, arch="arm64")) == "Arm64;1033"


def test_a_built_msi_whose_actions_were_changed_is_refused(tmp_path: Path) -> None:
    msi = _build(tmp_path)
    subprocess.run(
        [
            "msibuild",
            str(msi),
            "-q",
            "UPDATE `CustomAction` SET `Type` = 1042 WHERE `Action` = 'ItsMsiInstall'",
        ],
        check=True,
        capture_output=True,
    )
    with pytest.raises(MsiBuildError, match="custom actions"):
        msi_build.verify_msi(msi, arch="amd64")


@pytest.mark.parametrize(
    "version", ["1.7", "256.0.0", "1.256.0", "1.0.65536", "latest", "1.7.0+build"]
)
def test_a_version_windows_installer_cannot_hold_is_refused(version: str) -> None:
    with pytest.raises(MsiBuildError):
        product_version(version)


def test_a_prerelease_suffix_is_dropped() -> None:
    assert product_version("0.0.1-e2e.1") == "0.0.1"
    assert product_version("1.7.0") == "1.7.0"


@pytest.mark.parametrize(
    "console",
    [
        "http://itms.example.com",
        "https://itms.example.com/",
        "https://itms.example.com/i/x",
        "https://user@itms.example.com",
        "https://itms.example.com:0",
        'https://itms"example.com',
        "https://",
    ],
)
def test_the_settings_refuse_anything_but_a_bare_https_origin(console: str) -> None:
    with pytest.raises(MsiBuildError, match="bare https origin"):
        settings_json(console, CODE)


@pytest.mark.parametrize("code", ["short", CODE + "x", CODE[:-1] + "!", CODE[:-1] + "\n"])
def test_the_settings_refuse_a_code_that_is_not_one_and_do_not_repeat_it(code: str) -> None:
    with pytest.raises(MsiBuildError) as refused:
        settings_json(CONSOLE, code)
    assert code not in str(refused.value)


def test_an_unknown_architecture_is_refused_before_any_tool_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(msi_build.shutil, "which", lambda name: pytest.fail(f"looked for {name}"))
    with pytest.raises(MsiBuildError, match="x86"):
        build_agent_msi(
            exe=EXE, console=CONSOLE, code=CODE, org_id=ORG, version="1.7.0", arch="x86"
        )


def test_no_tool_sees_the_code_or_this_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _need_msitools()
    monkeypatch.setenv("ITS_DATABASE_URL", "postgresql://its:secret@db/itsuite")
    seen: list[tuple[list[str], dict[str, str], Path, int]] = []
    real = subprocess.run

    def spy(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        cwd = Path(kw["cwd"])
        seen.append((list(argv), dict(kw["env"]), cwd, cwd.stat().st_mode & 0o777))
        return real(argv, **kw)

    monkeypatch.setattr(msi_build.subprocess, "run", spy)
    build_agent_msi(exe=EXE, console=CONSOLE, code=CODE, org_id=ORG, version="1.7.0", arch="arm64")
    assert [Path(argv[0]).name for argv, *_ in seen][:2] == ["wixl", "msibuild"]
    for argv, env, cwd, mode in seen:
        assert not any(CODE in arg for arg in argv)
        assert CODE not in "".join(env.values())
        assert "ITS_DATABASE_URL" not in env
        assert mode == 0o700
        assert not cwd.exists()


def test_a_build_that_overruns_is_refused_and_leaves_nothing_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(msi_build.shutil, "which", lambda name: f"/usr/bin/{name}")
    dirs: list[Path] = []

    def slow(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        dirs.append(Path(kw["cwd"]))
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(msi_build.subprocess, "run", slow)
    with pytest.raises(MsiBuildError, match="did not finish"):
        build_agent_msi(
            exe=EXE, console=CONSOLE, code=CODE, org_id=ORG, version="1.7.0", arch="amd64"
        )
    assert dirs and not dirs[0].exists()


def test_the_builder_imports_only_the_standard_library() -> None:
    """The Windows e2e runs this file as a script, outside the backend's venv."""
    tree = ast.parse(Path(msi_build.__file__).read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add((node.module or "").split(".")[0])
    assert modules - {"__future__"} <= set(sys.stdlib_module_names), modules


def test_the_builder_runs_as_a_script_with_the_code_in_a_file(tmp_path: Path) -> None:
    _need_msitools()
    exe = tmp_path / "its-agent.exe"
    exe.write_bytes(EXE)
    code_file = tmp_path / "code"
    code_file.write_text(CODE)
    out = tmp_path / "x.msi"
    done = subprocess.run(
        [
            sys.executable,
            msi_build.__file__,
            "--exe",
            str(exe),
            "--console",
            CONSOLE,
            "--code-file",
            str(code_file),
            "--org",
            str(ORG),
            "--version",
            "1.7.0",
            "--arch",
            "amd64",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == hashlib.sha256(out.read_bytes()).hexdigest()
    assert CODE not in done.stdout + done.stderr


def test_the_api_image_carries_msitools() -> None:
    """The MSI is built inside the api container (spec §2.2 item 4)."""
    dockerfile = (REPO / "deploy" / "backend.Dockerfile").read_text()
    assert "apt-get install -y --no-install-recommends wixl msitools" in dockerfile
    # Installed as root, before the image drops to the unprivileged user.
    assert dockerfile.index("wixl msitools") < dockerfile.index("USER its")
```

- [ ] **Step 3: Run them to see them fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_msi_build.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'app.services.msi_build'`.

- [ ] **Step 4: Write the builder**

`backend/app/services/msi_build.py`:

```python
"""Build the Group Policy MSI: the published agent, its settings file and two
custom actions (docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md
§2.2).

STANDARD LIBRARY ONLY, AND RUNNABLE AS A SCRIPT. The Windows e2e
(`.github/workflows/agent-update-e2e.yml`, `TestMsiInstallWindows`) runs this
file directly -- `python backend/app/services/msi_build.py ...` -- with MSYS2's
wixl, so the MSI it installs comes from this template and nothing else.
`tests/test_msi_build.py` pins the rule.

WHY THIS IS NOT UNDER `app/modules/agent_distribution/`. Building runs three
local msitools programs, and that package's guard
(`tests/test_agent_distribution_opens_no_connection.py`) forbids the stdlib's
process module by name so that a push install cannot be written there by
accident. Running wixl opens no connection. The guard stays exact; the
process-running code lives here, and `agent_distribution/msi.py` calls it.

WHAT THE MSI HOLDS, AND NOTHING ELSE:
  * `its-agent.exe` -- bytes the caller verified against `agent_releases`;
  * `msi-enroll.json` -- `{"console": <https origin>, "code": <link code>}`;
  * `ItsMsiInstall` -- deferred, no impersonation (SYSTEM): the installed
    `its-agent.exe --msi-install`, after InstallFiles, on install;
  * `ItsMsiUninstall` -- the same with `--msi-uninstall`, before RemoveFiles,
    on a real removal only (never while an upgrade removes the older MSI),
    with its exit code ignored so a machine can always be removed;
  * a ServiceControl row that stops the `its-agent` service on install and on
    removal, so neither ever writes over a running image.

THE CODE NEVER REACHES A COMMAND LINE. It is written into `msi-enroll.json`
inside a private temporary directory; every argument a tool receives is a
constant or a file name chosen here. Each tool gets a minimal environment,
never this process's own, which holds database URLs and vault tokens. Errors
never carry the code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

__all__ = [
    "BUILD_TIMEOUT_S",
    "TEMPLATES",
    "UPGRADE_NAMESPACE",
    "MsiBuildError",
    "build_agent_msi",
    "product_version",
    "render_wxs",
    "settings_json",
    "upgrade_code",
    "verify_msi",
]

AGENT_FILE = "its-agent.exe"
SETTINGS_FILE = "msi-enroll.json"
PRODUCT_NAME = "ITMS Agent"
MANUFACTURER = "Toeshee"
#: The whole build -- every tool together -- must finish inside this (spec §2.2).
BUILD_TIMEOUT_S = 60.0
#: At most this many builds at once in one process; a third waits, then is refused.
MAX_CONCURRENT_BUILDS = 2
#: UpgradeCode is uuid5(UPGRADE_NAMESPACE, str(org_id)): constant for an
#: organisation, so a later MSI upgrades an earlier one instead of installing
#: beside it (spec §2.2 item 3). This value is fixed forever: changing it
#: would make every MSI already assigned in a domain a different product.
UPGRADE_NAMESPACE = uuid.UUID("22ad0fe1-20ac-49a3-938c-25347fa54ecb")
#: The Summary Information "Template" per architecture. wixl 0.103-0.106 knows
#: x86, x64 and ia64 only, so the ARM64 package is built as x64 and its
#: Template rewritten with msibuild; the tables are the same (one 64-bit
#: component under ProgramFiles64Folder).
TEMPLATES = {"amd64": "x64;1033", "arm64": "Arm64;1033"}
#: Explicit sequence numbers. wixl's Before=/After= scheduling is not reliable
#: (asked for Before="RemoveFiles", 0.103 placed the action at 1402, BEFORE
#: InstallInitialize, where a deferred action cannot run). verify_msi checks
#: the placement on every build.
INSTALL_SEQUENCE = 4010
UNINSTALL_SEQUENCE = 3490
INSTALL_CONDITION = 'NOT REMOVE~="ALL"'
UNINSTALL_CONDITION = 'REMOVE~="ALL" AND NOT UPGRADINGPRODUCTCODE'
#: msidbCustomActionType: 18 (run an installed file) + 1024 (deferred) + 2048
#: (no impersonation: runs as SYSTEM). Removal adds 64 (exit code ignored).
EXPECTED_CUSTOM_ACTIONS = frozenset(
    {
        ("ItsMsiInstall", "3090", "its_agent_exe", "--msi-install"),
        ("ItsMsiUninstall", "3154", "its_agent_exe", "--msi-uninstall"),
    }
)

_VERSION = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?")
_CODE = re.compile(r"[A-Za-z0-9_-]{32}")
_ORIGIN = re.compile(r"https://[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})(?::[1-9][0-9]{0,4})?")
_BUILDS = threading.BoundedSemaphore(MAX_CONCURRENT_BUILDS)


class MsiBuildError(RuntimeError):
    """The MSI could not be built, or did not come out as written. The message
    is safe to show an administrator: it never carries the link code."""


def upgrade_code(org_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(UPGRADE_NAMESPACE, str(org_id))


def product_version(version: str) -> str:
    """`MAJOR.MINOR.PATCH` for the MSI's ProductVersion. A pre-release suffix is
    dropped (Windows Installer versions are numeric; the stable channel carries
    none, the e2e build does); anything else is refused."""
    match = _VERSION.fullmatch(version)
    if match is None:
        raise MsiBuildError(f"{version!r} is not a release version")
    major, minor, patch = (int(group) for group in match.groups())
    if major > 255 or minor > 255 or patch > 65535:
        raise MsiBuildError(
            f"{version} does not fit a Windows Installer product version (at most 255.255.65535)"
        )
    return f"{major}.{minor}.{patch}"


def settings_json(console: str, code: str) -> bytes:
    """`msi-enroll.json`. The agent applies the same two rules again
    (`internal/install/msi.go`) and refuses anything else."""
    if _ORIGIN.fullmatch(console) is None:
        raise MsiBuildError(
            "the console address must be a bare https origin: a host and an optional port"
        )
    if _CODE.fullmatch(code) is None:
        raise MsiBuildError("the install link code must be 32 URL-safe characters")
    return json.dumps({"console": console, "code": code}, separators=(",", ":")).encode()


def render_wxs(*, upgrade: uuid.UUID, version: str) -> str:
    """The WiX source. FIXED TEXT: the only values in it are the product
    version (digits and dots, `product_version`) and the upgrade code (a UUID).
    The console and the code are in `msi-enroll.json`, never here."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Wix xmlns="http://schemas.microsoft.com/wix/2006/wi">
  <Product Id="*" Name="{PRODUCT_NAME}" Language="1033" Version="{product_version(version)}"
           Manufacturer="{MANUFACTURER}" UpgradeCode="{{{str(upgrade).upper()}}}">
    <Package InstallerVersion="500" Compressed="yes" InstallScope="perMachine"
             Description="{PRODUCT_NAME}" Manufacturer="{MANUFACTURER}"/>
    <Media Id="1" Cabinet="agent.cab" EmbedCab="yes"/>
    <MajorUpgrade AllowSameVersionUpgrades="yes"
                  DowngradeErrorMessage="A newer ITMS agent is already installed."/>
    <Property Id="ARPNOMODIFY" Value="1"/>
    <Property Id="ARPNOREPAIR" Value="1"/>
    <Directory Id="TARGETDIR" Name="SourceDir">
      <Directory Id="ProgramFiles64Folder">
        <Directory Id="INSTALLFOLDER" Name="ITSuite">
          <Component Id="Agent" Guid="*">
            <File Id="its_agent_exe" Name="{AGENT_FILE}" Source="{AGENT_FILE}" KeyPath="yes"/>
            <File Id="msi_enroll_json" Name="{SETTINGS_FILE}" Source="{SETTINGS_FILE}"/>
            <ServiceControl Id="StopAgent" Name="its-agent" Stop="both" Wait="yes"/>
          </Component>
        </Directory>
      </Directory>
    </Directory>
    <Feature Id="Agent" Level="1">
      <ComponentRef Id="Agent"/>
    </Feature>
    <CustomAction Id="ItsMsiInstall" FileKey="its_agent_exe" ExeCommand="--msi-install"
                  Execute="deferred" Impersonate="no" Return="check"/>
    <CustomAction Id="ItsMsiUninstall" FileKey="its_agent_exe" ExeCommand="--msi-uninstall"
                  Execute="deferred" Impersonate="no" Return="ignore"/>
    <InstallExecuteSequence>
      <Custom Action="ItsMsiInstall"
              Sequence="{INSTALL_SEQUENCE}">{_xml(INSTALL_CONDITION)}</Custom>
      <Custom Action="ItsMsiUninstall"
              Sequence="{UNINSTALL_SEQUENCE}">{_xml(UNINSTALL_CONDITION)}</Custom>
    </InstallExecuteSequence>
  </Product>
</Wix>
"""


def _xml(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise MsiBuildError(
            f"{name} is not installed; building the MSI needs msitools (wixl, msibuild, msiinfo)"
        )
    return path


def _env() -> dict[str, str]:
    """What a tool may see of the environment: PATH, and on Windows the few
    variables a process needs to start at all."""
    env = {"PATH": os.environ.get("PATH", ""), "LC_ALL": "C.UTF-8"}
    for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _run(argv: list[str], *, cwd: Path, deadline: float) -> str:
    name = Path(argv[0]).name
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MsiBuildError(f"building the MSI took longer than {BUILD_TIMEOUT_S:.0f} seconds")
    try:
        done = subprocess.run(
            argv,
            cwd=cwd,
            env=_env(),
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise MsiBuildError(f"{name} did not finish within {BUILD_TIMEOUT_S:.0f} seconds") from None
    if done.returncode != 0:
        raise MsiBuildError(
            f"{name} failed with exit code {done.returncode}: {done.stderr.strip()[:500]}"
        )
    return done.stdout


def _table(msiinfo: str, msi: Path, table: str, deadline: float) -> list[list[str]]:
    out = _run([msiinfo, "export", msi.name, table], cwd=msi.parent, deadline=deadline)
    return [line.split("\t") for line in out.splitlines()[3:] if line]


def _template(msiinfo: str, msi: Path, deadline: float) -> str:
    for line in _run(
        [msiinfo, "suminfo", msi.name], cwd=msi.parent, deadline=deadline
    ).splitlines():
        if line.startswith("Template:"):
            return line.split(":", 1)[1].strip()
    raise MsiBuildError("the built MSI has no Template in its summary information")


def verify_msi(msi: Path, *, arch: str, deadline: float | None = None) -> None:
    """Read the built file back and refuse it unless it is exactly what
    render_wxs wrote: the two custom actions, where they must run, the two
    files, the service stop, and the architecture. A future wixl that reads the
    source differently fails here, on the server, not on a domain's PCs."""
    msiinfo = _tool("msiinfo")
    deadline = time.monotonic() + BUILD_TIMEOUT_S if deadline is None else deadline
    actions = {tuple(row[:4]) for row in _table(msiinfo, msi, "CustomAction", deadline)}
    if actions != EXPECTED_CUSTOM_ACTIONS:
        raise MsiBuildError(
            f"the built MSI's custom actions are not the ones written: {sorted(actions)}"
        )
    seq = {
        row[0]: (row[1], int(row[2]))
        for row in _table(msiinfo, msi, "InstallExecuteSequence", deadline)
    }
    if seq.get("ItsMsiInstall") != (INSTALL_CONDITION, INSTALL_SEQUENCE) or seq.get(
        "ItsMsiUninstall"
    ) != (UNINSTALL_CONDITION, UNINSTALL_SEQUENCE):
        raise MsiBuildError(
            "the built MSI schedules its custom actions differently from the source"
        )
    try:
        ordered = (
            seq["InstallInitialize"][1]
            < UNINSTALL_SEQUENCE
            < seq["RemoveFiles"][1]
            < seq["InstallFiles"][1]
            < INSTALL_SEQUENCE
            < seq["InstallFinalize"][1]
        )
    except KeyError as exc:
        raise MsiBuildError(f"the built MSI has no {exc.args[0]} action") from None
    if not ordered:
        raise MsiBuildError("the built MSI runs its custom actions outside the install script")
    files = sorted(row[2] for row in _table(msiinfo, msi, "File", deadline))
    if files != [AGENT_FILE, SETTINGS_FILE]:
        raise MsiBuildError(f"the built MSI holds {files}, not the agent and its settings file")
    if _table(msiinfo, msi, "ServiceControl", deadline) != [
        ["StopAgent", "its-agent", "34", "", "1", "Agent"]
    ]:
        raise MsiBuildError(
            "the built MSI does not stop the its-agent service on install and removal"
        )
    if _template(msiinfo, msi, deadline) != TEMPLATES[arch]:
        raise MsiBuildError(f"the built MSI is not marked {TEMPLATES[arch]}")


def _write(path: Path, data: bytes) -> None:
    # O_BINARY on Windows: without it the C runtime would translate newlines
    # in the agent's bytes.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags, 0o600), "wb") as out:
        out.write(data)


def build_agent_msi(
    *,
    exe: bytes,
    console: str,
    code: str,
    org_id: uuid.UUID,
    version: str,
    arch: str,
    timeout_s: float = BUILD_TIMEOUT_S,
) -> bytes:
    """The MSI's bytes. The caller has already verified `exe` (digest and
    release signature); this only packs it, and checks what it packed."""
    if arch not in TEMPLATES:
        raise MsiBuildError(f"there is no MSI for the {arch!r} architecture")
    settings = settings_json(console, code)
    wxs = render_wxs(upgrade=upgrade_code(org_id), version=version)
    wixl, msibuild = _tool("wixl"), _tool("msibuild")
    if not _BUILDS.acquire(timeout=timeout_s):
        raise MsiBuildError("other MSIs are being built right now; try again in a minute")
    try:
        deadline = time.monotonic() + timeout_s
        # 0700, and removed on the way out whatever happens: the settings
        # file holds the code, and the MSI is never stored (spec §2.2 item 4).
        with tempfile.TemporaryDirectory(prefix="itms-msi-") as tmp:
            work = Path(tmp)
            _write(work / AGENT_FILE, exe)
            _write(work / SETTINGS_FILE, settings)
            _write(work / "agent.wxs", wxs.encode())
            _run([wixl, "-a", "x64", "-o", "agent.msi", "agent.wxs"], cwd=work, deadline=deadline)
            if arch == "arm64":
                _run(
                    [msibuild, "agent.msi", "-s", PRODUCT_NAME, MANUFACTURER, TEMPLATES["arm64"]],
                    cwd=work,
                    deadline=deadline,
                )
            verify_msi(work / "agent.msi", arch=arch, deadline=deadline)
            return (work / "agent.msi").read_bytes()
    finally:
        _BUILDS.release()


def main(argv: list[str] | None = None) -> int:
    """`python backend/app/services/msi_build.py ...`, for the Windows e2e.
    The code comes from a FILE, like everywhere else: never an argument."""
    parser = argparse.ArgumentParser(description="Build the ITMS Group Policy MSI.")
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--console", required=True)
    parser.add_argument("--code-file", required=True, type=Path)
    parser.add_argument("--org", required=True, type=uuid.UUID)
    parser.add_argument("--version", required=True)
    parser.add_argument("--arch", required=True, choices=sorted(TEMPLATES))
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        data = build_agent_msi(
            exe=args.exe.read_bytes(),
            console=args.console,
            code=args.code_file.read_text().strip(),
            org_id=args.org,
            version=args.version,
            arch=args.arch,
        )
    except MsiBuildError as exc:
        print(f"msi_build: {exc}", file=sys.stderr)
        return 1
    args.out.write_bytes(data)
    print(hashlib.sha256(data).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`deploy/backend.Dockerfile` — insert after the `ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1` line (before `COPY backend/pyproject.toml ./`; the image is still root there):

```dockerfile

# msitools: the Group Policy MSI is built per request inside this container
# (docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md §2.2,
# app/services/msi_build.py). Debian packages wixl separately from msitools;
# both are needed (wixl builds, msibuild marks ARM64, msiinfo reads it back).
RUN apt-get update \
    && apt-get install -y --no-install-recommends wixl msitools \
    && rm -rf /var/lib/apt/lists/* \
    && for tool in wixl msibuild msiinfo; do command -v "$tool" >/dev/null || exit 1; done
```

(A loop, not `command -v wixl msibuild msiinfo`: dash checks only the first name of that list, so a missing `msiinfo` would pass.)

- [ ] **Step 5: Run the tests to see them pass**

Run: `cd backend && ITS_TEST_REQUIRE_MSITOOLS=1 .venv/bin/python -m pytest tests/test_msi_build.py -q`
Expected: `30 passed`.

**This is the spec's proof.** `test_wixl_expresses_a_deferred_no_impersonate_custom_action` passing means wixl wrote type 3090/3154 rows; `test_both_actions_run_inside_the_install_script_where_they_must` passing means they sit between `InstallInitialize` and `InstallFinalize`, on the right side of `RemoveFiles`/`InstallFiles`. If either fails on a different wixl (the image gets 0.106; the planning host had 0.103), stop and report the `msiinfo export` output rather than weakening the assertions — the spec's named fallback is `wixl` for the file tables and `msibuild -q "INSERT INTO …"` for the `CustomAction` and `InstallExecuteSequence` rows.

- [ ] **Step 6: CI installs msitools and requires it**

`.github/workflows/ci.yml`, job `backend` — insert immediately before the step `- name: pytest`:

```yaml
      # The Group Policy MSI is built with wixl, msibuild and msiinfo
      # (app/services/msi_build.py). Ubuntu packages wixl apart from msitools.
      - name: msitools
        run: sudo apt-get update -qq && sudo apt-get install -y -qq wixl msitools
```

and in the `pytest` step's `env:` block, beside `ITS_TEST_REQUIRE_GO: "1"`, add:

```yaml
          # A missing msitools is a failure here, never a quiet skip.
          ITS_TEST_REQUIRE_MSITOOLS: "1"
```

- [ ] **Step 7: Lint and type-check**

Run: `cd backend && .venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: `All checks passed!` and `Success: no issues found`.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/msi_build.py backend/tests/test_msi_build.py deploy/backend.Dockerfile .github/workflows/ci.yml
git commit -m "feat(msi): build the Group Policy MSI with wixl, read back and verified" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---

### Task 2: The release key, reading the published binary back, and verifying it

**Files:**
- Modify: `backend/app/core/config.py` (after `public_console_url: str | None = None`)
- Modify: `backend/app/core/storage.py` (a method before `def agent_release_key`)
- Modify: `deploy/.env.example` (after the `ITS_PUBLIC_CONSOLE_URL=` line)
- Create: `backend/app/modules/agent_distribution/msi.py`
- Create: `docs/kernel/wire-protocol/testdata/release-signature.json`
- Create: `backend/tests/test_release_signature_vector.py`
- Create: `agent/internal/update/releasesig_vector_test.go`
- Modify: `backend/tests/test_storage.py` (append one test)

**Interfaces:**
- Consumes: `app.services.msi_build.build_agent_msi`, `MsiBuildError` (Task 1); `link_page.LinkRelease(version, os, arch, sha256, signature)`, `link_page.ARCH_LABEL`; `agent_update.semver.try_parse_version`.
- Produces:
  - `Settings.agent_release_public_key: str | None` (env `ITS_AGENT_RELEASE_PUBLIC_KEY`).
  - `ObjectStore.get_agent_release(version: str, os_name: str, arch: str) -> bytes` (async).
  - In `app.modules.agent_distribution.msi`: `class MsiRefused(Exception)` with `.status: int` and `.message: str`; `release_message(version: str, os_name: str, arch: str, sha256_hex: str) -> bytes`; `release_public_key(value: str | None) -> Ed25519PublicKey` (raises `MsiRefused(503, …)`); `verify_release_bytes(data: bytes, release: LinkRelease, key: Ed25519PublicKey) -> None` (raises `MsiRefused(409, …)`); `build_msi(release: LinkRelease, console: str, code: str, *, exe: bytes, org_id: UUID, key: Ed25519PublicKey) -> bytes`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_release_signature_vector.py`:

```python
"""The server verifies a release signature exactly as the agent does
(`agent/internal/update/releasesig.go`). One vector, two verifiers: the Go
twin is `TestReleaseSignatureVector`. A drift in either message format fails
one of the two, instead of every MSI download failing closed in production."""

import base64
import json
from pathlib import Path

import pytest

from app.modules.agent_distribution.link_page import LinkRelease
from app.modules.agent_distribution.msi import (
    MsiRefused,
    release_message,
    release_public_key,
    verify_release_bytes,
)

REPO = Path(__file__).resolve().parents[2]
VECTOR = json.loads(
    (REPO / "docs/kernel/wire-protocol/testdata/release-signature.json").read_text()
)


def test_the_shared_vector_verifies() -> None:
    key = release_public_key(VECTOR["public_key"])
    message = release_message(VECTOR["version"], VECTOR["os"], VECTOR["arch"], VECTOR["sha256"])
    assert message == (
        b"its-agent-release/v1\n1.6.0\nwindows\namd64\n" + VECTOR["sha256"].encode() + b"\n"
    )
    key.verify(base64.b64decode(VECTOR["signature"]), message)


def test_a_leading_v_and_upper_case_hex_sign_the_same_message() -> None:
    assert release_message("v1.6.0", "windows", "amd64", VECTOR["sha256"].upper()) == (
        release_message("1.6.0", "windows", "amd64", VECTOR["sha256"])
    )


def test_bytes_that_are_not_the_releases_are_refused_before_the_signature() -> None:
    key = release_public_key(VECTOR["public_key"])
    release = LinkRelease("1.6.0", "windows", "amd64", VECTOR["sha256"], VECTOR["signature"])
    with pytest.raises(MsiRefused, match="does not match its published SHA-256"):
        verify_release_bytes(b"not the release", release, key)


@pytest.mark.parametrize("value", [None, "", "   ", "not base64!", "AAAA"])
def test_a_missing_or_malformed_public_key_is_a_503(value: str | None) -> None:
    with pytest.raises(MsiRefused) as refused:
        release_public_key(value)
    assert refused.value.status == 503


def test_the_setting_is_documented_where_the_operator_edits() -> None:
    assert "\nITS_AGENT_RELEASE_PUBLIC_KEY=\n" in (REPO / "deploy" / ".env.example").read_text()
```

Append to `backend/tests/test_storage.py`:

```python


async def test_an_agent_release_is_read_back_by_its_identity(store: ObjectStore) -> None:
    """The Group Policy MSI reads the published binary by (version, os, arch);
    the key is derived, so a traversal in a version is refused, not read."""
    await store.put_agent_release(store.agent_release_key("1.7.0", "windows", "amd64"), b"MZ-1")
    assert await store.get_agent_release("1.7.0", "windows", "amd64") == b"MZ-1"
    with pytest.raises(ValueError):
        await store.get_agent_release("../../backups", "windows", "amd64")
```

`agent/internal/update/releasesig_vector_test.go`:

```go
package update

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// One offline release signature, checked by BOTH verifiers: this one, and the
// control plane's (backend tests/test_release_signature_vector.py), which
// checks the agent the Group Policy MSI wraps (GPO MSI spec 2026-09-30 §2.2).
// A change to ReleaseMessage fails here or there, instead of every MSI
// download failing closed in production.
func TestReleaseSignatureVector(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "docs", "kernel", "wire-protocol", "testdata", "release-signature.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v struct {
		PublicKey string `json:"public_key"`
		Version   string `json:"version"`
		OS        string `json:"os"`
		Arch      string `json:"arch"`
		SHA256    string `json:"sha256"`
		Signature string `json:"signature"`
	}
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	pub, err := ParseReleaseKey(v.PublicKey)
	if err != nil || pub == nil {
		t.Fatalf("the vector's key: %v", err)
	}
	if status, reason := CheckReleaseSignature(pub, v.Version, v.OS, v.Arch, v.SHA256, v.Signature); status != SignatureVerified {
		t.Fatalf("the shared vector does not verify: %q", reason)
	}
	if _, reason := CheckReleaseSignature(pub, v.Version, v.OS, "arm64", v.SHA256, v.Signature); reason == "" {
		t.Fatal("the vector verified for another architecture")
	}
}
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_release_signature_vector.py tests/test_storage.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'app.modules.agent_distribution.msi'`.

Run: `cd agent && go test ./internal/update/ -run TestReleaseSignatureVector -count=1`
Expected: FAIL, `open ../../../docs/kernel/wire-protocol/testdata/release-signature.json: no such file or directory`.

- [ ] **Step 3: The vector**

`docs/kernel/wire-protocol/testdata/release-signature.json` (the key is the Ed25519 key whose seed is the bytes `00 01 … 1f`; the signature was produced with it and checked with Go's `ed25519.Verify` while planning):

```json
{
  "comment": "One offline release signature, checked by BOTH verifiers: agent/internal/update (TestReleaseSignatureVector) and backend app/modules/agent_distribution/msi.py (tests/test_release_signature_vector.py). The key is Ed25519 from the seed 00 01 02 ... 1f; it signs nothing real.",
  "public_key": "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=",
  "version": "1.6.0",
  "os": "windows",
  "arch": "amd64",
  "sha256": "abababababababababababababababababababababababababababababababab",
  "signature": "5kFY5cSU+jX8861cnGZJ0WuAib24lex87BoZiPaDbx8aZDiwTYB4P0zlhNPAi4bBvlnL4uNRtYK5SCRKE1gKDA=="
}
```

- [ ] **Step 4: The setting, the object-store read and the verifier**

`backend/app/core/config.py` — after `public_console_url: str | None = None`:

```python
    # The PUBLIC half of the offline release-signing key, base64 Ed25519: the
    # `release-signing-key.pub` asset of an agent release (release-agent.yml).
    # Not a secret. The Group Policy MSI verifies the agent it wraps against it
    # (GPO MSI spec 2026-09-30 §2.2); unset, the MSI download answers 503 --
    # it never wraps bytes it could not verify.
    agent_release_public_key: str | None = None
```

`backend/app/core/storage.py` — immediately before `def agent_release_key(self, version: str, os_name: str, arch: str) -> str:`:

```python
    async def get_agent_release(self, version: str, os_name: str, arch: str) -> bytes:
        """One agent artefact's bytes, addressed by the release's IDENTITY.

        The key is derived here by `agent_release_suffix`, exactly as for a
        grant, never read from a stored `storage_ref`. The Group Policy MSI
        reads the published Windows binary this way and re-hashes it against
        `agent_releases.sha256` before it wraps it (GPO MSI spec §2.2).
        """
        key = agent_release_suffix(version, os_name, arch)
        response = await self._run(self._client.get_object, Bucket=self._bucket, Key=key)
        body: bytes = response["Body"].read()
        return body

```

`deploy/.env.example` — after the line `ITS_PUBLIC_CONSOLE_URL=` (and its following blank line), before `# --- Trusted proxy (install link) ---`:

```sh
# --- Agent release-signing public key (Group Policy MSI) ---
# The PUBLIC half of the offline key agent releases are signed with, base64:
# the `release-signing-key.pub` asset of any agent release on GitHub (the
# release notes print it too). Not a secret. "Download MSI for Group Policy"
# verifies the agent it wraps against it, and refuses while it is unset.
ITS_AGENT_RELEASE_PUBLIC_KEY=

```

`backend/app/modules/agent_distribution/msi.py` (Task 3 replaces the import block and appends the rest; write this now exactly):

```python
"""The Group Policy MSI, built for an administrator (GPO MSI spec 2026-09-30
§2.1, §2.2).

WHAT IS CHECKED, IN ORDER, BEFORE A BYTE IS PACKED:
  1. the link is on and the code the administrator supplied is its current
     code (only the code's hash is stored, so the console sends the code it
     was shown; `install_link.code_state`);
  2. the console is reached by a DNS name, because the MSI's file name and the
     agent's enrollment URL both carry it (`console_origin`);
  3. a stable, release-signed Windows agent of `MSI_MIN_AGENT_VERSION` or
     later is published for the architecture (`link_releases`);
  4. the platform knows the release-signing public key
     (`ITS_AGENT_RELEASE_PUBLIC_KEY`), and the bytes read back from object
     storage hash to `agent_releases.sha256` and carry a valid offline release
     signature under it (`verify_release_bytes`).
Anything else is a refusal with a sentence, and nothing is audited, because
nothing was handed out.

BUILT PER REQUEST AND NEVER STORED. The bytes go to the administrator and
nowhere else, so a regenerated code cannot survive in a cache. The audit
record carries the architecture, the release and the MSI's SHA-256 -- never
the code.

The build itself -- WiX source, wixl, the read-back -- is
`app/services/msi_build.py`, and why it lives there is said at its top.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.modules.agent_distribution.link_page import ARCH_LABEL, LinkRelease
from app.modules.agent_update.semver import try_parse_version
from app.services import msi_build


class MsiRefused(Exception):
    """A refusal an administrator can act on. `status` is the HTTP status the
    route answers with; `message` is one sentence, and never holds the code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def release_message(version: str, os_name: str, arch: str, sha256_hex: str) -> bytes:
    """What the offline release key signs: `update.ReleaseMessage` in the
    agent, byte for byte (`docs/kernel/wire-protocol/testdata/
    release-signature.json` pins both sides)."""
    parsed = try_parse_version(version)
    canonical = str(parsed) if parsed is not None else version
    return (
        f"its-agent-release/v1\n{canonical}\n{os_name}\n{arch}\n{sha256_hex.lower()}\n"
    ).encode()


def release_public_key(value: str | None) -> Ed25519PublicKey:
    """`ITS_AGENT_RELEASE_PUBLIC_KEY`, parsed. Unset or malformed is a 503:
    the MSI is never built from bytes that could not be verified."""
    if value is None or not value.strip():
        raise MsiRefused(
            503,
            "ITS_AGENT_RELEASE_PUBLIC_KEY is not set, so the agent the MSI would carry "
            "cannot be verified. Set it to the agent release's release-signing-key.pub.",
        )
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != 32:
        raise MsiRefused(503, "ITS_AGENT_RELEASE_PUBLIC_KEY is not a base64 Ed25519 public key.")
    return Ed25519PublicKey.from_public_bytes(raw)


def verify_release_bytes(data: bytes, release: LinkRelease, key: Ed25519PublicKey) -> None:
    """The bytes are the release's: their SHA-256 is `agent_releases.sha256`,
    and the release carries a valid offline signature over (version, windows,
    arch, that digest) -- the check self-update and the self-installer make."""
    digest = hashlib.sha256(data).hexdigest()
    label = ARCH_LABEL.get(release.arch, release.arch)
    if digest != release.sha256.lower():
        raise MsiRefused(
            409,
            f"The stored agent {release.version} for Windows ({label}) does not match its "
            "published SHA-256, so no MSI was built. Publish the release again.",
        )
    try:
        signature = base64.b64decode(release.signature or "", validate=True)
        key.verify(signature, release_message(release.version, "windows", release.arch, digest))
    except (binascii.Error, ValueError, InvalidSignature):
        raise MsiRefused(
            409,
            f"The agent {release.version} for Windows ({label}) does not carry a valid "
            "release signature, so no MSI was built.",
        ) from None


def build_msi(
    release: LinkRelease,
    console: str,
    code: str,
    *,
    exe: bytes,
    org_id: UUID,
    key: Ed25519PublicKey,
) -> bytes:
    """Spec §2.2's builder. Verifies `exe` first, then packs it."""
    verify_release_bytes(exe, release, key)
    try:
        return msi_build.build_agent_msi(
            exe=exe,
            console=console,
            code=code,
            org_id=org_id,
            version=release.version,
            arch=release.arch,
        )
    except msi_build.MsiBuildError as exc:
        raise MsiRefused(503, f"The MSI could not be built: {exc}") from None
```

- [ ] **Step 5: Run the tests to see them pass**

Run: `cd backend && .venv/bin/python -m pytest tests/test_release_signature_vector.py tests/test_storage.py tests/test_agent_distribution_opens_no_connection.py -q`
Expected: `16 passed` — the last file proves `msi.py` names nothing the guard forbids.

Run: `cd agent && go test ./internal/update/ -run TestReleaseSignatureVector -count=1 -v`
Expected: `--- PASS: TestReleaseSignatureVector`.

- [ ] **Step 6: Lint and type-check**

Run: `cd backend && .venv/bin/ruff check app tests && .venv/bin/mypy app` and `cd agent && test -z "$(gofmt -l .)" && go vet ./internal/update/`
Expected: no findings.

- [ ] **Step 7: Commit**

```bash
git add backend/app/core/config.py backend/app/core/storage.py deploy/.env.example backend/app/modules/agent_distribution/msi.py docs/kernel/wire-protocol/testdata/release-signature.json backend/tests/test_release_signature_vector.py backend/tests/test_storage.py agent/internal/update/releasesig_vector_test.go
git commit -m "feat(msi): verify the wrapped agent against the offline release key" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---

### Task 3: `POST /api/v1/install-link/msi`

**Files:**
- Modify: `backend/app/services/install_link.py` (a function before `async def resolve_org`)
- Modify: `backend/app/modules/agent_distribution/msi.py` (import block, constants, and four definitions appended)
- Modify: `backend/app/api/v1/install_link.py` (imports, one request model, one route)
- Create: `backend/tests/msi_fixtures.py`
- Create: `backend/tests/test_install_link_msi.py`

**Interfaces:**
- Consumes: Task 2's `MsiRefused`, `build_msi`, `release_public_key`, `release_message`; Task 1's msitools; `link_page.link_releases(session) -> dict[tuple[str, str], LinkRelease]`, `link_page.console_host_part(console) -> str | None`; `install_link.console_base`, `_row`, `is_live`, `looks_like_code`, `hash_code`; `agent_distribution.api._object_store(request)`; `audit_service.record`.
- Produces:
  - `install_link.code_state(session, org_id, *, code: str, now: datetime) -> str` returning `"live"`, `"off"` or `"other"`.
  - In `msi`: `MSI_MIN_AGENT_VERSION` (1.7.0), `MSI_ARCHES = ("amd64", "arm64")`, `MSI_MEDIA_TYPE = "application/x-msi"`, `AUDIT_ACTION = "install_link.msi_downloaded"`, `@dataclass(frozen=True) BuiltMsi(filename: str, data: bytes, sha256: str, version: str, arch: str)`, `msi_file_name(host_part: str, arch: str) -> str`, `console_origin(console: str) -> tuple[str, str] | None`, `async msi_for_link(session, store, *, org_id, actor_id, code, arch, console, public_key, now) -> BuiltMsi`.
  - Route `POST /api/v1/install-link/msi` (body `InstallLinkMsiRequest{code, arch}`), 200 `application/x-msi` with `Content-Disposition`, `X-Content-SHA256`, `Cache-Control: no-store`; refusals 409/422/503 with a string `detail`.
  - `tests/msi_fixtures.py`: `need_msitools()`, `SignedRelease`, `signed_release(version="1.7.0", arch="amd64")`, `offer(monkeypatch, *releases)`, `StoreWith(objects)` (with `.reads`).

- [ ] **Step 1: Write the failing tests**

`backend/tests/msi_fixtures.py`:

```python
"""Shared helpers for the Group Policy MSI tests (not a test module)."""

import base64
import hashlib
import os
import shutil
from dataclasses import dataclass
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.core.storage import ObjectStore
from app.modules.agent_distribution import msi
from app.modules.agent_distribution.link_page import LinkRelease
from app.modules.agent_distribution.msi import release_message

MSITOOLS = ("wixl", "msibuild", "msiinfo", "msiextract")


def need_msitools() -> None:
    """Skip without msitools -- except in CI, where ITS_TEST_REQUIRE_MSITOOLS=1
    makes a missing tool a failure rather than a quiet skip."""
    missing = [tool for tool in MSITOOLS if shutil.which(tool) is None]
    if missing:
        if os.environ.get("ITS_TEST_REQUIRE_MSITOOLS") == "1":
            pytest.fail(f"ITS_TEST_REQUIRE_MSITOOLS=1 but {missing} are not on PATH")
        pytest.skip(f"msitools is not installed ({missing})")


@dataclass
class SignedRelease:
    version: str
    arch: str
    data: bytes
    sha256: str
    signature: str
    public_key: str


def signed_release(version: str = "1.7.0", arch: str = "amd64") -> SignedRelease:
    """A Windows agent build and its offline release signature, under a key
    made for this test."""
    key = Ed25519PrivateKey.generate()
    data = b"MZ" + os.urandom(4096)
    sha = hashlib.sha256(data).hexdigest()
    signature = key.sign(release_message(version, "windows", arch, sha))
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return SignedRelease(
        version,
        arch,
        data,
        sha,
        base64.b64encode(signature).decode(),
        base64.b64encode(public).decode(),
    )


def offer(monkeypatch: pytest.MonkeyPatch, *releases: SignedRelease) -> None:
    """Make these the releases the link offers. The catalogue is GLOBAL and
    other tests publish into it, so the pick is replaced -- as
    `test_install_link_page.py`'s `releases` fixture does -- and the pick
    itself stays covered there (`pick_link_releases`)."""

    async def fake(_session: Any) -> dict[tuple[str, str], LinkRelease]:
        return {
            ("windows", r.arch): LinkRelease(r.version, "windows", r.arch, r.sha256, r.signature)
            for r in releases
        }

    monkeypatch.setattr(msi, "link_releases", fake)


class StoreWith(ObjectStore):
    """An object store holding exactly the given artefacts, by identity."""

    def __init__(self, objects: dict[tuple[str, str, str], bytes]) -> None:  # noqa: D107
        self.objects = objects
        self.reads: list[tuple[str, str, str]] = []

    async def get_agent_release(self, version: str, os_name: str, arch: str) -> bytes:
        self.reads.append((version, os_name, arch))
        return self.objects[(version, os_name, arch)]
```

`backend/tests/test_install_link_msi.py`:

```python
"""The Group Policy MSI download (GPO MSI spec 2026-09-30 §2.1, §2.2, §2.6)."""

import hashlib
import json
import logging
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent
from tests.install_link_fixtures import switch_on
from tests.msi_fixtures import StoreWith, need_msitools, offer, signed_release

CONSOLE = "https://itms.example.com"
URL = "/api/v1/install-link/msi"


async def _setup(api_env, monkeypatch, *, version: str = "1.7.0", key: bool = True):
    monkeypatch.setattr(api_env.app.state.settings, "public_console_url", CONSOLE)
    org_id = await api_env.create_org("msi")
    code = await switch_on(api_env, org_id, ["10.50.0.0/16"])
    release = signed_release(version)
    offer(monkeypatch, release)
    monkeypatch.setattr(
        api_env.app.state.settings,
        "agent_release_public_key",
        release.public_key if key else None,
    )
    store = StoreWith({(release.version, "windows", "amd64"): release.data})
    api_env.app.state.object_store = store
    admin = await api_env.headers(org_id, await api_env.create_user(org_id, "org-admin"))
    return org_id, code, release, store, admin


async def _audits(api_env, org_id) -> list[AuditEvent]:
    async with AsyncSession(api_env.admin_engine) as session:
        return list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org_id,
                        AuditEvent.action == "install_link.msi_downloaded",
                    )
                )
            ).scalars()
        )


async def test_it_needs_enrollment_link_manage(api_env, monkeypatch) -> None:
    org_id, code, *_ = await _setup(api_env, monkeypatch)
    tech = await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=tech)
    assert response.status_code == 403


async def test_the_msi_carries_the_agent_and_the_link_and_is_audited_without_the_code(
    api_env, monkeypatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    need_msitools()
    caplog.set_level(logging.DEBUG)
    org_id, code, release, store, admin = await _setup(api_env, monkeypatch)
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/x-msi"
    assert response.headers["cache-control"] == "no-store"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="ITMS-Agent-itms.example.com-amd64.msi"'
    )
    digest = hashlib.sha256(response.content).hexdigest()
    assert response.headers["x-content-sha256"] == digest
    assert store.reads == [("1.7.0", "windows", "amd64")]

    msi = tmp_path / "agent.msi"
    msi.write_bytes(response.content)
    out = tmp_path / "x"
    subprocess.run(["msiextract", "-C", str(out), str(msi)], check=True, capture_output=True)
    assert (out / "ITSuite" / "its-agent.exe").read_bytes() == release.data
    assert json.loads((out / "ITSuite" / "msi-enroll.json").read_text()) == {
        "console": CONSOLE,
        "code": code,
    }

    [audit] = await _audits(api_env, org_id)
    assert audit.target_type == "install_link" and audit.target_id == str(org_id)
    assert audit.meta == {
        "arch": "amd64",
        "version": "1.7.0",
        "msi_sha256": digest,
        "agent_sha256": release.sha256,
    }
    assert code not in json.dumps(audit.meta)
    assert code not in caplog.text


async def test_it_is_off_when_the_link_is_off(api_env, monkeypatch) -> None:
    org_id, code, _, store, admin = await _setup(api_env, monkeypatch)
    off = await api_env.client.put(
        "/api/v1/install-link", json={"enabled": False, "networks": ["10.50.0.0/16"]}, headers=admin
    )
    assert off.status_code == 200
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 409
    assert "install link is off" in response.json()["detail"]
    assert store.reads == [] and await _audits(api_env, org_id) == []


async def test_a_code_that_is_not_the_current_one_is_refused(api_env, monkeypatch) -> None:
    org_id, code, _, store, admin = await _setup(api_env, monkeypatch)
    regenerated = await api_env.client.post("/api/v1/install-link/regenerate", headers=admin)
    assert regenerated.status_code == 200
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 409
    assert "not this organisation's current install link" in response.json()["detail"]
    assert code not in response.text
    assert store.reads == [] and await _audits(api_env, org_id) == []


async def test_an_agent_older_than_the_msi_mode_is_not_wrapped(api_env, monkeypatch) -> None:
    org_id, code, _, store, admin = await _setup(api_env, monkeypatch, version="1.6.3")
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 409
    assert "1.7.0 or later" in response.json()["detail"]
    assert store.reads == []


async def test_without_the_release_public_key_nothing_is_built(api_env, monkeypatch) -> None:
    org_id, code, _, store, admin = await _setup(api_env, monkeypatch, key=False)
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 503
    assert "ITS_AGENT_RELEASE_PUBLIC_KEY" in response.json()["detail"]
    assert store.reads == [] and await _audits(api_env, org_id) == []


async def test_bytes_that_are_not_the_published_release_are_refused(api_env, monkeypatch) -> None:
    org_id, code, release, store, admin = await _setup(api_env, monkeypatch)
    store.objects[(release.version, "windows", "amd64")] = release.data + b"tampered"
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 409
    assert "does not match its published SHA-256" in response.json()["detail"]
    assert await _audits(api_env, org_id) == []


async def test_a_signature_under_another_key_is_refused(api_env, monkeypatch) -> None:
    org_id, code, _, _, admin = await _setup(api_env, monkeypatch)
    monkeypatch.setattr(
        api_env.app.state.settings, "agent_release_public_key", signed_release().public_key
    )
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 409
    assert "valid release signature" in response.json()["detail"]
    assert await _audits(api_env, org_id) == []


async def test_a_console_reached_by_ip_address_gets_no_msi(api_env, monkeypatch) -> None:
    org_id, code, _, store, admin = await _setup(api_env, monkeypatch)
    monkeypatch.setattr(api_env.app.state.settings, "public_console_url", "https://10.0.0.5")
    response = await api_env.client.post(URL, json={"code": code, "arch": "amd64"}, headers=admin)
    assert response.status_code == 409
    assert "DNS name" in response.json()["detail"]
    assert store.reads == []


async def test_an_unknown_arch_is_a_422_and_the_code_is_never_in_the_url(
    api_env, monkeypatch
) -> None:
    _, code, _, _, admin = await _setup(api_env, monkeypatch)
    response = await api_env.client.post(URL, json={"code": code, "arch": "x86"}, headers=admin)
    assert response.status_code == 422
    # POST only: a GET would put the code in a URL, which access logs keep.
    assert (await api_env.client.get(URL, headers=admin)).status_code == 405
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_install_link_msi.py -q`
Expected: `10 failed` — each in `_setup`, with `AttributeError: <module 'app.modules.agent_distribution.msi' …> has no attribute 'link_releases'` (the orchestration and its import of `link_releases` arrive in Step 4; the route in Step 5).

- [ ] **Step 3: `install_link.code_state`**

`backend/app/services/install_link.py` — immediately before `async def resolve_org(admin_engine: AsyncEngine, code: str) -> UUID | None:`:

```python
async def code_state(session: AsyncSession, org_id: UUID, *, code: str, now: datetime) -> str:
    """What `code` is to this organisation right now, for an ADMINISTRATOR's
    request (the Group Policy MSI, GPO MSI spec 2026-09-30 §2.2): `live` -- it
    is the current code and the link is on; `off` -- the link is off, or has
    no code; `other` -- the link is on and this is not its code.

    Only the hash is compared, as everywhere else: the code itself is never
    stored. The allowed networks are NOT checked here -- this answers an
    authenticated administrator in the console, not a machine joining.
    """
    row = await _row(session, org_id)
    if row is None or not is_live(row, now):
        return "off"
    if not looks_like_code(code) or row.code_hash != hash_code(code):
        return "other"
    return "live"


```

- [ ] **Step 4: The orchestration**

In `backend/app/modules/agent_distribution/msi.py`, replace the import block (from `from __future__ import annotations` through `from app.services import msi_build`) with:

```python
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.storage import ObjectStore
from app.modules.agent_distribution.link_page import (
    ARCH_LABEL,
    LinkRelease,
    console_host_part,
    link_releases,
)
from app.modules.agent_update.semver import Version, parse_version, try_parse_version
from app.services import install_link as link_svc
from app.services import msi_build

#: The first agent that has `--msi-install` and `--msi-uninstall`. 1.6.0 was
#: tagged before them (`agent-v1.6.0`); an MSI wrapping it would fail its own
#: custom action on every computer.
MSI_MIN_AGENT_VERSION: Version = parse_version("1.7.0")
MSI_ARCHES = ("amd64", "arm64")
MSI_MEDIA_TYPE = "application/x-msi"
AUDIT_ACTION = "install_link.msi_downloaded"
```

and append to the end of the file:

```python


@dataclass(frozen=True)
class BuiltMsi:
    filename: str
    data: bytes
    sha256: str
    version: str
    arch: str


def msi_file_name(host_part: str, arch: str) -> str:
    """`ITMS-Agent-<host>[@<port>]-<arch>.msi` (spec §2.1). The name is only a
    label here: the console the agent joins is inside the MSI."""
    return f"ITMS-Agent-{host_part}-{arch}.msi"


def console_origin(console: str) -> tuple[str, str] | None:
    """`(https://<host>[:<port>], <host>[@<port>])`, or None when the console
    is not a bare https origin on a DNS name -- the same rule as the link's
    Windows download (`console_host_part`)."""
    parts = urlsplit(console)
    if (
        parts.scheme != "https"
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or parts.username is not None
    ):
        return None
    host_part = console_host_part(console)
    if host_part is None:
        return None
    host, _, port = host_part.partition("@")
    return (f"https://{host}:{port}" if port else f"https://{host}", host_part)


async def msi_for_link(
    session: AsyncSession,
    store: ObjectStore,
    *,
    org_id: UUID,
    actor_id: UUID,
    code: str,
    arch: str,
    console: str,
    public_key: str | None,
    now: datetime,
) -> BuiltMsi:
    if arch not in MSI_ARCHES:
        raise MsiRefused(422, "The MSI is built for amd64 (64-bit Intel or AMD) or arm64 (ARM).")
    state = await link_svc.code_state(session, org_id, code=code, now=now)
    if state == "off":
        raise MsiRefused(
            409,
            "The install link is off. Switch it on before downloading the MSI: "
            "computers join through it.",
        )
    if state != "live":
        raise MsiRefused(
            409,
            "That is not this organisation's current install link. Paste the link shown "
            "when it was last generated, or generate a new one.",
        )
    origin = console_origin(console)
    if origin is None:
        raise MsiRefused(
            409,
            "The MSI needs ITMS to be reached by a DNS name, such as itms.example.com, "
            "not an IP address. Set ITS_PUBLIC_CONSOLE_URL.",
        )
    release = (await link_releases(session)).get(("windows", arch))
    if release is None or parse_version(release.version) < MSI_MIN_AGENT_VERSION:
        raise MsiRefused(
            409,
            f"No stable, release-signed agent {MSI_MIN_AGENT_VERSION} or later is published "
            f"for Windows ({ARCH_LABEL[arch]}). Mark one stable first.",
        )
    key = release_public_key(public_key)
    try:
        exe = await store.get_agent_release(release.version, "windows", arch)
    except Exception:  # noqa: BLE001 -- storage down or the object missing: one sentence
        raise MsiRefused(
            503,
            f"The agent {release.version} for Windows could not be read from object storage.",
        ) from None
    data = await asyncio.to_thread(
        build_msi, release, origin[0], code, exe=exe, org_id=org_id, key=key
    )
    digest = hashlib.sha256(data).hexdigest()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action=AUDIT_ACTION,
        target_type="install_link",
        target_id=str(org_id),
        # NEVER the code. What was handed out, and how to recognise the file.
        metadata={
            "arch": arch,
            "version": release.version,
            "msi_sha256": digest,
            "agent_sha256": release.sha256,
        },
    )
    return BuiltMsi(
        filename=msi_file_name(origin[1], arch),
        data=data,
        sha256=digest,
        version=release.version,
        arch=arch,
    )
```

- [ ] **Step 5: The route**

`backend/app/api/v1/install_link.py` — replace the import block with:

```python
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import AuthContext, require_permission
from app.core.tenancy import org_session
from app.modules.agent_distribution import msi as msi_svc
from app.modules.agent_distribution.api import _object_store
from app.services import install_link as svc
```

insert immediately before `def _out(view: svc.LinkView) -> InstallLinkOut:`:

```python
class InstallLinkMsiRequest(BaseModel):
    """Download the Group Policy MSI (GPO MSI spec 2026-09-30 §2.1).

    THE CODE TRAVELS IN THIS BODY, NEVER IN THE URL, which access logs keep.
    The server holds only the code's hash, so the administrator supplies the
    code -- from the link shown when it was generated -- and the server checks
    it against that hash before building anything.
    """

    model_config = ConfigDict(extra="forbid")

    code: str = Field(max_length=64, description="The install link's code: the part after /i/")
    arch: Literal["amd64", "arm64"] = Field(description="The Windows architecture of the MSI")


```

and append to the end of the file:

```python


@router.post(
    "/msi",
    response_class=Response,
    responses={200: {"content": {msi_svc.MSI_MEDIA_TYPE: {}}}},
)
async def download_install_link_msi(
    body: InstallLinkMsiRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permission("enrollment.link.manage")),
) -> Response:
    """The MSI for Group Policy software installation: the published agent and
    this organisation's install link, built for this request and not kept."""
    state = request.app.state
    async with org_session(ctx.org_id, engine=state.app_engine) as session:
        try:
            built = await msi_svc.msi_for_link(
                session,
                _object_store(request),
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                code=body.code,
                arch=body.arch,
                console=_base(request),
                public_key=state.settings.agent_release_public_key,
                now=datetime.now(UTC),
            )
        except msi_svc.MsiRefused as exc:
            raise HTTPException(exc.status, exc.message) from None
    return Response(
        content=built.data,
        media_type=msi_svc.MSI_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{built.filename}"',
            "X-Content-SHA256": built.sha256,
            "Cache-Control": "no-store",
        },
    )
```

The `HTTPException` is raised inside `org_session`, so the transaction rolls back: a refusal writes no audit row. The success path's audit row commits when the block exits, before the bytes are returned.

- [ ] **Step 6: Run the tests to see them pass**

Run: `cd backend && ITS_TEST_REQUIRE_MSITOOLS=1 .venv/bin/python -m pytest tests/test_install_link_msi.py tests/test_install_link_api.py tests/test_agent_distribution_opens_no_connection.py tests/test_route_gating.py tests/test_route_session_discipline.py tests/test_route_uniqueness.py tests/test_ai_tool_surface.py -q`
Expected: all pass. `test_route_gating` sees the new route gated on `enrollment.link.manage`; `test_ai_tool_surface` is unchanged because the route is a POST.

- [ ] **Step 7: Lint and type-check**

Run: `cd backend && .venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: no findings.

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/install_link.py backend/app/modules/agent_distribution/msi.py backend/app/api/v1/install_link.py backend/tests/msi_fixtures.py backend/tests/test_install_link_msi.py
git commit -m "feat(msi): download the Group Policy MSI for the current install link" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---

### Task 4: `POST /api/v1/devices/review/approve-all`

**Files:**
- Modify: `backend/app/services/device_review.py` (imports, a constant, an exception, `approve` split, three definitions)
- Modify: `backend/app/api/v1/devices.py` (two models and one route, before `@router.post("/{device_id}/review/approve", …)`)
- Create: `backend/tests/test_device_review_approve_all.py`

**Interfaces:**
- Consumes: `device_review.approve`'s audit shape (`device.review_approved`, metadata `hostname`, `link_joined_from`, `approved_at`); `tests.ipam_fixtures.seed_org`, `seed_device`.
- Produces:
  - `device_review.APPROVE_ALL_MAX = 1000`; `class ApproveAllRefused(ValueError)`; `review_list_digest(device_ids: Iterable[UUID]) -> str`; `@dataclass(frozen=True) ApproveAllOutcome(bulk_id: UUID, approved: int, not_pending: int, still_waiting: int)`; `async approve_all(session, *, org_id, actor_id, device_ids: list[UUID], list_digest: str, now) -> ApproveAllOutcome`.
  - Route `POST /api/v1/devices/review/approve-all` → `ApproveAllOut{bulk_id, approved, not_pending, still_waiting}`; 422 with a string `detail` for an empty list, more than 1,000 ids, or a digest that is not the list's.
  - The console's `reviewListDigest` (Task 9) must equal `review_list_digest`; both tests pin `7a70c782c5d30f61a8f57b905eaa11c41ec8ffeafaa3a0e99c4fca60044a28d4` for ids `…0001` and `…0002`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_device_review_approve_all.py`:

```python
"""Approve all (GPO MSI spec 2026-09-30 §2.5): only the LISTED machines that
are still pending, one audit record each under a shared bulk_id, at most
1,000, and only with the digest of the list that was shown."""

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, Device
from app.services.device_review import APPROVE_ALL_MAX, review_list_digest
from tests.ipam_fixtures import seed_device, seed_org

URL = "/api/v1/devices/review/approve-all"


async def _pending(api_env, org_id: UUID, n: int) -> list[UUID]:
    return [
        await seed_device(
            api_env.admin_engine,
            org_id,
            hostname=f"DESKTOP-{i:04d}",
            review_state="pending",
            link_joined_from="10.50.0.9",
            link_joined_at=datetime.now(UTC),
        )
        for i in range(n)
    ]


async def _tech(api_env, org_id: UUID) -> dict[str, str]:
    return await api_env.headers(org_id, await api_env.create_user(org_id, "technician"))


async def _states(api_env, ids: list[UUID]) -> dict[UUID, str]:
    async with AsyncSession(api_env.admin_engine) as session:
        rows = (
            await session.execute(select(Device.id, Device.review_state).where(Device.id.in_(ids)))
        ).all()
    return {row[0]: row[1] for row in rows}


def _body(ids: list[UUID]) -> dict[str, object]:
    return {"device_ids": [str(i) for i in ids], "list_digest": review_list_digest(ids)}


def test_the_digest_is_order_and_duplicate_blind_and_is_the_consoles() -> None:
    a, b = UUID(int=2), UUID(int=1)
    assert review_list_digest([a, b, a]) == review_list_digest([b, a])
    assert review_list_digest([a, b]) == hashlib.sha256(f"{b}\n{a}".encode()).hexdigest()
    # frontend/src/modules/devices/DevicesApproveAll.test.tsx pins the same value.
    assert review_list_digest([a, b]) == (
        "7a70c782c5d30f61a8f57b905eaa11c41ec8ffeafaa3a0e99c4fca60044a28d4"
    )


async def test_it_approves_the_listed_pending_machines_one_audit_each(api_env) -> None:
    org = await seed_org(api_env.admin_engine, "aa")
    listed = await _pending(api_env, org.org_id, 3)
    response = await api_env.client.post(
        URL, json=_body(listed), headers=await _tech(api_env, org.org_id)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["approved"] == 3 and body["not_pending"] == 0 and body["still_waiting"] == 0
    assert set((await _states(api_env, listed)).values()) == {"approved"}
    async with AsyncSession(api_env.admin_engine) as session:
        audits = list(
            (
                await session.execute(
                    select(AuditEvent).where(
                        AuditEvent.org_id == org.org_id,
                        AuditEvent.action == "device.review_approved",
                    )
                )
            ).scalars()
        )
    assert sorted(a.target_id for a in audits) == sorted(str(i) for i in listed)
    assert {a.meta["bulk_id"] for a in audits} == {body["bulk_id"]}


async def test_a_machine_that_joined_after_the_list_stays_pending_and_is_counted(api_env) -> None:
    org = await seed_org(api_env.admin_engine, "aa")
    listed = await _pending(api_env, org.org_id, 2)
    [late] = await _pending(api_env, org.org_id, 1)
    body = (
        await api_env.client.post(URL, json=_body(listed), headers=await _tech(api_env, org.org_id))
    ).json()
    assert body["approved"] == 2 and body["still_waiting"] == 1
    assert (await _states(api_env, [late]))[late] == "pending"


async def test_a_listed_machine_no_longer_pending_is_left_alone_and_counted(api_env) -> None:
    org = await seed_org(api_env.admin_engine, "aa")
    listed = await _pending(api_env, org.org_id, 2)
    headers = await _tech(api_env, org.org_id)
    rejected = await api_env.client.post(
        f"/api/v1/devices/{listed[0]}/review/reject", headers=headers
    )
    assert rejected.status_code == 200
    body = (await api_env.client.post(URL, json=_body(listed), headers=headers)).json()
    assert body["approved"] == 1 and body["not_pending"] == 1
    assert (await _states(api_env, listed))[listed[0]] == "rejected"


async def test_another_organisations_machine_is_never_approved(api_env) -> None:
    mine = await seed_org(api_env.admin_engine, "aa")
    theirs = await seed_org(api_env.admin_engine, "bb")
    [own] = await _pending(api_env, mine.org_id, 1)
    [foreign] = await _pending(api_env, theirs.org_id, 1)
    body = (
        await api_env.client.post(
            URL, json=_body([own, foreign]), headers=await _tech(api_env, mine.org_id)
        )
    ).json()
    assert body["approved"] == 1 and body["not_pending"] == 1
    assert (await _states(api_env, [foreign]))[foreign] == "pending"


async def test_a_digest_that_is_not_the_lists_is_refused_before_anything_changes(api_env) -> None:
    org = await seed_org(api_env.admin_engine, "aa")
    listed = await _pending(api_env, org.org_id, 2)
    body = {"device_ids": [str(i) for i in listed], "list_digest": review_list_digest(listed[:1])}
    response = await api_env.client.post(URL, json=body, headers=await _tech(api_env, org.org_id))
    assert response.status_code == 422
    assert "not the list that was shown" in response.json()["detail"]
    assert set((await _states(api_env, listed)).values()) == {"pending"}


async def test_more_than_the_limit_is_refused(api_env) -> None:
    org = await seed_org(api_env.admin_engine, "aa")
    ids = [uuid4() for _ in range(APPROVE_ALL_MAX + 1)]
    response = await api_env.client.post(
        URL, json=_body(ids), headers=await _tech(api_env, org.org_id)
    )
    assert response.status_code == 422
    assert "at most 1000" in response.json()["detail"]


async def test_it_needs_devices_review(api_env) -> None:
    org = await seed_org(api_env.admin_engine, "aa")
    listed = await _pending(api_env, org.org_id, 1)
    reader = await api_env.headers(org.org_id, await api_env.create_user(org.org_id, "read-only"))
    assert (await api_env.client.post(URL, json=_body(listed), headers=reader)).status_code == 403
    assert (await _states(api_env, listed))[listed[0]] == "pending"
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_device_review_approve_all.py -q`
Expected: collection error, `ImportError: cannot import name 'APPROVE_ALL_MAX' from 'app.services.device_review'`.

- [ ] **Step 3: The service**

`backend/app/services/device_review.py`:

Replace

```python
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
```

with

```python
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
```

After `REMOVAL_INCOMPLETE_EVENT = "device.removal_incomplete"` add:

```python
#: The most machines one Approve all may name (GPO MSI spec 2026-09-30 §2.5).
APPROVE_ALL_MAX = 1000
```

Immediately before `class NothingToAcknowledge(ValueError):` add:

```python
class ApproveAllRefused(ValueError):
    """An Approve all that names too many machines, or whose digest is not the
    digest of the list it names. The route answers 422 with the message."""


```

Replace the whole of `async def approve(...)` (from its `async def approve(` line through its `return device`) with:

```python
async def _approve_row(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    device: Device,
    now: datetime,
    bulk_id: UUID | None = None,
) -> None:
    """One approval: the state change and its own audit record. Approve all
    writes one of these per machine, all carrying the same `bulk_id`."""
    device.review_state = "approved"
    session.add(device)
    metadata: dict[str, Any] = {
        "hostname": device.hostname,
        "link_joined_from": str(device.link_joined_from) if device.link_joined_from else None,
        "approved_at": now.isoformat(),
    }
    if bulk_id is not None:
        metadata["bulk_id"] = str(bulk_id)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="device.review_approved",
        target_type="device",
        target_id=str(device.id),
        metadata=metadata,
    )


async def approve(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, device_id: UUID, now: datetime
) -> Device:
    device = await _pending(session, org_id, device_id)
    await _approve_row(session, org_id=org_id, actor_id=actor_id, device=device, now=now)
    await session.flush()
    return device


def review_list_digest(device_ids: Iterable[UUID]) -> str:
    """SHA-256, hex, of the sorted, de-duplicated ids, one per line. The
    console computes the same over the list it showed (`reviewListDigest`), so
    an Approve all confirms THAT list."""
    lines = "\n".join(sorted({str(device_id) for device_id in device_ids}))
    return hashlib.sha256(lines.encode()).hexdigest()


@dataclass(frozen=True)
class ApproveAllOutcome:
    bulk_id: UUID
    #: Listed and still pending: approved now.
    approved: int
    #: Listed, but no longer pending (someone approved or rejected it since),
    #: or not a device of this organisation: left as it is.
    not_pending: int
    #: Pending and NOT listed -- joined after the list was shown: still waiting.
    still_waiting: int


async def approve_all(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    device_ids: list[UUID],
    list_digest: str,
    now: datetime,
) -> ApproveAllOutcome:
    """Approve every LISTED machine that is still pending (GPO MSI spec
    2026-09-30 §2.5). Never "every pending machine": one that joined after the
    dialog opened is not in the list, so it stays pending and is counted."""
    listed = set(device_ids)
    if not listed:
        raise ApproveAllRefused("the list names no computers")
    if len(listed) > APPROVE_ALL_MAX:
        raise ApproveAllRefused(f"at most {APPROVE_ALL_MAX} computers can be approved at once")
    if review_list_digest(listed) != list_digest:
        raise ApproveAllRefused(
            "the list sent is not the list that was shown; open Approve all again"
        )
    rows = (
        (
            await session.execute(
                select(Device)
                .where(
                    Device.org_id == org_id,
                    Device.id.in_(listed),
                    Device.review_state == "pending",
                )
                # One order for every caller, so two Approve alls over
                # overlapping lists cannot deadlock on each other's rows.
                .order_by(Device.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    bulk_id = uuid4()
    for device in rows:
        await _approve_row(
            session, org_id=org_id, actor_id=actor_id, device=device, now=now, bulk_id=bulk_id
        )
    await session.flush()
    waiting = (
        await session.execute(
            select(func.count())
            .select_from(Device)
            .where(Device.org_id == org_id, Device.review_state == "pending")
        )
    ).scalar_one()
    return ApproveAllOutcome(
        bulk_id=bulk_id,
        approved=len(rows),
        not_pending=len(listed) - len(rows),
        still_waiting=int(waiting),
    )
```

The single-device `approve` still writes exactly the metadata it wrote before (`bulk_id` is added only by Approve all), so `tests/test_device_review_api.py` is unchanged and must still pass.

- [ ] **Step 4: The route**

`backend/app/api/v1/devices.py` — immediately before `@router.post("/{device_id}/review/approve", response_model=DeviceOut)`:

```python
class ApproveAllIn(BaseModel):
    """The list the Approve all dialog showed (GPO MSI spec 2026-09-30 §2.5)."""

    model_config = ConfigDict(extra="forbid")

    device_ids: list[UUID] = Field(description="The computers the dialog listed")
    list_digest: str = Field(
        min_length=64,
        max_length=64,
        description="SHA-256 hex of the sorted ids, one per line, as the dialog computed it",
    )


class ApproveAllOut(BaseModel):
    """What an Approve all did."""

    bulk_id: UUID = Field(description="Carried by every device.review_approved record it wrote")
    approved: int = Field(description="Listed computers approved now")
    not_pending: int = Field(description="Listed computers that were no longer waiting")
    still_waiting: int = Field(description="Computers still waiting: they joined after the list")


@router.post("/review/approve-all", response_model=ApproveAllOut)
async def approve_all_devices(
    body: ApproveAllIn,
    request: Request,
    ctx: AuthContext = Depends(require_permission("devices.review")),
) -> ApproveAllOut:
    """Approve every listed computer that is still waiting for review. A
    computer that joined after the list was shown is left waiting."""
    async with org_session(ctx.org_id, engine=request.app.state.app_engine) as session:
        try:
            outcome = await review_svc.approve_all(
                session,
                org_id=ctx.org_id,
                actor_id=ctx.user_id,
                device_ids=body.device_ids,
                list_digest=body.list_digest,
                now=datetime.now(UTC),
            )
        except review_svc.ApproveAllRefused as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    return ApproveAllOut(
        bulk_id=outcome.bulk_id,
        approved=outcome.approved,
        not_pending=outcome.not_pending,
        still_waiting=outcome.still_waiting,
    )


```

`/review/approve-all` and `/{device_id}/review/approve` cannot collide: they have two and three path segments after `/devices`.

- [ ] **Step 5: Run the tests to see them pass**

Run: `cd backend && .venv/bin/python -m pytest tests/test_device_review_approve_all.py tests/test_device_review_api.py tests/test_route_gating.py tests/test_route_uniqueness.py tests/test_ai_tool_surface.py -q`
Expected: all pass (`8 passed` in the new file; the review API file unchanged and green).

- [ ] **Step 6: Lint and type-check**

Run: `cd backend && .venv/bin/ruff check app tests && .venv/bin/mypy app`
Expected: no findings.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/device_review.py backend/app/api/v1/devices.py backend/tests/test_device_review_approve_all.py
git commit -m "feat(review): Approve all approves the listed machines still waiting" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---

### Task 5: Agent — `--msi-uninstall`, the signed uninstall's removal run in place

**Files:**
- Modify: `agent/internal/capabilities/uninstall/helper.go` (extract `removeServiceAndData` from `RunHelper`)
- Create: `agent/internal/capabilities/uninstall/msi.go`
- Create: `agent/internal/capabilities/uninstall/msi_windows.go`
- Create: `agent/internal/capabilities/uninstall/msi_other.go`
- Create: `agent/internal/capabilities/uninstall/msi_test.go`

**Interfaces:**
- Consumes: the package's `System`, `Targets`, `For(goos, binary, dataDir)`, `checkTargets`, `ownsDataDir`, `identityFile`, `hardenProcess`, `platformSystem`; `helper_test.go`'s `fakeSystem`, `layout(t)`, `gone(t, …)`.
- Produces: `const MsiUninstallArg = "--msi-uninstall"`; `RunForMSI(ctx context.Context, sys System, t Targets) error`; `MsiMain() int` (Windows: the removal; elsewhere: exit 2); unexported `removeServiceAndData(ctx, sys, t, note func(error)) bool`, `msiTargets(programData, binary string) Targets`. Task 6 routes `MsiUninstallArg` to `MsiMain` in `cmd/agent`.
- Imports: none added. `imports_test.go`'s pinned lists (linux, windows, darwin) are unchanged, and that is the idle-budget evidence for this task.

- [ ] **Step 1: Write the failing tests**

`agent/internal/capabilities/uninstall/msi_test.go`:

```go
package uninstall

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// An MSI removal (GPO MSI spec §2.3): the service and the data go; the binary
// and its directory stay for Windows Installer, which owns them; nothing waits
// and nothing is scheduled for the next boot.
func TestAnMsiRemovalLeavesTheBinaryToWindowsInstaller(t *testing.T) {
	tg := layout(t)
	sys := &fakeSystem{}
	if err := RunForMSI(context.Background(), sys, tg); err != nil {
		t.Fatal(err)
	}
	if want := []string{"stop its-agent", "purge", "delete its-agent"}; !reflect.DeepEqual(sys.calls, want) {
		t.Fatalf("calls = %v, want %v (no wait, no boot-time delete, no helper exit)", sys.calls, want)
	}
	gone(t, tg.DataDir, tg.Remove[0])
	if _, err := os.Stat(tg.Binary); err != nil {
		t.Fatalf("the binary is the MSI's to remove, and it went: %v", err)
	}
}

// Every step still runs when one fails, and the failure is reported.
func TestAnMsiRemovalGoesOnPastAFailureAndReportsIt(t *testing.T) {
	tg := layout(t)
	sys := &fakeSystem{stopErr: os.ErrPermission}
	if err := RunForMSI(context.Background(), sys, tg); err == nil {
		t.Fatal("a failed stop was not reported")
	}
	gone(t, tg.DataDir)
}

// A data directory that holds no identity keeps everything but the named
// credentials, exactly as the signed uninstall does (review M3).
func TestAnMsiRemovalLeavesADataDirectoryThatIsNotAnAgents(t *testing.T) {
	tg := layout(t)
	if err := os.Remove(filepath.Join(tg.DataDir, "identity.json")); err != nil {
		t.Fatal(err)
	}
	if err := RunForMSI(context.Background(), &fakeSystem{}, tg); err == nil {
		t.Fatal("removing only the credentials was not reported")
	}
	if _, err := os.Stat(filepath.Join(tg.DataDir, "update")); err != nil {
		t.Fatalf("a directory that may not be an agent's was emptied: %v", err)
	}
	gone(t, filepath.Join(tg.DataDir, "agent.key"))
}

func TestAnMsiRemovalRefusesTargetsThatAreNotAnAgent(t *testing.T) {
	tg := layout(t)
	tg.Binary = filepath.Join(filepath.Dir(tg.Binary), "explorer.exe")
	sys := &fakeSystem{}
	if err := RunForMSI(context.Background(), sys, tg); err == nil || len(sys.calls) != 0 {
		t.Fatalf("err=%v calls=%v; targets that are not an agent must be refused before anything runs", err, sys.calls)
	}
}

func TestMsiTargetsAreTheWindowsLayout(t *testing.T) {
	got := msiTargets(`D:\ProgramData\`, `C:\Program Files\ITSuite\its-agent.exe`)
	if got.DataDir != `D:\ProgramData\ITSuite\agent` || got.Binary != `C:\Program Files\ITSuite\its-agent.exe` ||
		got.ServiceName != "its-agent" {
		t.Fatalf("msiTargets = %+v", got)
	}
	if def := msiTargets("", `C:\x\its-agent.exe`); def.DataDir != `C:\ProgramData\ITSuite\agent` {
		t.Fatalf("default ProgramData: %+v", def)
	}
}
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd agent && go test ./internal/capabilities/uninstall/ -run 'Msi' -count=1`
Expected: build failure, `undefined: RunForMSI` and `undefined: msiTargets`.

- [ ] **Step 3: Extract the shared removal from `RunHelper`**

`agent/internal/capabilities/uninstall/helper.go` — in `RunHelper`, replace this block:

```go
	wait(HelperDelay)
	var errs []error
	note := func(err error) {
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			errs = append(errs, err)
		}
	}
	// Decided BEFORE anything is removed: whether this data directory is an
	// agent's (review M3). The credentials below are not identity.json, so
	// removing them does not change the answer, but reading it first means
	// nothing this function does can.
	ownsData := ownsDataDir(t)
	note(sys.StopService(ctx, t.ServiceName))
	purged, err := sys.PurgePackage(ctx)
	note(err)
	if !purged {
		note(sys.DeleteService(ctx, t.ServiceName))
	}
	for _, name := range t.Credentials {
		note(os.Remove(filepath.Join(t.DataDir, name)))
	}
	if ownsData {
		note(os.RemoveAll(t.DataDir))
	} else {
		note(fmt.Errorf("uninstall: %s holds no %s, so it may not be an agent's data directory; "+
			"only the credentials in it were removed", t.DataDir, identityFile))
	}
	for _, p := range t.Remove {
		note(os.RemoveAll(p))
	}
	if !purged {
```

with:

```go
	wait(HelperDelay)
	var errs []error
	note := func(err error) {
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			errs = append(errs, err)
		}
	}
	purged := removeServiceAndData(ctx, sys, t, note)
	if !purged {
```

and insert immediately before `// HelperMain is `its-agent --uninstall-helper`.`:

```go
// removeServiceAndData is what every removal does, in this order: stop the
// service, purge the package or delete the service, remove the credentials BY
// NAME, then the data directory -- recursively only when it is an agent's --
// and the service definitions and config in t.Remove. Every step runs; the
// failures go to note. It reports whether a package manager removed the agent
// (and with it the binary). RunHelper and RunForMSI both call it, so the two
// removals cannot drift apart.
func removeServiceAndData(ctx context.Context, sys System, t Targets, note func(error)) bool {
	// Decided BEFORE anything is removed: whether this data directory is an
	// agent's (review M3). The credentials below are not identity.json, so
	// removing them does not change the answer, but reading it first means
	// nothing this function does can.
	ownsData := ownsDataDir(t)
	note(sys.StopService(ctx, t.ServiceName))
	purged, err := sys.PurgePackage(ctx)
	note(err)
	if !purged {
		note(sys.DeleteService(ctx, t.ServiceName))
	}
	for _, name := range t.Credentials {
		note(os.Remove(filepath.Join(t.DataDir, name)))
	}
	if ownsData {
		note(os.RemoveAll(t.DataDir))
	} else {
		note(fmt.Errorf("uninstall: %s holds no %s, so it may not be an agent's data directory; "+
			"only the credentials in it were removed", t.DataDir, identityFile))
	}
	for _, p := range t.Remove {
		note(os.RemoveAll(p))
	}
	return purged
}

```

(The package's tests do not compile again until Step 4 exists; Step 5 runs the existing helper tests, which prove the extraction changed nothing.)

- [ ] **Step 4: The MSI removal**

`agent/internal/capabilities/uninstall/msi.go`:

```go
package uninstall

import (
	"context"
	"errors"
	"os"
	"strings"
)

// MsiUninstallArg is the one argument main.go routes to MsiMain. The Group
// Policy MSI's removal custom action runs the INSTALLED binary with it, as
// SYSTEM, before Windows Installer deletes the MSI's files
// (docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md §2.3,
// "Uninstall"). It carries nothing else: what to remove is this binary's own
// knowledge, exactly as for the signed agent.uninstall job.
const MsiUninstallArg = "--msi-uninstall"

// RunForMSI is agent.uninstall's removal, run in place by an MSI removal: the
// service is stopped and deleted, the credentials and the data directory
// removed (removeServiceAndData). Unlike RunHelper it does not wait -- no job
// asked for this, so there is no answer to get onto the wire first -- and it
// does not remove the binary or its directory: they are the MSI's files, the
// binary is this process's own image, and Windows Installer removes both
// right after this returns.
func RunForMSI(ctx context.Context, sys System, t Targets) error {
	if err := checkTargets(t); err != nil {
		return err
	}
	var errs []error
	note := func(err error) {
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			errs = append(errs, err)
		}
	}
	removeServiceAndData(ctx, sys, t, note)
	return errors.Join(errs...)
}

// msiTargets is what an MSI removal removes: the Windows targets for the
// installed binary and the data directory every Windows installer uses,
// %ProgramData%\ITSuite\agent (internal/install/layout). Pure, so it is
// tested on any GOOS.
func msiTargets(programData, binary string) Targets {
	if programData == "" {
		programData = `C:\ProgramData`
	}
	return For("windows", binary, strings.TrimRight(programData, `\`)+`\ITSuite\agent`)
}
```

`agent/internal/capabilities/uninstall/msi_windows.go`:

```go
//go:build windows

package uninstall

import (
	"context"
	"fmt"
	"os"
)

// MsiMain is `its-agent.exe --msi-uninstall`, run by the MSI as SYSTEM. Its
// exit code is logged by Windows Installer and otherwise ignored (the custom
// action is Return="ignore"): a removal that half-fails must still let the
// MSI go, or the machine could never be cleaned up through Group Policy.
func MsiMain() int {
	// FIRST, before anything can load a library (review I2).
	if err := hardenProcess(); err != nil {
		fmt.Fprintln(os.Stderr, "its-agent --msi-uninstall: cannot restrict the DLL search path:", err)
		return 2
	}
	binary, err := os.Executable()
	if err != nil {
		fmt.Fprintln(os.Stderr, "its-agent --msi-uninstall: cannot locate this binary:", err)
		return 2
	}
	if err := RunForMSI(context.Background(), platformSystem{}, msiTargets(os.Getenv("ProgramData"), binary)); err != nil {
		fmt.Fprintln(os.Stderr, "its-agent --msi-uninstall finished with errors:", err)
		return 1
	}
	fmt.Fprintln(os.Stderr, "its-agent --msi-uninstall: service and data removed")
	return 0
}
```

`agent/internal/capabilities/uninstall/msi_other.go`:

```go
//go:build !windows

package uninstall

import (
	"fmt"
	"os"
)

// MsiMain is Windows-only: an MSI exists for Windows alone.
func MsiMain() int {
	fmt.Fprintln(os.Stderr, "its-agent --msi-uninstall runs on Windows.")
	return 2
}
```

- [ ] **Step 5: Run the tests to see them pass, on every OS the package builds for**

Run: `cd agent && go test ./internal/capabilities/uninstall/ -count=1 && GOOS=windows go vet ./internal/capabilities/uninstall/ && GOOS=windows GOARCH=arm64 go vet ./internal/capabilities/uninstall/ && GOOS=darwin go vet ./internal/capabilities/uninstall/ && GOOS=windows go test -c -o /dev/null ./internal/capabilities/uninstall`
Expected: `ok`, then no output from the vets and the Windows test compile. `TestTheHelperWaitsThenStopsThenRemovesEverything` and its neighbours passing prove the extraction changed nothing for the signed uninstall; `TestUninstallImportsArePinnedPerOS` passing is the check that no import was added.

- [ ] **Step 6: Format and static checks**

Run: `cd agent && test -z "$(gofmt -l .)" && "$(go env GOPATH)/bin/staticcheck" ./internal/capabilities/uninstall/ && GOOS=windows "$(go env GOPATH)/bin/staticcheck" ./internal/capabilities/uninstall/`
Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add agent/internal/capabilities/uninstall/
git commit -m "feat(agent): --msi-uninstall runs agent.uninstall's removal for the MSI" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---

### Task 6: Agent — `--msi-install`, and routing both MSI arguments

**Files:**
- Modify: `agent/internal/install/install_windows.go` (split `apply` into `writeToken` and `registerAndStart`; add `applyInPlace`)
- Create: `agent/internal/install/msi.go`, `agent/internal/install/msi_windows.go`, `agent/internal/install/msi_other.go`
- Create: `agent/internal/install/msi_test.go`, `agent/internal/install/msi_windows_test.go`
- Modify: `agent/internal/install/dormant_test.go`
- Modify: `agent/cmd/agent/main.go`, `agent/cmd/agent/main_test.go`
- Modify: `.github/workflows/ci.yml` (jobs `agent`, `agent-windows`, `agent-macos`: step `idle budget — the numbers, in the log`)

**Interfaces:**
- Consumes: `NewPlan(programFiles, programData, console) Plan` (fields `Target`, `InstallDir`, `DataDir`, `TokenPath`, `ServiceName`, `Environment`), `Link{Console, Code}`, `codeLength`, `validCode`, `layout.CheckEnrollOrigin`, `prepareDirs`, `stop`; `dirs_windows_test.go`'s `fakeDirs`, `dirCall`, `dirsPlan`; Task 5's `uninstall.MsiUninstallArg`, `uninstall.MsiMain`.
- Produces: `install.MsiInstallArg = "--msi-install"`, `install.MsiSettingsFile = "msi-enroll.json"`, `install.RunMsi(exe string) int`; unexported `msiInstall(sys msiSystem, exe, programFiles, programData string) error`, `parseMsiSettings([]byte) (Link, error)`, `applyInPlace(p Plan, code string) error`, `writeToken(p Plan, code string) error`, `registerAndStart(m *mgr.Mgr, existing *mgr.Service, p Plan) error`; `cmd/agent`'s `msiInstallRequested(args []string) bool`, `msiUninstallRequested(args []string) bool`.
- Imports: none added to `internal/install` on any OS (`imports_test.go`'s `wantImports` and `wantUnixImports` are unchanged — that is the idle-budget proof).

- [ ] **Step 1: Write the failing tests**

`agent/internal/install/msi_test.go`:

```go
package install

import (
	"errors"
	"os"
	"reflect"
	"strings"
	"testing"
)

const (
	msiExe          = `C:\Program Files\ITSuite\its-agent.exe`
	msiSettingsPath = `C:\Program Files\ITSuite\msi-enroll.json`
	msiCode         = "AbCdEfGhIjKlMnOpQrStUvWxYz012_-9"
)

var msiGood = []byte(`{"console":"https://itms.example.com","code":"` + msiCode + `"}`)

type fakeMsi struct {
	elevated, enrolled             bool
	settings                       []byte
	readErr, installErr, removeErr error
	calls                          []string
	plan                           Plan
	code                           string
}

func (f *fakeMsi) Elevated() bool { f.calls = append(f.calls, "elevated?"); return f.elevated }
func (f *fakeMsi) Enrolled(p Plan) bool {
	f.calls = append(f.calls, "enrolled? "+p.DataDir)
	return f.enrolled
}
func (f *fakeMsi) EnsureRunning(name string) error {
	f.calls = append(f.calls, "ensure-running "+name)
	return nil
}
func (f *fakeMsi) ReadSettings(path string, limit int64) ([]byte, error) {
	f.calls = append(f.calls, "read "+path)
	if limit != maxMsiSettingsBytes {
		return nil, errors.New("unbounded read")
	}
	return f.settings, f.readErr
}
func (f *fakeMsi) Install(p Plan, code string) error {
	f.calls = append(f.calls, "install")
	f.plan, f.code = p, code
	return f.installErr
}
func (f *fakeMsi) Remove(path string) error {
	f.calls = append(f.calls, "remove "+path)
	return f.removeErr
}

func TestMsiInstallJoinsThroughTheSettingsFileAndDeletesIt(t *testing.T) {
	sys := &fakeMsi{elevated: true, settings: msiGood}
	if err := msiInstall(sys, msiExe, `C:\Program Files`, `C:\ProgramData`); err != nil {
		t.Fatal(err)
	}
	want := []string{"elevated?", `enrolled? C:\ProgramData\ITSuite\agent`, "read " + msiSettingsPath, "install", "remove " + msiSettingsPath}
	if !reflect.DeepEqual(sys.calls, want) {
		t.Fatalf("calls = %v\nwant %v", sys.calls, want)
	}
	if sys.code != msiCode || sys.plan.Target != msiExe {
		t.Fatalf("installed %q to %q", sys.code, sys.plan.Target)
	}
	env := strings.Join(sys.plan.Environment, "|")
	if !strings.Contains(env, "ITS_AGENT_ENROLL_URL=https://itms.example.com") || strings.Contains(env, msiCode) {
		t.Fatalf("service environment = %q", env)
	}
}

func TestMsiInstallRefusesUnlessElevated(t *testing.T) {
	sys := &fakeMsi{settings: msiGood}
	err := msiInstall(sys, msiExe, `C:\Program Files`, `C:\ProgramData`)
	if err == nil || !strings.Contains(err.Error(), "SYSTEM") || !reflect.DeepEqual(sys.calls, []string{"elevated?"}) {
		t.Fatalf("err=%v calls=%v", err, sys.calls)
	}
}

func TestMsiInstallSetsUpOnlyTheBinaryTheMsiPlaced(t *testing.T) {
	sys := &fakeMsi{elevated: true, settings: msiGood}
	err := msiInstall(sys, `C:\Users\Public\its-agent.exe`, `C:\Program Files`, `C:\ProgramData`)
	if err == nil || len(sys.calls) != 1 {
		t.Fatalf("err=%v calls=%v; a binary outside Program Files\\ITSuite must be refused before anything is read", err, sys.calls)
	}
	// Windows paths are case-insensitive, and the MSI may launch either way.
	sys = &fakeMsi{elevated: true, settings: msiGood}
	if err := msiInstall(sys, `c:\program files\itsuite\ITS-AGENT.EXE`, `C:\Program Files`, `C:\ProgramData`); err != nil {
		t.Fatal(err)
	}
}

func TestMsiInstallChangesNothingWhenAlreadyEnrolled(t *testing.T) {
	sys := &fakeMsi{elevated: true, enrolled: true}
	if err := msiInstall(sys, msiExe, `C:\Program Files`, `C:\ProgramData`); err != nil {
		t.Fatal(err)
	}
	want := []string{"elevated?", `enrolled? C:\ProgramData\ITSuite\agent`, "remove " + msiSettingsPath, "ensure-running its-agent"}
	if !reflect.DeepEqual(sys.calls, want) {
		t.Fatalf("calls = %v\nwant %v", sys.calls, want)
	}
}

func TestMsiInstallFailsWhenTheSettingsFileCannotBeDeleted(t *testing.T) {
	sys := &fakeMsi{elevated: true, settings: msiGood, removeErr: os.ErrPermission}
	err := msiInstall(sys, msiExe, `C:\Program Files`, `C:\ProgramData`)
	if err == nil || !strings.Contains(err.Error(), "could not be deleted") {
		t.Fatalf("err = %v", err)
	}
	// An already-deleted file is not a failure.
	sys = &fakeMsi{elevated: true, settings: msiGood, removeErr: os.ErrNotExist}
	if err := msiInstall(sys, msiExe, `C:\Program Files`, `C:\ProgramData`); err != nil {
		t.Fatal(err)
	}
}

func TestMsiInstallStopsAtAFailedInstallWithoutDeletingTheSettings(t *testing.T) {
	sys := &fakeMsi{elevated: true, settings: msiGood, installErr: errors.New("refused by the fake")}
	if err := msiInstall(sys, msiExe, `C:\Program Files`, `C:\ProgramData`); err == nil {
		t.Fatal("a failed install was reported as success")
	}
	if last := sys.calls[len(sys.calls)-1]; last != "install" {
		t.Fatalf("after a failed install: %v", sys.calls)
	}
}

func TestMsiSettingsAreValidated(t *testing.T) {
	good, err := parseMsiSettings(msiGood)
	if err != nil || good != (Link{Console: "https://itms.example.com", Code: msiCode}) {
		t.Fatalf("good settings: %+v, %v", good, err)
	}
	if l, err := parseMsiSettings([]byte(`{"console":"https://itms.example.com:8443","code":"` + msiCode + `"}`)); err != nil || l.Console != "https://itms.example.com:8443" {
		t.Fatalf("a port: %+v, %v", l, err)
	}
	for name, bad := range map[string]string{
		"not json":        `console=https://itms.example.com`,
		"empty":           ``,
		"another member":  `{"console":"https://itms.example.com","code":"` + msiCode + `","url":"x"}`,
		"two values":      string(msiGood) + ` {}`,
		"http":            `{"console":"http://itms.example.com","code":"` + msiCode + `"}`,
		"a path":          `{"console":"https://itms.example.com/i/x","code":"` + msiCode + `"}`,
		"no console":      `{"code":"` + msiCode + `"}`,
		"short code":      `{"console":"https://itms.example.com","code":"abc"}`,
		"long code":       `{"console":"https://itms.example.com","code":"` + msiCode + `x"}`,
		"bad character":   `{"console":"https://itms.example.com","code":"` + msiCode[:31] + `!"}`,
		"code not string": `{"console":"https://itms.example.com","code":42}`,
		"too large":       `{"console":"https://itms.example.com","code":"` + msiCode + `"}` + strings.Repeat(" ", maxMsiSettingsBytes),
	} {
		_, err := parseMsiSettings([]byte(bad))
		if err == nil {
			t.Errorf("%s: accepted %q", name, bad)
			continue
		}
		if strings.Contains(err.Error(), msiCode[:20]) {
			t.Errorf("%s: the error carries the code: %v", name, err)
		}
	}
}
```

`agent/internal/install/msi_windows_test.go`:

```go
//go:build windows

package install

import (
	"os"
	"reflect"
	"testing"
)

// The MSI path takes the same parent-first ownership path as the link
// installer (2026-09-29): with %ProgramData%\ITSuite refused, nothing under it
// is created or touched and no token file is written. Runs in the
// agent-windows CI job, which is elevated: applyInPlace connects to the real
// Service Control Manager, finds no its-agent service on the runner, and
// stops at the faked directory refusal.
func TestTheMsiPathSecuresTheDataDirectorysParentFirst(t *testing.T) {
	calls, _ := fakeDirs(t, `C:\ProgramData\ITSuite`)
	if err := applyInPlace(dirsPlan, msiCode); err == nil {
		t.Fatal("applyInPlace went on after the parent was refused")
	}
	want := []dirCall{
		{"check", `C:\Program Files\ITSuite`, true},
		{"mkdir", `C:\Program Files\ITSuite`, false},
		{"secure", `C:\ProgramData\ITSuite`, true},
	}
	if !reflect.DeepEqual(*calls, want) {
		t.Fatalf("directory operations = %+v\nwant %+v", *calls, want)
	}
	if _, err := os.Stat(dirsPlan.TokenPath); !os.IsNotExist(err) {
		t.Fatalf("a token file exists after a refused parent: %v", err)
	}
}
```

`agent/internal/install/dormant_test.go` — the scan must name the MSI mode's files, so a rename cannot quietly take them out of it. Replace

```go
	fset := token.NewFileSet()
	checked := 0
	for _, e := range entries {
		name := e.Name()
		if !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
```

with

```go
	fset := token.NewFileSet()
	checked := 0
	scanned := map[string]bool{}
	for _, e := range entries {
		name := e.Name()
		if !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		scanned[name] = true
```

and replace

```go
	if checked < 5 {
		t.Fatalf("checked %d files; the scan is not looking at this package", checked)
	}
```

with

```go
	if checked < 5 {
		t.Fatalf("checked %d files; the scan is not looking at this package", checked)
	}
	// The Group Policy MSI's mode (GPO MSI spec 2026-09-30 §2.3) is held to
	// the same rule, on every OS it builds for: named, so a rename cannot
	// quietly take it out of the scan.
	for _, name := range []string{"msi.go", "msi_windows.go", "msi_other.go"} {
		if !scanned[name] {
			t.Errorf("%s was not scanned; the MSI mode must have nothing to initialise either", name)
		}
	}
```

Append to `agent/cmd/agent/main_test.go`:

```go

// The MSI's two custom actions pass exactly one argument each, and nothing
// else is either mode: the service -- no arguments -- never reaches them.
func TestMsiModesAreExactlyTheirOneArgument(t *testing.T) {
	for _, tc := range []struct {
		args               []string
		install, uninstall bool
	}{
		{[]string{"--msi-install"}, true, false},
		{[]string{"--msi-uninstall"}, false, true},
		{nil, false, false},
		{[]string{}, false, false},
		{[]string{"--msi-install", "x"}, false, false},
		{[]string{"--msi-uninstall", "--msi-install"}, false, false},
		{[]string{"install"}, false, false},
		{[]string{"--elevated-install"}, false, false},
		{[]string{"--MSI-INSTALL"}, false, false},
	} {
		if got := msiInstallRequested(tc.args); got != tc.install {
			t.Errorf("msiInstallRequested(%v) = %v, want %v", tc.args, got, tc.install)
		}
		if got := msiUninstallRequested(tc.args); got != tc.uninstall {
			t.Errorf("msiUninstallRequested(%v) = %v, want %v", tc.args, got, tc.uninstall)
		}
	}
}
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd agent && go test ./internal/install/ ./cmd/agent/ -count=1`
Expected: build failures — `undefined: msiInstall`, `undefined: parseMsiSettings`, `undefined: maxMsiSettingsBytes` (install) and `undefined: msiInstallRequested` (cmd/agent).

- [ ] **Step 3: Split `apply` so the MSI path runs the same steps**

`agent/internal/install/install_windows.go` — replace everything from the line `	// Never written INTO an existing file (review I4): a pre-created` down to (not including) the line `// prepareDirs creates or secures the three directories, PARENT FIRST.` — that is, the token-file block, the stop, the rename, the service creation, environment, recovery actions and start, and the closing `}` of `apply` — with:

```go
	if err := writeToken(p, code); err != nil {
		return err
	}
	if existing != nil {
		if err := stop(existing); err != nil {
			return err
		}
	}
	if err := renameWithRetry(staged, p.Target); err != nil {
		return err
	}
	return registerAndStart(m, existing, p)
}

// applyInPlace is apply for a binary that is ALREADY at p.Target, placed there
// by the Group Policy MSI (GPO MSI spec 2026-09-30 §2.3): the same
// directories (parent first), token file, service, environment, recovery
// actions and start -- without staging a copy or asking the console to vouch
// for it. The MSI wraps only bytes whose SHA-256 and release signature the
// server checked (app/modules/agent_distribution/msi.py), and a binary
// checking its own bytes would prove nothing.
func applyInPlace(p Plan, code string) error {
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("open the Service Control Manager: %w", err)
	}
	defer m.Disconnect()
	existing, err := m.OpenService(p.ServiceName) // nil when there is none
	if err == nil {
		defer existing.Close()
	}
	if err := prepareDirs(p, existing == nil); err != nil {
		return err
	}
	if err := writeToken(p, code); err != nil {
		return err
	}
	if existing != nil {
		if err := stop(existing); err != nil {
			return err
		}
	}
	return registerAndStart(m, existing, p)
}

// writeToken creates the token file holding the code. Never written INTO an
// existing file (review I4): a pre-created enroll-token keeps its creator's
// owner and ACL. Removed, then created O_EXCL, inheriting the data
// directory's DACL. No BOM, no newline.
func writeToken(p Plan, code string) error {
	if err := os.Remove(p.TokenPath); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("remove a previous token file: %w", err)
	}
	tf, err := os.OpenFile(p.TokenPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return fmt.Errorf("create the token file: %w", err)
	}
	if _, err := tf.WriteString(code); err != nil {
		tf.Close()
		return fmt.Errorf("write the token file: %w", err)
	}
	if err := tf.Close(); err != nil {
		return fmt.Errorf("write the token file: %w", err)
	}
	return nil
}

// registerAndStart creates the service when there is none (existing nil),
// then sets its environment and recovery actions and starts it.
func registerAndStart(m *mgr.Mgr, existing *mgr.Service, p Plan) error {
	s := existing
	if s == nil {
		// No arguments on the service command line: CreateService quotes the
		// path and appends nothing.
		// ErrorControl is New-Service's default, which install.ps1 relies on;
		// mgr.Config's zero value would be SERVICE_ERROR_IGNORE instead.
		created, err := m.CreateService(p.ServiceName, p.Target, mgr.Config{
			StartType:    mgr.StartAutomatic,
			ErrorControl: mgr.ErrorNormal,
			DisplayName:  p.DisplayName,
			Description:  p.Description,
		})
		if err != nil {
			return fmt.Errorf("register the service: %w", err)
		}
		defer created.Close()
		s = created
	}

	key, err := registry.OpenKey(registry.LOCAL_MACHINE,
		`SYSTEM\CurrentControlSet\Services\`+p.ServiceName, registry.SET_VALUE)
	if err != nil {
		return fmt.Errorf("open the service key: %w", err)
	}
	err = key.SetStringsValue("Environment", p.Environment)
	key.Close()
	if err != nil {
		return fmt.Errorf("write the service environment: %w", err)
	}

	actions := make([]mgr.RecoveryAction, 0, len(p.RecoveryDelays))
	for _, d := range p.RecoveryDelays {
		actions = append(actions, mgr.RecoveryAction{Type: mgr.ServiceRestart, Delay: d})
	}
	if err := s.SetRecoveryActions(actions, p.RecoveryResetS); err != nil {
		return fmt.Errorf("set the service recovery actions: %w", err)
	}
	if err := s.Start(); err != nil && !errors.Is(err, windows.ERROR_SERVICE_ALREADY_RUNNING) {
		return fmt.Errorf("start the service: %w", err)
	}
	return nil
}

```

`apply` itself now reads: connect, open the existing service, `prepareDirs`, stage, `verifyFile`, `writeToken`, stop an existing service, `renameWithRetry`, `registerAndStart` — the same order as before, the same code.

- [ ] **Step 4: The MSI mode**

`agent/internal/install/msi.go`:

```go
package install

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/toeshee/it-suite/agent/internal/install/layout"
)

// THE GROUP POLICY MSI'S INSTALL MODE (docs/superpowers/specs/
// 2026-09-30-gpo-msi-and-subnet-push-design.md §2.3). Windows Installer copies
// its-agent.exe and msi-enroll.json into %ProgramFiles%\ITSuite, then runs
// ONE deferred custom action as SYSTEM, with no impersonation and no desktop:
//
//	"%ProgramFiles%\ITSuite\its-agent.exe" --msi-install
//
// The code is never on that command line: it is in msi-enroll.json, which
// this mode reads, hands to the link installer's own sequence (the token
// file), and deletes. Joining is then the install link's, unchanged: the
// allowed networks, the hourly limit, Waiting for review.
//
// IDLE BUDGET. The service never calls into this: cmd/agent routes
// MsiInstallArg by comparing a constant, and nothing here is a package-level
// value that costs anything to initialise (dormant_test.go).

// MsiInstallArg is the ONLY argument the MSI's install custom action passes.
const MsiInstallArg = "--msi-install"

// MsiSettingsFile is the settings file the MSI places beside the binary.
const MsiSettingsFile = "msi-enroll.json"

// maxMsiSettingsBytes bounds the settings read: two short strings.
const maxMsiSettingsBytes = 4096

// msiSettings is msi-enroll.json: exactly these two members.
type msiSettings struct {
	Console string `json:"console"`
	Code    string `json:"code"`
}

// parseMsiSettings requires `console` to be a bare https origin
// (layout.CheckEnrollOrigin, the rule every installed service's
// ITS_AGENT_ENROLL_URL follows) and `code` to be 32 URL-safe characters, and
// refuses anything else: another member, a second value, or more than
// maxMsiSettingsBytes. No error it returns contains the code.
func parseMsiSettings(b []byte) (Link, error) {
	if len(b) > maxMsiSettingsBytes {
		return Link{}, fmt.Errorf("%s is larger than %d bytes", MsiSettingsFile, maxMsiSettingsBytes)
	}
	dec := json.NewDecoder(strings.NewReader(string(b)))
	dec.DisallowUnknownFields()
	var s msiSettings
	if err := dec.Decode(&s); err != nil {
		return Link{}, fmt.Errorf("%s is not the MSI's settings file: %w", MsiSettingsFile, err)
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return Link{}, fmt.Errorf("%s holds more than one value", MsiSettingsFile)
	}
	if err := layout.CheckEnrollOrigin(s.Console); err != nil {
		return Link{}, fmt.Errorf("%s: %w", MsiSettingsFile, err)
	}
	if len(s.Code) != codeLength || !validCode(s.Code) {
		return Link{}, fmt.Errorf("%s does not hold an install link code", MsiSettingsFile)
	}
	return Link(s), nil
}

// msiSystem is everything --msi-install does to the machine. The real one is
// msi_windows.go; msi_test.go's fake records every call.
type msiSystem interface {
	// Elevated reports an elevated token: SYSTEM, or an elevated administrator.
	Elevated() bool
	// Enrolled reports an identity in the plan's data directory AND a
	// registered service. An identity alone -- a leftover, or one a standard
	// user planted -- is not enrolled: the full install runs, and its
	// ownership checks refuse a planted directory.
	Enrolled(p Plan) bool
	// EnsureRunning starts the named service if it is stopped.
	EnsureRunning(name string) error
	// ReadSettings reads a regular file of at most limit bytes, never through
	// a link.
	ReadSettings(path string, limit int64) ([]byte, error)
	// Install is the link installer's sequence for a binary already at
	// p.Target: directories (parent first), token file, service, start.
	Install(p Plan, code string) error
	// Remove deletes one file.
	Remove(path string) error
}

// msiInstall is --msi-install. exe is this binary's own path; programFiles and
// programData are the environment's %ProgramFiles% and %ProgramData%.
func msiInstall(sys msiSystem, exe, programFiles, programData string) error {
	// GPO deployment was the operator's decision, and SYSTEM has no desktop
	// to ask on: no question, only the privilege check.
	if !sys.Elevated() {
		return errors.New("--msi-install must run as SYSTEM or an elevated administrator; " +
			"it is the ITMS MSI's install step")
	}
	p := NewPlan(programFiles, programData, "")
	if !strings.EqualFold(exe, p.Target) {
		return fmt.Errorf("--msi-install sets up the agent the MSI placed at %s, not %s", p.Target, exe)
	}
	settings := p.InstallDir + `\` + MsiSettingsFile
	if sys.Enrolled(p) {
		// Re-assigning the GPO, an upgrade, a reboot: nothing to join. The
		// settings file still goes, and the service the MSI stopped before
		// copying files is started again.
		if err := removeSettings(sys, settings); err != nil {
			return err
		}
		return sys.EnsureRunning(p.ServiceName)
	}
	raw, err := sys.ReadSettings(settings, maxMsiSettingsBytes)
	if err != nil {
		return fmt.Errorf("read %s: %w", settings, err)
	}
	link, err := parseMsiSettings(raw)
	if err != nil {
		return err
	}
	p = NewPlan(programFiles, programData, link.Console)
	if err := sys.Install(p, link.Code); err != nil {
		return err
	}
	return removeSettings(sys, settings)
}

// removeSettings deletes msi-enroll.json. If it cannot, the install fails
// (spec §2.3): Windows Installer rolls back, and the file goes with it.
func removeSettings(sys msiSystem, path string) error {
	if err := sys.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("%s could not be deleted, so the install fails: %w", path, err)
	}
	return nil
}
```

`agent/internal/install/msi_windows.go`:

```go
//go:build windows

package install

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"
)

// RunMsi is `its-agent.exe --msi-install` (msi.go). It returns the process
// exit code: anything but 0 fails the MSI, which rolls back.
func RunMsi(exe string) int {
	// FIRST, before anything can load a library (review I2).
	if err := windows.SetDefaultDllDirectories(windows.LOAD_LIBRARY_SEARCH_SYSTEM32); err != nil {
		fmt.Fprintln(os.Stderr, "its-agent --msi-install: cannot restrict the DLL search path:", err)
		return 1
	}
	if err := msiInstall(realMsi{}, exe, os.Getenv("ProgramFiles"), os.Getenv("ProgramData")); err != nil {
		fmt.Fprintln(os.Stderr, "its-agent --msi-install:", err)
		return 1
	}
	fmt.Fprintln(os.Stderr, "its-agent --msi-install: installed; the service joins through the install link")
	return 0
}

// identityFile is config's identity file name: its presence, with the
// service registered, is what "already enrolled" means.
const identityFile = "identity.json"

// realMsi is msiSystem on Windows.
type realMsi struct{}

func (realMsi) Elevated() bool { return windows.GetCurrentProcessToken().IsElevated() }

func (realMsi) Enrolled(p Plan) bool {
	if info, err := os.Stat(filepath.Join(p.DataDir, identityFile)); err != nil || !info.Mode().IsRegular() {
		return false
	}
	m, err := mgr.Connect()
	if err != nil {
		return false
	}
	defer m.Disconnect()
	s, err := m.OpenService(p.ServiceName)
	if err != nil {
		return false
	}
	s.Close()
	return true
}

func (realMsi) EnsureRunning(name string) error {
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("open the Service Control Manager: %w", err)
	}
	defer m.Disconnect()
	s, err := m.OpenService(name)
	if err != nil {
		return fmt.Errorf("open the %s service: %w", name, err)
	}
	defer s.Close()
	st, err := s.Query()
	if err != nil {
		return fmt.Errorf("query the %s service: %w", name, err)
	}
	if st.State != svc.Stopped {
		return nil
	}
	if err := s.Start(); err != nil && !errors.Is(err, windows.ERROR_SERVICE_ALREADY_RUNNING) {
		return fmt.Errorf("start the %s service: %w", name, err)
	}
	return nil
}

func (realMsi) ReadSettings(path string, limit int64) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, fmt.Errorf("%s is not a regular file", path)
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > limit {
		return nil, fmt.Errorf("%s is larger than %d bytes", path, limit)
	}
	return b, nil
}

func (realMsi) Install(p Plan, code string) error { return applyInPlace(p, code) }

func (realMsi) Remove(path string) error { return os.Remove(path) }
```

`agent/internal/install/msi_other.go`:

```go
//go:build !windows

package install

import (
	"fmt"
	"os"
)

// RunMsi is Windows-only: the MSI exists for Windows alone.
func RunMsi(_ string) int {
	fmt.Fprintln(os.Stderr, "its-agent --msi-install runs on Windows, from the ITMS MSI.")
	return 2
}
```

- [ ] **Step 5: Route both arguments in `cmd/agent`**

`agent/cmd/agent/main.go` — immediately before `// main parses flags and hands the process to the service seam.`:

```go
// msiInstallRequested and msiUninstallRequested are the questions the service
// path asks about the Group Policy MSI's two custom actions (GPO MSI spec
// 2026-09-30 §2.3): exactly `--msi-install`, or exactly `--msi-uninstall`.
// Constants compared, nothing in internal/install or the uninstall
// capability called: the service is started with no arguments.
func msiInstallRequested(args []string) bool {
	return len(args) == 1 && args[0] == install.MsiInstallArg
}

func msiUninstallRequested(args []string) bool {
	return len(args) == 1 && args[0] == uninstall.MsiUninstallArg
}

```

and in `main()`, immediately before `	// The Windows self-installer: the file the install link served, opened`:

```go
	// The Group Policy MSI's two custom actions, run as SYSTEM by Windows
	// Installer against the installed binary. Neither runs the service seam.
	if msiUninstallRequested(os.Args[1:]) {
		os.Exit(uninstall.MsiMain())
	}
	if msiInstallRequested(os.Args[1:]) {
		exe, err := os.Executable()
		if err != nil {
			fmt.Fprintln(os.Stderr, "its-agent --msi-install: cannot locate this binary:", err)
			os.Exit(1)
		}
		os.Exit(install.RunMsi(exe))
	}
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `cd agent && go test ./internal/install/ ./internal/capabilities/uninstall/ ./cmd/agent/ -count=1 -v -run 'Msi|NothingToInitialise|ImportsArePinned|LinksNothing' 2>&1 | grep -E '^(--- |ok|FAIL)'`
Expected: every line `--- PASS` or `ok`; among them `TestMsiInstallJoinsThroughTheSettingsFileAndDeletesIt`, `TestMsiInstallChangesNothingWhenAlreadyEnrolled`, `TestMsiInstallFailsWhenTheSettingsFileCannotBeDeleted`, `TestMsiSettingsAreValidated`, `TestThisPackageHasNothingToInitialise`, `TestInstallImportsArePinned`, `TestInstallLinksNothingTheServiceDoesNotAlreadyLink`, `TestMsiModesAreExactlyTheirOneArgument`.

Run: `cd agent && GOOS=windows go test -c -o /dev/null ./internal/install && GOOS=windows go test -c -o /dev/null ./cmd/agent`
Expected: no output. (`TestTheMsiPathSecuresTheDataDirectorysParentFirst` runs in the `agent-windows` job, which is elevated: it connects to the real Service Control Manager, finds no `its-agent` service and stops at the faked refusal of `C:\ProgramData\ITSuite`.)

- [ ] **Step 7: Every OS, every check**

Run:

```bash
cd agent
test -z "$(gofmt -l .)"
go vet ./...
GOOS=windows GOARCH=amd64 go vet ./...
GOOS=windows GOARCH=arm64 go vet ./...
GOOS=darwin go vet ./...
go test ./... -count=1
"$(go env GOPATH)/bin/staticcheck" ./...
GOOS=windows "$(go env GOPATH)/bin/staticcheck" ./internal/install/ ./internal/capabilities/uninstall/ ./cmd/agent/
GOOS=darwin "$(go env GOPATH)/bin/staticcheck" ./internal/install/ ./internal/capabilities/uninstall/ ./cmd/agent/
```

Expected: no output but `ok` lines from `go test`. (`GOOS=windows staticcheck ./...` over the whole module reports pre-existing U1000 findings in `internal/capabilities/backup` and `patch`; CI runs staticcheck on Linux only. The three packages above must be clean.)

- [ ] **Step 8: The idle-budget step names the dormancy scan**

`.github/workflows/ci.yml` — the step `idle budget — the numbers, in the log` exists in three jobs (`agent`, `agent-windows`, `agent-macos`; today at lines 818, 1103 and 1154) with the same `-run` line. In all three, replace

```
            -run 'Falsifier|Idle|Allocat|Accumulat|Dormant|CostsABounded|MagicPacketCosts|Retained|LeakNoGoroutines|OpensNoSocket' \
```

with

```
            -run 'Falsifier|Idle|Allocat|Accumulat|Dormant|CostsABounded|MagicPacketCosts|Retained|LeakNoGoroutines|OpensNoSocket|NothingToInitialise|LinksNothingTheService' \
```

so the install package's dormancy scan and both packages' link-closure checks print in the budget log beside the numbers, on each OS. Then run the Linux step exactly as CI does:

```bash
cd agent
set -o pipefail
go test ./... -count=1 -v \
  -run 'Falsifier|Idle|Allocat|Accumulat|Dormant|CostsABounded|MagicPacketCosts|Retained|LeakNoGoroutines|OpensNoSocket|NothingToInitialise|LinksNothingTheService' \
  | grep -E 'falsifier|budget|one wake|magic packet|--- (PASS|FAIL|SKIP)|^(ok|FAIL)'
echo "exit=$?"
```

Expected: no `--- FAIL` and no `FAIL` line; `--- PASS: TestThisPackageHasNothingToInitialise`, `--- PASS: TestInstallLinksNothingTheServiceDoesNotAlreadyLink` and `--- PASS: TestUninstallLinksNothingTheServiceDoesNotAlreadyLink` among the lines; `exit=0`.

- [ ] **Step 9: Commit**

```bash
git add agent/internal/install/ agent/cmd/agent/main.go agent/cmd/agent/main_test.go .github/workflows/ci.yml
git commit -m "feat(agent): --msi-install joins through the MSI's settings file and deletes it" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---

### Task 7: Windows end-to-end — `TestMsiInstallWindows` on `windows-latest`

The model is `TestInstallLinkWindows` (`agent/internal/update/e2e/install_link_windows_test.go`, run by `.github/workflows/agent-update-e2e.yml`): the same PKI, gateway and enrollment server helpers, the same platform helpers (`plat.target()`, `plat.dataDir()`, `assertOwnedSDDL`, `ps`, `copyFileT`).

**Files:**
- Create: `agent/internal/update/e2e/msi_install_windows_test.go`
- Modify: `.github/workflows/agent-update-e2e.yml` (path filter; job timeout; three steps before `agent.uninstall on systemd (Linux)`)

**Interfaces:**
- Consumes: Task 1's `backend/app/services/msi_build.py` as a script; Tasks 5–6's `--msi-install` / `--msi-uninstall` in binary A (built by `internal/update/e2e/build.sh`, version `versionA = "0.0.1-e2e.1"`); harness helpers `newPKI`, `newPlatform`, `startGateway`, `startEnroll` (`enroll.url` is `https://localhost:<port>`, `enroll.requests()`), `gw.waitVersion`, `assertOwnedSDDL`, `ps`, `copyFileT`, `exe()`.
- Produces: the only evidence that the MSI installs, enrols, upgrades and removes on a real Windows Installer and SCM. **Not covered:** real Group Policy assignment at computer startup (the operator's one-OU trial, `deploy/WINDOWS-PUSH.md` §7 from Task 10) and any ARM64 machine.

- [ ] **Step 1: Write the test**

`agent/internal/update/e2e/msi_install_windows_test.go`:

```go
//go:build e2e && windows

package e2e

import (
	"crypto/ed25519"
	"crypto/rand"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
	"unicode/utf16"
)

// TestMsiInstallWindows is the Windows row of the GPO MSI spec's testing
// section (docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md):
// the MSI is built by the server's own builder (backend/app/services/
// msi_build.py) with a real wixl (MSYS2's msitools), installed by msiexec as
// SYSTEM from a scheduled task -- what Group Policy does at computer startup
// -- and the agent joins through the install link. A second MSI of the same
// version is an upgrade that changes nothing; `msiexec /x` removes it all.
//
// NOT covered, and cannot be here: real Group Policy assignment at startup.
// The operator's one-OU trial is that (deploy/WINDOWS-PUSH.md §7).
func TestMsiInstallWindows(t *testing.T) {
	binDir := os.Getenv("E2E_BIN_DIR")
	if binDir == "" {
		t.Fatal("E2E_BIN_DIR is unset; run internal/update/e2e/build.sh first")
	}
	msitools := os.Getenv("E2E_MSITOOLS_DIR")
	if msitools == "" {
		t.Fatal("E2E_MSITOOLS_DIR is unset: the directory holding MSYS2's wixl.exe, msibuild.exe and msiinfo.exe")
	}
	python := os.Getenv("E2E_PYTHON")
	if python == "" {
		python = "python"
	}
	repoRoot, err := filepath.Abs(filepath.Join("..", "..", "..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	if v := os.Getenv("E2E_REPO_ROOT"); v != "" {
		repoRoot = v
	}
	const (
		code = "e2eMSIcode_0123456789-ABCDEFGHIJ"
		org  = "5b0f9d8e-7a6c-4b3d-9e2f-1a0b9c8d7e6f"
	)

	jobPub, jobKey, _ := ed25519.GenerateKey(rand.Reader)
	p := newPKI(t)
	plat := newPlatform()
	caPath := filepath.Join(t.TempDir(), "e2e-ca.pem")
	if err := os.WriteFile(caPath, p.caPEM, 0o644); err != nil {
		t.Fatal(err)
	}
	plat.trustCA(t, caPath)
	t.Cleanup(func() { plat.untrustCA(t) })
	gw := startGateway(t, p, jobKey)
	enroll := startEnroll(t, p, gw.url, jobPub)
	t.Cleanup(func() { plat.uninstall(t, repoRoot) })

	work := t.TempDir()
	agentExe := filepath.Join(work, "its-agent.exe")
	copyFileT(t, filepath.Join(binDir, "A"+exe()), agentExe)
	codeFile := filepath.Join(work, "code")
	if err := os.WriteFile(codeFile, []byte(code), 0o600); err != nil {
		t.Fatal(err)
	}
	build := func(name string) string {
		t.Helper()
		out := filepath.Join(work, name)
		cmd := exec.Command(python, filepath.Join(repoRoot, "backend", "app", "services", "msi_build.py"),
			"--exe", agentExe, "--console", enroll.url, "--code-file", codeFile, "--org", org,
			"--version", versionA, "--arch", "amd64", "--out", out)
		cmd.Env = append(os.Environ(), "PATH="+msitools+string(os.PathListSeparator)+os.Getenv("PATH"))
		b, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("msi_build.py: %v\n%s", err, b)
		}
		if strings.Contains(string(b), code) {
			t.Fatalf("the builder printed the code:\n%s", b)
		}
		return out
	}
	settings := filepath.Join(filepath.Dir(plat.target()), "msi-enroll.json")

	// INSTALL, as Group Policy does it: msiexec as SYSTEM, no UI.
	first := build("ITMS-Agent-first-amd64.msi")
	if rc, log := msiexecAsSystem(t, `/i "`+first+`"`, filepath.Join(work, "install.log")); rc != 0 {
		t.Fatalf("msiexec /i exited %d\n%s", rc, log)
	} else if strings.Contains(log, code) {
		t.Fatal("the msiexec log carries the code")
	}
	if n := productCount(t); n != 1 {
		t.Fatalf("%d ITMS Agent products registered after one install, want 1", n)
	}
	if _, err := os.Stat(settings); !os.IsNotExist(err) {
		t.Fatalf("msi-enroll.json survived the install: %v", err)
	}
	pathName := ps(t, "(Get-CimInstance Win32_Service -Filter \"Name='its-agent'\").PathName")
	if strings.TrimSpace(pathName) != `"`+plat.target()+`"` {
		t.Fatalf("service command line = %q; it must be the quoted path and nothing else", pathName)
	}
	env := ps(t, "(Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\its-agent').Environment -join '|'")
	if !strings.Contains(env, "ITS_AGENT_ENROLL_URL="+enroll.url) || strings.Contains(env, code) {
		t.Fatalf("service environment = %q", env)
	}
	assertOwnedSDDL(t, "data directory", plat.dataDir())
	assertOwnedSDDL(t, "data directory's parent", filepath.Dir(plat.dataDir()))

	// JOINED THROUGH THE LINK: one enrollment, carrying link_code and no token.
	gw.waitVersion(versionA, 3*time.Minute)
	bodies := enroll.requests()
	if len(bodies) != 1 || bodies[0]["link_code"] != code {
		t.Fatalf("enrollment requests = %v; want one carrying link_code", bodies)
	}
	if _, hasToken := bodies[0]["token"]; hasToken {
		t.Fatal("the enrollment carried a token member as well as the link code")
	}
	if _, err := os.Stat(filepath.Join(plat.dataDir(), "enroll-token")); !os.IsNotExist(err) {
		t.Fatalf("the token file survived enrollment: %v", err)
	}
	lines := ps(t, "Get-CimInstance Win32_Process -Filter \"Name='its-agent.exe'\" | ForEach-Object { $_.CommandLine }")
	if strings.Contains(lines, code) {
		t.Fatalf("a running agent carries the code in its command line: %s", lines)
	}

	// A SECOND MSI, same version, new ProductCode: a major upgrade on an
	// enrolled machine. Nothing is re-enrolled, the identity is untouched, the
	// service the MSI stopped runs again, and there is still one product.
	identity, err := os.ReadFile(filepath.Join(plat.dataDir(), "identity.json"))
	if err != nil {
		t.Fatal(err)
	}
	second := build("ITMS-Agent-second-amd64.msi")
	if rc, log := msiexecAsSystem(t, `/i "`+second+`"`, filepath.Join(work, "upgrade.log")); rc != 0 {
		t.Fatalf("msiexec /i (upgrade) exited %d\n%s", rc, log)
	}
	if n := productCount(t); n != 1 {
		t.Fatalf("%d ITMS Agent products registered after an upgrade, want 1", n)
	}
	again, err := os.ReadFile(filepath.Join(plat.dataDir(), "identity.json"))
	if err != nil || string(again) != string(identity) {
		t.Fatalf("the upgrade changed the identity (err %v)", err)
	}
	if _, err := os.Stat(settings); !os.IsNotExist(err) {
		t.Fatalf("msi-enroll.json survived the upgrade: %v", err)
	}
	waitServiceRunning(t, 2*time.Minute)
	if n := len(enroll.requests()); n != 1 {
		t.Fatalf("the upgrade enrolled again: %d enrollment requests", n)
	}

	// REMOVE: msiexec /x runs --msi-uninstall, then the files go.
	if rc, log := msiexecAsSystem(t, `/x "`+second+`"`, filepath.Join(work, "remove.log")); rc != 0 {
		t.Fatalf("msiexec /x exited %d\n%s", rc, log)
	}
	if svc := strings.TrimSpace(ps(t, "(Get-Service -Name its-agent -ErrorAction SilentlyContinue).Name")); svc != "" {
		t.Fatalf("the service survived msiexec /x: %q", svc)
	}
	for _, path := range []string{plat.target(), plat.dataDir()} {
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Fatalf("%s survived msiexec /x: %v", path, err)
		}
	}
	if n := productCount(t); n != 0 {
		t.Fatalf("%d ITMS Agent products registered after removal, want 0", n)
	}
}

// msiexecAsSystem runs `msiexec <args> /qn /norestart /l*v <log>` as SYSTEM
// through a scheduled task and returns its exit code and the log.
func msiexecAsSystem(t *testing.T, args, logPath string) (int, string) {
	t.Helper()
	const task = "ITMS-MSI-E2E"
	script := fmt.Sprintf(`$ErrorActionPreference = 'Stop'
$a = New-ScheduledTaskAction -Execute 'msiexec.exe' -Argument '%s /qn /norestart /l*v "%s"'
Register-ScheduledTask -TaskName '%s' -Action $a -User 'NT AUTHORITY\SYSTEM' -RunLevel Highest -Force | Out-Null
$start = (Get-Date).AddSeconds(-2)
Start-ScheduledTask -TaskName '%s'
$deadline = (Get-Date).AddMinutes(10)
do {
  Start-Sleep -Seconds 2
  $state = (Get-ScheduledTask -TaskName '%s').State
  $info = Get-ScheduledTaskInfo -TaskName '%s'
} while (($state -eq 'Running' -or $info.LastRunTime -lt $start) -and (Get-Date) -lt $deadline)
Unregister-ScheduledTask -TaskName '%s' -Confirm:$false
$info.LastTaskResult`, args, logPath, task, task, task, task, task)
	out := strings.Fields(ps(t, script))
	if len(out) == 0 {
		t.Fatal("the scheduled task reported no result")
	}
	rc, err := strconv.Atoi(out[len(out)-1])
	if err != nil {
		t.Fatalf("the scheduled task's result %q is not a number", out[len(out)-1])
	}
	raw, _ := os.ReadFile(logPath)
	return rc, decodeLog(raw)
}

// decodeLog reads an msiexec log: UTF-16LE with a BOM on current Windows,
// otherwise single-byte text.
func decodeLog(b []byte) string {
	if len(b) >= 2 && b[0] == 0xFF && b[1] == 0xFE {
		u := make([]uint16, 0, len(b)/2)
		for i := 2; i+1 < len(b); i += 2 {
			u = append(u, uint16(b[i])|uint16(b[i+1])<<8)
		}
		return string(utf16.Decode(u))
	}
	return string(b)
}

// productCount is how many installed products are named "ITMS Agent".
func productCount(t *testing.T) int {
	t.Helper()
	out := strings.TrimSpace(ps(t, "@(Get-ChildItem 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall' | "+
		"Get-ItemProperty | Where-Object { $_.DisplayName -eq 'ITMS Agent' }).Count"))
	n, err := strconv.Atoi(out)
	if err != nil {
		t.Fatalf("product count %q", out)
	}
	return n
}

func waitServiceRunning(t *testing.T, d time.Duration) {
	t.Helper()
	deadline := time.Now().Add(d)
	for {
		status := strings.TrimSpace(ps(t, "(Get-Service -Name its-agent -ErrorAction SilentlyContinue).Status"))
		if status == "Running" {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("the its-agent service is %q, not Running, %s after the upgrade", status, d)
		}
		time.Sleep(2 * time.Second)
	}
}
```

What it asserts, in order: `msiexec /i` exits 0 as SYSTEM and its verbose log does not contain the code; one product is registered; `msi-enroll.json` is gone; the service's command line is the quoted path and nothing else and its environment holds the console but not the code; the data directory and its parent carry the owned, protected SDDL; the agent enrols once, with `link_code` and no `token`, and the token file is gone; no running `its-agent.exe` has the code on its command line; a second MSI of the same version (new ProductCode) upgrades in place without re-enrolling, leaves `identity.json` byte-identical, deletes the settings file again and leaves the service Running with one product registered; `msiexec /x` removes the service, the binary, the data directory and the product entry.

- [ ] **Step 2: It compiles and vets for Windows**

Run: `cd agent && test -z "$(gofmt -l ./internal/update/e2e)" && GOOS=windows go vet -tags e2e ./internal/update/e2e/ && GOOS=windows go test -c -tags e2e -o /dev/null ./internal/update/e2e`
Expected: no output. (It can only *run* on Windows, elevated, with msitools: Step 5.)

- [ ] **Step 3: The workflow**

`.github/workflows/agent-update-e2e.yml`:

In `on.pull_request.paths`, after `      - "backend/app/modules/agent_update/**"`:

```yaml
      # The Group Policy MSI: TestMsiInstallWindows builds it with this builder.
      - "backend/app/services/msi_build.py"
      - "backend/app/modules/agent_distribution/**"
```

Change `    timeout-minutes: 75` to `    timeout-minutes: 90` (the MSYS2 setup and the MSI test add roughly ten minutes to the Windows leg).

Immediately before `      - name: agent.uninstall on systemd (Linux)`:

```yaml
      # The Group Policy MSI (GPO MSI spec 2026-09-30, Testing): built by the
      # server's own builder (backend/app/services/msi_build.py, stdlib only,
      # run as a script) with MSYS2's wixl -- msitools 0.106, the version the
      # api image installs -- then installed by msiexec as SYSTEM.
      - uses: actions/setup-python@v5
        if: runner.os == 'Windows'
        with:
          python-version: "3.12"

      - uses: msys2/setup-msys2@v2
        if: runner.os == 'Windows'
        id: msys2
        with:
          msystem: UCRT64
          update: false
          install: mingw-w64-ucrt-x86_64-msitools

      - name: Group Policy MSI — wixl-built, msiexec as SYSTEM, upgrade, removal (Windows)
        if: runner.os == 'Windows'
        env:
          E2E_MSITOOLS_DIR: ${{ steps.msys2.outputs.msys2-location }}\ucrt64\bin
          E2E_PYTHON: python
        run: '"$E2E_TEST" -test.run TestMsiInstallWindows -test.v -test.timeout 30m'

```

Run: `go install github.com/rhysd/actionlint/cmd/actionlint@latest && "$(go env GOPATH)/bin/actionlint" .github/workflows/agent-update-e2e.yml`
Expected: no finding on the new lines (one pre-existing SC2129 style note on the `paths` step's `run:` block, line 56, is not this change's).

- [ ] **Step 4: Commit**

```bash
git add agent/internal/update/e2e/msi_install_windows_test.go .github/workflows/agent-update-e2e.yml
git commit -m "test(e2e): the Group Policy MSI installs, upgrades and removes on Windows" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

- [ ] **Step 5: Run it on the Windows runner**

Push the branch; the PR triggers `agent-update-e2e` (the path filter now matches `backend/app/services/msi_build.py`).
Run: `gh pr checks --watch`
Expected: `agent-update-e2e (windows-latest)` green, with `--- PASS: TestMsiInstallWindows` in its log. If it is red, the MSI log is printed in the failure message; do not mark spec Part 2's Windows row as tested until it is green. If `msys2/setup-msys2` cannot install `mingw-w64-ucrt-x86_64-msitools`, stop and report: the fallback is building the MSI in an `ubuntu-latest` job with a fixed enrollment-server port and handing it over as an artifact, which is a change to the harness, not to this test's assertions.


---

### Task 8: Console — *Download MSI for Group Policy* on the Install link panel

**Files:**
- Modify: `frontend/src/lib/api.ts` (two types after `InstallLinkUpdate`; one method after `regenerateInstallLink`)
- Modify: `frontend/src/lib/api.test.ts` (one test)
- Create: `frontend/src/modules/agent-releases/MsiDownload.tsx`
- Create: `frontend/src/modules/agent-releases/MsiDownload.test.tsx`
- Modify: `frontend/src/modules/agent-releases/InstallLinkPanel.tsx` (import; render beside the reveal)
- Modify: `frontend/src/modules/agent-releases/fixtures.ts` (one fixture)

**Interfaces:**
- Consumes: Task 3's route; `saveBlob(blob, filename)` from `src/lib/download.ts`; `describeSubmitError`; `Field`, `inputClass` (`src/components/ui`); `Btn`, `FieldGroup` (`src/shell/components`); `InstallLinkPanel`'s `revealed: string | null` state and `link.data.enabled`.
- Produces: `interface InstallLinkMsi { blob: Blob; filename: string; sha256: string }`, `type MsiArch = "amd64" | "arm64"`, `ApiClient.downloadInstallLinkMsi(body: { code: string; arch: MsiArch }): Promise<InstallLinkMsi>`; `MsiDownload({ linkOn, revealed })`; `codeFromLink(text: string): string | null`.

- [ ] **Step 1: Write the failing tests**

Append inside `describe("ApiClient", …)` in `frontend/src/lib/api.test.ts` (before its closing `});`):

```ts
  it("sends the MSI's code in the body, never the URL, and reads the name and digest back", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("MSI", {
        status: 200,
        headers: {
          "Content-Disposition": 'attachment; filename="ITMS-Agent-itms.example.com-amd64.msi"',
          "X-Content-SHA256": "ab".repeat(32),
        },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "t", vi.fn());
    const code = "AbCdEfGhIjKlMnOpQrStUvWxYz012_-9";
    const msi = await client.downloadInstallLinkMsi({ code, arch: "amd64" });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/install-link/msi");
    expect(url).not.toContain(code);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ code, arch: "amd64" });
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer t");
    expect(msi.filename).toBe("ITMS-Agent-itms.example.com-amd64.msi");
    expect(msi.sha256).toBe("ab".repeat(32));
    expect(await msi.blob.text()).toBe("MSI");
  });
```

(`new Response("MSI", …)`, not `new Response(new Blob(…))`: jsdom's `Blob` is not the one Node's `Response` streams from.)

`frontend/src/modules/agent-releases/MsiDownload.test.tsx`:

```tsx
/**
 * The MSI for Group Policy (GPO MSI spec 2026-09-30 §2.1).
 *
 * What is pinned: nothing is offered while the link is off; the link just
 * generated is pre-filled; the code (never the link's host) goes to the API
 * with the architecture; the file is saved under the server's name and its
 * SHA-256 is shown; a refusal says nothing was built; a paste that is not a
 * link is caught before any request.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../lib/api";
import type { ApiClient } from "../../lib/api";
import { MsiDownload, codeFromLink } from "./MsiDownload";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
const saveBlob = vi.fn();
vi.mock("../../lib/download", () => ({ saveBlob: (...args: unknown[]) => saveBlob(...args) }));

const CODE = "AbCdEfGhIjKlMnOpQrStUvWxYz012_-9";
const LINK = `https://itms.example.com/i/${CODE}`;
const SHA = "3f".repeat(32);

function renderBlock(api: Partial<ApiClient>, props: { linkOn: boolean; revealed: string | null }) {
  mockUseApi.mockReturnValue(api as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MsiDownload {...props} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockUseApi.mockReset();
  saveBlob.mockReset();
});

describe("codeFromLink", () => {
  it("takes the code from a link or a bare code, and nothing else", () => {
    expect(codeFromLink(LINK)).toBe(CODE);
    expect(codeFromLink(`  ${LINK}/ `)).toBe(CODE);
    expect(codeFromLink(CODE)).toBe(CODE);
    expect(codeFromLink("https://itms.example.com/i/short")).toBeNull();
    expect(codeFromLink(`${CODE}x`)).toBeNull();
    expect(codeFromLink("")).toBeNull();
  });
});

describe("MsiDownload", () => {
  it("offers nothing while the link is off", () => {
    renderBlock({}, { linkOn: false, revealed: null });
    expect(screen.getByText(/Switch the link on to download the MSI/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Download MSI/ })).not.toBeInTheDocument();
  });

  it("downloads with the link just generated and shows the file's SHA-256", async () => {
    const blob = new Blob(["MSI"]);
    const downloadInstallLinkMsi = vi
      .fn()
      .mockResolvedValue({ blob, filename: "ITMS-Agent-itms.example.com-arm64.msi", sha256: SHA });
    renderBlock({ downloadInstallLinkMsi }, { linkOn: true, revealed: LINK });
    expect(screen.getByLabelText("Install link")).toHaveValue(LINK);
    await userEvent.click(screen.getByRole("button", { name: "Download MSI for Group Policy (ARM)" }));
    expect(downloadInstallLinkMsi).toHaveBeenCalledWith({ code: CODE, arch: "arm64" });
    expect(await screen.findByText(SHA)).toBeInTheDocument();
    expect(saveBlob).toHaveBeenCalledWith(blob, "ITMS-Agent-itms.example.com-arm64.msi");
    expect(screen.getByRole("status")).toHaveTextContent("ITMS-Agent-itms.example.com-arm64.msi");
  });

  it("takes a pasted link when none is on screen, and refuses a paste that is not one", async () => {
    const downloadInstallLinkMsi = vi.fn().mockResolvedValue({ blob: new Blob([]), filename: "x.msi", sha256: SHA });
    renderBlock({ downloadInstallLinkMsi }, { linkOn: true, revealed: null });
    const amd = screen.getByRole("button", { name: "Download MSI for Group Policy (64-bit Intel or AMD)" });
    expect(amd).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Install link"), "not a link");
    expect(screen.getByRole("alert")).toHaveTextContent("That is not an install link");
    expect(amd).toBeDisabled();
    await userEvent.clear(screen.getByLabelText("Install link"));
    await userEvent.type(screen.getByLabelText("Install link"), LINK);
    await userEvent.click(amd);
    expect(downloadInstallLinkMsi).toHaveBeenCalledWith({ code: CODE, arch: "amd64" });
  });

  it("says no MSI was built when the server refuses", async () => {
    const downloadInstallLinkMsi = vi.fn().mockRejectedValue(
      new ApiError(409, JSON.stringify({ detail: "That is not this organisation's current install link." })),
    );
    renderBlock({ downloadInstallLinkMsi }, { linkOn: true, revealed: LINK });
    await userEvent.click(screen.getByRole("button", { name: "Download MSI for Group Policy (64-bit Intel or AMD)" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No MSI was built.");
    expect(screen.getByRole("alert")).toHaveTextContent("not this organisation's current install link");
    expect(saveBlob).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd frontend && npx vitest run src/lib/api.test.ts src/modules/agent-releases/MsiDownload.test.tsx`
Expected: `MsiDownload.test.tsx` fails to import `./MsiDownload`; `api.test.ts` fails with `client.downloadInstallLinkMsi is not a function`.

- [ ] **Step 3: The client method**

`frontend/src/lib/api.ts` — after the `InstallLinkUpdate` interface:

```ts

/**
 * The Group Policy MSI (GPO MSI spec 2026-09-30 §2.1): the file, the name the
 * server gave it and the SHA-256 it computed and audited.
 */
export interface InstallLinkMsi {
  blob: Blob;
  /** `ITMS-Agent-<host>[@<port>]-<arch>.msi`, from `Content-Disposition`. */
  filename: string;
  /** Hex SHA-256 of these bytes, from `X-Content-SHA256`. */
  sha256: string;
}

export type MsiArch = "amd64" | "arm64";
```

and after the `regenerateInstallLink()` method:

```ts

  /**
   * `enrollment.link.manage`. The Group Policy MSI for this organisation's
   * install link. THE CODE GOES IN THE BODY, never in the URL, which access
   * logs keep; the server holds only its hash and checks the code against it.
   * Not routed through `request`: the body is a file, and its name and
   * SHA-256 come back in headers.
   */
  async downloadInstallLinkMsi(body: { code: string; arch: MsiArch }): Promise<InstallLinkMsi> {
    const headers = new Headers({ "Content-Type": "application/json" });
    const token = this.getToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await fetch(`${this.base}/install-link/msi`, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
    });
    if (response.status === 401) {
      this.onUnauthorized();
      throw new ApiError(401, "unauthorized");
    }
    if (!response.ok) {
      throw new ApiError(response.status, await response.text());
    }
    const disposition = response.headers.get("Content-Disposition") ?? "";
    const filename = /filename="([^"]+)"/.exec(disposition)?.[1] ?? `ITMS-Agent-${body.arch}.msi`;
    return {
      blob: await response.blob(),
      filename,
      sha256: response.headers.get("X-Content-SHA256") ?? "",
    };
  }
```

- [ ] **Step 4: The block, and the panel that shows it**

`frontend/src/modules/agent-releases/MsiDownload.tsx`:

```tsx
/**
 * The MSI for Group Policy (GPO MSI spec 2026-09-30 §2.1, §2.4, §2.6).
 *
 * ONE MSI PER ORGANISATION AND LINK, BUILT ON REQUEST. It carries the
 * published agent and this organisation's install link; a computer that
 * installs it joins through the link and waits in Devices → Waiting for
 * review, like any other computer that used the link.
 *
 * THE LINK IS PASTED, BECAUSE THE SERVER DOES NOT HAVE IT. The server keeps
 * only the code's hash, so the MSI can only be built from a link the
 * administrator still holds: the one on screen right after it was generated
 * (pre-filled here), or one they saved. The code goes to the server in a
 * request BODY, never a URL, and is not kept in the query cache.
 *
 * THE SHA-256 IS SHOWN AFTER THE DOWNLOAD, not before: the MSI is built per
 * request, so each download is a different file. The share's permissions are
 * the only integrity control once it is on SYSVOL, and this is the value to
 * check it against once, before it is copied there.
 *
 * INVARIANT 6: rendered for a holder of `enrollment.link.manage`; the route
 * checks the permission, the link, the release and its signature itself.
 */

import { useMutation } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { describeSubmitError } from "../../components/form";
import { Field, inputClass } from "../../components/ui";
import type { InstallLinkMsi, MsiArch } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { saveBlob } from "../../lib/download";
import { Btn, FieldGroup } from "../../shell/components";

const CODE = /^[A-Za-z0-9_-]{32}$/;

/**
 * The code in a pasted install link (`https://<console>/i/<code>`), or a
 * bare code; null for anything else. Cosmetic: the server checks the code
 * against the link's hash and refuses anything else.
 */
export function codeFromLink(text: string): string | null {
  const value = text.trim();
  const at = value.lastIndexOf("/i/");
  const tail = at >= 0 ? value.slice(at + 3).replace(/\/+$/, "") : value;
  return CODE.test(tail) ? tail : null;
}

const DOWNLOADS: { arch: MsiArch; label: string }[] = [
  { arch: "amd64", label: "Download MSI for Group Policy (64-bit Intel or AMD)" },
  { arch: "arm64", label: "Download MSI for Group Policy (ARM)" },
];

export function MsiDownload({ linkOn, revealed }: { linkOn: boolean; revealed: string | null }) {
  const api = useApi();
  const [link, setLink] = useState(revealed ?? "");
  const [done, setDone] = useState<InstallLinkMsi | null>(null);
  useEffect(() => {
    if (revealed) setLink(revealed);
  }, [revealed]);
  const code = codeFromLink(link);
  const download = useMutation({
    mutationFn: (arch: MsiArch) => api.downloadInstallLinkMsi({ code: code ?? "", arch }),
    onSuccess: (msi) => {
      saveBlob(msi.blob, msi.filename);
      setDone(msi);
    },
  });

  return (
    <FieldGroup title="MSI for Group Policy" icon="package">
      <p className="note">
        For Group Policy software installation: Computer Configuration → Policies → Software Settings →
        Software installation → New → Package, then Assigned. Each computer installs the agent at its
        next startup and waits in Devices → Waiting for review.
      </p>
      {!linkOn ? (
        <p className="note">Switch the link on to download the MSI: computers join through it.</p>
      ) : (
        <>
          <Field
            label="Install link"
            help="The MSI carries the link. Paste the one shown when it was generated; ITMS keeps only a fingerprint of it."
          >
            <input
              className={inputClass}
              value={link}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => setLink(event.target.value)}
            />
          </Field>
          {link.trim() !== "" && code === null ? (
            <p role="alert" className="form-error">
              That is not an install link. It looks like https://itms.example.com/i/ followed by 32 letters and digits.
            </p>
          ) : null}
          <div className="form-actions">
            {DOWNLOADS.map(({ arch, label }) => (
              <Btn
                key={arch}
                label={label}
                icon="download"
                disabled={code === null || download.isPending}
                onClick={() => {
                  setDone(null);
                  download.mutate(arch);
                }}
              />
            ))}
          </div>
          {download.error ? (
            <p role="alert" className="form-error">
              No MSI was built. {describeSubmitError(download.error)}
            </p>
          ) : null}
          {done ? (
            <div role="status">
              <p className="note">
                Saved as <code>{done.filename}</code>. Check this SHA-256 on the file before you copy it
                to the share:
              </p>
              <pre className="code">{done.sha256}</pre>
            </div>
          ) : null}
          <p className="note">
            Rolling out to many computers at once? Raise “Most computers per hour” above for the day, up
            to 500; beyond it, computers wait and try again.
          </p>
          <p className="note">
            Anyone who can read the share can read the link inside the MSI. Put it only where Group Policy
            reads it; generating a new link stops every copy from joining more computers.
          </p>
        </>
      )}
    </FieldGroup>
  );
}
```

`frontend/src/modules/agent-releases/InstallLinkPanel.tsx` — add the import after `import { Btn, Panel, QueryState, Toggle } from "../../shell/components";`:

```tsx
import { MsiDownload } from "./MsiDownload";
```

and render the block immediately before `      <ConfirmDialog` (after the `{revealed ? (…) : null}` reveal section), so the link just generated is handed to it:

```tsx
      {link.data ? <MsiDownload linkOn={link.data.enabled} revealed={revealed} /> : null}

```

`frontend/src/modules/agent-releases/fixtures.ts` — immediately before `export const fixtures: FixtureMap = {`:

```ts
/**
 * The Group Policy MSI. Refuses a code that is not the fixture link's, as the
 * server refuses one that is not the current link's; the bytes are a
 * placeholder and install nothing.
 */
const downloadInstallLinkMsi: FixtureMap["downloadInstallLinkMsi"] = (body) => {
  if (!FIXTURE_LINK.endsWith(`/i/${body.code}`)) {
    throw new ApiError(409, JSON.stringify({
      detail: "That is not this organisation's current install link. Paste the link shown when it was last generated, or generate a new one.",
    }));
  }
  return {
    blob: new Blob(["fixture MSI"], { type: "application/x-msi" }),
    filename: `ITMS-Agent-console.toeshee.example-${body.arch}.msi`,
    sha256: "9c1e5b2f4a7d8e3c6b0a1f2e9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e1d0c",
  };
};

```

and add `downloadInstallLinkMsi,` as the first entry of that `fixtures` object.

- [ ] **Step 5: Run the tests to see them pass**

Run: `cd frontend && npx tsc --noEmit && npx vitest run src/lib/api.test.ts src/modules/agent-releases/ src/lib/noWireNames.test.ts`
Expected: no type errors; every file passes (the existing `InstallLinkPanel.test.tsx` included — the block renders no alert while the link is off).

- [ ] **Step 6: Build and harmony on the page it changed**

Run: `cd frontend && npm run build && node scripts/harmony.mjs --routes /agent-packages --strict-fixtures`
Expected: the build completes; harmony prints `0` strays, `ok` overflow, `0/0` axe and `0` misses for `/agent-packages` at both viewports. (The fixture link is on, so the block renders with its field and both buttons.)

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/lib/api.test.ts frontend/src/modules/agent-releases/MsiDownload.tsx frontend/src/modules/agent-releases/MsiDownload.test.tsx frontend/src/modules/agent-releases/InstallLinkPanel.tsx frontend/src/modules/agent-releases/fixtures.ts
git commit -m "feat(console): download the MSI for Group Policy from the Install link panel" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---

### Task 9: Console — *Approve all* on Devices → Waiting for review

**Files:**
- Modify: `frontend/src/lib/api.ts` (two types before `/** What a Reject did …`; one method before `listAssetCandidates`)
- Modify: `frontend/src/lib/api.test.ts` (one test)
- Create: `frontend/src/modules/devices/ApproveAllDialog.tsx`
- Create: `frontend/src/modules/devices/DevicesApproveAll.test.tsx`
- Modify: `frontend/src/modules/devices/DevicesPage.tsx` (import, one state, the button and the dialog)
- Modify: `frontend/src/fixtures/core.ts` (one fixture)

**Interfaces:**
- Consumes: Task 4's route and digest rule; `ApiClient.listDevices({ state: "review", limit, offset })` (`GET /devices` allows `limit` up to 500); `Dialog`, `Button` (`src/components/ui`); `Pager` (`src/shell/components`); `DevicesPage`'s `tab`, `canReview`, `tabCounts`, `setReviewNotice`.
- Produces: `interface ApproveAllRequest { device_ids: string[]; list_digest: string }`, `interface ApproveAllResult { bulk_id: string; approved: number; not_pending: number; still_waiting: number }`, `ApiClient.approveAllDevices(body): Promise<ApproveAllResult>`; `ApproveAllDialog({ open, onClose, onDone })`, `reviewListDigest(ids: string[]): Promise<string>`, `pendingForReview(api): Promise<{ devices: Device[]; more: boolean }>`, `approveAllMessage(result): string`, `APPROVE_ALL_MAX = 1000`.

- [ ] **Step 1: Write the failing tests**

Append inside `describe("ApiClient", …)` in `frontend/src/lib/api.test.ts`:

```ts
  it("posts Approve all's list and digest", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ bulk_id: "b", approved: 1, not_pending: 0, still_waiting: 0 }), {
        status: 200,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new ApiClient(() => "t", vi.fn());
    await client.approveAllDevices({ device_ids: ["d1"], list_digest: "f".repeat(64) });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/devices/review/approve-all");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ device_ids: ["d1"], list_digest: "f".repeat(64) });
  });
```

`frontend/src/modules/devices/DevicesApproveAll.test.tsx`:

```tsx
/**
 * Approve all (GPO MSI spec 2026-09-30 §2.5).
 *
 * What is pinned: the button is on Waiting for review, for a holder of
 * `devices.review`, and nowhere else; the dialog names the waiting machines
 * (paged) and leaves out a rejected row; it sends exactly the ids it showed
 * with their digest -- the server's digest, byte for byte; the result
 * sentence says how many were approved and how many still wait.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient, Device, DeviceListParams, DevicePage, Me } from "../../lib/api";
import { approveAllMessage, pendingForReview, reviewListDigest } from "./ApproveAllDialog";
import { DevicesPage } from "./DevicesPage";

const mockUseApi = vi.fn();
vi.mock("../../lib/ApiContext", () => ({ useApi: () => mockUseApi() }));
vi.mock("../../lib/ws", () => ({ useLiveEvents: () => undefined }));

function device(i: number, overrides: Partial<Device> = {}): Device {
  return {
    id: `00000000-0000-0000-0000-${String(i).padStart(12, "0")}`, hostname: `DESKTOP-${i}`, fqdn: null,
    os_family: "windows", os_version: "11 24H2", architecture: "amd64", device_group_id: null, status: "active",
    agent_status: "online", last_seen_at: "2026-09-30T09:00:00Z", cert_not_after: null, cert_state: "ok",
    cert_revoked: false, lifecycle: "in_service", asset_type: "computer", source: "agent", provenance: "agent",
    description: null, primary_ip: null, owner: null, location: null, review_state: "pending",
    link_joined_from: "10.50.0.9", link_joined_at: "2026-09-30T08:00:00Z",
    ...overrides,
  };
}

function listFrom(review: Device[]) {
  return vi.fn(async (params: DeviceListParams = {}): Promise<DevicePage> => {
    const items = params.state === "review" ? review : [];
    const offset = params.offset ?? 0;
    const limit = params.limit ?? 50;
    return {
      items: items.slice(offset, offset + limit), total: items.length,
      state_counts: { all: review.length, online: review.length, offline: 0, assets: 0, certs: 0, review: review.length },
      fleet: { total: review.length, managed: review.length, online: review.length, cert_attention: 0 },
    };
  });
}

function renderDevices(api: Partial<ApiClient>, permissions: string[], path = "/devices?state=review") {
  mockUseApi.mockReturnValue({ me: vi.fn().mockResolvedValue({ permissions } as Me), ...api } as unknown as ApiClient);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/devices" element={<DevicesPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => mockUseApi.mockReset());

describe("reviewListDigest", () => {
  it("is the server's review_list_digest, byte for byte", async () => {
    const one = "00000000-0000-0000-0000-000000000001";
    const two = "00000000-0000-0000-0000-000000000002";
    // tests/test_device_review_approve_all.py computes this same value.
    const expected = "7a70c782c5d30f61a8f57b905eaa11c41ec8ffeafaa3a0e99c4fca60044a28d4";
    expect(await reviewListDigest([two, one, two])).toBe(expected);
  });
});

describe("pendingForReview", () => {
  it("reads every page of the review tab and leaves out a rejected row", async () => {
    const rows = [
      ...Array.from({ length: 620 }, (_, i) => device(i + 1)),
      device(9999, { review_state: "rejected", rejection_outcome: "agent_did_not_confirm" }),
    ];
    const listDevices = listFrom(rows);
    const { devices, more } = await pendingForReview({ listDevices } as unknown as ApiClient);
    expect(devices).toHaveLength(620);
    expect(more).toBe(false);
    expect(listDevices).toHaveBeenCalledWith({ state: "review", limit: 500, offset: 0 });
    expect(listDevices).toHaveBeenCalledWith({ state: "review", limit: 500, offset: 500 });
  });

  it("stops at 1,000 and says there are more", async () => {
    const listDevices = listFrom(Array.from({ length: 1200 }, (_, i) => device(i + 1)));
    const { devices, more } = await pendingForReview({ listDevices } as unknown as ApiClient);
    expect(devices).toHaveLength(1000);
    expect(more).toBe(true);
  });
});

describe("approveAllMessage", () => {
  it("says what was approved and what still waits", () => {
    expect(approveAllMessage({ bulk_id: "b", approved: 3, not_pending: 0, still_waiting: 0 })).toBe(
      "Approved 3 machines.",
    );
    expect(approveAllMessage({ bulk_id: "b", approved: 1, not_pending: 1, still_waiting: 2 })).toBe(
      "Approved 1 machine. 1 machine on the list was no longer waiting. " +
        "2 machines still wait for review: they joined after the list was shown.",
    );
  });
});

describe("Approve all on Devices", () => {
  it("is offered on Waiting for review to a reviewer, and nowhere else", async () => {
    const review = [device(1)];
    renderDevices({ listDevices: listFrom(review) }, ["devices.read", "devices.review"]);
    expect(await screen.findByRole("button", { name: "Approve all…" })).toBeInTheDocument();
  });

  it("is not offered without devices.review", async () => {
    renderDevices({ listDevices: listFrom([device(1)]) }, ["devices.read"]);
    expect(await screen.findByText("DESKTOP-1")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve all…" })).not.toBeInTheDocument();
  });

  it("names the machines, sends exactly that list with its digest, and reports the result", async () => {
    const review = Array.from({ length: 25 }, (_, i) => device(i + 1));
    const approveAllDevices = vi.fn().mockResolvedValue({ bulk_id: "b", approved: 25, not_pending: 0, still_waiting: 1 });
    renderDevices({ listDevices: listFrom(review), approveAllDevices }, ["devices.read", "devices.review"]);
    await userEvent.click(await screen.findByRole("button", { name: "Approve all…" }));

    const dialog = await screen.findByRole("dialog", { name: "Approve all waiting machines" });
    const names = await within(dialog).findByRole("list", { name: "Machines to approve" });
    expect(within(names).getAllByRole("listitem")).toHaveLength(20);
    expect(within(dialog).getByText("Showing 1–20 of 25.")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Next page of the machines to approve" }));
    expect(within(names).getAllByRole("listitem")).toHaveLength(5);

    await userEvent.click(within(dialog).getByRole("button", { name: "Approve 25 machines" }));
    const ids = review.map((d) => d.id);
    expect(approveAllDevices).toHaveBeenCalledWith({ device_ids: ids, list_digest: await reviewListDigest(ids) });
    expect(
      await screen.findByText("Approved 25 machines. 1 machine still waits for review: it joined after the list was shown."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd frontend && npx vitest run src/lib/api.test.ts src/modules/devices/DevicesApproveAll.test.tsx`
Expected: `DevicesApproveAll.test.tsx` fails to import `./ApproveAllDialog`; `api.test.ts` fails with `client.approveAllDevices is not a function`.

- [ ] **Step 3: The client method**

`frontend/src/lib/api.ts` — immediately before `/** What a Reject did (install-link spec §1.5, §4). */`:

```ts
/** Approve all (GPO MSI spec 2026-09-30 §2.5): the list the dialog showed. */
export interface ApproveAllRequest {
  device_ids: string[];
  /** `reviewListDigest(device_ids)`: SHA-256 hex of the sorted ids, one per line. */
  list_digest: string;
}

/** What an Approve all did. */
export interface ApproveAllResult {
  bulk_id: string;
  /** Listed computers approved now. */
  approved: number;
  /** Listed computers that were no longer waiting. */
  not_pending: number;
  /** Computers still waiting: they joined after the list was shown. */
  still_waiting: number;
}

```

and immediately before `  listAssetCandidates(): Promise<AssetCandidatePage> {`:

```ts
  /**
   * `devices.review`. Approves the LISTED computers that are still waiting; a
   * computer that joined after the list was shown stays waiting and is
   * counted in `still_waiting`.
   */
  approveAllDevices(body: ApproveAllRequest): Promise<ApproveAllResult> {
    return this.request<ApproveAllResult>("/devices/review/approve-all", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

```

- [ ] **Step 4: The dialog, and the button that opens it**

`frontend/src/modules/devices/ApproveAllDialog.tsx`:

```tsx
/**
 * Approve all (GPO MSI spec 2026-09-30 §2.5): every computer waiting for
 * review, after a Group Policy rollout put three hundred of them there.
 *
 * THE LIST IS WHAT IS CONFIRMED, NOT "EVERYTHING PENDING". The dialog loads
 * the waiting computers once when it opens (at most 1,000), shows their
 * names, and sends exactly those ids with `reviewListDigest` of them. The
 * server approves only the listed ids that are still waiting; a computer that
 * joined after the list was shown stays waiting, and the result says how many.
 * The list is not refreshed by live events while the dialog is open, so what
 * is on screen is what is sent.
 *
 * INVARIANT 6: offered to a holder of `devices.review`; the route checks it.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { describeSubmitError } from "../../components/form";
import { Button, Dialog } from "../../components/ui";
import type { ApiClient, ApproveAllResult, Device } from "../../lib/api";
import { useApi } from "../../lib/ApiContext";
import { Pager } from "../../shell/components";

/** The server's `APPROVE_ALL_MAX`. */
export const APPROVE_ALL_MAX = 1000;
/** The device list's own page limit (`GET /devices`, `le=500`). */
const FETCH_PAGE = 500;
const SHOWN_PER_PAGE = 20;

/**
 * SHA-256, hex, of the sorted, de-duplicated ids, one per line: the server's
 * `review_list_digest`, byte for byte.
 */
export async function reviewListDigest(ids: string[]): Promise<string> {
  const text = [...new Set(ids)].sort().join("\n");
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * The computers waiting for review, at most `APPROVE_ALL_MAX`, read through
 * the Waiting for review tab's own query. That tab also lists rejected
 * computers whose agent may still be installed; they are not approvable and
 * are left out. `more`: there were more than one call may approve.
 */
export async function pendingForReview(api: ApiClient): Promise<{ devices: Device[]; more: boolean }> {
  const pending: Device[] = [];
  let offset = 0;
  let total = 0;
  do {
    const page = await api.listDevices({ state: "review", limit: FETCH_PAGE, offset });
    total = page.total;
    pending.push(...page.items.filter((device) => device.review_state === "pending"));
    offset += FETCH_PAGE;
  } while (offset < total && pending.length < APPROVE_ALL_MAX);
  return {
    devices: pending.slice(0, APPROVE_ALL_MAX),
    more: pending.length > APPROVE_ALL_MAX || offset < total,
  };
}

function machines(n: number): string {
  return `${n} ${n === 1 ? "machine" : "machines"}`;
}

/** The sentence the page shows after an Approve all. */
export function approveAllMessage(result: ApproveAllResult): string {
  const parts = [`Approved ${machines(result.approved)}.`];
  if (result.not_pending > 0) {
    parts.push(`${machines(result.not_pending)} on the list ${result.not_pending === 1 ? "was" : "were"} no longer waiting.`);
  }
  if (result.still_waiting > 0) {
    parts.push(
      `${machines(result.still_waiting)} still ${result.still_waiting === 1 ? "waits" : "wait"} for review: ` +
        `${result.still_waiting === 1 ? "it" : "they"} joined after the list was shown.`,
    );
  }
  return parts.join(" ");
}

export function ApproveAllDialog({
  open,
  onClose,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  /** Handed the result sentence, which the page shows after the dialog closes. */
  onDone: (message: string) => void;
}) {
  const api = useApi();
  const client = useQueryClient();
  const [page, setPage] = useState(0);
  const list = useQuery({
    // NOT under ["devices"]: live events invalidate that prefix, and the list
    // being confirmed must not change under the person confirming it.
    queryKey: ["review-approve-all"],
    queryFn: () => pendingForReview(api),
    enabled: open,
    staleTime: 0,
    gcTime: 0,
  });
  const approve = useMutation({
    mutationFn: async (devices: Device[]) => {
      const ids = devices.map((device) => device.id);
      return api.approveAllDevices({ device_ids: ids, list_digest: await reviewListDigest(ids) });
    },
    onSuccess: (result) => {
      // `["devices"]` is a prefix of both the list and the nav count's key.
      void client.invalidateQueries({ queryKey: ["devices"] });
      onDone(approveAllMessage(result));
      close();
    },
  });

  function close() {
    approve.reset();
    setPage(0);
    onClose();
  }

  const devices = list.data?.devices ?? [];
  const pageCount = Math.max(1, Math.ceil(devices.length / SHOWN_PER_PAGE));
  const first = page * SHOWN_PER_PAGE;
  const shown = devices.slice(first, first + SHOWN_PER_PAGE);

  return (
    <Dialog open={open} onClose={close} title="Approve all waiting machines">
      {list.isLoading ? <p className="note">Loading the machines waiting for review…</p> : null}
      {list.error ? (
        <p role="alert" className="form-error">
          {describeSubmitError(list.error)}
        </p>
      ) : null}
      {list.data && devices.length === 0 ? <p className="note">No machine is waiting for review.</p> : null}
      {devices.length > 0 ? (
        <>
          <p className="note mb-3">
            {machines(devices.length)} {devices.length === 1 ? "is" : "are"} waiting for review. Approved, each
            receives every job your organisation sends. A machine that joins after this list was shown stays
            waiting.
          </p>
          {list.data?.more ? (
            <p className="note mb-3">
              This is the first {APPROVE_ALL_MAX.toLocaleString("en")}. Approve them, then open Approve all again
              for the rest.
            </p>
          ) : null}
          <ul aria-label="Machines to approve">
            {shown.map((device) => (
              <li key={device.id}>
                {device.hostname ?? device.primary_ip ?? device.id}
                {device.link_joined_from ? ` — joined from ${device.link_joined_from}` : ""}
              </li>
            ))}
          </ul>
          <Pager
            page={page}
            pageCount={pageCount}
            onPrevious={() => setPage(page - 1)}
            onNext={() => setPage(page + 1)}
            scopeName="the machines to approve"
          >
            Showing {first + 1}–{first + shown.length} of {devices.length}.
          </Pager>
        </>
      ) : null}
      {approve.error ? (
        <p role="alert" className="form-error">
          Nothing was approved. {describeSubmitError(approve.error)}
        </p>
      ) : null}
      <div className="flex items-center justify-end gap-2">
        <Button type="button" variant="ghost" onClick={close}>
          Cancel
        </Button>
        <Button
          type="button"
          disabled={devices.length === 0 || approve.isPending}
          onClick={() => approve.mutate(devices)}
        >
          {`Approve ${machines(devices.length)}`}
        </Button>
      </div>
    </Dialog>
  );
}
```

`frontend/src/modules/devices/DevicesPage.tsx`:

After `import { AddAssetDialog } from "./AddAssetDialog";` add:

```tsx
import { ApproveAllDialog } from "./ApproveAllDialog";
```

After `  const [reviewNotice, setReviewNotice] = useState<string | null>(null);` add:

```tsx
  // Approve all (GPO MSI spec 2026-09-30 §2.5): open while its dialog is.
  const [approvingAll, setApprovingAll] = useState(false);
```

Immediately before `        {reviewNotice ? (` (inside the table panel, after the push-selection `FilterBar`) add:

```tsx
        {/* APPROVE ALL, on the Waiting for review tab only: after a Group
            Policy rollout the tab holds hundreds of machines, one Approve
            each. Cosmetic like every gate here: the route checks
            `devices.review`, and the dialog confirms a named list. */}
        {tab === "review" && canReview && (tabCounts?.review ?? 0) > 0 ? (
          <FilterBar>
            <Btn label="Approve all…" icon="check" onClick={() => setApprovingAll(true)} />
          </FilterBar>
        ) : null}
        <ApproveAllDialog
          open={approvingAll}
          onClose={() => setApprovingAll(false)}
          onDone={setReviewNotice}
        />
```

`frontend/src/fixtures/core.ts` — immediately before `const acknowledgeDeviceRemoval: FixtureMap["acknowledgeDeviceRemoval"] = (id) => {`:

```ts
/** Approve all: only the listed machines still pending, as on the server. */
const approveAllDevices: FixtureMap["approveAllDevices"] = (body) => {
  const pending = new Set(devices().filter((d) => d.review_state === "pending").map((d) => d.id));
  const listed = [...new Set(body.device_ids)];
  const approved = listed.filter((id) => pending.has(id));
  for (const id of approved) REVIEWED.set(id, "approved");
  return {
    bulk_id: "bulk-fixture",
    approved: approved.length,
    not_pending: listed.length - approved.length,
    still_waiting: devices().filter((d) => d.review_state === "pending").length,
  };
};

```

and in the exported `fixtures` object, after `  approveDevice,` add `  approveAllDevices,`.

- [ ] **Step 5: Run the tests to see them pass**

Run: `cd frontend && npx tsc --noEmit && npx vitest run src/lib/api.test.ts src/modules/devices/ src/fixtures/ src/lib/noWireNames.test.ts`
Expected: no type errors; every file passes, `DevicesReview.test.tsx` included.

- [ ] **Step 6: Everything the console runs, and harmony on both touched routes**

Run: `cd frontend && npx vitest run && npm run build && node scripts/harmony.mjs --routes /agent-packages,/devices --strict-fixtures`
Expected: every test file passes; the build completes; harmony `ok` for both routes at both viewports with no stray, no overflow, no serious axe violation and no fixture miss.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/lib/api.ts frontend/src/lib/api.test.ts frontend/src/modules/devices/ApproveAllDialog.tsx frontend/src/modules/devices/DevicesApproveAll.test.tsx frontend/src/modules/devices/DevicesPage.tsx frontend/src/fixtures/core.ts
git commit -m "feat(console): Approve all on Waiting for review confirms a named list" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---

### Task 10: Docs — the MSI recorded where the rule it overturns lives (spec §2.6)

**Files:**
- Modify: `backend/tests/test_install_link_docs.py` (three tests)
- Modify: `agent/packaging/README.md` (row 3 of the token table; a new section before `## Build`; `### No MSI`; the MSI bullet of *Not built*)
- Modify: `docs/modules/agent-distribution/spec.md` (§2 amendment after the 2026-09-28 one; §7's MSI bullet)
- Modify: `deploy/WINDOWS-PUSH.md` (a new §7 at the end)

**Interfaces:**
- Consumes: the facts established by Tasks 1–9 (names, paths, bounds).
- Produces: operator documentation; `test_install_link_docs.py` fails if a later edit "restores" the old rule by deleting the exception.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_install_link_docs.py`:

```python


MSI_SECTION = (
    "## The Group Policy MSI: the code inside a file on SYSVOL (operator decision, 2026-09-30)"
)


def test_the_packaging_readme_records_the_msi_and_its_residuals() -> None:
    """GPO MSI spec 2026-09-30 §2.6: the three residuals go where the rule the
    MSI overturns lives -- the token table's "distributable artifact" row."""
    text = (REPO / "agent" / "packaging" / "README.md").read_text()
    assert MSI_SECTION in text
    for fragment in (
        "Anyone who can read SYSVOL can take the code",
        "Regenerate",
        "The share's permissions are the only integrity control",
        "The code sits on disk briefly",
        "msi-enroll.json",
        "--msi-install",
        "--msi-uninstall",
        "Its Group Policy MSI is the second",
    ):
        assert fragment in text, fragment
    assert "### No MSI" not in text


def test_the_distribution_spec_amends_section_2_and_section_7_for_the_msi() -> None:
    text = (REPO / "docs" / "modules" / "agent-distribution" / "spec.md").read_text()
    assert "**Amended 2026-09-30 (operator's decision, GPO MSI spec Q3).**" in text
    assert "msi-enroll.json" in text
    assert "- **No MSI, no `.pkg`, no signed Windows binary.**" not in text


def test_the_windows_guide_says_how_to_assign_the_msi() -> None:
    text = (REPO / "deploy" / "WINDOWS-PUSH.md").read_text()
    assert "## 7. The Group Policy MSI" in text
    assert "Assigned" in text and "Approve all" in text
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && .venv/bin/python -m pytest tests/test_install_link_docs.py -q`
Expected: the three new tests fail (`assert MSI_SECTION in text`, the `Amended 2026-09-30` assertion, `## 7. The Group Policy MSI`); the existing three pass.

- [ ] **Step 3: `agent/packaging/README.md`**

Row 3 of the token table (line 30) — replace

```
The install link is the one recorded exception, and it puts a code in a file's NAME, never in its bytes — see the next section. |
```

with

```
The install link is the first recorded exception, and it puts a code in a file's NAME, never in its bytes — see the next section. Its Group Policy MSI is the second, and it does put the code in the bytes — see [The Group Policy MSI](#the-group-policy-msi-the-code-inside-a-file-on-sysvol-operator-decision-2026-09-30). |
```

Insert immediately before `## Build` (after the paragraph ending `run on a Mac.` and its blank line):

```markdown
## The Group Policy MSI: the code inside a file on SYSVOL (operator decision, 2026-09-30)

`docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md` Part 2
(the operator's answer to Q3) records a second exception to the token table's
third row: **Agent packages → Install link → Download MSI for Group Policy**
builds one MSI per organisation and link, and the MSI's **bytes** hold the link
code.

**What is in it, and what runs.** The published Windows agent — the server
re-hashes it against the catalogue and verifies its offline release signature
under `ITS_AGENT_RELEASE_PUBLIC_KEY` before wrapping it, and refuses otherwise —
and `msi-enroll.json`, `{"console": "https://<host>", "code": "<link code>"}`,
both into `%ProgramFiles%\ITSuite`. Windows Installer then runs the installed
binary as SYSTEM with one argument, `--msi-install`, never the code. It reads
the settings file, does what the self-installer does (secures
`%ProgramData%\ITSuite`, then `agent`; creates the token file `O_EXCL`;
registers and starts the service) and **deletes `msi-enroll.json`**; if it
cannot, the install fails and rolls back. On a computer that is already
enrolled it deletes the file, starts the service the MSI stopped, and changes
nothing else. Removal — **Remove** on the GPO, or `msiexec /x` — runs
`--msi-uninstall` first: the `agent.uninstall` removal (service, credentials,
data directory) without the binary, which Windows Installer removes itself. A
newer MSI for the same organisation upgrades the older one and never runs the
removal, so the machine stays enrolled. The MSI is built per request and never
stored; the audit record (`install_link.msi_downloaded`) holds the
architecture, the release and the MSI's SHA-256, never the code.

**Joining is the install link's, unchanged:** the allowed networks, the hourly
limit (raise it, up to 500, for a large rollout's day — computers above it are
told to try again), *Waiting for review* and **Approve all**, and the
`device.joined_via_link` event. Switching the link off or regenerating it stops
every MSI already handed out from joining more computers; computers that joined
are unaffected. The agent updates itself as usual: a new release does not need
a new MSI.

**What remains, stated plainly (spec §2.6).**

1. **Anyone who can read SYSVOL can take the code.** That is every domain user
   and computer, and they can read it from the MSI or its tables (`msiextract`
   is enough). The code only places a machine in the review queue. Revoking it
   is **Regenerate**.
2. **The share's permissions are the only integrity control.** Group Policy
   verifies no digest, and the MSI has no Authenticode signature ([What is
   still unsigned](#what-is-still-unsigned)). The console shows the SHA-256 of
   the file it just built — every download is a different file — so it can be
   checked once, before the file is copied to the share
   (`Get-FileHash .\ITMS-Agent-<host>-amd64.msi`).
3. **The code sits on disk briefly.** `msi-enroll.json` exists in
   `%ProgramFiles%\ITSuite` between Windows Installer copying the files and the
   custom action running. The directory is administrators-only, and the custom
   action deletes the file.
4. **An MSI install over an agent that is already on the computer may replace
   its binary** with the MSI's release (Windows Installer's rules for files
   without a version resource decide); the agent's self-update brings it
   forward again.
5. **ARM64 is built, not observed.** wixl has no ARM64 target, so the ARM64
   MSI is built as x64 and marked `Arm64` in its summary information; CI
   installs only the x64 MSI (`TestMsiInstallWindows` in `agent-update-e2e`).
   Real Group Policy assignment at computer startup is not observable in CI
   either: `deploy/WINDOWS-PUSH.md` §7 is the operator's one-OU trial.

```

Replace the whole `### No MSI` subsection (its heading and its one paragraph, up to the `---` after it) with:

```markdown
### The MSI

Group Policy's *Software Installation* takes an MSI and nothing else. Since
2026-09-30 the console builds one — **Agent packages → Install link →
Download MSI for Group Policy** — and it joins computers through the install
link, not through a deployment token: it carries a link code, which places a
machine in *Waiting for review*, not a credential that mints identities without
one. [The Group Policy MSI](#the-group-policy-msi-the-code-inside-a-file-on-sysvol-operator-decision-2026-09-30)
says what it holds and what that costs. The startup-script path above stays for
estates that keep the install link off.
```

Replace the *Not built* bullet that starts ``- **MSI / `.wxs`**`` (five lines, ending `cannot be used without one.`) with:

```markdown
- **A signed MSI, and an MSI with no link in it.** The Group Policy MSI exists
  ([above](#the-group-policy-msi-the-code-inside-a-file-on-sysvol-operator-decision-2026-09-30))
  but is not Authenticode-signed and works only with the install link on. An
  MSI for deployment tools that carries neither a code nor a token is not
  built; the script path covers them.
```

- [ ] **Step 4: `docs/modules/agent-distribution/spec.md`**

In §2, after the paragraph that ends `a file name".` (the 2026-09-28 amendment), add:

```markdown

**Amended 2026-09-30 (operator's decision, GPO MSI spec Q3).** One artefact is
no longer byte-identical per tenant: the Group Policy MSI
(`docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md` Part 2)
wraps the published, release-signature-verified agent together with
`msi-enroll.json`, which holds the console's address and the organisation's
link code. It is built per request for an administrator holding
`enrollment.link.manage`, never stored, and audited without the code. The code
on a SYSVOL share is readable by every domain user and computer; it only
places a machine in *Waiting for review*, and **Regenerate** revokes it. The
residuals are recorded in `agent/packaging/README.md`, "The Group Policy MSI".
```

In §7, replace the bullet

```markdown
- **No MSI, no `.pkg`, no signed Windows binary.** §3. *(Unchanged 2026-08-26,
  and the second half of it is now the most expensive item on §8: managed
  distribution is what makes an unsigned binary reach three hundred machines
  instead of one, and once the file is on a distribution point the share's ACL
  is the only control left — see `spec-managed-distribution.md` §4.)*
```

with

```markdown
- **No `.pkg`, no signed Windows binary, no signed MSI.** §3. *(Amended
  2026-09-30: the Group Policy MSI exists — §2's amendment — and it is
  unsigned, like the binary it wraps. Managed distribution is what makes an
  unsigned file reach three hundred machines instead of one, and once the MSI
  is on a distribution point the share's ACL is the only control left — see
  `spec-managed-distribution.md` §4 and the console's SHA-256, shown for the
  file just downloaded.)*
```

- [ ] **Step 5: `deploy/WINDOWS-PUSH.md`**

Append at the end of the file:

```markdown

## 7. The Group Policy MSI (no WinRM, no push credential)

A domain can install the agent without WinRM or a push credential at all:
Group Policy software installation runs an MSI as SYSTEM at each computer's
next startup. The MSI joins through the install link, so every computer lands
in **Devices → Waiting for review**.

1. **Agent packages → Install link**: the link is on, and its networks cover
   the computers' addresses. For a rollout of more than 50 computers, raise
   *Most computers per hour* for the day (up to 500); above it, computers wait
   and try again.
2. A stable, release-signed agent **1.7.0 or later** is published for the
   architecture, and `ITS_AGENT_RELEASE_PUBLIC_KEY` is set on the server (the
   download says so if it is not).
3. Paste the link (the one shown when it was generated) and click **Download
   MSI for Group Policy** (64-bit Intel or AMD, or ARM). Note the SHA-256 the
   console shows, and check it on the file: `Get-FileHash .\ITMS-Agent-<host>-amd64.msi`.
4. Copy the MSI to a share the computers' accounts can read — normally under
   `\\<domain>\SYSVOL\<domain>\` — whose write access is limited to the
   administrators who manage it. That ACL is the only thing protecting the
   file from then on.
5. **Group Policy Management → Computer Configuration → Policies → Software
   Settings → Software installation → New → Package**, choose the MSI by its
   UNC path, then **Assigned**. Link the GPO to one test OU first.
6. Restart one computer in the OU. At startup it installs the agent, joins, and
   appears in **Devices → Waiting for review**; `msi-enroll.json` is no longer in
   `C:\Program Files\ITSuite`.
7. Link the GPO to the rest, and after the next restarts click **Approve all**.

**Removing it:** in the GPO, *All Tasks → Remove → Immediately uninstall*. Each
computer removes the service, its credentials and its data directory at the
next startup. **Replacing it** after a new release or a regenerated link:
assign the new MSI in the same GPO with *Upgrade*; computers already enrolled
stay enrolled. **Anyone who can read SYSVOL can read the link inside the MSI**;
**Regenerate** it on the Install link panel to stop every copy from joining
more computers.
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `cd backend && .venv/bin/python -m pytest tests/test_install_link_docs.py -q`
Expected: `6 passed`.

- [ ] **Step 7: Commit**

```bash
git add backend/tests/test_install_link_docs.py agent/packaging/README.md docs/modules/agent-distribution/spec.md deploy/WINDOWS-PUSH.md
git commit -m "docs(msi): record the Group Policy MSI and its residuals where the rule lives" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_019gyzgtDWmPL7HFfYK1eg89"
```

---

### Task 11: Verify everything, then the path to a deployed `main` and to agent 1.7.0

**Files:**
- None created. This task runs every suite, fixes what they find in the task that owns it, and produces the PR's deploy section (Appendix A, verbatim) and the release steps (Appendix B).

**Interfaces:**
- Consumes: every task above.
- Produces: a green branch, a merged `main`, and the two appendices for the operator.

**The MSI needs agent 1.7.0.** Nothing in the tree carries the agent's version: it is stamped at release time (`-X main.version`). Until Appendix B's release is published and marked stable, *Download MSI for Group Policy* answers "No stable, release-signed agent 1.7.0 or later is published …" — the designed behaviour, not a defect. Approve all works as soon as Appendix A is done.

- [ ] **Step 1: Backend, all of it**

Run: `cd backend && .venv/bin/ruff check app tests && .venv/bin/mypy app && ITS_TEST_REQUIRE_MSITOOLS=1 ITS_TEST_REQUIRE_GO=1 .venv/bin/python -m pytest -q`
Expected: `All checks passed!`, `Success: no issues found`, and every test passing with none of the MSI tests skipped.

- [ ] **Step 2: No migration, confirmed**

Run: `git diff --stat origin/main -- backend/migrations && cd backend && .venv/bin/alembic heads`
Expected: no diff output; one head, the same revision `main` has.

- [ ] **Step 3: Agent, every GOOS CI checks**

Run:

```bash
cd agent
test -z "$(gofmt -l .)"
go vet ./...
GOOS=windows GOARCH=amd64 go vet ./...
GOOS=windows GOARCH=arm64 go vet ./...
GOOS=darwin go vet ./...
GOOS=windows go vet -tags e2e ./internal/update/e2e/
go test ./... -count=1
GOOS=windows go test -c -o /dev/null ./internal/install
GOOS=windows go test -c -o /dev/null ./internal/capabilities/uninstall
GOOS=darwin go test -c -o /dev/null ./internal/capabilities/uninstall
"$(go env GOPATH)/bin/staticcheck" ./...
set -o pipefail
go test ./... -count=1 -v \
  -run 'Falsifier|Idle|Allocat|Accumulat|Dormant|CostsABounded|MagicPacketCosts|Retained|LeakNoGoroutines|OpensNoSocket|NothingToInitialise|LinksNothingTheService' \
  | grep -E 'falsifier|budget|one wake|magic packet|--- (PASS|FAIL|SKIP)|^(ok|FAIL)' | grep -E -- '--- FAIL|^FAIL'
```

Expected: no output from any line except `ok` lines from `go test ./...`; the final pipeline prints nothing (no failing falsifier, dormancy scan or link-closure check).

- [ ] **Step 4: Console**

Run: `cd frontend && npx tsc --noEmit && npx vitest run && npm run build && node scripts/harmony.mjs --routes /agent-packages,/devices --strict-fixtures`
Expected: no type errors; every test file passes; the build completes; harmony exits 0 with no stray, overflow, serious axe violation or fixture miss on either route.

- [ ] **Step 5: CI on the pushed branch, including Windows**

Run: `git push -u origin "$(git branch --show-current)"`, open the PR, then `gh pr checks --watch`.
Expected: `backend`, `migrations`, `frontend`, `harmony`, `agent`, `agent-windows`, `agent-macos` and `agent-update-e2e (windows-latest)` green. `agent-windows` carries `TestTheMsiPathSecuresTheDataDirectorysParentFirst`; `agent-update-e2e (windows-latest)` carries `TestMsiInstallWindows`, the only evidence for the spec's Windows MSI row. If it is red, the PR does not claim that row.

- [ ] **Step 6: Merge**

The operator deploys from `main`, never from a feature branch: this work is deployable only once the PR is reviewed and merged. Then hand the operator Appendix A, and after it Appendix B.

---

## Appendix A — the deploy sequence for Part 2 (GPO MSI and Approve all)

**This starts after the PR is merged to `main`.** Every step is listed, including the ones that are no-ops for this change.

**If Part 1 (subnet push, migration `0200`) reaches `main` before this is deployed,** deploy both at once with Part 1's sequence, and add this appendix's steps 4 and 5 (the key) before its image build and step 9's MSI checks to its verification; its migrate step then applies `0200`, and its rollback advice (a migration was applied) is the one that holds.

What this release contains, for the operator: **no migration**; **one new `.env` key, `ITS_AGENT_RELEASE_PUBLIC_KEY`** (the public half of the release-signing key; not a secret); a **rebuilt `api`/`worker` image** that now installs `wixl` and `msitools` (the build needs to reach the Debian mirrors); no new beat entry; no Traefik change. Approve all works after this deploy. The MSI download works after this deploy **and** Appendix B.

```sh
# 0. On the host, in the checkout that runs the stack.
cd /opt/it-suite/deploy

# 1. Is OpenBao sealed? It is sealed after EVERY restart and EVERY host reboot,
#    and a sealed OpenBao makes `up -d` BLOCK rather than fail. Whether it is
#    sealed now depends on this host's history, which nobody here can see.
docker compose exec openbao bao status
#    If it reports "Sealed  true", unseal with three shares, one per command:
docker compose exec openbao bao operator unseal
docker compose exec openbao bao operator unseal
docker compose exec openbao bao operator unseal

# 2. Record what is running, and back up. No migration in this release, so the
#    backup is insurance rather than the only way back -- take it anyway.
grep IT_SUITE_VERSION .env
docker image ls itsuite/backend --format '{{.Tag}}'
./scripts/backup.sh ~/backups/pre-gpo-msi-$(date -u +%FT%H%MZ)
#    Copy the directory it printed OFF this host (it contains role password hashes).

# 3. Pull main.
git checkout main
git pull
```

**4. The release-signing public key — a substitution step, not a command to paste.** On any computer where `gh` is signed in to GitHub, download the public key file of the agent release that is already published (every release carries the same offline key's public half):

```sh
gh release download agent-v1.6.0 --repo Toeshee-Lab/Toeshee-RMS --pattern release-signing-key.pub
```

and copy that `release-signing-key.pub` into `/opt/it-suite/deploy/` on the host (for example with `scp`). The same value is printed in the release notes on GitHub ("public half: …").

```sh
# 5. Put the key in .env: remove any earlier line, then append exactly one.
cd /opt/it-suite/deploy
test -s release-signing-key.pub \
  && sed -i '/^ITS_AGENT_RELEASE_PUBLIC_KEY=/d' .env \
  && printf 'ITS_AGENT_RELEASE_PUBLIC_KEY=%s\n' "$(tr -d '[:space:]' < release-signing-key.pub)" >> .env
grep -c '^ITS_AGENT_RELEASE_PUBLIC_KEY=' .env      # -> 1
#    No other new key. ITS_PUBLIC_CONSOLE_URL is what the MSI's console address
#    and file name come from; this shows it (it must be the https DNS name):
grep -n '^ITS_PUBLIC_CONSOLE_URL=' .env

# 6. Build the images from main, tagged with the commit, and select that tag.
#    The backend image now apt-installs wixl and msitools.
./scripts/build-images.sh
NEW_VERSION=$(git rev-parse --short HEAD)
grep -q '^IT_SUITE_VERSION=' .env \
  && sed -i "s/^IT_SUITE_VERSION=.*/IT_SUITE_VERSION=$NEW_VERSION/" .env \
  || echo "IT_SUITE_VERSION=$NEW_VERSION" >> .env
grep IT_SUITE_VERSION .env      # exactly one line, with the new SHA

# 7. Migrate, then the idempotent one-shots. NO NEW MIGRATION in this release:
#    `migrate` runs anyway (it is also a dependency of `api`) and finds nothing
#    to do.
docker compose up migrate
docker compose up postgres-grants     # idempotent; nothing new in this release
docker compose up openbao-init        # idempotent; nothing new in this release
docker compose up keycloak-init       # idempotent; nothing new in this release

# 8. Restart. Recreates `api` and `worker` with the new image and the new key.
docker compose up -d

# 9. Verify.
docker compose ps -a --format 'table {{.Service}}\t{{.Status}}'
docker compose exec postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -h localhost -U its_admin -d itsuite -tAc "select version_num from alembic_version;"'
#    -> the same revision as before this deploy (no migration)
docker compose exec api sh -c 'for t in wixl msibuild msiinfo; do command -v "$t"; done; wixl --version'
#    -> three paths under /usr/bin, then 0.106 (or later)
docker compose exec api python -c "import base64; from app.core.config import get_settings; print(len(base64.b64decode(get_settings().agent_release_public_key or '')))"
#    -> 32
docker compose exec api python -c "import httpx; print(httpx.post('http://localhost:8000/api/v1/install-link/msi', json={}).status_code, httpx.post('http://localhost:8000/api/v1/devices/review/approve-all', json={}).status_code)"
#    -> 401 401: both routes exist and ask for a signed-in user (404 would mean the old image)
```

**Rolling back.** There is no migration, so the previous application image runs against the database unchanged. If step 2's `docker image ls` printed an earlier tag, rolling back is retagging to it:

```sh
cd /opt/it-suite/deploy
PREVIOUS=$(docker image ls itsuite/backend --format '{{.Tag}}' | grep -v "^$(git rev-parse --short HEAD)$" | grep -v '^dev$' | head -n 1)
echo "rolling back to: $PREVIOUS"      # read it; if it is empty, stop here -- see below
sed -i "s/^IT_SUITE_VERSION=.*/IT_SUITE_VERSION=$PREVIOUS/" .env
docker compose up -d
```

If that list held no earlier tag (`(none yet)` from `build-images.sh`, or only `dev`), **there is no image to roll back to**, and the only way back is restoring step 2's backup (`./scripts/restore.sh` with the directory it printed, per `docs/07-backup-restore.md`). `ITS_AGENT_RELEASE_PUBLIC_KEY` can stay in `.env` either way; an older image ignores it.

**Then, in the console:** *Devices → Waiting for review → Approve all…* is there for anyone with `devices.review`. *Agent packages → Install link* shows *MSI for Group Policy*; it builds nothing until Appendix B's release is stable.

## Appendix B — cutting agent 1.7.0 (after Appendix A)

The MSI wraps only a **stable**, release-signed agent **1.7.0 or later** (`--msi-install` exists only in agents built from this PR). From a clone with `gh` signed in:

```sh
VERSION=1.7.0
gh workflow run release-agent.yml --ref main -f version="$VERSION"
gh run watch
gh release download "agent-v$VERSION" --dir "its-agent-$VERSION"
ls "its-agent-$VERSION"
cat "its-agent-$VERSION/release-signing-key.pub"
```

The key printed last must equal the host's `ITS_AGENT_RELEASE_PUBLIC_KEY` (Appendix A step 5); if it does not, the MSI download refuses with "does not carry a valid release signature".

Then in the console: *Agent packages → Publish a release…* with version `1.7.0` and the files just downloaded (`SIGNATURES` included), then *Mark as stable…* for 1.7.0. Verify: *Agent packages → Install link* (link on) → paste the link → *Download MSI for Group Policy (64-bit Intel or AMD)* saves `ITMS-Agent-<host>-amd64.msi` and shows its SHA-256; *Audit* shows `install_link.msi_downloaded` with that SHA-256 and no code. Then follow `deploy/WINDOWS-PUSH.md` §7 on one test OU.

---

## Self-review against the spec (Part 2)

| Spec | Where |
|---|---|
| §2.1 download per arch, SHA-256 shown, file name | Tasks 3, 8 (D11: shown after download) |
| §2.1 steps 2–5 (share, GPO Assigned, startup install, Approve all) | Task 10 (`WINDOWS-PUSH.md` §7), Task 9 |
| §2.2 `build_msi`, re-hash, release signature | Tasks 2, 3 (D4, D14) |
| §2.2 fixed WiX content: two files, deferred no-impersonate action, code never on the command line | Task 1 |
| §2.2 UpgradeCode per org, ProductVersion = agent version | Task 1 |
| §2.2 wixl in `backend.Dockerfile`, temp dir, 60 s, removed, never stored | Task 1 |
| §2.2 audit `install_link.msi_downloaded` without the code | Task 3 |
| §2.2 risk: wixl custom action proof, `msiinfo` read-back | *What was settled*; Task 1 Steps 2–5 |
| §2.3 `--msi-install`: SYSTEM/elevated, no question, settings validation, parent-first dirs, `O_EXCL` token, service, delete settings, fail if not deleted | Task 6 (D5, D6) |
| §2.3 already enrolled → no change, exit 0 | Task 6 (refined, D5) |
| §2.3 uninstall runs `agent.uninstall` logic locally | Task 5 (D8) |
| §2.3 idle budget: import-pinning and dormant tests | Tasks 5, 6 |
| §2.4 joining unchanged, hourly-limit advice beside the download | Task 8 (copy), Task 10 |
| §2.5 Approve all: dialog with count and names (paged), `Approve N machines`, `{device_ids, list_digest}`, still-pending only, `devices.review`, per-device audit with `bulk_id`, ≤ 1,000 | Tasks 4, 9 (D9, D10) |
| §2.6 residuals in packaging README and distribution spec §2, §7 | Task 10 |
| Testing — backend (bytes/signature refusal, msiinfo read-back, permission, link off, audit without code, Approve all bounds) | Tasks 1–4 |
| Testing — agent (validation, no-op when enrolled, settings deleted, parent-first, import-pinning) | Tasks 5, 6 |
| Testing — CI `TestMsiInstallWindows` on windows-latest | Task 7 |
| Testing — not provable in CI: real GPO; `WINDOWS-PUSH.md` GPO MSI section | Task 10 |
