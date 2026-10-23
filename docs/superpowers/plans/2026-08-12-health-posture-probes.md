# Health Posture Probes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `posture.go`'s blanket `unavailable` with real probes for the four posture-cadence check types, on Linux, macOS and Windows.

**Architecture:** Untagged `_probe_` files hold every decision and shell out only through an injected `postureRunner` seam, so Linux *and* macOS logic is unit-tested on Linux CI. Only the dispatch that selects a sampler is build-tagged. Windows cannot use the seam (`x/sys/windows` builds nowhere else) so its logic and dispatch share one tagged file and compile checks are its entire verification.

**Tech Stack:** Go 1.24, no cgo, no new dependency. `golang.org/x/sys/windows` is already required.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-12-health-posture-probes-design.md` (commit `1bd1537`). Where this plan and the spec disagree, the spec governs — raise it rather than choosing.
- **`true` and `false` each require a positive parsed statement. Everything else is `unavailable`.** Tool absent, non-zero exit with unrecognised output, unparseable output, permission denied, deadline exceeded, overlap-guard rejection → `unavailable()`. Never infer either answer from absence of evidence.
- Check-type strings, exactly as registered in `checks.go:76-79`: `posture.disk_encryption`, `posture.firewall_enabled`, `posture.antivirus_healthy`, `disk.smart_ok`.
- `posture.antivirus_healthy` is `unavailable` on Linux and macOS (§4.1: Windows only). `disk.smart_ok` is `unavailable` on macOS (`—` in §4.1).
- Per-command deadline **5s**, `cmd.WaitDelay` **2s**, output capped at **1 MiB**.
- No caching between passes (§4.3). The overlap guard returns `unavailable`, never a stale value.
- `NewPostureSampler() Sampler` keeps its exact signature — `main.go:214` and §0.5 falsifier 3 must remain untouched and passing.
- No new `check_type`, no wire kind, no field, no schema, no migration, no server change.
- Windows and macOS ship compile-only. Every file that has never executed says so in its header, listing what could not be confirmed.
- Never `git add -A`, `git add .`, `git commit -a`, `git checkout .`, or `git stash`. Stage named paths only.
- Run after every task: `cd agent && gofmt -l . && go vet ./... && go test ./internal/capabilities/health/ -count=1`.

---

## File Structure

| File | Tag | Responsibility |
|---|---|---|
| `posture.go` | none | Runner seam, bounded exec, deadline, overlap guard, probe dispatch table |
| `posture_linux_probes.go` | none | LUKS / firewall ladder / SMART logic |
| `posture_darwin_probes.go` | none | FileVault / socketfilterfw logic |
| `posture_linux.go` | `linux` | `newPlatformPosture()` |
| `posture_darwin.go` | `darwin` | `newPlatformPosture()` |
| `posture_windows.go` | `windows` | Four COM bindings + `newPlatformPosture()` |
| `posture_other.go` | `!linux && !darwin && !windows` | `newPlatformPosture()` → all unavailable |
| `posture_test.go` | none | Core + both probe files, table-driven |
| `posture_budget_test.go` | none | The four new falsifiers |
| `posture_linux_integration_test.go` | none | Real-tool test, skips loudly |

---

### Task 1: The posture core — seam, deadline, overlap guard

**Files:**
- Modify: `agent/internal/capabilities/health/posture.go` (replaces the whole file)
- Test: `agent/internal/capabilities/health/posture_test.go` (create)

**Interfaces:**
- Consumes: `Sample`, `unavailable()` from `checks.go:108-119`; `Sampler` from `health.go`.
- Produces: `postureRunner`, `postureResult` (fields `Stdout, Stderr, ExitCode, Err, NotFound`; method `ok() bool`), `execPostureRunner`, `probeFunc`, `newPostureSampler(run postureRunner, probes map[string]probeFunc) Sampler`, `boolSample(bool) Sample`, and the constants `postureDeadline`, `postureWaitDelay`, `maxPostureOutput`.

- [ ] **Step 1: Write the failing test**

```go
// posture_test.go
package health

import (
	"context"
	"sync"
	"testing"
)

// fakeRun records every command and answers from a table keyed by the binary
// name. A name absent from the table is reported NotFound, which is how a
// probe learns a tool is not installed.
type fakeRun struct {
	mu    sync.Mutex
	calls []string
	out   map[string]postureResult
}

func (f *fakeRun) run(_ context.Context, name string, args ...string) postureResult {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, name)
	if r, ok := f.out[name]; ok {
		return r
	}
	return postureResult{ExitCode: -1, NotFound: true, Err: errNotInstalled}
}

func (f *fakeRun) n() int { f.mu.Lock(); defer f.mu.Unlock(); return len(f.calls) }

func TestAnUnknownCheckTypeIsUnavailableAndRunsNothing(t *testing.T) {
	f := &fakeRun{}
	s := newPostureSampler(f.run, map[string]probeFunc{})
	if got := s.Sample("posture.disk_encryption", ""); got.Available {
		t.Errorf("Sample of an unregistered probe = %+v, want unavailable", got)
	}
	if f.n() != 0 {
		t.Errorf("ran %d commands for an unregistered probe, want 0", f.n())
	}
}

func TestASecondConcurrentSampleRunsZeroCommands(t *testing.T) {
	// The overlap guard. health.check.now is a signed job that samples every
	// posture check; the posture sub-tick is driven by the engine's own timer.
	// Only one of the two is a job, so the dispatcher's per-capability
	// serialisation does not keep them apart.
	release := make(chan struct{})
	entered := make(chan struct{})
	f := &fakeRun{}
	blocking := func(ctx context.Context, run postureRunner, _ string) Sample {
		close(entered)
		<-release
		run(ctx, "slow-probe")
		return boolSample(true)
	}
	s := newPostureSampler(f.run, map[string]probeFunc{"posture.disk_encryption": blocking})

	done := make(chan Sample, 1)
	go func() { done <- s.Sample("posture.disk_encryption", "") }()
	<-entered

	second := s.Sample("posture.disk_encryption", "")
	if second.Available {
		t.Errorf("the second concurrent Sample = %+v, want unavailable", second)
	}
	if n := f.n(); n != 0 {
		t.Errorf("the second concurrent Sample ran %d commands, want 0 -- probes must not stack", n)
	}
	close(release)
	if first := <-done; !first.Available || !first.Bool {
		t.Errorf("the first Sample = %+v, want available/true", first)
	}
}

func TestTheRealRunnerReportsAMissingBinaryAsNotFoundRatherThanFailure(t *testing.T) {
	// A probe distinguishes "the tool is not installed" from "the tool ran and
	// said no" -- firewall-cmd exits non-zero to say "not running", which IS an
	// answer, while an absent binary is not.
	res := execPostureRunner(context.Background(), "definitely-not-a-real-binary-xyz")
	if !res.NotFound {
		t.Errorf("execPostureRunner on a missing binary = %+v, want NotFound", res)
	}
}
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd agent && go test ./internal/capabilities/health/ -run 'Unregistered|Concurrent|MissingBinary' -count=1`
Expected: FAIL — `undefined: newPostureSampler`, `undefined: postureResult`, `undefined: errNotInstalled`.

- [ ] **Step 3: Write `posture.go`**

```go
package health

// The posture sampler.
//
// It is a SEPARATE object from the fast-tick sampler, on every platform, and
// that separation is the mechanism behind §0.5 falsifier 3: posture probes
// running on the fast tick instead of the posture sub-tick is a DEFECT, and the
// observation that catches it is this object's call counter staying at zero
// across a run of fast ticks.
//
// Everything here is bounded on two axes, because these probes spawn processes
// on Linux and macOS:
//
//   - a 5s deadline and cmd.WaitDelay per command, and
//   - an overlap guard, so a `health.check.now` job landing on top of a posture
//     sub-tick gets `unavailable` instead of a second set of shell-outs. Only
//     one of those two callers is a job, so the Dispatcher's per-capability
//     serialisation does not keep them apart.
//
// The guard returns `unavailable`; it is NOT a cache. §4.3 forbids holding a
// sample between ticks and this holds none.

