//go:build !windows

package remote

// realHelper exists only where there is a real platform to capture.
func realHelper() int { return 2 }
