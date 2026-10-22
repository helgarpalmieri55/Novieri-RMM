// Read-path tests. This file is in package `backupstore`, not
// `backupstore_test`, because the window and retry bounds it asserts are the
// unexported constants themselves -- a test that hard-coded 256 and 3 would go
// on passing after someone changed the constant it exists to pin.
//
// Like backupstore_test.go, nothing here calls t.Parallel(): the TLS trust seam
// is a single process-global.
package backupstore

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/capabilities/backup"
	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// httptestRoots trusts the certificate an httptest TLS server presents.
//
// Every httptest TLS server in a process presents the SAME built-in
// certificate unless the caller installs its own, so one throwaway server
// yields the root that every server in this file needs, and newTestStore can
// install trust without being handed the server under test. The alternative --
// InsecureSkipVerify -- would relax the shipped https rule these tests exist to
// exercise, which is exactly what the seam's comment in backupstore.go refuses
// to do. If that shared-certificate property ever stops holding, every read
// test here fails loudly on trust rather than quietly proving less.
func httptestRoots(t *testing.T) *tls.Config {
	t.Helper()
	probe := httptest.NewTLSServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	defer probe.Close()
	pool := x509.NewCertPool()
	pool.AddCert(probe.Certificate())
	return &tls.Config{RootCAs: pool}
}

// newTestStore opens a store whose repository prefix is `org1/backup/repo1`.
//
// The trust seam is installed BEFORE open, because open reads it once into the
// client's Transport; installing it afterwards would silently do nothing.
func newTestStore(t *testing.T, g ReadGranter) *store {
	t.Helper()
	t.Cleanup(SetTestTLSConfig(httptestRoots(t)))
	s, err := open(context.Background(), backup.Repository{
		ID: "repo1", StorageRef: "org1/backup/repo1", JobID: "job-1",
	})
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	s.granter = g
	t.Cleanup(func() { _ = s.Close() })
	return s
}

type fakeGranter struct {
	mu      sync.Mutex
	queries int
	batches [][]string
	url     string // every suffix resolves to this URL
	err     error
}

func (f *fakeGranter) Query(_ context.Context, q wire.ReadQuery) (wire.ReadGrant, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.queries++
	f.batches = append(f.batches, append([]string(nil), q.Suffixes...))
	if f.err != nil {
		return wire.ReadGrant{}, f.err
	}
	g := wire.ReadGrant{JobID: q.JobID, QueryID: q.QueryID}
	for _, s := range q.Suffixes {
		g.Grants = append(g.Grants, wire.ReadGrantItem{Suffix: s, URL: f.url})
	}
	return g, nil
}

// wrongSuffixGranter answers every query with a grant for an object nobody
// asked for. Its URL is a REACHABLE one, so that a store which used the grant
// anyway would visibly fetch the other object's bytes rather than fail on the
// unrelated ground that an empty URL is not https.
type wrongSuffixGranter struct{ url string }

func (g *wrongSuffixGranter) Query(_ context.Context, q wire.ReadQuery) (wire.ReadGrant, error) {
	return wire.ReadGrant{
		JobID:   q.JobID,
		QueryID: q.QueryID,
		Grants:  []wire.ReadGrantItem{{Suffix: "chunks/zz/other", URL: g.url}},
	}, nil
}

func TestGetFetchesTheBytesTheGrantPointsAt(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("chunk-bytes"))
	}))
	defer srv.Close()
	g := &fakeGranter{url: srv.URL}
	s := newTestStore(t, g)

	got, err := s.Get(context.Background(), "chunks/ab/abcd")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if string(got) != "chunk-bytes" {
		t.Fatalf("Get = %q, want %q", got, "chunk-bytes")
	}
}

