package patch

import (
	"reflect"
	"testing"
)

// The Windows Update mapping, tested on Linux.
//
// This is the half of the Windows engine that is a DECISION rather than a
// syscall, and it is all here on purpose: which update becomes a catalog row,
// what its stable identifier is, how MSRC's severity vocabulary maps onto the
// server's, and what is filtered out. com_windows.go and wua_windows.go -- the
// COM apartment, the IDispatch calls, the download/install loop -- have never
// executed and are not testable here at all.
//
// The `wuaUpdate` values below are constructed, not captured. That is a
// meaningfully weaker claim than apt's real fixture and a meaningfully stronger
// one than the macOS text fixture: a STRUCT cannot be wrong about its shape the
// way a text format can, so what is at risk is whether readWUAUpdate fills it
// correctly from COM -- which is exactly the part that has never run.

func TestTheIdentifierIsTheKBArticleAndTheGUIDIsOnlyTheFallback(t *testing.T) {
	// spec §4 names "KB id" as the Windows identifier and app/modules/patch's
	// tests use "KB1". The identifier is the catalog's unique key, so an
	// identifier that changed between two scans of the same machine would
	// produce a second catalog row and a device that looks permanently
	// unpatched.
	//
	// FALSIFYING OBSERVATION: a GUID where a KB article exists, or an
	// identifier that varies with the ORDER of KBArticleIDs.
	guid := "b0dd6ad2-1e4d-4b58-9c14-4dd8a1c0f74b"

	withKB := wuaUpdate{UpdateID: guid, KBArticleIDs: []string{"5034123"}}
	if got := windowsIdentifier(withKB); got != "KB5034123" {
		t.Errorf("windowsIdentifier with a KB article = %q, want KB5034123", got)
	}

	noKB := wuaUpdate{UpdateID: guid}
	if got := windowsIdentifier(noKB); got != guid {
		t.Errorf("windowsIdentifier with no KB article = %q, want the UpdateID %q; definition "+
			"updates and some driver packages have no KB, and an update with no identifier "+
			"cannot be approved, dispatched or reconciled at all", got, guid)
	}

	// Order-independence. WUA returns the array in whatever order the metadata
	// had, and this is the single easiest way for the identifier to become
	// unstable across scans.
	a := windowsIdentifier(wuaUpdate{KBArticleIDs: []string{"5034123", "5030219"}})
	b := windowsIdentifier(wuaUpdate{KBArticleIDs: []string{"5030219", "5034123"}})
	if a != b {
		t.Errorf("the same update enumerated in two orders got identifiers %q and %q; the catalog "+
			"is keyed on this and would grow a second row", a, b)
	}
	if a != "KB5030219" {
		t.Errorf("the chosen identifier is %q; the rule is the LOWEST KB article so the value is a "+
			"function of the update rather than of enumeration order", a)
	}

	// WUA reports bare digits; an already-prefixed value must not get a second
	// prefix.
	if got := windowsIdentifier(wuaUpdate{KBArticleIDs: []string{"KB5034123"}}); got != "KB5034123" {
		t.Errorf("an already-prefixed KB became %q", got)
	}
	if got := windowsIdentifier(wuaUpdate{}); got != "" {
		t.Errorf("an update with neither a KB nor a GUID produced identifier %q", got)
	}
}

func TestMsrcSeverityMapsOntoTheServersLiteralAndNeverPassesThrough(t *testing.T) {
	// PatchEntry.severity is a Literal and the ValidationError it raises is on
	// the WHOLE document, so ONE unrecognised severity costs the device its
	// entire scan. That is why this is a switch rather than strings.ToLower --
	// the two vocabularies happen to share four words, which is exactly the
	// coincidence that gets a mapping written as a passthrough and then breaks
	// silently when Microsoft returns a fifth.
	//
	// FALSIFYING OBSERVATION: any output outside the server's Literal.
	for _, tc := range []struct{ in, want string }{
		{"Critical", "critical"},
		{"Important", "important"},
		{"Moderate", "moderate"},
		{"Low", "low"},
		{"critical", "critical"},
		{" Important ", "important"},
		// UNRATED is `unknown` and NOT `low`. Most Windows 11 cumulative
		// updates carry no MSRC severity at all despite containing security
		// fixes, and calling them `low` would tell the compliance report
		// something Microsoft did not say.
		{"", "unknown"},
		{"Severe", "unknown"},
		{"None", "unknown"},
	} {
		got := windowsSeverity(tc.in)
		if got != tc.want {
			t.Errorf("windowsSeverity(%q) = %q, want %q", tc.in, got, tc.want)
		}
		if !severities[got] {
			t.Fatalf("windowsSeverity(%q) produced %q, which the server's Literal rejects; the "+
				"whole document would fail", tc.in, got)
		}
	}
}

