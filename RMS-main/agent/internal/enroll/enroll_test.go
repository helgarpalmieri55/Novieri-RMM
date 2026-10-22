package enroll

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"errors"
	"log/slog"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"sync/atomic"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/config"
)

// ---------------------------------------------------------------------------
// A fake control plane that mimics POST /enroll/v1 (app/api/v1/enrollment.py).
// ---------------------------------------------------------------------------

type fakeCP struct {
	srv      *httptest.Server
	requests atomic.Int64
	paths    []string

	caCert *x509.Certificate
	caKey  ed25519.PrivateKey

	// knobs
	status          int    // non-zero: reply with this and no body
	statuses        []int  // consumed one per request BEFORE `status`: a scripted sequence
	retryAfter      string // sent as Retry-After with every scripted status
	signingKeyPEM   string // overridden to test a non-Ed25519 pin
	signSomeoneElse bool   // issue a certificate over a DIFFERENT public key
	lastBody        map[string]any
	gatewayURL      string
}

func newFakeCP(t *testing.T) *fakeCP {
	t.Helper()
	caPub, caKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	caTmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "its-test-ca"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(365 * 24 * time.Hour),
		IsCA:                  true,
		KeyUsage:              x509.KeyUsageCertSign,
		BasicConstraintsValid: true,
	}
	caDER, err := x509.CreateCertificate(rand.Reader, caTmpl, caTmpl, caPub, caKey)
	if err != nil {
		t.Fatal(err)
	}
	caCert, err := x509.ParseCertificate(caDER)
	if err != nil {
		t.Fatal(err)
	}

	signPub, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	signDER, err := x509.MarshalPKIXPublicKey(signPub)
	if err != nil {
		t.Fatal(err)
	}

	cp := &fakeCP{
		caCert:        caCert,
		caKey:         caKey,
		signingKeyPEM: string(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: signDER})),
		gatewayURL:    "wss://gateway.test:8443/agent",
	}
	cp.srv = httptest.NewServer(http.HandlerFunc(cp.handle))
	t.Cleanup(cp.srv.Close)
	return cp
}

func (cp *fakeCP) handle(w http.ResponseWriter, r *http.Request) {
	n := cp.requests.Add(1)
	cp.paths = append(cp.paths, r.URL.Path)
	if r.URL.Path != "/enroll/v1" {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	if i := int(n - 1); i < len(cp.statuses) && cp.statuses[i] != 0 {
		if cp.retryAfter != "" {
			w.Header().Set("Retry-After", cp.retryAfter)
		}
		http.Error(w, "scripted", cp.statuses[i])
		return
	}
	if cp.status != 0 {
		// The real server's rejection is a bare 403 with no detail about which
		// check failed (app/api/v1/enrollment.py:160-161).
		http.Error(w, "enrollment rejected", cp.status)
		return
	}
	var body map[string]any
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json", http.StatusUnprocessableEntity)
		return
	}
	cp.lastBody = body

	csrPEM, _ := body["csr_pem"].(string)
	block, _ := pem.Decode([]byte(csrPEM))
	if block == nil {
		http.Error(w, "no csr", http.StatusUnprocessableEntity)
		return
	}
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil || csr.CheckSignature() != nil {
		http.Error(w, "bad csr", http.StatusUnprocessableEntity)
		return
	}

	pub := csr.PublicKey
	if cp.signSomeoneElse {
		other, _, gerr := ed25519.GenerateKey(rand.Reader)
		if gerr != nil {
			http.Error(w, "boom", http.StatusInternalServerError)
			return
		}
		pub = other
	}

	deviceID := "11111111-2222-3333-4444-555555555555"
	orgID := "99999999-8888-7777-6666-555555555555"
	serial, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	tmpl := &x509.Certificate{
		SerialNumber: serial,
		// The CA overwrites CN/O from device_id/org_id and does NOT read the
		// CSR subject (app/core/ca.py:157-168).
		Subject:     pkix.Name{CommonName: deviceID, Organization: []string{orgID}},
		NotBefore:   time.Now().Add(-5 * time.Minute),
		NotAfter:    time.Now().Add(365 * 24 * time.Hour), // AGENT_CERT_TTL_DAYS
		KeyUsage:    x509.KeyUsageDigitalSignature,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
		URIs:        nil,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, cp.caCert, pub, cp.caKey)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	caPEM := string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cp.caCert.Raw}))
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusCreated)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"device_id":                  deviceID,
		"cert_pem":                   string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})),
		"chain_pem":                  caPEM,
		"ca_bundle_pem":              caPEM,
		"gateway_url":                cp.gatewayURL,
		"job_signing_public_key_pem": cp.signingKeyPEM,
	})
}

