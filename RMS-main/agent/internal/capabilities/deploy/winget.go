package deploy

import (
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// Finding winget.exe when the agent runs as LocalSystem.
//
// winget reaches PATH through a per-user App Execution Alias
// (%LOCALAPPDATA%\Microsoft\WindowsApps\winget.exe). LocalSystem has no such
// alias, so exec.LookPath("winget.exe") fails on a machine that has winget.
// The binary itself lives in the App Installer package directory,
// %ProgramFiles%\WindowsApps\Microsoft.DesktopAppInstaller_<ver>_<arch>__8wekyb3d8bbwe,
// which only TrustedInstaller can write. We pick the highest installed version
// for this architecture and fall back to PATH.

const (
	wingetPackagePrefix = "Microsoft.DesktopAppInstaller_"
	// The publisher id of Microsoft's Store-signed packages. A directory with
	// the right prefix and any other suffix is not App Installer.
	wingetPackageSuffix = "__8wekyb3d8bbwe"
)

// wingetArchitectures lists the package architectures that can run on goarch,
// native first. arm64 Windows runs x64 binaries under emulation.
func wingetArchitectures(goarch string) []string {
	switch goarch {
	case "amd64":
		return []string{"x64"}
	case "arm64":
		return []string{"arm64", "x64"}
	case "386":
		return []string{"x86"}
	}
	return nil
}

// wingetCandidates returns the App Installer package directory names from a
// WindowsApps listing, best first: native architecture before emulated, then
// highest version. Pure.
func wingetCandidates(names []string, goarch string) []string {
	archs := wingetArchitectures(goarch)
	type candidate struct {
		name    string
		rank    int
		version []int
	}
	var found []candidate
	for _, name := range names {
		middle, ok := strings.CutPrefix(name, wingetPackagePrefix)
		if !ok {
			continue
		}
		middle, ok = strings.CutSuffix(middle, wingetPackageSuffix)
		if !ok {
			continue
		}
		version, arch, ok := strings.Cut(middle, "_")
		if !ok {
			continue
		}
		rank := -1
		for i, a := range archs {
			if a == arch {
				rank = i
			}
		}
		parts, ok := parseDottedVersion(version)
		if rank < 0 || !ok {
			continue
		}
		found = append(found, candidate{name: name, rank: rank, version: parts})
	}
	sort.SliceStable(found, func(i, j int) bool {
		if found[i].rank != found[j].rank {
			return found[i].rank < found[j].rank
		}
		return compareVersions(found[i].version, found[j].version) > 0
	})
	out := make([]string, 0, len(found))
	for _, c := range found {
		out = append(out, c.name)
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// parseDottedVersion parses "1.23.1911.0". Every part must be a number.
func parseDottedVersion(s string) ([]int, bool) {
	fields := strings.Split(s, ".")
	out := make([]int, 0, len(fields))
	for _, f := range fields {
		n, err := strconv.Atoi(f)
		if err != nil || n < 0 {
			return nil, false
		}
		out = append(out, n)
	}
	return out, len(out) > 0
}

func compareVersions(a, b []int) int {
	for i := 0; i < len(a) || i < len(b); i++ {
		var x, y int
		if i < len(a) {
			x = a[i]
		}
		if i < len(b) {
			y = b[i]
		}
		if x != y {
			if x > y {
				return 1
			}
			return -1
		}
	}
	return 0
}

// locateWingetIn returns the winget.exe of the best App Installer package
// under programFiles\WindowsApps whose executable actually exists.
func locateWingetIn(programFiles, goarch string, readDir func(string) ([]string, error),
	exists func(string) bool) (string, bool) {
	if programFiles == "" {
		return "", false
	}
	apps := filepath.Join(programFiles, "WindowsApps")
	names, err := readDir(apps)
	if err != nil {
		return "", false
	}
	for _, dir := range wingetCandidates(names, goarch) {
		exe := filepath.Join(apps, dir, "winget.exe")
		if exists(exe) {
			return exe, true
		}
	}
	return "", false
}

// wingetAwareLookPath wraps a LookPath so that "winget.exe" is looked for in
// the App Installer package first and on PATH second. Every other name goes
// straight to base.
func wingetAwareLookPath(base LookPath, locate func() (string, bool)) LookPath {
	return func(name string) (string, error) {
		if name == "winget.exe" {
			if p, ok := locate(); ok {
				return p, nil
			}
		}
		return base(name)
	}
}

// WingetPath is the SYSTEM-safe winget.exe for this machine, if App Installer
// is present. The patch capability lists and applies app updates with it.
func WingetPath() (string, bool) {
	path, err := wingetAwareLookPath(exec.LookPath, locateWinget)("winget.exe")
	return path, err == nil
}