import (
	"bytes"
	"context"
	"errors"
	"os/exec"
	"sync"
	"time"
)

const (
	postureDeadline  = 5 * time.Second
	postureWaitDelay = 2 * time.Second
	maxPostureOutput = 1 << 20 // 1 MiB
)

var errNotInstalled = errors.New("health: the tool is not installed")

type postureResult struct {
	Stdout   string
	Stderr   string
	ExitCode int
	Err      error
	// NotFound separates "this tool is not installed" from "this tool ran and
	// answered". `firewall-cmd --state` exits non-zero to say "not running",
	// which is a real answer; a missing binary is not.
	NotFound bool
}

func (r postureResult) ok() bool { return r.Err == nil }

// postureRunner is the injected seam the shell-out probes take, so Linux's and
// macOS's decision-making is unit-tested on a machine that has neither tool.
//
// patch's `runner` is the same shape but unexported in its own package. It is
// duplicated here rather than shared, per the convention stated at
// internal/capabilities/netsweep/netsweep_test.go:1240.
type postureRunner func(ctx context.Context, name string, args ...string) postureResult

func execPostureRunner(ctx context.Context, name string, args ...string) postureResult {
	path, err := exec.LookPath(name)
	if err != nil {
		return postureResult{ExitCode: -1, Err: err, NotFound: true}
	}
	ctx, cancel := context.WithTimeout(ctx, postureDeadline)
	defer cancel()

	cmd := exec.CommandContext(ctx, path, args...)
	// WaitDelay is NOT OPTIONAL, and it was measured on this project rather
	// than reasoned about (patch/exec.go and inventory/exec.go independently
	// record the same finding). CommandContext's cancellation kills only the
	// direct child; cmd.Wait then blocks until the stdout pipe closes, and a
	// GRANDCHILD that inherited it keeps it open. A 200ms deadline took 30.003s
	// before this line existed.
	cmd.WaitDelay = postureWaitDelay

	var out, errb postureBoundedBuffer
	out.limit, errb.limit = maxPostureOutput, maxPostureOutput
	cmd.Stdout, cmd.Stderr = &out, &errb

	runErr := cmd.Run()
	res := postureResult{Stdout: out.String(), Stderr: errb.String(), Err: runErr}
	var ee *exec.ExitError
	switch {
	case runErr == nil:
		res.ExitCode = 0
	case errors.As(runErr, &ee):
		res.ExitCode = ee.ExitCode()
	default:
		res.ExitCode = -1
	}
	return res
}

// postureBoundedBuffer refuses writes past limit. A cap enforced by truncating
// AFTERWARDS is not a cap: the memory was already allocated.
type postureBoundedBuffer struct {
	buf   bytes.Buffer
	limit int
}

func (b *postureBoundedBuffer) Write(p []byte) (int, error) {
	if room := b.limit - b.buf.Len(); room > 0 {
		if len(p) > room {
			p = p[:room]
		}
		b.buf.Write(p)
	}
	// Always reports the full length: reporting short would make the child see
	// a write error and possibly die on SIGPIPE.
	return len(p), nil
}

func (b *postureBoundedBuffer) String() string { return b.buf.String() }

// probeFunc answers one check. selector is the SIGNED string from the policy
// and is never a command, a script or a path the server composed.
type probeFunc func(ctx context.Context, run postureRunner, selector string) Sample

// boolSample is the ONLY way this package produces a definite posture answer.
// Every other path returns unavailable(), which is what makes the asymmetry in
// §5 of the design a property of the code rather than of each author's care.
func boolSample(v bool) Sample { return Sample{Available: true, Bool: v} }

type postureSampler struct {
	run    postureRunner
	probes map[string]probeFunc
	mu     sync.Mutex
}

func newPostureSampler(run postureRunner, probes map[string]probeFunc) Sampler {
	return &postureSampler{run: run, probes: probes}
}

// NewPostureSampler returns the platform's posture-cadence sampler.
func NewPostureSampler() Sampler { return newPlatformPosture() }

func (p *postureSampler) Sample(checkType, selector string) Sample {
	probe, ok := p.probes[checkType]
	if !ok {
		// Includes every check this platform cannot answer: they are simply
		// absent from the table. §0.7 -- unavailable is stored, surfaced and
		// countable, and never opens or clears an alert.
		return unavailable()
	}
	// TryLock, not Lock: a caller that finds a pass in flight must be answered
	// now with `unavailable`, not queued behind a second set of shell-outs.
	if !p.mu.TryLock() {
		return unavailable()
	}
	defer p.mu.Unlock()
	return probe(context.Background(), p.run, selector)
}
```

- [ ] **Step 4: Add `posture_other.go` so every platform still builds**

```go
//go:build !linux && !darwin && !windows

package health

// No posture probe exists for this platform. Every posture check reports
// `unavailable` (§0.7), which is a visible, countable state and never opens or
// clears an alert.
func newPlatformPosture() Sampler {
	return newPostureSampler(execPostureRunner, map[string]probeFunc{})
}
```

- [ ] **Step 5: Add temporary `posture_linux.go` and `posture_darwin.go` stubs so the package builds on this machine**

```go
//go:build linux

package health

func newPlatformPosture() Sampler {
	return newPostureSampler(execPostureRunner, map[string]probeFunc{})
}
```

```go
//go:build darwin

package health

func newPlatformPosture() Sampler {
	return newPostureSampler(execPostureRunner, map[string]probeFunc{})
}
```

And the Windows stub, for the same reason — `posture_other.go`'s tag excludes
`windows`, so without this `GOOS=windows go vet ./...` fails with
`undefined: newPlatformPosture`, and that vet is a CI step:

```go
//go:build windows

package health

// Replaced in Task 7 by the real COM bindings. Until then every posture check
// reports `unavailable` on Windows, which is a visible, countable state that
// never opens or clears an alert (§0.7).
func newPlatformPosture() Sampler {
	return newPostureSampler(execPostureRunner, map[string]probeFunc{})
}
```

(Tasks 5, 6 and 7 fill these three in. They are written now so the package compiles on every platform and every existing test — including §0.5 falsifier 3 — keeps running.)

- [ ] **Step 6: Run the tests**

Run: `cd agent && go test ./internal/capabilities/health/ -count=1`
Expected: PASS, including all eight existing falsifier tests.

- [ ] **Step 7: Commit**

```bash
git add agent/internal/capabilities/health/posture.go \
        agent/internal/capabilities/health/posture_test.go \
        agent/internal/capabilities/health/posture_other.go \
        agent/internal/capabilities/health/posture_linux.go \
        agent/internal/capabilities/health/posture_darwin.go
