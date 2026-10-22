//go:build e2e_noconfirm

package main

// Binary C of the e2e job: starts, never dials, so it can never confirm and
// its own deadline must roll it back.
const (
	e2eNoDial      = true
	e2eBadSelfTest = false
)
