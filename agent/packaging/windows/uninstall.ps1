<#
.SYNOPSIS
    Remove the Toeshee agent from this Windows endpoint.

.DESCRIPTION
    Stops and deletes the service, removes the binary, and -- unless
    -KeepState -- removes the data directory, which holds the device identity,
    its private key and the pinned trust anchors.

    DELIBERATELY LEFT ALONE:
      * The DEVICE RECORD in the control plane. It keeps its history, its
        inventory and its audit trail until someone retires it in the console.
        An uninstall on the endpoint is not authorised to delete a tenant's
        record, and an endpoint that is merely offline would look identical.
      * The agent's CERTIFICATE at the gateway. Deleting the local copy revokes
        nothing. Revoke in the console when decommissioning or after a
        compromise.
      * The AUDIT LOG, which is append-only and hash-chained (invariant #4).

    Removing the data directory is irreversible in one way: the device key and
    certificate are destroyed, so a re-install needs a NEW enrollment token and
    mints a NEW device record. Use -KeepState when replacing the binary rather
    than the endpoint.

    UNTESTED. No Windows host has run this script.

.PARAMETER KeepState
    Leave %ProgramData%\ITSuite\agent in place.
#>
[CmdletBinding()]
param(
    [switch]$KeepState,
    [string]$InstallDir = "$env:ProgramFiles\ITSuite",
    [string]$DataDir = "$env:ProgramData\ITSuite\agent"
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ServiceName = 'its-agent'

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'uninstall.ps1 must run from an elevated PowerShell session.'
}

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    if ($svc.Status -ne 'Stopped') {
        # -Force stops dependents too; there are none, but a half-uninstall
        # that leaves a running agent nobody thinks is installed is the failure
        # to avoid here.
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        # WAITED FOR, not fired and forgotten. Windows keeps a section open on
        # a running executable's image, so removing its-agent.exe below fails
        # while the process is still exiting -- and every removal here is
        # -ErrorAction SilentlyContinue, so it would fail into the success
        # banner at the bottom.
        try { $svc.WaitForStatus('Stopped', (New-TimeSpan -Seconds 30)) } catch { }
    }
    & sc.exe delete $ServiceName | Out-Null
}

$binary = Join-Path $InstallDir 'its-agent.exe'
foreach ($attempt in 1..20) {
    Remove-Item -Path $binary -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $binary)) { break }
    Start-Sleep -Milliseconds 500
}

if ($KeepState) {
    Write-Host "kept $DataDir (identity, key, pins)"
} else {
    # THE CREDENTIALS FIRST AND BY NAME, so a partial failure still takes them
    # with it. All four, not two: `agent.key.pending` is the private key a
    # certificate rotation built its CSR over, and `previous.json` carries
    # {cert, chain, KEY} for the identity a rotation replaced
    # (agent/internal/enroll/rotate.go). Both are device private key material
    # and both survived a failed recursive remove under the previous list.
    foreach ($f in @('enroll-token', 'agent.key', 'agent.key.pending', 'previous.json')) {
        Remove-Item -Path (Join-Path $DataDir $f) -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -Path $DataDir -Recurse -Force -ErrorAction SilentlyContinue
}

# SAY WHAT ACTUALLY HAPPENED. Every removal above is best-effort, so an
# unconditional "removed" banner is a claim this script has not checked.
$leftBinary = Test-Path $binary
$leftState = (-not $KeepState) -and (Test-Path $DataDir)
Write-Host ''
if ($leftBinary -or $leftState) {
    Write-Host 'its-agent PARTIALLY removed from this endpoint.'
    if ($leftBinary) { Write-Host "  still present: $binary (something still holds it open)" }
    if ($leftState) { Write-Host "  still present: $DataDir (the credentials in it were removed by name)" }
    Write-Host '  reboot and re-run, or remove these by hand.'
} else {
    Write-Host 'its-agent removed from this endpoint.'
}
Write-Host 'Still present, on purpose:'
Write-Host '  * the device record in the control plane (retire it in the console)'
Write-Host '  * its certificate at the gateway (revoke it there if decommissioning)'
Write-Host '  * the audit log, which is append-only'
