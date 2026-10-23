package backup

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// A RESTORE WRITES TO THE FILESYSTEM AS ROOT. These are the tests that say what
// it may not write, and every one of them asserts on the FILESYSTEM — that
// nothing exists outside the target — rather than merely on the error that came
// back. An implementation that refuses AND writes would pass a test that only
// checked the return value, and that combination is not hypothetical: the
// natural way to write the loop is to create the file and then validate it.

// plantSentinel creates a directory outside any restore root and returns it,
// along with the paths a hostile manifest would try to reach inside it.
func plantSentinel(t *testing.T) (outside string) {
	t.Helper()
	outside = filepath.Join(t.TempDir(), "outside")
	if err := os.MkdirAll(outside, 0o755); err != nil {
		t.Fatal(err)
	}
	return outside
}

// forgeManifest seals an arbitrary manifest into the store under a chosen
// reference, so a test can present the restore path with a document its own
// writer would never produce.
//
// THE MANIFEST IS ENCRYPTED UNDER THE REPOSITORY KEY, so this is not a
// straw-man input: it is precisely what an attacker who has the repository key
// or who can write to the object store can present, which is the threat the
// path defences exist for. A test that fed the restore path an unencrypted
// document would be testing a code path that cannot be reached.
func forgeManifest(t *testing.T, store *fakeStore, entries []manifestEntry) string {
	t.Helper()
	key, err := decodeRepoKey(testKey)
	if err != nil {
		t.Fatal(err)
	}
	defer key.zero()
	m := &backupManifest{
		SchemaVersion: manifestSchemaVersion,
		Streams:       []filetreeStream{{Type: streamTypeFiletree, Entries: entries}},
	}
	blob, err := sealManifest(key, m)
	if err != nil {
		t.Fatal(err)
	}
	suffix, err := store.Put(context.Background(), testGrant(), "manifests/forged", blob)
	if err != nil {
		t.Fatal(err)
	}
	// Put returns a bare SUFFIX; every caller here feeds the result straight
	// into restoreParamsJSON as manifest_ref, and a restore genuinely
	// receives the FULL reference (server-derived from that suffix), so it
	// is built explicitly here rather than returning the suffix as if it
	// were one.
	return store.prefix + suffix
}

// storeChunk seals one plaintext chunk into the store and returns its id, so a
// forged entry can reference real, decryptable bytes. Without this the restore
// would fail at the chunk fetch and the path defence would never be reached —
// a test that passes for the wrong reason.
func storeChunk(t *testing.T, store *fakeStore, plaintext []byte) string {
	t.Helper()
	key, err := decodeRepoKey(testKey)
	if err != nil {
		t.Fatal(err)
	}
	defer key.zero()
	id := chunkID(newBlake3(), plaintext)
	sealed, err := key.seal(nil, plaintext)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Put(context.Background(), testGrant(), chunkSuffix(id), sealed); err != nil {
		t.Fatal(err)
	}
	return id
}

func fileEntry(p string, id string, size int64) manifestEntry {
	e := newEntry(p, kindFile, 0o644, size, 1_700_000_000)
	e.ChunkIDs = []string{id}
	return e
}

// ---------------------------------------------------------------------------
// Lexical traversal
// ---------------------------------------------------------------------------

