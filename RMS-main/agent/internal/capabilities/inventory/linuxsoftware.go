package inventory

import (
	"bufio"
	"bytes"
	"errors"
	"io"
	"strconv"
	"strings"
	"time"
)

// Linux installed-software and fact PARSERS (no build tag; tested everywhere).
// facts_linux.go opens dpkg's status file and runs rpm with the query
// format parseRPMQuery expects.

// maxStatusLine is the longest dpkg status line kept. Longer lines are
// Description text or corruption; they are skipped without being buffered.
const maxStatusLine = 64 << 10

// parseDpkgStatus streams dpkg's status file and returns at most limit
// installed packages. Memory is bounded by maxStatusLine, not by the file.
func parseDpkgStatus(r io.Reader, limit int) []Software {
	var out []Software
	var pkg, status, version, maintainer string
	flush := func() {
		if pkg != "" && strings.HasSuffix(strings.TrimSpace(status), " installed") && len(out) < limit {
			out = append(out, Software{Name: pkg, Version: version, Publisher: maintainerName(maintainer)})
		}
		pkg, status, version, maintainer = "", "", "", ""
	}
	br := bufio.NewReaderSize(r, maxStatusLine)
	for len(out) < limit {
		line, err := br.ReadSlice('\n')
		if errors.Is(err, bufio.ErrBufferFull) {
			// Discard the rest of an over-long line.
			for errors.Is(err, bufio.ErrBufferFull) {
				_, err = br.ReadSlice('\n')
			}
			if err != nil {
				break
			}
			continue
		}
		line = bytes.TrimRight(line, "\r\n")
		switch {
		case len(line) == 0:
			flush()
		case line[0] == ' ' || line[0] == '\t':
			// continuation of a multi-line field
		default:
			key, value, ok := bytes.Cut(line, []byte(":"))
			if ok {
				v := string(bytes.TrimSpace(value))
				switch string(key) {
				case "Package":
					pkg = v
				case "Status":
					status = v
				case "Version":
					version = v
				case "Maintainer":
					maintainer = v
				}
			}
		}
		if err != nil {
			break
		}
	}
	flush()
	return out
}

// maintainerName drops the address from "Name <address>".
func maintainerName(m string) string {
	if i := strings.Index(m, "<"); i >= 0 {
		m = m[:i]
	}
	return strings.TrimSpace(m)
}

// parseRPMQuery reads rpmQueryFormat output. gpg-pubkey entries are the RPM
// database's imported signing keys, not packages.
func parseRPMQuery(out string, limit int) []Software {
	var sw []Software
	for _, line := range strings.Split(out, "\n") {
		if len(sw) >= limit {
			break
		}
		fields := strings.Split(line, "\t")
		if len(fields) != 5 || fields[0] == "" || fields[0] == "gpg-pubkey" {
			continue
		}
		s := Software{Name: fields[0], Version: fields[2], Publisher: noneToEmpty(fields[3])}
		if epoch := noneToEmpty(fields[1]); epoch != "" && epoch != "0" {
			s.Version = epoch + ":" + s.Version
		}
		if secs, err := strconv.ParseInt(strings.TrimSpace(fields[4]), 10, 64); err == nil && secs > 0 {
			d := time.Unix(secs, 0).UTC().Format("2006-01-02")
			s.InstallDate = &d
		}
		sw = append(sw, s)
	}
	return sw
}

func noneToEmpty(s string) string {
	s = strings.TrimSpace(s)
	if s == "(none)" {
		return ""
	}
	return s
}

// parseProcStatBtime returns /proc/stat's boot time as RFC 3339 UTC, or "".
func parseProcStatBtime(content string) string {
	for _, line := range strings.Split(content, "\n") {
		fields := strings.Fields(line)
		if len(fields) != 2 || fields[0] != "btime" {
			continue
		}
		secs, err := strconv.ParseInt(fields[1], 10, 64)
		if err != nil || secs <= 0 {
			return ""
		}
		return time.Unix(secs, 0).UTC().Format(time.RFC3339)
	}
	return ""
}

// dmiPlaceholders are the strings firmware ships when the OEM did not fill a
// field in. They name no manufacturer or model.
var dmiPlaceholders = map[string]bool{
	"to be filled by o.e.m.": true, "system manufacturer": true, "system product name": true,
	"default string": true, "not specified": true, "not applicable": true, "none": true,
	"o.e.m.": true, "oem": true, "unknown": true,
}

func dmiFact(raw string) string {
	v := strings.TrimSpace(raw)
	if dmiPlaceholders[strings.ToLower(v)] {
		return ""
	}
	return v
}
