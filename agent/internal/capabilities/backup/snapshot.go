package backup

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// snapTagPrefix is the literal marker on every snapshot this agent creates.
//
// It is an EXACT string, not a convention: the orphan sweep removes anything
// carrying it, so a sweep matching a wider pattern than it creates would delete
// a volume the agent does not own.
const snapTagPrefix = "toeshee-snap-"

// snapMountRoot is where the agent mounts snapshots it takes. The sweep
// reclaims mountpoints under this directory and nowhere else.
const snapMountRoot = "/var/lib/toeshee-agent/snap"

// btrfsSnapDir is the directory name a btrfs snapshot is created under, inside
// the subvolume it snapshots. The walk skips it, on every platform, because a
// source tree can carry one from a Linux host.
const btrfsSnapDir = ".itms-snap-"

type snapResult struct {
	Stdout   string
	Stderr   string
	ExitCode int
	Err      error
}

func (r snapResult) ok() bool { return r.Err == nil }

// snapRunner is the injected seam the shell-out providers take, so LVM's and
// APFS's decision-making is unit-tested on a machine that has neither.
//
// patch's `runner` is the same shape but unexported in its own package. It is
// duplicated here rather than shared, per the convention stated at
// internal/capabilities/netsweep/netsweep_test.go:1240.
type snapRunner func(ctx context.Context, env []string, name string, args ...string) snapResult

// maxSnapCommandOutput caps what one snapshot-provider command may contribute.
//
// Duplicated from patch/exec.go's maxCommandOutput rather than shared, for the
// same reason snapRunner is duplicated: rule 4 of that file's four shell-out
// rules -- "a hostile or broken tool that writes forever costs a bounded
// buffer rather than the agent's whole memory budget" -- applies here exactly
// as written, and importing it is not possible because it is unexported in
// package patch.
const maxSnapCommandOutput = 4 << 20 // 4 MiB

func execSnapRunner(ctx context.Context, extraEnv []string, name string, args ...string) snapResult {
	path, err := exec.LookPath(name)
	if err != nil {
		return snapResult{ExitCode: -1, Err: err}
	}
	cmd := exec.CommandContext(ctx, path, args...)
	if len(extraEnv) > 0 {
		cmd.Env = append(os.Environ(), extraEnv...)
	}

	// WaitDelay is NOT OPTIONAL, and this was measured on this project rather
	// than reasoned about (patch/exec.go and
	// internal/capabilities/inventory/exec.go independently record the same
	// finding). exec.CommandContext's cancellation kills only the direct
	// child; cmd.Wait then blocks until the stdout pipe closes, and a
	// GRANDCHILD that inherited that pipe keeps it open. A 200ms deadline took
	// 30.003s before this line existed.
	//
	// This seam is what all three snapshot providers shell out through,
	// INCLUDING TEARDOWN. A hung `lvremove` would hold a backup job open past
	// its own expires_at while the snapshot it failed to remove keeps
	// consuming copy-on-write space -- the exact hazard Release's doc comment
	// above warns about.
	cmd.WaitDelay = 10 * time.Second

	var out, errb snapBoundedBuffer
	out.limit, errb.limit = maxSnapCommandOutput, maxSnapCommandOutput
	cmd.Stdout, cmd.Stderr = &out, &errb
	runErr := cmd.Run()
	res := snapResult{Stdout: out.String(), Stderr: errb.String(), Err: runErr}
	var ee *exec.ExitError
	switch {
	case runErr == nil:
		res.ExitCode = 0
	case errors.As(runErr, &ee):
		res.ExitCode = ee.ExitCode()
	default:
		// Not a clean non-zero exit: failed to start, killed by WaitDelay,
		// or some other non-ExitError failure. A caller reading ExitCode
		// without also checking Err would otherwise misread this as success.
		res.ExitCode = -1
	}
	return res
}