func TestAHostileManifestEntryCannotWriteOutsideTheRestoreRoot(t *testing.T) {
	// THE ATTACK: manifest entries whose paths escape the target — the Zip Slip
	// family. Each case is a real, decryptable chunk under a hostile path, so
	// the restore reaches the path check with everything else in working order.
	//
	// FALSIFYING OBSERVATION IS ON THE FILESYSTEM, not on the error: the
	// sentinel directory outside the root must be EMPTY afterwards, and the
	// specific file each case aims at must not exist. An implementation that
	// wrote the file and then returned an error would pass an error-only test.
	outside := plantSentinel(t)
	victim := filepath.Join(outside, "OWNED")

	// The restore root is a sibling of the sentinel, so `..` from inside the
	// root genuinely reaches it. A root under a different tmpdir would make
	// every case fail for the wrong reason.
	base := t.TempDir()
	if err := os.MkdirAll(filepath.Join(base, "outside"), 0o755); err != nil {
		t.Fatal(err)
	}
	victim2 := filepath.Join(base, "outside", "OWNED")

	cases := []struct {
		name string
		path string
		// refuses distinguishes the two correct answers, and the distinction is
		// the design rather than a concession.
		//
		// A MANIFEST PATH IS NORMALLY ABSOLUTE — the writer records
		// `/data/report.docx`, and the server's own tests select on exactly that
		// spelling — so "absolute" cannot mean "hostile" without refusing every
		// restore there is. An absolute path is CONFINED: the leading separator
		// (and any drive letter) is dropped and the rest lands under the root.
		// The security property is the same one either way and it is asserted
		// the same way either way: NOTHING EXISTS OUTSIDE THE ROOT.
		//
		// A `..` component IS hostile, because this package's writer never emits
		// one, so its presence means the manifest or the object store has been
		// modified.
		refuses bool
		why     string
	}{
		{"dot-dot", "../outside/OWNED", true, "the plain relative escape"},
		{"dot-dot-deep", "a/b/../../../outside/OWNED", true,
			"an escape that only appears after the components are resolved"},
		{"absolute", victim, false,
			"an absolute path straight at the sentinel: confined under the root, not refused"},
		{"absolute-with-dotdot", victim + "/../OWNED", true,
			"absolute and relative combined"},
		{"windows-separator", "..\\..\\outside\\OWNED", true,
			"one component on Linux, a traversal on the fleet's primary OS"},
		{"unc", "//server/share/OWNED", true, "a write to another host over SMB"},
		{"nul-byte", "ok\x00/../../outside/OWNED", true,
			"a NUL that a C-string path API would truncate at"},
		{"ads", "ok:stream", true, "an NTFS alternate data stream"},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			store := newFakeStore(testStorageRef)
			id := storeChunk(t, store, []byte("pwned"))
			ref := forgeManifest(t, store, []manifestEntry{fileEntry(c.path, id, 5)})

			root := filepath.Join(base, "root-"+c.name)
			svc := newTestService(store, quiet())
			res, err := svc.restore(context.Background(), testJobID,
				restoreParamsJSON(t, ref, []string{c.path}, root, modeRestore))

			var refusal *jobs.Refusal
			switch {
			case c.refuses && !errors.As(err, &refusal):
				// `refused` and not `failed`: this agent's own writer cannot
				// produce such an entry, so its presence is a standing
				// condition, not a transient one.
				t.Errorf("%s (%s) did not refuse: err=%v result=%+v", c.path, c.why, err, res)
			case !c.refuses && err != nil:
				t.Errorf("%s (%s) was refused and should have been confined: %v",
					c.path, c.why, err)
			}

			// THE ASSERTION THAT MATTERS, and it is on the filesystem: nothing
			// exists outside the root. An implementation that created the file
			// and then returned an error would pass an error-only test.
			for _, v := range []string{victim, victim2} {
				if _, err := os.Lstat(v); err == nil {
					t.Fatalf("%s (%s) created %s — outside the restore root %s",
						c.path, c.why, v, root)
				}
			}
			if left := listAll(t, outside); len(left) != 0 {
				t.Fatalf("%s left %v in the sentinel directory", c.path, left)
			}
			if left := listAll(t, filepath.Join(base, "outside")); len(left) != 0 {
				t.Fatalf("%s left %v in the sibling directory", c.path, left)
			}

			// For the confined case, the file must actually exist INSIDE the
			// root at the mapped location — otherwise this case would also pass
			// if the restore silently did nothing, and it would prove nothing
			// about confinement.
			if !c.refuses {
				want := expectedDest(root, c.path)
				got, err := os.ReadFile(want)
				if err != nil {
					t.Fatalf("the confined absolute path produced no file at %s: %v", want, err)
				}
				if string(got) != "pwned" {
					t.Fatalf("the confined file reads %q, want %q", got, "pwned")
				}
			}
		})
	}
}

func TestTheTraversalFixtureIsNotBlindTheSameEntryWithoutTheEscapeIsWritten(t *testing.T) {
	// FIXTURE-BLINDNESS GUARD for the test above. Every case there could be
	// refused for a reason that has nothing to do with traversal — an
	// unreadable chunk, a malformed manifest, a target that does not exist —
	// and the suite would be green while the defence did nothing.
	//
	// THE MUTATION: `../outside/OWNED` -> `inside/OWNED`. Asserted different
	// before the run. The ONLY difference is the escape.
	//
	// FALSIFYING OBSERVATION: the benign path also failing to be written, which
	// would mean the refusal above proves nothing about traversal.
	const hostile = "../outside/OWNED"
	const benign = "inside/OWNED"
	if hostile == benign {
		t.Fatal("the mutation did not apply")
	}
	if !strings.Contains(hostile, "..") || strings.Contains(benign, "..") {
		t.Fatalf("the mutation is not the escape: %q vs %q", hostile, benign)
	}

	store := newFakeStore(testStorageRef)
	id := storeChunk(t, store, []byte("pwned"))
	ref := forgeManifest(t, store, []manifestEntry{fileEntry(benign, id, 5)})

	root := filepath.Join(t.TempDir(), "root")
	svc := newTestService(store, quiet())
	res, err := svc.restore(context.Background(), testJobID,
		restoreParamsJSON(t, ref, []string{benign}, root, modeRestore))
	if err != nil {
		t.Fatalf("the benign path was refused: %v", err)
	}
	if res.State != stateSuccess || len(res.Mismatch) != 0 {
		t.Fatalf("the benign path did not restore: state=%s mismatch=%v error=%v",
			res.State, res.Mismatch, deref(res.Error))
	}
	written := filepath.Join(root, "inside", "OWNED")
	got, err := os.ReadFile(written)
	if err != nil {
		t.Fatalf("the benign path produced no file at %s: %v", written, err)
	}
	if string(got) != "pwned" {
		t.Fatalf("the benign path restored %q, want %q", got, "pwned")
	}
}

