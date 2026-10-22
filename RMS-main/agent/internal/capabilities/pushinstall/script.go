package pushinstall

import (
	"errors"
	"fmt"
	"net/url"
	"regexp"
	"strconv"
	"strings"

	"github.com/toeshee/it-suite/agent/internal/install/layout"
)

// Exit codes and stdout markers of the fixed install script. stdout carries
// only these markers; stderr is never read back (PowerShell echoes failing
// script lines there).
const (
	scriptExitArchMismatch   = 3
	scriptExitDigestMismatch = 4
	scriptExitNoToken        = 5
	// scriptExitUnsafeDirectory: %ProgramData%\ITSuite, push or agent is a
	// reparse point (or not a directory). Nothing under it is used.
	scriptExitUnsafeDirectory = 6
	// scriptExitInstalledMismatch: the binary in %ProgramFiles% does not hash
	// to the signed SHA-256 after the copy. It is deleted; the service is not
	// started.
	scriptExitInstalledMismatch = 7
	// scriptExitNoLayout: the session has no rooted %ProgramFiles% or
	// %ProgramData%, so there is nowhere to resolve the layout against.
	scriptExitNoLayout = 8
	// scriptExitForeignRoot: %ProgramData%\ITSuite already exists and is
	// owned by neither Administrators nor SYSTEM. It is left untouched: what
	// its owner planted inside, or a handle they opened earlier, would survive
	// a new owner and a new DACL (the install link refuses it for the same
	// reason).
	scriptExitForeignRoot = 9
	scriptMarkerInstalled = "ITS-PUSH-INSTALLED"
	scriptMarkerArch      = "ITS-PUSH-ARCH="
)

var (
	hexDigest = regexp.MustCompile(`^[0-9a-fA-F]{64}$`)
	// safeURL: printable ASCII with none of PowerShell's quote, escape or
	// expansion characters and no whitespace. The value is quoted anyway; this
	// is the second wall, and a signed URL never needs any of these.
	safeURL = regexp.MustCompile(`^[A-Za-z0-9._~:/?#\[\]@!&()*+,;=%-]+$`)
)

// psQuote renders s as a PowerShell single-quoted (verbatim) string. Inside
// one, nothing expands; the only special character is the single quote, which
// PowerShell also recognises in its typographic forms U+2018..U+201B. Every
// form is doubled, so no value can close the string.
func psQuote(s string) string {
	var b strings.Builder
	b.WriteByte('\'')
	for _, r := range s {
		b.WriteRune(r)
		switch r {
		case '\'', '‘', '’', '‚', '‛':
			b.WriteRune(r)
		}
	}
	b.WriteByte('\'')
	return b.String()
}

// consoleOrigin reduces an https URL to its origin (https only, no userinfo).
// checkConsole uses it to require that the signed enroll_url IS a bare origin.
// It is NOT used to derive the enroll URL from the presigned release URL: that
// was plan W2, superseded 2026-09-29, because with external object storage the
// release URL's origin is the storage provider, not the console.
func consoleOrigin(raw string) (string, error) {
	u, err := url.Parse(raw)
	if err != nil {
		return "", fmt.Errorf("release URL: %w", err)
	}
	if u.Scheme != "https" || u.Host == "" || u.User != nil {
		return "", errors.New("release URL is not an https URL with a host and no userinfo")
	}
	o := "https://" + u.Host
	if !safeURL.MatchString(o) {
		return "", errors.New("release URL host carries characters a URL never needs")
	}
	return o, nil
}

// checkConsole requires an origin exactly as consoleOrigin produces one.
func checkConsole(console string) error {
	o, err := consoleOrigin(console)
	if err != nil {
		return fmt.Errorf("console URL: %w", err)
	}
	if o != console {
		return fmt.Errorf("console URL %q is not a bare https origin", console)
	}
	return nil
}

