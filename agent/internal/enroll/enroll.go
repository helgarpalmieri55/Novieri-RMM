// Package enroll obtains and persists the agent's identity: an Ed25519
// keypair, the certificate the control-plane CA issues over it, and the two
// pins that every later security decision depends on.
//
// Implements docs/kernel/spec.md §7 and docs/03-security-model.md, against
// app/api/v1/enrollment.py and app/services/enrollment.py.
//
// # What is pinned, and why the word means something
//
// Enrollment returns exactly six fields (app/api/v1/enrollment.py:47-53), two
// of which are trust anchors this agent stores and never re-fetches:
//
//   - ca_bundle_pem — the ONLY roots the agent will accept for the gateway.
//     transport.Dial refuses to dial without it, so "pinned" is a fact rather
//     than a claim.
//   - job_signing_public_key_pem — the Ed25519 key every job signature is
//     checked against (invariant #1). It is validated as Ed25519 HERE, at
//     enrollment, not at first job: an agent that stored an unusable pin would
//     look enrolled, connect, and then refuse every job for a reason no
//     operator can see from the console.
//
// # Invariant #3: the device private key
//
// Generated on the endpoint, written 0600 inside a 0700 data directory, and it
// never leaves: not in a log line, not in a job_log, not in any request. Only
// the CSR — the public half plus a proof of possession — is transmitted.
// enroll_test.go asserts the log buffer contains neither the key bytes nor the
// string "PRIVATE KEY".
//
// # Nothing is written until everything validates
//
// A rejected or malformed enrollment leaves the data directory empty, and
// identity.json is renamed into place LAST so it is the commit marker
// config.Enrolled() reads. A partial identity is worse than none: the next
// boot would find it, skip enrollment, and fail mTLS forever with a token that
// has already been consumed.
package enroll

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"log/slog"
	mrand "math/rand/v2"
	"net/http"
	"os"
	"path"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/toeshee/it-suite/agent/internal/config"
)

// EnrollPath is the enrollment endpoint, relative to the API base URL.
//
// It is NOT under /api/v1: app/main.py mounts enroll_router with no prefix.
// Pinned by a test because it is the most guessable-wrong fact in the agent,
// and getting it wrong is a 404 that reads like an outage.
const EnrollPath = "/enroll/v1"

// ErrRejected is the sentinel behind a *Rejected.
var ErrRejected = errors.New("enrollment rejected")

// Rejected is a 4xx from the enrollment endpoint. The server answers a bad,
// expired, revoked or exhausted token with a bare 403 and leaks nothing about
// which check failed, so neither does this.
type Rejected struct {
	Status int
}

func (e *Rejected) Error() string {
	return fmt.Sprintf("enroll: %v (HTTP %d)", ErrRejected, e.Status)
}

func (e *Rejected) Unwrap() error { return ErrRejected }

// Identity is everything the agent needs to be itself.
type Identity struct {
	// DeviceID is the control plane's id for this endpoint. It is also the CN
	// of Cert, and jobs name it in their signed `device` field.
	DeviceID string
	// Cert is the mTLS client identity. Leaf is always populated: Task 6 reads
	// NotAfter from it and the handshake wants it without a re-parse.
	Cert tls.Certificate
	// CACerts is the pinned root set for the gateway.
	CACerts *x509.CertPool
	// GatewayURL is the wss:// URL to dial.
	GatewayURL string
	// JobSigningKey is the pinned Ed25519 key every job signature is verified
	// against. Nothing else may be used for that, ever.
	JobSigningKey ed25519.PublicKey
}

// Options are the knobs LoadOrEnroll takes beyond the process configuration.
//
// Capabilities deliberately arrives from the CALLER rather than being a
// constant in this package: it must come from the real capability registry, so
// the advertisement cannot drift from what the binary can actually run. An
// empty list advertises {} — which is what agents.capabilities already
// defaults to, and what IPAM's dispatcher already tolerates.
type Options struct {
	Capabilities []string
	HTTPClient   *http.Client
	Logger       *slog.Logger

	// wait replaces the link-retry timer in tests. nil is a real timer.
	wait func(context.Context, time.Duration) error
}