git commit -m "feat(agent/health): the posture probe core — seam, deadline, overlap guard"
```

---

### Task 2: Linux disk encryption

**Files:**
- Create: `agent/internal/capabilities/health/posture_linux_probes.go`
- Modify: `agent/internal/capabilities/health/posture_test.go`

**Interfaces:**
- Consumes: `postureRunner`, `postureResult`, `probeFunc`, `boolSample`, `unavailable` (Task 1).
- Produces: `probeDiskEncryptionLinux(ctx context.Context, run postureRunner, selector string) Sample`.

- [ ] **Step 1: Write the failing test**

```go
func TestLinuxDiskEncryption(t *testing.T) {
	const dev = "/dev/mapper/vg0-root"
	for _, tc := range []struct {
		name string
		out  map[string]postureResult
		want Sample
	}{
		{
			name: "root sits on a crypt device",
			out: map[string]postureResult{
				"findmnt": {Stdout: dev + "\n"},
				"lsblk":   {Stdout: "vg0-root lvm\nluks-abc crypt\nvda2 part\nvda disk\n"},
			},
			want: boolSample(true),
		},
		{
			name: "root has no crypt anywhere in its ancestry",
			out: map[string]postureResult{
				"findmnt": {Stdout: dev + "\n"},
				"lsblk":   {Stdout: "vg0-root lvm\nvda2 part\nvda disk\n"},
			},
			want: boolSample(false),
		},
		{
			name: "lsblk is not installed -- absence of evidence is not `false`",
			out:  map[string]postureResult{"findmnt": {Stdout: dev + "\n"}},
			want: unavailable(),
		},
		{
			name: "lsblk answered nothing parseable",
			out: map[string]postureResult{
				"findmnt": {Stdout: dev + "\n"},
				"lsblk":   {Stdout: "\n\n"},
			},
			want: unavailable(),
		},
		{
			name: "findmnt is not installed",
			out:  map[string]postureResult{},
			want: unavailable(),
		},
		{
			name: "findmnt reported an empty source",
			out:  map[string]postureResult{"findmnt": {Stdout: "\n"}},
			want: unavailable(),
		},
		{
			name: "a bind-mounted root's [subpath] suffix is stripped before lsblk",
			out: map[string]postureResult{
				"findmnt": {Stdout: "/dev/vda2[/rootfs]\n"},
				"lsblk":   {Stdout: "vda2 part\nluks-x crypt\n"},
			},
			want: boolSample(true),
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := &fakeRun{out: tc.out}
			if got := probeDiskEncryptionLinux(context.Background(), f.run, ""); got != tc.want {
				t.Errorf("= %+v, want %+v", got, tc.want)
			}
		})
	}
}
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd agent && go test ./internal/capabilities/health/ -run TestLinuxDiskEncryption -count=1`
Expected: FAIL — `undefined: probeDiskEncryptionLinux`.

- [ ] **Step 3: Write the probe**

```go
package health

// This file has NO build tag. It shells out only through the injected
// postureRunner, so every decision it makes -- device resolution, the crypt
// test, the firewall ladder, SMART aggregation, and every `unavailable` branch
// -- is unit-tested on any machine, including this project's CI, which has no
// cryptsetup, no ufw, no firewalld and no smartctl. Only the dispatch that
// SELECTS these probes (posture_linux.go) needs `//go:build linux`.

import (
	"context"
	"strings"
)

// probeDiskEncryptionLinux answers for the SYSTEM/BOOT volume only (design
// §2.3): the device root is mounted from, and whether a dm-crypt layer appears
// anywhere in that device's ancestry.
//
// `lsblk -s` walks PARENTS, which is what makes this correct for root on
// LVM-on-LUKS as well as for a plain LUKS partition.
//
// A machine with an encrypted root and an unencrypted /data reads as healthy.
// That is a stated limit, not an oversight: the check takes no selector, and
// per-volume coverage needs its own check type, which §0.1 makes a schema
// change.
func probeDiskEncryptionLinux(ctx context.Context, run postureRunner, _ string) Sample {
	res := run(ctx, "findmnt", "-n", "-o", "SOURCE", "--target", "/")
	if res.NotFound || !res.ok() {
		return unavailable()
	}
	dev := strings.TrimSpace(res.Stdout)
	if i := strings.IndexByte(dev, '\n'); i >= 0 {
		dev = dev[:i]
	}
	// findmnt renders a bind mount's source as `/dev/vda2[/subpath]`. lsblk
	// does not accept that form, so the suffix is stripped: the QUESTION is
	// about the device, and the subpath does not change which device it is.
	if i := strings.IndexByte(dev, '['); i >= 0 {
		dev = dev[:i]
	}
	if dev == "" {
		return unavailable()
	}

	anc := run(ctx, "lsblk", "-nso", "NAME,TYPE", dev)
	if anc.NotFound || !anc.ok() {
		return unavailable()
	}
	parsed := false
	for _, line := range strings.Split(anc.Stdout, "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		parsed = true
		if fields[len(fields)-1] == "crypt" {
			return boolSample(true)
		}
	}
	if !parsed {
		// lsblk ran and said nothing this code understands. Reporting `false`
		// here would be inferring "not encrypted" from "cannot tell", which is
		// the one direction §5 forbids in both directions.
		return unavailable()
	}
	return boolSample(false)
}
```

- [ ] **Step 4: Run the tests**

Run: `cd agent && go test ./internal/capabilities/health/ -run TestLinuxDiskEncryption -count=1 -v`
Expected: PASS, 7 subtests.

- [ ] **Step 5: Commit**

```bash
git add agent/internal/capabilities/health/posture_linux_probes.go \
        agent/internal/capabilities/health/posture_test.go
git commit -m "feat(agent/health): the Linux disk-encryption probe"
```

---

### Task 3: The Linux firewall ladder

**Files:**
- Modify: `agent/internal/capabilities/health/posture_linux_probes.go`
- Modify: `agent/internal/capabilities/health/posture_test.go`

**Interfaces:**
- Produces: `probeFirewallLinux(ctx context.Context, run postureRunner, selector string) Sample`, `nftInputDrops(out string) bool`.

- [ ] **Step 1: Write the failing test**

```go
func TestLinuxFirewall(t *testing.T) {
	for _, tc := range []struct {
		name     string
		out      map[string]postureResult
		want     Sample
		wantRuns []string // exact commands, in order
	}{
		{
			name:     "ufw active wins and nothing below it runs",
			out:      map[string]postureResult{"ufw": {Stdout: "Status: active\n"}},
			want:     boolSample(true),
			wantRuns: []string{"ufw"},
		},
		{
			name:     "ufw inactive is an answer, not a fallthrough",
			out:      map[string]postureResult{"ufw": {Stdout: "Status: inactive\n"}},
			want:     boolSample(false),
			wantRuns: []string{"ufw"},
		},
		{
			name:     "ufw present but unreadable (not root) is unavailable, not false",
			out:      map[string]postureResult{"ufw": {Stderr: "ERROR: You need to be root to run this script", ExitCode: 1, Err: errNotInstalled}},
			want:     unavailable(),
			wantRuns: []string{"ufw"},
		},
		{
			name: "firewalld running, ufw absent",
			out:  map[string]postureResult{"firewall-cmd": {Stdout: "running\n"}},
			want: boolSample(true), wantRuns: []string{"ufw", "firewall-cmd"},
		},
		{
			name: "firewalld says not running on a NON-ZERO exit -- still an answer",
			out: map[string]postureResult{
				"firewall-cmd": {Stdout: "not running\n", ExitCode: 252, Err: errNotInstalled},
			},
			want: boolSample(false), wantRuns: []string{"ufw", "firewall-cmd"},
		},
		{
			name: "no front-end: nft input policy drop",
			out: map[string]postureResult{
				"nft": {Stdout: "table inet filter {\n  chain input {\n    type filter hook input priority 0; policy drop;\n  }\n}\n"},
			},
			want: boolSample(true), wantRuns: []string{"ufw", "firewall-cmd", "nft"},
		},
		{
			name: "no front-end: nft ruleset with an accept policy is NOT enabled",
			out: map[string]postureResult{
				"nft": {Stdout: "table inet filter {\n  chain input {\n    type filter hook input priority 0; policy accept;\n    tcp dport 22 accept\n  }\n}\n"},
			},
			want: boolSample(false), wantRuns: []string{"ufw", "firewall-cmd", "nft"},
		},
		{
			name: "no front-end, no nft: iptables DROP policy",
			out: map[string]postureResult{
				"iptables": {Stdout: "-P INPUT DROP\n-A INPUT -i lo -j ACCEPT\n"},
			},
			want: boolSample(true), wantRuns: []string{"ufw", "firewall-cmd", "nft", "iptables"},
		},
		{
			name: "no front-end, no nft: iptables ACCEPT policy with rules is not a firewall",
			out: map[string]postureResult{
				"iptables": {Stdout: "-P INPUT ACCEPT\n-A INPUT -p tcp --dport 22 -j ACCEPT\n"},
			},
			want: boolSample(false), wantRuns: []string{"ufw", "firewall-cmd", "nft", "iptables"},
		},
		{
			name: "nothing installed at all",
			out:  map[string]postureResult{},
			want: unavailable(), wantRuns: []string{"ufw", "firewall-cmd", "nft", "iptables"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := &fakeRun{out: tc.out}
			if got := probeFirewallLinux(context.Background(), f.run, ""); got != tc.want {
				t.Errorf("= %+v, want %+v", got, tc.want)
			}
			if strings.Join(f.calls, ",") != strings.Join(tc.wantRuns, ",") {
				t.Errorf("ran %v, want %v", f.calls, tc.wantRuns)
			}
		})
	}
}
```

Add `"strings"` to `posture_test.go`'s imports.

- [ ] **Step 2: Run it and watch it fail**

Run: `cd agent && go test ./internal/capabilities/health/ -run TestLinuxFirewall -count=1`
Expected: FAIL — `undefined: probeFirewallLinux`.

- [ ] **Step 3: Write the ladder**

```go
// probeFirewallLinux asks the front-end that OWNS the policy, and only falls
// back to the kernel ruleset when no front-end is installed (design §2.2).
//
// The order matters and the stopping rule matters more: a front-end that is
// installed ANSWERS, including when its answer is "inactive". Falling through
// to the raw ruleset after ufw said "inactive" would let a leftover iptables
// chain report a disabled firewall as enabled.
//
// `firewall-cmd --state` exits NON-ZERO (252) to say "not running". That is an
// answer, which is why every step here tests NotFound rather than ok().
func probeFirewallLinux(ctx context.Context, run postureRunner, _ string) Sample {
	if res := run(ctx, "ufw", "status"); !res.NotFound {
		out := strings.ToLower(res.Stdout)
		switch {
		case strings.Contains(out, "status: active"):
			return boolSample(true)
		case strings.Contains(out, "status: inactive"):
			return boolSample(false)
		}
		// Installed but unreadable -- most often "you need to be root".
		return unavailable()
	}
	if res := run(ctx, "firewall-cmd", "--state"); !res.NotFound {
		switch strings.TrimSpace(res.Stdout + res.Stderr) {
		case "running":
			return boolSample(true)
		case "not running":
			return boolSample(false)
		}
		return unavailable()
	}
	if res := run(ctx, "nft", "list", "ruleset"); !res.NotFound {
		if !res.ok() {
			return unavailable()
		}
		return boolSample(nftInputDrops(res.Stdout))
	}
	if res := run(ctx, "iptables", "-S", "INPUT"); !res.NotFound {
		if !res.ok() {
			return unavailable()
		}
		for _, line := range strings.Split(res.Stdout, "\n") {
			switch strings.TrimSpace(line) {
			case "-P INPUT DROP", "-P INPUT REJECT":
				return boolSample(true)
			}
		}
		// An ACCEPT policy with rules in it is NOT "enabled": those rules may
		// all be permissive, and "iptables is installed" is not a firewall.
		return boolSample(false)
	}
	return unavailable()
}

