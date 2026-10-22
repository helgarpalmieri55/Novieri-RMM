package inventory

import (
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode"
)

// Installed-software normalisation, shared by every platform's collector.
//
// The server keys installed_software on (device_id, name) and diffs by name,
// so the list is deduplicated by exact name here. It is also sorted, so that
// when a host has more than maxSoftware entries the ones dropped are the same
// on every sweep rather than whichever the OS enumerated last.

const (
	// installed_software.version is VARCHAR(100) and publisher VARCHAR(200).
	// The wire schema does not bound them, so an over-long value would pass
	// validation and then fail the ingest's INSERT, losing the whole sweep.
	maxSoftwareVersion   = 100
	maxSoftwarePublisher = 200
	// maxSoftwareScan bounds what a collector gathers before deduplication,
	// so a pathological host costs a bounded slice.
	maxSoftwareScan = 2 * maxSoftware
)

// Fact keys this package emits in InventoryDocument.facts. domain_joined and
// disk_encryption are two of the server's TRACKED_FACTS (ingest.py), so a
// change in either is recorded as a fact_changed event.
const (
	factLastBoot          = "last_boot"    // RFC 3339, UTC
	factConsoleUser       = "console_user" // the user at the physical console
	factManufacturer      = "manufacturer"
	factModel             = "model"
	factDomainJoined      = "domain_joined"   // Windows: Active Directory
	factEntraJoined       = "entra_joined"    // Windows: Entra ID (incl. hybrid)
	factDiskEncryption    = "disk_encryption" // BitLocker on the system drive / FileVault
	factSoftwareSources   = "software_sources"
	factSoftwareTruncated = "software_truncated"
)

// normalizeSoftware cleans, deduplicates, sorts and caps a collector's list.
// truncated reports that entries were dropped at the cap.
func normalizeSoftware(in []Software) (out []Software, truncated bool) {
	byName := make(map[string]int, len(in))
	out = make([]Software, 0, len(in))
	for _, s := range in {
		s.Name = clamp(cleanText(s.Name), maxName)
		if s.Name == "" {
			continue // minLength 1
		}
		s.Version = clamp(cleanText(s.Version), maxSoftwareVersion)
		s.Publisher = clamp(cleanText(s.Publisher), maxSoftwarePublisher)
		if s.InstallDate != nil {
			s.InstallDate = installDate(*s.InstallDate)
		}
		i, seen := byName[s.Name]
		if !seen {
			byName[s.Name] = len(out)
			out = append(out, s)
			continue
		}
		out[i] = mergeSoftware(out[i], s)
	}
	sort.Slice(out, func(i, j int) bool {
		a, b := strings.ToLower(out[i].Name), strings.ToLower(out[j].Name)
		if a != b {
			return a < b
		}
		return out[i].Name < out[j].Name
	})
	if len(out) > maxSoftware {
		return out[:maxSoftware], true
	}
	return out, false
}

// mergeSoftware keeps the higher version of two same-named entries and fills
// whatever it lacks from the other.
func mergeSoftware(a, b Software) Software {
	if compareLooseVersions(b.Version, a.Version) > 0 {
		a, b = b, a
	}
	if a.Publisher == "" {
		a.Publisher = b.Publisher
	}
	if a.InstallDate == nil {
		a.InstallDate = b.InstallDate
	}
	return a
}

// installDate returns an ISO date the server's pydantic `date` accepts, or nil.
// Accepts YYYYMMDD (the Windows Uninstall key's format) and YYYY-MM-DD.
func installDate(raw string) *string {
	raw = strings.TrimSpace(raw)
	var layout string
	switch len(raw) {
	case 8:
		layout = "20060102"
	case 10:
		layout = "2006-01-02"
	default:
		return nil
	}
	t, err := time.Parse(layout, raw)
	if err != nil || t.Year() < 1970 {
		return nil
	}
	iso := t.Format("2006-01-02")
	return &iso
}

// cleanText makes a string from the OS safe to store: valid UTF-8, no control
// characters (PostgreSQL text cannot hold NUL), trimmed.
func cleanText(s string) string {
	s = strings.ToValidUTF8(s, "")
	if strings.IndexFunc(s, unicode.IsControl) >= 0 {
		s = strings.Map(func(r rune) rune {
			if unicode.IsControl(r) {
				return ' '
			}
			return r
		}, s)
		s = strings.Join(strings.Fields(s), " ")
	}
	return strings.TrimSpace(s)
}

// compareLooseVersions orders version strings by their numeric and alphabetic
// runs: "1.10" > "1.9", "2.0" == "2.0.0". It only decides which of two
// duplicates to keep, so it need not be any package manager's exact rule.
func compareLooseVersions(a, b string) int {
	ta, tb := versionTokens(a), versionTokens(b)
	for i := 0; i < len(ta) || i < len(tb); i++ {
		var x, y string
		if i < len(ta) {
			x = ta[i]
		}
		if i < len(tb) {
			y = tb[i]
		}
		if c := compareVersionToken(x, y); c != 0 {
			return c
		}
	}
	return 0
}

func versionTokens(v string) []string {
	var out []string
	start := -1
	digit := false
	for i, r := range v {
		isDigit := r >= '0' && r <= '9'
		isAlpha := unicode.IsLetter(r)
		if start >= 0 && (!(isDigit || isAlpha) || isDigit != digit) {
			out = append(out, v[start:i])
			start = -1
		}
		if start < 0 && (isDigit || isAlpha) {
			start, digit = i, isDigit
		}
	}
	if start >= 0 {
		out = append(out, v[start:])
	}
	return out
}

func compareVersionToken(x, y string) int {
	xn, xerr := strconv.ParseUint(orZero(x), 10, 64)
	yn, yerr := strconv.ParseUint(orZero(y), 10, 64)
	switch {
	case xerr == nil && yerr == nil:
		switch {
		case xn > yn:
			return 1
		case xn < yn:
			return -1
		}
		return 0
	case xerr == nil && x != "":
		return 1 // a number outranks a letter run
	case yerr == nil && y != "":
		return -1
	}
	return strings.Compare(x, y)
}

// orZero lets a missing token compare as 0 against a number, so "2.0" and
// "2.0.0" are equal.
func orZero(s string) string {
	if s == "" {
		return "0"
	}
	return s
}

// documentFacts copies a gatherer's facts, dropping unknowns (nil or "") and
// clamping strings, so an unknown fact is absent rather than empty.
func documentFacts(in map[string]any) map[string]any {
	out := make(map[string]any, len(in)+1)
	for k, v := range in {
		switch value := v.(type) {
		case nil:
			continue
		case string:
			value = clamp(cleanText(value), maxName)
			if value == "" {
				continue
			}
			out[k] = value
		case []string:
			if len(value) == 0 {
				continue
			}
			out[k] = value
		default:
			out[k] = v
		}
	}
	return out
}
