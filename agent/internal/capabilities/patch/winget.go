package patch

import (
	"context"
	"fmt"
	"regexp"
	"strings"
)

// winget app updates. No build tag: parsing and outcome mapping run in CI on
// Linux; the engine is selected only on Windows (engine_windows.go).
//
// winget.exe is found by deploy.WingetPath, which looks in the App Installer
// package directory because LocalSystem has no WindowsApps alias on PATH.
// Running as SYSTEM, winget sees machine-wide installs only; a per-user install
// (an app under %LOCALAPPDATA%) is invisible here and is not claimed.
//
// NEVER EXECUTED: winget has not run under this agent. The fixture in
// testdata/winget-upgrade-RECONSTRUCTED.txt is written from the documented
// table format, not captured.

// wingetCommonFlags keep winget from waiting on a console nobody is at.
var wingetCommonFlags = []string{"--disable-interactivity", "--accept-source-agreements"}

// wingetRestartCodes are the documented "installed, restart to finish" results.
var wingetRestartCodes = map[uint32]bool{
	0x8A150109: true, // APPINSTALLER_CLI_ERROR_INSTALL_REBOOT_REQUIRED_TO_FINISH
	0x8A15010A: true, // APPINSTALLER_CLI_ERROR_INSTALL_REBOOT_REQUIRED_TO_INSTALL
}

// wingetID is what a winget or msstore package id looks like. A truncated id
// ("Mozilla.Fire…") fails it and is skipped rather than guessed at.
var wingetID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$`)

type wingetEngine struct {
	path    string
	run     runner
	restart bool
}

func (w *wingetEngine) Source() string      { return sourceWinget }
func (w *wingetEngine) RestartNeeded() bool { return w.restart }

func (w *wingetEngine) Outdated(ctx context.Context) ([]entry, error) {
	args := append([]string{"upgrade"}, wingetCommonFlags...)
	res := w.run(ctx, nil, w.path, args...)
	if !res.ok() && strings.TrimSpace(res.Stdout) == "" {
		return nil, fmt.Errorf("winget upgrade could not list updates (exit 0x%08X): %s",
			uint32(res.ExitCode), firstLine(res.Stderr))
	}
	return parseWingetUpgrade(res.Stdout), nil
}

// Upgrade runs `winget upgrade --id <id> --exact` for an id winget itself just
// listed in this job.
func (w *wingetEngine) Upgrade(ctx context.Context, e entry) error {
	args := []string{"upgrade", "--id", e.Identifier, "--exact", "--silent",
		"--accept-package-agreements"}
	args = append(args, wingetCommonFlags...)
	res := w.run(ctx, nil, w.path, args...)
	if res.ok() {
		return nil
	}
	code := uint32(res.ExitCode)
	if wingetRestartCodes[code] {
		w.restart = true
		return nil
	}
	return fmt.Errorf("winget upgrade failed (exit 0x%08X): %s", code,
		firstLine(res.Stdout+"\n"+res.Stderr))
}

// parseWingetUpgrade reads the table `winget upgrade` prints:
//
//	Name              Id                Version   Available  Source
//	--------------------------------------------------------------
//	Mozilla Firefox   Mozilla.Firefox   130.0     131.0      winget
//	2 upgrades available.
//
// Rows are read from the right, because the Name column may hold spaces and
// wide characters that shift fixed positions. The table ends at the first line
// that is not a row, so the "require explicit targeting" table that can follow
// is never read.
func parseWingetUpgrade(stdout string) []entry {
	lines := strings.Split(stdout, "\n")
	header := -1
	for i, raw := range lines {
		line := wingetLine(raw)
		if len(line) >= 10 && strings.Trim(line, "-") == "" && i > 0 {
			header = i - 1
			break
		}
	}
	if header < 0 {
		return nil
	}
	withSource := len(strings.Fields(wingetLine(lines[header]))) >= 5

	var out []entry
	for _, raw := range lines[header+2:] {
		row, isRow, usable := parseWingetRow(wingetLine(raw), withSource)
		if !isRow {
			break
		}
		if usable {
			out = append(out, row)
		}
	}
	return out
}

// wingetLine drops the CRLF ending and then progress output: winget redraws
// its spinner with carriage returns, so only the text after the last one is
// the line.
func wingetLine(raw string) string {
	raw = strings.TrimRight(raw, "\r\n")
	if i := strings.LastIndex(raw, "\r"); i >= 0 {
		raw = raw[i+1:]
	}
	return strings.TrimRight(raw, " \t")
}

// parseWingetRow reads one row. isRow is false at the end of the table;
// usable is false for a row whose id winget truncated.
func parseWingetRow(line string, withSource bool) (e entry, isRow, usable bool) {
	fields := strings.Fields(line)
	need := 4
	if withSource {
		need = 5
	}
	if len(fields) < need {
		return entry{}, false, false
	}
	if withSource {
		fields = fields[:len(fields)-1]
	}
	available := fields[len(fields)-1]
	version := fields[len(fields)-2]
	fields = fields[:len(fields)-2]
	// "< 1.2.3" is winget's spelling of "older than"; the marker is its own field.
	if n := len(fields); n > 0 && (fields[n-1] == "<" || fields[n-1] == ">") {
		version = fields[n-1] + " " + version
		fields = fields[:n-1]
	}
	if len(fields) < 2 {
		return entry{}, false, false
	}
	id := fields[len(fields)-1]
	if !wingetID.MatchString(id) {
		return entry{}, true, false
	}
	name := strings.Join(fields[:len(fields)-1], " ")
	return entry{
		Source:     sourceWinget,
		Identifier: id,
		Title:      fmt.Sprintf("%s %s → %s", name, version, available),
		// winget publishes no severity.
		Severity: "unknown",
	}, true, true
}

func firstLine(s string) string {
	for _, line := range strings.Split(s, "\n") {
		if line = strings.TrimSpace(wingetLine(line)); line != "" {
			return line
		}
	}
	return "no output"
}