// snapBoundedBuffer is an io.Writer that stops accepting after limit bytes.
//
// Duplicated from patch/exec.go's boundedBuffer rather than shared, per the
// convention stated at internal/capabilities/netsweep/netsweep_test.go:1240.
// A cap enforced by truncating AFTERWARDS is not a cap: the memory was
// already allocated. This one refuses the write, so a tool that decides to
// print forever costs a fixed 4 MiB and not the agent's residency claim.
type snapBoundedBuffer struct {
	buf   bytes.Buffer
	limit int
}

func (b *snapBoundedBuffer) Write(p []byte) (int, error) {
	n := len(p)
	if room := b.limit - b.buf.Len(); room > 0 {
		if len(p) > room {
			p = p[:room]
		}
		b.buf.Write(p)
	}
	// io.Writer requires n == len(p) whenever err == nil (see the io.Writer
	// doc comment). Reporting the TRUNCATED length here is a short write in
	// disguise: exec.Cmd copies a non-*os.File Stdout/Stderr through an
	// internal io.Copy goroutine, and io.Copy treats n < len(p) with a nil
	// error as its own bug and returns "short write" -- which cmd.Run()
	// surfaces as a process failure even though the process ran and answered
	// correctly. So the ORIGINAL length is always reported; the buffer keeps
	// only the HEAD of the output up to limit and silently drops the rest.
	return n, nil
}

func (b *snapBoundedBuffer) String() string { return b.buf.String() }

// requireKind refuses a snapshot kind that does not match this platform.
//
// The kind arrives inside a signed job. Silently substituting the locally
// correct mechanism would discard a signed instruction; refusing puts the
// server's misconfiguration in the job record where an operator sees it.
func requireKind(got, want string) error {
	if got == want {
		return nil
	}
	return jobs.Refuse("backup.run: this endpoint provides the %s snapshot mechanism, "+
		"but the job asked for %s", want, clean(got))
}

// # How a snapshot provider names a root it will not accept
//
// BY INDEX, NEVER BY PATH. A refusal reason travels into the server's
// hash-chained audit log (see clean's doc comment in wire.go), and an
// operational failure's text travels into `backup_runs.error` and thence into
// reports. A source path is customer data by CLAUDE.md's reading of this
// module -- it is the same rule redact() enforces for log lines and
// security_test.go's
// TestNeitherSourcePathsNorFileContentsNorTheRepositoryKeyEverReachALogLine
// asserts over a whole run. Every pre-existing jobs.Refuse in this package is
// path-free; the snapshot providers hold to that by identifying a root by its
// position in the job's sorted, de-duplicated source list, which is enough for
// an operator to tell WHICH root was rejected without the audit log learning a
// customer's directory names.
//
// Device names, mount points and command stderr are held to the same rule for
// the same reason: `findmnt` and `mount` echo the path they were given.

// rootRelative returns root's path relative to the mount point it lives on,
// with NO leading separator, or ok=false when root is not under target.
//
// This is the whole of C1. A volume snapshot presents the FILESYSTEM's root,
// not the job's root: a root of /home/alice/docs on a filesystem mounted at
// /home appears inside the snapshot's mount at <mount>/alice/docs, and
// translating it to <mount> instead backs up the entire snapshotted filesystem
// under the name of one subdirectory. verifyPresents cannot catch that -- it
// checks existence and non-emptiness, and the wrong tree is both.
//
// It is snapshot_windows.go's volumeRelative for POSIX, deliberately including
// that function's path-BOUNDARY rule: a mount point of /mnt/data must not
// swallow /mnt/database. It differs in two ways, both because the platforms
// do: the comparison is case-SENSITIVE, and the result carries no leading
// separator because its only consumer is filepath.Join rather than a
// concatenation onto a `\\?\` device name.
//
// POSIX `path` semantics, not `filepath`: a mount table is POSIX on every
// platform that has one here (LVM, btrfs, ZFS, APFS), so the answer must not
// depend on which OS runs the test.
func rootRelative(root, target string) (string, bool) {
	const sep = "/"
	r := path.Clean(root)
	t := strings.TrimRight(path.Clean(target), sep)
	if len(r) < len(t) || r[:len(t)] != t {
		return "", false
	}
	rest := r[len(t):]
	if rest != "" && !strings.HasPrefix(rest, sep) {
		return "", false
	}
	return strings.TrimPrefix(rest, sep), true
}

