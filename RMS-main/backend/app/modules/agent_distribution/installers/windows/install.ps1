<#
.SYNOPSIS
    Install the Toeshee agent as a Windows service.

.DESCRIPTION
    Places its-agent.exe, optionally writes the enrollment token to a file only
    SYSTEM and Administrators can read, and registers the service with the
    Service Control Manager.

    IT DOES NOT ALWAYS START THE SERVICE, and that is deliberate rather than a
    gap: an agent with neither a token nor an identity exits 2 on every start,
    and the recovery actions configured below would turn that into a restart
    loop. On the managed path (GPO, Intune) "installed, not yet enrolled" is the
    NORMAL intermediate state -- install.ps1 puts the service on the machine and
    its-agent-enroll.ps1 puts the credential on it, afterwards, from the
    channel's own secret mechanism.

    THE TOKEN IS NEVER A PARAMETER. -Token is REFUSED, with the reason.
    PowerShell records every command line in the per-user history file
    (PSReadLine's ConsoleHost_history.txt), Windows records process command
    lines in Security event 4688 where auditing is on, and any local user can
    read them from a running process. It arrives in the environment variable
    ITS_AGENT_ENROLL_TOKEN, or on stdin with -TokenFromStdin.

    Invariant #2: the agent opens no listening port, so no firewall rule is
    created here and none is needed.

    UNTESTED. No Windows host has run this script. The SCM integration it
    depends on IS built (agent/internal/service/service_windows.go, using
    golang.org/x/sys/windows/svc) and its handler test compiles for
    windows/amd64 in CI -- but nothing in this repository has executed either.
    Read agent/packaging/README.md "What is untested" and follow
    docs/modules/agent-distribution/first-run-verification.md before a fleet
    rollout.

.PARAMETER Url
    Control-plane API base URL, e.g. https://its.example.com. Not a secret.
    On a re-install the previously configured URL is kept when this is omitted.

.PARAMETER Binary
    Path to its-agent.exe. Defaults to ..\..\dist\its-agent-windows-amd64.exe.

.PARAMETER TokenFromStdin
    Read the enrollment token from the process's standard input instead of the
    environment.

    THIS MEANS REAL REDIRECTED STDIN, NOT A POWERSHELL PIPELINE. A .ps1 runs in
    the calling PowerShell process, so `'tok' | .\install.ps1 -TokenFromStdin`
    sends the string down the PowerShell pipeline and leaves the process's
    stdin attached to the console -- the script would sit waiting for somebody
    to type. Redirect at the OS level, or use the environment variable:

        cmd /c "type token.txt | powershell -NoProfile -File install.ps1 -TokenFromStdin -Url https://..."

.PARAMETER NoStart
    Register the service but do not start it. This is what a managed
    deployment's install step uses.

.EXAMPLE
    # Interactive, without the token ever reaching a command line or the
    # history file. Read-Host -AsSecureString does not echo.
    $sec = Read-Host -AsSecureString 'enrollment token'
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { $env:ITS_AGENT_ENROLL_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    .\install.ps1 -Url https://its.example.com
    $env:ITS_AGENT_ENROLL_TOKEN = $null

.EXAMPLE
    # Managed deployment: install only, enrol separately.
    .\install.ps1 -Binary .\its-agent-windows-amd64.exe -NoStart
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)][string]$Url = $env:ITS_AGENT_ENROLL_URL,
    [string]$Binary,
    [switch]$TokenFromStdin,
    [switch]$NoStart,
    [string]$InstallDir = "$env:ProgramFiles\ITSuite",
    [string]$DataDir = "$env:ProgramData\ITSuite\agent",
    # -Token EXISTS ONLY TO BE REFUSED, exactly as in its-agent-enroll.ps1 and
    # as `--token` is refused by linux/install.sh, darwin/install.sh and
    # linux/its-agent-enroll. Left out of the parameter block entirely,
    # PowerShell answers a bare "A parameter cannot be found that matches
    # parameter name 'Token'", which teaches the operator nothing and sends
    # them looking for the right spelling rather than the right method.
    [string]$Token
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ServiceName = 'its-agent'
$DisplayName = 'ITMS Agent'
$Description = 'Toeshee endpoint agent. Maintains one outbound mutual-TLS connection to the control plane and executes only cryptographically signed jobs. Opens no listening port.'

if ($Token) {
    throw @'
the enrollment token must not be passed as an argument: PowerShell keeps command
lines in its history file, Windows records them in Security event 4688 where
command-line auditing is on, and any local user can read them from a running
process.
  Use:  $env:ITS_AGENT_ENROLL_TOKEN = <token>; .\install.ps1 -Url ...
  or:   cmd /c "type token.txt | powershell -NoProfile -File install.ps1 -TokenFromStdin -Url ..."
'@
}

