package deploy

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// THE ANTIVIRUS/EDR CONSTRAINTS, ASSERTED.
//
// `script.run` is definitionally the behaviour EDR exists to catch, so the
// three rules in the package comment are checked here rather than remembered:
// the spawn list is closed and documented, no PowerShell invocation ever
// carries an encoded command, and no shell is ever the program.
//
// Every planner is a pure function of (goos, params, paths), so the WINDOWS and
// MACOS argv are asserted on this Linux container. What is NOT asserted is
// what those argv do when a real Windows kernel runs them — see the report's
// "what never executed".

// ---------------------------------------------------------------------------
// The spawn list
// ---------------------------------------------------------------------------

func TestTheDocumentedSpawnListIsTheOnlyOneThePlannerCanProduce(t *testing.T) {
	// The list an EDR team is given must be complete BY CONSTRUCTION, not by
	// somebody having read the source carefully. This drives every
	// (goos, interpreter) and every (goos, installer_type, action) through the
	// planners with EVERY documented program "installed", and asserts that the
	// program actually chosen is always one of them.
	//
	// FALSIFYING OBSERVATION: a Command whose Path names a program outside
	// `spawnable`, or a documented program the planner can never choose (a
	// stale entry is a line in an allowlist nobody is defending).
	all := make([]string, 0, len(spawnable))
	for name := range spawnable {
		all = append(all, name)
	}

	chosen := map[string]bool{}
	record := func(c Command) {
		// Path is "/fake/<name>" for anything resolved through LookPath.
		chosen[strings.TrimPrefix(c.Path, "/fake/")] = true
	}

	// The sweep is run once per "what is installed" profile, because several
	// entries are FALLBACKS — `powershell.exe` and `python` are only chosen
	// when `pwsh.exe` and `python3` are absent. A single all-installed pass
	// would report them as stale entries, which is a false alarm that would
	// eventually get the assertion deleted.
	profiles := [][]string{all}
	for _, preferred := range []string{"pwsh.exe", "pwsh", "python3"} {
		var without []string
		for _, n := range all {
			if n != preferred {
				without = append(without, n)
			}
		}
		profiles = append(profiles, without)
	}

	for _, profile := range profiles {
		sweepPlanners(t, fakePaths(profile...), record)
	}

	for name := range spawnable {
		if !chosen[name] {
			t.Errorf("%q is on the documented spawn list and no (goos, job) combination can "+
				"produce it. A stale entry in an allowlist is a hole nobody is defending", name)
		}
	}
	if len(SpawnList()) != len(spawnable)+1 {
		t.Errorf("SpawnList() reports %d entries for %d spawnable programs plus the exe artifact",
			len(SpawnList()), len(spawnable))
	}
}

// sweepPlanners drives every (goos, interpreter) and (goos, installer_type,
// action) through the planners and reports what each one chose.
func sweepPlanners(t *testing.T, look LookPath, record func(Command)) {
	t.Helper()
	for _, goos := range []string{"windows", "darwin", "linux"} {
		for interp := range interpreters {
			cmd, err := planScript(goos, interp, "/scratch/script.x", look)
			if err != nil {
				continue // a refusal spawns nothing, which is the point
			}
			record(cmd)
			if _, ok := spawnable[strings.TrimPrefix(cmd.Path, "/fake/")]; !ok {
				t.Errorf("planScript(%s, %s) chose %q, which is not on the documented spawn list",
					goos, interp, cmd.Path)
			}
		}
		for it := range installerTypes {
			for act := range actions {
				p := deployParams{Action: act, InstallerType: it}
				cmd, err := planInstall(goos, p, "/scratch/package.bin", "Some.Package", nil, look)
				if err != nil {
					continue
				}
				name := strings.TrimPrefix(cmd.Path, "/fake/")
				if cmd.Path == "/scratch/package.bin" {
					// The one documented exception: an `exe` package IS the
					// downloaded, hash-verified artifact.
					if it != artifactIsTheProgram {
						t.Errorf("planInstall(%s, %s, %s) spawned the artifact itself; only %q "+
							"is documented as doing that", goos, it, act, artifactIsTheProgram)
					}
					continue
				}
				record(cmd)
				if _, ok := spawnable[name]; !ok {
					t.Errorf("planInstall(%s, %s, %s) chose %q, which is not on the documented "+
						"spawn list", goos, it, act, cmd.Path)
				}
			}
		}
	}
}

