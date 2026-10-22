// Package backupstore spends the control plane's short-lived object-store
// grants: a prefix-scoped S3 POST policy for writes, and batched signed GETs
// for reads.
//
// # What this package is allowed to be
//
// This agent holds NO object-store credential and must never be given one: a
// long-lived key with write access to every tenant's objects, on a machine
// whose threat model is "may be compromised", is invariant #3 inverted. Every
// write here is authorized by a short-lived POST policy the control plane
// minted for one job, one repository and one prefix, and handed back on the
// dedup exchange. Nothing is held between jobs.
//
// # A second trust domain, and why it is acceptable
//
// The gateway is reached on :8443 with a certificate pinned to the internal CA
// at enrollment. Object storage is reached on :443 with the operator's
// publicly-trusted certificate, which the internal CA cannot validate, so this
// client uses the SYSTEM trust store. That is a real widening: a machine with a
// hostile root CA installed could MITM an upload.
//
// It is bounded because the payload is ALREADY SEALED with the repository key
// before it reaches this package. A MITM sees ciphertext; it can withhold or
// corrupt an upload, not read one. That property is what makes system roots
// acceptable here, so it must not be "simplified" later by moving encryption
// server-side. It holds in the read direction too: what comes back is
// ciphertext, and restore.go re-hashes every chunk against the id its manifest
// names, so a substituted body fails there rather than landing in a file.
//
// # Reads travel in windows of signed GETs
//
// SigV4 query signing cannot express a prefix-scoped GET, so a read grant is
// signed PER OBJECT. Asking for one per Get would be a gateway round trip per
// chunk, and a restore's chunk selection runs to six figures -- the objection
// docs/modules/backup/spec.md §6.1 raised against reads at all. So the
// restorer announces its fetch ORDER through Expect, and a Get that misses the
// current window asks for up to 256 suffixes at once, starting at the one being
// fetched (readBatchMax; the control plane's READ_BATCH_MAX rejects a larger
// batch at the parse layer). Those URLs live 900 seconds (READ_GRANT_TTL), and
// a read is idempotent, so an expiry mid-window costs a re-request rather than
// a failed restore. A store never told an order degrades to a window of one:
// correct, and slow.
//
// A query names only the JOB and suffixes RELATIVE to the repository: the
// control plane resolves the repository, the prefix and the tenancy from the
// job it signed, so this agent cannot express a key outside its own repository
// (docs/superpowers/specs/2026-08-13-prefix-scoped-read-design.md §2.2).
//
// Two limits of that design are stated here rather than discovered later (§7 of
// the same document): prefix scoping does not stop a compromised agent from
// requesting chunks belonging to ANOTHER backup point of its own organisation,
// which would need a chunk index or a manifest membership check to close; and a
// restore reads sequentially, so it is slower than the link allows.
package backupstore

