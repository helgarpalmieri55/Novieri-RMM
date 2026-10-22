package backup

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// THE WINDOWS RESTORE RULES, PROVED ON EVERY OS.
//
// The fleet is mostly Windows and CI is mostly Linux. pathRules is injected so
// the rules a Windows endpoint enforces run here too, instead of existing only
// on the OS where nobody watches them run.

type ruleWant int

const (
	mapsTo ruleWant = iota
	refused
	skipped
)

func TestManifestPathsMapUnderARootByTheRestoringHostsRules(t *testing.T) {
	for _, tc := range []struct {
		rules pathRules
		in    string
		want  ruleWant
		rel   string
	}{
		// Windows: both separators, the volume as a directory.
		{windowsPaths, `C:/Users/a/x`, mapsTo, "C/Users/a/x"},
		{windowsPaths, `C:\Users\a\x`, mapsTo, "C/Users/a/x"},
		{windowsPaths, `c:\x`, mapsTo, "c/x"},
		{windowsPaths, `\\?\C:\Users\a`, mapsTo, "C/Users/a"},
		{windowsPaths, `/data/x`, mapsTo, "data/x"},
		{windowsPaths, `\data\x`, mapsTo, "data/x"},
		{windowsPaths, `a\b/c`, mapsTo, "a/b/c"},
		{windowsPaths, `a\.\b`, mapsTo, "a/b"},
		{windowsPaths, `..\x`, refused, ""},
		{windowsPaths, `..\..\outside\OWNED`, refused, ""},
		{windowsPaths, `a\..\..\x`, refused, ""},
		{windowsPaths, `C:\a\..\..\x`, refused, ""},
		{windowsPaths, `C:\a/../x`, refused, ""},
		{windowsPaths, `\\server\share\x`, refused, ""},
		{windowsPaths, `//server/share/x`, refused, ""},
		{windowsPaths, `\/server\share\x`, refused, ""},
		{windowsPaths, `\\?\UNC\server\share\x`, refused, ""},
		{windowsPaths, `\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1\x`, refused, ""},
		{windowsPaths, `\\?\Volume{6f1a2c34-5b6d-4e7f-8a9b-0c1d2e3f4a5b}\x`, refused, ""},
		{windowsPaths, `\\.\PhysicalDrive0`, refused, ""},
		{windowsPaths, `\\.\C:\x`, refused, ""},
		{windowsPaths, `x:stream`, refused, ""},
		{windowsPaths, `a\x:$DATA`, refused, ""},
		{windowsPaths, `C:x`, refused, "a drive-relative path"},
		{windowsPaths, `\C:\x`, refused, "a drive letter anywhere but first"},
		{windowsPaths, `a\CON`, skipped, ""},
		{windowsPaths, `a\con.txt`, skipped, ""},
		{windowsPaths, `a\NUL .txt`, skipped, ""},
		{windowsPaths, `a\COM1`, skipped, ""},
		{windowsPaths, "a\\LPT\u00b9", skipped, ""},
		{windowsPaths, `a\CONOUT$`, skipped, ""},
		{windowsPaths, `a\x.`, skipped, ""},
		{windowsPaths, `a\x `, skipped, ""},
		{windowsPaths, `a\x<y`, skipped, ""},
		{windowsPaths, `a\x?`, skipped, ""},
		{windowsPaths, `a\x*`, skipped, ""},
		{windowsPaths, `a\x|y`, skipped, ""},
		{windowsPaths, `a\x"y`, skipped, ""},
		{windowsPaths, "a\\x\x01y", skipped, ""},

		// Unix: a backslash is refused, not split; `?` is an ordinary name.
		{posixPaths, `C:/Users/a/x`, mapsTo, "C/Users/a/x"},
		{posixPaths, `/data/x`, mapsTo, "data/x"},
		{posixPaths, `/data/a?b*`, mapsTo, "data/a?b*"},
		{posixPaths, `..\..\outside\OWNED`, refused, ""},
		{posixPaths, `C:\x`, refused, ""},
		{posixPaths, `//server/share/x`, refused, ""},
		{posixPaths, `a/../x`, refused, ""},
		{posixPaths, `a/CON`, skipped, ""},
		{posixPaths, `a/x.`, skipped, ""},
	} {
		rules := map[pathRules]string{windowsPaths: "windows", posixPaths: "posix"}[tc.rules]
		rel, err := tc.rules.relUnderRoot(tc.in)
		var refusal *jobs.Refusal
		switch tc.want {
		case mapsTo:
			if err != nil || rel != tc.rel {
				t.Errorf("%s %q -> %q, %v; want %q", rules, tc.in, rel, err, tc.rel)
			}
		case refused:
			if !errors.As(err, &refusal) {
				t.Errorf("%s %q -> %q, %v; want a refusal", rules, tc.in, rel, err)
			}
		case skipped:
			if !errors.Is(err, errUnsafeName) {
				t.Errorf("%s %q -> %q, %v; want it skipped as unsafe", rules, tc.in, rel, err)
			}
		}
	}
}

