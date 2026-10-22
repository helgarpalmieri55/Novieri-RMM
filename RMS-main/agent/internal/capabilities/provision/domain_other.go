//go:build !windows && !darwin

package provision

// Directory membership on every platform that is neither of the two fleet
// platforms.
//
// Supported=false, and that is the honest answer rather than a stub: this
// binary also builds for Linux, where "domain-joined" could mean realmd/SSSD,
// Winbind, or nothing at all, and a guess dressed as an observation is worse
// than an absence. run() turns Supported=false into "this endpoint cannot
// report directory membership" and leaves `domain_joined` NULL -- never
// `false`, which would tell an auditor the machine was checked and found
// unjoined.
//
// Note that this file is what CI actually compiles and runs. The two real
// implementations (domain_windows.go, domain_darwin.go) are compiled by the
// cross-platform vet steps and executed nowhere in this repository.
func domainStatus() (DomainStatus, error) { return DomainStatus{}, nil }
