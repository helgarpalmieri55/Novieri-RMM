//go:build !windows

package deploy

// locateWinget: winget exists only on Windows.
func locateWinget() (string, bool) { return "", false }
