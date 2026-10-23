//go:build !linux && !darwin && !windows

package patch

import "context"

// Every GOOS the control plane has no os_family vocabulary for.
//
// This file exists so the package COMPILES on such a target rather than failing
// with "undefined: osEngine", which would be a confusing way to learn that
// PatchScanDocument.os_family is a Literal of windows|linux|macos. The
// capability still refuses, on the endpoint, with a message naming the GOOS --
// which is a better place to find out than a pydantic Literal error on the
// server.
//
// None of the agent's cross-compile targets reaches this file.
func osEngine(ctx context.Context) (engine, error) {
	return nil, errNoEngine("the control plane's os_family vocabulary is windows|linux|macos")
}
