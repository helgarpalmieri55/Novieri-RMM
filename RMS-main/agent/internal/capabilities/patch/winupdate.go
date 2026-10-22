package patch

import (
	"sort"
	"strings"
)

// The Windows Update mapping, on the testable side of the seam.
//
// This file has NO build tag. Everything about Windows patching that is a
// DECISION rather than a syscall lives here -- which update becomes a catalog
// row, what its stable identifier is, how Microsoft's severity vocabulary maps
// onto the server's, what is filtered out and why -- and all of it is exercised
// on Linux in win_test.go against `wuaUpdate` values.
//
// What is NOT here, and has never executed anywhere, is com_windows.go and
// wua_windows.go: the COM apartment, the IDispatch calls and the download/
// install loop. agent-capabilities.md §6 says so plainly.

const sourceWindowsUpdate = "windows_update"

// wuaUpdate is one IUpdate, flattened to the fields this capability uses.
//
// It is a PLAIN STRUCT with no COM in it, which is the point: the Windows
// engine's job is to fill this in, and every decision taken about it afterwards
// is ordinary Go that runs in CI.
type wuaUpdate struct {
	// UpdateID is the IUpdateIdentity.UpdateID GUID. Always present.
	UpdateID string
	Title    string
	// KBArticleIDs are IUpdate.KBArticleIDs, WITHOUT the "KB" prefix -- that is
	// how WUA reports them ("5034123", not "KB5034123").
	KBArticleIDs []string
	// MsrcSeverity is IUpdate.MsrcSeverity: "Critical", "Important",
	// "Moderate", "Low", or empty for an update Microsoft did not rate.
	MsrcSeverity string
	// CveIDs is IUpdate.CveIDs, present since Windows 7 and empty for most
	// non-security updates.
	CveIDs []string
	// IsInstalled partitions the single search this engine runs.
	IsInstalled bool
	// IsHidden marks an update an administrator hid on this machine. Hidden
	// updates are excluded by the search criteria, but the flag is carried so
	// the filter is expressible here rather than only in the criteria string.
	IsHidden bool
	// BrowseOnly marks an update WUA will not install without explicit intent
	// (drivers and optional previews are the usual case).
	BrowseOnly bool
	// RebootRequired is IInstallationResult.RebootRequired for an update this
	// job installed, or IUpdate.RebootRequired for one it merely listed.
	RebootRequired bool
}

// windowsIdentifier is the stable per-update key the catalog is keyed on.
//
// KB ARTICLE FIRST, UpdateID second, and the order matters more than it looks.
// spec §4 names "KB id" as the Windows identifier, and app/modules/patch's
// tests use "KB1"; a KB article is also the thing an operator recognises, the
// thing Microsoft's advisories are keyed on and the thing that stays the same
// when the same fix is re-released for a new build.
//
// The UpdateID fallback exists because not every update has a KB article --
// definition updates and some driver packages have none -- and an update with
// no identifier cannot be approved, dispatched or reconciled at all. A GUID is
// an unfriendly identifier and a real one; an empty one is neither.
//
// The LOWEST KB article is chosen when an update carries several, so the
// identifier is a function of the update rather than of the order WUA happened
// to enumerate its array in. An identifier that changed between two scans of
// the same machine would produce a second catalog row and a device that looks
// permanently unpatched.
func windowsIdentifier(u wuaUpdate) string {
	if kb := lowestKB(u.KBArticleIDs); kb != "" {
		return kb
	}
	return strings.TrimSpace(u.UpdateID)
}

// lowestKB normalises and picks. WUA reports bare digits; the "KB" prefix is
// added here so the identifier reads the way the rest of the world writes it,
// and a value that ALREADY carries the prefix is not given a second one.
func lowestKB(ids []string) string {
	normalised := make([]string, 0, len(ids))
	for _, id := range ids {
		id = strings.TrimSpace(id)
		if id == "" {
			continue
		}
		if !strings.HasPrefix(strings.ToUpper(id), "KB") {
			id = "KB" + id
		} else {
			id = "KB" + strings.TrimSpace(id[2:])
		}
		if id == "KB" {
			continue
		}
		normalised = append(normalised, id)
	}
	if len(normalised) == 0 {
		return ""
	}
	sort.Strings(normalised)
	return normalised[0]
}

