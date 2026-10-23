//go:build !linux && !windows && !darwin

package inventory

import (
	"context"
	"os"
	"runtime"
)

// Every GOOS that is not one of the three the control plane has a vocabulary
// for.
//
// This file exists so the package COMPILES on such a target rather than failing
// with "undefined: gather", which would be a confusing way to learn that the
// server's os_family pattern is ^(windows|linux|macos)$. The collection itself
// still refuses: collect() gets "" from osFamilyFor and returns ErrNoFacts, so
// the job fails on the endpoint with a message naming the GOOS instead of
// failing server-side with a pydantic pattern error.
//
// None of the six cross-compile targets reaches this file.

func gather(ctx context.Context) hostFacts {
	hostname, _ := os.Hostname()
	return hostFacts{
		Hostname:     hostname,
		Architecture: runtime.GOARCH,
		Components:   nicComponents(newOriginProber(ctx)),
	}
}
