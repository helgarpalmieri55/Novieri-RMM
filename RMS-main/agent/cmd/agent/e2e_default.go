//go:build !e2e_noconfirm && !e2e_badselftest

package main

// The e2e build tags (agent-update-e2e, spec §11) flip these. A release build
// has both false.
const (
	e2eNoDial      = false
	e2eBadSelfTest = false
)
