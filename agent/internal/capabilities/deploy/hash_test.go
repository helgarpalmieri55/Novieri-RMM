package deploy

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// AD-07, AND THE ONE TEST FILE THIS TASK IS ACTUALLY ABOUT.
//
// "The agent verifies `artifact_sha256` after pull, before execution. A
// mismatch fails the job (never executes)."
//
// # Fixture blindness is what this file is written against
//
// A fixture where the artifact and the expected hash always agree proves
// nothing: it passes identically against an implementation that computes the
// hash and throws it away, and against one that never computes it at all. So
// every test below MUTATES — it takes bytes that DO verify, changes them, and
// CONFIRMS THE MUTATION APPLIED (`assert new != old`) before asserting the
// failure. A mutation that silently did not apply is a green test measuring
// nothing, and it is the specific way this project has been burned before.
//
// And the required failure is not "an error came back". It is A FAILURE NAMING
// THE WRONG VALUE: the installer EXECUTED when it should not have. An error
// can be returned for a dozen reasons; only the spy's call count distinguishes
// "refused to run it" from "ran it and then complained".

// ---------------------------------------------------------------------------
// Apparatus: an artifact whose bytes and hash can be pulled apart
// ---------------------------------------------------------------------------

// artifact is a body plus its true digest. Nothing in this file writes a
// digest by hand: every "correct" hash is COMPUTED from the bytes, so the
// baseline cannot drift into agreement by accident.
type artifact struct {
	body []byte
	sha  string
}

func newArtifact(body string) artifact {
	sum := sha256.Sum256([]byte(body))
	return artifact{body: []byte(body), sha: hex.EncodeToString(sum[:])}
}

func (a artifact) fetcher() Fetcher {
	return func(context.Context, string) (io.ReadCloser, error) {
		return io.NopCloser(bytes.NewReader(a.body)), nil
	}
}

// msiJob is a well-formed artifact-backed deployment for the given artifact.
func msiJob(t *testing.T, a artifact, sha string) (Options, *execSpy, string) {
	t.Helper()
	spy := &execSpy{}
	root := t.TempDir()
	return Options{
		GOOS:        "windows",
		LookPath:    fakePaths("msiexec.exe"),
		Logger:      quiet(),
		Exec:        spy.exec,
		Fetch:       a.fetcher(),
		ScratchRoot: root,
	}, spy, root
}

func msiParams(t *testing.T, sha string) []byte {
	t.Helper()
	return deployParamsJSON(t, map[string]any{
		"artifact_ref":    "https://packages.example/pkg.msi",
		"artifact_sha256": sha,
	})
}

// ---------------------------------------------------------------------------
// The baseline: matching bytes DO execute. Without this the mismatch tests
// below pass for an implementation that never executes anything.
// ---------------------------------------------------------------------------

func TestAnArtifactWhoseBytesMatchTheSignedHashIsExecuted(t *testing.T) {
	a := newArtifact("MZ\x90\x00 this is a perfectly good installer")
	opts, spy, _ := msiJob(t, a, a.sha)
	// The staged file exists only WHILE the installer runs — the scratch
	// directory is removed in a defer — so the contents are read from inside
	// the executor, which is the one moment the installer could have seen them.
	var staged []byte
	inner := opts.Exec
	opts.Exec = func(ctx context.Context, c Command) (Outcome, error) {
		got, err := os.ReadFile(c.Args[1])
		if err != nil {
			t.Errorf("the staged artifact is not readable at execution time: %v", err)
		}
		staged = got
		return inner(ctx, c)
	}

	res, err := New(opts).deploy(context.Background(), msiParams(t, a.sha))
	if err != nil {
		t.Fatalf("a matching artifact was rejected: %v", err)
	}
	runs := spy.calls()
	if len(runs) != 1 {
		t.Fatalf("a matching artifact produced %d spawns, want 1. Every mismatch test in this "+
			"file would pass vacuously against an implementation that never executes", len(runs))
	}
	if res.Outcomes[0].State != stateInstalled {
		t.Fatalf("state = %q, want %q", res.Outcomes[0].State, stateInstalled)
	}
	// A path that exists is not the same as a path with the right contents in
	// it: the bytes the installer was pointed at must be the verified ones.
	if !bytes.Equal(staged, a.body) {
		t.Fatal("the staged file does not hold the bytes that were verified")
	}
}

// ---------------------------------------------------------------------------
// Mutation 1: the BYTES change, the hash stays
// ---------------------------------------------------------------------------