// nftInputDrops reports whether any base chain hooked to `input` has a default
// policy of drop. Only the DEFAULT policy counts, for the same reason the
// iptables branch above only reads `-P`.
func nftInputDrops(out string) bool {
	inInputChain := false
	for _, line := range strings.Split(out, "\n") {
		l := strings.TrimSpace(line)
		if strings.Contains(l, "hook input") {
			inInputChain = true
			if strings.Contains(l, "policy drop") {
				return true
			}
			continue
		}
		if inInputChain {
			if strings.Contains(l, "policy drop") {
				return true
			}
			if l == "}" {
				inInputChain = false
			}
		}
	}
	return false
}
```

- [ ] **Step 4: Run the tests**

Run: `cd agent && go test ./internal/capabilities/health/ -run TestLinuxFirewall -count=1 -v`
Expected: PASS, 10 subtests.

- [ ] **Step 5: Commit**

```bash
git add agent/internal/capabilities/health/posture_linux_probes.go \
        agent/internal/capabilities/health/posture_test.go
git commit -m "feat(agent/health): the Linux firewall ladder"
```

---

### Task 4: Linux SMART, and the `*` aggregation rule

**Files:**
- Modify: `agent/internal/capabilities/health/posture_linux_probes.go`
- Modify: `agent/internal/capabilities/health/posture_test.go`

**Interfaces:**
- Produces: `probeSMARTLinux(ctx context.Context, run postureRunner, selector string) Sample`.

- [ ] **Step 1: Write the failing test**

```go
func TestLinuxSMART(t *testing.T) {
	pass := postureResult{Stdout: "SMART overall-health self-assessment test result: PASSED\n"}
	fail := postureResult{Stdout: "SMART overall-health self-assessment test result: FAILED!\n", ExitCode: 8, Err: errNotInstalled}

	t.Run("a named device that passes", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{"smartctl": pass}}
		if got := probeSMARTLinux(context.Background(), f.run, "/dev/sda"); got != boolSample(true) {
			t.Errorf("= %+v, want true", got)
		}
	})
	t.Run("a named device that is failing", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{"smartctl": fail}}
		if got := probeSMARTLinux(context.Background(), f.run, "/dev/sda"); got != boolSample(false) {
			t.Errorf("= %+v, want false", got)
		}
	})
	t.Run("smartctl absent is unavailable, never true", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{}}
		if got := probeSMARTLinux(context.Background(), f.run, "/dev/sda"); got.Available {
			t.Errorf("= %+v, want unavailable", got)
		}
	})
	t.Run("star: no device could be read at all is unavailable, NOT true", func(t *testing.T) {
		// The tempting "true if no failures were seen" would report a healthy
		// fleet on a machine where smartctl cannot read a single disk.
		f := &fakeRun{out: map[string]postureResult{
			"lsblk":    {Stdout: "sda disk\nsdb disk\n"},
			"smartctl": {Stdout: "Smartctl open device: /dev/sda failed: Permission denied\n", ExitCode: 2, Err: errNotInstalled},
		}}
		if got := probeSMARTLinux(context.Background(), f.run, "*"); got.Available {
			t.Errorf("= %+v, want unavailable", got)
		}
	})
	t.Run("star: any failing device makes the whole answer false", func(t *testing.T) {
		calls := 0
		run := func(ctx context.Context, name string, args ...string) postureResult {
			if name == "lsblk" {
				return postureResult{Stdout: "sda disk\nsdb disk\n"}
			}
			calls++
			if calls == 2 {
				return fail
			}
			return pass
		}
		if got := probeSMARTLinux(context.Background(), run, "*"); got != boolSample(false) {
			t.Errorf("= %+v, want false", got)
		}
	})
	t.Run("star: every readable device passing is true", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{
			"lsblk":    {Stdout: "sda disk\nsdb disk\n"},
			"smartctl": pass,
		}}
		if got := probeSMARTLinux(context.Background(), f.run, "*"); got != boolSample(true) {
			t.Errorf("= %+v, want true", got)
		}
	})
}
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd agent && go test ./internal/capabilities/health/ -run TestLinuxSMART -count=1`
Expected: FAIL — `undefined: probeSMARTLinux`.

- [ ] **Step 3: Write the probe**

```go
// probeSMARTLinux reads one device's SMART self-assessment, or aggregates over
// every fixed disk when the selector is `*`.
//
// The aggregation rule is stated because the obvious one is wrong:
//
//   - `false` if ANY readable device reports failing;
//   - `true` only if AT LEAST ONE device was read and none reported failing;
//   - `unavailable` if NO device could be read.
//
// "true if no failures were seen" would report a healthy fleet on a machine
// where smartctl cannot read a single disk -- absence of evidence becoming a
// definite answer, which §5 forbids.
//
// smartctl's exit code is a BITFIELD (bit 3 = failing), so the health verdict
// is read from the output text rather than from the exit status.
func probeSMARTLinux(ctx context.Context, run postureRunner, selector string) Sample {
	if selector != "" && selector != "*" {
		return smartOneLinux(ctx, run, selector)
	}
	devs := fixedDisksLinux(ctx, run)
	if len(devs) == 0 {
		return unavailable()
	}
	read := 0
	for _, d := range devs {
		s := smartOneLinux(ctx, run, d)
		if !s.Available {
			continue
		}
		read++
		if !s.Bool {
			return boolSample(false)
		}
	}
	if read == 0 {
		return unavailable()
	}
	return boolSample(true)
}