func TestASelectionAndAnEntryAreComparedInOneSpelling(t *testing.T) {
	for _, tc := range []struct {
		rules   pathRules
		in, key string
	}{
		{windowsPaths, `C:\Users\a`, "C:/Users/a"},
		{windowsPaths, `c:/Users/a`, "C:/Users/a"},
		{windowsPaths, `\\?\C:\Users\a`, "C:/Users/a"},
		{windowsPaths, `..\..\x`, "../../x"},
		{posixPaths, `a\b`, `a\b`},
		{posixPaths, `c:/x`, "c:/x"},
	} {
		if got := tc.rules.selectionKey(tc.in); got != tc.key {
			t.Errorf("selectionKey(%q) = %q, want %q", tc.in, got, tc.key)
		}
	}
	// The operator picks `C:/Users/a` in the browser; the entry the writer
	// recorded may be spelled with the other separator or drive case.
	sel := windowsPaths.normaliseSelection([]string{`c:\Users\a\`})
	if !selects(sel, windowsPaths.selectionKey(`C:/Users/a/doc.txt`)) {
		t.Errorf("selection %v does not cover its own subtree", sel)
	}
}

func TestUnderWindowsRulesAHostileEntryIsSelectedRefusedAndWritesNothing(t *testing.T) {
	// THE BUG THIS PINS: on Windows the selection was normalised to forward
	// slashes and the entry path was not, so `..\..\outside\OWNED` matched
	// nothing, was never looked at, and the job reported success. Selected,
	// it must be REFUSED -- a refusal tells the control plane the manifest is
	// hostile; a silent success tells it nothing.
	//
	// FALSIFYING OBSERVATION: a nil error, or anything outside the root.
	base := t.TempDir()
	outside := filepath.Join(base, "outside")
	if err := os.MkdirAll(outside, 0o755); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct{ name, path string }{
		{"windows-separator", `..\..\outside\OWNED`},
		{"mixed-dotdot", filepath.ToSlash(outside) + `\..\OWNED`},
		{"unc", `\\server\share\OWNED`},
		{"long-unc", `\\?\UNC\server\share\OWNED`},
		{"globalroot", `\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1\OWNED`},
		{"device", `\\.\PhysicalDrive0`},
		{"ads", `C:\ok.txt:hidden`},
	} {
		t.Run(c.name, func(t *testing.T) {
			store := newFakeStore(testStorageRef)
			id := storeChunk(t, store, []byte("pwned"))
			ref := forgeManifest(t, store, []manifestEntry{fileEntry(c.path, id, 5)})
			root := filepath.Join(base, "root-"+c.name)
			svc := newTestService(store, quiet())
			svc.paths = windowsPaths
			res, err := svc.restore(context.Background(), testJobID,
				restoreParamsJSON(t, ref, []string{c.path}, root, modeRestore))
			var refusal *jobs.Refusal
			if !errors.As(err, &refusal) {
				t.Errorf("%q did not refuse: err=%v result=%+v", c.path, err, res)
			}
			if left := listAll(t, outside); len(left) != 0 {
				t.Fatalf("%q left %v outside the root", c.path, left)
			}
			if left := listAll(t, root); len(left) != 0 {
				t.Fatalf("%q was refused but wrote %v", c.path, left)
			}
		})
	}
}

func TestUnderWindowsRulesAnAbsoluteEntryLandsUnderItsDriveDirectory(t *testing.T) {
	// The mapping an alternate-location restore makes on Windows:
	// <root>\C\Users\..., never <root>\C:\Users\..., which is not a legal path.
	//
	// FALSIFYING OBSERVATION: the file anywhere but the drive-letter directory.
	for _, c := range []struct {
		entry, sel string
		rel        []string
	}{
		{`C:\Users\a\OWNED`, `C:\Users\a`, []string{"C", "Users", "a", "OWNED"}},
		{`C:/Users/a/OWNED`, `c:\Users\a\OWNED`, []string{"C", "Users", "a", "OWNED"}},
		{`\\?\D:\data\OWNED`, `D:\data`, []string{"D", "data", "OWNED"}},
	} {
		store := newFakeStore(testStorageRef)
		id := storeChunk(t, store, []byte("pwned"))
		ref := forgeManifest(t, store, []manifestEntry{fileEntry(c.entry, id, 5)})
		root := filepath.Join(t.TempDir(), "root")
		svc := newTestService(store, quiet())
		svc.paths = windowsPaths
		res, err := svc.restore(context.Background(), testJobID,
			restoreParamsJSON(t, ref, []string{c.sel}, root, modeRestore))
		if err != nil || res.State != stateSuccess {
			t.Fatalf("%q: %v %+v", c.entry, err, res)
		}
		got, err := os.ReadFile(filepath.Join(append([]string{root}, c.rel...)...))
		if err != nil || string(got) != "pwned" {
			t.Errorf("%q: nothing at %v under the root (%v); tree is %v",
				c.entry, c.rel, err, listAll(t, root))
		}
	}
}
