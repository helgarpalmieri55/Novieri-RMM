// This package does not call t.Parallel(): the TLS trust seam
// (backupstore.SetTestTLSConfig) is a single process-global variable, so two
// tests changing it concurrently would race each other's trust roots, not
// just their servers.
package backupstore_test

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"mime"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/backupstore"
	"github.com/toeshee/it-suite/agent/internal/capabilities/backup"
	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

func grantFor(url string) wire.UploadGrant {
	return wire.UploadGrant{
		URL:       url,
		Fields:    map[string]string{"key": "org/backup/repo/${filename}", "policy": "signed", "x-amz-signature": "sig"},
		KeyPrefix: "org/backup/repo/",
		MaxBytes:  1 << 20,
	}
}

// trustPool builds a tls.Config trusting exactly the given certificates, so
// the upload client accepts a self-signed test server without relaxing the
// https-only rule under test -- the rule stays exactly what's shipped, and
// only the trust root the test client checks it against changes.
func trustPool(certs ...*x509.Certificate) *tls.Config {
	pool := x509.NewCertPool()
	for _, c := range certs {
		pool.AddCert(c)
	}
	return &tls.Config{RootCAs: pool}
}

// captured records what the fake object store received.
type captured struct {
	fields map[string]string
	body   string
	order  []string
}

// capturingServer runs the fake object store over REAL TLS -- not a
// convenience http:// stand-in -- and installs it as the only trusted root
// for the duration of the test, via the SetTestTLSConfig seam. That way the
// https-only check in Put runs unmodified and unmocked: it evaluates a real
// TLS handshake against a self-signed cert this test explicitly trusted,
// rather than being loosened for loopback or skipped.
func capturingServer(t *testing.T, status int) (*httptest.Server, *captured) {
	t.Helper()
	got := &captured{fields: map[string]string{}}
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, params, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
		if err != nil {
			t.Errorf("Content-Type: %v", err)
			w.WriteHeader(400)
			return
		}
		mr := multipart.NewReader(r.Body, params["boundary"])
		for {
			p, err := mr.NextPart()
			if err != nil {
				break
			}
			// bytes.Buffer, not strings.Builder: only the former has ReadFrom.
			buf := new(bytes.Buffer)
			_, _ = buf.ReadFrom(p)
			got.order = append(got.order, p.FormName())
			if p.FormName() == "file" {
				got.body = buf.String()
			} else {
				got.fields[p.FormName()] = buf.String()
			}
		}
		w.WriteHeader(status)
	}))
	t.Cleanup(srv.Close)
	// Installed before the caller ever calls Open: the seam is read once, at
	// Open time, into the client's Transport -- setting it after Open would
	// silently do nothing.
	t.Cleanup(backupstore.SetTestTLSConfig(trustPool(srv.Certificate())))
	return srv, got
}

func TestPutSendsTheSignedFieldsWithALiteralKey(t *testing.T) {
	srv, got := capturingServer(t, 204)

	st, err := backupstore.Open(context.Background(), backup.Repository{ID: "repo", StorageRef: "org/backup/repo"})
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer st.Close()

	ref, err := st.Put(context.Background(), grantFor(srv.URL), "chunks/ab/abcd", []byte("sealed"))
	if err != nil {
		t.Fatalf("Put: %v", err)
	}

	state := [4]any{got.fields["key"], got.fields["policy"], got.body, ref}
	want := [4]any{"org/backup/repo/chunks/ab/abcd", "signed", "sealed", "chunks/ab/abcd"}
	if state != want {
		t.Errorf("upload = %v, want %v", state, want)
	}
	if len(got.order) == 0 || got.order[len(got.order)-1] != "file" {
		t.Errorf("part order = %v; `file` must be last or S3 ignores what follows", got.order)
	}
}

// A grant is scoped to a prefix. A suffix that escapes it must never reach the
// network -- S3 would reject it, but only after the bytes were sent. This runs
// over TLS, like the other Put tests: on a plain http:// server "never reached
// the network" would be ambiguous between "the suffix guard blocked it" and
// "the scheme guard blocked it", which proves less than it claims.
func TestPutRefusesASuffixThatEscapesThePrefix(t *testing.T) {
	srv, got := capturingServer(t, 204)
	st, _ := backupstore.Open(context.Background(), backup.Repository{ID: "repo"})
	defer st.Close()

	for _, bad := range []string{"../elsewhere/x", "/absolute", "a/../../b", ""} {
		if _, err := st.Put(context.Background(), grantFor(srv.URL), bad, []byte("x")); err == nil {
			t.Errorf("Put(%q) was accepted", bad)
		}
	}
	if len(got.order) != 0 {
		t.Errorf("a refused suffix still reached the network: %v", got.order)
	}
}

func TestPutRefusesABodyOverTheGrantCap(t *testing.T) {
	srv, got := capturingServer(t, 204)
	st, _ := backupstore.Open(context.Background(), backup.Repository{ID: "repo"})
	defer st.Close()

	g := grantFor(srv.URL)
	g.MaxBytes = 4
	if _, err := st.Put(context.Background(), g, "chunks/ab/x", []byte("more than four")); err == nil {
		t.Error("a body over max_bytes was accepted")
	}
	if len(got.order) != 0 {
		t.Errorf("an oversized body still reached the network: %v", got.order)
	}
}