// validateRoots refuses a root no snapshot provider can honestly map.
//
// It lives in the shared core so all three providers agree: before this,
// snapshot_windows.go's resolveRoots refused a relative root and the two
// shell-out providers did not.
//
// # The leading-dash check is a GUARD, not a fix for a live hazard
//
// An earlier version of this comment and of the refusal reason claimed the
// tools "read a leading-dash root as an OPTION rather than a path". That is
// not true of any call site this package currently has, and the claim was
// checked rather than reasoned about, with real findmnt (util-linux 2.39.3) on
// this project's Linux container:
//
//	$ mkdir -- -dashdir
//	$ findmnt -n -o SOURCE,TARGET --target -dashdir
//	/dev/vda  /
//
// `--target` takes a REQUIRED argument, so getopt consumes the next argv as
// that argument whatever it starts with -- the dash-named directory above
// resolved correctly, and `--target --help` was likewise swallowed as a path
// rather than printing help. The only root this package hands to any tool is
// findmnt's `--target` (snapshot_lvm.go); since the APFS reshape `openAPFS`
// passes no root to `mount_apfs` at all -- it mounts `/System/Volumes/Data` --
// so the other tool the old sentence named never sees one either.
//
// The check STAYS regardless, because it is cheap and it is sound for the case
// that would matter: a future call site that passes a root POSITIONALLY, where
// a leading dash genuinely would be parsed as an option. Refusing here is the
// standing guarantee that no such call site can be introduced without also
// deciding what to do about it.
//
// # Zero roots is refused here, for all three providers
//
// openVSS already guarded this and the other two did not, which cost openAPFS
// a leak: its teardown closure was attached to vols[0] only, so with an empty
// roots slice `vols` was empty, Release was a no-op, and the `mount_apfs` it
// had already run stayed mounted until the next run's sweep found it. Guarding
// in the shared core is stronger than tearing down afterwards -- nothing is
// created in the first place -- and it is the only way all three agree.
//
// wire.go's sourcesSpec.roots already refuses an empty `sources.paths`, so
// this is unreachable through `run`. It is reachable for any direct caller,
// which is exactly what the provider tests are.
//
// restore.go:206 sets the precedent for the absolute-path half ("target.path
// is not absolute. A relative path resolves against the service's working
// directory"). Per the convention above, no reason here names a root.
func validateRoots(roots []string) error {
	if len(roots) == 0 {
		return jobs.Refuse("backup.run: no source roots to snapshot")
	}
	for i, root := range roots {
		if strings.HasPrefix(root, "-") {
			return jobs.Refuse("backup.run: the source root at index %d begins with '-'. This agent "+
				"will not hand an option-looking path to the tools that resolve and mount a volume, "+
				"where a future positional argument would be parsed as a flag", i)
		}
		if !filepath.IsAbs(root) {
			return jobs.Refuse("backup.run: the source root at index %d is not an absolute path. A "+
				"relative path resolves against the service's working directory, so it cannot be "+
				"mapped onto a volume snapshot", i)
		}
	}
	return nil
}

// snapFailuref builds an OPERATIONAL snapshot failure: something this agent
// tried and could not finish, as opposed to something it will not do.
//
// It is deliberately NOT a jobs.Refusal, and that distinction is the whole
// point. agent-engine.md §9 splits `refused` (a standing incapacity of the
// device -- no provider in this build, the wrong snapshot kind, a root that is
// not on a logical volume) from a `state:"failed"` RESULT DOCUMENT (transient
// or operational -- the volume group is out of space, a mount failed, the
// snapshot presented nothing). A Go error for a transient condition produces a
// FAILED JOB, `ingest_backup_result` never runs, and the `backup_runs` row is
// stuck on `running` forever with no error text anywhere an operator looks.
// run.go's call site is what sorts them, by errors.As into *jobs.Refusal.
func snapFailuref(format string, args ...any) error {
	return fmt.Errorf(format, args...)
}

