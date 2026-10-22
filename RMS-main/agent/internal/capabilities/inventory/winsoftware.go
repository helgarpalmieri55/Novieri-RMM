package inventory

import (
	"encoding/json"
	"regexp"
	"strings"
)

// Windows installed-software PARSERS, in a file with no build tag so they are
// tested on Linux (see winfacts.go for why the name avoids `_windows.go`).
// facts_windows.go reads the registry and runs the Appx script; everything
// that decides what counts as installed software is here.

// uninstallEntry is one subkey of an Uninstall key, with the values the
// Programs and Features list is built from.
type uninstallEntry struct {
	DisplayName     string
	DisplayVersion  string
	Publisher       string
	InstallDate     string
	ParentKeyName   string
	ReleaseType     string
	SystemComponent bool
}

// softwareFromUninstall applies the rules Programs and Features uses: no
// DisplayName or SystemComponent=1 means hidden, and an entry with a parent
// or an update release type is a patch to something else, not a product.
func softwareFromUninstall(entries []uninstallEntry) []Software {
	out := make([]Software, 0, len(entries))
	for _, e := range entries {
		if strings.TrimSpace(e.DisplayName) == "" || e.SystemComponent || isUpdateEntry(e) {
			continue
		}
		out = append(out, Software{
			Name:        e.DisplayName,
			Version:     e.DisplayVersion,
			Publisher:   e.Publisher,
			InstallDate: installDate(e.InstallDate),
		})
	}
	return out
}

func isUpdateEntry(e uninstallEntry) bool {
	if strings.TrimSpace(e.ParentKeyName) != "" {
		return true
	}
	rt := strings.ToLower(e.ReleaseType)
	return strings.Contains(rt, "update") || strings.Contains(rt, "hotfix") ||
		strings.Contains(rt, "service pack")
}

// isUserHive reports whether an HKEY_USERS subkey is a loaded user profile:
// a domain/local account (S-1-5-21-...) or an Entra ID account (S-1-12-1-...),
// and not its _Classes companion. Service accounts are skipped.
func isUserHive(name string) bool {
	if strings.HasSuffix(strings.ToLower(name), "_classes") {
		return false
	}
	return strings.HasPrefix(name, "S-1-5-21-") || strings.HasPrefix(name, "S-1-12-1-")
}

// windowsAppxScript lists Store/MSIX packages for every user in ONE
// PowerShell process. Frameworks, resource packages and inbox (System-signed)
// packages are left out: they are parts of Windows, not installed software.
// -InputObject keeps the output an array even with a single package.
const windowsAppxScript = `$ErrorActionPreference='SilentlyContinue';` +
	`ConvertTo-Json -Compress -InputObject @(Get-AppxPackage -AllUsers | ` +
	`Where-Object { -not $_.IsFramework -and -not $_.IsResourcePackage -and $_.SignatureKind -ne 'System' } | ` +
	`ForEach-Object { [pscustomobject]@{n=$_.Name;v=[string]$_.Version;p=$_.Publisher} })`

type appxPackage struct {
	Name      string `json:"n"`
	Version   string `json:"v"`
	Publisher string `json:"p"`
}

// parseAppx reads windowsAppxScript's output. Anything unparseable yields
// nothing: the registry list stands on its own.
func parseAppx(raw string) []Software {
	raw = strings.TrimSpace(raw)
	var packages []appxPackage
	if strings.HasPrefix(raw, "{") {
		var one appxPackage
		if json.Unmarshal([]byte(raw), &one) != nil {
			return nil
		}
		packages = []appxPackage{one}
	} else if json.Unmarshal([]byte(raw), &packages) != nil {
		return nil
	}
	out := make([]Software, 0, len(packages))
	for _, p := range packages {
		if strings.TrimSpace(p.Name) == "" {
			continue
		}
		out = append(out, Software{Name: p.Name, Version: p.Version, Publisher: appxPublisher(p.Publisher)})
	}
	return out
}

var guidLike = regexp.MustCompile(`^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$`)

// appxPublisher turns a package publisher DN into an organisation name: O=
// when present, else CN= unless it is the GUID the Store assigns.
func appxPublisher(dn string) string {
	attrs := parseDN(dn)
	if o := attrs["O"]; o != "" {
		return o
	}
	if cn := attrs["CN"]; cn != "" && !guidLike.MatchString(cn) {
		return cn
	}
	return ""
}

// parseDN splits `CN=a, O="b, c", C=US` into attributes, honouring quotes.
func parseDN(dn string) map[string]string {
	out := map[string]string{}
	var parts []string
	var cur strings.Builder
	quoted := false
	for _, r := range dn {
		switch {
		case r == '"':
			quoted = !quoted
		case r == ',' && !quoted:
			parts = append(parts, cur.String())
			cur.Reset()
		default:
			cur.WriteRune(r)
		}
	}
	parts = append(parts, cur.String())
	for _, p := range parts {
		key, value, ok := strings.Cut(p, "=")
		if !ok {
			continue
		}
		key = strings.ToUpper(strings.TrimSpace(key))
		if _, dup := out[key]; !dup {
			out[key] = strings.TrimSpace(value)
		}
	}
	return out
}