// ---------------------------------------------------------------------------
// Symlink escape — the one a lexical check does not see
// ---------------------------------------------------------------------------

func TestASymlinkedParentInTheManifestCannotBeTraversed(t *testing.T) {
	// THE ATTACK: a manifest containing `data -> <outside>` and then
	// `data/OWNED`. Every path stays under the root lexically at every step;
	// the escape happens in the kernel, when the write follows the link.
	//
	// FALSIFYING OBSERVATION: `<outside>/OWNED` existing afterwards.
	outside := plantSentinel(t)
	victim := filepath.Join(outside, "OWNED")

	store := newFakeStore(testStorageRef)
	id := storeChunk(t, store, []byte("pwned"))
	link := newEntry("data", kindSymlink, 0o777, 0, 1_700_000_000)
	link.Target = &outside
	ref := forgeManifest(t, store, []manifestEntry{
		link,
		fileEntry("data/OWNED", id, 5),
	})

	root := filepath.Join(t.TempDir(), "root")
	svc := newTestService(store, quiet())
	res, err := svc.restore(context.Background(), testJobID,
		restoreParamsJSON(t, ref, []string{"data", "data/OWNED"}, root, modeRestore))
	if err != nil {
		var refusal *jobs.Refusal
		if !errors.As(err, &refusal) {
			t.Fatalf("unexpected error: %v", err)
		}
	}

	if _, err := os.Lstat(victim); err == nil {
		t.Fatalf("the restore wrote %s by following a symlink it created itself; result=%+v",
			victim, res)
	}
	if left := listAll(t, outside); len(left) != 0 {
		t.Fatalf("the restore left %v outside the root", left)
	}
	// `data` must be a real directory in the root — the file was written and
	// the symlink was the thing that failed, not the other way round.
	fi, err := os.Lstat(filepath.Join(root, "data"))
	if err != nil {
		t.Fatalf("nothing was created at the root's `data`: %v", err)
	}
	if fi.Mode()&os.ModeSymlink != 0 {
		t.Fatal("`data` was created as a symbolic link pointing outside the root; the two-pass " +
			"ordering did not hold")
	}
}

func TestASymlinkPlantedInTheTargetBeforeTheRestoreIsRefusedNotFollowed(t *testing.T) {
	// The same escape, but planted by something ELSE on the machine rather than
	// by the manifest — a local user who can write inside the restore target and
	// waits for a restore to run as root. The two-pass ordering does nothing
	// here; the Lstat-every-component check is what does.
	//
	// FALSIFYING OBSERVATION: the file landing at the symlink's destination.
	outside := plantSentinel(t)
	victim := filepath.Join(outside, "OWNED")

	root := filepath.Join(t.TempDir(), "root")
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(root, "data")); err != nil {
		t.Fatal(err)
	}

	store := newFakeStore(testStorageRef)
	id := storeChunk(t, store, []byte("pwned"))
	ref := forgeManifest(t, store, []manifestEntry{fileEntry("data/OWNED", id, 5)})

	svc := newTestService(store, quiet())
	_, err := svc.restore(context.Background(), testJobID,
		restoreParamsJSON(t, ref, []string{"data/OWNED"}, root, modeRestore))
	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Errorf("a pre-planted symlinked parent was not refused: %v", err)
	}
	if _, err := os.Lstat(victim); err == nil {
		t.Fatalf("the restore wrote %s through a symlink planted in the target", victim)
	}
	if left := listAll(t, outside); len(left) != 0 {
		t.Fatalf("the restore left %v outside the root", left)
	}
}