func TestOneFlippedByteInTheArtifactMeansTheInstallerNeverRuns(t *testing.T) {
	// THE MUTATION: byte 0 of the artifact, flipped. Real byte-level
	// corruption, not a substituted hash string — this is the shape of the
	// attack AD-07 exists for, where whoever can write the object store
	// replaces the installer and the signed params still name the old digest.
	//
	// THE WRONG VALUE THE FAILURE MUST NAME: that the installer EXECUTED. An
	// implementation that hashes, compares, logs a warning and runs it anyway
	// returns no error at all and would pass a test that only checked `err !=
	// nil`.
	good := newArtifact("MZ\x90\x00 this is a perfectly good installer")

	corrupted := append([]byte(nil), good.body...)
	old := corrupted[0]
	corrupted[0] ^= 0xFF
	// CONFIRM THE MUTATION APPLIED. A fixture where the "corrupted" bytes are
	// the original bytes is a green test that measures nothing.
	if corrupted[0] == old {
		t.Fatal("the mutation did not apply: the flipped byte equals the original")
	}
	if bytes.Equal(corrupted, good.body) {
		t.Fatal("the mutation did not apply: the corrupted artifact equals the good one")
	}
	if hex.EncodeToString(hash(corrupted)) == good.sha {
		t.Fatal("the mutation did not change the digest; SHA-256 does not work that way and " +
			"this fixture is broken")
	}

	bad := artifact{body: corrupted, sha: good.sha}
	opts, spy, _ := msiJob(t, bad, good.sha)
	// The params still carry the GOOD hash — that is the point.
	_, err := New(opts).deploy(context.Background(), msiParams(t, good.sha))

	spy.ranNothing(t, "the artifact's bytes do not hash to the value in the signed params")
	if err == nil {
		t.Fatal("a corrupted artifact produced no error")
	}
	if !strings.Contains(err.Error(), "NOT executed") {
		t.Errorf("the error %q does not state that the installer was not executed", err)
	}
	assertAbsent(t, err.Error(), good.sha, "the mismatch error")
}

func TestAnArtifactTruncatedByOneByteMeansTheInstallerNeverRuns(t *testing.T) {
	// A SECOND MUTATION with a different shape: a short read rather than a
	// changed byte. A hash check written as "hash the first N bytes" or
	// "compare prefixes" survives the flipped-byte test and dies here.
	good := newArtifact("MZ\x90\x00 this is a perfectly good installer")

	truncated := good.body[:len(good.body)-1]
	if len(truncated) == len(good.body) {
		t.Fatal("the mutation did not apply: the truncated artifact is the same length")
	}
	if bytes.Equal(truncated, good.body) {
		t.Fatal("the mutation did not apply")
	}

	opts, spy, _ := msiJob(t, artifact{body: truncated, sha: good.sha}, good.sha)
	_, err := New(opts).deploy(context.Background(), msiParams(t, good.sha))

	spy.ranNothing(t, "the artifact was truncated by one byte")
	if err == nil {
		t.Fatal("a truncated artifact produced no error")
	}
}

func TestAnArtifactWithAnExtraTrailingByteMeansTheInstallerNeverRuns(t *testing.T) {
	// THIRD MUTATION: APPENDED bytes. This is the one a length check alone
	// would not catch if the length check came from the stream rather than the
	// digest, and it is the shape of a real polyglot/appended-payload attack on
	// an installer.
	good := newArtifact("MZ\x90\x00 this is a perfectly good installer")

	extended := append(append([]byte(nil), good.body...), 0x00)
	if len(extended) <= len(good.body) {
		t.Fatal("the mutation did not apply: nothing was appended")
	}
	if !bytes.HasPrefix(extended, good.body) {
		t.Fatal("the fixture is wrong: the extended artifact should still start with the good one")
	}

	opts, spy, _ := msiJob(t, artifact{body: extended, sha: good.sha}, good.sha)
	_, err := New(opts).deploy(context.Background(), msiParams(t, good.sha))

	spy.ranNothing(t, "a byte was appended to the artifact")
	if err == nil {
		t.Fatal("an extended artifact produced no error")
	}
}

// ---------------------------------------------------------------------------
// Mutation 2: the HASH changes, the bytes stay
// ---------------------------------------------------------------------------

