package pushinstall

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

// The capability's decision logic, exercised through the testable `probe`/
// `install` cores (a VerifiedJob is unforgeable by design, exactly as netsweep
// and site test their `run`). The transport is a fake so no socket is opened;
// every branch that matters to an invariant has a test that removes one thing.

// --------------------------------------------------------------------------- //
// Fakes
// --------------------------------------------------------------------------- //

type fakeSession struct {
	unameOut string
	unameErr error
	putCalls []putCall
	runCalls []string
	runErr   error
	// failRuns fails only the commands with these prefixes; failPutAt fails
	// the Nth Put (1-based).
	failRuns  []string
	failPutAt int
	closed    bool
}

type putCall struct {
	path string
	data []byte
	mode uint32
}

func (f *fakeSession) Run(_ context.Context, cmd string) (string, error) {
	f.runCalls = append(f.runCalls, cmd)
	if strings.HasPrefix(cmd, "uname") {
		return f.unameOut, f.unameErr
	}
	for _, prefix := range f.failRuns {
		if strings.HasPrefix(cmd, prefix) {
			return "", errors.New("exit status 1")
		}
	}
	return "", f.runErr
}

func (f *fakeSession) Put(_ context.Context, path string, data []byte, mode uint32) error {
	f.putCalls = append(f.putCalls, putCall{path: path, data: append([]byte(nil), data...), mode: mode})
	if f.failPutAt == len(f.putCalls) {
		return errors.New("scp: write failed")
	}
	return nil
}

func (f *fakeSession) Close() error { f.closed = true; return nil }

type fakeDialer struct {
	probeInfo ProbeInfo
	probeErr  error
	openErr   error
	session   *fakeSession
	// Recorded to prove what the capability did.
	probedAddr  string
	openedAddr  string
	expectFP    string
	openUser    string
	openSecret  []byte
	openCalled  bool
	probeCalled bool
}

func (d *fakeDialer) Probe(_ context.Context, addr string) (ProbeInfo, error) {
	d.probeCalled = true
	d.probedAddr = addr
	return d.probeInfo, d.probeErr
}

func (d *fakeDialer) Open(_ context.Context, addr, expectFP, user string, secret []byte) (Session, error) {
	d.openCalled = true
	d.openedAddr = addr
	d.expectFP = expectFP
	d.openUser = user
	// Copy the secret bytes AS SEEN, so the test can prove they are zeroed after.
	d.openSecret = append([]byte(nil), secret...)
	if d.openErr != nil {
		return nil, d.openErr
	}
	return d.session, nil
}

type fakeDownloader struct {
	data   []byte
	err    error
	called bool
}

func (d *fakeDownloader) Download(_ context.Context, _ string) ([]byte, error) {
	d.called = true
	return d.data, d.err
}

const goodFP = "SHA256:AAAAtestkeyfingerprintAAAA"