// Link-enrollment retry bounds (see Enroll). The first wait is 2.5-5 s, the
// ceiling doubles per attempt to linkRetryCap, and each wait is drawn from the
// upper half of its ceiling so a fleet switched on together does not return
// together. A Retry-After above linkRetryAfterMax is read as linkRetryAfterMax.
const (
	linkRetryBase     = 5 * time.Second
	linkRetryCap      = 5 * time.Minute
	linkRetryAfterMax = time.Hour
)

// linkBackoff is the jittered wait before retry `attempt` (0-based).
func linkBackoff(attempt int) time.Duration {
	ceil := linkRetryCap
	if attempt < 20 {
		if c := linkRetryBase << attempt; c < ceil {
			ceil = c
		}
	}
	half := ceil / 2
	return half + time.Duration(mrand.Int64N(int64(half)+1))
}

// retryAfter reads a Retry-After header -- delay-seconds or an HTTP-date --
// as a duration from now. Absent, malformed or in the past is 0 (use the
// backoff); anything longer than linkRetryAfterMax is linkRetryAfterMax.
func retryAfter(header string, now time.Time) time.Duration {
	header = strings.TrimSpace(header)
	if header == "" {
		return 0
	}
	var d time.Duration
	if secs, err := strconv.Atoi(header); err == nil {
		if secs <= 0 {
			return 0
		}
		if int64(secs) > int64(linkRetryAfterMax/time.Second) {
			return linkRetryAfterMax
		}
		d = time.Duration(secs) * time.Second
	} else if at, err := http.ParseTime(header); err == nil {
		d = at.Sub(now)
	}
	if d <= 0 {
		return 0
	}
	return min(d, linkRetryAfterMax)
}