function Assert-Administrator {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'install.ps1 must run from an elevated PowerShell session.'
    }
}

Assert-Administrator

# A junction or symbolic link where a directory of ours should be is refused:
# everything this script then did to "the directory" would happen to wherever
# the link points, and the link itself would stay in the hands of whoever made
# it. FileSystemInfo.Attributes are the link's own, not its target's.
function Assert-NotReparsePoint([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return }
    if (([int]$item.Attributes -band [int][System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Path is a junction or symbolic link, not a directory. Nothing was installed; an administrator must remove it and run install.ps1 again."
    }
    if (-not $item.PSIsContainer) {
        throw "$Path exists and is not a directory. Nothing was installed."
    }
}

# An existing directory on a FRESH install must belong to Administrators,
# SYSTEM or the administrator running this. Taking ownership makes it ours
# from now on, but whatever its previous owner planted inside -- an
# identity.json and pins.json naming their gateway and job-signing key --
# survives the new ACL.
function Assert-ItsOwner([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $owner = (Get-Acl -LiteralPath $Path).GetOwner([System.Security.Principal.SecurityIdentifier])
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User
    if ($owner.Value -eq 'S-1-5-32-544' -or $owner.Value -eq 'S-1-5-18' -or $owner.Value -eq $me.Value) { return }
    $name = $owner.Value
    try { $name = $owner.Translate([System.Security.Principal.NTAccount]).Value } catch { }
    throw "$Path already exists and belongs to $name, not to an administrator. Nothing was installed; delete it and run install.ps1 again."
}

# The data directory's PARENT, %ProgramData%\ITSuite, is where the data
# directory's protection actually lives. On a default Windows install
# BUILTIN\Users may create folders in C:\ProgramData and CREATOR OWNER gets
# Full Control on them, so a standard user can create ITSuite before this
# script runs -- and whoever owns a directory holds FILE_DELETE_CHILD on it:
# they rename the protected `agent` directory away afterwards and put their
# own in its place, with an identity that points this SYSTEM service at their
# gateway and their job-signing key. So ITSuite gets the data directory's own
# treatment -- owner Administrators, SYSTEM and Administrators only, nothing
# inherited -- BEFORE `agent` is created or touched. The self-installer does
# the same (internal/install prepareDirs, secdir.SecureOwned).
function Protect-ItsDirectory([string]$Path, [switch]$RefuseForeignOwner) {
    $sec = New-Object System.Security.AccessControl.DirectorySecurity
    $sec.SetAccessRuleProtection($true, $false)
    foreach ($who in @('NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators')) {
        $sec.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            $who, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    }
    $sec.SetOwner((New-Object System.Security.Principal.NTAccount('BUILTIN\Administrators')))
    if (-not (Test-Path -LiteralPath $Path)) {
        # Created WITH its descriptor where this PowerShell can (Windows
        # PowerShell 5.1), so it never exists with the ACL ProgramData would
        # hand it. Elsewhere it is created and secured in two steps; the
        # checks below hold either way.
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
    # Read back, through the name, AFTER the change: still not a link, and
    # ours. Once it is, nobody but SYSTEM and Administrators can rename it.
    Assert-NotReparsePoint $Path
    $owner = (Get-Acl -LiteralPath $Path).GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    if ($owner -ne 'S-1-5-32-544') {
        throw "$Path could not be given to BUILTIN\Administrators (its owner is $owner). Nothing was installed."
    }
}

# --- the binary ------------------------------------------------------------
# $PSScriptRoot, not $MyInvocation.MyCommand.Path: the latter is empty when the
# script is dot-sourced or run through `powershell -Command`, and
# `Split-Path -Parent ''` then throws under ErrorActionPreference=Stop.
if (-not $Binary) {
    $here = $PSScriptRoot
    foreach ($candidate in @(
            (Join-Path $here '..\..\dist\its-agent-windows-amd64.exe'),
            (Join-Path $here '..\..\dist\its-agent-windows-arm64.exe'),
            (Join-Path $here '..\..\dist\its-agent.exe'),
            (Join-Path $here 'its-agent.exe'))) {
        if (Test-Path $candidate) { $Binary = (Resolve-Path $candidate).Path; break }
    }
}
if (-not $Binary -or -not (Test-Path $Binary)) {
    throw 'No its-agent.exe found. Build one with agent/packaging/build.sh and pass -Binary.'
}

# --- the token -------------------------------------------------------------
# Held in a plain string only for as long as it takes to write the file. It is
# never written to the transcript, never passed to a child process, and never
# put in the service's Environment value.
$token = $env:ITS_AGENT_ENROLL_TOKEN
if ($TokenFromStdin) {
    if ($token) { throw '-TokenFromStdin and ITS_AGENT_ENROLL_TOKEN are both set; pick one.' }
    $token = [Console]::In.ReadLine()
    # ReadLine returns $null at end of stream. Asked for stdin and given none is
    # an error, not an unenrolled install: silently proceeding here is how a
    # deployment ends with a service on every machine and a token on none.
    if (-not $token) {
        throw '-TokenFromStdin was given but standard input was empty. Note that a
PowerShell pipeline does NOT feed this script''s stdin -- redirect at the OS
level, e.g. cmd /c "type token.txt | powershell -NoProfile -File install.ps1 ..."'
    }
}
if ($token -and -not $Url) { throw '-Url is required alongside an enrollment token.' }

# --- directories -----------------------------------------------------------
# A fresh install is one with no service yet. Known before any directory is
# touched: on a fresh install an existing directory someone else owns is
# refused rather than taken over (Assert-ItsOwner).
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
$fresh = -not $existing

# %ProgramFiles%\ITSuite keeps the ACL it inherits: C:\Program Files grants
# BUILTIN\Users read and execute only, so they cannot pre-create it. A link
# there is still refused.
Assert-NotReparsePoint $InstallDir
if ($fresh) { Assert-ItsOwner $InstallDir }
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

# The PARENT first (Protect-ItsDirectory above), and only a parent that is
# ours to lock down: a custom -DataDir of C:\ITS must not end with a
# protected DACL on C:\.
$DataParent = Split-Path -Parent $DataDir
if ((Split-Path -Leaf $DataParent) -eq 'ITSuite') {
    Protect-ItsDirectory -Path $DataParent -RefuseForeignOwner:$fresh
} else {
    Write-Warning "$DataParent is not an ITSuite directory, so install.ps1 leaves its ACL alone. Anyone who can rename entries in it can replace $DataDir; make sure only Administrators can."
}
Assert-NotReparsePoint $DataDir
if ($fresh) { Assert-ItsOwner $DataDir }
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

# The data directory holds the device private key. Break inheritance and grant
# SYSTEM and Administrators only -- the NTFS equivalent of the 0700 that
# internal/config enforces on unix.
$acl = Get-Acl $DataDir
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRule($rule) }
foreach ($who in @('NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators')) {
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        $who, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
}
# The OWNER too, not only the DACL. A directory a local user created before
# this script ran is owned by that user, and an owner always keeps WRITE_DAC --
# it could put its own ACE back the moment this script finished.
$acl.SetOwner((New-Object System.Security.Principal.NTAccount('BUILTIN\Administrators')))
Set-Acl -Path $DataDir -AclObject $acl

# --- token file ------------------------------------------------------------
if ($token) {
    $tokenFile = Join-Path $DataDir 'enroll-token'
    # Never written INTO an existing file: a pre-created enroll-token keeps its
    # creator's owner and ACL. Removed, then created with CreateNew, which
    # fails if anything recreated it in between; it inherits the DACL above.
    Remove-Item -LiteralPath $tokenFile -Force -ErrorAction SilentlyContinue
    # No BOM and no trailing newline: the agent trims whitespace, but a UTF-8
    # BOM would become part of the token and the control plane would answer a
    # bare 403 that reads exactly like a revoked token.
    $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($token)
    $stream = [System.IO.File]::Open($tokenFile, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
    $bytes = $null
    $token = $null
    # Inherits the directory ACL set above (SYSTEM + Administrators only).
    Write-Host "  enrollment token written to $tokenFile (SYSTEM and Administrators only)"
    Write-Host '  the agent deletes it as soon as it has exchanged it for a certificate'
}

# --- stop an existing service BEFORE the binary is replaced ----------------
# ORDER IS LOAD-BEARING AND WAS WRONG. Windows holds an open section on a
# running executable's image file, so Copy-Item over its-agent.exe while the
# service runs fails with "The process cannot access the file because it is
# being used by another process" -- and with ErrorActionPreference=Stop that
# aborts the whole re-install, after the token has already been written. Every
# upgrade-in-place would have hit it; a first install never would, which is
# exactly the shape of defect an unexecuted script keeps. ($existing is the
# Get-Service taken before the directories, above.)
if ($existing -and $existing.Status -ne 'Stopped') {
    Stop-Service -Name $ServiceName -Force
    $existing.WaitForStatus('Stopped', (New-TimeSpan -Seconds 30))
}

# --- binary ----------------------------------------------------------------
# The SCM can report Stopped a moment before the process has actually exited
# and released its image, so this is retried rather than attempted once.
$target = Join-Path $InstallDir 'its-agent.exe'
$copied = $false
foreach ($attempt in 1..20) {
    try { Copy-Item -Path $Binary -Destination $target -Force; $copied = $true; break }
    catch { Start-Sleep -Milliseconds 500 }
}
if (-not $copied) {
    throw "could not write $target -- the previous its-agent.exe is still in use. Stop the $ServiceName service and re-run."
}

# --- service ---------------------------------------------------------------
if (-not $existing) {
    # binPath has NO arguments. Everything on a service command line is
    # readable by any local user for the life of the process, which is why the
    # token is in a file instead.
    New-Service -Name $ServiceName -BinaryPathName "`"$target`"" `
        -DisplayName $DisplayName -Description $Description `
        -StartupType Automatic | Out-Null
}

# The service's own environment block. The URL is here; the token is NOT, and
# must never be -- this value is readable by anyone who can read the service key.
$serviceKey = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"

# A RE-INSTALL WITHOUT -Url MUST NOT ERASE THE URL. New-ItemProperty -Force
# replaces the whole MultiString, so the previous version dropped
# ITS_AGENT_ENROLL_URL on any re-run that did not repeat it -- leaving a
# staged, not-yet-enrolled machine with a token it could never spend.
$priorEnv = @()
try { $priorEnv = @((Get-ItemProperty -Path $serviceKey -Name 'Environment').Environment) } catch { $priorEnv = @() }
$effectiveUrl = $Url
if (-not $effectiveUrl) {
    $prior = $priorEnv | Where-Object { $_ -like 'ITS_AGENT_ENROLL_URL=*' } | Select-Object -First 1
    if ($prior) { $effectiveUrl = $prior.Substring('ITS_AGENT_ENROLL_URL='.Length) }
}
$environment = @("ITS_AGENT_DATA_DIR=$DataDir")
if ($effectiveUrl) { $environment += "ITS_AGENT_ENROLL_URL=$effectiveUrl" }
New-ItemProperty -Path $serviceKey -Name 'Environment' -PropertyType MultiString `
    -Value $environment -Force | Out-Null

# Recovery: restart on every failure, matching Restart=always in the systemd
# unit. reset= 300 means "forget the failure count after five clean minutes",
# so a broken install backs off instead of hot-looping.
& sc.exe failure $ServiceName reset= 300 actions= restart/5000/restart/15000/restart/60000 | Out-Null
if ($LASTEXITCODE -ne 0) {
    # A native command's non-zero exit does NOT trip ErrorActionPreference, so
    # this would otherwise be a silent no-op leaving the service with no
    # recovery actions at all.
    throw "sc.exe failure $ServiceName exited $LASTEXITCODE; the service has no recovery actions configured."
}

# AN UNENROLLED AGENT IS NOT STARTED, and on the managed path that is the
# NORMAL case rather than an operator error.
#
# The agent exits 2 on every start when it has neither a token to spend nor an
# identity it already holds, and `sc.exe failure ... restart` above turns that
# into a restart loop -- which, on a GPO rollout that installs first and enrols
# second, is a crashing service on every machine in the domain and an event log
# full of it.
#
# linux/debian/postinst has always got this right and says why in the same
# words: "an agent with neither a token nor an identity exits 2 on every start,
# and Restart=always would turn a forgotten enrollment step into a restart loop
# that reads like a crash." This is that, for Windows.
#
# Note the ORDER of the two tests. `identity.json` first, because an endpoint
# that is already enrolled must start whether or not a token file happens to be
# lying around; `enroll-token` second, because a fresh endpoint holding one is
# about to spend it. Neither file means there is nothing to start FOR.
$hasIdentity = Test-Path (Join-Path $DataDir 'identity.json')
$hasToken = Test-Path (Join-Path $DataDir 'enroll-token')
if ($NoStart) {
    Write-Host '  service registered, not started (-NoStart)'
} elseif ($hasIdentity -or $hasToken) {
    Start-Service -Name $ServiceName
} else {
    Write-Host '  service registered and NOT started: this endpoint has no enrollment token'
    Write-Host '  and no identity, so the agent would exit 2 on every start and the failure'
    Write-Host '  actions above would restart it forever.'
    Write-Host '  Enroll it with:  .\its-agent-enroll.ps1 -Url https://its.example.com'
}

Write-Host ''
Write-Host 'its-agent installed.'
Write-Host "  status    Get-Service $ServiceName"
Write-Host "  logs      the agent writes to STDERR and nothing here registers an event"
Write-Host "            source, so the Application log has nothing to read yet."
Write-Host "            Run '$target' from an elevated console to watch it (agent/packaging/README.md)."
Write-Host "  enrolled  Test-Path '$DataDir\identity.json'"
Write-Host "  enroll    .\its-agent-enroll.ps1 -Url https://its.example.com  (separate step; token from the environment or stdin)"
Write-Host "  remove    .\uninstall.ps1"
Write-Host ''
Write-Host 'NOT done here and required before a real rollout: the binary is UNSIGNED.'
Write-Host 'See agent/packaging/README.md "Code signing" and'
Write-Host 'docs/modules/agent-distribution/authenticode-signing.md.'
