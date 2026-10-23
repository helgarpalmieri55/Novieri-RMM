//go:build !windows

package site

import "syscall"

// enableBroadcast sets SO_BROADCAST on the descriptor before connect.
//
// stdlib `syscall` and not `golang.org/x/sys/unix`, deliberately.
// agent/packaging/README.md records that `golang.org/x/sys` is imported ONLY
// from `//go:build windows` files, which is why the Linux and macOS binaries do
// not link it at all. SO_BROADCAST and SOL_SOCKET are in stdlib `syscall` on
// every unix the agent cross-compiles for, so there is no reason to spend that
// property on this.
func enableBroadcast(_, _ string, c syscall.RawConn) error {
	var setErr error
	if err := c.Control(func(fd uintptr) {
		setErr = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_BROADCAST, 1)
	}); err != nil {
		return err
	}
	return setErr
}
