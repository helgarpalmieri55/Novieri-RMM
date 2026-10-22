//go:build windows

package site

import "syscall"

// enableBroadcast sets SO_BROADCAST on the descriptor before connect.
//
// Winsock's setsockopt takes a SOCKET handle rather than an int fd, which is
// the only difference from the unix build. Windows refuses a sendto() to a
// broadcast address without this option for the same reason Linux does.
//
// NOTHING HERE IS EXECUTED BY ANY TEST. The Linux CI runner cross-compiles and
// vets this file (`GOOS=windows go vet ./...`) and runs none of it, exactly as
// agent/packaging/README.md says of internal/service/service_windows.go. The
// first real execution is on an endpoint.
func enableBroadcast(_, _ string, c syscall.RawConn) error {
	var setErr error
	if err := c.Control(func(fd uintptr) {
		setErr = syscall.SetsockoptInt(syscall.Handle(fd), syscall.SOL_SOCKET, syscall.SO_BROADCAST, 1)
	}); err != nil {
		return err
	}
	return setErr
}
