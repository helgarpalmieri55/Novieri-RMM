package inventory

import (
	"strconv"
	"strings"
	"testing"
)

func TestInstallDateAcceptsOnlyWhatTheServerCanParse(t *testing.T) {
	// SoftwareDoc.install_date is a pydantic `date`. One unparseable value
	// fails the WHOLE document, so anything that is not a real calendar date
	// becomes absent rather than being passed through.
	for _, tc := range []struct{ raw, want string }{
		{"20230415", "2023-04-15"},   // Windows Uninstall key: YYYYMMDD
		{" 20230415 ", "2023-04-15"}, // trailing space, seen in the wild
		{"2023-04-15", "2023-04-15"}, // already ISO
		{"20231341", ""},             // month 13
		{"20230230", ""},             // Feb 30
		{"00000000", ""},
		{"4/15/2023", ""}, // locale formats are ambiguous; not guessed
		{"", ""},
		{"2023041", ""},
		{"Mon Apr 17 2023", ""},
	} {
		got := installDate(tc.raw)
		switch {
		case tc.want == "" && got != nil:
			t.Errorf("installDate(%q) = %q; want absent", tc.raw, *got)
		case tc.want != "" && (got == nil || *got != tc.want):
			t.Errorf("installDate(%q) = %v; want %q", tc.raw, got, tc.want)
		}
	}
}

func TestNormalizeSoftwareDedupesSortsAndClamps(t *testing.T) {
	in := []Software{
		{Name: "Zoom Workplace", Version: "6.1.0", Publisher: "Zoom Video Communications, Inc."},
		// Same product registered in both registry views, older one first.
		{Name: "7-Zip 23.01 (x64)", Version: "23.01", Publisher: ""},
		{Name: "7-Zip 23.01 (x64)", Version: "23.01", Publisher: "Igor Pavlov", InstallDate: ptr("2024-01-02")},
		{Name: "Google Chrome", Version: "128.0.6613.85", Publisher: "Google LLC"},
		{Name: "Google Chrome", Version: "129.0.6668.59", Publisher: "Google LLC"},
		{Name: "  ", Version: "1"},
		{Name: "bad\x00name\tin registry", Version: "1.0\x00"},
		{Name: "Long", Version: strings.Repeat("9", 150), Publisher: strings.Repeat("p", 250)},
	}
	out, truncated := normalizeSoftware(in)
	if truncated {
		t.Fatal("truncated with 8 entries")
	}
	names := make([]string, 0, len(out))
	for _, s := range out {
		names = append(names, s.Name)
	}
	want := []string{"7-Zip 23.01 (x64)", "bad name in registry", "Google Chrome", "Long", "Zoom Workplace"}
	if strings.Join(names, "|") != strings.Join(want, "|") {
		t.Fatalf("names = %q; want %q", names, want)
	}
	byName := map[string]Software{}
	for _, s := range out {
		byName[s.Name] = s
	}
	if s := byName["7-Zip 23.01 (x64)"]; s.Publisher != "Igor Pavlov" || s.InstallDate == nil {
		t.Errorf("duplicate did not merge its missing fields: %+v", s)
	}
	if s := byName["Google Chrome"]; s.Version != "129.0.6668.59" {
		t.Errorf("duplicate kept version %q; want the higher one", s.Version)
	}
	if s := byName["bad name in registry"]; s.Version != "1.0" {
		t.Errorf("control characters survived: %+v", s)
	}
	// installed_software.version is VARCHAR(100) and publisher VARCHAR(200):
	// an over-long value is a database error that fails the whole ingest.
	if s := byName["Long"]; len(s.Version) != maxSoftwareVersion || len(s.Publisher) != maxSoftwarePublisher {
		t.Errorf("version %d / publisher %d bytes; want %d / %d",
			len(s.Version), len(s.Publisher), maxSoftwareVersion, maxSoftwarePublisher)
	}
}

func TestNormalizeSoftwareDropsAnInstallDateTheServerWouldReject(t *testing.T) {
	out, _ := normalizeSoftware([]Software{{Name: "x", InstallDate: ptr("20231341")}})
	if len(out) != 1 || out[0].InstallDate != nil {
		t.Fatalf("out = %+v; an invalid date must be dropped, not sent", out)
	}
}

func TestNormalizeSoftwareCapsAndSaysSo(t *testing.T) {
	in := make([]Software, 0, maxSoftware+10)
	for i := 0; i < maxSoftware+10; i++ {
		in = append(in, Software{Name: "pkg-" + strconv.Itoa(i)})
	}
	out, truncated := normalizeSoftware(in)
	if len(out) != maxSoftware || !truncated {
		t.Fatalf("len = %d truncated = %v; want %d and true", len(out), truncated, maxSoftware)
	}
}

func TestCompareLooseVersions(t *testing.T) {
	for _, tc := range []struct {
		a, b string
		want int
	}{
		{"129.0.6668.59", "128.0.6613.85", 1},
		{"1.9", "1.10", -1},
		{"2.0", "2.0.0", 0},
		{"1:2.3-1ubuntu1", "1:2.3-1ubuntu2", -1},
		{"", "1", -1},
		{"1.0b", "1.0a", 1},
	} {
		if got := compareLooseVersions(tc.a, tc.b); got != tc.want {
			t.Errorf("compareLooseVersions(%q, %q) = %d; want %d", tc.a, tc.b, got, tc.want)
		}
	}
}

func TestCollectCarriesFactsAndSoftware(t *testing.T) {
	f := fakeFacts()
	f.Software = []Software{{Name: "Firefox", Version: "130.0"}, {Name: "Firefox", Version: "129.0"}}
	f.Facts = map[string]any{
		"disk_encryption": true,
		"console_user":    "CORP\\alice",
		"model":           "",  // unknown: omitted
		"manufacturer":    nil, // unknown: omitted
	}
	doc, err := collect(f, "windows")
	if err != nil {
		t.Fatal(err)
	}
	if len(doc.Software) != 1 || doc.Software[0].Version != "130.0" {
		t.Fatalf("software = %+v", doc.Software)
	}
	if doc.Facts["disk_encryption"] != true || doc.Facts["console_user"] != "CORP\\alice" {
		t.Fatalf("facts = %v", doc.Facts)
	}
	for _, k := range []string{"model", "manufacturer", "software_truncated"} {
		if _, ok := doc.Facts[k]; ok {
			t.Errorf("facts[%q] present; an unknown fact is absent", k)
		}
	}
	for _, problem := range validateAgainstSchema(t, reencode(t, doc)) {
		t.Errorf("schema: %s", problem)
	}
}

func ptr(s string) *string { return &s }