func TestPartitionSeparatesOutstandingFromInstalledAndFiltersWhatCannotBeApproved(t *testing.T) {
	// ONE search, partitioned here. The filters are the point: a hidden update
	// re-surfaced in a console that can approve it fleet-wide would override a
	// local administrator's decision from somewhere they cannot see, and a
	// BrowseOnly driver is not what a patch ring means.
	//
	// FALSIFYING OBSERVATION: a hidden or browse-only update in `available`, or
	// an installed one missing from `installed`.
	updates := []wuaUpdate{
		{KBArticleIDs: []string{"5034123"}, Title: "2026-08 Cumulative Update", MsrcSeverity: "Critical"},
		{KBArticleIDs: []string{"5030219"}, Title: "Servicing Stack Update", IsInstalled: true},
		{KBArticleIDs: []string{"5011111"}, Title: "Hidden by the admin", IsHidden: true},
		{KBArticleIDs: []string{"5022222"}, Title: "Intel display driver", BrowseOnly: true},
		{UpdateID: "9f1b-guid", Title: "Defender definitions"},
		// A duplicate of the first, which WUA does return when the same KB is
		// offered by two channels.
		{KBArticleIDs: []string{"5034123"}, Title: "2026-08 Cumulative Update (dup)"},
	}

	available, installed := partitionWindowsUpdates(updates)

	gotAvailable := identifiers(available)
	wantAvailable := []string{"KB5034123", "9f1b-guid"}
	if !reflect.DeepEqual(gotAvailable, wantAvailable) {
		t.Fatalf("available = %v, want %v", gotAvailable, wantAvailable)
	}
	wantInstalled := []string{"windows_update:KB5030219"}
	if !reflect.DeepEqual(installed, wantInstalled) {
		t.Fatalf("installed = %v, want %v", installed, wantInstalled)
	}

	// The filters must actually differ from the unfiltered answer, or this
	// fixture proves nothing. Six updates in, two out.
	if len(available)+len(installed) == len(updates) {
		t.Fatal("every update survived; the hidden, browse-only and duplicate filters are " +
			"indistinguishable from no filters on this fixture")
	}

	// An INSTALLED update is not filtered by hidden/browse-only: reconciliation
	// is about what is present, and a hidden-but-installed update is present.
	hiddenInstalled := []wuaUpdate{
		{KBArticleIDs: []string{"5099999"}, IsInstalled: true, IsHidden: true},
	}
	_, inst := partitionWindowsUpdates(hiddenInstalled)
	if len(inst) != 1 {
		t.Errorf("a hidden-but-installed update was dropped from `installed` (%v); it is still "+
			"present on the machine", inst)
	}
}

func TestAnUpdateWithNoIdentifierIsDroppedRatherThanEmittedWithAnEmptyKey(t *testing.T) {
	// PatchEntry.identifier is required, and `patches.identifier` is part of
	// the catalog's unique constraint. An entry with an empty one would occupy
	// a row nothing can ever reference, dispatch to or reconcile.
	available, installed := partitionWindowsUpdates([]wuaUpdate{{Title: "no id at all"}})
	if len(available) != 0 || len(installed) != 0 {
		t.Fatalf("an update with neither a KB nor a GUID produced %v / %v", available, installed)
	}
}

func TestTheEntryCarriesTheKBAndTheCVEsAndNeverGuessesASupersede(t *testing.T) {
	// FALSIFYING OBSERVATION: a non-nil superseded_by, or CVEs in metadata
	// order rather than a stable one.
	e, ok := windowsEntry(wuaUpdate{
		UpdateID:     "guid",
		Title:        "2026-08 Cumulative Update for Windows 11",
		KBArticleIDs: []string{"5034123"},
		MsrcSeverity: "Critical",
		CveIDs:       []string{"cve-2026-1002", "CVE-2026-1001", "CVE-2026-1001", " "},
	})
	if !ok {
		t.Fatal("windowsEntry rejected a well-formed update")
	}
	if e.Source != sourceWindowsUpdate || e.Identifier != "KB5034123" || e.Severity != "critical" {
		t.Fatalf("got %+v", e)
	}
	if e.KB == nil || *e.KB != "KB5034123" {
		t.Fatalf("kb = %v", e.KB)
	}
	want := []string{"CVE-2026-1001", "CVE-2026-1002"}
	if !reflect.DeepEqual(e.CVE, want) {
		t.Errorf("cve = %v, want %v (upper-cased, de-duplicated, sorted, blanks dropped -- so two "+
			"scans of an unchanged machine produce byte-identical documents)", e.CVE, want)
	}
	if e.SupersededBy != nil {
		t.Errorf("superseded_by = %v; WUA exposes only what an update REPLACES, never what "+
			"replaces it, so any value here is a guess that could point a catalog row at the "+
			"wrong patch", *e.SupersededBy)
	}
}

func TestAnUpdateWithNoCVEsSaysNilAndNotEmptyList(t *testing.T) {
	// `cve` is `list[str] | None`. An empty list CLAIMS "this update has no
	// CVEs"; nil correctly says "this source did not report any". The
	// difference shows up in the compliance report.
	e, _ := windowsEntry(wuaUpdate{KBArticleIDs: []string{"5000000"}})
	if e.CVE != nil {
		t.Errorf("cve = %v, want nil", e.CVE)
	}
}

func identifiers(entries []entry) []string {
	out := make([]string, 0, len(entries))
	for _, e := range entries {
		out = append(out, e.Identifier)
	}
	return out
}