func smartOneLinux(ctx context.Context, run postureRunner, dev string) Sample {
	res := run(ctx, "smartctl", "-H", dev)
	if res.NotFound {
		return unavailable()
	}
	out := res.Stdout + res.Stderr
	switch {
	case strings.Contains(out, "FAILED!"), strings.Contains(out, "FAILING_NOW"):
		return boolSample(false)
	case strings.Contains(out, "PASSED"), strings.Contains(out, "SMART Health Status: OK"):
		return boolSample(true)
	}
	return unavailable()
}

// fixedDisksLinux lists whole disks, excluding partitions, loop and ram devices.
func fixedDisksLinux(ctx context.Context, run postureRunner) []string {
	res := run(ctx, "lsblk", "-ndo", "NAME,TYPE")
	if res.NotFound || !res.ok() {
		return nil
	}
	var out []string
	for _, line := range strings.Split(res.Stdout, "\n") {
		f := strings.Fields(line)
		if len(f) != 2 || f[1] != "disk" {
			continue
		}
		if strings.HasPrefix(f[0], "loop") || strings.HasPrefix(f[0], "ram") || strings.HasPrefix(f[0], "zram") {
			continue
		}
		out = append(out, "/dev/"+f[0])
	}
	return out
}
```

- [ ] **Step 4: Run the tests**

Run: `cd agent && go test ./internal/capabilities/health/ -run TestLinuxSMART -count=1 -v`
Expected: PASS, 6 subtests.

- [ ] **Step 5: Commit**

```bash
git add agent/internal/capabilities/health/posture_linux_probes.go \
        agent/internal/capabilities/health/posture_test.go
git commit -m "feat(agent/health): the Linux SMART probe and its aggregation rule"
```

---

### Task 5: The Linux dispatch, and a real-tool integration test

**Files:**
- Modify: `agent/internal/capabilities/health/posture_linux.go` (replace the Task 1 stub)
- Create: `agent/internal/capabilities/health/posture_linux_integration_test.go`

**Interfaces:**
- Consumes: the three `probe*Linux` functions.
- Produces: a `newPlatformPosture()` whose table has exactly three entries.

- [ ] **Step 1: Write the dispatch**

```go
//go:build linux

package health

// The posture sampler on Linux.
//
// `posture.antivirus_healthy` is deliberately ABSENT from this table: §4.1
// makes it Windows-only, because "AV health" on Linux has no vendor-neutral
// answer and inventing one produces a check that is confidently wrong. An
// absent entry reports `unavailable`, which is a visible, countable state.
func newPlatformPosture() Sampler {
	return newPostureSampler(execPostureRunner, map[string]probeFunc{
		"posture.disk_encryption":  probeDiskEncryptionLinux,
		"posture.firewall_enabled": probeFirewallLinux,
		"disk.smart_ok":            probeSMARTLinux,
	})
}
```

- [ ] **Step 2: Write the integration test**

```go
package health

// This file has NO build tag; each test skips at runtime when the tool it
// needs is absent. A skip that reads as a pass is how a gap survives, so every
// skip here names what did not run.

import (
	"context"
	"os/exec"
	"strings"
	"testing"
)

// TestARealDiskEncryptionProbeAgreesWithLsblk runs the probe against the REAL
// tools on this machine and checks its answer against an independently derived
// one, rather than merely checking that it returned something.
func TestARealDiskEncryptionProbeAgreesWithLsblk(t *testing.T) {
	for _, tool := range []string{"findmnt", "lsblk"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skipf("SKIPPING the only real-tool disk-encryption proof: %q is not installed. "+
				"Nothing in this run exercised a real %s.", tool, tool)
		}
	}
	got := probeDiskEncryptionLinux(context.Background(), execPostureRunner, "")
	if !got.Available {
		t.Fatalf("the probe could not answer on a machine with both tools present: %+v", got)
	}

	// Independent derivation: ask the same two tools by hand.
	src, err := exec.Command("findmnt", "-n", "-o", "SOURCE", "--target", "/").Output()
	if err != nil {
		t.Fatalf("findmnt: %v", err)
	}
	dev := strings.TrimSpace(string(src))
	if i := strings.IndexByte(dev, '['); i >= 0 {
		dev = dev[:i]
	}
	anc, err := exec.Command("lsblk", "-nso", "NAME,TYPE", dev).Output()
	if err != nil {
		t.Fatalf("lsblk: %v", err)
	}
	want := strings.Contains(string(anc), " crypt")
	if got.Bool != want {
		t.Errorf("probe said encrypted=%v; lsblk's ancestry for %s says %v\n%s",
			got.Bool, dev, want, anc)
	}
	t.Logf("real disk-encryption probe on this machine: encrypted=%v (device %s)", got.Bool, dev)
}

// TestARealFirewallProbeReachesADefiniteAnswerOrSaysWhyNot exercises the
// ladder against whatever this machine actually has.
func TestARealFirewallProbeReachesADefiniteAnswerOrSaysWhyNot(t *testing.T) {
	have := []string{}
	for _, tool := range []string{"ufw", "firewall-cmd", "nft", "iptables"} {
		if _, err := exec.LookPath(tool); err == nil {
			have = append(have, tool)
		}
	}
	if len(have) == 0 {
		t.Skip("SKIPPING the only real-tool firewall proof: none of ufw, firewall-cmd, nft or iptables " +
			"is installed. Nothing in this run exercised a real firewall front-end or ruleset.")
	}
	got := probeFirewallLinux(context.Background(), execPostureRunner, "")
	t.Logf("real firewall probe on this machine (tools present: %v): available=%v value=%v",
		have, got.Available, got.Bool)
	if !got.Available && len(have) > 0 {
		// Not a failure: `nft list ruleset` needs privilege, and unavailable is
		// the correct answer for "installed but unreadable". Logged so a run
		// that answers nothing is visible rather than silent.
		t.Logf("the probe reported unavailable despite %v being present -- expected without privilege", have)
	}
}
```

- [ ] **Step 3: Run them**

Run: `cd agent && go test ./internal/capabilities/health/ -run 'AReal' -count=1 -v`
Expected: PASS. `findmnt` and `lsblk` are present in the dev container, so the first test really runs; the firewall test runs against `nft`/`iptables`.

- [ ] **Step 4: Prove the integration test can fail (falsification)**

Temporarily change `probeDiskEncryptionLinux`'s final `return boolSample(false)` to `return boolSample(true)`, re-run the test above, and confirm it fails with the `probe said encrypted=true; lsblk's ancestry ... says false` message. Revert, and confirm `git diff` for that file is empty.

- [ ] **Step 5: Run the whole package**

Run: `cd agent && go test ./internal/capabilities/health/ -count=1`
Expected: PASS, including all eight existing falsifiers.

- [ ] **Step 6: Commit**

```bash
git add agent/internal/capabilities/health/posture_linux.go \
        agent/internal/capabilities/health/posture_linux_integration_test.go
git commit -m "feat(agent/health): wire the Linux posture probes, with a real-tool proof"
```

---

### Task 6: macOS probes and dispatch

**Files:**
- Create: `agent/internal/capabilities/health/posture_darwin_probes.go`
- Modify: `agent/internal/capabilities/health/posture_darwin.go` (replace the Task 1 stub)
- Modify: `agent/internal/capabilities/health/posture_test.go`

**Interfaces:**
- Produces: `probeDiskEncryptionDarwin`, `probeFirewallDarwin`, both `probeFunc`-shaped.

- [ ] **Step 1: Write the failing test**

