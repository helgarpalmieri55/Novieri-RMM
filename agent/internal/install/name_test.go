package install

import (
	"strings"
	"testing"
)

// The operator's decision of 2026-09-28 (it replaces plan D6): the console
// comes from the installer's own FILE NAME, `ITMS-Agent-Setup_<host>[@<port>]_<code>.exe`,
// so a copy from a share or a USB stick installs exactly as a fresh download does.

const code = "e2eLINKcode_0123456789-ABCDEFGHI" // 32 characters of [A-Za-z0-9_-], with both '_' and '-'

const host = "itms.toeshee.com"

func TestTheLinkComesFromTheFileNameAsTheLinkServesItAndAsBrowsersRenameIt(t *testing.T) {
	for name, want := range map[string]Link{
		`C:\Users\ana\Downloads\ITMS-Agent-Setup_` + host + `_` + code + `.exe`:     {"https://itms.toeshee.com", code},
		`C:\Users\ana\Downloads\ITMS-Agent-Setup_` + host + `_` + code + ` (1).exe`: {"https://itms.toeshee.com", code}, // Chrome, Edge
		`C:\Users\ana\Downloads\ITMS-Agent-Setup_` + host + `_` + code + `(12).exe`: {"https://itms.toeshee.com", code}, // Firefox
		`ITMS-Agent-Setup_` + host + `_` + code + `.EXE`:                            {"https://itms.toeshee.com", code},
		`D:/tmp/ITMS-Agent-Setup_` + host + `_` + code + `.Exe`:                     {"https://itms.toeshee.com", code},
		`ITMS-Agent-Setup_` + host + `@8443_` + code + `.exe`:                       {"https://itms.toeshee.com:8443", code},
		`ITMS-Agent-Setup_` + host + `@1_` + code + ` (3).exe`:                      {"https://itms.toeshee.com:1", code},
		`ITMS-Agent-Setup_` + host + `@65535_` + code + `.exe`:                      {"https://itms.toeshee.com:65535", code},
		`ITMS-Agent-Setup_localhost@50123_` + code + `.exe`:                         {"https://localhost:50123", code},
		`ITMS-Agent-Setup_it-ms.Example.co.uk_` + code + `.exe`:                     {"https://it-ms.Example.co.uk", code},
		// The code's own alphabet holds '_' and '-', so it is read from the
		// RIGHT: a code that starts with '_' still parses.
		`ITMS-Agent-Setup_` + host + `__` + code[1:] + `.exe`: {"https://itms.toeshee.com", "_" + code[1:]},
		`ITMS-Agent-Setup_` + host + `_-` + code[1:] + `.exe`: {"https://itms.toeshee.com", "-" + code[1:]},
	} {
		got, ok := FromFileName(name)
		if !ok || got != want {
			t.Errorf("FromFileName(%q) = %+v, %v; want %+v (installer mode)", name, got, ok, want)
		}
	}
}

func TestAnythingElseIsNotAnInstallerAndStartsNormally(t *testing.T) {
	long := strings.Repeat("a", 63) + "." + strings.Repeat("b", 63) + "." + strings.Repeat("c", 63) + "." + strings.Repeat("d", 62)
	if len(long) != 254 {
		t.Fatalf("fixture is %d characters, want 254", len(long))
	}
	longest := long[:len(long)-1] // 253: the limit itself is accepted
	if _, ok := FromFileName(`ITMS-Agent-Setup_` + longest + `_` + code + `.exe`); !ok {
		t.Fatalf("a 253-character host was refused")
	}
	for _, name := range []string{
		`C:\Program Files\ITSuite\its-agent.exe`, // the installed service
		`ITMS-Agent-Setup_.exe`,
		// A missing code, a code of the wrong length, a code outside the alphabet.
		`ITMS-Agent-Setup_` + host + `.exe`,
		`ITMS-Agent-Setup_` + host + `_.exe`,
		`ITMS-Agent-Setup_` + host + `_` + code[:31] + `.exe`,
		`ITMS-Agent-Setup_` + host + `_` + code + `X.exe`, // 33: the 32 on the right are preceded by 'e', not '_'
		`ITMS-Agent-Setup_` + host + `_` + code[:31] + `!.exe`,
		// The code alone, the pre-decision shape: no host, so no console.
		`ITMS-Agent-Setup_` + code + `.exe`,
		// Bad hosts.
		`ITMS-Agent-Setup_192.168.1.10_` + code + `.exe`, // an IP literal
		`ITMS-Agent-Setup_10.1.2_` + code + `.exe`,       // an IP by any other name
		`ITMS-Agent-Setup_itms_toeshee.com_` + code + `.exe`,
		`ITMS-Agent-Setup_-itms.toeshee.com_` + code + `.exe`,
		`ITMS-Agent-Setup_itms-.toeshee.com_` + code + `.exe`,
		`ITMS-Agent-Setup_itms..toeshee.com_` + code + `.exe`,
		`ITMS-Agent-Setup_.itms.toeshee.com_` + code + `.exe`,
		`ITMS-Agent-Setup_itms.toeshee.com._` + code + `.exe`,
		`ITMS-Agent-Setup_` + strings.Repeat("a", 64) + `.com_` + code + `.exe`, // a 64-character label
		`ITMS-Agent-Setup_` + long + `_` + code + `.exe`,                        // 254 characters
		`ITMS-Agent-Setup_user@itms.toeshee.com_` + code + `.exe`,
		`ITMS-Agent-Setup_itms.toeshee.com:8443_` + code + `.exe`,
		// Bad ports.
		`ITMS-Agent-Setup_` + host + `@0_` + code + `.exe`,
		`ITMS-Agent-Setup_` + host + `@70000_` + code + `.exe`,
		`ITMS-Agent-Setup_` + host + `@65536_` + code + `.exe`,
		`ITMS-Agent-Setup_` + host + `@_` + code + `.exe`,
		`ITMS-Agent-Setup_` + host + `@08443_` + code + `.exe`,
		`ITMS-Agent-Setup_` + host + `@84a3_` + code + `.exe`,
		`ITMS-Agent-Setup_` + host + `@1@2_` + code + `.exe`,
		// Not a browser's suffix, not an .exe, not the prefix.
		`ITMS-Agent-Setup_` + host + `_` + code + ` (1)(2).exe`,
		`ITMS-Agent-Setup_` + host + `_` + code + ` .exe`,
		`ITMS-Agent-Setup_` + host + `_` + code + `.exe.crdownload`,
		`itms-agent-setup_` + host + `_` + code + `.exe`, // the link serves this exact prefix
	} {
		if got, ok := FromFileName(name); ok {
			t.Errorf("FromFileName(%q) = %+v; want normal start", name, got)
		}
	}
}