// pause waits d or until ctx ends, whichever is first.
func (o Options) pause(ctx context.Context, d time.Duration) error {
	if o.wait != nil {
		return o.wait(ctx, d)
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

func (o Options) logger() *slog.Logger {
	if o.Logger != nil {
		return o.Logger
	}
	return slog.Default()
}

func (o Options) client() *http.Client {
	if o.HTTPClient != nil {
		return o.HTTPClient
	}
	return &http.Client{Timeout: 60 * time.Second}
}

// ---------------------------------------------------------------------------
// Persisted shapes
// ---------------------------------------------------------------------------

type persistedIdentity struct {
	DeviceID   string `json:"device_id"`
	GatewayURL string `json:"gateway_url"`
	CertPEM    string `json:"cert_pem"`
	ChainPEM   string `json:"chain_pem"`
}

type persistedPins struct {
	CABundlePEM            string `json:"ca_bundle_pem"`
	JobSigningPublicKeyPEM string `json:"job_signing_public_key_pem"`
}

// enrollRequest implements app/api/v1/enrollment.py::EnrollRequest.
//
// `capabilities` is an ADDITIVE field: an older control plane's EnrollRequest
// is a Pydantic model with default extra behaviour ("ignore"), so an unknown
// key is dropped and enrollment still succeeds. backend/tests/test_enrollment.py
// asserts that tolerance so a future extra="forbid" goes red there rather than
// in the field.
type enrollRequest struct {
	// Exactly ONE of these is set. `omitempty` on `token` is safe for every
	// older control plane: Enroll refuses an empty credential before building
	// the body, so a token enrollment always carries a non-empty token.
	Token string `json:"token,omitempty"`
	// LinkCode is the install link's code (spec 2026-09-28 §1.4). ADDITIVE:
	// a control plane older than the install link answers 422 (no token),
	// which this agent reports as a rejection and keeps the file for.
	LinkCode     string          `json:"link_code,omitempty"`
	CSRPEM       string          `json:"csr_pem"`
	DeviceFacts  deviceFacts     `json:"device_facts"`
	Capabilities map[string]bool `json:"capabilities"`
}

// deviceFacts implements app/services/enrollment.py::DeviceFacts.
type deviceFacts struct {
	Hostname     string  `json:"hostname"`
	OSFamily     string  `json:"os_family"`
	OSVersion    string  `json:"os_version"`
	Architecture string  `json:"architecture"`
	Serial       *string `json:"serial"`
	// MACs is ADDITIVE, added 2026-08-18 for asset-inventory §4 rule 2. The
	// server defaults it to an empty list, so an agent built before this field
	// existed enrols exactly as it did -- which is the compatibility case that
	// matters, because a fleet is always mixed-version and an agent in the
	// field updates on its own schedule.
	//
	// See macs.go for what is in it and, more importantly, what is kept out:
	// the server AUTO-LINKS on these, so a locally administered address here
	// would merge two records rather than merely display something wrong.
	MACs []string `json:"macs"`
}

// enrollResponse implements app/api/v1/enrollment.py::EnrollResponse.
type enrollResponse struct {
	DeviceID               string `json:"device_id"`
	CertPEM                string `json:"cert_pem"`
	ChainPEM               string `json:"chain_pem"`
	CABundlePEM            string `json:"ca_bundle_pem"`
	GatewayURL             string `json:"gateway_url"`
	JobSigningPublicKeyPEM string `json:"job_signing_public_key_pem"`
}

// linkCodeLength is the install link code's length (Task 3: 32 characters of
// secrets.token_urlsafe(24)).
const linkCodeLength = 32

// IsLinkCode reports whether a credential is an install-link code rather than
// an enrollment token. A token is `<uuid>.<secret>` and always carries a dot;
// a link code is 32 characters of [A-Za-z0-9_-] and never does, so the shape
// decides without a second file or a flag -- and the code still reaches the
// agent only through the token file, never argv.
func IsLinkCode(s string) bool {
	if len(s) != linkCodeLength {
		return false
	}
	for _, c := range s {
		switch {
		case c >= 'A' && c <= 'Z', c >= 'a' && c <= 'z', c >= '0' && c <= '9', c == '-', c == '_':
		default:
			return false
		}
	}
	return true
}

// ---------------------------------------------------------------------------

// LoadOrEnroll returns the persisted identity, enrolling once if there is none.
//
// A data directory that HAS an identity is never re-enrolled, and a data
// directory whose identity is unreadable is an error rather than a silent
// re-enrollment: enrollment tokens are use-capped, so an agent that re-enrolls
// on every boot burns the fleet's tokens and mints a duplicate Device row per
// reboot.
func LoadOrEnroll(ctx context.Context, cfg config.Config, opts Options) (*Identity, error) {
	// An interrupted certificate rotation is finished (or unwound) BEFORE
	// anything is loaded -- otherwise a key and a certificate that do not pair
	// would reach build(), which refuses them, and the branch below would
	// refuse to re-enroll over the wreckage. One os.Stat in the common case.
	// See rotate.go and docs/modules/agent/cert-renewal.md §4.2.
	if err := RecoverRotation(cfg); err != nil {
		return nil, fmt.Errorf("enroll: interrupted certificate rotation: %w", err)
	}
	if cfg.Enrolled() {
		// An installer's token file that outlived its use. config.Load does not
		// read it once an identity exists, so nothing else would ever remove it,
		// and a spent credential sitting on disk buys an attacker with local
		// read exactly one thing this agent no longer needs.
		if err := cfg.ConsumeEnrollToken(); err != nil {
			opts.logger().Warn("could not remove the spent enrollment token file",
				"path", cfg.EnrollTokenPath(), "err", err)
		}
		id, err := Load(cfg)
		if err != nil {
			return nil, fmt.Errorf("enroll: %s exists but is unusable (refusing to re-enroll and burn a token): %w",
				cfg.IdentityPath(), err)
		}
		return id, nil
	}
	return Enroll(ctx, cfg, opts)
}

// Load reads a previously persisted identity. It performs no I/O beyond the
// data directory.
func Load(cfg config.Config) (*Identity, error) {
	var ident persistedIdentity
	if err := readJSON(cfg.IdentityPath(), &ident); err != nil {
		return nil, err
	}
	var pins persistedPins
	if err := readJSON(cfg.PinsPath(), &pins); err != nil {
		return nil, err
	}
	keyPEM, err := os.ReadFile(cfg.KeyPath())
	if err != nil {
		return nil, err
	}
	return build(&ident, &pins, keyPEM)
}

// Enroll generates a keypair, obtains a certificate, and persists everything.
func Enroll(ctx context.Context, cfg config.Config, opts Options) (*Identity, error) {
	log := opts.logger()
	if cfg.EnrollURL == "" || cfg.EnrollToken == "" {
		return nil, fmt.Errorf("enroll: %w", config.ErrNoEnrollment)
	}

	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, fmt.Errorf("enroll: generate key: %w", err)
	}
	// The CSR subject is EMPTY on purpose. The CA overwrites CN and O from
	// device_id/org_id and never reads the CSR subject (app/core/ca.py:157-168),
	// so a hostname here would be ignored while reading as meaningful.
	csrDER, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{}, priv)
	if err != nil {
		return nil, fmt.Errorf("enroll: build CSR: %w", err)
	}
	csrPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: csrDER})

	facts, err := collectFacts()
	if err != nil {
		return nil, err
	}
	caps := map[string]bool{}
	for _, c := range opts.Capabilities {
		caps[c] = true
	}

	request := enrollRequest{
		CSRPEM:       string(csrPEM),
		DeviceFacts:  facts,
		Capabilities: caps,
	}
	if IsLinkCode(cfg.EnrollToken) {
		request.LinkCode = cfg.EnrollToken
	} else {
		request.Token = cfg.EnrollToken
	}
	body, err := json.Marshal(request)
	if err != nil {
		return nil, err
	}

	url := cfg.EnrollURL + EnrollPath
	link := request.LinkCode != ""

	// The token is a credential. It is in the request body, never in a log
	// line, and this log line names only the destination.
	log.Info("enrolling", "url", url, "hostname", facts.Hostname, "os_family", facts.OSFamily, "via_install_link", link)

	// A LINK ENROLLMENT WAITS OUT "NOT NOW" IN-PROCESS: 429 (the link's hourly
	// limit), 5xx and an unreachable control plane are retried on a capped,
	// jittered timer that honours Retry-After and stops when ctx does. Exiting
	// instead would hand the retry to the service manager, and a unit that
	// fails fast enough trips systemd's start limit and stays down. A token
	// enrollment keeps its one attempt: its exit code is its answer.
	var resp *http.Response
	for attempt := 0; ; attempt++ {
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
		if err != nil {
			return nil, err
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err = opts.client().Do(req)
		if err != nil {
			if !link || ctx.Err() != nil {
				return nil, fmt.Errorf("enroll: POST %s: %w", url, err)
			}
			d := linkBackoff(attempt)
			log.Warn("enrollment through the install link: the control plane did not answer; retrying",
				"in", d.String(), "err", err)
			if werr := opts.pause(ctx, d); werr != nil {
				return nil, fmt.Errorf("enroll: %w", werr)
			}
			continue
		}
		if link && (resp.StatusCode == http.StatusTooManyRequests || resp.StatusCode >= 500) {
			d := retryAfter(resp.Header.Get("Retry-After"), time.Now())
			if d == 0 {
				d = linkBackoff(attempt)
			}
			_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
			resp.Body.Close()
			log.Warn("enrollment through the install link: not now; retrying",
				"status", resp.StatusCode, "in", d.String())
			if werr := opts.pause(ctx, d); werr != nil {
				return nil, fmt.Errorf("enroll: %w", werr)
			}
			continue
		}
		break
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 && resp.StatusCode < 500 {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
		return nil, &Rejected{Status: resp.StatusCode}
	}
	if resp.StatusCode != http.StatusCreated {
		return nil, fmt.Errorf("enroll: POST %s: unexpected status %d", url, resp.StatusCode)
	}

	var out enrollResponse
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&out); err != nil {
		return nil, fmt.Errorf("enroll: malformed enrollment response: %w", err)
	}

	keyDER, err := x509.MarshalPKCS8PrivateKey(priv)
	if err != nil {
		return nil, err
	}
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER})

	ident := &persistedIdentity{
		DeviceID:   out.DeviceID,
		GatewayURL: out.GatewayURL,
		CertPEM:    out.CertPEM,
		ChainPEM:   out.ChainPEM,
	}
	pins := &persistedPins{
		CABundlePEM:            out.CABundlePEM,
		JobSigningPublicKeyPEM: out.JobSigningPublicKeyPEM,
	}

	// VALIDATE BEFORE PERSISTING. build() re-derives everything the agent will
	// later depend on -- the leaf parses, its public key is the one we just
	// generated, the CA bundle is a usable pool, the signing pin is Ed25519 --
	// so a response that cannot produce a working agent never reaches the disk.
	id, err := build(ident, pins, keyPEM)
	if err != nil {
		return nil, err
	}
	if !bytes.Equal(id.Cert.Leaf.PublicKey.(ed25519.PublicKey), pub) {
		return nil, errors.New("enroll: the issued certificate is not over this device's public key")
	}

	if err := persist(cfg, ident, pins, keyPEM); err != nil {
		return nil, err
	}
	// The token has bought a certificate and is single-use server-side: from
	// here it is a credential at rest that can buy nothing. Deleted AFTER
	// persist, never before -- a token removed ahead of a failed write would
	// leave an endpoint that can neither start nor re-enroll.
	//
	// A rejection returns earlier and deliberately leaves the file: the operator
	// may be fixing the URL rather than the token, and a retry that needs a
	// freshly minted token is a worse default than a token that outlives one
	// failed attempt.
	if err := cfg.ConsumeEnrollToken(); err != nil {
		log.Warn("could not remove the spent enrollment token file",
			"path", cfg.EnrollTokenPath(), "err", err)
	}
	log.Info("enrolled",
		"device_id", id.DeviceID,
		"gateway_url", id.GatewayURL,
		"cert_not_after", id.Cert.Leaf.NotAfter.UTC().Format(time.RFC3339),
	)
	return id, nil
}

