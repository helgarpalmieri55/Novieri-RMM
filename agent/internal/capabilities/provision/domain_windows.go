//go:build windows

package provision

import (
	"unsafe"

	"golang.org/x/sys/windows"
)

// Windows directory membership, read with ONE library call and no process.
//
// `NetGetJoinInformation` (netapi32.dll) is the documented API behind
// `(Get-WmiObject Win32_ComputerSystem).PartOfDomain` and behind `net config
// workstation`. Calling it directly is the whole point: this agent runs as
// SYSTEM, and a SYSTEM process spawning powershell.exe to ask whether the
// machine is domain-joined is a behaviour on every EDR's watch list and trips
// ASR rules that block child processes of service binaries. There is no reason
// to pay that for a value the OS will hand over through a DLL export.
//
// NEVER EXECUTED IN THIS REPOSITORY'S CI. It is compiled by the
// `GOOS=windows go vet` step and by cross-compilation, and the logic that
// consumes its output (domainMatches, the requested/supported/joined branches
// in run) is fully unit-tested through the Host seam on Linux. What has never
// run is the syscall itself.

// NetSetupJoinStatus values from lmjoin.h.
const (
	netSetupUnknownStatus = 0
	netSetupUnjoined      = 1
	netSetupWorkgroupName = 2
	netSetupDomainName    = 3
)

var (
	netapi32                  = windows.NewLazySystemDLL("netapi32.dll")
	procNetGetJoinInformation = netapi32.NewProc("NetGetJoinInformation")
	procNetApiBufferFree      = netapi32.NewProc("NetApiBufferFree")
)

// domainStatus asks Windows whether this machine is joined to a domain.
//
// The returned name is the NetBIOS domain name when joined, which is why
// domainMatches compares first labels case-insensitively rather than requiring
// equality with the DNS name a profile conventionally carries.
func domainStatus() (DomainStatus, error) {
	var (
		namePtr *uint16
		status  uint32
	)
	// lpServer = NULL: this machine. The buffer is allocated by the API and
	// must be released with NetApiBufferFree, which is what the defer does --
	// a leak here would be a per-job leak on the one code path that runs
	// during provisioning, and provisioning runs once per machine, so it would
	// never be noticed.
	ret, _, _ := procNetGetJoinInformation.Call(
		0,
		uintptr(unsafe.Pointer(&namePtr)),
		uintptr(unsafe.Pointer(&status)),
	)
	if namePtr != nil {
		defer procNetApiBufferFree.Call(uintptr(unsafe.Pointer(namePtr)))
	}
	if ret != 0 {
		// NERR_Success is 0. Anything else means the question could not be
		// answered, and Supported=false is how run() reports "I cannot be
		// asked" rather than the false claim "not joined".
		return DomainStatus{}, windows.Errno(ret)
	}

	name := ""
	if namePtr != nil {
		name = windows.UTF16PtrToString(namePtr)
	}
	switch status {
	case netSetupDomainName:
		return DomainStatus{Supported: true, Joined: true, Domain: name}, nil
	case netSetupUnjoined, netSetupWorkgroupName:
		return DomainStatus{Supported: true, Joined: false, Domain: name}, nil
	case netSetupUnknownStatus:
		fallthrough
	default:
		// Windows itself said it does not know. Reporting that as "not joined"
		// would fail a provisioning run over the OS's own uncertainty.
		return DomainStatus{}, nil
	}
}
