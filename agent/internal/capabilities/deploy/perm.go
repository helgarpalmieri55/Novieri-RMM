package deploy

import "github.com/toeshee/it-suite/agent/internal/secdir"

// restrictDir makes a job's scratch directory owner-only; see secdir.
func restrictDir(dir string) error { return secdir.Restrict(dir) }