```go
func TestDarwinPostureProbes(t *testing.T) {
	t.Run("FileVault on", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{"fdesetup": {Stdout: "FileVault is On.\n"}}}
		if got := probeDiskEncryptionDarwin(context.Background(), f.run, ""); got != boolSample(true) {
			t.Errorf("= %+v, want true", got)
		}
	})
	t.Run("FileVault off", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{"fdesetup": {Stdout: "FileVault is Off.\n"}}}
		if got := probeDiskEncryptionDarwin(context.Background(), f.run, ""); got != boolSample(false) {
			t.Errorf("= %+v, want false", got)
		}
	})
	t.Run("fdesetup said something else entirely", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{"fdesetup": {Stdout: "Deferred enablement appears to be active\n"}}}
		if got := probeDiskEncryptionDarwin(context.Background(), f.run, ""); got.Available {
			t.Errorf("= %+v, want unavailable", got)
		}
	})
	t.Run("fdesetup absent", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{}}
		if got := probeDiskEncryptionDarwin(context.Background(), f.run, ""); got.Available {
			t.Errorf("= %+v, want unavailable", got)
		}
	})
	t.Run("firewall enabled for specific services (state 1)", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{
			"/usr/libexec/ApplicationFirewall/socketfilterfw": {Stdout: "Firewall is enabled. (State = 1)\n"},
		}}
		if got := probeFirewallDarwin(context.Background(), f.run, ""); got != boolSample(true) {
			t.Errorf("= %+v, want true", got)
		}
	})
	t.Run("firewall blocking all (state 2) is still enabled", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{
			"/usr/libexec/ApplicationFirewall/socketfilterfw": {Stdout: "Firewall is enabled. (State = 2)\n"},
		}}
		if got := probeFirewallDarwin(context.Background(), f.run, ""); got != boolSample(true) {
			t.Errorf("= %+v, want true", got)
		}
	})
	t.Run("firewall disabled (state 0)", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{
			"/usr/libexec/ApplicationFirewall/socketfilterfw": {Stdout: "Firewall is disabled. (State = 0)\n"},
		}}
		if got := probeFirewallDarwin(context.Background(), f.run, ""); got != boolSample(false) {
			t.Errorf("= %+v, want false", got)
		}
	})
	t.Run("socketfilterfw output not recognised", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{
			"/usr/libexec/ApplicationFirewall/socketfilterfw": {Stdout: "???\n"},
		}}
		if got := probeFirewallDarwin(context.Background(), f.run, ""); got.Available {
			t.Errorf("= %+v, want unavailable", got)
		}
	})
}
```

- [ ] **Step 2: Run it and watch it fail**

Run: `cd agent && go test ./internal/capabilities/health/ -run TestDarwinPostureProbes -count=1`
Expected: FAIL — `undefined: probeDiskEncryptionDarwin`.

- [ ] **Step 3: Write the probes**

```go
package health

// This file has NO build tag. Like posture_linux_probes.go it shells out only
// through the injected postureRunner, so every branch is exercised by a real
// `go test` on this project's Linux CI even though no macOS machine exists in
// this project.
//
// UNVERIFIED: the two command output FORMATS below are read from Apple's
// documentation and have never been observed on a real machine. What is tested
// here is the logic around them -- which strings mean on, which mean off, and
// that everything else is `unavailable`. If a format is wrong, the probe
// reports `unavailable` (no branch matches), which is visible and never opens
// or clears an alert. It does not silently report a healthy machine.

import (
	"context"
	"strings"
)

// probeDiskEncryptionDarwin answers for the system volume: FileVault is a
// single whole-container state, so there is no per-volume question to ask.
func probeDiskEncryptionDarwin(ctx context.Context, run postureRunner, _ string) Sample {
	res := run(ctx, "fdesetup", "status")
	if res.NotFound || !res.ok() {
		return unavailable()
	}
	out := strings.TrimSpace(res.Stdout)
	switch {
	case strings.HasPrefix(out, "FileVault is On."):
		return boolSample(true)
	case strings.HasPrefix(out, "FileVault is Off."):
		return boolSample(false)
	}
	// "Deferred enablement appears to be active for user X" is a real fdesetup
	// answer and it is NEITHER on nor off -- the volume is not encrypted yet
	// and will be at next logout. Reporting `false` would alarm on a machine
	// mid-enrolment; reporting `true` would hide one that never completes.
	return unavailable()
}

// socketfilterfwPath is the absolute path, not a PATH lookup. A root process
// resolving a bare name through PATH is a privilege-escalation shape, and there
// is exactly one socketfilterfw this can mean.
const socketfilterfwPath = "/usr/libexec/ApplicationFirewall/socketfilterfw"

// probeFirewallDarwin reads the application firewall's global state.
//
// State 1 (enabled for specific services) and state 2 (block all incoming) both
// count as enabled; only state 0 is disabled.
func probeFirewallDarwin(ctx context.Context, run postureRunner, _ string) Sample {
	res := run(ctx, socketfilterfwPath, "--getglobalstate")
	if res.NotFound || !res.ok() {
		return unavailable()
	}
	out := strings.ToLower(res.Stdout)
	switch {
	case strings.Contains(out, "state = 1"), strings.Contains(out, "state = 2"):
		return boolSample(true)
	case strings.Contains(out, "state = 0"):
		return boolSample(false)
	}
	return unavailable()
}
```

- [ ] **Step 4: Write the dispatch**

```go
//go:build darwin

package health

// The posture sampler on macOS.
//
// THIS FILE HAS NEVER EXECUTED. No macOS machine exists in this project. It is
// verified by `GOOS=darwin go vet ./...` and `GOOS=darwin go test -c`, which
// prove it TYPE-CHECKS and nothing more.
//
// Two entries are deliberately absent, and an absent entry reports
// `unavailable` (§0.7):
//
//   - `posture.antivirus_healthy`: §4.1 makes it Windows-only. "AV health" on
//     macOS has no vendor-neutral answer.
//   - `disk.smart_ok`: §4.1's table marks it `—` on macOS.
func newPlatformPosture() Sampler {
	return newPostureSampler(execPostureRunner, map[string]probeFunc{
		"posture.disk_encryption":  probeDiskEncryptionDarwin,
		"posture.firewall_enabled": probeFirewallDarwin,
	})
}
```

- [ ] **Step 5: Run the tests and the cross-platform checks**

```bash
cd agent
go test ./internal/capabilities/health/ -count=1
GOOS=darwin go vet ./internal/capabilities/health/
GOOS=darwin go test -c -o /dev/null ./internal/capabilities/health/
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add agent/internal/capabilities/health/posture_darwin_probes.go \
        agent/internal/capabilities/health/posture_darwin.go \
        agent/internal/capabilities/health/posture_test.go
git commit -m "feat(agent/health): the macOS posture probes (compile-only)"
```

---

### Task 7: The four Windows COM bindings

**Files:**
- Modify: `agent/internal/capabilities/health/posture_windows.go` (replaces the Task 1 stub — the file already exists and returns an empty probe table)

**Interfaces:**
- Produces: `newPlatformPosture()` for `GOOS=windows`, with four entries.

**Precedent to follow:** `agent/internal/capabilities/backup/snapshot_windows.go` is this repo's worked example of in-process COM without cgo — `CoInitializeEx`, a vtable struct of named `uintptr` fields with index comments, `syscall.SyscallN` dispatch, and a header listing what could not be confirmed. Read it before writing this file and mirror its mechanics. Do **not** copy its VSS-specific logic.

**Exact COM contracts to bind:**

| purpose | namespace / CLSID | what to read |
|---|---|---|
| BitLocker | WMI namespace `root\CIMV2\Security\MicrosoftVolumeEncryption`, class `Win32_EncryptableVolume` | for the volume whose `DriveLetter` equals the system drive, call `GetProtectionStatus`; out-param `ProtectionStatus == 1` means protected |
| AV health | WMI namespace `root\SecurityCenter2`, class `AntiVirusProduct` | property `productState` (uint32) |
| firewall | COM class `HNetCfg.FwPolicy2`, interface `INetFwPolicy2` (IID `{98325047-C671-4174-8D81-DEFCD3F03186}`) | `get_FirewallEnabled(profileType)` for each of `NET_FW_PROFILE2_DOMAIN=1`, `PRIVATE=2`, `PUBLIC=4` |
| SMART | WMI namespace `root\WMI`, class `MSStorageDriver_FailurePredictStatus` | property `PredictFailure` (bool); `true` means the drive predicts failure |