func TestADestinationThatIsAlreadyASymlinkIsRefusedRatherThanTruncated(t *testing.T) {
	// The narrowest form: the FILE itself, not a parent, is already a symlink.
	// O_TRUNC through it writes to whatever it points at.
	//
	// FALSIFYING OBSERVATION: the pointed-at file's contents changing.
	outside := plantSentinel(t)
	victim := filepath.Join(outside, "important.conf")
	if err := os.WriteFile(victim, []byte("original\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	root := filepath.Join(t.TempDir(), "root")
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(victim, filepath.Join(root, "f")); err != nil {
		t.Fatal(err)
	}

	store := newFakeStore(testStorageRef)
	id := storeChunk(t, store, []byte("pwned"))
	ref := forgeManifest(t, store, []manifestEntry{fileEntry("f", id, 5)})

	svc := newTestService(store, quiet())
	_, err := svc.restore(context.Background(), testJobID,
		restoreParamsJSON(t, ref, []string{"f"}, root, modeRestore))
	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Errorf("a destination that was already a symlink was not refused: %v", err)
	}
	got, err := os.ReadFile(victim)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "original\n" {
		t.Fatalf("the file the symlink pointed at now reads %q; the restore wrote through it", got)
	}
}

// ---------------------------------------------------------------------------
// Modes, targets and the verify's scratch
// ---------------------------------------------------------------------------

func TestAVerifyCarryingATargetPathIsRefused(t *testing.T) {
	// api.py::verify_point sends `target={}` and its docstring says a verify
	// "never lands files at any caller-chosen destination". A verify is
	// dispatched with NO operator choice of destination, so honouring one would
	// turn the unprivileged-destination mode into a write-anywhere primitive.
	//
	// THE MUTATION: target={} -> target={"path": <dir>}. The only change.
	//
	// FALSIFYING OBSERVATION: the job being accepted, or anything appearing at
	// the caller-chosen path.
	store := newFakeStore(testStorageRef)
	id := storeChunk(t, store, []byte("data!"))
	ref := forgeManifest(t, store, []manifestEntry{fileEntry("f", id, 5)})
	chosen := filepath.Join(t.TempDir(), "caller-chosen")

	svc := newTestService(store, quiet())

	// Unmutated: accepted.
	if _, err := svc.restore(context.Background(), testJobID,
		restoreParamsJSON(t, ref, nil, "", modeVerify)); err != nil {
		t.Fatalf("a verify with target={} was refused: %v", err)
	}

	// Mutated: refused.
	_, err := svc.restore(context.Background(), testJobID, restoreParamsJSON(t, ref, nil, chosen, modeVerify))
	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Fatalf("a verify carrying target.path=%q was accepted: %v", chosen, err)
	}
	if _, err := os.Lstat(chosen); err == nil {
		t.Fatalf("the refused verify created %s anyway", chosen)
	}
}

func TestARestoreWithARelativeTargetIsRefused(t *testing.T) {
	// A relative target resolves against the service's working directory, which
	// on a Windows service is commonly C:\Windows\System32.
	//
	// FALSIFYING OBSERVATION: acceptance, or a directory appearing under the
	// test process's own working directory.
	store := newFakeStore(testStorageRef)
	id := storeChunk(t, store, []byte("data!"))
	ref := forgeManifest(t, store, []manifestEntry{fileEntry("f", id, 5)})

	svc := newTestService(store, quiet())
	_, err := svc.restore(context.Background(), testJobID,
		restoreParamsJSON(t, ref, []string{"f"}, "relative-target", modeRestore))
	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Fatalf("a relative target.path was accepted: %v", err)
	}
	if _, err := os.Lstat("relative-target"); err == nil {
		_ = os.RemoveAll("relative-target")
		t.Fatal("a directory was created relative to the process working directory")
	}
}

func TestAnEntryWithAWindowsDeviceNameIsSkippedAndReportedNotRefused(t *testing.T) {
	// `CON` on Windows is the console device in every directory; restoring a
	// file with that name writes to it. But a macOS source tree CAN contain
	// one, so refusing the whole job would cost a customer their entire restore
	// over one oddly-named file. It is skipped and named in `mismatch`.
	//
	// FALSIFYING OBSERVATION: a refusal (too harsh), or a silent skip with an
	// empty mismatch (which reads as a clean restore).
	store := newFakeStore(testStorageRef)
	id := storeChunk(t, store, []byte("data!"))
	ref := forgeManifest(t, store, []manifestEntry{
		fileEntry("CON", id, 5),
		fileEntry("trailing-dot.", id, 5),
		fileEntry("ordinary.txt", id, 5),
	})

	root := filepath.Join(t.TempDir(), "root")
	svc := newTestService(store, quiet())
	res, err := svc.restore(context.Background(), testJobID,
		restoreParamsJSON(t, ref, []string{"CON", "trailing-dot.", "ordinary.txt"}, root, modeRestore))
	if err != nil {
		t.Fatalf("a reserved name refused the whole restore: %v", err)
	}
	if len(res.Mismatch) != 2 {
		t.Fatalf("mismatch = %v, want the two unsafe names", res.Mismatch)
	}
	if _, err := os.Lstat(filepath.Join(root, "CON")); err == nil {
		t.Error("the reserved name was created")
	}
	// The ordinary file in the same manifest still came back — a skip must not
	// take the rest of the restore with it.
	if got, err := os.ReadFile(filepath.Join(root, "ordinary.txt")); err != nil ||
		string(got) != "data!" {
		t.Errorf("the ordinary entry did not restore: %q %v", got, err)
	}
}

