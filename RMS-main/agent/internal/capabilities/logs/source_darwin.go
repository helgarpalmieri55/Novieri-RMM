//go:build darwin

package logs

import (
	"context"
	"os/exec"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// THE MACOS UNIFIED LOG.
//
// # NEVER EXECUTED IN THIS REPOSITORY
//
// The process launch below has run nowhere. Everything downstream of it -- the
// NDJSON stream decode, the predicate-to-taxonomy mapping, the username
// extraction, the uid derivation -- is in unified.go and is unit-tested on
// Linux against fixture lines. The retrieval is unverified until it runs on a
// real Mac.
//
// # This is the ONE process this capability spawns, on either platform
//
// macOS has no query interface to the unified log outside the OSLog Swift/ObjC
// framework, which needs cgo -- and cgo would cost this agent its single static
// cross-compiled binary (CLAUDE.md: "Go, single static binary, minimal
// dependencies"). `/usr/bin/log` is Apple's documented interface and is what
// every macOS management tool uses. So: one process, and these five properties,
// each of which is load-bearing:
//
//  1. ARGV, NEVER A SHELL. No `sh -c`, so there is no word splitting and no
//     metacharacter to escape. Nothing job-supplied reaches an argument in any
//     case -- LogsShipParams is empty and this package never reads a params
//     member -- but the predicate is a string this file composes, and composing
//     it into a shell command line would be the one place that could ever stop
//     being true.
//  2. `cmd.WaitDelay`, WHICH IS NOT OPTIONAL AND WAS MEASURED. Cancelling the
//     context kills the process, but Wait then blocks until the stdout pipe
//     closes, and a grandchild holding that pipe keeps it open. Measured in
//     this module: `sh -c "sleep 30"` under a 200 ms deadline took 30.003 s
//     before WaitDelay existed (internal/capabilities/inventory/exec.go, and
//     TestRunIsBoundedByTheJobsContext asserts on elapsed time because of it).
//     `log show` over a large store is exactly that case.
//  3. STREAMED, NOT BUFFERED. The output is decoded from the stdout PIPE one
//     line at a time. `cmd.Output()` would hold the whole result in memory, and
//     a week of authentication records is not a small string.
//  4. A NARROW PREDICATE AND A START TIME, so the OS does the filtering. The
//     cursor becomes `--start`, which is what stops each job from re-reading
//     the whole store.
//  5. NO STDIN. A tool that decides to prompt gets EOF instead of holding the
//     job open until its deadline.

// platformSource returns the macOS source.
func platformSource() (Source, error) { return macSource{}, nil }

type macSource struct{}

func (macSource) Name() string { return "macos-unified-log" }

func (macSource) Normalize(rec Record) (wire.SecurityEvent, bool, error) {
	return parseUnifiedLine(rec.Payload, rec.Position)
}

// Rewound is always false for this source, and that is a statement rather than
// a stub.
//
// The Windows cursor is a record id, which RESETS TO 1 when the log is cleared
// -- so a stale cursor there filters out everything forever and the rewind
// check is what keeps the evidence flowing. This cursor is a TIMESTAMP. A
// cleared or rotated unified log does not move time backwards: the same
// `--start` still matches everything after it, and the only effect of a purge
// is that some of what it would have matched is gone. There is nothing for a
// rewind check to detect and nothing for it to fix.
func (macSource) Rewound(context.Context, string) (bool, error) { return false, nil }

// Open starts `log show` from the cursor.
func (macSource) Open(ctx context.Context, after string) (Reader, error) {
	args := []string{
		"show",
		// NDJSON: one JSON object per line, which is what makes the decode
		// streamable. `--style json` emits a single array and would invite
		// someone to unmarshal it whole.
		"--style", "ndjson",
		"--predicate", unifiedPredicate,
		// `--info` includes info-level messages; authentication results are
		// logged at info, not default. Without it the feed is nearly empty.
		"--info",
	}
	if start := unifiedStart(after); start != "" {
		args = append(args, "--start", start)
	} else {
		// No cursor: a bounded first read rather than the whole store. An
		// endpoint enrolling for the first time must not spend its first job
		// reading months of history -- and the retention window the control
		// plane cares about starts when the device is managed.
		args = append(args, "--last", firstRunWindow)
	}

	cmd := exec.CommandContext(ctx, logBinary, args...)
	cmd.Stdin = nil
	cmd.WaitDelay = unifiedWaitDelay
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return newUnifiedReader(stdout, cmd.Wait), nil
}