// build assembles an Identity from persisted bytes, validating every part.
func build(ident *persistedIdentity, pins *persistedPins, keyPEM []byte) (*Identity, error) {
	if ident.DeviceID == "" {
		return nil, errors.New("enroll: identity carries no device_id")
	}
	if ident.GatewayURL == "" {
		return nil, errors.New("enroll: identity carries no gateway_url")
	}

	// The chain is presented alongside the leaf so the gateway can build a path
	// even when it holds only the root.
	chain := append(append([]byte{}, []byte(ident.CertPEM)...), []byte(ident.ChainPEM)...)
	cert, err := tls.X509KeyPair(chain, keyPEM)
	if err != nil {
		return nil, fmt.Errorf("enroll: certificate and key do not form a pair: %w", err)
	}
	leaf, err := x509.ParseCertificate(cert.Certificate[0])
	if err != nil {
		return nil, fmt.Errorf("enroll: leaf certificate: %w", err)
	}
	cert.Leaf = leaf

	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM([]byte(pins.CABundlePEM)) {
		return nil, errors.New("enroll: ca_bundle_pem contains no usable certificate; there would be nothing to pin")
	}

	signing, err := parseEd25519PublicKey(pins.JobSigningPublicKeyPEM)
	if err != nil {
		return nil, err
	}

	return &Identity{
		DeviceID:      ident.DeviceID,
		Cert:          cert,
		CACerts:       pool,
		GatewayURL:    ident.GatewayURL,
		JobSigningKey: signing,
	}, nil
}