WMI access goes through `IWbemLocator` (CLSID `{4590F811-1D3A-11D0-891F-00AA004B2E24}`, IID `{DC12A687-737F-11CF-884D-00AA004B2E24}`) → `ConnectServer` → `IWbemServices::ExecQuery` → `IEnumWbemClassObject::Next` → `IWbemClassObject::Get`.

- [ ] **Step 1: Write the file header first, and make it truthful**

```go
//go:build windows

package health

// The posture sampler on Windows.
//
// THIS FILE HAS NEVER EXECUTED. No Windows machine exists in this project. It
// is verified by `GOOS=windows go vet ./...` and `GOOS=windows go test -c`,
// which prove it TYPE-CHECKS and nothing more. A vtable index off by one, a
// wrong namespace string, or a misread out-parameter compiles cleanly here and
// fails on a customer's machine.
//
// Could not be confirmed, and each would be invisible until a real Windows run:
//
//  1. that the four vtable index constants match the real interfaces;
//  2. that `Win32_EncryptableVolume.GetProtectionStatus` returns 1 for a
//     BitLocker-protected volume on every supported Windows build;
//  3. that the system drive letter obtained from GetSystemDirectory is the
//     right volume to ask about on a machine that boots from a non-C: volume;
//  4. that `productState`'s enabled/up-to-date nibbles are where this code
//     reads them -- Microsoft has never formally documented that bitfield;
//  5. that `INetFwPolicy2` reports the profile types this code enumerates on a
//     domain-joined machine;
//  6. that `MSStorageDriver_FailurePredictStatus` is present on machines whose
//     storage driver does not implement it (it may simply return no instances,
//     which this code treats as `unavailable`);
//  7. that COM initialisation succeeds inside the agent's service host without
//     a prior apartment being set by another capability.
//
// # Why COM and not a shell-out
//
// Not `manage-bde`, not `netsh`, not PowerShell, on any path including ones
// this code does not take. `docs/modules/backup/agent-engine.md` §2 records a
// promise to an EDR team that this agent spawns no processes on Windows: a
// SYSTEM-privileged process spawning a shell is among the loudest behavioural
// signals an endpoint can emit, and these probes run on a schedule, on every
// endpoint, forever. This file spawns nothing.
```

- [ ] **Step 2: Implement the four probes and the dispatch**

**This step specifies interfaces rather than transcribing ~600 lines of COM, and
that is deliberate — read this before starting.** Everywhere else in this plan
the code is given in full. Here it is not, for one reason: none of it can be
verified on this machine, so a transcription would be a long guess wearing the
authority of a plan. What IS given is exact and checkable — namespaces, class
names, CLSIDs, IIDs, property names and the call sequence — plus a worked
in-repo precedent for the mechanics. If any of it turns out to be wrong, say so
rather than making the code match the plan.

Write, in this file:

- a `wmiQueryScalar(namespace, wql, property string) (any, bool)` helper doing the `IWbemLocator` → `ConnectServer` → `ExecQuery` → `Next` → `Get` sequence, returning `ok=false` on any failure or zero instances;
- `probeDiskEncryptionWindows`, `probeAntivirusWindows`, `probeFirewallWindows`, `probeSMARTWindows`, each `probeFunc`-shaped, each returning `unavailable()` on any COM failure;
- `newPlatformPosture()` returning all four in the table.

`probeAntivirusWindows` must apply the `productState` rule from the design:

```go
// productState is a bitfield Microsoft has never formally documented. Only two
// nibbles are attested well enough to act on:
//
//	(state >> 12) & 0x10 != 0  -> real-time protection enabled
//	(state >> 8)  & 0x10 == 0  -> definitions up to date
//
// A value whose high byte is outside the observed range is NOT interpreted:
// §4.1 warns that "AV health" invented from an unclear source "produces a check
// that is confidently wrong", and a confidently wrong `true` CLEARS an alert.
func avHealthy(state uint32) (healthy bool, known bool) {
	if state == 0 || state>>20 != 0 {
		return false, false
	}
	enabled := (state>>12)&0x10 != 0
	outOfDate := (state>>8)&0x10 != 0
	return enabled && !outOfDate, true
}
```

Return `unavailable()` whenever `known` is false.

- [ ] **Step 3: Verify it compiles for Windows**

```bash
cd agent
GOOS=windows go vet ./internal/capabilities/health/
GOOS=windows go test -c -o /dev/null ./internal/capabilities/health/
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -o /dev/null ./cmd/agent
```
Expected: all succeed.

- [ ] **Step 4: Prove the no-shell claim mechanically**

Run: `grep -n "exec\.\|os/exec" agent/internal/capabilities/health/posture_windows.go`
Expected: no output. If there is any, the header's central claim is false and must be fixed rather than the header softened.

- [ ] **Step 5: Commit**

```bash
git add agent/internal/capabilities/health/posture_windows.go
git commit -m "feat(agent/health): the Windows posture probes via in-process COM (never executed)"
```

---

### Task 8: The four new budget falsifiers

**Files:**
- Create: `agent/internal/capabilities/health/posture_budget_test.go`

**Interfaces:**
- Consumes: `budget.Measure`, `budget.Retained`, `budget.AssertNoGoroutineGrowth` from `agent/internal/budget`; `newPostureSampler`, `execPostureRunner`, `fakeRun` (Task 1).

- [ ] **Step 1: Write the four falsifiers**

