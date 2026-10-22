package install

import "strings"

// codeLength is the install link code's length (backend CODE_LENGTH).
const codeLength = 32

// maxHostLength is a DNS name's limit, in its dotted text form.
const maxHostLength = 253

// maxLabelLength is one DNS label's limit.
const maxLabelLength = 63

// Link is what an installer's file name carries: the console to enroll with
// and the link code to enroll through.
type Link struct {
	// Console is `https://<host>[:<port>]`. The enrollment that follows
	// verifies it against the system trust store like any other https URL;
	// nothing here or downstream skips verification.
	Console string
	// Code is the install link code. It goes to the token file, never to argv
	// or to the service's environment.
	Code string
}

// FromFileName returns the console and the link code in an installer's file
// name, or false -- and false means NOT installer mode: the binary starts
// normally.
//
// The operator's decision of 2026-09-28 (it replaces plan D6, the Mark of the
// Web): the console comes from the file name itself, so a copy made from a
// share or a USB stick installs exactly as a fresh download does. The name is
//
//	ITMS-Agent-Setup_<host>[@<port>]_<code><suffix>.exe
//
// read in this order: the base name; `.exe` stripped in any case; one browser
// suffix stripped -- ` (n)` (Chrome, Edge) or `(n)` (Firefox), what a browser
// appends to a second download of the same name; FilePrefix required; the code
// is the LAST 32 characters and the character before them must be `_`. It is
// read from the RIGHT because the code's own URL-safe alphabet contains `_`
// and `-`. What remains is the host, with an optional `@<port>`.
//
// The host must be a DNS hostname -- labels of [A-Za-z0-9-] that neither start
// nor end with `-`, joined by dots, at most 253 characters, no underscore and
// never an IP literal. The port, when present, is 1-65535.
func FromFileName(path string) (Link, bool) {
	base := path
	if i := strings.LastIndexAny(base, `/\`); i >= 0 {
		base = base[i+1:]
	}
	if len(base) < 4 || !strings.EqualFold(base[len(base)-4:], ".exe") {
		return Link{}, false
	}
	stem := stripBrowserSuffix(base[:len(base)-4])
	if !strings.HasPrefix(stem, FilePrefix) {
		return Link{}, false
	}
	rest := stem[len(FilePrefix):]
	if len(rest) < codeLength+2 { // at least a one-character host and the `_`
		return Link{}, false
	}
	code := rest[len(rest)-codeLength:]
	if !validCode(code) || rest[len(rest)-codeLength-1] != '_' {
		return Link{}, false
	}
	authority := rest[:len(rest)-codeLength-1]
	host, port, hasPort := strings.Cut(authority, "@")
	if !validHost(host) {
		return Link{}, false
	}
	console := "https://" + host
	if hasPort {
		if !validPort(port) {
			return Link{}, false
		}
		console += ":" + port
	}
	return Link{Console: console, Code: code}, true
}

// stripBrowserSuffix removes one trailing ` (n)` or `(n)`, n of 1-4 digits.
// A stem without one is returned unchanged.
func stripBrowserSuffix(stem string) string {
	if !strings.HasSuffix(stem, ")") {
		return stem
	}
	open := strings.LastIndexByte(stem, '(')
	if open < 0 {
		return stem
	}
	digits := stem[open+1 : len(stem)-1]
	if len(digits) < 1 || len(digits) > 4 || !allDigits(digits) {
		return stem
	}
	return strings.TrimSuffix(stem[:open], " ")
}

func validCode(s string) bool {
	for _, c := range s {
		switch {
		case c >= 'A' && c <= 'Z', c >= 'a' && c <= 'z', c >= '0' && c <= '9', c == '-', c == '_':
		default:
			return false
		}
	}
	return true
}

// validHost: a DNS hostname and never an IP literal. An IPv6 literal cannot
// get this far (':' and '[' are not in the label alphabet); an IPv4 literal --
// or a shortened form a resolver would still read as one, such as `10.1.2` --
// is refused by its all-numeric last label, which no DNS name has.
func validHost(host string) bool {
	if host == "" || len(host) > maxHostLength {
		return false
	}
	labels := strings.Split(host, ".")
	for _, label := range labels {
		if len(label) == 0 || len(label) > maxLabelLength || label[0] == '-' || label[len(label)-1] == '-' {
			return false
		}
		for _, c := range label {
			switch {
			case c >= 'A' && c <= 'Z', c >= 'a' && c <= 'z', c >= '0' && c <= '9', c == '-':
			default:
				return false
			}
		}
	}
	return !allDigits(labels[len(labels)-1])
}

// validPort: 1-65535, digits only, no leading zero.
func validPort(s string) bool {
	if len(s) == 0 || len(s) > 5 || s[0] == '0' || !allDigits(s) {
		return false
	}
	n := 0
	for _, c := range s {
		n = n*10 + int(c-'0')
	}
	return n >= 1 && n <= 65535
}

func allDigits(s string) bool {
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}