func TestAManifestEntryNamingAForgedChunkIDIsRejectedBeforeItBecomesAKey(t *testing.T) {
	// `chunks/{id[:2]}/{id}` with an unvalidated id is an object-store key
	// chosen by whoever wrote the manifest.
	//
	// FALSIFYING OBSERVATION: a Get against a key derived from the forged id.
	store := newFakeStore(testStorageRef)
	e := newEntry("f", kindFile, 0o644, 5, 1)
	e.ChunkIDs = []string{"../../../../etc/passwd"}
	ref := forgeManifest(t, store, []manifestEntry{e})

	root := filepath.Join(t.TempDir(), "root")
	svc := newTestService(store, quiet())
	res, err := svc.restore(context.Background(), testJobID,
		restoreParamsJSON(t, ref, []string{"f"}, root, modeRestore))
	if err != nil {
		t.Fatalf("unexpected refusal: %v", err)
	}
	if len(res.Mismatch) != 1 {
		t.Fatalf("mismatch = %v, want the one bad entry", res.Mismatch)
	}
	// Counted against `chunks/` suffixes only: the manifest fetch is a Get too,
	// and an assertion on the total would have been satisfied by it — which is
	// how this test read "1 object fetched" on its first run and had to be
	// narrowed to the thing it is actually about.
	if store.chunkGets != 0 {
		t.Errorf("the object store was asked for %d chunk object(s) after a forged chunk id; the "+
			"id is validated before it becomes a key", store.chunkGets)
	}
	// The forged id must also produce NO file, not a zero-length one.
	if _, err := os.Lstat(filepath.Join(root, "f")); err == nil {
		t.Error("a file was created for an entry whose chunk id was rejected")
	}
}

// ---------------------------------------------------------------------------
// The key, and the logs
// ---------------------------------------------------------------------------