// BuildInstallScript is the FIXED PowerShell a WinRM push runs on the target
// (design §2.2). It is a constant template filled ONLY with layout.Plan layout
// (PR 1's self-installer layout, D5 -- one place names %ProgramFiles%\ITSuite
// and %ProgramData%\ITSuite\agent) and signed-param VALUES: the presigned URL,
// the SHA-256, the declared arch, and the signed enroll_url. No operator- or
// platform-authored command travels (2a clause (b)); the only thing that
// executes is the platform's own published its-agent.exe, placed by these
// fixed steps. Every value is validated and single-quoted.
//
// THE ENROLMENT TOKEN IS NOT IN THE SCRIPT. The script is powershell.exe's
// -EncodedCommand -- its command line, visible to process listings, Security
// event 4688 and script-block logging -- so the token arrives on stdin, the
// same rule install.ps1 enforces by refusing -Token.
//
// The steps mirror install.ps1: verify the arch; protect the vendor root;
// staging dir with a protected SYSTEM+Administrators DACL owned by
// Administrators; download and verify the SHA-256 (refuse on mismatch, before
// anything is placed); stop an existing service; copy to
// %ProgramFiles%\ITSuite\its-agent.exe and VERIFY IT AGAIN THERE; write the
// token file (CreateNew, no BOM) in the protected data dir; register the
// service with no arguments and the data dir + enrol URL in its Environment;
// recovery actions from the plan; start; delete the staging dir.
//
// REVIEW C1 (2026-09-29). A standard user may create %ProgramData%\ITSuite
// before any agent is installed, and would then own it -- and with it the
// staging and data directories, whatever DACL they were later given: swap
// push\its-agent.exe between the hash and the copy (an oplock makes that race
// deterministic), or plant an agent\ identity, and their code runs as SYSTEM.
// So, before anything under it is used, the root is created WITH its
// descriptor or taken (owner BUILTIN\Administrators, protected SYSTEM +
// Administrators DACL); a root that already exists owned by anyone but
// Administrators or SYSTEM is refused and left as it is (scriptExitForeignRoot)
// -- it was discarded-and-continued until the install link settled the rule;
// every directory is refused if it is a reparse point, checked again before
// every recursive delete; recursive deletes use Directory.Delete, which does
// not descend through a junction (Windows PowerShell 5.1's Remove-Item
// -Recurse can); and -- whatever happened before -- the binary is hashed AGAIN
// in admin-only %ProgramFiles% and a mismatch is refused before the service is
// started. The same approach PR 1's self-installer and install.ps1 take for
// the root (owner BA, protected SYSTEM+Administrators DACL, reparse points
// refused).
//
// PATHS COME FROM THE TARGET (review Minor 7): the plan's layout is expressed
// relative to %ProgramFiles% and %ProgramData% and joined, on the target, to
// $env:ProgramFiles and $env:ProgramData -- never a literal C:\.
func BuildInstallScript(p layout.Plan, rel release, arch, consoleURL string) (string, error) {
	if !hexDigest.MatchString(rel.SHA256) {
		return "", errors.New("install script: the signed SHA-256 is not 64 hex digits")
	}
	u, err := url.Parse(rel.URL)
	if err != nil || u.Scheme != "https" || u.Host == "" || !safeURL.MatchString(rel.URL) {
		return "", errors.New("install script: the release URL is not a plain https URL")
	}
	if arch != "amd64" && arch != "arm64" {
		return "", fmt.Errorf("install script: unsupported arch %q", arch)
	}
	if err := checkConsole(consoleURL); err != nil {
		return "", fmt.Errorf("install script: %w", err)
	}
	rels, err := relativeLayout(p)
	if err != nil {
		return "", fmt.Errorf("install script: %w", err)
	}
	actions := make([]string, 0, len(p.RecoveryDelays))
	for _, d := range p.RecoveryDelays {
		actions = append(actions, "restart/"+strconv.FormatInt(d.Milliseconds(), 10))
	}

	r := strings.NewReplacer(
		"{{root}}", psQuote(rels.root),
		"{{staging}}", psQuote(rels.staging),
		"{{install}}", psQuote(rels.install),
		"{{target}}", psQuote(rels.target),
		"{{data}}", psQuote(rels.data),
		"{{tokenFile}}", psQuote(rels.token),
		"{{svc}}", psQuote(p.ServiceName),
		"{{display}}", psQuote(p.DisplayName),
		"{{desc}}", psQuote(p.Description),
		"{{url}}", psQuote(rel.URL),
		"{{want}}", psQuote(strings.ToLower(rel.SHA256)),
		"{{arch}}", psQuote(arch),
		"{{enrollUrl}}", psQuote(consoleURL),
		"{{reset}}", strconv.FormatUint(uint64(p.RecoveryResetS), 10),
		"{{actions}}", strings.Join(actions, "/"),
		"{{exitArch}}", strconv.Itoa(scriptExitArchMismatch),
		"{{exitDigest}}", strconv.Itoa(scriptExitDigestMismatch),
		"{{exitToken}}", strconv.Itoa(scriptExitNoToken),
		"{{exitUnsafe}}", strconv.Itoa(scriptExitUnsafeDirectory),
		"{{exitInstalled}}", strconv.Itoa(scriptExitInstalledMismatch),
		"{{exitNoLayout}}", strconv.Itoa(scriptExitNoLayout),
		"{{exitForeign}}", strconv.Itoa(scriptExitForeignRoot),
		"{{markInstalled}}", scriptMarkerInstalled,
		"{{markArch}}", scriptMarkerArch,
	)
	return r.Replace(installScriptTemplate), nil
}