// snapOptions carries the per-job choices a provider needs beyond kind and
// roots. It travels in the context so Options.OpenSnapshot keeps the signature
// every provider and test already implements.
type snapOptions struct {
	vssType      string // vssTypeCopy or vssTypeFull
	allowLive    bool   // auto may fall back to a live read
	failOnWriter bool   // a failed VSS writer before the read fails the job
}

type snapOptionsKey struct{}

func withSnapOptions(ctx context.Context, o snapOptions) context.Context {
	return context.WithValue(ctx, snapOptionsKey{}, o)
}

// snapOptionsFrom defaults to the safe reading: a copy backup, no live read.
func snapOptionsFrom(ctx context.Context) snapOptions {
	if o, ok := ctx.Value(snapOptionsKey{}).(snapOptions); ok {
		return o
	}
	return snapOptions{vssType: vssTypeCopy, failOnWriter: true}
}

// snapshotDetail is implemented by providers that can say how each root's view
// was produced. mechNone means a live read of the filesystem.
type snapshotDetail interface {
	mechanism(original string) string
}

// snapshotRedirector maps a directory that is itself the origin of another
// snapshotted volume (a nested btrfs subvolume, a child ZFS dataset) to that
// volume's view. Inside the parent's snapshot such a directory is empty.
type snapshotRedirector interface {
	redirect(original string) (string, bool)
}

// writerReporter is implemented by the VSS provider: the writer status at each
// check, and any warning the checks produced. Complete only after Release.
type writerReporter interface {
	writerReport() ([]writerDoc, []string)
}

// volSnapshot is one snapshotted volume: where it came from, where its contents
// now appear, and how to tear it down.
type volSnapshot struct {
	origin     string
	translated string
	teardown   func() error
	mech       string
}

type multiSnapshot struct{ vols []volSnapshot }

func (m *multiSnapshot) mechanism(original string) string {
	for _, v := range m.vols {
		if original == v.origin || strings.HasPrefix(original, v.origin+string(os.PathSeparator)) {
			return v.mech
		}
	}
	return ""
}

func (m *multiSnapshot) redirect(original string) (string, bool) {
	for _, v := range m.vols {
		if original == v.origin {
			return v.translated, true
		}
	}
	return "", false
}

// newMultiSnapshot orders volumes longest-origin-first so Translate resolves
// through the most specific prefix: /data/inner must win over /data.
func newMultiSnapshot(vols []volSnapshot) Snapshot {
	ordered := append([]volSnapshot(nil), vols...)
	sort.Slice(ordered, func(i, j int) bool { return len(ordered[i].origin) > len(ordered[j].origin) })
	return &multiSnapshot{vols: ordered}
}

func (m *multiSnapshot) Translate(original string) string {
	for _, v := range m.vols {
		if original == v.origin {
			return v.translated
		}
		if strings.HasPrefix(original, v.origin+string(os.PathSeparator)) {
			return filepath.Join(v.translated, strings.TrimPrefix(original, v.origin))
		}
	}
	return original
}

// Release tears down every volume and reports the FIRST failure.
//
// It does not stop at the first error: returning early would leave the
// remaining snapshots mounted and, on LVM, consuming copy-on-write space until
// they are invalidated.
func (m *multiSnapshot) Release() error {
	var first error
	for _, v := range m.vols {
		if v.teardown == nil {
			continue
		}
		if err := v.teardown(); err != nil && first == nil {
			first = err
		}
	}
	return first
}

