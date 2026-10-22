package capabilities_test

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"math/big"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/capabilities/cert"
	"github.com/toeshee/it-suite/agent/internal/config"
	"github.com/toeshee/it-suite/agent/internal/enroll"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// Certificate rotation, driven THROUGH THE SIGNED-JOB RUNNER
// (docs/modules/agent/cert-renewal.md).
//
// This is the only place the rotation capabilities are exercised the way they
// actually run. jobs.VerifiedJob is an interface with an unexported method, so
// the runner is the only thing that can produce one -- which means invariant #1
// is not something these tests could bypass even deliberately. Both legs here
// arrive as Ed25519-signed frames and go through all five checks first.
//
// The job type whose subject is the agent's OWN IDENTITY is the one that most
// needs that, which is why it is asserted here rather than assumed.

// ---------------------------------------------------------------------------
// A data directory holding a real, working identity, and a CA that can rotate it
// ---------------------------------------------------------------------------

type rotatingCA struct {
	cert *x509.Certificate
	key  ed25519.PrivateKey
	pem  string
}

func newRotatingCA(t *testing.T, name string) *rotatingCA {
	t.Helper()
	pub, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: name},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(3650 * 24 * time.Hour),
		IsCA:                  true,
		KeyUsage:              x509.KeyUsageCertSign,
		BasicConstraintsValid: true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, pub, key)
	if err != nil {
		t.Fatal(err)
	}
	parsed, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return &rotatingCA{
		cert: parsed,
		key:  key,
		pem:  string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})),
	}
}

// issue signs a leaf for deviceID over pub, the way app/core/ca.py::sign_csr
// does: CN = device UUID, O = org UUID, client-auth EKU, AGENT_CERT_TTL_DAYS.
func (ca *rotatingCA) issue(t *testing.T, deviceID string, pub any) string {
	t.Helper()
	serial, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	tmpl := &x509.Certificate{
		SerialNumber: serial,
		Subject: pkix.Name{
			CommonName:   deviceID,
			Organization: []string{"99999999-8888-7777-6666-555555555555"},
		},
		NotBefore:   time.Now().Add(-5 * time.Minute),
		NotAfter:    time.Now().Add(365 * 24 * time.Hour),
		KeyUsage:    x509.KeyUsageDigitalSignature,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca.cert, pub, ca.key)
	if err != nil {
		t.Fatal(err)
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}))
}

// enrolledDir writes an identity the way enroll.persist does and points the
// process configuration at it.
//
// `cert.Process` resolves its configuration from the environment at JOB TIME
// (not at init), so setting ITS_AGENT_DATA_DIR here is what makes the real
// registered handlers -- the ones in capabilities.Default() -- operate on this
// directory. That is the same mechanism the running binary uses.
func enrolledDir(t *testing.T, ca *rotatingCA, deviceID string) config.Config {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "data")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	pub, key, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(key)
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

	write := func(name string, v any) {
		b, err := json.Marshal(v)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, name), b, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(dir, "agent.key"),
		pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER}), 0o600); err != nil {
		t.Fatal(err)
	}
	write("pins.json", map[string]string{
		"ca_bundle_pem": ca.pem,
		"job_signing_public_key_pem": string(pem.EncodeToMemory(
			&pem.Block{Type: "PUBLIC KEY", Bytes: signDER})),
	})
	write("identity.json", map[string]string{
		"device_id":   deviceID,
		"gateway_url": "wss://gateway.test:8443/agent",
		"cert_pem":    ca.issue(t, deviceID, pub),
		"chain_pem":   ca.pem,
	})

	t.Setenv(config.EnvDataDir, dir)
	t.Setenv(config.EnvEnrollURL, "")
	t.Setenv(config.EnvEnrollToken, "")
	cfg, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	return cfg
}