// scriptLayout is the plan's layout relative to %ProgramFiles% (install,
// target) and %ProgramData% (root, staging, data, token).
type scriptLayout struct{ root, staging, install, target, data, token string }

// relativeLayout expresses the plan relative to the two known folders it was
// built under: %ProgramFiles% is InstallDir's parent, %ProgramData% the
// parent of the vendor root that holds both the data and staging directories.
func relativeLayout(p layout.Plan) (scriptLayout, error) {
	programFiles := winParent(p.InstallDir)
	root := winParent(p.DataDir)
	programData := winParent(root)
	if programFiles == "" || root == "" || programData == "" || winParent(p.StagingDir) != root {
		return scriptLayout{}, errors.New("the install layout is not <ProgramFiles>\\<vendor> and <ProgramData>\\<vendor>\\{agent,push}")
	}
	var out scriptLayout
	for _, f := range []struct {
		dst        *string
		path, base string
	}{
		{&out.root, root, programData},
		{&out.staging, p.StagingDir, programData},
		{&out.data, p.DataDir, programData},
		{&out.token, p.TokenPath, programData},
		{&out.install, p.InstallDir, programFiles},
		{&out.target, p.Target, programFiles},
	} {
		rel, ok := strings.CutPrefix(f.path, f.base+`\`)
		if !ok || rel == "" || strings.Contains(rel, "..") {
			return scriptLayout{}, fmt.Errorf("%q is not under %q", f.path, f.base)
		}
		*f.dst = rel
	}
	return out, nil
}

// winParent is a backslash path's parent, or "" at a root.
func winParent(path string) string {
	i := strings.LastIndex(path, `\`)
	if i <= 0 {
		return ""
	}
	return path[:i]
}

// installScriptTemplate is compiled in. {{name}} slots are filled only by
// BuildInstallScript with validated, psQuote'd values.
const installScriptTemplate = `$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ProgressPreference = 'SilentlyContinue'

# 0. The layout, resolved against THIS machine's folders (never a literal drive).
foreach ($folder in @($env:ProgramFiles, $env:ProgramData)) {
    if (-not $folder -or -not [System.IO.Path]::IsPathRooted($folder)) { exit {{exitNoLayout}} }
}
$root      = Join-Path $env:ProgramData {{root}}
$staging   = Join-Path $env:ProgramData {{staging}}
$data      = Join-Path $env:ProgramData {{data}}
$tokenFile = Join-Path $env:ProgramData {{tokenFile}}
$install   = Join-Path $env:ProgramFiles {{install}}
$target    = Join-Path $env:ProgramFiles {{target}}

# Fixed values: the shared install layout (internal/install) and the signed job.
$svc       = {{svc}}
$display   = {{display}}
$desc      = {{desc}}
$url       = {{url}}
$want      = {{want}}
$declared  = {{arch}}
$enrollUrl = {{enrollUrl}}

# 1. The operator declared an arch; the target must agree before anything else.
$a = $env:PROCESSOR_ARCHITEW6432
if (-not $a) { $a = $env:PROCESSOR_ARCHITECTURE }
$got = "$a".ToLowerInvariant()
if ($got -eq 'x86_64') { $got = 'amd64' }
Write-Output ('{{markArch}}' + $got)
if ($got -ne $declared) { exit {{exitArch}} }

# 2. The enrolment token arrives on stdin, never on this command line.
$token = [Console]::In.ReadLine()
if (-not $token) { exit {{exitToken}} }

# SYSTEM and Administrators only, owned by Administrators; by SID, so a
# localised Windows resolves the same principals.
function New-DirSecurity {
    $sec = New-Object System.Security.AccessControl.DirectorySecurity
    $sec.SetAccessRuleProtection($true, $false)
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) {
        $who = New-Object System.Security.Principal.SecurityIdentifier($sid)
        $sec.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            $who, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    }
    $sec.SetOwner((New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')))
    return $sec
}

# A junction or symlink anywhere in the layout is refused, never followed.
function Assert-SafeDirectory([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -or -not $item.PSIsContainer) {
        exit {{exitUnsafe}}
    }
}

# A recursive delete that does not descend through a junction: .NET's
# Directory.Delete removes a reparse point without recursing into it.
function Remove-Tree([string]$Path) {
    Assert-SafeDirectory $Path
    if (Test-Path -LiteralPath $Path) { [System.IO.Directory]::Delete($Path, $true) }
}

# Created WITH its descriptor: there is no moment it has an inherited DACL.
function New-ProtectedDirectory([string]$Path) {
    Assert-SafeDirectory $Path
    [void][System.IO.Directory]::CreateDirectory($Path, (New-DirSecurity))
    Assert-SafeDirectory $Path
    (Get-Item -LiteralPath $Path -Force).SetAccessControl((New-DirSecurity))
}

# THE VENDOR ROOT FIRST (review C1). A root owned by anyone but
# Administrators or SYSTEM is refused and left as it is -- never taken over:
# what its owner planted inside, or a handle they opened earlier, survives a
# new owner and a new DACL. The install link's installer refuses it the same way.
function Protect-Root([string]$Path) {
    Assert-SafeDirectory $Path
    if (-not (Test-Path -LiteralPath $Path)) {
        New-ProtectedDirectory $Path
        return
    }
    $owner = (Get-Acl -LiteralPath $Path).GetOwner([System.Security.Principal.SecurityIdentifier]).Value
    $ours = @('S-1-5-32-544', 'S-1-5-18') -contains $owner
    if (-not $ours) { exit {{exitForeign}} }
    # takeown /A: owner BUILTIN\Administrators, whatever the DACL says --
    # it enables the take-ownership privilege Set-Acl would not.
    & takeown.exe /F $Path /A | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "takeown exited $LASTEXITCODE" }
    Assert-SafeDirectory $Path
    (Get-Item -LiteralPath $Path -Force).SetAccessControl((New-DirSecurity))
    Assert-SafeDirectory $Path
}

# 3. The vendor root, then a fresh, protected staging dir. The window between
# the first reparse check and takeown is the one this script cannot close
# without opening handles itself; every step after takeown re-checks, and
# once the root is protected no one but SYSTEM and Administrators can change
# anything beneath it.
Assert-SafeDirectory $root
Protect-Root $root
Assert-SafeDirectory $data
Assert-SafeDirectory $staging
Remove-Tree $staging
New-ProtectedDirectory $staging

# 4. Download, then verify the signed SHA-256 BEFORE anything is placed.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$pkg = Join-Path $staging 'its-agent.exe'
Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $pkg
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $pkg).Hash.ToLowerInvariant()
if ($hash -ne $want) {
    Remove-Tree $staging
    exit {{exitDigest}}
}

# 5. Stop an existing service before its image is replaced (install.ps1's order).
$existing = Get-Service -Name $svc -ErrorAction SilentlyContinue
if ($existing -and $existing.Status -ne 'Stopped') {
    Stop-Service -Name $svc -Force
    $existing.WaitForStatus('Stopped', (New-TimeSpan -Seconds 30))
}
New-Item -ItemType Directory -Force -Path $install | Out-Null
$copied = $false
foreach ($attempt in 1..20) {
    try { Copy-Item -LiteralPath $pkg -Destination $target -Force; $copied = $true; break }
    catch { Start-Sleep -Milliseconds 500 }
}
if (-not $copied) { throw 'the previous its-agent.exe is still in use' }

# 5b. VERIFY AGAIN WHERE IT WILL RUN (review C1). %ProgramFiles% is
# administrators-only; whatever happened in the staging directory, the file the
# service will start as SYSTEM is the signed one or nothing starts.
$placed = (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash.ToLowerInvariant()
if ($placed -ne $want) {
    Remove-Item -LiteralPath $target -Force
    Remove-Tree $staging
    exit {{exitInstalled}}
}

# 6. The token file: protected dir, never written into an existing file, no BOM.
New-ProtectedDirectory $data
Remove-Item -LiteralPath $tokenFile -Force -ErrorAction SilentlyContinue
$bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($token)
$stream = [System.IO.File]::Open($tokenFile, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
$bytes = $null
$token = $null

# 7. The service: binPath is the quoted target and NOTHING else.
if (-not $existing) {
    New-Service -Name $svc -BinaryPathName "` + "`" + `"$target` + "`" + `"" -DisplayName $display -Description $desc -StartupType Automatic | Out-Null
}
$serviceKey = 'HKLM:\SYSTEM\CurrentControlSet\Services\' + $svc
New-ItemProperty -Path $serviceKey -Name 'Environment' -PropertyType MultiString -Value @("ITS_AGENT_DATA_DIR=$data", "ITS_AGENT_ENROLL_URL=$enrollUrl") -Force | Out-Null
& sc.exe failure $svc reset= {{reset}} actions= {{actions}} | Out-Null
if ($LASTEXITCODE -ne 0) { throw "sc.exe failure exited $LASTEXITCODE" }
Start-Service -Name $svc

# 8. Nothing of the push is left behind but the installed agent.
Remove-Tree $staging
Write-Output '{{markInstalled}}'
exit 0
`