func TestOneChangedHexDigitInTheSignedHashMeansTheInstallerNeverRuns(t *testing.T) {
	// THE MUTATION, from the other end: the artifact is untouched and the
	// expected digest has ONE hex digit changed. This is what a
	// wrong-package-in-the-catalog looks like, and it is also the direction
	// that catches an implementation which compares the digest against itself.
	good := newArtifact("MZ\x90\x00 this is a perfectly good installer")

	wrong := []byte(good.sha)
	oldDigit := wrong[0]
	if wrong[0] == 'f' {
		wrong[0] = '0'
	} else {
		wrong[0] = 'f'
	}
	if wrong[0] == oldDigit {
		t.Fatal("the mutation did not apply: the changed hex digit equals the original")
	}
	wrongSHA := string(wrong)
	if wrongSHA == good.sha {
		t.Fatal("the mutation did not apply: the wrong digest equals the right one")
	}
	if len(wrongSHA) != len(good.sha) {
		t.Fatal("the mutation changed the LENGTH; this would be caught by the length check " +
			"rather than by the comparison, and would not test what it claims to")
	}

	opts, spy, _ := msiJob(t, good, wrongSHA)
	_, err := New(opts).deploy(context.Background(), msiParams(t, wrongSHA))

	spy.ranNothing(t, "the signed params name a different digest from the artifact's")
	if err == nil {
		t.Fatal("a wrong expected digest produced no error")
	}
}

func TestAnUppercaseDigestStillVerifies(t *testing.T) {
	// The counterpart, and it is here so the test above cannot be satisfied by
	// a string comparison that rejects everything unusual. The server emits
	// lowercase (hashlib.hexdigest), and accepting uppercase is
	// docs/kernel/wire-protocol/README.md's "loosen inbound before you tighten
	// outbound" — a server-side rendering change must not take a fleet offline.
	good := newArtifact("MZ\x90\x00 this is a perfectly good installer")
	upper := strings.ToUpper(good.sha)
	if upper == good.sha {
		t.Fatal("the fixture digest has no letters in it; this test would be vacuous")
	}
	opts, spy, _ := msiJob(t, good, upper)
	if _, err := New(opts).deploy(context.Background(), msiParams(t, upper)); err != nil {
		t.Fatalf("an uppercase digest was rejected: %v", err)
	}
	if len(spy.calls()) != 1 {
		t.Fatal("an uppercase digest did not execute")
	}
}

// ---------------------------------------------------------------------------
// Mutation 3: the hash is REMOVED
// ---------------------------------------------------------------------------

func TestAnArtifactBackedJobWithNoHashIsRefusedAndNothingIsEvenDownloaded(t *testing.T) {
	// THE MUTATION: `artifact_sha256` deleted from a params document that is
	// otherwise identical to the one that works. This is the exact shape of
	// "the no-artifact path becomes a way to skip hash verification" — a job
	// that names an installer and no digest.
	//
	// THE WRONG VALUE: the installer executed, OR the artifact was downloaded
	// at all. The fetcher counts too: an implementation that downloads first
	// and validates later has already pulled an attacker-controlled binary
	// onto the endpoint.
	good := newArtifact("MZ\x90\x00 this is a perfectly good installer")

	with := msiParams(t, good.sha)
	without := deployParamsJSON(t, map[string]any{
		"artifact_ref":    "https://packages.example/pkg.msi",
		"artifact_sha256": nil,
	})
	if string(with) == string(without) {
		t.Fatal("the mutation did not apply: the two params documents are identical")
	}
	if !strings.Contains(string(with), "artifact_sha256") {
		t.Fatal("the baseline params carry no artifact_sha256; this test would be vacuous")
	}
	if strings.Contains(string(without), "artifact_sha256") {
		t.Fatal("the mutation did not apply: artifact_sha256 is still present")
	}

	fetched := 0
	spy := &execSpy{}
	opts, _, _ := msiJob(t, good, good.sha)
	opts.Exec = spy.exec
	opts.Fetch = func(context.Context, string) (io.ReadCloser, error) {
		fetched++
		return io.NopCloser(bytes.NewReader(good.body)), nil
	}

	_, err := New(opts).deploy(context.Background(), without)

	spy.ranNothing(t, "the job names an artifact and no digest")
	if fetched != 0 {
		t.Fatalf("the artifact was downloaded %d time(s) despite the job carrying no digest. "+
			"An unverifiable binary must not be pulled onto the endpoint at all", fetched)
	}
	reason := refusalReason(t, err)
	if !strings.Contains(reason, "artifact_sha256") {
		t.Errorf("the refusal %q does not name the missing field", reason)
	}
}