// ---------------------------------------------------------------------------
// PowerShell, the loudest thing this agent does
// ---------------------------------------------------------------------------

func TestPowerShellIsInvokedWithAFileAndNeverAnEncodedOrInlineCommand(t *testing.T) {
	// `-EncodedCommand` is a top-tier malware indicator and `-Command` puts the
	// body on a command line that every process listing and every EDR
	// telemetry event on the box can read.
	//
	// FALSIFYING OBSERVATION: any of the forbidden switches in the argv, or a
	// final argument that is not the script path.
	for _, tc := range []struct{ goos, want string }{
		{"windows", "pwsh.exe"},
		{"darwin", "pwsh"},
	} {
		cmd, err := planPowerShell(tc.goos, `C:\scratch\script.ps1`,
			fakePaths("pwsh.exe", "powershell.exe", "pwsh"))
		if err != nil {
			t.Fatalf("planPowerShell(%s): %v", tc.goos, err)
		}
		if !strings.HasSuffix(cmd.Path, tc.want) {
			t.Errorf("planPowerShell(%s) chose %q, want %s", tc.goos, cmd.Path, tc.want)
		}
		joined := strings.Join(cmd.Args, " ")
		for _, forbidden := range []string{
			"-EncodedCommand", "-encodedcommand", "-e ", "-Command", "-c ", "-enc",
		} {
			if strings.Contains(joined, forbidden) {
				t.Errorf("the PowerShell argv %q carries %q", joined, forbidden)
			}
		}
		for _, required := range []string{"-NoProfile", "-NonInteractive", "-File"} {
			if !strings.Contains(joined, required) {
				t.Errorf("the PowerShell argv %q is missing %q", joined, required)
			}
		}
		if cmd.Args[len(cmd.Args)-1] != `C:\scratch\script.ps1` {
			t.Errorf("the last argument is %q, not the script path", cmd.Args[len(cmd.Args)-1])
		}
		if cmd.Args[len(cmd.Args)-2] != "-File" {
			t.Errorf("the script path is not the argument to -File; argv is %v", cmd.Args)
		}
	}
}