```go
package health

// The posture probes' own idle-budget falsifiers.
//
// §0.5 falsifier 3 drives a STAND-IN sampler: it measures that the engine calls
// the posture object only on the sub-tick, which is a property of engine.go,
// not of these probes. These four measure the probes themselves, and they exist
// because the probes SPAWN PROCESSES -- a cost none of the original six covers.
//
// Each is an ordinary Test calling testing.Benchmark, not `go test -bench`,
// for the reason internal/budget records: -bench exits 0 on a regression.

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/budget"
)

func benchSampler() Sampler {
	f := &fakeRun{out: map[string]postureResult{
		"findmnt": {Stdout: "/dev/mapper/vg0-root\n"},
		"lsblk":   {Stdout: "vg0-root lvm\nluks-abc crypt\nvda disk\n"},
	}}
	return newPostureSampler(f.run, map[string]probeFunc{
		"posture.disk_encryption": probeDiskEncryptionLinux,
	})
}

// Falsifier A: a posture pass must retain nothing.
func TestPostureFalsifierA_APassRetainsNothingAcrossAHundredOfThem(t *testing.T) {
	one := budget.Retained(1, func() func() {
		s := benchSampler()
		return func() { s.Sample("posture.disk_encryption", "") }
	})
	hundred := budget.Retained(100, func() func() {
		s := benchSampler()
		return func() { s.Sample("posture.disk_encryption", "") }
	})
	const limit = 4 << 10
	if d := hundred - one; d > limit {
		t.Errorf("posture falsifier A: 1 pass retained %d bytes, 100 retained %d — delta %d exceeds %d. "+
			"§4.3: between ticks, nothing.", one, hundred, d, limit)
	}
	t.Logf("posture falsifier A: retained 1 pass = %d bytes, 100 passes = %d bytes, delta = %d",
		one, hundred, hundred-one)
}

// Falsifier B: no goroutine may outlive a pass.
func TestPostureFalsifierB_APassLeavesNoGoroutineBehind(t *testing.T) {
	r := budget.Measure("posture-pass", func() func() {
		s := benchSampler()
		return func() { s.Sample("posture.disk_encryption", "") }
	})
	budget.AssertNoGoroutineGrowth(t, r, 0)
	t.Logf("posture falsifier B: %d allocs/op, %d B/op, goroutine delta %d over %d iterations",
		r.AllocsPerOp, r.BytesPerOp, r.Goroutines, r.N)
}

// Falsifier C: no child process may outlive a pass.
//
// This one uses the REAL runner, because a fake cannot spawn anything and the
// whole claim is about processes. It runs a command that outlives its own
// deadline and asserts both that the call returns promptly and that nothing is
// left behind -- the WaitDelay discipline asserted rather than assumed.
func TestPostureFalsifierC_NoChildProcessOutlivesAPass(t *testing.T) {
	if _, err := os.Stat("/bin/sh"); err != nil {
		t.Skip("SKIPPING the only real-process proof: /bin/sh is absent, so nothing here spawned a process.")
	}
	before := childPIDs(t)
	start := time.Now()
	// A grandchild that holds the stdout pipe open is exactly the shape
	// patch/exec.go measured at 30.003s before WaitDelay existed.
	res := execPostureRunner(context.Background(), "/bin/sh", "-c", "sleep 60 & wait")
	elapsed := time.Since(start)

	if max := postureDeadline + postureWaitDelay + 3*time.Second; elapsed > max {
		t.Errorf("posture falsifier C: a hung probe took %s, want under %s. cmd.WaitDelay is what bounds it.",
			elapsed, max)
	}
	if res.Err == nil {
		t.Errorf("a command that outlives its deadline returned success: %+v", res)
	}
	after := childPIDs(t)
	if extra := len(after) - len(before); extra > 0 {
		t.Errorf("posture falsifier C: %d child process(es) survived the pass (before %v, after %v)",
			extra, before, after)
	}
	t.Logf("posture falsifier C: a 60s command under a %s deadline returned in %s, children before=%d after=%d",
		postureDeadline, elapsed, len(before), len(after))
}

// childPIDs lists this process's children via /proc. Linux-only; the test skips
// elsewhere because there is no portable equivalent and inventing one would
// measure nothing.
func childPIDs(t *testing.T) []string {
	t.Helper()
	// The children list lives at /proc/self/task/<TID>/children, one file PER
	// THREAD -- there is no /proc/self/task/self/children -- so every thread's
	// file is read and the results unioned. A Go process has many threads and
	// the child may be recorded under any of them.
	files, err := filepath.Glob("/proc/self/task/*/children")
	if err != nil || len(files) == 0 {
		t.Skip("SKIPPING the child-process half: this kernel exposes no children list, " +
			"so nothing in this run checked for a surviving child.")
	}
	seen := map[string]bool{}
	var out []string
	for _, f := range files {
		raw, err := os.ReadFile(f)
		if err != nil {
			continue // a thread that exited between Glob and ReadFile
		}
		for _, pid := range strings.Fields(string(raw)) {
			if !seen[pid] {
				seen[pid] = true
				out = append(out, pid)
			}
		}
	}
	return out
}

// Falsifier D: a concurrent pass must run zero commands.
func TestPostureFalsifierD_TheSecondConcurrentPassRunsZeroCommands(t *testing.T) {
	release := make(chan struct{})
	entered := make(chan struct{})
	var mu sync.Mutex
	var ran []string
	run := func(ctx context.Context, name string, args ...string) postureResult {
		mu.Lock()
		ran = append(ran, name)
		mu.Unlock()
		return postureResult{Stdout: "vg0-root lvm\n"}
	}
	blocking := func(ctx context.Context, r postureRunner, _ string) Sample {
		close(entered)
		<-release
		r(ctx, "held-probe")
		return boolSample(true)
	}
	s := newPostureSampler(run, map[string]probeFunc{"posture.disk_encryption": blocking})

	go func() { s.Sample("posture.disk_encryption", "") }()
	<-entered

	mu.Lock()
	baseline := len(ran)
	mu.Unlock()

	for i := 0; i < 5; i++ {
		if got := s.Sample("posture.disk_encryption", ""); got.Available {
			t.Fatalf("concurrent pass %d = %+v, want unavailable", i, got)
		}
	}
	mu.Lock()
	after := len(ran)
	mu.Unlock()
	if after != baseline {
		t.Errorf("posture falsifier D: five concurrent passes ran %d commands, want 0. "+
			"A health.check.now landing on a posture sub-tick must not stack shell-outs.", after-baseline)
	}
	close(release)
	t.Logf("posture falsifier D: 5 concurrent passes ran %d commands", after-baseline)
}
```

- [ ] **Step 2: Run them**

Run: `cd agent && go test ./internal/capabilities/health/ -run 'PostureFalsifier' -count=1 -v`
Expected: PASS, four tests, each logging its measured numbers.

- [ ] **Step 3: Prove falsifier C bites**

Temporarily set `cmd.WaitDelay = 0` in `execPostureRunner` (which disables the bound), re-run falsifier C, and confirm it fails on elapsed time or on a surviving child. Revert and confirm `git diff` for `posture.go` is empty.

- [ ] **Step 4: Confirm the CI budget step picks them up**

Run: `cd agent && go test ./... -count=1 -run 'Falsifier|Idle|Allocat|Accumulat|Dormant|CostsABounded|MagicPacketCosts|Retained|LeakNoGoroutines|OpensNoSocket' 2>&1 | grep -c 'posture falsifier'`
Expected: `4`. The CI idle-budget step uses this exact `-run` pattern, so a falsifier whose name does not match it would never run there.

- [ ] **Step 5: Commit**

```bash
git add agent/internal/capabilities/health/posture_budget_test.go
git commit -m "test(agent/health): four falsifiers for the posture probes' own footprint"
```

---

### Task 9: Correct the prose that says these probes do not exist

**Files:**
- Modify: `agent/internal/capabilities/health/posture.go` (header)
- Modify: `agent/internal/capabilities/health/sampler_other.go:15-18`
- Modify: `docs/modules/endpoint-health-alerting/spec.md` (§4.1 area)
- Modify: `docs/06-roadmap.md`

- [ ] **Step 1: Find every claim that is now false**

Run:
```bash
grep -rn "v1 has no posture probes\|posture.\* check and .disk.smart_ok. reports\|every posture probe" \
  agent/internal/capabilities/health/ docs/
```
Every hit is a claim this cycle has falsified. `posture.go`'s old comment said "v1 has no posture probes. Every `posture.*` check and `disk.smart_ok` reports `unavailable` on every platform" — that is now wrong on all three platforms.

- [ ] **Step 2: Rewrite each one to say what is true**

State, in each place: which probes exist, on which platforms, and what is still `unavailable` and why —
`posture.antivirus_healthy` on Linux and macOS (§4.1: Windows only), `disk.smart_ok` on macOS (`—` in §4.1),
and everything on Windows and macOS being compile-only and never executed.

Do not delete the superseded sentence; quote it as superseded, the way the other corrections in these files do.

- [ ] **Step 3: Verify no stale claim survives**

Run the same grep from Step 1. Every remaining hit must be inside quoted, explicitly-superseded text.

- [ ] **Step 4: Full verification**

```bash
cd agent
gofmt -l .
go vet ./...
go test ./... -race -count=1
GOOS=windows go vet ./... && GOOS=windows go test -c -o /dev/null ./internal/capabilities/health/
GOOS=darwin go vet ./... && GOOS=darwin go test -c -o /dev/null ./internal/capabilities/health/
"$(go env GOPATH)/bin/staticcheck" ./...
```
Expected: gofmt silent, vet clean on all three platforms, all tests pass, staticcheck zero findings.

- [ ] **Step 5: Commit**

```bash
git add agent/internal/capabilities/health/posture.go \
        agent/internal/capabilities/health/sampler_other.go \
        docs/modules/endpoint-health-alerting/spec.md \
        docs/06-roadmap.md
git commit -m "docs(agent/health): the posture probes exist now; say so where it was claimed otherwise"
```
