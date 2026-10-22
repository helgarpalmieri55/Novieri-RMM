package inventory

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The macOS application scan and plist readers, on Linux, against bundles
// whose Info.plist files were written by Python's plistlib in both of the
// formats macOS uses (XML and bplist00). testdata/macos stands in for "/".

func TestMacApplicationsReadsBundlesInBothPlistFormats(t *testing.T) {
	got := macApplications(os.DirFS("testdata/macos"), maxSoftwareScan)
	out, _ := normalizeSoftware(got)

	want := map[string]string{
		"Google Chrome":  "129.0.6668.59", // XML; CFBundleDisplayName wins over "Chrome"
		"Slack":          "4.40.126",      // binary plist
		"Café Menu ☕":    "2.1",           // binary plist, UTF-16 string
		"Terminal":       "2.14",          // /Applications/Utilities
		"Microsoft Word": "16.89",         // a vendor folder one level down
		"Tool":           "42",            // placeholder display name; CFBundleVersion fallback
		"zoom.us":        "6.2.0 (38990)", // no name keys: the bundle's folder name
	}
	for _, s := range out {
		v, ok := want[s.Name]
		if !ok {
			t.Errorf("unexpected application %q (a nested helper, a broken bundle or a non-bundle leaked)", s.Name)
			continue
		}
		if s.Version != v {
			t.Errorf("%s version = %q; want %q", s.Name, s.Version, v)
		}
		if s.Publisher != "" || s.InstallDate != nil {
			t.Errorf("%s: publisher/install_date %q/%v invented; Info.plist has neither", s.Name, s.Publisher, s.InstallDate)
		}
	}
	if len(out) != len(want) {
		t.Fatalf("got %d applications %+v; want %d", len(out), out, len(want))
	}
}

func TestMacApplicationsStopsAtTheBound(t *testing.T) {
	if got := macApplications(os.DirFS("testdata/macos"), 2); len(got) != 2 {
		t.Fatalf("got %d; want the scan to stop at 2", len(got))
	}
}

func TestParsePlistReadsTopLevelStrings(t *testing.T) {
	for _, path := range []string{
		"testdata/macos/Applications/Google Chrome.app/Contents/Info.plist", // XML
		"testdata/macos/Applications/Slack.app/Contents/Info.plist",         // binary
	} {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		values, err := parsePlist(raw)
		if err != nil {
			t.Fatalf("%s: %v", filepath.Base(filepath.Dir(filepath.Dir(path))), err)
		}
		if values["CFBundleIdentifier"] == "" || values["CFBundlePackageType"] != "APPL" {
			t.Errorf("%s: %v", path, values)
		}
		// Nested dictionaries are skipped, not flattened into the top level.
		for _, nested := range []string{"CFBundleURLName", "MallocNanoZone", "algorithm"} {
			if _, ok := values[nested]; ok {
				t.Errorf("%s: nested key %q surfaced at the top level", path, nested)
			}
		}
	}
}

func TestParsePlistRefusesMalformedInput(t *testing.T) {
	// FALSIFIER: a panic or an out-of-range read. Every one of these must come
	// back as an error, never as a crash of the agent.
	slack, err := os.ReadFile("testdata/macos/Applications/Slack.app/Contents/Info.plist")
	if err != nil {
		t.Fatal(err)
	}
	cases := [][]byte{
		nil,
		[]byte("bplist00"),
		[]byte("bplist00\x00\x01garbage"),
		append([]byte(nil), slack[:len(slack)-5]...), // truncated trailer
		[]byte("<?xml version=\"1.0\"?><plist><array><string>x</string></array></plist>"),
		[]byte("not a plist"),
	}
	// Every single-byte corruption of a real binary plist.
	for i := 8; i < len(slack); i++ {
		c := append([]byte(nil), slack...)
		c[i] ^= 0xff
		cases = append(cases, c)
	}
	for _, raw := range cases {
		func() {
			defer func() {
				if r := recover(); r != nil {
					t.Fatalf("parsePlist panicked on %d bytes: %v", len(raw), r)
				}
			}()
			_, _ = parsePlist(raw)
		}()
	}
	if _, err := parsePlist([]byte("not a plist")); err == nil {
		t.Error("parsePlist accepted text that is not a plist")
	}
}

func TestParseKernBoottime(t *testing.T) {
	out := "{ sec = 1726816323, usec = 483000 } Fri Sep 20 07:12:03 2024\n"
	if got := parseKernBoottime(out); got != "2024-09-20T07:12:03Z" {
		t.Fatalf("parseKernBoottime = %q", got)
	}
	for _, junk := range []string{"", "sysctl: unknown oid", "{ sec = x }"} {
		if got := parseKernBoottime(junk); got != "" {
			t.Errorf("parseKernBoottime(%q) = %q; want unknown", junk, got)
		}
	}
}

func TestParseFDESetupStatus(t *testing.T) {
	for _, tc := range []struct {
		out  string
		want any
	}{
		{"FileVault is On.\n", true},
		{"FileVault is Off.\n", false},
		{"FileVault is On.\nEncryption in progress: Percent completed = 45.2\n", true},
		{"FileVault is Off, but will be enabled after the next restart.\n", false},
		{"FileVault is Off.\nDecryption in progress: Percent completed = 12.0\n", false},
		{"", nil},
		{"Error: This command requires root.", nil},
	} {
		if got := parseFDESetup(tc.out); got != tc.want {
			t.Errorf("parseFDESetup(%q) = %#v; want %#v", tc.out, got, tc.want)
		}
	}
}

func TestMacConsoleUser(t *testing.T) {
	for _, tc := range []struct{ out, want string }{
		{"alice\n", "alice"},
		{"root\n", ""}, // the login window
		{"_mbsetupuser\n", ""},
		{"", ""},
	} {
		if got := macConsoleUser(tc.out); got != tc.want {
			t.Errorf("macConsoleUser(%q) = %q; want %q", tc.out, got, tc.want)
		}
	}
	if strings.Contains(macConsoleUser("a\nb"), "\n") {
		t.Error("a multi-line answer passed through")
	}
}