func TestNeitherSourcePathsNorFileContentsNorTheRepositoryKeyEverReachALogLine(t *testing.T) {
	// CLAUDE.md invariant #3 and the module brief: backed-up data is customer
	// data, and the repository key is a secret. The standard library makes the
	// path half easy to violate by accident — os.Open returns a *PathError whose
	// Error() carries the full filename, and `log.Warn("...", "err", err)` then
	// ships a customer's document name to a SIEM.
	//
	// This drives a WHOLE run and restore, including the failure paths (an
	// unreadable file, an unrestorable entry), at Debug level with everything
	// captured.
	//
	// FALSIFYING OBSERVATION: any of the three markers appearing in any emitted
	// record.
	const secretDir = "ZZmarkerdirZZ"
	const secretFile = "ZZmarkerfileZZ.txt"
	const secretContent = "ZZmarkercontentZZ"

	root := t.TempDir()
	src := filepath.Join(root, "src", secretDir)
	if err := os.MkdirAll(src, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(src, secretFile), []byte(secretContent), 0o644); err != nil {
		t.Fatal(err)
	}

	cap := &capture{}
	store := newFakeStore(testStorageRef)
	svc := New(Options{
		OpenStore: func(context.Context, Repository) (ObjectStore, error) { return store, nil },
		OpenSnapshot: func(context.Context, string, []string) (Snapshot, error) {
			return passthroughSnapshot{}, nil
		},
		QueryChunks: newFakeDedup(testStorageRef).answer,
		Logger:      slog.New(cap),
	})
	ctx := context.Background()

	// (a) A successful run and restore of a tree whose directory name, file
	//     name and contents are all markers.
	res, err := svc.run(ctx, testJobID, runParamsJSON(t, []string{filepath.Join(root, "src")}, nil))
	if err != nil {
		t.Fatalf("backup.run refused: %v", err)
	}
	if res.State != stateSuccess {
		t.Fatalf("backup.run failed: %v", deref(res.Error))
	}
	dest := filepath.Join(t.TempDir(), "restored")
	// res.ManifestRef is the bare suffix Put returned; a restore genuinely
	// receives the full, server-derived reference, so it is built explicitly
	// here (dispatch_test.go's convention) rather than fed straight through.
	if _, err := svc.restore(ctx, testJobID, restoreParamsJSON(t, store.prefix+*res.ManifestRef,
		[]string{toManifestPath(filepath.Join(root, "src"))}, dest, modeRestore)); err != nil {
		t.Fatalf("backup.restore refused: %v", err)
	}

	// (b) THE PATH THAT MATTERS MOST, driven deterministically: a source root
	//     the machine does not have. os.Lstat returns a *PathError whose
	//     Error() is "lstat <the whole marker path>: no such file or
	//     directory", and run() logs it. Without redact(), this single line
	//     puts a customer's directory name in a log file that ships to a SIEM.
	//
	//     Deterministic on purpose. The first version of this test made a file
	//     mode 0000 and relied on the read failing — which it does not when the
	//     tests run as root, as they do in this container, so the whole
	//     assertion measured an empty log.
	missing := filepath.Join(root, "no-such", secretDir, secretFile)
	miss, err := svc.run(ctx, testJobID, runParamsJSON(t, []string{missing}, nil))
	if err != nil {
		t.Fatalf("a missing source root refused rather than failing: %v", err)
	}
	if miss.State != stateFailed {
		t.Fatalf("a missing source root produced state=%s", miss.State)
	}
	// The RESULT DOCUMENT is read by an operator and stored in
	// `backup_runs.error`, so it is held to the same rule as a log line.
	if body := deref(miss.Error); strings.Contains(body, secretDir) ||
		strings.Contains(body, secretFile) {
		t.Errorf("the failure reported to the control plane names a source path: %q", body)
	}

	// (c) A restore entry that cannot be created, so the per-entry warning path
	//     runs too.
	badRef := forgeManifest(t, store, []manifestEntry{
		fileEntry(toManifestPath(filepath.Join(root, secretDir, "CON")), "", 0),
	})
	if _, err := svc.restore(ctx, testJobID, restoreParamsJSON(t, badRef,
		[]string{toManifestPath(filepath.Join(root, secretDir, "CON"))},
		filepath.Join(t.TempDir(), "d2"), modeRestore)); err != nil {
		t.Fatalf("unexpected refusal: %v", err)
	}

	logs := cap.all()
	if strings.TrimSpace(logs) == "" {
		t.Fatal("no records were emitted; this test would pass while measuring nothing")
	}
	// The OS's own words for the missing root, whatever this OS calls it:
	// "no such file or directory" here, "The system cannot find the path
	// specified." on Windows. Taken from the same failure run() hit.
	_, lerr := os.Lstat(missing)
	var pe *os.PathError
	if !errors.As(lerr, &pe) {
		t.Fatalf("lstat of the missing root returned %v, not a *PathError", lerr)
	}
	if !strings.Contains(logs, pe.Err.Error()) {
		t.Fatalf("the missing-source-root warning (%q) was not emitted, so the *PathError "+
			"redaction path never ran. Captured records:\n%s", pe.Err.Error(), logs)
	}
	for _, marker := range []struct{ what, s string }{
		{"a source directory name", secretDir},
		{"a source file name", secretFile},
		{"a file's contents", secretContent},
		{"the repository key", testKey},
	} {
		if strings.Contains(logs, marker.s) {
			t.Errorf("%s (%q) appears in a log record. Captured records:\n%s",
				marker.what, marker.s, logs)
		}
	}
	// And the key's raw bytes, not only its base64 spelling.
	rawKey, _ := base64.StdEncoding.DecodeString(testKey)
	if strings.Contains(logs, string(rawKey)) {
		t.Error("the repository key's raw bytes appear in a log record")
	}
	t.Logf("captured %d bytes of log across a full run and restore, containing none of the "+
		"four markers", len(logs))
}