// resultFor picks the job_result for ONE job.
//
// The shared harness's `result` returns the first job_result it recorded, which
// is fine for a single-job test and wrong for a two-leg exchange: leg 2's
// assertions would silently read leg 1's answer, which is exactly the "passes
// because something else happened" shape this module has been bitten by.
//
// Bounded-polled rather than a single synchronous read: since Task 6 wired
// jobs.Dispatcher into Runner.Handle, capability execution happens on a
// Dispatcher worker goroutine, asynchronously to h.dispatch's call to Handle.
func resultFor(t *testing.T, h *harness, jobID string) wire.JobResult {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		for _, msg := range h.sender.sent() {
			if r, ok := msg.(wire.JobResult); ok && r.JobID == jobID {
				return r
			}
		}
		if time.Now().After(deadline) {
			t.Fatalf("no job_result for %s; sent: %v", jobID, h.sender.sent())
		}
		time.Sleep(2 * time.Millisecond)
	}
}

func fingerprint(t *testing.T, id *enroll.Identity) string {
	t.Helper()
	sum := sha256.Sum256(id.Cert.Leaf.Raw)
	return hex.EncodeToString(sum[:])
}

// ---------------------------------------------------------------------------

func TestBothRotationLegsRunOnlyAsVerifiedSignedJobs(t *testing.T) {
	ca := newRotatingCA(t, "its-rotation-ca")
	cfg := enrolledDir(t, ca, testDeviceID)
	before, err := enroll.Load(cfg)
	if err != nil {
		t.Fatal(err)
	}

	h := newHarness(t)

	// --- leg 1: agent.cert.renew ---
	h.dispatch(t, cert.JobTypeRenew, `{}`, "9c3f0a11-0000-4000-8000-00000000c001")
	renewed := resultFor(t, h, "9c3f0a11-0000-4000-8000-00000000c001")
	if renewed.State != wire.StateSucceeded {
		t.Fatalf("renew state = %q, result = %v", renewed.State, renewed.Result)
	}
	csrPEM, _ := renewed.Result["csr_pem"].(string)
	if csrPEM == "" {
		t.Fatalf("no csr_pem in the result; keys = %v", keys(renewed.Result))
	}
	// INVARIANT #3, on the wire this time: what leaves the endpoint is a CSR
	// and nothing else.
	for key, v := range renewed.Result {
		if s, ok := v.(string); ok && key != "csr_pem" && len(s) > 0 {
			t.Fatalf("the renew result carries %q as well as the CSR", key)
		}
	}
	if strings.Contains(csrPEM, "PRIVATE KEY") {
		t.Fatal("the CSR carries private key material")
	}

	// --- the control plane signs it ---
	block, _ := pem.Decode([]byte(csrPEM))
	if block == nil {
		t.Fatal("csr is not PEM")
	}
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	newCertPEM := ca.issue(t, testDeviceID, csr.PublicKey)

	// --- leg 2: agent.cert.install ---
	params, err := json.Marshal(map[string]string{
		"cert_pem":  newCertPEM,
		"chain_pem": ca.pem,
		// On the wire and deliberately not read by the endpoint. An agent that
		// choked on it would break the moment the control plane added a field.
		"cert_serial": "deadbeef",
	})
	if err != nil {
		t.Fatal(err)
	}
	h.dispatch(t, cert.JobTypeInstall, string(params), "9c3f0a11-0000-4000-8000-00000000c002")
	installed := resultFor(t, h, "9c3f0a11-0000-4000-8000-00000000c002")
	if installed.State != wire.StateSucceeded {
		t.Fatalf("install state = %q, result = %v", installed.State, installed.Result)
	}

	after, err := enroll.Load(cfg)
	if err != nil {
		t.Fatalf("the rotated identity does not load: %v", err)
	}
	if fingerprint(t, after) == fingerprint(t, before) {
		t.Fatal("the identity did not change")
	}
	// The acknowledgement the control plane checks against the certificate IT
	// issued (confirm_installed_cert). A wrong value here would leave the
	// admission gate on the old certificate and fail the job.
	if got := installed.Result["cert_fingerprint"]; got != fingerprint(t, after) {
		t.Fatalf("acknowledged fingerprint %v, installed %s", got, fingerprint(t, after))
	}
	if _, ok := installed.Result["not_after"].(string); !ok {
		t.Fatalf("no not_after in the install result; keys = %v", keys(installed.Result))
	}
}