func TestPwshIsPreferredOverWindowsPowerShellAndPowerShellIsTheFallback(t *testing.T) {
	// Not a style preference. `pwsh.exe` is a distinct binary, so an EDR rule
	// can tell a managed script run from the Windows PowerShell that every
	// piece of malware on the platform also uses.
	cmd, err := planPowerShell("windows", "s.ps1", fakePaths("pwsh.exe", "powershell.exe"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(cmd.Path, "pwsh.exe") {
		t.Fatalf("with both present the planner chose %q", cmd.Path)
	}
	cmd, err = planPowerShell("windows", "s.ps1", fakePaths("powershell.exe"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(cmd.Path, "powershell.exe") {
		t.Fatalf("with only Windows PowerShell present the planner chose %q", cmd.Path)
	}
	if _, err := planPowerShell("windows", "s.ps1", nothingInstalled()); err == nil {
		t.Fatal("with neither present the planner returned no error")
	}
}

func TestBashIsGivenTheFileAndNeverDashC(t *testing.T) {
	cmd, err := planBash("linux", "/scratch/script.sh", fakePaths("bash"))
	if err != nil {
		t.Fatal(err)
	}
	if len(cmd.Args) != 1 || cmd.Args[0] != "/scratch/script.sh" {
		t.Fatalf("bash argv is %v; it must be exactly the script path. `bash -c <body>` puts the "+
			"body in every process listing on the box", cmd.Args)
	}
}

// ---------------------------------------------------------------------------
// The installer argv
// ---------------------------------------------------------------------------

func TestTheInstallerArgvIsWhatEachEngineActuallyNeeds(t *testing.T) {
	// The Windows and macOS argv, asserted on Linux. These are the strings a
	// reviewer with a Windows box can check by hand, and they are pinned so a
	// change to one is a visible change rather than a silent one.
	look := fakePaths("msiexec.exe", "winget.exe", "choco.exe", "installer")
	for _, tc := range []struct {
		name    string
		goos    string
		params  deployParams
		pkgID   string
		extra   []string
		wantBin string
		wantArg []string
	}{
		{
			name: "msi install", goos: "windows",
			params: deployParams{Action: "install", InstallerType: "msi"},
			// /qn because there is no desktop session to show a dialog in;
			// /norestart because a deploy must never reboot an endpoint out
			// from under an operator.
			wantBin: "msiexec.exe",
			wantArg: []string{"/i", "/scratch/package.msi", "/qn", "/norestart"},
		},
		{
			name: "msi uninstall", goos: "windows",
			params:  deployParams{Action: "uninstall", InstallerType: "msi"},
			wantBin: "msiexec.exe",
			wantArg: []string{"/x", "/scratch/package.msi", "/qn", "/norestart"},
		},
		{
			name: "msi update is a reinstall", goos: "windows",
			params:  deployParams{Action: "update", InstallerType: "msi"},
			wantBin: "msiexec.exe",
			wantArg: []string{"/i", "/scratch/package.msi", "/qn", "/norestart"},
		},
		{
			name: "msi with extra args", goos: "windows",
			params:  deployParams{Action: "install", InstallerType: "msi"},
			extra:   []string{"REBOOT=ReallySuppress"},
			wantBin: "msiexec.exe",
			wantArg: []string{"/i", "/scratch/package.msi", "/qn", "/norestart", "REBOOT=ReallySuppress"},
		},
		{
			name: "winget install", goos: "windows",
			params: deployParams{Action: "install", InstallerType: "winget"},
			pkgID:  "Mozilla.Firefox", wantBin: "winget.exe",
			// --exact so a package id is matched as an id and never as a fuzzy
			// search that could resolve to a different package.
			wantArg: []string{"install", "--id", "Mozilla.Firefox", "--exact", "--silent",
				"--disable-interactivity", "--accept-package-agreements", "--accept-source-agreements"},
		},
		{
			name: "winget update is upgrade", goos: "windows",
			params: deployParams{Action: "update", InstallerType: "winget"},
			pkgID:  "Mozilla.Firefox", wantBin: "winget.exe",
			wantArg: []string{"upgrade", "--id", "Mozilla.Firefox", "--exact", "--silent",
				"--disable-interactivity", "--accept-package-agreements", "--accept-source-agreements"},
		},
		{
			name: "winget uninstall drops the agreement flags", goos: "windows",
			params: deployParams{Action: "uninstall", InstallerType: "winget"},
			pkgID:  "Mozilla.Firefox", wantBin: "winget.exe",
			wantArg: []string{"uninstall", "--id", "Mozilla.Firefox", "--exact", "--silent",
				"--disable-interactivity"},
		},
		{
			name: "choco install", goos: "windows",
			params: deployParams{Action: "install", InstallerType: "choco"},
			pkgID:  "googlechrome", wantBin: "choco.exe",
			wantArg: []string{"install", "googlechrome", "-y", "--no-progress", "--limit-output"},
		},
		{
			name: "pkg install", goos: "darwin",
			params:  deployParams{Action: "install", InstallerType: "pkg"},
			wantBin: "installer",
			wantArg: []string{"-pkg", "/scratch/package.pkg", "-target", "/"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := "/scratch/package." + tc.params.InstallerType
			cmd, err := planInstall(tc.goos, tc.params, path, tc.pkgID, tc.extra, look)
			if err != nil {
				t.Fatalf("planInstall: %v", err)
			}
			if !strings.HasSuffix(cmd.Path, tc.wantBin) {
				t.Errorf("program = %q, want %s", cmd.Path, tc.wantBin)
			}
			if strings.Join(cmd.Args, "\x00") != strings.Join(tc.wantArg, "\x00") {
				t.Errorf("argv =\n  %v\nwant\n  %v", cmd.Args, tc.wantArg)
			}
		})
	}
}

func TestAnExeArtifactIsItsOwnProgramAndCarriesOnlyValidatedArguments(t *testing.T) {
	// The one spawn whose program is not a compile-time constant. It is safe
	// only because the path exists solely as a result of a matching SHA-256.
	cmd, err := planEXE("install", "/scratch/package.exe", []string{"/S"})
	if err != nil {
		t.Fatal(err)
	}
	if cmd.Path != "/scratch/package.exe" {
		t.Fatalf("program = %q", cmd.Path)
	}
	if len(cmd.Args) != 1 || cmd.Args[0] != "/S" {
		t.Fatalf("argv = %v", cmd.Args)
	}
	if _, err := planEXE("uninstall", "/scratch/package.exe", nil); err == nil {
		t.Fatal("uninstalling an exe was planned; AppDeployParams carries no uninstall command " +
			"and this agent must not choose a program to run as SYSTEM from unsigned data")
	}
}

func TestEveryInstallerTypeWithoutAnEngineOnThisPlatformIsRefused(t *testing.T) {
	// The fleet is Windows 11 and macOS. `deb`, `rpm`, `dmg` and `script` are
	// in the wire enum and this build implements none of them; an unimplemented
	// engine is a REFUSAL, because retrying does not implement it.
	//
	// FALSIFYING OBSERVATION: a plan produced for a type this build has no
	// engine for, or a plain error where a refusal is required.
	look := fakePaths("msiexec.exe", "winget.exe", "choco.exe", "installer")
	cases := []struct{ goos, it string }{
		{"windows", "pkg"}, {"windows", "dmg"}, {"windows", "deb"}, {"windows", "rpm"},
		{"windows", "script"},
		{"darwin", "msi"}, {"darwin", "exe"}, {"darwin", "winget"}, {"darwin", "choco"},
		{"darwin", "dmg"}, {"darwin", "deb"}, {"darwin", "rpm"}, {"darwin", "script"},
		{"linux", "msi"}, {"linux", "deb"}, {"linux", "rpm"},
	}
	for _, c := range cases {
		p := deployParams{Action: "install", InstallerType: c.it}
		cmd, err := planInstall(c.goos, p, "/scratch/package.bin", "Some.Package", nil, look)
		if err == nil {
			t.Errorf("planInstall(%s, %s) produced a plan: %+v", c.goos, c.it, cmd)
			continue
		}
		if !isRefusal(err) {
			t.Errorf("planInstall(%s, %s) failed rather than refusing: %v", c.goos, c.it, err)
		}
	}
}

func TestARefusalReasonNeverCarriesAValueTheServerInvented(t *testing.T) {
	// A refusal reason lands in the server's hash-chained audit log. An audit
	// record an attacker can write into is not an audit record — the same rule
	// internal/jobs' safeReason applies.
	//
	// FALSIFYING OBSERVATION: an invented installer_type or GOOS appearing
	// verbatim in the reason.
	const invented = "SMUGGLED-INTO-THE-AUDIT-LOG"
	p := deployParams{Action: "install", InstallerType: invented}
	_, err := planInstall(invented, p, "", "", nil, nothingInstalled())
	if err == nil {
		t.Fatal("an invented installer_type produced a plan")
	}
	assertAbsent(t, err.Error(), invented, "the refusal reason")

	_, err = planScript(invented, "powershell", "s.ps1", nothingInstalled())
	if err == nil {
		t.Fatal("an invented GOOS produced a plan")
	}
	assertAbsent(t, err.Error(), invented, "the refusal reason")
}

// ---------------------------------------------------------------------------
// No shell, ever — asserted against the source
// ---------------------------------------------------------------------------

func TestNoSourceFileInThisPackageEverSpawnsAShell(t *testing.T) {
	// The behavioural tests above check what the planners CHOOSE. This checks
	// what the source can even express, which is the one that catches a
	// regression in a path no test happens to exercise. Same shape as
	// internal/transport/listen_test.go's walker.
	//
	// FALSIFYING OBSERVATION: any of these strings as a literal in non-test
	// source. `sh -c`, `cmd /c` and `-EncodedCommand` are not things this
	// package needs, and each of them is a way to put a job-supplied string on
	// a command line.
	forbidden := map[string]string{
		`"sh"`:              "spawning a shell means word splitting and metacharacters",
		`"/bin/sh"`:         "spawning a shell means word splitting and metacharacters",
		`"cmd"`:             "cmd.exe re-parses its command line; nothing here needs it",
		`"cmd.exe"`:         "cmd.exe re-parses its command line; nothing here needs it",
		`"-c"`:              "`bash -c` / `pwsh -c` puts the script body on a command line",
		`"-Command"`:        "-Command puts the script body on a command line",
		`"-EncodedCommand"`: "a base64 payload on a PowerShell command line is a top-tier malware indicator",
		`"/c"`:              "`cmd /c` re-parses its command line",
		"exec.Command(":     "use exec.CommandContext so the job's deadline bounds the process",
	}

	fset := token.NewFileSet()
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatal(err)
	}
	found := 0
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		found++
		raw, err := os.ReadFile(name)
		if err != nil {
			t.Fatal(err)
		}
		// Parsed rather than grepped so a mention inside a COMMENT — of which
		// this package has many, deliberately — is not a violation. Only
		// literals in code count.
		file, err := parser.ParseFile(fset, name, raw, parser.SkipObjectResolution)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		ast.Inspect(file, func(n ast.Node) bool {
			switch node := n.(type) {
			case *ast.BasicLit:
				if node.Kind == token.STRING {
					if why, bad := forbidden[node.Value]; bad {
						t.Errorf("%s:%d has the literal %s (%s)",
							name, fset.Position(node.Pos()).Line, node.Value, why)
					}
				}
			case *ast.CallExpr:
				if sel, ok := node.Fun.(*ast.SelectorExpr); ok {
					if id, ok := sel.X.(*ast.Ident); ok && id.Name == "exec" && sel.Sel.Name == "Command" {
						t.Errorf("%s:%d calls exec.Command (%s)", name,
							fset.Position(node.Pos()).Line, forbidden["exec.Command("])
					}
				}
			}
			return true
		})
	}
	if found == 0 {
		t.Fatal("the walker found no source files; it would pass forever")
	}
}

func TestTheWalkerActuallyReportsAShellWhenThereIsOne(t *testing.T) {
	// A walker nobody has ever seen report a violation is a walker nobody
	// knows works. This is the same argument
	// internal/transport/listen_test.go makes for its own.
	dir := t.TempDir()
	src := "package deploy\n\nimport \"os/exec\"\n\n" +
		"func sneaked() { _ = exec.Command(\"sh\", \"-c\", \"whoami\") }\n"
	if err := os.WriteFile(filepath.Join(dir, "sneaked.go"), []byte(src), 0o600); err != nil {
		t.Fatal(err)
	}
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, filepath.Join(dir, "sneaked.go"), nil,
		parser.SkipObjectResolution)
	if err != nil {
		t.Fatal(err)
	}
	hits := 0
	ast.Inspect(file, func(n ast.Node) bool {
		if lit, ok := n.(*ast.BasicLit); ok && lit.Kind == token.STRING {
			if lit.Value == `"sh"` || lit.Value == `"-c"` {
				hits++
			}
		}
		return true
	})
	if hits != 2 {
		t.Fatalf("the walker's literal check found %d of the two planted shell literals", hits)
	}
}