func TestTheLogRedactionFixtureIsNotBlindAnUnredactedErrorDoesCarryThePath(t *testing.T) {
	// FIXTURE-BLINDNESS GUARD for the test above. If os.Open's error did not
	// contain the path in the first place, that test would pass with redact()
	// deleted.
	//
	// THE MUTATION: redact(err) -> err. Asserted to produce a DIFFERENT string,
	// and the unredacted one is asserted to contain the path.
	dir := t.TempDir()
	missing := filepath.Join(dir, "ZZmarkerZZ.txt")
	_, err := os.Open(missing)
	if err == nil {
		t.Fatal("opening a missing file succeeded")
	}
	raw := err.Error()
	red := redact(err).Error()
	if raw == red {
		t.Fatalf("redact() returned the error unchanged: %q", red)
	}
	if !strings.Contains(raw, "ZZmarkerZZ") {
		t.Fatalf("the unredacted error does not contain the path (%q); the guard above proves "+
			"nothing", raw)
	}
	if strings.Contains(red, "ZZmarkerZZ") {
		t.Fatalf("redact() left the path in: %q", red)
	}
	// The useful part is the operation and the OS's reason, in the OS's own
	// words -- which are not the same words on Windows as on Linux.
	var pe *os.PathError
	if !errors.As(err, &pe) {
		t.Fatalf("os.Open returned %T, not a *PathError", err)
	}
	if want := pe.Op + ": " + pe.Err.Error(); red != want {
		t.Fatalf("redact() = %q, want %q: the operation and the reason, and nothing else", red, want)
	}
	// And a caller can still tell what kind of failure it was.
	if !errors.Is(redact(err), fs.ErrNotExist) {
		t.Fatalf("redact(%q) no longer reports fs.ErrNotExist", raw)
	}
}

func TestRedactionStripsANestedPathAndOneWrappedInAMessage(t *testing.T) {
	// A *PathError reached through fmt.Errorf("%w") carries the path in the
	// wrapper's text too, and a *PathError can wrap another. Both must go.
	inner := &os.PathError{Op: "CreateFile", Path: `C:\Users\ZZmarkerZZ\x`, Err: fs.ErrPermission}
	outer := &os.PathError{Op: "open", Path: `C:\Users\ZZmarkerZZ`, Err: inner}
	for _, err := range []error{
		outer,
		fmt.Errorf("reading C:\\Users\\ZZmarkerZZ: %w", inner),
		&os.LinkError{Op: "rename", Old: "/ZZmarkerZZ/a", New: "/ZZmarkerZZ/b", Err: inner},
	} {
		red := redact(err)
		if strings.Contains(red.Error(), "ZZmarkerZZ") {
			t.Errorf("redact(%q) = %q; the path survived", err, red)
		}
		if !errors.Is(red, fs.ErrPermission) {
			t.Errorf("redact(%q) = %q; the reason no longer unwraps", err, red)
		}
	}
}

func TestTheRepositoryKeyIsZeroedAndNeverReachesTheResultDocument(t *testing.T) {
	// Invariant #3: job-scoped, decrypt-at-use, and it does not outlive the job.
	//
	// FALSIFYING OBSERVATION: a non-zero byte left in the decoded key after
	// zero(), or the key's spelling anywhere in the marshalled result.
	k, err := decodeRepoKey(testKey)
	if err != nil {
		t.Fatal(err)
	}
	raw := k.raw // aliases the same array
	nonZero := 0
	for _, b := range raw {
		if b != 0 {
			nonZero++
		}
	}
	if nonZero == 0 {
		t.Fatal("the test key is all zeroes; zeroing it would prove nothing")
	}
	k.zero()
	for i, b := range raw {
		if b != 0 {
			t.Fatalf("byte %d of the repository key is %#x after zero()", i, b)
		}
	}
	// A sealed operation after zeroing must fail rather than silently use a
	// zero key, which would encrypt every chunk under a key an attacker knows.
	if _, err := k.seal(nil, []byte("x")); err == nil {
		t.Fatal("seal() succeeded after the key was zeroed")
	}

	// And the key never appears in a result document.
	src := buildFixture(t)
	store := newFakeStore(testStorageRef)
	svc := newTestService(store, quiet())
	res, err := svc.run(context.Background(), testJobID, runParamsJSON(t, []string{src}, nil))
	if err != nil {
		t.Fatal(err)
	}
	body, err := json.Marshal(res)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(body), testKey) {
		t.Fatal("the repository key appears in the backup.run result document")
	}
	if strings.Contains(string(body), "repo_key") {
		t.Fatal("the result document carries a repo_key member")
	}
}