func TestAnInstallTheEndpointRefusesFailsTheJobAndKeepsTheWorkingIdentity(t *testing.T) {
	// A certificate from a root this endpoint never pinned. FALSIFIER: a
	// `succeeded` state, or a changed identity on disk. "An error was logged"
	// is not the observation -- the observation is the identity that survives,
	// because the control plane only flips its admission gate on a SUCCEEDED
	// install, and the two have to agree.
	ca := newRotatingCA(t, "its-rotation-ca")
	cfg := enrolledDir(t, ca, testDeviceID)
	before, err := enroll.Load(cfg)
	if err != nil {
		t.Fatal(err)
	}

	h := newHarness(t)
	h.dispatch(t, cert.JobTypeRenew, `{}`, "9c3f0a11-0000-4000-8000-00000000c003")
	csrPEM, _ := resultFor(t, h, "9c3f0a11-0000-4000-8000-00000000c003").Result["csr_pem"].(string)
	block, _ := pem.Decode([]byte(csrPEM))
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}

	rogue := newRotatingCA(t, "some-other-ca")
	params, err := json.Marshal(map[string]string{
		"cert_pem":    rogue.issue(t, testDeviceID, csr.PublicKey),
		"chain_pem":   rogue.pem,
		"cert_serial": "deadbeef",
	})
	if err != nil {
		t.Fatal(err)
	}
	h.dispatch(t, cert.JobTypeInstall, string(params), "9c3f0a11-0000-4000-8000-00000000c004")

	result := resultFor(t, h, "9c3f0a11-0000-4000-8000-00000000c004")
	if result.State != wire.StateFailed {
		t.Fatalf("state = %q, result = %v; want failed", result.State, result.Result)
	}
	after, err := enroll.Load(cfg)
	if err != nil {
		t.Fatalf("a refused install left the endpoint unable to load its identity: %v", err)
	}
	if fingerprint(t, after) != fingerprint(t, before) {
		t.Fatal("a refused install changed the working certificate")
	}
}

func TestAnAgentAgainstAServerThatNeverRotatesIsUnchanged(t *testing.T) {
	// COMPATIBILITY: NEW AGENT -> OLD CONTROL PLANE
	// (docs/modules/agent/cert-renewal.md §1).
	//
	// Rotation is entirely server-initiated. An older control plane never
	// dispatches either leg, so nothing here runs -- and the assertion is that
	// this agent therefore behaves exactly as it did before the capability
	// existed: it touches none of the rotation files, and it still answers the
	// job types it always did.
	//
	// FALSIFIER: any of the three rotation files existing, or the identity
	// changing. A capability that "helpfully" pre-generated a keypair at
	// startup, or a scheduler entry that rotated on the agent's own initiative,
	// would fail here -- and either would be an agent asking for a credential
	// nobody asked it to have.
	ca := newRotatingCA(t, "its-rotation-ca")
	cfg := enrolledDir(t, ca, testDeviceID)
	before, err := enroll.Load(cfg)
	if err != nil {
		t.Fatal(err)
	}

	h := newHarness(t)
	h.dispatch(t, "hello", `{"message":"hi"}`, "9c3f0a11-0000-4000-8000-00000000c005")
	if got := resultFor(t, h, "9c3f0a11-0000-4000-8000-00000000c005").Result["greeting"]; got != "hi" {
		t.Fatalf("greeting = %v; the ordinary job path changed", got)
	}

	for _, path := range []string{
		cfg.PendingKeyPath(), cfg.RotationPath(), cfg.PreviousIdentityPath(),
	} {
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Errorf("%s exists though no rotation was ever dispatched", path)
		}
	}
	after, err := enroll.Load(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if fingerprint(t, after) != fingerprint(t, before) {
		t.Fatal("the identity changed with no rotation dispatched")
	}
}