func TestADigestOfTheWrongShapeIsRefusedRatherThanTolerated(t *testing.T) {
	// "Close enough" on the value that decides whether a binary runs as SYSTEM
	// is the same as no check at all.
	//
	// Each case MUTATES the good digest in a way that must be rejected, and
	// each mutation is confirmed to differ from the good one first.
	good := newArtifact("MZ\x90\x00 this is a perfectly good installer")
	for _, tc := range []struct {
		name string
		sha  string
	}{
		{"one digit short", good.sha[:63]},
		{"one digit long", good.sha + "0"},
		{"empty", ""},
		{"not hex", strings.Repeat("z", 64)},
		{"the right digest with whitespace inside", good.sha[:32] + " " + good.sha[33:]},
		{"a prefix that matches", good.sha[:16]},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if tc.sha == good.sha {
				t.Fatal("the mutation did not apply: this case equals the good digest")
			}
			opts, spy, _ := msiJob(t, good, tc.sha)
			_, err := New(opts).deploy(context.Background(), msiParams(t, tc.sha))
			spy.ranNothing(t, "artifact_sha256 is not a 64-character hex digest")
			refusalReason(t, err)
		})
	}
}

// ---------------------------------------------------------------------------
// The partition guard: the no-artifact path cannot be reached WITH an artifact
// ---------------------------------------------------------------------------

func TestAManagerSourcedJobThatCarriesAnArtifactIsRefused(t *testing.T) {
	// THE MUTATION: take a valid winget job and ADD `artifact_ref` +
	// `artifact_sha256` to it. If the branch were chosen by "is there an
	// artifact?" this would be an install; if it were chosen by installer_type
	// and the extra fields were IGNORED, an operator would believe an artifact
	// they uploaded was deployed when it was not.
	//
	// Neither. It is refused, so the two halves of the job can never disagree
	// about what is being installed.
	good := newArtifact("MZ\x90\x00 this is a perfectly good installer")
	base := map[string]any{
		"installer_type": "winget",
		"detection_rule": map[string]any{"type": "package_installed", "name": "Mozilla.Firefox"},
	}
	clean := deployParamsJSON(t, base)

	dirty := map[string]any{}
	for k, v := range base {
		dirty[k] = v
	}
	dirty["artifact_ref"] = "https://packages.example/pkg.msi"
	dirty["artifact_sha256"] = good.sha
	dirtyDoc := deployParamsJSON(t, dirty)
	if string(clean) == string(dirtyDoc) {
		t.Fatal("the mutation did not apply: the two documents are identical")
	}

	// The clean job installs.
	spy := &execSpy{}
	opts := Options{
		GOOS: "windows", LookPath: fakePaths("winget.exe"), Logger: quiet(), Exec: spy.exec,
		Fetch: good.fetcher(), ScratchRoot: t.TempDir(),
	}
	if _, err := New(opts).deploy(context.Background(), clean); err != nil {
		t.Fatalf("a clean winget job was rejected: %v; this test would be vacuous", err)
	}
	if len(spy.calls()) != 1 {
		t.Fatal("a clean winget job did not execute; this test would be vacuous")
	}

	// The dirty one is refused.
	spy2 := &execSpy{}
	opts.Exec = spy2.exec
	_, err := New(opts).deploy(context.Background(), dirtyDoc)
	spy2.ranNothing(t, "a manager-sourced job also named an artifact")
	reason := refusalReason(t, err)
	if !strings.Contains(reason, "artifact_ref") {
		t.Errorf("the refusal %q does not name the offending field", reason)
	}
}

func TestAnArtifactBackedJobWithNoArtifactRefIsRefused(t *testing.T) {
	// The mirror image, and the one that closes the loop: an `msi` job with
	// artifact_ref removed must not fall through onto the manager path (where
	// there is no hash check) — it must be refused.
	with := msiParams(t, newArtifact("x").sha)
	without := deployParamsJSON(t, map[string]any{
		"artifact_ref":    nil,
		"artifact_sha256": newArtifact("x").sha,
	})
	if string(with) == string(without) {
		t.Fatal("the mutation did not apply")
	}
	spy := &execSpy{}
	opts, _, _ := msiJob(t, newArtifact("x"), "")
	opts.Exec = spy.exec
	_, err := New(opts).deploy(context.Background(), without)
	spy.ranNothing(t, "an artifact-backed job carries no artifact_ref")
	refusalReason(t, err)
}

