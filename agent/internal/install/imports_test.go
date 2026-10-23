package install

import (
	"os/exec"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// IDLE BUDGET, MEASURED BY WHAT IS LINKED (review I6). The service binary
// links this package -- cmd/agent's installer branch names it -- so "the
// service never initialises it" is only true if linking it adds NOTHING the
// service does not already carry: no package whose init() the service would
// otherwise not run, no dependency pulled in for the installer's sake. The
// budget benchmarks do not measure this; they measure the running service.
// This test does: it pins this package's direct imports under GOOS=windows
// (the one OS where it does anything) to an explicit, reviewed list, and
// requires that its whole dependency closure is already in the closure of
// service packages that do not import it.
//
// Since 2026-09-29 it does something on Linux and macOS too -- the SSH push's
// install mode (pushunix.go, push_unix.go) -- so both are pinned the same way:
// wantUnixImports, and the closure check runs for all three systems.
//
// Adding an import here is a decision: add it to wantImports, and if it is not
// already linked by the service, justify it against CLAUDE.md's idle budget.

// wantImports are internal/install's direct imports under windows/amd64.
var wantImports = []string{
	"context",
	"crypto/ed25519",
	"crypto/sha256",
	"encoding/hex",
	"encoding/json",
	"errors",
	"fmt",
	"github.com/toeshee/it-suite/agent/internal/install/layout",
	"github.com/toeshee/it-suite/agent/internal/secdir",
	"github.com/toeshee/it-suite/agent/internal/update",
	"golang.org/x/sys/windows",
	"golang.org/x/sys/windows/registry",
	"golang.org/x/sys/windows/svc",
	"golang.org/x/sys/windows/svc/mgr",
	"io",
	"net/http",
	"os",
	"path/filepath",
	"runtime",
	"strings",
	"time",
}

// wantUnixImports are internal/install's direct imports under linux/amd64 and
// darwin/amd64: the Windows list's portable part, plus what the install mode's
// real system needs (os/exec for systemctl/launchctl, syscall for O_NOFOLLOW
// and the umask, path/filepath, runtime). All of them are already linked by
// the service on those systems (TestInstallLinksNothingTheServiceDoesNotAlreadyLink).
var wantUnixImports = []string{
	"context",
	"crypto/ed25519",
	"crypto/sha256",
	"encoding/hex",
	"encoding/json",
	"errors",
	"fmt",
	"github.com/toeshee/it-suite/agent/internal/install/layout",
	"github.com/toeshee/it-suite/agent/internal/update",
	"io",
	"net/http",
	"os",
	"os/exec",
	"path/filepath",
	"runtime",
	"strings",
	"syscall",
	"time",
}

// pinnedSystems are the systems whose imports and closure are pinned.
var pinnedSystems = map[string][]string{
	"windows": wantImports,
	"linux":   wantUnixImports,
	"darwin":  wantUnixImports,
}

const installPkg = "github.com/toeshee/it-suite/agent/internal/install"

// serviceRoots are cmd/agent's direct imports, internal/install removed: what
// the service links on its own account.
func serviceRoots(t *testing.T, goos string) []string {
	t.Helper()
	var roots []string
	for _, p := range goList(t, goos, `{{join .Imports " "}}`, "./cmd/agent") {
		if p != installPkg {
			roots = append(roots, p)
		}
	}
	if len(roots) < 10 {
		t.Fatalf("cmd/agent imports only %v; the comparison would prove nothing", roots)
	}
	return roots
}

func goList(t *testing.T, goos, format string, pkgs ...string) []string {
	t.Helper()
	gobin, err := exec.LookPath("go")
	if err != nil {
		t.Fatalf("the go tool is not on PATH, so what this package links cannot be measured: %v", err)
	}
	cmd := exec.Command(gobin, append([]string{"list", "-f", format}, pkgs...)...)
	cmd.Dir = "../.." // the module root
	cmd.Env = append(cmd.Environ(), "GOOS="+goos, "GOARCH=amd64", "CGO_ENABLED=0")
	out, err := cmd.Output()
	if err != nil {
		var stderr string
		if ee, ok := err.(*exec.ExitError); ok {
			stderr = string(ee.Stderr)
		}
		t.Fatalf("go list %v: %v\n%s", pkgs, err, stderr)
	}
	return strings.Fields(string(out))
}

func TestInstallImportsArePinned(t *testing.T) {
	for goos, want := range pinnedSystems {
		got := goList(t, goos, `{{join .Imports " "}}`, "./internal/install")
		sort.Strings(got)
		if !reflect.DeepEqual(got, want) {
			t.Errorf("internal/install imports under %s:\n got %v\nwant %v\n"+
				"update the pinned list deliberately (and the idle-budget reasoning above) if this is intended",
				goos, got, want)
		}
	}
}

func TestInstallLinksNothingTheServiceDoesNotAlreadyLink(t *testing.T) {
	for goos := range pinnedSystems {
		service := map[string]bool{}
		for _, p := range goList(t, goos, "{{.ImportPath}}", append([]string{"-deps"}, serviceRoots(t, goos)...)...) {
			service[p] = true
		}
		if service[installPkg] {
			t.Fatalf("%s: a service root imports internal/install; this comparison would be circular", goos)
		}
		var extra []string
		for _, p := range goList(t, goos, "{{.ImportPath}}", "-deps", "./internal/install") {
			if p != installPkg && !service[p] {
				extra = append(extra, p)
			}
		}
		if len(extra) > 0 {
			t.Errorf("%s: internal/install makes the service link %v, which nothing else in it needs; "+
				"their init() would run in every service process (CLAUDE.md idle budget)", goos, extra)
		}
	}
}