func TestExpectMakesGetAskForAWholeWindowAtOnce(t *testing.T) {
	// 600 chunks must cost 3 batches, not 600 round trips. One presigned URL
	// per chunk in job params is what spec §6.1 rejected outright.
	//
	// This fixture is deliberately NON-repetitive -- every suffix is fetched
	// exactly once, in order -- which is exactly why it could not catch the
	// repeated-reference collapse a dedup store must expect as normal.
	// TestARepeatedReferenceBehindTheWindowDoesNotCollapseBatching below
	// covers that case.
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("x"))
	}))
	defer srv.Close()
	g := &fakeGranter{url: srv.URL}
	s := newTestStore(t, g)

	want := make([]string, 600)
	for i := range want {
		want[i] = fmt.Sprintf("chunks/ab/%04x", i)
	}
	s.Expect(want)
	for _, suffix := range want {
		if _, err := s.Get(context.Background(), suffix); err != nil {
			t.Fatalf("Get(%s): %v", suffix, err)
		}
	}

	if g.queries != 3 {
		t.Fatalf("fetching %d suffixes took %d batches, want 3 (ceil(600/256))", len(want), g.queries)
	}
	for i, b := range g.batches {
		if len(b) > readBatchMax {
			t.Fatalf("batch %d asked for %d suffixes, want <= %d", i, len(b), readBatchMax)
		}
	}
	if n := len(g.batches[0]); n != readBatchMax {
		t.Fatalf("the first batch asked for %d suffixes, want a full window of %d", n, readBatchMax)
	}
}

func TestAWindowThatLandsExactlyOnTheBoundaryTakesOneBatch(t *testing.T) {
	// The off-by-one the spec's §6 calls out by name.
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("x"))
	}))
	defer srv.Close()
	g := &fakeGranter{url: srv.URL}
	s := newTestStore(t, g)

	want := make([]string, readBatchMax)
	for i := range want {
		want[i] = fmt.Sprintf("chunks/ab/%04x", i)
	}
	s.Expect(want)
	for _, suffix := range want {
		if _, err := s.Get(context.Background(), suffix); err != nil {
			t.Fatalf("Get(%s): %v", suffix, err)
		}
	}
	if g.queries != 1 {
		t.Fatalf("exactly %d suffixes took %d batches, want 1", readBatchMax, g.queries)
	}
}

// TestARepeatedReferenceBehindTheWindowDoesNotCollapseBatching is the
// controller's probe, made permanent. A store's dedup premise is that the
// SAME chunk can be fetched more than once (a chunk shared by two files is
// announced once via Expect but fetched once per reference), so a repeated
// reference behind the current window is the normal case, not an edge case.
//
// Announce 600 suffixes, fetch 0-299 in order (two full windows' worth of
// grants get signed), re-fetch suffix 0 (behind the current window), then
// fetch 300-599. This must still cost exactly ceil(600/256) = 3 grant
// requests: window() must be able to find a suffix behind s.at without
// falling back to a single-suffix grant that (via grantFor replacing s.urls
// wholesale) evicts the live window and collapses every request after it to
// one grant per chunk -- the measured defect was 216 requests for this exact
// scenario.
func TestARepeatedReferenceBehindTheWindowDoesNotCollapseBatching(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("x"))
	}))
	defer srv.Close()
	g := &fakeGranter{url: srv.URL}
	s := newTestStore(t, g)

	want := make([]string, 600)
	for i := range want {
		want[i] = fmt.Sprintf("chunks/ab/%04x", i)
	}
	s.Expect(want)

	for _, suffix := range want[:300] {
		if _, err := s.Get(context.Background(), suffix); err != nil {
			t.Fatalf("Get(%s): %v", suffix, err)
		}
	}
	if _, err := s.Get(context.Background(), want[0]); err != nil {
		t.Fatalf("re-fetching %s: %v", want[0], err)
	}
	for _, suffix := range want[300:] {
		if _, err := s.Get(context.Background(), suffix); err != nil {
			t.Fatalf("Get(%s): %v", suffix, err)
		}
	}

	if g.queries != 3 {
		t.Fatalf("PROBE queries=%d batchsizes(first12)=%v, want 3 (ceil(600/256))",
			g.queries, firstBatchSizes(g.batches, 12))
	}
}

