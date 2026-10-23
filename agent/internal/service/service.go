// Package service is the seam between "a process someone started in a shell"
// and "a service the operating system started".
//
// It exists for exactly one platform. On Linux and macOS a daemon is an
// ordinary process that exits on SIGTERM, which is what systemd and launchd
// send, so Run here is signal.NotifyContext and nothing else. On Windows the
// Service Control Manager will not accept a program that has not called
// StartServiceCtrlDispatcher: `sc.exe create` over a plain console binary
// installs a service that fails to start with error 1053 every time. That
// dispatcher is what service_windows.go provides.
//
// # Why a third dependency
//
// docs/modules/agent/plan.md Task 14 Step 2 asks for `sc.exe` over a console
// binary if it works, on the standing "fewer dependencies" instruction. It does
// not work: the control protocol is not optional, and there is no way to speak
// it from Go without golang.org/x/sys/windows/svc. The dependency is pinned at
// v0.33.0 rather than latest because v0.36.0+ raises the module's `go`
// directive (v0.47.0 demands go >= 1.25.0) and the agent's CI pins Go 1.24 —
// the same trap internal/capabilities/netsweep documents for golang.org/x/net.
// It is imported ONLY from a //go:build windows file, so the Linux and macOS
// binaries neither link it nor change size.
//
// # This is not a command path
//
// The SCM can start and stop this process. It cannot make it run anything:
// invariant #1's only route to a capability is the signed-job runner, and Run
// dispatches nothing. A Stop control cancels a context; that is its whole
// authority.
package service

// Name is the Windows service name, and the label the packaging scripts use for
// the systemd unit and the launchd job. One string, so a rename cannot half
// happen.
const Name = "its-agent"

// DisplayName is what the Windows Services console shows.
const DisplayName = "ITMS Agent"

// Description is the Windows service description. It names the outbound-only
// property because a service with domain-admin-equivalent reach should say what
// it does in the one place an operator will actually read.
const Description = "Toeshee endpoint agent. Maintains one outbound mutual-TLS " +
	"connection to the control plane and executes only cryptographically signed jobs. " +
	"Opens no listening port."