func cfgFor(t *testing.T, cp *fakeCP) (config.Config, string) {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "data")
	t.Setenv(config.EnvDataDir, dir)
	t.Setenv(config.EnvEnrollURL, cp.srv.URL)
	t.Setenv(config.EnvEnrollToken, "tok.secret")
	cfg, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	return cfg, dir
}

func logTo(buf *bytes.Buffer) *slog.Logger {
	return slog.New(slog.NewTextHandler(buf, &slog.HandlerOptions{Level: slog.LevelDebug}))
}

// ---------------------------------------------------------------------------

func TestFirstRunEnrollsAndPersistsAUsableIdentity(t *testing.T) {
	cp := newFakeCP(t)
	cfg, dir := cfgFor(t, cp)

	id, err := LoadOrEnroll(context.Background(), cfg, Options{})
	if err != nil {
		t.Fatalf("LoadOrEnroll: %v", err)
	}
	if id.DeviceID != "11111111-2222-3333-4444-555555555555" {
		t.Fatalf("device id = %q", id.DeviceID)
	}
	if id.GatewayURL != cp.gatewayURL {
		t.Fatalf("gateway url = %q, want %q", id.GatewayURL, cp.gatewayURL)
	}
	if id.Cert.Leaf == nil {
		t.Fatal("Leaf is nil; Task 6 parses NotAfter from it and mTLS needs it cheap")
	}
	if len(id.JobSigningKey) != ed25519.PublicKeySize {
		t.Fatalf("job signing key is %d bytes, want %d", len(id.JobSigningKey), ed25519.PublicKeySize)
	}
	if id.CACerts == nil {
		t.Fatal("no pinned CA pool; transport.Dial refuses to dial without one")
	}
	for _, p := range []string{cfg.IdentityPath(), cfg.KeyPath(), cfg.PinsPath()} {
		if _, err := os.Stat(p); err != nil {
			t.Errorf("%s not written: %v", p, err)
		}
	}
	if runtime.GOOS != "windows" {
		info, err := os.Stat(dir)
		if err != nil {
			t.Fatal(err)
		}
		if perm := info.Mode().Perm(); perm != config.DirMode {
			t.Errorf("data dir mode = %o, want %o", perm, config.DirMode)
		}
	}
}

func TestThePrivateKeyIsOwnerOnlyAndNeverReachesALogLine(t *testing.T) {
	// Invariant #3. Falsifying observation: the key material, or even the PEM
	// header that announces it, present in the captured log buffer.
	cp := newFakeCP(t)
	cfg, _ := cfgFor(t, cp)

	var buf bytes.Buffer
	if _, err := LoadOrEnroll(context.Background(), cfg, Options{Logger: logTo(&buf)}); err != nil {
		t.Fatalf("LoadOrEnroll: %v", err)
	}

	keyPEM, err := os.ReadFile(cfg.KeyPath())
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(buf.Bytes(), keyPEM) {
		t.Fatal("the device private key was written to the log")
	}
	if bytes.Contains(buf.Bytes(), []byte("PRIVATE KEY")) {
		t.Fatalf("a PRIVATE KEY PEM header reached the log:\n%s", buf.String())
	}
	// The base64 body without its armour would be just as fatal.
	block, _ := pem.Decode(keyPEM)
	if block == nil {
		t.Fatal("the persisted key is not PEM")
	}
	if _, err := x509.ParsePKCS8PrivateKey(block.Bytes); err != nil {
		t.Fatalf("the persisted key does not parse: %v", err)
	}

	if runtime.GOOS == "windows" {
		t.Skip("file modes are not meaningful on windows")
	}
	info, err := os.Stat(cfg.KeyPath())
	if err != nil {
		t.Fatal(err)
	}
	if perm := info.Mode().Perm(); perm != config.KeyMode {
		t.Fatalf("key mode = %o, want %o", perm, config.KeyMode)
	}
}

func TestASecondLoadOrEnrollMakesNoHTTPRequest(t *testing.T) {
	// An agent that re-enrolls on restart burns an enrollment token per reboot,
	// and tokens are use-capped. The observation is the SERVER's request
	// counter, not "it returned an identity".
	cp := newFakeCP(t)
	cfg, _ := cfgFor(t, cp)

	first, err := LoadOrEnroll(context.Background(), cfg, Options{})
	if err != nil {
		t.Fatalf("first LoadOrEnroll: %v", err)
	}
	after := cp.requests.Load()

	second, err := LoadOrEnroll(context.Background(), cfg, Options{})
	if err != nil {
		t.Fatalf("second LoadOrEnroll: %v", err)
	}
	if n := cp.requests.Load(); n != after {
		t.Fatalf("the second run made %d extra request(s); it must load from disk", n-after)
	}
	if second.DeviceID != first.DeviceID {
		t.Fatalf("device id changed across restart: %q -> %q", first.DeviceID, second.DeviceID)
	}
	if !bytes.Equal(second.Cert.Leaf.Raw, first.Cert.Leaf.Raw) {
		t.Fatal("the reloaded certificate is not the enrolled one")
	}
}

