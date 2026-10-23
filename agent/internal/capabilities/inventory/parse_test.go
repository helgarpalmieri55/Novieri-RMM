package inventory

import (
	"strings"
	"testing"
)

// The Windows and macOS fact parsers, exercised ON LINUX.
//
// This container has no Windows and no Mac, so the honest statement about those
// platforms is: the shell-out in facts_windows.go / facts_darwin.go has never
// run, and everything that interprets its output -- the JSON shape, the string
// -> int64 conversions, the FQDN composition, the ioreg and sw_vers scraping --
// is tested here against captured output. That is why those parsers live in
// files with no build tag.

func TestParseWindowsFactsMapsTheDocumentedCimShape(t *testing.T) {
	// The shape `windowsFactsScript | ConvertTo-Json -Compress` produces.
	// TotalPhysicalMemory and Size arrive as STRINGS by construction: they are
	// UInt64 and exceed float64's exact-integer range on a large machine, and a
	// silent reformat is the class of bug internal/canonical exists to stop.
	raw := `{"caption":"Microsoft Windows 11 Pro","version":"10.0.22631","build":"22631",` +
		`"domain":"corp.example","dnshost":"WS-0042","memory":"34359738368",` +
		`"serial":"PF3ABCDE","cpu":"12th Gen Intel(R) Core(TM) i7-1265U","cores":10,` +
		`"disks":[{"id":"\\\\.\\PHYSICALDRIVE0","model":"SAMSUNG MZVL2512","serial":"S64ANX0",` +
		`"size":"512110190592"}]}`

	f := parseWindowsFacts(raw)

	if f.OSVersion != "Microsoft Windows 11 Pro 10.0.22631" {
		t.Errorf("OSVersion = %q", f.OSVersion)
	}
	if f.OSBuild != "22631" {
		t.Errorf("OSBuild = %q", f.OSBuild)
	}
	if f.FQDN != "WS-0042.corp.example" {
		t.Errorf("FQDN = %q; the domain-joined name is host + domain", f.FQDN)
	}
	if f.Serial != "PF3ABCDE" {
		t.Errorf("Serial = %q", f.Serial)
	}
	if f.RAMBytes != 34359738368 {
		t.Errorf("RAMBytes = %d; want 34359738368 exactly -- a float64 round trip is the failure "+
			"this string field exists to prevent", f.RAMBytes)
	}
	var disk *Component
	for i := range f.Components {
		if f.Components[i].Type == "disk" {
			disk = &f.Components[i]
		}
	}
	if disk == nil {
		t.Fatalf("no disk component: %+v", f.Components)
	}
	if disk.Identifier != `\\.\PHYSICALDRIVE0` {
		t.Errorf("disk identifier = %q", disk.Identifier)
	}
	if disk.Attributes["size_bytes"] != int64(512110190592) {
		t.Errorf("disk size_bytes = %v (%T); want int64(512110190592)",
			disk.Attributes["size_bytes"], disk.Attributes["size_bytes"])
	}
}

func TestParseWindowsFactsDegradesRatherThanFailing(t *testing.T) {
	// A PowerShell that did not run, or ran and errored, is not a reason for
	// the device to have no inventory: the caller still has a hostname and an
	// architecture from the Go runtime.
	//
	// FALSIFIER: any non-zero field. "It did not panic" is not an observation.
	for _, tc := range []struct{ name, raw string }{
		{"no output at all", ""},
		{"an error message", "Get-CimInstance : Access is denied."},
		{"truncated json", `{"caption":"Windows`},
		{"json array", `[1,2,3]`},
		{"json null", `null`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := parseWindowsFacts(tc.raw)
			if f.OSVersion != "" || f.RAMBytes != 0 || f.Serial != "" || len(f.Components) != 0 {
				t.Fatalf("unparseable output produced facts: %+v", f)
			}
		})
	}
}

