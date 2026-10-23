package inventory

import (
	"bufio"
	"os"
	"strconv"
	"strings"
	"testing"
)

// The Windows software parsers, exercised on Linux against a regedit export
// of real Uninstall keys (testdata/windows-uninstall.reg).

func TestUninstallEntriesBecomeTheProgramsAndFeaturesList(t *testing.T) {
	got := softwareFromUninstall(loadRegExport(t, "testdata/windows-uninstall.reg"))
	out, _ := normalizeSoftware(got)

	want := map[string]Software{
		"7-Zip 23.01 (x64)":                         {Version: "23.01", Publisher: "Igor Pavlov"},
		"Microsoft 365 Apps for enterprise - en-us": {Version: "16.0.17928.20156", Publisher: "Microsoft Corporation"},
		"Notepad++ (64-bit x64)":                    {Version: "8.6.2", Publisher: "Notepad++ Team"},
		"Check Point Harmony Endpoint":              {Version: "E88.30", Publisher: "Check Point Software Technologies Ltd.", InstallDate: ptr("2024-08-01")},
		"Google Chrome":                             {Version: "129.0.6668.59", Publisher: "Google LLC", InstallDate: ptr("2024-09-18")},
		"Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.38.33135": {Version: "14.38.33135.0", Publisher: "Microsoft Corporation", InstallDate: ptr("2024-01-10")},
		"Microsoft OneDrive": {Version: "24.161.0811.0001", Publisher: "Microsoft Corporation"},
		"Zoom Workplace":     {Version: "6.1.11 (45896)", Publisher: "Zoom Video Communications, Inc.", InstallDate: ptr("2024-09-05")},
	}
	var names []string
	for _, s := range out {
		names = append(names, s.Name)
		w, ok := want[s.Name]
		if !ok {
			t.Errorf("unexpected entry %q (system component, update or unnamed key leaked through)", s.Name)
			continue
		}
		if s.Version != w.Version || s.Publisher != w.Publisher {
			t.Errorf("%s = %q/%q; want %q/%q", s.Name, s.Version, s.Publisher, w.Version, w.Publisher)
		}
		if (s.InstallDate == nil) != (w.InstallDate == nil) ||
			(s.InstallDate != nil && *s.InstallDate != *w.InstallDate) {
			t.Errorf("%s install_date = %v; want %v", s.Name, deref(s.InstallDate), deref(w.InstallDate))
		}
	}
	if len(out) != len(want) {
		t.Fatalf("got %d entries %q; want %d", len(out), names, len(want))
	}
}

func TestIsUpdateEntry(t *testing.T) {
	for _, tc := range []struct {
		e    uninstallEntry
		want bool
	}{
		{uninstallEntry{ReleaseType: "Security Update"}, true},
		{uninstallEntry{ReleaseType: "Update Rollup"}, true},
		{uninstallEntry{ReleaseType: "Hotfix"}, true},
		{uninstallEntry{ReleaseType: "Service Pack"}, true},
		{uninstallEntry{ParentKeyName: "Office16.PROPLUS"}, true},
		{uninstallEntry{ReleaseType: ""}, false},
		{uninstallEntry{ReleaseType: "Application"}, false},
	} {
		if got := isUpdateEntry(tc.e); got != tc.want {
			t.Errorf("isUpdateEntry(%+v) = %v; want %v", tc.e, got, tc.want)
		}
	}
}

func TestOnlyRealUserHivesAreRead(t *testing.T) {
	// HKEY_USERS as SYSTEM sees it with one AD user and one Entra user logged
	// on. _Classes hives and the service accounts hold no user installs.
	hku := []string{
		".DEFAULT", "S-1-5-19", "S-1-5-20", "S-1-5-18",
		"S-1-5-21-3623811015-3361044348-30300820-1013",
		"S-1-5-21-3623811015-3361044348-30300820-1013_Classes",
		"S-1-12-1-1484371212-1235112345-2340987654-1234567890",
		"S-1-12-1-1484371212-1235112345-2340987654-1234567890_Classes",
	}
	var got []string
	for _, sid := range hku {
		if isUserHive(sid) {
			got = append(got, sid)
		}
	}
	want := []string{
		"S-1-5-21-3623811015-3361044348-30300820-1013",
		"S-1-12-1-1484371212-1235112345-2340987654-1234567890",
	}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("user hives = %v; want %v", got, want)
	}
}

func TestParseAppxReadsTheOneShotListing(t *testing.T) {
	// windowsAppxScript's output: ConvertTo-Json -InputObject @(...) keeps an
	// array even for one element; the single-object form is accepted anyway.
	raw := `[{"n":"Microsoft.WindowsTerminal","v":"1.20.11781.0","p":"CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, S=Washington, C=US"},` +
		`{"n":"MSTeams","v":"24215.1007.3082.1590","p":"CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, S=Washington, C=US"},` +
		`{"n":"SpotifyAB.SpotifyMusic","v":"1.246.453.0","p":"CN=453637B3-4E12-4CDF-B0D3-2A3C863BF6EF"},` +
		`{"n":"Contoso.App","v":"2.0.0.0","p":"CN=\"Contoso, Ltd.\", O=\"Contoso, Ltd.\", C=GB"},` +
		`{"n":"","v":"1.0.0.0","p":"CN=x"}]`
	got := parseAppx(raw)
	want := []Software{
		{Name: "Microsoft.WindowsTerminal", Version: "1.20.11781.0", Publisher: "Microsoft Corporation"},
		{Name: "MSTeams", Version: "24215.1007.3082.1590", Publisher: "Microsoft Corporation"},
		// A Store publisher is a GUID; it is not a name, so none is invented.
		{Name: "SpotifyAB.SpotifyMusic", Version: "1.246.453.0", Publisher: ""},
		{Name: "Contoso.App", Version: "2.0.0.0", Publisher: "Contoso, Ltd."},
	}
	if len(got) != len(want) {
		t.Fatalf("parseAppx = %+v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("[%d] = %+v; want %+v", i, got[i], want[i])
		}
	}
	single := parseAppx(`{"n":"MSTeams","v":"1.0","p":"O=Microsoft Corporation"}`)
	if len(single) != 1 || single[0].Name != "MSTeams" {
		t.Errorf("single object = %+v", single)
	}
	for _, junk := range []string{"", "Get-AppxPackage : not recognized", "null", "[1,2]"} {
		if got := parseAppx(junk); len(got) != 0 {
			t.Errorf("parseAppx(%q) = %+v; want nothing", junk, got)
		}
	}
}

