<#
.SYNOPSIS
    Hand an already-installed Windows endpoint its control-plane URL and its
    enrollment token, then start the service.

.DESCRIPTION
    The Windows counterpart of linux/its-agent-enroll, and it exists for the
    same reason that script does: INSTALLING AND ENROLLING ARE TWO STEPS.

    A managed rollout (GPO startup script, Intune Win32 app) stages one package
    on a distribution point that every target machine reads. A per-org
    enrollment token inside that package would be a credential in a
    distributable artifact -- which agent/packaging/README.md forbids by name,
    and which on a SYSVOL or DFS share means every machine account in the
    domain holds one enrollment credential with no expiry anyone remembers.

    So install.ps1 puts the service on the machine and this puts the credential
    on it, from wherever the channel's own secret mechanism keeps it.

    THE TOKEN IS NEVER A PARAMETER. -Token is REFUSED, with the reason.
    PowerShell keeps every command line in PSReadLine's history file, Windows
    records process command lines in Security event 4688 where command-line
    auditing is on, and any local user can read them from a running process.
    It arrives in ITS_AGENT_ENROLL_TOKEN, or on stdin with -TokenFromStdin.

    UNTESTED. No Windows host has run this script, for the same reason no
    Windows host has run install.ps1: CI is ubuntu-latest and nothing in this
    repository has a Windows machine. Read agent/packaging/README.md "What is
    untested" and follow
    docs/modules/agent-distribution/first-run-verification.md before a fleet
    rollout.

.PARAMETER Url
    Control-plane API base URL, e.g. https://its.example.com. Not a secret.

.PARAMETER TokenFromStdin
    Read the enrollment token from the process's standard input instead of the
    environment.

    THIS MEANS REAL REDIRECTED STDIN, NOT A POWERSHELL PIPELINE. A .ps1 runs in
    the calling PowerShell process, so `'tok' | .\its-agent-enroll.ps1
    -TokenFromStdin` puts the string on the PowerShell pipeline (where nothing
    reads it) and leaves the process's stdin attached to the console -- the
    script sits waiting for somebody to type. Redirect at the OS level instead,
    or use the environment variable.

.PARAMETER DataDir
    Where the agent keeps its state. DEFAULTED FROM THE INSTALLED SERVICE, not
    from this script's own default: writing the token somewhere the service
    does not read is an endpoint that never enrols, and re-running would then
    mint a second device record.

.PARAMETER NoStart
    Write the configuration but do not (re)start the service.

.EXAMPLE
    $env:ITS_AGENT_ENROLL_TOKEN = <read from your secret store>
    .\its-agent-enroll.ps1 -Url https://its.example.com

.EXAMPLE
    cmd /c "type \\server\secrets\token.txt | powershell -NoProfile -ExecutionPolicy Bypass -File its-agent-enroll.ps1 -TokenFromStdin -Url https://its.example.com"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)][string]$Url = $env:ITS_AGENT_ENROLL_URL,
    [switch]$TokenFromStdin,
    [switch]$NoStart,
    [string]$DataDir = "$env:ProgramData\ITSuite\agent",
    # -Token EXISTS ONLY TO BE REFUSED. Left out of the parameter block
    # entirely, PowerShell answers a bare "A parameter cannot be found that
    # matches parameter name 'Token'", which teaches the operator nothing and
    # sends them looking for the right spelling rather than the right method.
    [string]$Token
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ServiceName = 'its-agent'

if ($Token) {
    throw @'
the enrollment token must not be passed as an argument: PowerShell keeps command
lines in its history file, Windows records them in Security event 4688 where
command-line auditing is on, and any local user can read them from a running
process.
  Use:  $env:ITS_AGENT_ENROLL_TOKEN = <token>; .\its-agent-enroll.ps1 -Url ...
  or:   cmd /c "type token.txt | powershell -NoProfile -File its-agent-enroll.ps1 -TokenFromStdin -Url ..."
'@
}

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'its-agent-enroll.ps1 must run elevated (a GPO startup script and an Intune Win32 app both run as SYSTEM, which qualifies).'
}
if (-not $Url) { throw '-Url is required (or set ITS_AGENT_ENROLL_URL).' }