func TestAWorkgroupMachineGetsNoComposedFQDN(t *testing.T) {
	// Win32_ComputerSystem.Domain is "WORKGROUP" on a machine that is not
	// domain-joined. "WS-0042.WORKGROUP" is not a name that resolves anywhere,
	// and inventing one would put a fictional FQDN on every home device.
	f := parseWindowsFacts(`{"dnshost":"WS-0042","domain":"WORKGROUP"}`)
	if f.FQDN != "" {
		t.Fatalf("FQDN = %q; a workgroup machine has no fully-qualified name", f.FQDN)
	}
}

func TestParseWindowsFactsRefusesNonsenseNumbers(t *testing.T) {
	f := parseWindowsFacts(`{"memory":"-1","disks":[{"id":"d","size":"not-a-number"}]}`)
	if f.RAMBytes != 0 {
		t.Errorf("RAMBytes = %d; a negative memory size is no size", f.RAMBytes)
	}
	if len(f.Components) != 1 {
		t.Fatalf("components = %+v; the disk is still a disk without a size", f.Components)
	}
	if _, ok := f.Components[0].Attributes["size_bytes"]; ok {
		t.Errorf("an unparseable size became an attribute: %v", f.Components[0].Attributes)
	}
}

func TestParseSWVersReadsTheThreeKeysItNeeds(t *testing.T) {
	name, version, build := parseSWVers("ProductName:\t\tmacOS\nProductVersion:\t\t14.5\nBuildVersion:\t\t23F79\n")
	if name != "macOS" || version != "14.5" || build != "23F79" {
		t.Fatalf("parseSWVers = %q, %q, %q", name, version, build)
	}
}

func TestParseSWVersDegradesOnAnythingElse(t *testing.T) {
	for _, raw := range []string{"", "command not found", "ProductName", ":::"} {
		name, version, build := parseSWVers(raw)
		if name != "" || version != "" || build != "" {
			t.Fatalf("parseSWVers(%q) = %q, %q, %q; want empties", raw, name, version, build)
		}
	}
}

func TestParseIORegSerialFindsThePlatformSerial(t *testing.T) {
	out := `+-o J316cAP  <class IOPlatformExpertDevice, id 0x100000241>
    {
      "IOPlatformUUID" = "1B2C3D4E-0000-0000-0000-000000000000"
      "IOPlatformSerialNumber" = "C02XY1Z2ABCD"
      "model" = <"Mac14,7">
    }`
	if got := parseIORegSerial(out); got != "C02XY1Z2ABCD" {
		t.Fatalf("parseIORegSerial = %q", got)
	}
}

func TestParseIORegSerialSaysNothingWhenThereIsNoSerial(t *testing.T) {
	// A VM has no platform serial. Absence must stay absence: `optional()`
	// turns "" into an omitted member, and an omitted member does not overwrite
	// a serial the control plane already holds.
	for _, raw := range []string{"", "no such key here", `"IOPlatformUUID" = "x"`} {
		if got := parseIORegSerial(raw); got != "" {
			t.Fatalf("parseIORegSerial(%q) = %q; want %q", raw, got, "")
		}
	}
}

func TestTheWindowsFactScriptInterpolatesNothing(t *testing.T) {
	// Rule 1 in exec.go: nothing job-supplied ever reaches an argument. The
	// source walker in inventory_test.go proves this package never READS
	// job.Params(); this proves the one string that is handed to a shell-like
	// interpreter is a constant with no place to put anything.
	//
	// FALSIFIER: a format verb or a Go string concatenation marker in the
	// script. `+` between adjacent constants is invisible here -- the value is
	// already folded by the compiler -- so what this can catch is a %s left
	// behind by someone reaching for fmt.Sprintf.
	for _, forbidden := range []string{"%s", "%v", "%d", "%q", "{}"} {
		if strings.Contains(windowsFactsScript, forbidden) {
			t.Fatalf("windowsFactsScript contains %q; it must be a constant with nothing "+
				"interpolated into it", forbidden)
		}
	}
	// And it is not vacuous: it really is the script that asks for the facts.
	for _, expected := range []string{"Win32_OperatingSystem", "Win32_BIOS", "ConvertTo-Json"} {
		if !strings.Contains(windowsFactsScript, expected) {
			t.Fatalf("windowsFactsScript no longer mentions %q", expected)
		}
	}
}