func TestTheAppxScriptInterpolatesNothing(t *testing.T) {
	for _, forbidden := range []string{"%s", "%v", "%d", "%q", "{}"} {
		if strings.Contains(windowsAppxScript, forbidden) {
			t.Fatalf("windowsAppxScript contains %q", forbidden)
		}
	}
	if !strings.Contains(windowsAppxScript, "-AllUsers") || !strings.Contains(windowsAppxScript, "-InputObject") {
		t.Fatal("windowsAppxScript no longer lists every user's packages as a JSON array")
	}
}

func TestParseWindowsFactsReadsTheExtraFacts(t *testing.T) {
	raw := `{"caption":"Microsoft Windows 11 Enterprise","version":"10.0.22631","build":"22631",` +
		`"manufacturer":"LENOVO","model":"21AH00FKUK","partofdomain":true,"user":"CORP\\alice",` +
		`"boot":"2026-09-20T07:12:03.4830000Z","bitlocker":1}`
	f := parseWindowsFacts(raw)
	want := map[string]any{
		factManufacturer: "LENOVO", factModel: "21AH00FKUK", factDomainJoined: true,
		factConsoleUser: `CORP\alice`, factLastBoot: "2026-09-20T07:12:03Z", factDiskEncryption: true,
	}
	for k, v := range want {
		if f.Facts[k] != v {
			t.Errorf("facts[%s] = %#v; want %#v", k, f.Facts[k], v)
		}
	}

	// Unknowns stay unknown: no BitLocker namespace (Home edition), nobody at
	// the console, a boot time PowerShell could not render.
	f = parseWindowsFacts(`{"partofdomain":false,"bitlocker":null,"user":null,"boot":"garbage"}`)
	if f.Facts[factDomainJoined] != false {
		t.Errorf("domain_joined = %#v; want false", f.Facts[factDomainJoined])
	}
	for _, k := range []string{factDiskEncryption, factConsoleUser, factLastBoot} {
		if v, ok := f.Facts[k]; ok && v != nil && v != "" {
			t.Errorf("facts[%s] = %#v; want unknown", k, v)
		}
	}
	// ProtectionStatus 0 is "off", 2 is "unknown" (volume locked).
	if f := parseWindowsFacts(`{"bitlocker":0}`); f.Facts[factDiskEncryption] != false {
		t.Errorf("bitlocker 0 -> %#v; want false", f.Facts[factDiskEncryption])
	}
	if f := parseWindowsFacts(`{"bitlocker":2}`); f.Facts[factDiskEncryption] != nil {
		t.Errorf("bitlocker 2 -> %#v; want unknown", f.Facts[factDiskEncryption])
	}
}

// loadRegExport parses the subset of the regedit export format the fixture
// uses: [key] headers, "name"="string" and "name"=dword:hex. Other value types
// (hex(2): and friends) are skipped, as the collector never reads them.
func loadRegExport(t *testing.T, path string) []uninstallEntry {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	var out []uninstallEntry
	var cur map[string]string
	flush := func() {
		if cur != nil {
			e := uninstallEntry{
				DisplayName:    cur["DisplayName"],
				DisplayVersion: cur["DisplayVersion"],
				Publisher:      cur["Publisher"],
				InstallDate:    cur["InstallDate"],
				ParentKeyName:  cur["ParentKeyName"],
				ReleaseType:    cur["ReleaseType"],
			}
			if n, err := strconv.ParseUint(cur["SystemComponent"], 10, 32); err == nil {
				e.SystemComponent = n == 1
			}
			out = append(out, e)
		}
	}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		switch {
		case strings.HasPrefix(line, "["):
			flush()
			cur = map[string]string{}
		case strings.HasPrefix(line, `"`) && cur != nil:
			name, value, ok := strings.Cut(line[1:], `"=`)
			if !ok {
				continue
			}
			switch {
			case strings.HasPrefix(value, `"`):
				s, err := strconv.Unquote(value)
				if err != nil {
					t.Fatalf("bad string value in %s: %s", path, line)
				}
				cur[name] = s
			case strings.HasPrefix(value, "dword:"):
				n, err := strconv.ParseUint(value[len("dword:"):], 16, 32)
				if err != nil {
					t.Fatalf("bad dword in %s: %s", path, line)
				}
				cur[name] = strconv.FormatUint(n, 10)
			}
		}
	}
	flush()
	if len(out) == 0 {
		t.Fatalf("%s produced no keys", path)
	}
	return out
}

func deref(s *string) string {
	if s == nil {
		return "<nil>"
	}
	return *s
}