import (
	"bytes"
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/toeshee/it-suite/agent/internal/capabilities/backup"
	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

const (
	// uploadTimeout bounds one object upload, end to end. It is the http.Client's
	// own Timeout, so it also caps a read; readTimeout below is the read's
	// stated bound and is applied per request through the context.
	uploadTimeout = 5 * time.Minute
	// maxAttempts is 1 try plus 2 retries, for TRANSPORT errors only. Put
	// indexes retryBackoff by attempt number WITHOUT clamping, so raising
	// this without extending retryBackoff to match panics Put mid-upload.
	// See that variable's comment.
	maxAttempts = 3
)

const (
	// readBatchMax is how many suffixes one grant request covers. NOT the
	// write side's 1024: no bytes move on the write exchange, so its batch is
	// free, while every grant here is ~1 MiB to fetch. 1024 keys is ~1 GiB per
	// batch, long enough on a slow link that URLs expire mid-batch and the
	// restore thrashes. 256 (~256 MiB, ~3.5 min on 10 Mbps) sits comfortably
	// inside the server's 15-minute window. It must stay <= the control
	// plane's READ_BATCH_MAX, which rejects a larger batch at the parse layer.
	readBatchMax = 256
	// readAttempts is 1 try plus 2 retries. A read is idempotent, so an
	// expired URL costs a re-request rather than a failed restore; the bound
	// is what stops a genuinely forbidden key from looping forever.
	readAttempts = 3
	// readTimeout bounds one object fetch, end to end.
	readTimeout = 5 * time.Minute
	// maxReadObject is a backstop against a hostile or broken store answering
	// a 1 MiB chunk request with an unbounded body and exhausting this
	// agent's memory. It is NOT a tight bound: the manifest is the largest
	// object a restore reads and its size is not known here (restore params
	// carry no dedup_params), so this is deliberately generous.
	maxReadObject = 512 << 20
	// maxCachedURLs bounds how many signed GET URLs s.urls holds at once: the
	// current window plus the one before it. Merging grants into s.urls
	// (instead of replacing it) is what lets a repeated reference behind the
	// current window be served without a fallback grant evicting the window
	// still in use -- see grantFor's comment. Without SOME bound that would
	// grow across the whole restore: a 1 TB restore is ~1M chunks, and
	// holding every URL ever signed would be hundreds of megabytes of
	// resident bearer credentials. Two windows is enough for the common
	// dedup case (a chunk referenced again shortly after its window
	// advanced) while staying flat regardless of restore size; a reference
	// further behind than that pays for one more grant instead of holding a
	// third window on the chance it recurs.
	maxCachedURLs = 2 * readBatchMax
)

// ReadGranter mints short-lived signed GETs by asking the control plane.
//
// Deliberately narrow, and deliberately not a credential: this agent holds no
// object-store key and must never be given one (invariant #3). Every read is
// authorized by a URL the control plane signed for one job, one repository and
// one window. *readreply.Registry satisfies it.
type ReadGranter interface {
	Query(ctx context.Context, q wire.ReadQuery) (wire.ReadGrant, error)
}

// retryBackoff holds maxAttempts-1 waits, indexed by attempt number. Its two
// callers do not agree on what to do past the end of the table: Put (below)
// indexes it bare on the assumption that it never retries past
// maxAttempts-1, while the read loop in Get clamps to the last entry instead
// of indexing straight, because readAttempts is a SEPARATE constant that
// could outgrow this table without anyone touching Put. Raising maxAttempts
// without extending this table panics Put mid-upload -- in the field, where
// no test is watching.
var retryBackoff = []time.Duration{time.Second, 2 * time.Second}

// testTLSConfig overrides the upload client's TLS configuration.
//
// Unexported on purpose, and settable only from this package's own tests: no
// build tag, no environment variable, nothing an operator or a deployment can
// reach. It mirrors internal/transport's `dialTLS` seam and exists for the
// same reason -- the https-only rule below is a security control, so the
// tests proving it must run against a REAL https endpoint rather than have
// the rule relaxed for their convenience.
//
// nil -- the production value -- means the system trust store.
var testTLSConfig *tls.Config

// errRedirectRefused is what CheckRedirect returns. It is a package-level
// sentinel, constructed exactly once, rather than a fresh errors.New per
// call: errors.Is compares by identity, and http.Client wraps whatever
// CheckRedirect returns in a *url.Error (net/http/client.go's `uerr`), so
// detecting it downstream after client.Do requires errors.Is to unwrap that
// *url.Error back to the SAME value this hook returned -- a new error string
// each call, or a plain string comparison, would not survive that wrapping.
var errRedirectRefused = errors.New("backupstore: refused to follow a redirect away from the granted endpoint")

// scrubURLErr replaces a transport error with a CLASSIFICATION that cannot
// carry the endpoint, the bucket, the object key or the signature.
//
// Every grant URL this package handles -- the write side's POST endpoint and
// the read side's presigned GET -- is a bearer credential (see the package
// doc and the scheme checks below). http.Client wraps every transport
// failure in a *url.Error whose Error() embeds the request URL; left wrapped
// with `%w`, that string reaches a job result and from there the hash-chained
// audit log, both effectively permanent.
//
// UNWRAPPING THE *url.Error IS NOT ENOUGH, AND CI PROVED IT RATHER THAN
// REVIEW. An earlier version returned `ue.Err` and was verified by hand
// against a host that failed to resolve -- but that check ran behind an
// HTTPS proxy, so the dial never left the process and the error came back as
// a plain "Bad Gateway" with no hostname in it. On a runner with no proxy the
// same code produced:
//
//	dial tcp: lookup acme-backups.example.invalid on 127.0.0.53:53: no such host
//
// because the cause is a *net.OpError wrapping a *net.DNSError, and
// DNSError.Error() embeds the name it could not resolve. For virtual-host
// style S3 the BUCKET IS THAT HOSTNAME. *net.OpError is the same shape of
// hazard: its Error() includes Addr.
//
// So this does not unwrap and hope. It maps each failure onto a fixed
// sentence chosen for an operator reading a job result, and returns text it
// constructed rather than text the network handed it. The unclassified case
// deliberately says nothing about the error at all: losing detail on a rare
// failure is the cheaper mistake.
//
// The two context sentinels pass through unchanged: they carry no address,
// and callers match them with errors.Is.
func scrubURLErr(err error) error {
	if err == nil {
		return nil
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return err
	}
	var ue *url.Error
	if errors.As(err, &ue) {
		err = ue.Err
	}

	var dnsErr *net.DNSError
	if errors.As(err, &dnsErr) {
		return errors.New("backupstore: the object store's hostname could not be resolved")
	}
	var certErr *tls.CertificateVerificationError
	if errors.As(err, &certErr) {
		return errors.New("backupstore: the object store's TLS certificate was not accepted " +
			"by the system trust store")
	}
	var recordErr tls.RecordHeaderError
	if errors.As(err, &recordErr) {
		return errors.New("backupstore: the object store did not answer with TLS")
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return errors.New("backupstore: the object store did not answer before the deadline")
	}
	var opErr *net.OpError
	if errors.As(err, &opErr) {
		// opErr.Op alone -- "dial", "read", "write". opErr.Error() would also
		// print Addr and Source, which is the whole problem.
		return fmt.Errorf("backupstore: the object store could not be reached (%s failed)", opErr.Op)
	}
	return errors.New("backupstore: the request to the object store failed")
}

// Opener returns an OpenStore function bound to a read-grant source.
// Assignable to backup.Options.OpenStore.
func Opener(g ReadGranter) func(context.Context, backup.Repository) (backup.ObjectStore, error) {
	return func(ctx context.Context, repo backup.Repository) (backup.ObjectStore, error) {
		s, err := open(ctx, repo)
		if err != nil {
			return nil, err
		}
		s.granter = g
		return s, nil
	}
}

// Open returns a job-scoped store with NO read route: Get and GetRef refuse.
// Kept for callers that only write. It dials nothing; the first byte leaves in
// Put.
func Open(ctx context.Context, repo backup.Repository) (backup.ObjectStore, error) {
	return Opener(nil)(ctx, repo)
}

// open builds the store itself. It dials nothing: the first byte leaves in Put
// or in a read's fetch.
func open(_ context.Context, repo backup.Repository) (*store, error) {
	// Cloned from the default transport, not built bare, so proxy settings,
	// dial timeouts and HTTP/2 stay whatever this binary already runs with;
	// only the TLS config is ever touched, and only by the test seam above.
	//
	// The type assertion is comma-ok, not bare: nothing under agent/
	// reassigns http.DefaultTransport today, but a bare assertion would turn
	// an unrelated future change elsewhere in the process into a panic on
	// every backup job, and no test in this package would catch it. The
	// fallback loses the clone's proxy/dial-timeout/HTTP2 defaults, which is
	// an acceptable trade against that panic.
	var tr *http.Transport
	if dt, ok := http.DefaultTransport.(*http.Transport); ok {
		tr = dt.Clone()
	} else {
		tr = &http.Transport{}
	}
	tr.TLSClientConfig = testTLSConfig
	return &store{
		client: &http.Client{
			Timeout:   uploadTimeout,
			Transport: tr,
			// A 302 would turn a client aimed at one signed endpoint into an
			// arbitrary-host client. The grant's URL is the only address this
			// package may talk to.
			CheckRedirect: func(*http.Request, []*http.Request) error {
				return errRedirectRefused
			},
		},
		jobID:  repo.JobID,
		prefix: strings.Trim(repo.StorageRef, "/"),
		urls:   map[string]string{},
	}, nil
}

type store struct {
	client  *http.Client
	granter ReadGranter
	jobID   string
	// prefix is the repository's storage_ref, used only to turn a
	// server-issued `s3://bucket/{prefix}/{suffix}` reference back into the
	// suffix a query can carry.
	prefix string

	// mu makes each individual field access atomic; it does NOT make
	// concurrent Get/GetRef calls safe together. Two Gets racing through
	// grantFor can each win the lock in turn and each overwrite s.urls
	// wholesale (see the comment there), so the second discards the window
	// the first just signed for -- correctness here depends on a restore
	// driving Get sequentially, in the order announced to Expect, not on
	// this mutex.
	mu       sync.Mutex
	expected []string          // the fetch order the restorer announced
	at       int               // how far into `expected` the window has advanced
	urls     map[string]string // suffix -> signed URL, bounded to maxCachedURLs
	// urlOrder is the insertion order of urls' keys, for FIFO eviction: a
	// plain map has no order of its own, and grantFor needs to evict the
	// OLDEST entries first to keep recently-granted ones (the ones a
	// backward reference is likeliest to hit) alive longest.
	urlOrder []string
	seq      int // mints query ids
}

func (s *store) Close() error {
	s.client.CloseIdleConnections()
	return nil
}

// safeSuffix rejects anything that would escape the granted prefix.
//
// S3 would reject it too -- the policy's `starts-with $key` condition is
// enforced by the object store, not here -- but only after the body crossed the
// wire. This is the cheap half of the check, not the authoritative one.
func safeSuffix(suffix string) error {
	switch {
	case suffix == "":
		return errors.New("backupstore: empty object suffix")
	case strings.HasPrefix(suffix, "/"), strings.HasPrefix(suffix, `\`):
		return fmt.Errorf("backupstore: absolute object suffix %q", suffix)
	case strings.Contains(suffix, `\`):
		return fmt.Errorf("backupstore: backslash in object suffix %q", suffix)
	}
	for _, seg := range strings.Split(suffix, "/") {
		if seg == "" || seg == "." || seg == ".." {
			return fmt.Errorf("backupstore: unsafe segment in object suffix %q", suffix)
		}
	}
	return nil
}

func (s *store) Put(ctx context.Context, grant wire.UploadGrant, suffix string, data []byte) (string, error) {
	if err := safeSuffix(suffix); err != nil {
		return "", err
	}
	if grant.MaxBytes > 0 && int64(len(data)) > grant.MaxBytes {
		return "", fmt.Errorf("backupstore: object is %d bytes, over the grant's %d-byte cap",
			len(data), grant.MaxBytes)
	}
	u, err := url.Parse(grant.URL)
	if err != nil {
		// url.Parse's own error is a *url.Error embedding the unparsed input
		// -- the grant URL itself -- so it is scrubbed before it can reach a
		// job result. See scrubURLErr.
		return "", fmt.Errorf("backupstore: unparseable grant endpoint: %w", scrubURLErr(err))
	}
	if u.Scheme != "https" {
		// Never downgraded, not even for a lab: a grant is a bearer credential
		// and the form carries it in the clear.
		return "", fmt.Errorf("backupstore: grant endpoint is %q, and only https is allowed", u.Scheme)
	}

	key := grant.KeyPrefix + suffix
	for attempt := 0; ; attempt++ {
		body, contentType, err := buildForm(grant, key, data)
		if err != nil {
			return "", err
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, grant.URL, bytes.NewReader(body))
		if err != nil {
			// Scrubbed even though it should be unreachable: NewRequestWithContext
			// re-parses the URL and can return a *url.Error of its own, and the
			// url.Parse above already rejected anything malformed. "Unreachable"
			// is a claim about today's code, and this cycle has already shipped
			// one such claim that was false -- the cost of being wrong here is a
			// signed grant in the audit log, so it is not worth reasoning about.
			return "", scrubURLErr(err)
		}
		req.Header.Set("Content-Type", contentType)

		resp, err := s.client.Do(req)
		if err != nil && errors.Is(err, errRedirectRefused) {
			// Same determinism argument as the 4xx branch below, not merely
			// the same words: a redirect will recur identically against the
			// same URL, so replaying it buys nothing and resends the sealed
			// payload to a redirecting endpoint repeatedly instead of once.
			// http.Client wraps this in a *url.Error, and that wrapper's URL
			// field is attacker/endpoint influenced (the redirect target) --
			// wrap the SENTINEL directly, not err, so no URL rides along and
			// errors.Is still finds it downstream.
			return "", fmt.Errorf("%w (a redirect recurs identically, so it is not retried)", errRedirectRefused)
		}
		if err != nil {
			// A genuine transport failure (dial, TLS, timeout): http.Client
			// wraps it in a *url.Error embedding the request URL, which
			// carries the POST grant endpoint. Scrubbed before it can fall
			// through to the wrap below.
			err = scrubURLErr(err)
		}
		if err == nil {
			_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4<<10))
			_ = resp.Body.Close()
			if resp.StatusCode >= 200 && resp.StatusCode < 300 {
				return suffix, nil
			}
			// 4xx is the signed policy's verdict -- wrong prefix, over the size
			// cap, expired. It will not change on a retry, and the response body
			// is deliberately not included: it can echo request fields.
			if resp.StatusCode < 500 {
				return "", fmt.Errorf("backupstore: the object store refused the upload with %d "+
					"(the signed policy's verdict, not a transient)", resp.StatusCode)
			}
			err = fmt.Errorf("backupstore: object store returned %d", resp.StatusCode)
		}
		if attempt >= maxAttempts-1 {
			return "", fmt.Errorf("backupstore: upload failed after %d attempts: %w", maxAttempts, err)
		}
		// Safe to retry: the store is content-addressed, so this rewrites
		// identical bytes to the same key.
		select {
		case <-time.After(retryBackoff[attempt]):
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}
}

// buildForm assembles the S3 POST body. `file` MUST be the last part: S3
// ignores every field after it.
func buildForm(grant wire.UploadGrant, key string, data []byte) ([]byte, string, error) {
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	// `key` is overridden rather than copied: boto3 returns it as the
	// placeholder "{prefix}/${filename}", and the signed condition is
	// `starts-with $key "{prefix}/"`, which any conforming literal satisfies.
	if err := w.WriteField("key", key); err != nil {
		return nil, "", err
	}
	for k, v := range grant.Fields {
		if k == "key" {
			continue
		}
		if err := w.WriteField(k, v); err != nil {
			return nil, "", err
		}
	}
	part, err := w.CreateFormFile("file", "object")
	if err != nil {
		return nil, "", err
	}
	if _, err := part.Write(data); err != nil {
		return nil, "", err
	}
	if err := w.Close(); err != nil {
		return nil, "", err
	}
	return buf.Bytes(), w.FormDataContentType(), nil
}

// noReadRoute is what a store opened without a grant source returns. It is a
// jobs.Refusal, not an error, because it is a standing incapacity of this
// agent rather than a failed fetch -- restore.go reports the two differently.
//
// The old text here said reads were impossible ("SigV4 query signing cannot
// express a prefix-scoped READ"). They are implemented as of the
// prefix-scoped-READ cycle; see
// docs/superpowers/specs/2026-08-13-prefix-scoped-read-design.md.
const noReadRoute = "backup: this agent has no route to read from the repository. Reads " +
	"travel under short-lived GET grants the control plane signs on the read exchange, and " +
	"this store was opened without one"

// Expect announces the order in which suffixes are about to be fetched, so
// grants can be requested a window at a time instead of one per object.
//
// A store that is never told degrades to a window of 1: correct, and slow.
// This is the whole of the interface the restorer needs to announce an order.
func (s *store) Expect(suffixes []string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.expected = append(s.expected[:0], suffixes...)
	s.at = 0
	s.urls = map[string]string{}
	s.urlOrder = nil
}

func (s *store) Get(ctx context.Context, suffix string) ([]byte, error) {
	if s.granter == nil {
		return nil, jobs.Refuse(noReadRoute)
	}
	if err := safeSuffix(suffix); err != nil {
		return nil, err
	}
	var last error
	for attempt := 0; attempt < readAttempts; attempt++ {
		u, err := s.grantFor(ctx, suffix)
		if err != nil {
			// A granter failure is a dropped correlation, a refused query, or
			// a lost connection. None is fixed by asking the same way again
			// inside this loop; the restore fails with the stated reason.
			return nil, fmt.Errorf("backupstore: no read grant for the requested object: %w", err)
		}
		data, retry, err := s.fetch(ctx, u)
		if err == nil {
			return data, nil
		}
		last = err
		if !retry {
			return nil, err
		}
		// The URL may simply have expired mid-window. Drop the whole window so
		// the next attempt signs a fresh one; a read is idempotent, so this
		// re-fetches identical bytes.
		s.invalidate()
		if attempt < readAttempts-1 {
			// Clamped to the last entry rather than indexed straight. The
			// backoff table holds readAttempts-1 waits TODAY, so a bare
			// retryBackoff[attempt] is correct today and panics the moment
			// someone raises readAttempts without extending it -- in the middle
			// of a customer's restore, where a test would not be watching.
			wait := retryBackoff[len(retryBackoff)-1]
			if attempt < len(retryBackoff) {
				wait = retryBackoff[attempt]
			}
			select {
			case <-time.After(wait):
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
	}
	return nil, fmt.Errorf("backupstore: read failed after %d attempts: %w", readAttempts, last)
}

func (s *store) GetRef(ctx context.Context, ref string) ([]byte, error) {
	// Checked BEFORE suffixOfRef, not after: a granter-less store (Open's
	// only production path today) has no read route at all, which is a
	// standing incapacity of this device and must come back as a
	// jobs.Refusal so restore.go reports `refused` rather than `failed`
	// (see noReadRoute's comment, and Get's identical check above it).
	// suffixOfRef's error is a plain error -- correct for an unparseable
	// reference, wrong for "this store was never given a granter" -- so
	// running it first would misreport every granter-less GetRef.
	if s.granter == nil {
		return nil, jobs.Refuse(noReadRoute)
	}
	suffix, err := suffixOfRef(ref, s.prefix)
	if err != nil {
		return nil, err
	}
	return s.Get(ctx, suffix)
}

// suffixOfRef turns a server-issued `s3://{bucket}/{storage_ref}/{suffix}`
// reference into the repository-relative suffix a read query can carry.
//
// The manifest reaches this agent as a full reference because the control
// plane builds one (dispatch.py::_resolve_manifest_ref) -- the agent does not
// know the bucket and must not. Rather than give the manifest its own wire
// path, the suffix is recovered here and travels as an ordinary one-suffix
// query, under the same prefix rule as any chunk.
//
// The error names no part of the reference: it reaches the job result and the
// audit log.
func suffixOfRef(ref, prefix string) (string, error) {
	if prefix == "" {
		return "", errors.New("backupstore: this store has no repository prefix, so a manifest " +
			"reference cannot be resolved to a suffix")
	}
	marker := "/" + prefix + "/"
	i := strings.Index(ref, marker)
	if i < 0 {
		return "", errors.New("backupstore: the manifest reference does not lie under this " +
			"repository's prefix")
	}
	return ref[i+len(marker):], nil
}

// grantFor returns a signed URL for suffix, requesting a whole window if the
// current one does not hold it.
func (s *store) grantFor(ctx context.Context, suffix string) (string, error) {
	s.mu.Lock()
	if u, ok := s.urls[suffix]; ok {
		s.mu.Unlock()
		return u, nil
	}
	batch := s.window(suffix)
	s.seq++
	q := wire.ReadQuery{JobID: s.jobID, QueryID: fmt.Sprintf("r%d", s.seq), Suffixes: batch}
	s.mu.Unlock()

	grant, err := s.granter.Query(ctx, q)
	if err != nil {
		return "", err
	}

	s.mu.Lock()
	s.mergeGrant(grant.Grants)
	u, ok := s.urls[suffix]
	s.mu.Unlock()
	if !ok {
		// A grant that answered with objects other than the one asked for
		// would, if used, restore one file's bytes as another's. restore.go
		// re-hashes every chunk and would catch it, but this store must not
		// present it as the requested object in the first place.
		return "", errors.New("backupstore: the read grant did not cover the requested object")
	}
	return u, nil
}

// mergeGrant adds a grant's URLs into the live cache instead of replacing it.
// A store is fed by two shapes of query -- the forward window in window()'s
// first loop, and a fallback (single suffix, or a short backward window) for
// a reference window() finds behind s.at -- and REPLACING s.urls on either
// one discards whatever the other shape is still relying on: a fallback
// query for one suffix behind the window would evict the live forward
// window, and every in-order suffix after it would then also miss the
// window and fall back, one grant per chunk, with nothing to recover it.
// That is the collapse this function exists to close. See window()'s
// comment for the matching half of the fix.
//
// The cache is bounded to maxCachedURLs by FIFO eviction (oldest-inserted
// suffix goes first) so merging cannot grow it across the life of a restore;
// see that constant's comment for the bound and why. Caller holds s.mu.
func (s *store) mergeGrant(items []wire.ReadGrantItem) {
	for _, item := range items {
		if _, ok := s.urls[item.Suffix]; ok {
			// Already cached (a re-granted overlap, e.g. a backward window
			// that reaches into territory the forward window already
			// covers). Refresh the URL -- the new grant is the fresher one
			// -- but leave its place in urlOrder alone; re-inserting it
			// would count it twice against the eviction bound below.
			s.urls[item.Suffix] = item.URL
			continue
		}
		s.urls[item.Suffix] = item.URL
		s.urlOrder = append(s.urlOrder, item.Suffix)
	}
	// Evict oldest-first until back at the bound. copy+reslice, not
	// re-slicing from the front: re-slicing would shrink urlOrder's
	// capacity by one on every eviction, forcing a fresh backing-array
	// allocation on nearly every subsequent append across a restore of
	// millions of chunks. copy keeps one backing array for urlOrder's
	// whole life, sized to its high-water mark.
	for len(s.urls) > maxCachedURLs {
		delete(s.urls, s.urlOrder[0])
		copy(s.urlOrder, s.urlOrder[1:])
		s.urlOrder = s.urlOrder[:len(s.urlOrder)-1]
	}
}

// window is the batch to ask for, given the suffix being fetched now. Caller
// holds s.mu.
func (s *store) window(suffix string) []string {
	// Find the requested suffix in the announced order, starting where the
	// last window ended -- a restore fetches in that order, so this is O(1) in
	// the normal case and never scans backwards over what is already done.
	for i := s.at; i < len(s.expected); i++ {
		if s.expected[i] != suffix {
			continue
		}
		end := i + readBatchMax
		if end > len(s.expected) {
			end = len(s.expected)
		}
		s.at = end
		return append([]string(nil), s.expected[i:end]...)
	}
	// Not found ahead of s.at: the suffix may still be BEHIND it. A store's
	// whole premise is deduplication, and restore.go's Expect list carries
	// each id once while r.stream fetches once per REFERENCE -- so a chunk
	// shared by two files sends its suffix through Get a second time once
	// the second file's turn comes, and that second Get can land here every
	// time the suffix's window has already advanced past it. That makes this
	// the NORMAL case for a shared chunk, not an edge case reserved for
	// GetRef's manifest.
	//
	// s.at must NOT move for a hit found here: it marks how far the in-order
	// walk has advanced. Dragging it back to the hit's position would make
	// every suffix between the hit and the true position look "ahead" of
	// s.at on the next forward scan, so each of THOSE would also fall
	// through to this same rescan -- the batching would never recover, which
	// is the defect this function exists to close. The window returned here
	// is capped at s.at for the same reason: nothing past s.at needs
	// re-granting, the forward scan above will reach it in its own turn.
	for i := 0; i < s.at && i < len(s.expected); i++ {
		if s.expected[i] != suffix {
			continue
		}
		end := i + readBatchMax
		if end > s.at {
			end = s.at
		}
		return append([]string(nil), s.expected[i:end]...)
	}
	// Genuinely not in the announced order -- GetRef's manifest, for one, or
	// a suffix Get is asked for without ever having been Expect-ed. A window
	// of exactly what was asked for.
	return []string{suffix}
}

func (s *store) invalidate() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.urls = map[string]string{}
	s.urlOrder = nil
	// s.at resets all the way to 0, the start of the WHOLE announced order --
	// not to the start of the window that just expired. The next window()
	// call therefore rescans from the beginning of `expected` to relocate
	// the suffix being fetched, rather than resuming from where the expired
	// window began; on a large restore that rescan is O(n) instead of O(1)
	// for that one call. It is still correct -- window() finds the same
	// suffix and forms the same window either way -- and an expiry is rare
	// enough (the read grant's 900s TTL against windows sized to finish
	// well inside it) that trading the extra bookkeeping a remembered
	// window-start would need for that one-time rescan is the right side of
	// simple.
	s.at = 0
}

// fetch performs one GET. `retry` reports whether the failure is one a fresh
// grant could fix.
func (s *store) fetch(ctx context.Context, rawURL string) (data []byte, retry bool, err error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		// url.Parse's own error is a *url.Error embedding the unparsed
		// input -- the signed GET itself, live signature included -- so it
		// is scrubbed before it can reach a job result. See scrubURLErr.
		return nil, false, fmt.Errorf("backupstore: unparseable read grant endpoint: %w", scrubURLErr(err))
	}
	if u.Scheme != "https" {
		// Never downgraded, not even for a lab: a signed URL is a bearer
		// credential and travels in the clear.
		return nil, false, fmt.Errorf("backupstore: read grant endpoint is %q, and only https is allowed", u.Scheme)
	}
	ctx, cancel := context.WithTimeout(ctx, readTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		// Scrubbed for the same reason as Put's twin above: the url.Parse
		// immediately preceding this should make it unreachable, and an
		// unreachable-claim is not worth a signed grant in the audit log.
		return nil, false, scrubURLErr(err)
	}
	resp, err := s.client.Do(req)
	if err != nil {
		if errors.Is(err, errRedirectRefused) {
			// A redirect recurs identically against the same URL, so
			// replaying it buys nothing. http.Client wraps this refusal in
			// a *url.Error whose URL field is the redirect target -- not
			// under this package's control -- so the SENTINEL is wrapped
			// directly, not err: no URL rides along, and errors.Is still
			// finds it downstream. See errRedirectRefused's comment.
			return nil, false, fmt.Errorf("%w (a redirect recurs identically, so it is not retried)", errRedirectRefused)
		}
		// A transport failure: DNS, dial, TLS, a mid-transfer reset, an
		// intermediary answering for a dead host ("Bad Gateway"). http.Client
		// wraps every one of these in a *url.Error whose Error() embeds the
		// full request URL -- bucket, key and the live SigV4 signature that
		// authorizes it until its TTL expires. That string reaches
		// restore.go's job result and, from there, the hash-chained audit
		// log, so the wrapper is stripped before this error is built; only
		// the underlying cause ("Bad Gateway", "no such host", ...) is kept.
		return nil, true, fmt.Errorf("backupstore: fetching the object: %w", scrubURLErr(err))
	}
	defer func() { _ = resp.Body.Close() }()

	switch {
	case resp.StatusCode == http.StatusOK:
	case resp.StatusCode == http.StatusForbidden, resp.StatusCode == http.StatusUnauthorized:
		// What an expired presigned GET answers. Also what a genuinely
		// unauthorized one answers -- indistinguishable from here, which is
		// why the retry is bounded rather than conditional.
		return nil, true, fmt.Errorf("backupstore: the object store answered %d "+
			"(an expired or unauthorized read grant)", resp.StatusCode)
	case resp.StatusCode >= 500:
		return nil, true, fmt.Errorf("backupstore: object store returned %d", resp.StatusCode)
	default:
		// The response body is deliberately not included: it can echo the key.
		return nil, false, fmt.Errorf("backupstore: the object store refused the read with %d", resp.StatusCode)
	}

	// LimitReader + 1 so an object AT the cap still reads and one over it is
	// detected rather than silently truncated.
	data, err = io.ReadAll(io.LimitReader(resp.Body, maxReadObject+1))
	if err != nil {
		return nil, true, fmt.Errorf("backupstore: reading the object body: %w", err)
	}
	if int64(len(data)) > maxReadObject {
		return nil, false, fmt.Errorf("backupstore: the object store returned more than the "+
			"%d-byte cap for one object", int64(maxReadObject))
	}
	return data, false, nil
}