// windowsSeverity maps MSRC's vocabulary onto PatchEntry.severity's.
//
// The two vocabularies happen to share four words, which is exactly the sort of
// coincidence that gets a mapping written as `strings.ToLower` and then breaks
// silently when Microsoft returns something else. An unrecognised value becomes
// `unknown` rather than being passed through, because PatchEntry.severity is a
// Literal and `extra="forbid"`: one unrecognised value would raise a
// ValidationError on the WHOLE document and cost the device its entire scan.
//
// An UNRATED update (empty MsrcSeverity) is `unknown` and NOT `low`. Most
// cumulative updates for Windows 11 carry no MSRC severity at all despite
// containing security fixes, and calling them `low` would tell the compliance
// report something Microsoft did not say.
func windowsSeverity(msrc string) string {
	switch strings.ToLower(strings.TrimSpace(msrc)) {
	case "critical":
		return "critical"
	case "important":
		return "important"
	case "moderate":
		return "moderate"
	case "low":
		return "low"
	default:
		return "unknown"
	}
}

// windowsEntry converts one update into a catalog entry.
func windowsEntry(u wuaUpdate) (entry, bool) {
	id := windowsIdentifier(u)
	if id == "" {
		return entry{}, false
	}
	e := entry{
		Source:     sourceWindowsUpdate,
		Identifier: id,
		Title:      strings.TrimSpace(u.Title),
		Severity:   windowsSeverity(u.MsrcSeverity),
	}
	if kb := lowestKB(u.KBArticleIDs); kb != "" {
		e.KB = &kb
	}
	if cves := normalizeCVEs(u.CveIDs); len(cves) > 0 {
		e.CVE = cves
	}
	// `superseded_by` stays nil. WUA exposes IUpdate.SupersededUpdateIDs, which
	// is the set this update REPLACES -- the opposite direction -- and no
	// property for what replaces it. Deriving the reverse edge would need the
	// whole catalog in memory and would still be wrong for an update whose
	// successor this machine is not offered. ingest.py treats an unresolvable
	// `superseded_by` as absent anyway, so guessing buys nothing and could
	// point a catalog row at the wrong patch.
	return e, true
}

// normalizeCVEs upper-cases, de-duplicates and sorts. WUA returns them in
// whatever order the metadata had; a stable order means two scans of an
// unchanged machine produce byte-identical documents.
func normalizeCVEs(ids []string) []string {
	seen := map[string]bool{}
	out := make([]string, 0, len(ids))
	for _, id := range ids {
		id = strings.ToUpper(strings.TrimSpace(id))
		if id == "" || seen[id] {
			continue
		}
		seen[id] = true
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}

// partitionWindowsUpdates splits one search's results into what is outstanding
// and what is already present.
//
// ONE SEARCH, PARTITIONED HERE, and that is a deliberate cost decision. A WUA
// search is the expensive part of a scan -- it can take minutes on a machine
// that has not been patched in months -- so running `IsInstalled=0` and
// `IsInstalled=1` as two searches doubles the most expensive thing the
// capability does. `IsInstalled=0 or IsInstalled=1` is one pass, and the
// IsInstalled flag on each result is what separates them.
//
// The filters, and why each is a filter rather than a catalog row:
//
//   - HIDDEN: an administrator hid it on this machine. Re-surfacing it in a
//     console that can approve it fleet-wide would override a local decision
//     from somewhere the local administrator cannot see.
//   - BROWSEONLY: drivers and optional previews. These install through a
//     different consent path in Windows and are not what a patch ring means.
//
// An installed update is NOT filtered by either: it is reported as installed
// regardless, because reconciliation is about what is present and a
// hidden-but-installed update is still present.
//
// SUPERSESSION is deliberately absent from this list. IUpdate exposes
// `SupersededUpdateIDs` -- what this update replaces -- and no property for
// what replaces IT, so there is nothing here to filter on. WUA's own search
// already omits superseded updates from an `IsInstalled=0` result, which is
// where that exclusion actually happens. A `IsSuperseded` field on wuaUpdate
// that the COM layer could never set would be a guard whose two branches always
// produce the same value: coverage-shaped and worthless.
func partitionWindowsUpdates(updates []wuaUpdate) (available []entry, installed []string) {
	available = make([]entry, 0, len(updates))
	installed = make([]string, 0)
	seenAvailable := map[string]bool{}
	seenInstalled := map[string]bool{}

	for _, u := range updates {
		e, ok := windowsEntry(u)
		if !ok {
			continue
		}
		if u.IsInstalled {
			if !seenInstalled[e.key()] {
				seenInstalled[e.key()] = true
				installed = append(installed, e.key())
			}
			continue
		}
		if u.IsHidden || u.BrowseOnly {
			continue
		}
		if seenAvailable[e.key()] {
			continue
		}
		seenAvailable[e.key()] = true
		available = append(available, e)
	}
	return available, installed
}