func TestARefusalReasonNeverEchoesAJobSuppliedStringUnbounded(t *testing.T) {
	// A refusal reason lands in the server's HASH-CHAINED AUDIT LOG. An audit
	// record an attacker can write arbitrary bytes into is not an audit record.
	//
	// FALSIFYING OBSERVATION: the whole hostile string, or its control
	// characters, surviving into the reason.
	hostile := "vss\x00\x1b[2Jinjected-newline\ncontrol" + strings.Repeat("A", 500)
	doc := map[string]any{
		"repository_id": "r", "storage_ref": "s", "repo_key": testKey,
		"snapshot": hostile,
		"sources":  map[string]any{"paths": []string{"/data"}},
	}
	raw, _ := json.Marshal(doc)
	_, err := parseRunParams(raw)
	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Fatalf("an out-of-enum snapshot kind was not refused: %v", err)
	}
	if strings.Contains(refusal.Reason, hostile) {
		t.Fatal("the refusal reason echoes the job-supplied string verbatim")
	}
	if strings.ContainsAny(refusal.Reason, "\x00\x1b\n\r") {
		t.Fatalf("the refusal reason carries control characters: %q", refusal.Reason)
	}
	if len(refusal.Reason) > 300 {
		t.Fatalf("the refusal reason is %d bytes; a job-supplied string is bounded before it is "+
			"formatted in", len(refusal.Reason))
	}
}

func TestBothCapabilitiesRefuseWhenThereIsNoRepositoryTransport(t *testing.T) {
	// The production wiring: both seams are nil, and BOTH must be named
	// separately rather than collapsed into one message. They are two different
	// missing pieces now — `QueryChunks` is the dedup answer AND the write
	// grant, `OpenStore` is the thing that spends the grant — and a deployment
	// missing one is not missing the other.
	//
	// FALSIFYING OBSERVATION: anything other than a refusal, or a scratch
	// directory created before the refusal.
	src := buildFixture(t)
	var refusal *jobs.Refusal

	// (a) No exchange at all. This is checked FIRST in run(), before the
	//     snapshot is taken and before a byte is read, because a build that
	//     cannot get a grant can do nothing with what it would read.
	noExchange := New(Options{
		OpenStore: func(context.Context, Repository) (ObjectStore, error) {
			return newFakeStore(testStorageRef), nil
		},
		OpenSnapshot: func(context.Context, string, []string) (Snapshot, error) {
			return passthroughSnapshot{}, nil
		},
		Logger: quiet(),
	})
	_, err := noExchange.run(context.Background(), testJobID, runParamsJSON(t, []string{src}, nil))
	if !errors.As(err, &refusal) {
		t.Fatalf("backup.run did not refuse without the dedup exchange: %v", err)
	}
	if !strings.Contains(refusal.Reason, "backup_chunk_query") {
		t.Errorf("the refusal does not name the missing exchange: %q", refusal.Reason)
	}

	// (b) An exchange but no transport to spend the grant on.
	noStore := New(Options{
		OpenSnapshot: func(context.Context, string, []string) (Snapshot, error) {
			return passthroughSnapshot{}, nil
		},
		QueryChunks: newFakeDedup(testStorageRef).answer,
		Logger:      quiet(),
	})
	_, err = noStore.run(context.Background(), testJobID, runParamsJSON(t, []string{src}, nil))
	if !errors.As(err, &refusal) {
		t.Fatalf("backup.run did not refuse without a transport: %v", err)
	}
	if !strings.Contains(refusal.Reason, "grant") {
		t.Errorf("the refusal does not name what authorizes a write: %q", refusal.Reason)
	}

	_, err = noStore.restore(context.Background(), testJobID,
		restoreParamsJSON(t, "s3://x/y", []string{"/a"}, filepath.Join(t.TempDir(), "d"), modeRestore))
	if !errors.As(err, &refusal) {
		t.Fatalf("backup.restore did not refuse without a transport: %v", err)
	}
}

func TestTheProductionSnapshotProviderRefusesRatherThanReadingTheLiveFilesystem(t *testing.T) {
	// The stated gap. A build with no VSS must NOT quietly fall back to reading
	// the live filesystem: that produces a backup point full of torn files that
	// reports success, which is worse than no backup point at all because it
	// stops anyone looking.
	//
	// FALSIFYING OBSERVATION: defaultOpenSnapshot returning a usable Snapshot
	// for any of the three kinds.
	for _, kind := range []string{snapshotVSS, snapshotAPFS, snapshotLVM} {
		snap, err := defaultOpenSnapshot(context.Background(), kind, []string{"/data"})
		var refusal *jobs.Refusal
		if !errors.As(err, &refusal) {
			t.Errorf("defaultOpenSnapshot(%q) returned %v, %v; a build with no snapshot provider "+
				"must refuse, not read the live filesystem", kind, snap, err)
		}
		if snap != nil {
			t.Errorf("defaultOpenSnapshot(%q) returned a usable snapshot", kind)
		}
	}
	if _, err := defaultOpenSnapshot(context.Background(), snapshotVSS, nil); err == nil ||
		!strings.Contains(err.Error(), "volume shadow copy") {
		t.Error("the VSS refusal does not name the volume shadow copy service; the module brief " +
			"names that exact wording as the `refused` case")
	}
}