// firstBatchSizes reports the length of up to the first n batches, for a
// failure message shaped like the controller's measurement.
func firstBatchSizes(batches [][]string, n int) []int {
	if len(batches) < n {
		n = len(batches)
	}
	sizes := make([]int, n)
	for i := 0; i < n; i++ {
		sizes[i] = len(batches[i])
	}
	return sizes
}

// TestTheUrlCacheStaysBoundedAcrossManyWindows proves the fix for the
// collapse does not trade unbounded batching for unbounded memory: merging
// grants into s.urls instead of replacing it must still keep the cache
// bounded, or a restore over ~1M chunks would hold hundreds of megabytes of
// resident bearer credentials (the exact cost the old wholesale-replace
// existed to avoid -- see grantFor's comment for the bound chosen).
func TestTheUrlCacheStaysBoundedAcrossManyWindows(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("x"))
	}))
	defer srv.Close()
	g := &fakeGranter{url: srv.URL}
	s := newTestStore(t, g)

	const n = 5000
	want := make([]string, n)
	for i := range want {
		want[i] = fmt.Sprintf("chunks/ab/%04x", i)
	}
	s.Expect(want)

	for _, suffix := range want {
		if _, err := s.Get(context.Background(), suffix); err != nil {
			t.Fatalf("Get(%s): %v", suffix, err)
		}
		s.mu.Lock()
		n := len(s.urls)
		s.mu.Unlock()
		if n > maxCachedURLs {
			t.Fatalf("s.urls holds %d entries after fetching %s, want at most maxCachedURLs=%d",
				n, suffix, maxCachedURLs)
		}
	}
}

func TestAnExpiredUrlIsRetriedWithAFreshGrant(t *testing.T) {
	// Reads are idempotent, so an expiry costs a re-request rather than a
	// failed restore. 403 is what an expired presigned GET returns.
	var hits int32
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if atomic.AddInt32(&hits, 1) == 1 {
			w.WriteHeader(http.StatusForbidden)
			return
		}
		_, _ = w.Write([]byte("chunk-bytes"))
	}))
	defer srv.Close()
	g := &fakeGranter{url: srv.URL}
	s := newTestStore(t, g)

	got, err := s.Get(context.Background(), "chunks/ab/abcd")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	if string(got) != "chunk-bytes" {
		t.Fatalf("Get = %q after retry, want the bytes", got)
	}
	if g.queries < 2 {
		t.Fatalf("the store asked for %d grants; an expired URL must be re-requested", g.queries)
	}
}

func TestAPermanentlyForbiddenFetchGivesUpBounded(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	}))
	defer srv.Close()
	g := &fakeGranter{url: srv.URL}
	s := newTestStore(t, g)

	if _, err := s.Get(context.Background(), "chunks/ab/abcd"); err == nil {
		t.Fatal("a permanently forbidden fetch returned nil error; a restore would loop")
	}
	// == , not <=: a 403 is `retry: true` in fetch, so the loop is
	// SUPPOSED to spend every attempt before giving up. `queries >
	// readAttempts` only proves the bound isn't exceeded -- setting the 403
	// branch to `retry: false` and giving up after one query would also
	// satisfy that, and would pass here despite deleting the retry entirely.
	// Pinning `==` requires the retry path to actually have run.
	if g.queries != readAttempts {
		t.Fatalf("asked for %d grants, want exactly %d -- the bound must be exercised, not merely not exceeded",
			g.queries, readAttempts)
	}
}

func TestADroppedCorrelationFailsWithTheGrantersReason(t *testing.T) {
	g := &fakeGranter{err: errors.New("readreply: gateway connection lost while awaiting a grant")}
	s := newTestStore(t, g)
	_, err := s.Get(context.Background(), "chunks/ab/abcd")
	if err == nil || !strings.Contains(err.Error(), "connection lost") {
		t.Fatalf("Get with a failing granter = %v, want it to carry the granter's reason", err)
	}
	if g.queries != 1 {
		t.Fatalf("the granter was asked %d times; a granter failure is not fixed by asking it "+
			"again the same way, so it must not be retried inside the fetch loop", g.queries)
	}
}