// Refusing an http:// grant URL IS the assertion here, so this stays on plain
// HTTP -- there is no server to trust in the first place, since the scheme
// check must reject the URL before any dial is attempted.
func TestPutRefusesAPlaintextGrantURL(t *testing.T) {
	st, _ := backupstore.Open(context.Background(), backup.Repository{ID: "repo"})
	defer st.Close()
	g := grantFor("http://insecure.example/bucket")
	_, err := st.Put(context.Background(), g, "chunks/ab/x", []byte("x"))
	if err == nil {
		t.Fatal("a plaintext grant URL was accepted")
	}
	// Not merely `err != nil`. "insecure.example" resolves to nothing, so a
	// store that had LOST its scheme check would still fail here -- on the
	// dial, after having already sent the sealed payload in the clear -- and
	// `err != nil` alone cannot distinguish that from the scheme guard
	// working. The read side's sibling test (TestAPlaintextGrantUrlIsRefused
	// in backupstore_read_test.go) already pins its refusal text; this does
	// the same for the write side.
	if !strings.Contains(err.Error(), "https") {
		t.Fatalf("Put against a plaintext grant = %v, want it refused for its scheme", err)
	}
}

// A 302 turns a fixed-host client into an arbitrary-host one. Both servers run
// over TLS with DIFFERENT certificates, so the trust pool installed for the
// test must include both -- otherwise the request would fail on trust before
// ever reaching the redirect logic, which would prove nothing about
// redirects. redirected and reached both being observed, not just `err`, is
// what makes this test about the redirect rather than about the handshake.
//
// attempts asserts the count, not merely that Put returned an error: a test
// that only checked `err != nil` would pass identically whether the
// redirector was hit once or retried three times, and would keep passing if
// a future change silently started retrying it thirty times. A redirect
// 302s identically every time, so exactly one hit is the only count that
// says "this was recognised as non-retryable" rather than "this eventually
// gave up".
func TestPutNeverFollowsARedirect(t *testing.T) {
	var reached, redirected bool
	var attempts int
	elsewhere := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reached = true
		w.WriteHeader(204)
	}))
	defer elsewhere.Close()
	redirector := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		redirected = true
		attempts++
		http.Redirect(w, r, elsewhere.URL, http.StatusFound)
	}))
	defer redirector.Close()
	t.Cleanup(backupstore.SetTestTLSConfig(trustPool(redirector.Certificate(), elsewhere.Certificate())))

	st, _ := backupstore.Open(context.Background(), backup.Repository{ID: "repo"})
	defer st.Close()
	if _, err := st.Put(context.Background(), grantFor(redirector.URL), "chunks/ab/x", []byte("x")); err == nil {
		t.Error("a redirected upload was accepted")
	}
	if !redirected {
		t.Error("the upload never reached the redirector -- the redirect path was not exercised")
	}
	if attempts != 1 {
		t.Errorf("redirector was hit %d times, want exactly 1 -- a redirect refusal must not be retried, "+
			"it will recur identically against the same URL", attempts)
	}
	if reached {
		t.Error("the upload followed a redirect to another host")
	}
}

// A 403 is the signed policy's verdict, not a transient. Retrying it wastes an
// endpoint's bandwidth on an answer that will not change. This runs over TLS
// so the request genuinely reaches the handler and attempts counts something
// real, not a scheme rejection that never dialed.
func TestPutDoesNotRetryAPolicyRejection(t *testing.T) {
	var attempts int
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		attempts++
		w.WriteHeader(403)
	}))
	defer srv.Close()
	t.Cleanup(backupstore.SetTestTLSConfig(trustPool(srv.Certificate())))

	st, _ := backupstore.Open(context.Background(), backup.Repository{ID: "repo"})
	defer st.Close()
	if _, err := st.Put(context.Background(), grantFor(srv.URL), "chunks/ab/x", []byte("x")); err == nil {
		t.Fatal("a 403 was treated as success")
	}
	if attempts != 1 {
		t.Errorf("attempts = %d, want 1", attempts)
	}
}

// Reads exist now, but only through Opener: a store opened by `Open` has no
// grant source, and must still REFUSE rather than fail somewhere less legible,
// so backup.restore keeps reporting `refused` for it.
//
// `err != nil` alone cannot prove that: restore.go:62 only takes the
// `refused` branch for a jobs.Refusal specifically, and GetRef's path to an
// error runs through suffixOfRef before it ever reaches Get -- so a version
// that checked s.granter too late (or not at all) still returns SOME error
// here (an unparseable-prefix error, ordinary and not a Refusal), and this
// test would not have told the difference. errors.As pins the TYPE, which is
// the property restore.go actually branches on.
func TestGetAndGetRefStillRefuse(t *testing.T) {
	st, _ := backupstore.Open(context.Background(), backup.Repository{ID: "repo"})
	defer st.Close()

	var refusal *jobs.Refusal
	if _, err := st.Get(context.Background(), "chunks/ab/x"); !errors.As(err, &refusal) {
		t.Errorf("Get = %v, want a jobs.Refusal", err)
	}
	if _, err := st.GetRef(context.Background(), "s3://b/k"); !errors.As(err, &refusal) {
		t.Errorf("GetRef = %v, want a jobs.Refusal", err)
	}
}

// The idle budget: Open must not dial. It is called at job time, but the store
// it returns must not touch the network until Put does.
func TestOpenDialsNothing(t *testing.T) {
	var reached bool
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		reached = true
	}))
	defer srv.Close()
	st, err := backupstore.Open(context.Background(), backup.Repository{ID: "repo", StorageRef: srv.URL})
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer st.Close()
	if reached {
		t.Error("Open made a request")
	}
}
