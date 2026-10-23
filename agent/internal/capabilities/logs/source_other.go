//go:build !windows && !darwin

package logs

import (
	"errors"
	"runtime"
)

// NO SECURITY LOG SOURCE ON THIS PLATFORM.
//
// The fleet is Windows 11 and macOS (docs/modules/security-logs/spec.md §2 also
// names Linux auditd/journald/pam as a v1 target, and that collector is NOT
// built -- see §10.5). This file is what an agent on any other platform
// answers, and it answers by REFUSING rather than by failing or by silently
// shipping nothing.
//
// The distinction is the one CLAUDE.md and jobs.Refusal draw: `failed` reads to
// the control plane as "something went wrong, try again", and `refused` reads
// as "this agent will not do this". "There is no Windows Event Log here" is
// permanently true, so refusing is the honest answer -- and it is the answer
// that lets an operator see WHY a Linux box in the fleet contributes no
// evidence, instead of watching a job fail on a timer forever.
//
// Note that this file is what CI actually compiles and runs. The two real
// sources (source_windows.go, source_darwin.go) are compiled by the
// cross-platform vet steps and executed nowhere in this repository -- which is
// why the Source seam exists and why every test drives it with a fake.
func platformSource() (Source, error) {
	return nil, errors.New("this build runs on " + runtime.GOOS +
		", which has no security-log collector in this agent. The fleet collectors are the " +
		"Windows Event Log (Security channel) and the macOS unified log; the Linux " +
		"auditd/journald collector named in the security-logs spec is not built. No security " +
		"log was read")
}

// _ keeps the import of runtime honest if the message above is ever rewritten.
var _ = runtime.GOOS