// parseEd25519PublicKey rejects anything that is not Ed25519.
//
// Invariant #1 has exactly one signature algorithm. Accepting an RSA or ECDSA
// pin here would mean the verification code either has a second algorithm path
// -- more attack surface for no requirement -- or fails on every job later,
// far from the cause.
func parseEd25519PublicKey(pemText string) (ed25519.PublicKey, error) {
	block, _ := pem.Decode([]byte(pemText))
	if block == nil {
		return nil, errors.New("enroll: job_signing_public_key_pem is not PEM")
	}
	parsed, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("enroll: job_signing_public_key_pem: %w", err)
	}
	key, ok := parsed.(ed25519.PublicKey)
	if !ok {
		return nil, fmt.Errorf(
			"enroll: job signing key is %T; jobs are signed Ed25519 and no other algorithm is accepted", parsed)
	}
	return key, nil
}

// persist writes the three files, identity.json LAST.
//
// config.Enrolled() tests for identity.json, so renaming it last makes it the
// commit marker: an interrupted enrollment leaves a key and pins that the next
// boot ignores and overwrites, never a half-identity that is treated as real.
func persist(cfg config.Config, ident *persistedIdentity, pins *persistedPins, keyPEM []byte) error {
	if err := os.MkdirAll(cfg.DataDir, config.DirMode); err != nil {
		return err
	}
	if err := os.Chmod(cfg.DataDir, config.DirMode); err != nil {
		return err
	}
	if err := writeFile(cfg.KeyPath(), keyPEM, config.KeyMode); err != nil {
		return err
	}
	if err := writeJSON(cfg.PinsPath(), pins); err != nil {
		return err
	}
	return writeJSON(cfg.IdentityPath(), ident)
}

