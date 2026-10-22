package deploy

import (
	"fmt"
	"sort"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// THE ONLY PLACE A PROCESS IS NAMED, AND THE LIST THE EDR TEAM GETS.
//
// Every program this agent may spawn for `script.run` or `app.deploy` appears
// in `spawnable` below as a compile-time constant. Nothing in this file reads
// a job-supplied string to decide WHAT to run — job-supplied strings can only
// become arguments, and only after passing args.go's allowlist. The one
// exception is an `exe` artifact, which IS the program by definition; it is
// the file this agent just downloaded and hash-verified, at a path this agent
// chose, and it is called out explicitly below.
//
// The planners are pure functions of (goos, params, paths) with LookPath
// injected. That is what makes the Windows and macOS plans testable on the
// Linux container this was built in: the ARGV is checked here, and only the
// spawn itself is unverified. See the report's "what never executed".

// spawnable is the full, closed list of program names this package can ask
// LookPath to resolve, with the platform and the reason.
//
// It is exported through TestTheDocumentedSpawnListIsTheOnlyOneThePlannerCanProduce,
// which drives every (goos, interpreter) and (goos, installer_type, action)
// combination through the planners and asserts that no program outside this
// map is ever named. An EDR allowlist built from this map is complete by
// construction rather than by inspection.
var spawnable = map[string]string{
	// script.run
	"pwsh":           "PowerShell 7+ (windows, macos). Preferred over powershell.exe where present: it is a distinct binary, so an EDR rule can tell a managed script run from a Windows PowerShell one.",
	"pwsh.exe":       "PowerShell 7+ on Windows.",
	"powershell.exe": "Windows PowerShell 5.1, the fallback when pwsh.exe is absent. Invoked as `-NoProfile -NonInteractive -NoLogo -ExecutionPolicy Bypass -File <path>`; NEVER -EncodedCommand and never -Command.",
	"bash":           "bash script bodies on macOS and Linux. Invoked as `bash <path>`; never `bash -c`.",
	"python3":        "python script bodies. Preferred spelling.",
	"python":         "python script bodies, when python3 is absent.",

	// app.deploy
	"msiexec.exe": "MSI install (/i) and uninstall (/x) on Windows.",
	"winget.exe":  "manager-sourced install/upgrade/uninstall on Windows.",
	"choco.exe":   "manager-sourced install/upgrade/uninstall on Windows.",
	"installer":   "macOS .pkg install (/usr/sbin/installer -pkg <path> -target /).",
}

// artifactIsTheProgram names the one installer type whose program is not in
// `spawnable`, because there is no name to put there: an `exe` package IS a
// downloaded file. It is spawned only from the verified path — install.go's
// `stage` does not produce that path until the SHA-256 in the signed params
// matches the fetched bytes.
const artifactIsTheProgram = "exe"

// SpawnList returns the documented spawn list, sorted. It exists so the list
// in the report and the list in the code are the same object.
func SpawnList() []string {
	out := make([]string, 0, len(spawnable)+1)
	for name := range spawnable {
		out = append(out, name)
	}
	out = append(out, "<the hash-verified exe artifact itself>")
	sort.Strings(out)
	return out
}

// ---------------------------------------------------------------------------
// script.run
// ---------------------------------------------------------------------------

// planScript resolves an interpreter and builds the argv that runs a script
// FILE. Pure, so the Windows plan is asserted on a Linux runner.
//
// scriptPath is a path this agent wrote, inside a directory only SYSTEM and
// Administrators can open (scratch.go). It is never a job-supplied string.
//
// An interpreter that is not installed is a REFUSAL and not a failure: it is a
// durable property of this device rather than a transient condition, and
// retrying it forever would be the wrong answer.
func planScript(goos, interpreter, scriptPath string, look LookPath) (Command, error) {
	switch interpreter {
	case "powershell":
		return planPowerShell(goos, scriptPath, look)
	case "bash":
		return planBash(goos, scriptPath, look)
	case "python":
		return planPython(goos, scriptPath, look)
	}
	// Unreachable from a valid ScriptRunParams (the enum is closed), which is
	// exactly why it refuses rather than panicking: a control plane that grew a
	// fourth interpreter must get "this agent cannot", not a crash.
	return Command{}, jobs.Refuse("script.run: interpreter is not one this agent runs; " +
		"nothing was executed")
}

// powershellArgs is the invocation, and every element is a constant.
//
//   - -NoProfile: a profile is code from the endpoint's filesystem running
//     inside a job the control plane signed. It must not.
//   - -NonInteractive and -NoLogo: nothing to prompt, nothing to parse.
//   - -ExecutionPolicy Bypass: the body's authorization is the Ed25519
//     signature the runner already checked, not an execution policy set by
//     whoever last touched the registry on this box. Bypass here is narrower
//     than it sounds — it applies to THIS invocation only, and the alternative
//     is signing the .ps1 with a code-signing certificate the platform does
//     not have.
//   - -File: the body is a FILE. Not -Command, and above all not
//     -EncodedCommand, which is a top-tier malware indicator and the one thing
//     most likely to have this agent quarantined on every endpoint at once.
var powershellArgs = []string{
	"-NoProfile", "-NonInteractive", "-NoLogo", "-ExecutionPolicy", "Bypass", "-File",
}

func planPowerShell(goos, scriptPath string, look LookPath) (Command, error) {
	var candidates []string
	switch goos {
	case "windows":
		candidates = []string{"pwsh.exe", "powershell.exe"}
	case "darwin", "linux":
		// PowerShell on macOS exists only if somebody installed pwsh. There is
		// no fallback: `powershell.exe` is not a thing here.
		candidates = []string{"pwsh"}
	default:
		candidates = nil
	}
	path, name, err := resolve(look, candidates)
	if err != nil {
		return Command{}, jobs.Refuse("script.run: no PowerShell interpreter on this %s device; "+
			"nothing was executed", safeGOOS(goos))
	}
	return Command{
		Path:  path,
		Args:  append(append([]string{}, powershellArgs...), scriptPath),
		Label: name,
	}, nil
}

func planBash(goos, scriptPath string, look LookPath) (Command, error) {
	if goos == "windows" {
		// Refusal, not a hunt for WSL or Git Bash. A bash body on Windows means
		// the operator targeted the wrong OS family, and quietly finding SOME
		// bash-like thing on a Windows endpoint and running an operator's
		// script under it as SYSTEM is a worse outcome than saying no.
		return Command{}, jobs.Refuse("script.run: this agent does not run bash bodies on " +
			"Windows; nothing was executed")
	}
	path, name, err := resolve(look, []string{"bash"})
	if err != nil {
		return Command{}, jobs.Refuse("script.run: no bash on this %s device; nothing was executed",
			safeGOOS(goos))
	}
	// `bash <path>`, never `bash -c <body>`. -c would put the body on a command
	// line, where it is visible in every process listing on the box and in
	// every EDR telemetry event — which is where credentials in a script body
	// leak from.
	return Command{Path: path, Args: []string{scriptPath}, Label: name}, nil
}

func planPython(goos, scriptPath string, look LookPath) (Command, error) {
	path, name, err := resolve(look, []string{"python3", "python"})
	if err != nil {
		return Command{}, jobs.Refuse("script.run: no python interpreter on this %s device; "+
			"nothing was executed", safeGOOS(goos))
	}
	return Command{Path: path, Args: []string{scriptPath}, Label: name}, nil
}

// resolve tries each candidate in order and returns the first that exists.
func resolve(look LookPath, candidates []string) (path, name string, err error) {
	for _, c := range candidates {
		if _, ok := spawnable[c]; !ok {
			// A candidate that is not on the documented list is a programming
			// error, and it must not become a spawn.
			return "", "", fmt.Errorf("undocumented program %q", c)
		}
		if p, lerr := look(c); lerr == nil && p != "" {
			return p, c, nil
		}
	}
	return "", "", fmt.Errorf("none of %v is installed", candidates)
}

// safeGOOS keeps an unexpected GOOS out of a refusal reason verbatim. The
// value is this process's own runtime.GOOS in production, but the planner is
// parameterised on it and a refusal reason lands in the server's hash-chained
// audit log.
func safeGOOS(goos string) string {
	switch goos {
	case "windows", "darwin", "linux":
		return goos
	}
	return "unrecognised-platform"
}

// ---------------------------------------------------------------------------
// app.deploy
// ---------------------------------------------------------------------------

// planInstall builds the argv for one deployment.
//
// artifactPath is empty for a manager-sourced package and is otherwise a path
// that EXISTS ONLY BECAUSE THE HASH MATCHED (install.go's `stage`). packageID
// is the manager package identifier, already validated against args.go's
// allowlist. extra are the validated `install_args` tokens.
//
// Pure, so every Windows and macOS argv below is asserted on a Linux runner.
func planInstall(goos string, p deployParams, artifactPath, packageID string,
	extra []string, look LookPath) (Command, error) {

	if err := platformAllows(goos, p.InstallerType); err != nil {
		return Command{}, err
	}

	switch p.InstallerType {
	case "msi":
		return planMSI(p.Action, artifactPath, extra, look)
	case "exe":
		return planEXE(p.Action, artifactPath, extra)
	case "winget":
		return planWinget(p.Action, packageID, extra, look)
	case "choco":
		return planChoco(p.Action, packageID, extra, look)
	case "pkg":
		return planPkg(p.Action, artifactPath, extra, look)
	}
	return Command{}, jobs.Refuse("app.deploy: this agent has no install engine for that "+
		"installer_type on %s; nothing was executed", safeGOOS(goos))
}

// platformAllows is the OS gate, stated once.
//
// THE FLEET IS WINDOWS 11 AND MACOS. `deb` and `rpm` are in the wire enum
// because the server's Package model spans every family the product might
// eventually manage; this build implements neither, and says so as a REFUSAL
// rather than pretending an unimplemented engine is a transient failure.
func platformAllows(goos, installerType string) error {
	allowed := map[string][]string{
		"windows": {"msi", "exe", "winget", "choco"},
		"darwin":  {"pkg"},
	}[goos]
	for _, a := range allowed {
		if a == installerType {
			return nil
		}
	}
	return jobs.Refuse("app.deploy: installer_type %q has no install engine on %s in this agent "+
		"build; nothing was executed", safeInstallerType(installerType), safeGOOS(goos))
}

// safeInstallerType keeps a value the server invented out of the audit log
// verbatim. Every member of the wire enum passes through unchanged.
func safeInstallerType(t string) string {
	if _, known := installerTypes[t]; known {
		return t
	}
	return "unrecognised"
}

// msiQuiet is the silent-install argv, all constants. `/qn` because there is
// no desktop session to show a dialog in when running as SYSTEM, and
// `/norestart` because a deploy job must never reboot an endpoint out from
// under an operator — reboot orchestration is not this module's decision.
var msiQuiet = []string{"/qn", "/norestart"}

func planMSI(action, artifactPath string, extra []string, look LookPath) (Command, error) {
	var verb string
	switch action {
	case "install", "update":
		// `/i` for both. An MSI update is a reinstall of a higher-versioned
		// package; the difference is the package, not the verb.
		verb = "/i"
	case "uninstall":
		verb = "/x"
	default:
		return Command{}, jobs.Refuse("app.deploy: unrecognised action; nothing was executed")
	}
	path, name, err := resolve(look, []string{"msiexec.exe"})
	if err != nil {
		return Command{}, jobs.Refuse("app.deploy: msiexec.exe is not on this device; nothing " +
			"was executed")
	}
	args := []string{verb, artifactPath}
	args = append(args, msiQuiet...)
	args = append(args, extra...)
	return Command{Path: path, Args: args, Label: name}, nil
}

func planEXE(action, artifactPath string, extra []string) (Command, error) {
	if action == "uninstall" {
		// AppDeployParams carries `uninstall_rule` as a free-form dict with no
		// documented grammar for "the command that uninstalls this". Guessing
		// one — probing the Windows uninstall registry keys and executing
		// whatever string is there — would be this agent choosing a program to
		// run as SYSTEM from data the signature does not cover. It refuses
		// instead. See the wire-contract findings in
		// docs/modules/app-deploy/agent-capability.md.
		return Command{}, jobs.Refuse("app.deploy: uninstalling an `exe` package needs an " +
			"uninstall command, and AppDeployParams carries no field for one. This agent will " +
			"not choose a program to run as SYSTEM from data the job signature does not cover; " +
			"nothing was executed")
	}
	// THE ARTIFACT IS THE PROGRAM. This is the one spawn whose program name is
	// not a compile-time constant, and it is safe for exactly one reason: this
	// path exists only because the SHA-256 in the SIGNED params matched the
	// bytes that were fetched. install.go's `stage` never produces it
	// otherwise.
	return Command{Path: artifactPath, Args: append([]string{}, extra...), Label: "exe-artifact"}, nil
}

// wingetCommon is the non-negotiable part of every winget invocation.
// `--disable-interactivity` and the two agreement flags exist because there is
// no console to answer a prompt on; `--silent` because there is no desktop.
var wingetCommon = []string{
	"--silent", "--disable-interactivity",
	"--accept-package-agreements", "--accept-source-agreements",
}

func planWinget(action, packageID string, extra []string, look LookPath) (Command, error) {
	verb, err := managerVerb(action, "upgrade")
	if err != nil {
		return Command{}, err
	}
	path, name, lerr := resolve(look, []string{"winget.exe"})
	if lerr != nil {
		// A device without winget cannot be given winget by retrying. Refusal.
		return Command{}, jobs.Refuse("app.deploy: winget is not installed on this device; " +
			"nothing was executed")
	}
	// `--id <pkg> --exact` so a package id is matched as an id and never as a
	// fuzzy search that could resolve to a different package than the operator
	// cataloged.
	args := []string{verb, "--id", packageID, "--exact"}
	if verb == "uninstall" {
		// `winget uninstall` does not accept the package-agreement flags.
		args = append(args, "--silent", "--disable-interactivity")
	} else {
		args = append(args, wingetCommon...)
	}
	args = append(args, extra...)
	return Command{Path: path, Args: args, Label: name}, nil
}

func planChoco(action, packageID string, extra []string, look LookPath) (Command, error) {
	verb, err := managerVerb(action, "upgrade")
	if err != nil {
		return Command{}, err
	}
	path, name, lerr := resolve(look, []string{"choco.exe"})
	if lerr != nil {
		return Command{}, jobs.Refuse("app.deploy: chocolatey is not installed on this device; " +
			"nothing was executed")
	}
	args := []string{verb, packageID, "-y", "--no-progress", "--limit-output"}
	args = append(args, extra...)
	return Command{Path: path, Args: args, Label: name}, nil
}

func managerVerb(action, updateVerb string) (string, error) {
	switch action {
	case "install":
		return "install", nil
	case "update":
		return updateVerb, nil
	case "uninstall":
		return "uninstall", nil
	}
	return "", jobs.Refuse("app.deploy: unrecognised action; nothing was executed")
}

func planPkg(action, artifactPath string, extra []string, look LookPath) (Command, error) {
	if action == "uninstall" {
		// macOS .pkg has no uninstaller. Apple's installer(8) does not remove
		// anything, and the only general "uninstall a pkg" is to read its BOM
		// and delete the files it lists — which is a destructive filesystem
		// operation driven by a manifest, not an install engine.
		return Command{}, jobs.Refuse("app.deploy: a macOS .pkg carries no uninstaller and this " +
			"agent will not delete a package's files from its receipt; nothing was executed")
	}
	path, name, err := resolve(look, []string{"installer"})
	if err != nil {
		return Command{}, jobs.Refuse("app.deploy: /usr/sbin/installer is not on this device; " +
			"nothing was executed")
	}
	args := []string{"-pkg", artifactPath, "-target", "/"}
	args = append(args, extra...)
	return Command{Path: path, Args: args, Label: name}, nil
}
