//go:build !windows

package remote

// Supported reports whether this build can share a screen. Only Windows has
// a helper in phase 1; macOS (phase 4) and Linux X11 (phase 5) follow.
const Supported = false

// DefaultLauncher is nil where there is no helper.
func DefaultLauncher() Launcher { return nil }