# The same three functions as install.ps1 (see there for the reasoning).
# A junction or symbolic link where a directory of ours should be is refused.
function Assert-NotReparsePoint([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return }
    if (([int]$item.Attributes -band [int][System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Path is a junction or symbolic link, not a directory. No token was written; an administrator must remove it and run its-agent-enroll.ps1 again."
    }
    if (-not $item.PSIsContainer) {
        throw "$Path exists and is not a directory. No token was written."
    }
}

function Assert-ItsOwner([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $owner = (Get-Acl -LiteralPath $Path).GetOwner([System.Security.Principal.SecurityIdentifier])
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User
    if ($owner.Value -eq 'S-1-5-32-544' -or $owner.Value -eq 'S-1-5-18' -or $owner.Value -eq $me.Value) { return }
    $name = $owner.Value
    try { $name = $owner.Translate([System.Security.Principal.NTAccount]).Value } catch { }
    throw "$Path already exists and belongs to $name, not to an administrator. No token was written."
}

# %ProgramData%\ITSuite, the data directory's PARENT: whoever owns it holds
# FILE_DELETE_CHILD on it and can swap the protected data directory for their
# own, and on a default Windows install a standard user can create it. Owner
# Administrators, SYSTEM and Administrators only, a link refused -- before the
# data directory is read, created or written.
function Protect-ItsDirectory([string]$Path, [switch]$RefuseForeignOwner) {
    $sec = New-Object System.Security.AccessControl.DirectorySecurity
    $sec.SetAccessRuleProtection($true, $false)
    foreach ($who in @('NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators')) {
        $sec.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            $who, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    }
    $sec.SetOwner((New-Object System.Security.Principal.NTAccount('BUILTIN\Administrators')))
    if (-not (Test-Path -LiteralPath $Path)) {
        $withDescriptor = [System.IO.Directory].GetMethod('CreateDirectory',
            [type[]]@([string], [System.Security.AccessControl.DirectorySecurity]))
        if ($withDescriptor) {
            [void][System.IO.Directory]::CreateDirectory($Path, $sec)
        } else {
            New-Item -ItemType Directory -Path $Path | Out-Null
            $RefuseForeignOwner = $true
        }
    }
    Assert-NotReparsePoint $Path
    if ($RefuseForeignOwner) { Assert-ItsOwner $Path }
    Set-Acl -LiteralPath $Path -AclObject $sec
    Assert-NotReparsePoint $Path
    $owner = (Get-Acl -LiteralPath $Path).GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    if ($owner -ne 'S-1-5-32-544') {
        throw "$Path could not be given to BUILTIN\Administrators (its owner is $owner). No token was written."
    }
}

# --- the service must already exist ----------------------------------------
# CHECKED BEFORE ANY CREDENTIAL TOUCHES DISK, and the order is the whole point.
# This ran last in the previous version, so on a machine where install.ps1 had
# never succeeded -- exactly the machine a GPO startup script meets -- the
# script wrote the deployment token to disk and THEN threw. The credential was
# left behind, unspendable, with nothing to remove it, on every such machine.
$serviceKey = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"
if (-not (Test-Path $serviceKey)) {
    throw "the service $ServiceName is not installed; run install.ps1 first (installing and enrolling are two steps on this path)."
}

# --- where the service actually reads its state ----------------------------
# install.ps1 accepts -DataDir, so its default and this script's default can
# disagree. Writing the token to a directory the service does not read is an
# endpoint that silently never enrols; worse, the already-enrolled check below
# would then miss an existing identity and a re-run would mint a SECOND device
# record for one machine. Take the installed value unless the caller was
# explicit.
$priorEnv = @()
try { $priorEnv = @((Get-ItemProperty -Path $serviceKey -Name 'Environment').Environment) } catch { $priorEnv = @() }
if (-not $PSBoundParameters.ContainsKey('DataDir')) {
    $installed = $priorEnv | Where-Object { $_ -like 'ITS_AGENT_DATA_DIR=*' } | Select-Object -First 1
    if ($installed) { $DataDir = $installed.Substring('ITS_AGENT_DATA_DIR='.Length) }
}

# --- the data directory's parent, before the data directory is used ---------
# The service exists, so this is not a fresh install: the parent is taken over
# rather than refused (install.ps1 refuses a foreign owner on a fresh install).
# Only a parent that is ours to lock down: a custom data directory of C:\ITS
# must not end with a protected DACL on C:\.
$DataParent = Split-Path -Parent $DataDir
if ((Split-Path -Leaf $DataParent) -eq 'ITSuite') {
    Protect-ItsDirectory -Path $DataParent
} else {
    Write-Warning "$DataParent is not an ITSuite directory, so its-agent-enroll.ps1 leaves its ACL alone. Anyone who can rename entries in it can replace $DataDir; make sure only Administrators can."
}
Assert-NotReparsePoint $DataDir

# --- already enrolled? ------------------------------------------------------
# Enrolling twice is not possible and should not look possible: a second
# enrollment would mint a duplicate device record with its own history, and on
# a managed rollout this script runs at EVERY boot.
$identity = Join-Path $DataDir 'identity.json'
if (Test-Path $identity) {
    Write-Host "this endpoint is already enrolled ($identity exists); nothing to do"
    Write-Host 'to re-enroll, remove the data directory first - and retire the old device in the console'
    exit 0
}

# --- the token --------------------------------------------------------------
# Held in a plain string only for as long as it takes to write the file.
$secret = $env:ITS_AGENT_ENROLL_TOKEN
if ($TokenFromStdin) {
    if ($secret) { throw '-TokenFromStdin and ITS_AGENT_ENROLL_TOKEN are both set; pick one.' }
    $secret = [Console]::In.ReadLine()
}
if (-not $secret) {
    throw 'no enrollment token: set ITS_AGENT_ENROLL_TOKEN or use -TokenFromStdin.
Note that a PowerShell pipeline does NOT feed this script''s stdin -- redirect
at the OS level, e.g. cmd /c "type token.txt | powershell -NoProfile -File
its-agent-enroll.ps1 -TokenFromStdin -Url ...".'
}

if (-not (Test-Path $DataDir)) {
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
}

# The data directory holds the device private key and, briefly, the token.
# SYSTEM and Administrators only -- the NTFS equivalent of the 0700 that
# internal/config enforces on unix. Re-applied here rather than assumed,
# because on the managed path this script may run against a directory some
# other installer created. APPLIED BEFORE THE TOKEN IS WRITTEN, so the token is
# never briefly readable through a wider inherited ACL.
$acl = Get-Acl $DataDir
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRule($rule) }
foreach ($who in @('NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators')) {
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        $who, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
}
# The OWNER too, not only the DACL: a directory a local user created is owned
# by that user, and an owner always keeps WRITE_DAC.
$acl.SetOwner((New-Object System.Security.Principal.NTAccount('BUILTIN\Administrators')))
Set-Acl -Path $DataDir -AclObject $acl

# Never written INTO an existing file: a pre-created enroll-token keeps its
# creator's owner and ACL. Removed, then created with CreateNew, which fails if
# anything recreated it in between; it inherits the DACL above.
# No BOM and no trailing newline: a UTF-8 BOM would become part of the token
# and the control plane would answer a bare 403 that reads exactly like a
# revoked token.
$tokenFile = Join-Path $DataDir 'enroll-token'
Remove-Item -LiteralPath $tokenFile -Force -ErrorAction SilentlyContinue
$bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($secret)
$stream = [System.IO.File]::Open($tokenFile, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
$bytes = $null
$secret = $null

# --- the URL, in the service environment and NOT the token ------------------
# This registry value is readable by anyone who can read the service key, which
# is why only the URL is here. Any entry install.ps1 (or a future version of
# it) set that is neither of these two is preserved rather than dropped.
$keep = $priorEnv | Where-Object {
    $_ -notlike 'ITS_AGENT_DATA_DIR=*' -and $_ -notlike 'ITS_AGENT_ENROLL_URL=*'
}
$environment = @("ITS_AGENT_DATA_DIR=$DataDir", "ITS_AGENT_ENROLL_URL=$Url") + @($keep)
New-ItemProperty -Path $serviceKey -Name 'Environment' -PropertyType MultiString `
    -Value $environment -Force | Out-Null

Write-Host "  enrollment token written to $tokenFile (SYSTEM and Administrators only)"
Write-Host '  the agent deletes it as soon as it has exchanged it for a certificate'

if (-not $NoStart) {
    Restart-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    if ((Get-Service -Name $ServiceName).Status -ne 'Running') { Start-Service -Name $ServiceName }
    Write-Host "  started $ServiceName"
    Write-Host "  enrolled when:  Test-Path '$identity'"
    Write-Host "  still holding a token means NOT enrolled yet:  Test-Path '$tokenFile'"
} else {
    Write-Host '  not started (-NoStart)'
}