func TestARejectionIsTypedAndLeavesTheDataDirEmpty(t *testing.T) {
	// A partial identity is worse than none: the agent would find identity.json
	// on the next boot, skip enrollment, and fail mTLS forever with a token it
	// has already burned.
	cp := newFakeCP(t)
	cp.status = http.StatusForbidden
	cfg, dir := cfgFor(t, cp)

	_, err := LoadOrEnroll(context.Background(), cfg, Options{})
	if err == nil {
		t.Fatal("a 403 produced no error")
	}
	if !errors.Is(err, ErrRejected) {
		t.Fatalf("error = %v, want ErrRejected", err)
	}
	var rej *Rejected
	if !errors.As(err, &rej) || rej.Status != http.StatusForbidden {
		t.Fatalf("error does not carry the status: %v", err)
	}
	entries, rerr := os.ReadDir(dir)
	if rerr != nil && !os.IsNotExist(rerr) {
		t.Fatal(rerr)
	}
	if len(entries) != 0 {
		names := make([]string, 0, len(entries))
		for _, e := range entries {
			names = append(names, e.Name())
		}
		t.Fatalf("a rejected enrollment left %v in the data dir", names)
	}
}

func TestANonEd25519JobSigningKeyIsRejectedAtEnrollmentTime(t *testing.T) {
	// Not at first job. An agent that stores an unusable pin looks enrolled,
	// connects, and then refuses every job for a reason no operator can see
	// from the console.
	cp := newFakeCP(t)
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	cp.signingKeyPEM = string(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der}))
	cfg, dir := cfgFor(t, cp)

	if _, err := LoadOrEnroll(context.Background(), cfg, Options{}); err == nil {
		t.Fatal("an ECDSA job-signing pin was accepted; jobs are Ed25519-signed")
	}
	if entries, _ := os.ReadDir(dir); len(entries) != 0 {
		t.Fatalf("an unusable enrollment response was persisted anyway: %d files", len(entries))
	}
}

func TestACertificateIssuedOverSomeoneElsesKeyIsRejected(t *testing.T) {
	// The CA signing a certificate whose public key is not ours means the mTLS
	// handshake will fail at the gateway with a 4401 the operator reads as
	// "revoked". Catch it where the cause is visible.
	cp := newFakeCP(t)
	cp.signSomeoneElse = true
	cfg, dir := cfgFor(t, cp)

	if _, err := LoadOrEnroll(context.Background(), cfg, Options{}); err == nil {
		t.Fatal("accepted a certificate that does not match the private key")
	}
	if entries, _ := os.ReadDir(dir); len(entries) != 0 {
		t.Fatalf("persisted a certificate that does not match the private key: %d files", len(entries))
	}
}

func TestEnrollmentPostsToExactlySlashEnrollV1(t *testing.T) {
	// The enrollment router is mounted with NO prefix (app/main.py), so it is
	// NOT under /api/v1. This is the single most guessable-wrong fact in the
	// whole agent, and getting it wrong is a 404 that reads like an outage.
	cp := newFakeCP(t)
	cfg, _ := cfgFor(t, cp)

	if _, err := LoadOrEnroll(context.Background(), cfg, Options{}); err != nil {
		t.Fatalf("LoadOrEnroll: %v", err)
	}
	if len(cp.paths) != 1 || cp.paths[0] != "/enroll/v1" {
		t.Fatalf("requested %v, want exactly [/enroll/v1]", cp.paths)
	}
}

func TestTheEnrollmentRequestCarriesTheFieldsTheServerRequires(t *testing.T) {
	cp := newFakeCP(t)
	cfg, _ := cfgFor(t, cp)

	if _, err := LoadOrEnroll(context.Background(), cfg, Options{}); err != nil {
		t.Fatalf("LoadOrEnroll: %v", err)
	}
	body := cp.lastBody
	for _, k := range []string{"token", "csr_pem", "device_facts"} {
		if _, ok := body[k]; !ok {
			t.Errorf("request has no %q", k)
		}
	}
	if body["token"] != "tok.secret" {
		t.Errorf("token = %v", body["token"])
	}
	facts, _ := body["device_facts"].(map[string]any)
	if facts == nil {
		t.Fatal("device_facts is not an object")
	}
	if h, _ := facts["hostname"].(string); h == "" {
		t.Error("hostname is empty; the server requires min_length=1")
	}
	switch facts["os_family"] {
	case "windows", "linux", "macos":
	default:
		t.Errorf("os_family = %v, outside the server's ^(windows|linux|macos)$", facts["os_family"])
	}
	if a, _ := facts["architecture"].(string); a != runtime.GOARCH {
		t.Errorf("architecture = %v, want %q", facts["architecture"], runtime.GOARCH)
	}
}

