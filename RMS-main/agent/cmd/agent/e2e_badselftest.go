//go:build e2e_badselftest

package main

// Binary D of the e2e job: --update-selftest exits 1, so the swap aborts.
const (
	e2eNoDial      = false
	e2eBadSelfTest = true
)
