package deploy

import (
	"errors"
	"path/filepath"
	"testing"
)

// Directory names as they appear in `C:\Program Files\WindowsApps` on a
// Windows 11 machine that has updated App Installer at least once: the old
// version is left behind until servicing removes it, and the neutral/resource
// packages sit beside the arch-specific one that actually holds winget.exe.
var windowsAppsListing = []string{
	"Microsoft.DesktopAppInstaller_1.21.3482.0_x64__8wekyb3d8bbwe",
	"Microsoft.DesktopAppInstaller_1.9.25200.0_x64__8wekyb3d8bbwe",
	"Microsoft.DesktopAppInstaller_1.23.1911.0_x64__8wekyb3d8bbwe",
	"Microsoft.DesktopAppInstaller_1.23.1911.0_neutral_~_8wekyb3d8bbwe",
	"Microsoft.DesktopAppInstaller_1.23.1911.0_neutral_split.scale-100_8wekyb3d8bbwe",
	"Microsoft.WindowsTerminal_1.18.10301.0_x64__8wekyb3d8bbwe",
	"Microsoft.VCLibs.140.00.UWPDesktop_14.0.33519.0_x64__8wekyb3d8bbwe",
	// Right prefix, wrong publisher id: not Microsoft's package.
	"Microsoft.DesktopAppInstaller_9.9.9.9_x64__0000000000000",
	// Garbage version.
	"Microsoft.DesktopAppInstaller_latest_x64__8wekyb3d8bbwe",
}

func TestWingetCandidatesPreferTheHighestNativeVersion(t *testing.T) {
	for _, tc := range []struct {
		name    string
		listing []string
		goarch  string
		want    []string
	}{
		{
			// 1.23 > 1.21 > 1.9: numeric, not lexical. A lexical sort puts
			// 1.9 first and runs a winget two years out of date.
			name: "x64 picks the numerically highest", listing: windowsAppsListing, goarch: "amd64",
			want: []string{
				"Microsoft.DesktopAppInstaller_1.23.1911.0_x64__8wekyb3d8bbwe",
				"Microsoft.DesktopAppInstaller_1.21.3482.0_x64__8wekyb3d8bbwe",
				"Microsoft.DesktopAppInstaller_1.9.25200.0_x64__8wekyb3d8bbwe",
			},
		},
		{
			name: "arm64 prefers native, then emulated x64",
			listing: []string{
				"Microsoft.DesktopAppInstaller_1.24.1.0_x64__8wekyb3d8bbwe",
				"Microsoft.DesktopAppInstaller_1.23.1911.0_arm64__8wekyb3d8bbwe",
			},
			goarch: "arm64",
			want: []string{
				"Microsoft.DesktopAppInstaller_1.23.1911.0_arm64__8wekyb3d8bbwe",
				"Microsoft.DesktopAppInstaller_1.24.1.0_x64__8wekyb3d8bbwe",
			},
		},
		{
			name: "no x64 package on an x64 host means no candidate",
			listing: []string{
				"Microsoft.DesktopAppInstaller_1.23.1911.0_arm64__8wekyb3d8bbwe",
				"Microsoft.DesktopAppInstaller_1.23.1911.0_neutral_~_8wekyb3d8bbwe",
			},
			goarch: "amd64",
			want:   nil,
		},
		{name: "empty directory", listing: nil, goarch: "amd64", want: nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := wingetCandidates(tc.listing, tc.goarch)
			if len(got) != len(tc.want) {
				t.Fatalf("wingetCandidates = %v; want %v", got, tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Fatalf("wingetCandidates[%d] = %q; want %q (full: %v)", i, got[i], tc.want[i], got)
				}
			}
		})
	}
}

func TestLocateWingetSkipsAPackageDirectoryWithoutTheExecutable(t *testing.T) {
	// A half-serviced package directory (the newest version staged, its files
	// not yet in place) must not be chosen just because its name sorts first.
	root := filepath.Join("C:", "Program Files")
	apps := filepath.Join(root, "WindowsApps")
	older := filepath.Join(apps, "Microsoft.DesktopAppInstaller_1.21.3482.0_x64__8wekyb3d8bbwe", "winget.exe")
	readDir := func(dir string) ([]string, error) {
		if dir != apps {
			t.Fatalf("listed %q; want %q", dir, apps)
		}
		return windowsAppsListing, nil
	}
	exists := func(p string) bool { return p == older }

	got, ok := locateWingetIn(root, "amd64", readDir, exists)
	if !ok || got != older {
		t.Fatalf("locateWingetIn = %q, %v; want %q", got, ok, older)
	}

	if _, ok := locateWingetIn(root, "amd64", readDir, func(string) bool { return false }); ok {
		t.Fatal("found a winget.exe where none exists")
	}
	if _, ok := locateWingetIn(root, "amd64", func(string) ([]string, error) {
		return nil, errors.New("access denied")
	}, exists); ok {
		t.Fatal("an unreadable WindowsApps produced a winget path")
	}
	if _, ok := locateWingetIn("", "amd64", readDir, exists); ok {
		t.Fatal("an unknown Program Files directory produced a winget path")
	}
}

func TestWingetLookupPrefersThePackageAndFallsBackToPath(t *testing.T) {
	// The agent runs as LocalSystem, where the per-user App Execution Alias
	// that puts winget on PATH does not exist. The package directory is tried
	// first; PATH is the fallback (a machine where an admin has put winget on
	// the system PATH, or a future Windows that ships it there).
	const pkg = `C:\Program Files\WindowsApps\Microsoft.DesktopAppInstaller_1.23.1911.0_x64__8wekyb3d8bbwe\winget.exe`
	found := func() (string, bool) { return pkg, true }
	missing := func() (string, bool) { return "", false }
	path := fakePaths("winget.exe", "msiexec.exe")

	look := wingetAwareLookPath(path, found)
	if got, err := look("winget.exe"); err != nil || got != pkg {
		t.Fatalf("winget.exe resolved to %q, %v; want the package path", got, err)
	}
	if got, err := look("msiexec.exe"); err != nil || got != "/fake/msiexec.exe" {
		t.Fatalf("msiexec.exe resolved to %q, %v; only winget.exe is special", got, err)
	}

	look = wingetAwareLookPath(path, missing)
	if got, err := look("winget.exe"); err != nil || got != "/fake/winget.exe" {
		t.Fatalf("winget.exe resolved to %q, %v; want the PATH fallback", got, err)
	}

	look = wingetAwareLookPath(nothingInstalled(), missing)
	if _, err := look("winget.exe"); err == nil {
		t.Fatal("winget.exe resolved on a machine that has none")
	}
}

func TestWingetUnderSystemPlansTheResolvedPackagePath(t *testing.T) {
	// End to end through the planner: the argv names the package's winget.exe.
	const pkg = `C:\Program Files\WindowsApps\Microsoft.DesktopAppInstaller_1.23.1911.0_x64__8wekyb3d8bbwe\winget.exe`
	look := wingetAwareLookPath(nothingInstalled(), func() (string, bool) { return pkg, true })
	cmd, err := planWinget("install", "Mozilla.Firefox", nil, look)
	if err != nil {
		t.Fatalf("planWinget: %v", err)
	}
	if cmd.Path != pkg || cmd.Label != "winget.exe" {
		t.Fatalf("planned %q (%s); want %q", cmd.Path, cmd.Label, pkg)
	}
}
