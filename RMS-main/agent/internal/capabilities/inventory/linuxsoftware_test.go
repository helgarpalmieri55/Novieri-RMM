package inventory

import (
	"os"
	"testing"
)

func TestParseDpkgStatusListsInstalledPackagesOnly(t *testing.T) {
	f, err := os.Open("testdata/dpkg-status")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	out, _ := normalizeSoftware(parseDpkgStatus(f, maxSoftwareScan))

	want := []Software{
		{Name: "adduser", Version: "3.137ubuntu1", Publisher: "Ubuntu Core Developers"},
		{Name: "docker-ce", Version: "5:27.3.1-1~ubuntu.24.04~noble", Publisher: "Docker"}, // held is installed
		{Name: "libc6", Version: "2.39-0ubuntu8.3", Publisher: "Ubuntu Developers"},        // both arches, one row
		{Name: "openssh-server", Version: "1:9.6p1-3ubuntu13.5", Publisher: "Ubuntu Developers"},
		{Name: "zabbix-agent2", Version: "1:7.0.4-1+ubuntu24.04", Publisher: "Zabbix LLC"}, // last stanza, no trailing blank line
	}
	if len(out) != len(want) {
		t.Fatalf("got %+v; want %+v (config-files and half-installed packages are not installed)", out, want)
	}
	for i := range want {
		if out[i] != want[i] {
			t.Errorf("[%d] = %+v; want %+v", i, out[i], want[i])
		}
	}
}

func TestParseDpkgStatusStopsAtTheBound(t *testing.T) {
	f, err := os.Open("testdata/dpkg-status")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if got := parseDpkgStatus(f, 2); len(got) != 2 {
		t.Fatalf("got %d; want 2", len(got))
	}
}

func TestParseRPMQuery(t *testing.T) {
	raw, err := os.ReadFile("testdata/rpm-qa.txt")
	if err != nil {
		t.Fatal(err)
	}
	out, _ := normalizeSoftware(parseRPMQuery(string(raw), maxSoftwareScan))
	want := []Software{
		{Name: "bash", Version: "5.1.8-9.el9", Publisher: "Red Hat, Inc.", InstallDate: ptr("2024-04-25")},
		{Name: "glibc", Version: "2.34-100.el9_4.4", Publisher: "Red Hat, Inc.", InstallDate: ptr("2024-06-21")},
		{Name: "local-tool", Version: "1.0-1"},
		{Name: "openssl", Version: "1:3.0.7-27.el9", Publisher: "Red Hat, Inc.", InstallDate: ptr("2024-04-25")},
		{Name: "zabbix-agent2", Version: "7.0.4-release1.el9", Publisher: "Zabbix LLC", InstallDate: ptr("2024-09-22")},
	}
	if len(out) != len(want) {
		t.Fatalf("got %+v; want %d entries (gpg-pubkey and a truncated line excluded)", out, len(want))
	}
	for i, w := range want {
		g := out[i]
		if g.Name != w.Name || g.Version != w.Version || g.Publisher != w.Publisher ||
			deref(g.InstallDate) != deref(w.InstallDate) {
			t.Errorf("[%d] = %+v (%s); want %+v (%s)", i, g, deref(g.InstallDate), w, deref(w.InstallDate))
		}
	}
}

func TestParseProcStatBootTime(t *testing.T) {
	content := "cpu  1 2 3 4\nintr 12345\nctxt 999\nbtime 1726816323\nprocesses 4242\n"
	if got := parseProcStatBtime(content); got != "2024-09-20T07:12:03Z" {
		t.Fatalf("parseProcStatBtime = %q", got)
	}
	for _, junk := range []string{"", "btime\n", "btime x\n", "cpu 1\n"} {
		if got := parseProcStatBtime(junk); got != "" {
			t.Errorf("parseProcStatBtime(%q) = %q; want unknown", junk, got)
		}
	}
}

func TestDMIPlaceholdersAreNotFacts(t *testing.T) {
	for _, junk := range []string{"To Be Filled By O.E.M.", "System manufacturer", "System Product Name",
		"Default string", "Not Specified", "None", "", "  "} {
		if got := dmiFact(junk); got != "" {
			t.Errorf("dmiFact(%q) = %q; a firmware placeholder is not a manufacturer", junk, got)
		}
	}
	for _, real := range []string{"Dell Inc.", "PowerEdge R650", "VMware, Inc.", "QEMU"} {
		if got := dmiFact(real + "\n"); got != real {
			t.Errorf("dmiFact(%q) = %q", real, got)
		}
	}
}
