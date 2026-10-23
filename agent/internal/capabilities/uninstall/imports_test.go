package uninstall

import (
	"os/exec"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// IDLE BUDGET, MEASURED BY WHAT IS LINKED (review I6). Registering this
// capability is a map insert and binding it stores options -- TestBindingStartsNothing
// measures that. What that test cannot see is what LINKING it costs: every
// package it imports is initialised in every service process. This pins its
// direct imports, per OS it ships on, to an explicit list, and requires that
// its dependency closure is already in the closure of the rest of the service
// (cmd/agent's other imports and every other capability). The budget
// benchmarks do not measure this; they measure the running service.

const (
	uninstallPkg = "github.com/toeshee/it-suite/agent/internal/capabilities/uninstall"
	registryPkg  = "github.com/toeshee/it-suite/agent/internal/capabilities"
)

var wantImports = map[string][]string{
	"linux": {
		"context", "encoding/json", "errors", "fmt",
		"github.com/toeshee/it-suite/agent/internal/config",
		"github.com/toeshee/it-suite/agent/internal/jobs",
		"log/slog", "os", "os/exec", "path/filepath", "runtime", "strconv", "strings",
		"sync", "sync/atomic", "time",
	},
	"windows": {
		"context", "encoding/json", "errors", "fmt",
		"github.com/toeshee/it-suite/agent/internal/config",
		"github.com/toeshee/it-suite/agent/internal/jobs",
		"github.com/toeshee/it-suite/agent/internal/secdir",
		"golang.org/x/sys/windows", "golang.org/x/sys/windows/svc", "golang.org/x/sys/windows/svc/mgr",
		"io", "log/slog", "os", "os/exec", "path/filepath", "runtime", "strconv", "strings",
		"sync", "sync/atomic", "syscall", "time",
	},
	"darwin": {
		"context", "encoding/json", "errors", "fmt",
		"github.com/toeshee/it-suite/agent/internal/config",
		"github.com/toeshee/it-suite/agent/internal/jobs",
		"log/slog", "os", "os/exec", "path/filepath", "runtime", "strconv", "strings",
		"sync", "sync/atomic", "time",
	},
}

func goList(t *testing.T, goos, format string, pkgs ...string) []string {
	t.Helper()
	return strings.Fields(strings.Join(goListLines(t, goos, format, pkgs...), " "))
}

func goListLines(t *testing.T, goos, format string, pkgs ...string) []string {
	t.Helper()
	gobin, err := exec.LookPath("go")
	if err != nil {
		t.Fatalf("the go tool is not on PATH, so what this package links cannot be measured: %v", err)
	}
	cmd := exec.Command(gobin, append([]string{"list", "-f", format}, pkgs...)...)
	cmd.Dir = "../../.." // the module root
	cmd.Env = append(cmd.Environ(), "GOOS="+goos, "GOARCH=amd64", "CGO_ENABLED=0")
	out, err := cmd.Output()
	if err != nil {
		var stderr string
		if ee, ok := err.(*exec.ExitError); ok {
			stderr = string(ee.Stderr)
		}
		t.Fatalf("go list %v: %v\n%s", pkgs, err, stderr)
	}
	return strings.Split(strings.TrimSpace(string(out)), "\n")
}

func TestUninstallImportsArePinnedPerOS(t *testing.T) {
	for goos, want := range wantImports {
		got := goList(t, goos, `{{join .Imports " "}}`, "./internal/capabilities/uninstall")
		sort.Strings(got)
		w := append([]string(nil), want...)
		sort.Strings(w)
		if !reflect.DeepEqual(got, w) {
			t.Errorf("%s: internal/capabilities/uninstall imports\n got %v\nwant %v\n"+
				"update wantImports deliberately if this is intended", goos, got, w)
		}
	}
}

func TestUninstallLinksNothingTheServiceDoesNotAlreadyLink(t *testing.T) {
	for goos := range wantImports {
		// The rest of the service: cmd/agent's imports and every capability,
		// minus the registry (which imports this package) and this package.
		var roots []string
		for _, p := range goList(t, goos, `{{join .Imports " "}}`, "./cmd/agent") {
			if p != registryPkg && p != uninstallPkg {
				roots = append(roots, p)
			}
		}
		// Every other capability package that does not itself reach this one
		// (the registry does, and so does remote/relayagent, which embeds it).
		reaches := map[string]bool{}
		for _, line := range goListLines(t, goos, `{{.ImportPath}} {{join .Deps " "}}`, "./internal/capabilities/...") {
			f := strings.Fields(line)
			for _, d := range f[1:] {
				if d == uninstallPkg {
					reaches[f[0]] = true
				}
			}
			if f[0] != uninstallPkg && !reaches[f[0]] {
				roots = append(roots, f[0])
			}
		}
		if len(roots) < 20 {
			t.Fatalf("%s: only %d service roots; the comparison would prove nothing", goos, len(roots))
		}
		service := map[string]bool{}
		for _, p := range goList(t, goos, "{{.ImportPath}}", append([]string{"-deps"}, roots...)...) {
			service[p] = true
		}
		if service[uninstallPkg] {
			t.Fatalf("%s: a service root imports the uninstall package; the comparison would be circular", goos)
		}
		var extra []string
		for _, p := range goList(t, goos, "{{.ImportPath}}", "-deps", "./internal/capabilities/uninstall") {
			if p != uninstallPkg && !service[p] {
				extra = append(extra, p)
			}
		}
		if len(extra) > 0 {
			t.Errorf("%s: agent.uninstall makes the service link %v, which nothing else in it needs "+
				"(CLAUDE.md idle budget)", goos, extra)
		}
	}
}
