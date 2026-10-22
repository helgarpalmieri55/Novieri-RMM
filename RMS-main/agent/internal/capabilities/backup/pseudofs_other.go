//go:build !linux

package backup

func isPseudoFS(string) bool { return false }
