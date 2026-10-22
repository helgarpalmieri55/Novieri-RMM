package inventory

import (
	"strconv"
	"strings"
	"time"
)

// The macOS fact PARSERS, in a file with no build tag (and NOT named
// parse_darwin.go -- a `_darwin.go` suffix is an implicit build constraint), for the reason given at
// the top of parse_windows.go: what stays unexercised on a linux container
// should be the exec call, not the interpretation of its output.

// parseIORegSerial pulls IOPlatformSerialNumber out of
// `ioreg -rd1 -c IOPlatformExpertDevice`, whose output looks like
//
//	"IOPlatformSerialNumber" = "C02XY1Z2ABCD"
//
// Returns "" when the key is absent, which is what a VM without a platform
// serial produces.
func parseIORegSerial(out string) string {
	const key = `"IOPlatformSerialNumber"`
	for _, line := range strings.Split(out, "\n") {
		_, value, ok := strings.Cut(line, key)
		if !ok {
			continue
		}
		_, value, ok = strings.Cut(value, "=")
		if !ok {
			continue
		}
		return strings.Trim(strings.TrimSpace(value), `"`)
	}
	return ""
}

// parseSWVers reads the `sw_vers` key/value output:
//
//	ProductName:		macOS
//	ProductVersion:		14.5
//	BuildVersion:		23F79
func parseSWVers(out string) (name, version, build string) {
	for _, line := range strings.Split(out, "\n") {
		key, value, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		value = strings.TrimSpace(value)
		switch strings.TrimSpace(key) {
		case "ProductName":
			name = value
		case "ProductVersion":
			version = value
		case "BuildVersion":
			build = value
		}
	}
	return name, version, build
}

// parseKernBoottime reads `sysctl -n kern.boottime`:
//
//	{ sec = 1726816323, usec = 483000 } Fri Sep 20 07:12:03 2024
//
// and returns the boot time as RFC 3339 UTC, or "".
func parseKernBoottime(out string) string {
	_, rest, ok := strings.Cut(out, "sec =")
	if !ok {
		return ""
	}
	digits, _, _ := strings.Cut(strings.TrimSpace(rest), ",")
	sec, err := strconv.ParseInt(strings.TrimSpace(digits), 10, 64)
	if err != nil || sec <= 0 {
		return ""
	}
	return time.Unix(sec, 0).UTC().Format(time.RFC3339)
}

// parseFDESetup reads `fdesetup status`: true for "FileVault is On.", false
// for "FileVault is Off..." (including "will be enabled after the next
// restart", which is not yet protection), nil when it said neither.
func parseFDESetup(out string) any {
	first, _, _ := strings.Cut(strings.TrimSpace(out), "\n")
	switch {
	case strings.HasPrefix(first, "FileVault is On"):
		return true
	case strings.HasPrefix(first, "FileVault is Off"):
		return false
	}
	return nil
}

// macConsoleUser reads `stat -f %Su /dev/console`, the owner of the console.
// At the login window that is root, and during Setup Assistant
// _mbsetupuser; neither is a person, so both are reported as nobody.
func macConsoleUser(out string) string {
	user := strings.TrimSpace(out)
	if strings.ContainsAny(user, "\n\r") || user == "root" || user == "_mbsetupuser" {
		return ""
	}
	return user
}