func TestAGrantForADifferentSuffixIsNotUsed(t *testing.T) {
	// A grant that answered with the wrong object would restore one file's
	// bytes into another's. The id check in restore.go would catch it, but
	// this store must not present it as the requested object in the first
	// place.
	var reached int32
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&reached, 1)
		_, _ = w.Write([]byte("another-objects-bytes"))
	}))
	defer srv.Close()
	g := &wrongSuffixGranter{url: srv.URL}
	s := newTestStore(t, g)
	_, err := s.Get(context.Background(), "chunks/ab/abcd")
	if err == nil {
		t.Fatal("a grant naming a different suffix was accepted")
	}
	// Not merely `err != nil`. Deleting the coverage check leaves the URL
	// empty, and an empty URL then fails the scheme rule one line later -- an
	// error, from a store that no longer checks what it was granted. Asserting
	// only on `err != nil` here would pass under exactly that mutation, so the
	// refusal itself is pinned.
	if !strings.Contains(err.Error(), "did not cover the requested object") {
		t.Fatalf("Get = %v, want the grant-coverage refusal", err)
	}
	if n := atomic.LoadInt32(&reached); n != 0 {
		t.Fatalf("the store fetched %d times under a grant naming another object", n)
	}
}

func TestAPlaintextGrantUrlIsRefused(t *testing.T) {
	// The write path's rule, unchanged: a grant is a bearer credential and the
	// URL carries it in the clear.
	g := &fakeGranter{url: "http://store.example/o?sig=x"}
	s := newTestStore(t, g)
	if _, err := s.Get(context.Background(), "chunks/ab/abcd"); err == nil ||
		!strings.Contains(err.Error(), "https") {
		t.Fatalf("Get against an http grant = %v, want it refused for its scheme", err)
	}
}

func TestGetRefTurnsAServerIssuedReferenceIntoAOneSuffixQuery(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("manifest-bytes"))
	}))
	defer srv.Close()
	g := &fakeGranter{url: srv.URL}
	s := newTestStore(t, g) // built with StorageRef "org1/backup/repo1"

	got, err := s.GetRef(context.Background(), "s3://the-bucket/org1/backup/repo1/manifests/m1")
	if err != nil {
		t.Fatalf("GetRef: %v", err)
	}
	if string(got) != "manifest-bytes" {
		t.Fatalf("GetRef = %q, want the manifest bytes", got)
	}
	if len(g.batches) != 1 || len(g.batches[0]) != 1 || g.batches[0][0] != "manifests/m1" {
		t.Fatalf("GetRef asked for %v, want one query for the suffix manifests/m1", g.batches)
	}
}

func TestGetRefRefusesAReferenceOutsideThisRepository(t *testing.T) {
	g := &fakeGranter{url: "https://x"}
	s := newTestStore(t, g)
	_, err := s.GetRef(context.Background(), "s3://the-bucket/other-org/backup/repo9/manifests/m1")
	if err == nil {
		t.Fatal("a manifest reference outside this repository's prefix was accepted")
	}
	// Not merely `err != nil`. A suffixOfRef that fell back to (say) the
	// last two path segments would resolve ANOTHER repository's manifest
	// ref and go on to query for it -- the wrong repository's object, under
	// this device's own credentials -- and still return SOME error (the
	// unreachable "https://x" dial that follows), which `err != nil` alone
	// cannot tell apart from a correct, in-package refusal. The refusal
	// text and queries==0 pin that the granter was never asked at all.
	if !strings.Contains(err.Error(), "does not lie under") {
		t.Fatalf("GetRef = %v, want the prefix refusal", err)
	}
	if strings.Contains(err.Error(), "other-org") {
		t.Fatalf("the refusal echoed the reference: %v", err)
	}
	if g.queries != 0 {
		t.Fatalf("the granter was queried %d times for a reference outside this repository; "+
			"it must be refused before ever asking", g.queries)
	}
}