// verifyPresents rejects a snapshot that does not actually present the data.
//
// The rule is exact: reject when the live root has children and the snapshot
// root has none. Nothing else rejects -- a smaller count is normal on a running
// system, and a genuinely empty root is fine.
//
// This is what catches a macOS Full Disk Access denial, where mount_apfs
// succeeds and the tree reads empty, so the backup would complete, upload
// almost nothing, and REPORT SUCCESS.
//
// It is NOT the only thing that lands here on macOS, and the reasons below say
// so rather than asserting the TCC cause alone. apfsRelative falls back to `/`
// for a root that is not under the APFS data volume, so a root on a separate
// or external volume (/Volumes/ExternalDisk/photos) translates to an offset
// that, inside a data-volume snapshot, is an empty mount-point directory --
// indistinguishable from a TCC denial at this point in the code. Naming only
// the grant would send an operator to check a setting that was never the
// problem. Both causes are UNVERIFIED in the sense the rest of the macOS path
// is (there is no Mac in this project), which is exactly why the reason must
// not pick one.
//
// What it CANNOT catch is a snapshot that presents a real, populated tree that
// is not the one the root names -- it checks existence and non-emptiness, never
// correspondence. That is C1, and rootRelative is what prevents it; do not read
// a pass here as evidence the translation is right.
//
// Every rejection here is an OPERATIONAL failure (snapFailuref), not a
// refusal: an unreadable root or a snapshot that mounted and presents nothing
// is something this run hit, not a standing incapacity of the device. Per §9
// that belongs in a `state:"failed"` result document. Roots are named by
// index, never by path -- see the convention above.
func verifyPresents(roots []string, s Snapshot) error {
	for i, root := range roots {
		liveN, err := countChildren(root)
		if err != nil {
			return snapFailuref("the source root at index %d could not be read to verify the snapshot", i)
		}
		if liveN == 0 {
			continue
		}
		snapN, err := countChildren(s.Translate(root))
		if err != nil {
			// This branch carries the same macOS hint as the empty-directory
			// one below, and gained it with C1's fix: now that a root
			// translates to its own OFFSET inside the snapshot rather than to
			// the snapshot's mount root, a Full Disk Access denial -- which
			// leaves the mount succeeding and its tree empty -- shows up here,
			// as the offset path not existing, more often than it shows up as
			// an existing-but-empty directory. A root that is not on the data
			// volume at all lands here the same way, via apfsRelative's `/`
			// fallback, which is why the reason names both.
			return snapFailuref("the snapshot does not present the source root at index %d. On macOS "+
				"this has two causes this agent cannot tell apart: a missing Full Disk Access grant, "+
				"or a root that is not on the APFS data volume the snapshot covers", i)
		}
		if snapN == 0 {
			return snapFailuref("the snapshot presents no entries under the source root at index %d, "+
				"but the live filesystem has %d. Backing up from it would report success having "+
				"stored nothing. On macOS this has two causes this agent cannot tell apart: a "+
				"missing Full Disk Access grant, or a root that is not on the APFS data volume the "+
				"snapshot covers", i, liveN)
		}
	}
	return nil
}

func countChildren(dir string) (int, error) {
	f, err := os.Open(dir)
	if err != nil {
		return 0, err
	}
	defer f.Close() //nolint:errcheck
	names, err := f.Readdirnames(-1)
	if err != nil {
		return 0, err
	}
	return len(names), nil
}

// snapMountPath is where one volume's snapshot is mounted for a given tag.
func snapMountPath(tag string, n int) string {
	return filepath.Join(snapMountRoot, fmt.Sprintf("%s%s-%d", snapTagPrefix, tag, n))
}

// pidTag returns this process's pid as a snapshot tag.
//
// The tag exists only so the orphan sweep can recognise snapshots THIS AGENT
// created; a pid is sufficient for that. Threading the real job id through
// instead would mean changing Options.OpenSnapshot's signature, which is
// shared by main.go and all three snapshot providers.
func pidTag() string {
	return strconv.Itoa(os.Getpid())
}
