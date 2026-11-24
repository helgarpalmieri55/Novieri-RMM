package install

import (
	"strings"
	"testing"
)

// The operator's decision of 2026-09-28 (it replaces plan D6): the console
// comes from the installer's own FILE NAME, `NovieriRMM-Agent-Setup_<host>[@<port>]_<code>.exe`,
// so a copy from a share or a USB stick installs exactly as a fresh download does.

const code = "e2eLINKcode_0123456789-ABCDEFGHI" // 32 characters of [A-Za-z0-9_-], with both '_' and '-'

const host = "rmm.novieri.com"

func TestTheLinkComesFromTheFileNameAsTheLinkServesItAndAsBrowsersRenameIt(t *testing.T) {
	for name, want := range map[string]Link{
		`C:\Users\ana\Downloads\NovieriRMM-Agent-Setup_` + host + `_` + code + `.exe`:     {"https://rmm.novieri.com", code},
		`C:\Users\ana\Downloads\NovieriRMM-Agent-Setup_` + host + `_` + code + ` (1).exe`: {"https://rmm.novieri.com", code}, // Chrome, Edge
		`C:\Users\ana\Downloads\NovieriRMM-Agent-Setup_` + host + `_` + code + `(12).exe`: {"https://rmm.novieri.com", code}, // Firefox
		`NovieriRMM-Agent-Setup_` + host + `_` + code + `.EXE`:                            {"https://rmm.novieri.com", code},
		`D:/tmp/NovieriRMM-Agent-Setup_` + host + `_` + code + `.Exe`:                     {"https://rmm.novieri.com", code},
		`NovieriRMM-Agent-Setup_` + host + `@8443_` + code + `.exe`:                       {"https://rmm.novieri.com:8443", code},
		`NovieriRMM-Agent-Setup_` + host + `@1_` + code + ` (3).exe`:                      {"https://rmm.novieri.com:1", code},
		`NovieriRMM-Agent-Setup_` + host + `@65535_` + code + `.exe`:                      {"https://rmm.novieri.com:65535", code},
		`NovieriRMM-Agent-Setup_localhost@50123_` + code + `.exe`:                         {"https://localhost:50123", code},
		`NovieriRMM-Agent-Setup_it-ms.Example.co.uk_` + code + `.exe`:                     {"https://it-ms.Example.co.uk", code},
		// The code's own alphabet holds '_' and '-', so it is read from the
		// RIGHT: a code that starts with '_' still parses.
		`NovieriRMM-Agent-Setup_` + host + `__` + code[1:] + `.exe`: {"https://rmm.novieri.com", "_" + code[1:]},
		`NovieriRMM-Agent-Setup_` + host + `_-` + code[1:] + `.exe`: {"https://rmm.novieri.com", "-" + code[1:]},
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
	if _, ok := FromFileName(`NovieriRMM-Agent-Setup_` + longest + `_` + code + `.exe`); !ok {
		t.Fatalf("a 253-character host was refused")
	}
	for _, name := range []string{
		`C:\Program Files\NovieriRMM\novieri-agent.exe`, // the installed service
		`NovieriRMM-Agent-Setup_.exe`,
		// A missing code, a code of the wrong length, a code outside the alphabet.
		`NovieriRMM-Agent-Setup_` + host + `.exe`,
		`NovieriRMM-Agent-Setup_` + host + `_.exe`,
		`NovieriRMM-Agent-Setup_` + host + `_` + code[:31] + `.exe`,
		`NovieriRMM-Agent-Setup_` + host + `_` + code + `X.exe`, // 33: the 32 on the right are preceded by 'e', not '_'
		`NovieriRMM-Agent-Setup_` + host + `_` + code[:31] + `!.exe`,
		// The code alone, the pre-decision shape: no host, so no console.
		`NovieriRMM-Agent-Setup_` + code + `.exe`,
		// Bad hosts.
		`NovieriRMM-Agent-Setup_192.168.1.10_` + code + `.exe`, // an IP literal
		`NovieriRMM-Agent-Setup_10.1.2_` + code + `.exe`,       // an IP by any other name
		`NovieriRMM-Agent-Setup_itms_toeshee.com_` + code + `.exe`,
		`NovieriRMM-Agent-Setup_-rmm.novieri.com_` + code + `.exe`,
		`NovieriRMM-Agent-Setup_itms-.toeshee.com_` + code + `.exe`,
		`NovieriRMM-Agent-Setup_itms..toeshee.com_` + code + `.exe`,
		`NovieriRMM-Agent-Setup_.rmm.novieri.com_` + code + `.exe`,
		`NovieriRMM-Agent-Setup_rmm.novieri.com._` + code + `.exe`,
		`NovieriRMM-Agent-Setup_` + strings.Repeat("a", 64) + `.com_` + code + `.exe`, // a 64-character label
		`NovieriRMM-Agent-Setup_` + long + `_` + code + `.exe`,                        // 254 characters
		`NovieriRMM-Agent-Setup_user@rmm.novieri.com_` + code + `.exe`,
		`NovieriRMM-Agent-Setup_rmm.novieri.com:8443_` + code + `.exe`,
		// Bad ports.
		`NovieriRMM-Agent-Setup_` + host + `@0_` + code + `.exe`,
		`NovieriRMM-Agent-Setup_` + host + `@70000_` + code + `.exe`,
		`NovieriRMM-Agent-Setup_` + host + `@65536_` + code + `.exe`,
		`NovieriRMM-Agent-Setup_` + host + `@_` + code + `.exe`,
		`NovieriRMM-Agent-Setup_` + host + `@08443_` + code + `.exe`,
		`NovieriRMM-Agent-Setup_` + host + `@84a3_` + code + `.exe`,
		`NovieriRMM-Agent-Setup_` + host + `@1@2_` + code + `.exe`,
		// Not a browser's suffix, not an .exe, not the prefix.
		`NovieriRMM-Agent-Setup_` + host + `_` + code + ` (1)(2).exe`,
		`NovieriRMM-Agent-Setup_` + host + `_` + code + ` .exe`,
		`NovieriRMM-Agent-Setup_` + host + `_` + code + `.exe.crdownload`,
		`itms-agent-setup_` + host + `_` + code + `.exe`, // the link serves this exact prefix
	} {
		if got, ok := FromFileName(name); ok {
			t.Errorf("FromFileName(%q) = %+v; want normal start", name, got)
		}
	}
}
