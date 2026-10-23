//go:build e2e

package e2e

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"
	"testing"
	"time"
)

// platform is what differs per service manager.
type platform interface {
	dataDir() string
	target() string
	install(t *testing.T, repoRoot, binary string)
	uninstall(t *testing.T, repoRoot string)
	trustCA(t *testing.T, pemPath string)
	untrustCA(t *testing.T)
	// configuredBinary is the path the service definition names now.
	configuredBinary(t *testing.T) string
	killHelper(t *testing.T)
	helperGone() (bool, string)
	listeners() ([]string, error)
}

// commandBudget bounds every external command the harness runs. A tool that
// waits on something it will never get -- an authorization prompt on a
// headless Mac, a service manager that never answers -- must fail the test
// with its own name on it, not sit until the 65-minute test timeout says only
// that something, somewhere, did not return.
const commandBudget = 3 * time.Minute

func run(t *testing.T, name string, args ...string) string {
	t.Helper()
	out, err := runQuiet(name, args...)
	if err != nil {
		t.Fatalf("%s %s: %v\n%s", name, strings.Join(args, " "), err, out)
	}
	return out
}

func runQuiet(name string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), commandBudget)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	// A killed tool's children can hold the output pipe open; stop waiting
	// for them shortly after the kill.
	cmd.WaitDelay = 10 * time.Second
	out, err := cmd.CombinedOutput()
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		err = fmt.Errorf("did not return within %s (killed): %w", commandBudget, err)
	}
	return string(out), err
}

// agentLines keeps the lines of a tool's output that name an its-agent process.
func agentLines(out string) []string {
	var found []string
	for _, line := range strings.Split(out, "\n") {
		if strings.Contains(strings.ToLower(line), "its-agent") {
			found = append(found, strings.TrimSpace(line))
		}
	}
	return found
}