func installParamsJSON(t *testing.T, mutate func(m map[string]any)) json.RawMessage {
	t.Helper()
	artefact := []byte("this is the agent artefact")
	sum := sha256Hex(artefact)
	m := map[string]any{
		"target_address":       "10.0.4.17",
		"transport":            "ssh",
		"os":                   "linux",
		"arch":                 "amd64",
		"release":              map[string]any{"version": "1.0.0", "sha256": sum, "url": "https://store/x"},
		"enroll_token":         "SECRET-TOKEN",
		"credential":           map[string]any{"username": "svc", "secret": "hunter2"},
		"host_key_fingerprint": goodFP,
		"enroll_url":           "https://itms.example.com",
	}
	if mutate != nil {
		mutate(m)
	}
	b, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func artefactBytes() []byte { return []byte("this is the agent artefact") }

func sha256Hex(b []byte) string {
	return hexSum(b)
}

// --------------------------------------------------------------------------- //
// Probe: no credential, learns the key
// --------------------------------------------------------------------------- //

func TestProbePresentsNoCredentialAndReturnsTheKey(t *testing.T) {
	d := &fakeDialer{probeInfo: ProbeInfo{Fingerprint: goodFP, KeyType: "ssh-ed25519", Banner: "OpenSSH_9"}}
	s := New(Options{Dialer: d})
	raw := json.RawMessage(`{"target_address":"10.0.4.17","transport":"ssh"}`)

	out, err := s.probe(context.Background(), raw)
	if err != nil {
		t.Fatal(err)
	}
	res := out.(probeResult)
	if res.HostKeyFingerprint != goodFP {
		t.Fatalf("fingerprint = %q", res.HostKeyFingerprint)
	}
	// FALSIFIER 22: a probe never opens an authenticated connection.
	if d.openCalled {
		t.Fatal("the probe called Open -- it must present no credential")
	}
	if d.probedAddr != "10.0.4.17" {
		t.Fatalf("probed %q, not the signed address", d.probedAddr)
	}
	if s.Runs() != 1 {
		t.Fatalf("runs = %d", s.Runs())
	}
}

// --------------------------------------------------------------------------- //
// Install: the whole path, and the credential is zeroed and never in argv
// --------------------------------------------------------------------------- //

func TestInstallSignsNothingIntoArgvAndZeroesTheCredential(t *testing.T) {
	sess := &fakeSession{unameOut: "Linux x86_64"}
	d := &fakeDialer{session: sess}
	dl := &fakeDownloader{data: artefactBytes()}
	s := New(Options{Dialer: d, Downloader: dl})

	out, err := s.install(context.Background(), installParamsJSON(t, nil))
	if err != nil {
		t.Fatal(err)
	}
	res := out.(installResult)
	if res.Outcome != outcomeInstalled {
		t.Fatalf("outcome = %q, detail = %q", res.Outcome, res.Detail)
	}
	// The host key that was verified is the CONFIRMED one from the signed params.
	if d.expectFP != goodFP {
		t.Fatalf("Open verified %q, not the pinned key", d.expectFP)
	}
	// FALSIFIER 14: the token is placed as a 0600 file, NEVER in argv.
	var tokenPut *putCall
	for i := range sess.putCalls {
		if sess.putCalls[i].path == tokenFilePath {
			tokenPut = &sess.putCalls[i]
		}
	}
	if tokenPut == nil {
		t.Fatal("the enrolment token was not placed as a file")
	}
	if tokenPut.mode != tokenFileMode {
		t.Fatalf("token file mode = %#o, want %#o", tokenPut.mode, tokenFileMode)
	}
	if string(tokenPut.data) != "SECRET-TOKEN" {
		t.Fatalf("token file content = %q", tokenPut.data)
	}
	for _, cmd := range sess.runCalls {
		if strings.Contains(cmd, "SECRET-TOKEN") {
			t.Fatalf("the token appeared in a command line: %q", cmd)
		}
		if strings.Contains(cmd, "hunter2") {
			t.Fatalf("the credential appeared in a command line: %q", cmd)
		}
	}
	// The whole remote side, in order: read the OS, make the root-only staging
	// directory, then (after the two Puts) the ONE install command -- a
	// constant template whose only filled value is the validated enroll
	// origin. The token is a file; it is never in a command.
	wantRuns := []string{
		"uname -s -m",
		// Review I3: whatever was there -- a directory someone else made, or a
		// symlink (rm removes the link, never its target) -- is removed, and
		// a fresh root-only directory made; no -p, so a race is a failure.
		"rm -rf /opt/toeshee && mkdir -m 0700 /opt/toeshee",
		"/opt/toeshee/agent.pkg install --token-file /opt/toeshee/enroll.token --enroll-url 'https://itms.example.com'",
	}
	if strings.Join(sess.runCalls, "\n") != strings.Join(wantRuns, "\n") {
		t.Fatalf("remote commands =\n%s\nwant\n%s", strings.Join(sess.runCalls, "\n"), strings.Join(wantRuns, "\n"))
	}
	// FALSIFIER 3: the credential the dialer received is zeroed after the job.
	if bytes.Equal(d.openSecret, []byte("hunter2")) {
		// openSecret is a copy taken at Open time; the ORIGINAL buffer the
		// Service holds must be zero now. Prove it by re-reading via a second
		// path below.
	}
	// The Service's own secret buffer is the one `defer zero` cleared. We cannot
	// reach it from here, so assert the observable contract: the result carries
	// no secret and no log was written (the capability logs nothing).
	if strings.Contains(res.Detail, "hunter2") {
		t.Fatal("the credential leaked into the result")
	}
}

func TestTheCredentialBufferIsZeroedBeforeReturn(t *testing.T) {
	// A dialer that CAPTURES the live secret slice (not a copy) and lets us read
	// it after install returns -- proving `defer zero(secret)` ran.
	var live []byte
	d := &captureDialer{onOpen: func(secret []byte) { live = secret }, session: &fakeSession{unameOut: "Linux x86_64"}}
	dl := &fakeDownloader{data: artefactBytes()}
	s := New(Options{Dialer: d, Downloader: dl})

	if _, err := s.install(context.Background(), installParamsJSON(t, nil)); err != nil {
		t.Fatal(err)
	}
	for _, b := range live {
		if b != 0 {
			t.Fatalf("the credential buffer was not zeroed: %q", live)
		}
	}
	if len(live) == 0 {
		t.Fatal("the measurement is vacuous: no secret was captured")
	}
}

type captureDialer struct {
	onOpen  func([]byte)
	session Session
}

func (c *captureDialer) Probe(context.Context, string) (ProbeInfo, error) { return ProbeInfo{}, nil }
func (c *captureDialer) Open(_ context.Context, _, _, _ string, secret []byte) (Session, error) {
	c.onOpen(secret) // keep the LIVE slice; the Service zeroes it after return
	return c.session, nil
}

// --------------------------------------------------------------------------- //
// Install refusals: each removes one bound
// --------------------------------------------------------------------------- //

func TestInstallRefusesAnEmptyHostKeyRatherThanTrustingOnFirstUse(t *testing.T) {
	d := &fakeDialer{session: &fakeSession{unameOut: "Linux x86_64"}}
	s := New(Options{Dialer: d, Downloader: &fakeDownloader{data: artefactBytes()}})
	raw := installParamsJSON(t, func(m map[string]any) { m["host_key_fingerprint"] = "" })

	_, err := s.install(context.Background(), raw)
	if err == nil {
		t.Fatal("an install with no pinned host key must be refused, not attempted")
	}
	if d.openCalled {
		t.Fatal("it dialled the target with no pinned key -- trust-on-first-use")
	}
	if !strings.Contains(err.Error(), "trust-on-first-use") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestInstallRefusesWithoutAUsableEnrollURLBeforeDialling(t *testing.T) {
	// W2 superseded: the pushed machine enrolls at the SIGNED enroll_url. With
	// none, or one the install command could not carry safely, there is no
	// install -- and no credential is presented to find that out.
	for name, mutate := range map[string]func(m map[string]any){
		"no enroll_url":         func(m map[string]any) { delete(m, "enroll_url") },
		"null enroll_url":       func(m map[string]any) { m["enroll_url"] = nil },
		"http enroll_url":       func(m map[string]any) { m["enroll_url"] = "http://itms.example.com" },
		"a path":                func(m map[string]any) { m["enroll_url"] = "https://itms.example.com/enroll/v1" },
		"a shell metacharacter": func(m map[string]any) { m["enroll_url"] = "https://itms.example.com';reboot;'" },
	} {
		d := &fakeDialer{session: &fakeSession{unameOut: "Linux x86_64"}}
		dl := &fakeDownloader{data: artefactBytes()}
		s := New(Options{Dialer: d, Downloader: dl})
		if _, err := s.install(context.Background(), installParamsJSON(t, mutate)); err == nil {
			t.Errorf("%s: accepted", name)
		}
		if d.openCalled || dl.called {
			t.Errorf("%s: dialled or downloaded before refusing", name)
		}
	}
}

func TestInstallCommandIsAConstantTemplate(t *testing.T) {
	got, err := InstallCommand("https://itms.example.com:8443")
	if err != nil {
		t.Fatal(err)
	}
	want := "/opt/toeshee/agent.pkg install --token-file /opt/toeshee/enroll.token --enroll-url 'https://itms.example.com:8443'"
	if got != want {
		t.Fatalf("installCommand = %q, want %q", got, want)
	}
	for _, bad := range []string{"", "http://x.example", "https://x.example/p", "https://x.example'", "https://x.example $(id)"} {
		if _, err := InstallCommand(bad); err == nil {
			t.Errorf("InstallCommand(%q) accepted", bad)
		}
	}
}

func TestInstallReportsAHostKeyMismatchAsUnreachableAndAuthenticatesNothing(t *testing.T) {
	d := &fakeDialer{openErr: ErrHostKey}
	s := New(Options{Dialer: d, Downloader: &fakeDownloader{data: artefactBytes()}})

	out, err := s.install(context.Background(), installParamsJSON(t, nil))
	if err != nil {
		t.Fatal(err)
	}
	if out.(installResult).Outcome != outcomeUnreach {
		t.Fatalf("outcome = %q", out.(installResult).Outcome)
	}
}

func TestInstallReportsAnAuthFailure(t *testing.T) {
	d := &fakeDialer{openErr: errors.Join(ErrAuth, errors.New("bad password"))}
	s := New(Options{Dialer: d, Downloader: &fakeDownloader{data: artefactBytes()}})

	out, _ := s.install(context.Background(), installParamsJSON(t, nil))
	if out.(installResult).Outcome != outcomeAuthFailed {
		t.Fatalf("outcome = %q", out.(installResult).Outcome)
	}
}

func TestInstallRefusesAnOSMismatchAndNeverDownloads(t *testing.T) {
	sess := &fakeSession{unameOut: "Darwin arm64"} // target is not linux/amd64
	d := &fakeDialer{session: sess}
	dl := &fakeDownloader{data: artefactBytes()}
	s := New(Options{Dialer: d, Downloader: dl})

	out, _ := s.install(context.Background(), installParamsJSON(t, nil))
	res := out.(installResult)
	if res.Outcome != outcomeOSMismatch {
		t.Fatalf("outcome = %q", res.Outcome)
	}
	if dl.called {
		t.Fatal("it downloaded the artefact for a machine of the wrong OS")
	}
}

func TestInstallNeverTransfersAnArtefactWhoseDigestDoesNotMatch(t *testing.T) {
	// FALSIFIER 6: a wrong-digest artefact is never transferred.
	sess := &fakeSession{unameOut: "Linux x86_64"}
	d := &fakeDialer{session: sess}
	dl := &fakeDownloader{data: []byte("TAMPERED bytes, wrong digest")}
	s := New(Options{Dialer: d, Downloader: dl})

	out, _ := s.install(context.Background(), installParamsJSON(t, nil))
	if out.(installResult).Outcome != outcomeFailed {
		t.Fatalf("outcome = %q", out.(installResult).Outcome)
	}
	if len(sess.putCalls) != 0 {
		t.Fatalf("it transferred %d file(s) despite a digest mismatch", len(sess.putCalls))
	}
}

func TestInstallOnlyEverDialsTheSignedAddress(t *testing.T) {
	// FALSIFIER 4: the endpoint never decides what it may touch. The only address
	// dialled is the one in the signed params.
	sess := &fakeSession{unameOut: "Linux x86_64"}
	d := &fakeDialer{session: sess}
	s := New(Options{Dialer: d, Downloader: &fakeDownloader{data: artefactBytes()}})

	if _, err := s.install(context.Background(), installParamsJSON(t, nil)); err != nil {
		t.Fatal(err)
	}
	if d.openedAddr != "10.0.4.17" {
		t.Fatalf("dialled %q, not the signed target_address", d.openedAddr)
	}
}

// --------------------------------------------------------------------------- //
// Pure helpers
// --------------------------------------------------------------------------- //

func TestVerifyDigestRejectsAMismatch(t *testing.T) {
	if err := verifyDigest([]byte("abc"), hexSum([]byte("abc"))); err != nil {
		t.Fatalf("clean digest rejected: %v", err)
	}
	if err := verifyDigest([]byte("abc"), hexSum([]byte("different"))); err == nil {
		t.Fatal("a mismatched digest was accepted")
	}
}

func TestNormalizeOSArch(t *testing.T) {
	if normalizeOS("Linux") != "linux" || normalizeOS("Darwin") != "darwin" {
		t.Fatal("OS normalization")
	}
	if normalizeArch("x86_64") != "amd64" || normalizeArch("aarch64") != "arm64" {
		t.Fatal("arch normalization")
	}
}

func TestFingerprintsEqualToleratesThePrefix(t *testing.T) {
	if !fingerprintsEqual("SHA256:abc", "abc") {
		t.Fatal("prefix tolerance")
	}
	if fingerprintsEqual("abc", "abd") {
		t.Fatal("distinct fingerprints compared equal")
	}
	if fingerprintsEqual("", "") {
		t.Fatal("empty fingerprints must never match")
	}
}

func hexSum(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// REVIEW Minor 6. A failed SSH install leaves nothing staged behind if the
// pushing agent can help it: after any failure once the staging directory
// exists, one more constant command removes both staged files, best effort.
func TestAFailedSSHInstallCleansUpTheStagedFiles(t *testing.T) {
	const cleanup = "rm -f /opt/toeshee/enroll.token /opt/toeshee/agent.pkg"
	for name, sess := range map[string]*fakeSession{
		"the install command failed": {unameOut: "Linux x86_64", failRuns: []string{"/opt/toeshee/agent.pkg install"}},
		"the artefact copy failed":   {unameOut: "Linux x86_64", failPutAt: 1},
		"the token copy failed":      {unameOut: "Linux x86_64", failPutAt: 2},
	} {
		s := New(Options{Dialer: &fakeDialer{session: sess}, Downloader: &fakeDownloader{data: artefactBytes()}})
		out, err := s.install(context.Background(), installParamsJSON(t, nil))
		if err != nil {
			t.Fatal(err)
		}
		if r := out.(installResult); r.Outcome != outcomeFailed {
			t.Errorf("%s: outcome = %q", name, r.Outcome)
		}
		if n := len(sess.runCalls); n == 0 || sess.runCalls[n-1] != cleanup {
			t.Errorf("%s: last command = %v, want the cleanup", name, sess.runCalls)
		}
	}
	// A cleanup that itself fails does not change the reported outcome.
	sess := &fakeSession{unameOut: "Linux x86_64", failRuns: []string{"/opt/toeshee/agent.pkg install", "rm -f "}}
	s := New(Options{Dialer: &fakeDialer{session: sess}, Downloader: &fakeDownloader{data: artefactBytes()}})
	out, _ := s.install(context.Background(), installParamsJSON(t, nil))
	if r := out.(installResult); !strings.Contains(r.Detail, "install:") {
		t.Errorf("detail = %q; the install failure is what is reported", r.Detail)
	}
}

func TestASuccessfulSSHInstallRunsNoCleanup(t *testing.T) {
	sess := &fakeSession{unameOut: "Linux x86_64"}
	s := New(Options{Dialer: &fakeDialer{session: sess}, Downloader: &fakeDownloader{data: artefactBytes()}})
	if _, err := s.install(context.Background(), installParamsJSON(t, nil)); err != nil {
		t.Fatal(err)
	}
	for _, cmd := range sess.runCalls {
		if strings.HasPrefix(cmd, "rm -f ") {
			t.Fatalf("a successful install ran %q; the install mode deletes its own staged files", cmd)
		}
	}
}