func TestCapabilitiesAdvertisedAreExactlyWhatThisBuildHandles(t *testing.T) {
	// C4: `agents.capabilities` was never populated by anything. It is
	// advertised here, but ONLY from a list the caller supplies from the real
	// registry -- a hardcoded list is a claim the binary may not be able to
	// keep, and IPAM dispatches against it.
	cp := newFakeCP(t)
	cfg, _ := cfgFor(t, cp)

	if _, err := LoadOrEnroll(context.Background(), cfg, Options{
		Capabilities: []string{"inventory.collect", "health.check.now"},
	}); err != nil {
		t.Fatalf("LoadOrEnroll: %v", err)
	}
	got, _ := cp.lastBody["capabilities"].(map[string]any)
	want := map[string]any{"inventory.collect": true, "health.check.now": true}
	if len(got) != len(want) {
		t.Fatalf("capabilities = %v, want %v", cp.lastBody["capabilities"], want)
	}
	for k, v := range want {
		if got[k] != v {
			t.Fatalf("capabilities[%q] = %v, want %v", k, got[k], v)
		}
	}
}

func TestABuildWithNoCapabilitiesAdvertisesAnEmptyObject(t *testing.T) {
	// Today's honest answer: the capability registry is Task 10 and nothing is
	// registered yet. `{}` is exactly the value the column already defaults to,
	// and IPAM's dispatcher tolerates it by dispatching and reading `refused`
	// as the answer. Advertising a capability this binary cannot run would be
	// worse than advertising none.
	cp := newFakeCP(t)
	cfg, _ := cfgFor(t, cp)

	if _, err := LoadOrEnroll(context.Background(), cfg, Options{}); err != nil {
		t.Fatalf("LoadOrEnroll: %v", err)
	}
	got, ok := cp.lastBody["capabilities"].(map[string]any)
	if !ok {
		t.Fatalf("capabilities = %v (%T), want an object", cp.lastBody["capabilities"], cp.lastBody["capabilities"])
	}
	if len(got) != 0 {
		t.Fatalf("capabilities = %v, want {}", got)
	}
}

func TestOSFamilyMapsGOOSToTheServersVocabulary(t *testing.T) {
	cases := []struct {
		goos string
		want string
		ok   bool
	}{
		{"windows", "windows", true},
		{"linux", "linux", true},
		{"darwin", "macos", true}, // the one that bites
		{"freebsd", "", false},
		{"macos", "", false},
		{"", "", false},
	}
	for _, tc := range cases {
		got, err := osFamily(tc.goos)
		if tc.ok && (err != nil || got != tc.want) {
			t.Errorf("osFamily(%q) = %q, %v; want %q, nil", tc.goos, got, err, tc.want)
		}
		if !tc.ok && err == nil {
			t.Errorf("osFamily(%q) = %q, nil; want an error rather than a 422 from the server", tc.goos, got)
		}
	}
}

func TestLoadOrEnrollWithoutCredentialsAndWithoutAnIdentityFails(t *testing.T) {
	// config.Load already refuses this combination; assert enroll does not
	// paper over it by, say, POSTing an empty token.
	cp := newFakeCP(t)
	dir := filepath.Join(t.TempDir(), "data")
	cfg := config.Config{DataDir: dir}
	if _, err := LoadOrEnroll(context.Background(), cfg, Options{}); err == nil {
		t.Fatal("enrolled with no URL and no token")
	}
	if n := cp.requests.Load(); n != 0 {
		t.Fatalf("made %d request(s) with no enrollment URL", n)
	}
}

func TestACorruptIdentityIsAnErrorNotASilentReEnrollment(t *testing.T) {
	// Silently re-enrolling would burn a token on every boot of a machine whose
	// disk is failing, and would mint a second Device row for the same host.
	cp := newFakeCP(t)
	cfg, _ := cfgFor(t, cp)
	if _, err := LoadOrEnroll(context.Background(), cfg, Options{}); err != nil {
		t.Fatalf("LoadOrEnroll: %v", err)
	}
	before := cp.requests.Load()

	if err := os.WriteFile(cfg.IdentityPath(), []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadOrEnroll(context.Background(), cfg, Options{}); err == nil {
		t.Fatal("a corrupt identity.json was tolerated")
	}
	if n := cp.requests.Load(); n != before {
		t.Fatalf("a corrupt identity triggered %d re-enrollment request(s)", n-before)
	}
}
