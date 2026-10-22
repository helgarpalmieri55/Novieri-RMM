package update

import (
	"bytes"
	"encoding/xml"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Service identities. The unit, label and service name are fixed by the
// shipped installers; the service definition is never changed (spec §5.1).
const (
	systemdUnit        = "its-agent"
	helperUnit         = "its-agent-update"
	launchdLabel       = "com.toeshee.itsagent"
	launchdHelperLabel = "com.toeshee.itsagent.update"
	windowsService     = "its-agent"
)

func parseSystemdShow(out string) (pid int, path string, err error) {
	for _, line := range strings.Split(out, "\n") {
		k, v, ok := strings.Cut(strings.TrimSpace(line), "=")
		if !ok {
			continue
		}
		switch k {
		case "MainPID":
			pid, _ = strconv.Atoi(v)
		case "ExecStart":
			if _, rest, ok := strings.Cut(v, "path="); ok {
				path, _, _ = strings.Cut(rest, " ;")
				path = strings.TrimSpace(path)
			}
		}
	}
	if path == "" {
		return 0, "", errors.New("the unit has no ExecStart path")
	}
	return pid, path, nil
}

func parseLaunchctlPrint(out string) (pid int, path string, err error) {
	var firstArg string
	inArgs := false
	for _, raw := range strings.Split(out, "\n") {
		line := strings.TrimSpace(raw)
		if inArgs {
			if line == "}" {
				inArgs = false
			} else if firstArg == "" && line != "" {
				firstArg = line
			}
			continue
		}
		k, v, ok := strings.Cut(line, " = ")
		if !ok {
			continue
		}
		switch k {
		case "pid":
			pid, _ = strconv.Atoi(v)
		case "program":
			path = v
		case "arguments":
			inArgs = strings.HasSuffix(v, "{")
		}
	}
	if path == "" {
		path = firstArg
	}
	if path == "" {
		return 0, "", errors.New("the job has no program")
	}
	return pid, path, nil
}

// parseWindowsImagePath extracts the executable from an SCM BinaryPathName.
func parseWindowsImagePath(s string) string {
	s = strings.TrimSpace(s)
	if strings.HasPrefix(s, `"`) {
		if end := strings.Index(s[1:], `"`); end >= 0 {
			return s[1 : end+1]
		}
		return strings.Trim(s, `"`)
	}
	if i := strings.Index(strings.ToLower(s), ".exe"); i >= 0 {
		return s[:i+4]
	}
	path, _, _ := strings.Cut(s, " ")
	return path
}

// sameFile compares two paths after resolving symlinks.
func sameFile(a, b string, foldCase bool) bool {
	ra, err1 := filepath.EvalSymlinks(a)
	rb, err2 := filepath.EvalSymlinks(b)
	if err1 != nil || err2 != nil {
		ra, rb = filepath.Clean(a), filepath.Clean(b)
	}
	if foldCase {
		return strings.EqualFold(ra, rb)
	}
	return ra == rb
}

// checkIdentity turns the service manager's view into spec §4.1 step 7.
func checkIdentity(managerPID int, configured string, foldCase bool) (string, error) {
	if managerPID != os.Getpid() {
		return "", fail(ReasonNotRunningAsService, errors.New("this process is not the service's main process"))
	}
	self, err := os.Executable()
	if err != nil {
		return "", fail(ReasonUnsupportedLayout, err)
	}
	if !sameFile(configured, self, foldCase) {
		return "", fail(ReasonUnsupportedLayout, errors.New("the service runs a different file than this process"))
	}
	return configured, nil
}

// helperPlist is the transient launchd job for the macOS helper. It lives in
// the data directory, not /Library/LaunchDaemons, so a reboot never
// relaunches it.
func helperPlist(prev, phase string, l Layout) []byte {
	var b bytes.Buffer
	esc := func(s string) string {
		var e bytes.Buffer
		_ = xml.EscapeText(&e, []byte(s))
		return e.String()
	}
	b.WriteString(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>` + launchdHelperLabel + `</string>
    <key>ProgramArguments</key>
    <array>
        <string>` + esc(prev) + `</string>
        <string>--update-helper=` + esc(phase) + `</string>
        <string>--state</string>
        <string>` + esc(l.StatePath()) + `</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ITS_AGENT_DATA_DIR</key>
        <string>` + esc(l.DataDir()) + `</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>LaunchOnlyOnce</key>
    <true/>
    <key>StandardErrorPath</key>
    <string>` + esc(filepath.Join(l.Dir, "helper.log")) + `</string>
</dict>
</plist>
`)
	return b.Bytes()
}
