//go:build darwin

package backup

// # THIS FILE HAS NEVER EXECUTED
//
// Nothing here has run. No macOS machine exists in this project: there is no
// Mac in CI, no Mac this agent has enrolled, and no APFS container or
// `tmutil`/`mount_apfs` binary anywhere this code has touched. Its
// verification is `GOOS=darwin go vet ./...` and
// `GOOS=darwin go test -c -o /dev/null ./internal/capabilities/backup/` --
// both type-check and compile this file and everything it calls, which is a
// real check (a call to a function that does not exist, or with the wrong
// argument types, fails either one) and a small one: neither runs a single
// line of it. A falsifier for the SCOPE of that check: a mistranslated
// `dispatch(kind)` string -- say, this file asking requireKind for "APFS"
// where snapshotAPFS is actually "apfs" -- type-checks and compiles cleanly
// under both commands and would still be wrong. Nothing here proves
// mount_apfs's actual flags, tmutil's actual stdout format, or that
// /System/Volumes/Data is the right source path on a real machine; the two
// commands above are a build-time check that this code TYPE-CHECKS, not
// evidence that it WORKS.
//
// Everything this file's own logic could get wrong instead lives in
// snapshot_apfs.go, which is built on every non-Windows OS specifically so that logic can
// be exercised by a real `go test` run through the injected snapRunner seam --
// `go test ./internal/capabilities/backup/`, which passes when run locally on
// this project's Linux development container. It is NOT currently being run by
// CI: the agent job in `.github/workflows/ci.yml` exists and covers this
// package, but is red for reasons outside it (a `GOOS=windows go vet ./...`
// failure in internal/capabilities/health, plus staticcheck findings elsewhere
// in the module), and unblocking it is deferred by an explicit decision. So
// the guarantee here is "these tests pass when run", not "a gate stops a
// regression reaching the branch".
//
// This file is deliberately left with nothing to get wrong beyond a kind check
// and a function call: putting logic here instead would mean nobody tests any
// of it until a Mac appears. Its Linux sibling's equivalent two statements ARE
// covered, by snapshot_linux_test.go; there is no way to do the same for this
// one without a Mac.

import "context"

// PlatformSnapshot takes an APFS local snapshot on macOS.
//
// The job id used in the tag is this process's pid: the tag exists so the
// sweep can recognise mountpoints THIS AGENT created, and a pid is enough for
// that. Threading the real job id would change Options.OpenSnapshot's
// signature for no gain.
func PlatformSnapshot(ctx context.Context, kind string, roots []string) (Snapshot, error) {
	if err := requireKind(kind, snapshotAPFS); err != nil {
		return nil, err
	}
	return openAPFS(ctx, execSnapRunner, pidTag(), roots)
}