func TestStageItselfRefusesWithNoArtifactRefEvenWhenTheCallerDidNotCheck(t *testing.T) {
	// `stage`'s contract is "no path without a verified hash", and a guarantee
	// that depends on the CALLER having validated something is not a
	// guarantee. This calls stage directly with params parseDeployParams would
	// have rejected.
	//
	// FALSIFYING OBSERVATION: a path returned, or a nil error.
	sc, err := newScratch(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer sc.Close()
	svc := New(Options{Logger: quiet(), Fetch: newArtifact("x").fetcher()})

	for _, p := range []deployParams{
		{InstallerType: "msi", Action: "install"},
		{InstallerType: "msi", Action: "install", ArtifactRef: ptr("https://x/y")},
	} {
		path, err := svc.stage(context.Background(), sc, p)
		if err == nil {
			t.Fatalf("stage returned %q for params with no ref or no digest", path)
		}
		if path != "" {
			t.Fatalf("stage returned a path (%q) alongside an error; a caller that ignored the "+
				"error would spawn it", path)
		}
	}
}

// ---------------------------------------------------------------------------
// Apparatus
// ---------------------------------------------------------------------------

func hash(b []byte) []byte {
	sum := sha256.Sum256(b)
	return sum[:]
}

func ptr[T any](v T) *T { return &v }

// TestOnAMismatchTheExecutablePathDoesNotExistAtAll is the STRUCTURAL half,
// and it is deliberately not folded into the end-to-end tests above.
//
// Folding it in would have been vacuous, and that was caught rather than
// avoided: `deploy` removes the scratch directory in a defer, so a walk of the
// scratch root AFTER deploy returns finds nothing whether the staging worked
// or not. It passes identically against an implementation with no hash check
// at all. So the question is asked at the only level where it can be answered
// — `stage` itself, with the scratch directory still in hand.
//
// The claim being tested: on a mismatch, the file the PLANNER WOULD NAME does
// not exist. That is what makes "the installer did not run" independent of any
// caller remembering to check an error — a future edit that ignored stage's
// error would spawn a path that is not there.
//
// FALSIFYING OBSERVATION: `package.msi` present in the scratch directory after
// a failed verification, or a non-empty path returned alongside the error.
func TestOnAMismatchTheExecutablePathDoesNotExistAtAll(t *testing.T) {
	good := newArtifact("MZ\x90\x00 this is a perfectly good installer")
	corrupted := append([]byte(nil), good.body...)
	corrupted[len(corrupted)/2] ^= 0xFF
	if bytes.Equal(corrupted, good.body) {
		t.Fatal("the mutation did not apply")
	}

	p := deployParams{
		Action: "install", InstallerType: "msi",
		ArtifactRef: ptr("https://packages.example/pkg.msi"), ArtifactSHA256: ptr(good.sha),
	}

	// The BASELINE first, in the same shape, so the assertion below cannot pass
	// because staging never produces a file under any circumstances.
	okScratch, err := newScratch(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer okScratch.Close()
	okPath, err := New(Options{Logger: quiet(), Fetch: good.fetcher()}).
		stage(context.Background(), okScratch, p)
	if err != nil {
		t.Fatalf("staging a matching artifact failed: %v", err)
	}
	if filepath.Base(okPath) != stagedMSI {
		t.Fatalf("a verified artifact staged to %q, not %q", okPath, stagedMSI)
	}
	if _, err := os.Stat(okPath); err != nil {
		t.Fatalf("a verified artifact is not on disk: %v; this test would be vacuous", err)
	}

	// Now the mismatch, into a fresh scratch directory.
	badScratch, err := newScratch(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer badScratch.Close()
	badFetch := Fetcher(func(context.Context, string) (io.ReadCloser, error) {
		return io.NopCloser(bytes.NewReader(corrupted)), nil
	})
	badPath, err := New(Options{Logger: quiet(), Fetch: badFetch}).
		stage(context.Background(), badScratch, p)
	if err == nil {
		t.Fatal("staging a corrupted artifact returned no error")
	}
	if badPath != "" {
		t.Fatalf("stage returned the path %q alongside its error", badPath)
	}
	if _, statErr := os.Stat(badScratch.path(stagedMSI)); statErr == nil {
		t.Fatalf("%s exists after a failed verification. The executable path must not come into "+
			"being unless the digest matched", badScratch.path(stagedMSI))
	}
	// And the unverified bytes are not left lying around under the name the
	// download used either.
	if _, statErr := os.Stat(badScratch.path(partName)); statErr == nil {
		t.Errorf("%s still holds the unverified bytes after the mismatch; they should be "+
			"discarded", badScratch.path(partName))
	}
}