func TestAStoreWithNoGranterStillRefusesWithAStatedReason(t *testing.T) {
	// `Open` (no granter) is what every existing caller and test fake gets.
	// It must refuse as a standing incapacity, so restore.go's jobs.Refusal
	// branch still reports `refused` rather than a failed restore.
	s := newTestStore(t, nil)
	_, err := s.Get(context.Background(), "chunks/ab/abcd")
	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Fatalf("Get with no granter = %v, want a jobs.Refusal", err)
	}
}

// signedURLMarker stands in for the live part of a presigned GET: on a real
// grant this would be `X-Amz-Signature=<bytes that authorize the bearer>`.
// If this substring, or the equally distinctive host below, ever reaches an
// error string, that string is not safe for a job result or the
// hash-chained audit log -- both are effectively permanent and both are
// where fetch's returned error ends up (restore.go:80, through redact,
// which does not touch a *url.Error).
const signedURLMarker = "X-Amz-Signature=deadbeefcafe"

// TestATransportFailureDoesNotLeakTheSignedURL reproduces FINDING 1
// verbatim: fetch used to wrap client.Do's *url.Error whole, and
// url.Error.Error() embeds the full request URL -- bucket, key and live
// signature -- so a transient transport failure (a dead host, a mid-window
// timeout, "Bad Gateway") put a bearer credential into the job result and
// from there into the audit log.
func TestATransportFailureDoesNotLeakTheSignedURL(t *testing.T) {
	s := newTestStore(t, &fakeGranter{})
	// acme-backups.example.invalid is guaranteed by RFC 2606 never to
	// resolve, so this reliably drives the TRANSPORT branch (a DNS
	// failure), not the redirect branch, on every machine and network.
	rawURL := "https://acme-backups.example.invalid/org1/backup/repo1/chunks/ab/abcd?" + signedURLMarker
	_, _, err := s.fetch(context.Background(), rawURL)
	if err == nil {
		t.Fatal("fetch against an unreachable host returned a nil error")
	}
	if strings.Contains(err.Error(), signedURLMarker) {
		t.Fatalf("fetch leaked the signature into its error: %v", err)
	}
	if strings.Contains(err.Error(), "acme-backups.example.invalid") {
		t.Fatalf("fetch leaked the host/bucket/key into its error: %v", err)
	}
}

// TestARefusedRedirectDoesNotLeakTheSignedURL is Finding 1's other half: the
// CheckRedirect branch wrapped the same *url.Error, whole, via `%w`.
//
// net/http's Client sets that *url.Error's URL field to the REDIRECT
// TARGET (client.go's `uerr`, overridden to `loc` on a CheckRedirect
// refusal), not to the URL fetch was called with -- so an ordinary
// cross-host redirect would not carry the original signed URL at all, and a
// test built that way would pass whether or not the leak existed. The
// server here redirects to ITSELF, echoing the same path and query back as
// the Location header, so the target URL IS the signed one -- the shape
// that actually exercises the bug, and does so identically to how a
// compromised or misbehaving object-store endpoint could redirect a signed
// GET back onto a URL carrying its own signature.
func TestARefusedRedirectDoesNotLeakTheSignedURL(t *testing.T) {
	var srv *httptest.Server
	srv = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, srv.URL+r.URL.RequestURI(), http.StatusFound)
	}))
	defer srv.Close()
	s := newTestStore(t, &fakeGranter{})
	rawURL := srv.URL + "/chunks/ab/abcd?" + signedURLMarker
	_, _, err := s.fetch(context.Background(), rawURL)
	if err == nil {
		t.Fatal("fetch across a refused redirect returned a nil error")
	}
	// The fix must wrap the errRedirectRefused SENTINEL, not the *url.Error
	// the client returned, so errors.Is still finds it downstream -- see
	// that sentinel's comment in backupstore.go for why identity matters.
	if !errors.Is(err, errRedirectRefused) {
		t.Fatalf("fetch = %v, want it to still satisfy errors.Is(err, errRedirectRefused)", err)
	}
	if strings.Contains(err.Error(), signedURLMarker) {
		t.Fatalf("fetch leaked the signature into its error: %v", err)
	}
}