func writeJSON(path string, v any) error {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	return writeFile(path, append(b, '\n'), 0o600)
}

func readJSON(path string, v any) error {
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if err := json.Unmarshal(b, v); err != nil {
		return fmt.Errorf("%s: %w", path, err)
	}
	return nil
}

// writeFile writes atomically: a torn write during a power cut must not leave a
// file that parses as half an identity.
func writeFile(name string, data []byte, mode os.FileMode) error {
	tmp := name + ".tmp"
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, mode)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		os.Remove(tmp)
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		os.Remove(tmp)
		return err
	}
	if err := f.Close(); err != nil {
		os.Remove(tmp)
		return err
	}
	// OpenFile's mode is masked by the process umask; set it explicitly so the
	// device key is 0600 whatever the service manager's umask happens to be.
	if err := os.Chmod(tmp, mode); err != nil {
		os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, name); err != nil {
		os.Remove(tmp)
		return err
	}
	return nil
}

// ---------------------------------------------------------------------------
// Device facts
// ---------------------------------------------------------------------------

func collectFacts() (deviceFacts, error) {
	family, err := osFamily(runtime.GOOS)
	if err != nil {
		return deviceFacts{}, err
	}
	host, err := os.Hostname()
	if err != nil || host == "" {
		// The server requires min_length=1; failing here is clearer than a 422.
		return deviceFacts{}, fmt.Errorf("enroll: cannot determine hostname: %w", err)
	}
	return deviceFacts{
		Hostname:     host,
		OSFamily:     family,
		OSVersion:    osVersion(),
		Architecture: runtime.GOARCH,
		// Serial needs a per-OS collector (WMI, ioreg, DMI). That is
		// inventory's job (Task 10/11), and the field is nullable server-side,
		// so enrollment sends null rather than a guess.
		//
		// THIS IS WHY MACs EXIST BELOW. The asset-inventory spec's §4 wanted to
		// match an enrolling agent to an existing record on its serial first --
		// but nothing collects one here, so that rule could never fire. MACs
		// are the strongest identifier this function can gather PORTABLY: one
		// stdlib call, no per-OS code, no privileges.
		Serial: nil,
		MACs:   physicalMACs(),
	}, nil
}

// osFamily maps runtime.GOOS to the server's ^(windows|linux|macos)$.
//
// darwin -> macos is the one that bites. Anything unmapped is an error here
// rather than a 422 from the server: the agent should not put a value on the
// wire that the control plane's own regex rejects.
func osFamily(goos string) (string, error) {
	switch goos {
	case "windows":
		return "windows", nil
	case "linux":
		return "linux", nil
	case "darwin":
		return "macos", nil
	default:
		return "", fmt.Errorf(
			"enroll: GOOS %q has no os_family in the server's vocabulary (windows|linux|macos)", goos)
	}
}

// osVersion is a cheap, best-effort string. The authoritative value is
// inventory.collect's (Task 10); this exists so the Device row is not blank
// between enrollment and the first inventory run.
func osVersion() string {
	if runtime.GOOS != "linux" {
		return ""
	}
	b, err := os.ReadFile(path.Join("/proc", "sys", "kernel", "osrelease"))
	if err != nil {
		return ""
	}
	return string(bytes.TrimSpace(b))
}
