package enroll

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"errors"
	"math/big"
	"os"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/config"
)

// Certificate rotation on the endpoint (docs/modules/agent/cert-renewal.md §4).
//
// What these tests are for. The failure this design must not have is an
// endpoint that ends up with no usable identity -- a key and a certificate that
// do not pair, or a certificate the control plane has never heard of and no way
// back. Every test here therefore ends by asserting that SOMETHING WORKS on
// disk, not merely that a call returned an error.
//
// The other shape they guard is the one that has bitten this module twice: a
// test that passes because a different check caught the case. Each of the four
// install-time checks is exercised with the OTHER three satisfied, so removing
// any one of them makes exactly one test fail.

// ---------------------------------------------------------------------------
// a rotating control plane: it signs a CSR the way app/core/ca.py does
// ---------------------------------------------------------------------------

// issueFor signs csrPEM with the fake CP's CA, binding it to deviceID.
//
// `over` overrides the public key the certificate is issued for, which is how
// the "somebody else's key" case is built.
func (cp *fakeCP) issueFor(t *testing.T, csrPEM, deviceID string, over any, notAfter time.Time) (string, string) {
	t.Helper()
	block, _ := pem.Decode([]byte(csrPEM))
	if block == nil {
		t.Fatal("issueFor: csr is not PEM")
	}
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	pub := csr.PublicKey
	if over != nil {
		pub = over
	}
	serial, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	tmpl := &x509.Certificate{
		SerialNumber: serial,
		Subject: pkix.Name{
			CommonName:   deviceID,
			Organization: []string{"99999999-8888-7777-6666-555555555555"},
		},
		NotBefore:   time.Now().Add(-5 * time.Minute),
		NotAfter:    notAfter,
		KeyUsage:    x509.KeyUsageDigitalSignature,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, cp.caCert, pub, cp.caKey)
	if err != nil {
		t.Fatal(err)
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})),
		string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: cp.caCert.Raw}))
}

// enrolled sets up a data directory holding a real, working identity.
func enrolled(t *testing.T) (*fakeCP, cfgAndID) {
	t.Helper()
	cp := newFakeCP(t)
	cfg, dir := cfgFor(t, cp)
	id, err := LoadOrEnroll(t.Context(), cfg, Options{})
	if err != nil {
		t.Fatalf("LoadOrEnroll: %v", err)
	}
	return cp, cfgAndID{cfg: cfg, dir: dir, id: id}
}

type cfgAndID struct {
	cfg config.Config
	dir string
	id  *Identity
}

func mustRead(t *testing.T, path string) []byte {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return b
}

// ---------------------------------------------------------------------------
// §4.1 — the CSR
// ---------------------------------------------------------------------------

func TestGenerateCSRKeepsThePrivateKeyOnTheEndpoint(t *testing.T) {
	// INVARIANT #3, at renewal exactly as at enrollment. FALSIFIER: the CSR
	// containing key material, or the pending key not being 0600.
	_, env := enrolled(t)
	csrPEM, err := GenerateCSR(env.cfg)
	if err != nil {
		t.Fatalf("GenerateCSR: %v", err)
	}
	if strings.Contains(csrPEM, "PRIVATE KEY") {
		t.Fatal("the CSR carries private key material")
	}
	block, _ := pem.Decode([]byte(csrPEM))
	if block == nil || block.Type != "CERTIFICATE REQUEST" {
		t.Fatalf("not a CSR: %q", csrPEM)
	}
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	if err := csr.CheckSignature(); err != nil {
		t.Fatalf("CSR proof of possession does not verify: %v", err)
	}
	// The subject is empty on purpose: the CA overwrites CN/O and never reads
	// it, so anything here would be ignored while reading as meaningful.
	if csr.Subject.String() != "" {
		t.Fatalf("CSR subject = %q; want empty", csr.Subject.String())
	}
	if runtime.GOOS != "windows" {
		info, err := os.Stat(env.cfg.PendingKeyPath())
		if err != nil {
			t.Fatalf("pending key not written: %v", err)
		}
		if info.Mode().Perm() != 0o600 {
			t.Fatalf("pending key mode %v; the device key must not be readable by anyone else",
				info.Mode().Perm())
		}
	}
}

func TestEachRenewalUsesANewKeypair(t *testing.T) {
	// Reusing the old key would renew the certificate and not the identity,
	// which is most of the value of rotating at all.
	_, env := enrolled(t)
	first, err := GenerateCSR(env.cfg)
	if err != nil {
		t.Fatal(err)
	}
	firstKey := mustRead(t, env.cfg.PendingKeyPath())
	second, err := GenerateCSR(env.cfg)
	if err != nil {
		t.Fatal(err)
	}
	secondKey := mustRead(t, env.cfg.PendingKeyPath())
	if first == second {
		t.Fatal("two renewals produced the same CSR")
	}
	if string(firstKey) == string(secondKey) {
		t.Fatal("two renewals reused one private key")
	}
	// And the endpoint's LIVE key was not touched by either.
	if strings.Contains(string(secondKey), string(mustRead(t, env.cfg.KeyPath()))) {
		t.Fatal("the pending key is the live key")
	}
}

// ---------------------------------------------------------------------------
// §4.1 / §4.2 — installing
// ---------------------------------------------------------------------------

func TestInstallSwapsTheIdentityAndLeavesItLoadable(t *testing.T) {
	cp, env := enrolled(t)
	before := mustRead(t, env.cfg.IdentityPath())

	csrPEM, err := GenerateCSR(env.cfg)
	if err != nil {
		t.Fatal(err)
	}
	certPEM, chainPEM := cp.issueFor(t, csrPEM, env.id.DeviceID, nil, time.Now().Add(365*24*time.Hour))

	installed, err := Install(env.cfg, certPEM, chainPEM)
	if err != nil {
		t.Fatalf("Install: %v", err)
	}
	if installed.DeviceID != env.id.DeviceID {
		t.Fatalf("device id changed: %q", installed.DeviceID)
	}

	// THE OBSERVABLE: the identity on disk loads, pairs, and is the new one.
	reloaded, err := Load(env.cfg)
	if err != nil {
		t.Fatalf("the rotated identity does not load: %v", err)
	}
	if string(reloaded.Cert.Leaf.Raw) == string(env.id.Cert.Leaf.Raw) {
		t.Fatal("Install left the old certificate in place")
	}
	if string(reloaded.Cert.Leaf.Raw) != string(installed.Cert.Leaf.Raw) {
		t.Fatal("what was persisted is not what Install returned")
	}

	// the replaced identity is retained for the revert (§4.4)...
	var prev previousIdentity
	if err := readJSON(env.cfg.PreviousIdentityPath(), &prev); err != nil {
		t.Fatalf("previous.json not written: %v", err)
	}
	var beforeIdent persistedIdentity
	if err := json.Unmarshal(before, &beforeIdent); err != nil {
		t.Fatal(err)
	}
	if prev.CertPEM != beforeIdent.CertPEM {
		t.Fatal("previous.json does not hold the certificate that was replaced")
	}
	// ...and the transaction left nothing behind.
	for _, path := range []string{env.cfg.RotationPath(), env.cfg.PendingKeyPath()} {
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Errorf("%s survived a completed rotation", path)
		}
	}
}

func TestInstallRefusesACertificateOverSomebodyElsesKey(t *testing.T) {
	// THE GUARD. Every other check passes here: the CN is right, it chains to
	// the pinned root, the PEM parses. Only the key differs -- so this test
	// fails if and only if the key check is removed, and cannot be passing
	// because a different check caught the case.
	cp, env := enrolled(t)
	csrPEM, err := GenerateCSR(env.cfg)
	if err != nil {
		t.Fatal(err)
	}
	other, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	certPEM, chainPEM := cp.issueFor(t, csrPEM, env.id.DeviceID, other, time.Now().Add(365*24*time.Hour))

	if _, err := Install(env.cfg, certPEM, chainPEM); err == nil {
		t.Fatal("installed a certificate this endpoint has no key for")
	} else if !strings.Contains(err.Error(), "pending key") {
		t.Fatalf("wrong reason: %v", err)
	}
	assertStillOnTheOldIdentity(t, env)
}

func TestInstallRefusesACertificateForAnotherDevice(t *testing.T) {
	// THE GUARD, again with everything else satisfied: the certificate is over
	// THIS endpoint's pending key and chains to the pinned root; only the CN
	// names somebody else.
	cp, env := enrolled(t)
	csrPEM, err := GenerateCSR(env.cfg)
	if err != nil {
		t.Fatal(err)
	}
	certPEM, chainPEM := cp.issueFor(
		t, csrPEM, "00000000-0000-0000-0000-000000000000", nil, time.Now().Add(365*24*time.Hour))

	if _, err := Install(env.cfg, certPEM, chainPEM); err == nil {
		t.Fatal("installed a certificate naming a different device")
	} else if !strings.Contains(err.Error(), "not this one") {
		t.Fatalf("wrong reason: %v", err)
	}
	assertStillOnTheOldIdentity(t, env)
}

func TestInstallRefusesACertificateFromAnotherRoot(t *testing.T) {
	// THE GUARD: pinning holds at renewal. A second, perfectly well-formed CA
	// issues a certificate over this endpoint's own pending key, with the right
	// CN -- and it is refused because it does not chain to the bundle pinned at
	// enrollment.
	_, env := enrolled(t)
	rogue := newFakeCP(t)
	csrPEM, err := GenerateCSR(env.cfg)
	if err != nil {
		t.Fatal(err)
	}
	certPEM, chainPEM := rogue.issueFor(t, csrPEM, env.id.DeviceID, nil, time.Now().Add(365*24*time.Hour))

	if _, err := Install(env.cfg, certPEM, chainPEM); err == nil {
		t.Fatal("installed a certificate from a root this endpoint never pinned")
	} else if !strings.Contains(err.Error(), "pinned at enrollment") {
		t.Fatalf("wrong reason: %v", err)
	}
	assertStillOnTheOldIdentity(t, env)
}

func TestInstallWithNoPendingKeyIsRefusedAndSurvivable(t *testing.T) {
	cp, env := enrolled(t)
	csrPEM, err := GenerateCSR(env.cfg)
	if err != nil {
		t.Fatal(err)
	}
	certPEM, chainPEM := cp.issueFor(t, csrPEM, env.id.DeviceID, nil, time.Now().Add(365*24*time.Hour))
	if err := os.Remove(env.cfg.PendingKeyPath()); err != nil {
		t.Fatal(err)
	}

	_, err = Install(env.cfg, certPEM, chainPEM)
	if !errors.Is(err, ErrNoPendingKey) {
		t.Fatalf("err = %v, want ErrNoPendingKey", err)
	}
	assertStillOnTheOldIdentity(t, env)
}

func assertStillOnTheOldIdentity(t *testing.T, env cfgAndID) {
	t.Helper()
	reloaded, err := Load(env.cfg)
	if err != nil {
		t.Fatalf("a refused install left the endpoint unable to load its identity: %v", err)
	}
	if string(reloaded.Cert.Leaf.Raw) != string(env.id.Cert.Leaf.Raw) {
		t.Fatal("a refused install changed the working certificate")
	}
	if _, err := os.Stat(env.cfg.RotationPath()); !os.IsNotExist(err) {
		t.Fatal("a refused install left a commit marker behind")
	}
}

// ---------------------------------------------------------------------------
// §4.2 — the recovery rule
// ---------------------------------------------------------------------------

func TestAnInterruptedSwapIsFinishedOnTheNextStart(t *testing.T) {
	// Simulates a crash between step 2 (the marker) and step 4 (identity.json):
	// the marker and the pending key are on disk, the live identity is still
	// the old one. LoadOrEnroll must finish the swap, not refuse to start.
	cp, env := enrolled(t)
	csrPEM, err := GenerateCSR(env.cfg)
	if err != nil {
		t.Fatal(err)
	}
	certPEM, chainPEM := cp.issueFor(t, csrPEM, env.id.DeviceID, nil, time.Now().Add(365*24*time.Hour))
	if err := writeJSON(env.cfg.RotationPath(), &persistedIdentity{
		DeviceID:   env.id.DeviceID,
		GatewayURL: env.id.GatewayURL,
		CertPEM:    certPEM,
		ChainPEM:   chainPEM,
	}); err != nil {
		t.Fatal(err)
	}

	recovered, err := LoadOrEnroll(t.Context(), env.cfg, Options{})
	if err != nil {
		t.Fatalf("an interrupted rotation stopped the agent starting: %v", err)
	}
	if string(recovered.Cert.Leaf.Raw) == string(env.id.Cert.Leaf.Raw) {
		t.Fatal("the interrupted swap was not finished")
	}
	for _, path := range []string{env.cfg.RotationPath(), env.cfg.PendingKeyPath()} {
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Errorf("%s survived recovery", path)
		}
	}
}

func TestASwapWhoseKeyIsGoneFallsBACKNotForward(t *testing.T) {
	// The broken-invariant case: a marker with no pending key. There is no way
	// to complete the swap, so recovery must restore the certificate that WAS
	// working rather than leave a certificate with no key -- which would not
	// load at all, and an agent that will not start is not recoverable over the
	// network.
	cp, env := enrolled(t)
	csrPEM, err := GenerateCSR(env.cfg)
	if err != nil {
		t.Fatal(err)
	}
	certPEM, chainPEM := cp.issueFor(t, csrPEM, env.id.DeviceID, nil, time.Now().Add(365*24*time.Hour))
	if _, err := Install(env.cfg, certPEM, chainPEM); err != nil {
		t.Fatal(err)
	}
	// now corrupt the world: a marker for a THIRD certificate, and no key.
	newCSR, err := GenerateCSR(env.cfg)
	if err != nil {
		t.Fatal(err)
	}
	thirdPEM, thirdChain := cp.issueFor(t, newCSR, env.id.DeviceID, nil, time.Now().Add(365*24*time.Hour))
	if err := writeJSON(env.cfg.RotationPath(), &persistedIdentity{
		DeviceID:   env.id.DeviceID,
		GatewayURL: env.id.GatewayURL,
		CertPEM:    thirdPEM,
		ChainPEM:   thirdChain,
	}); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(env.cfg.PendingKeyPath()); err != nil {
		t.Fatal(err)
	}

	recovered, err := LoadOrEnroll(t.Context(), env.cfg, Options{})
	if err != nil {
		t.Fatalf("the agent could not start: %v", err)
	}
	if string(recovered.Cert.Leaf.Raw) == string(mustDER(t, thirdPEM)) {
		t.Fatal("recovery moved FORWARD onto a certificate with no key")
	}
	if string(recovered.Cert.Leaf.Raw) != string(env.id.Cert.Leaf.Raw) {
		t.Fatal("recovery did not fall back to the identity that was known to work")
	}
	if _, err := os.Stat(env.cfg.RotationPath()); !os.IsNotExist(err) {
		t.Error("the marker survived recovery, so this would repeat forever")
	}
}

// ---------------------------------------------------------------------------
// §4.4 — the revert
// ---------------------------------------------------------------------------

func TestARevertPutsTheEndpointBackOnTheCertificateTheServerStillAccepts(t *testing.T) {
	// The lost-acknowledgement case: the endpoint installed, the control plane
	// never recorded it, so the gateway's gate still names the OLD certificate
	// and closes the new one 4401.
	cp, env := enrolled(t)
	csrPEM, err := GenerateCSR(env.cfg)
	if err != nil {
		t.Fatal(err)
	}
	certPEM, chainPEM := cp.issueFor(t, csrPEM, env.id.DeviceID, nil, time.Now().Add(365*24*time.Hour))
	if _, err := Install(env.cfg, certPEM, chainPEM); err != nil {
		t.Fatal(err)
	}

	reverted, err := RevertToPrevious(env.cfg)
	if err != nil || !reverted {
		t.Fatalf("RevertToPrevious = %v, %v", reverted, err)
	}
	back, err := Load(env.cfg)
	if err != nil {
		t.Fatalf("the reverted identity does not load: %v", err)
	}
	if string(back.Cert.Leaf.Raw) != string(env.id.Cert.Leaf.Raw) {
		t.Fatal("the revert did not restore the certificate the control plane still accepts")
	}
}

func TestARevertIsAvailableExactlyOncePerRotation(t *testing.T) {
	// THE BOUND, and it is the whole reason 4401 can stop being terminal
	// without becoming a retry storm. A revert consumes previous.json, so the
	// second 4401 in the same situation is terminal exactly as before -- which
	// is what makes a fleet whose certificates have genuinely expired stop
	// dialling rather than hammer the control plane.
	cp, env := enrolled(t)
	csrPEM, err := GenerateCSR(env.cfg)
	if err != nil {
		t.Fatal(err)
	}
	certPEM, chainPEM := cp.issueFor(t, csrPEM, env.id.DeviceID, nil, time.Now().Add(365*24*time.Hour))
	if _, err := Install(env.cfg, certPEM, chainPEM); err != nil {
		t.Fatal(err)
	}

	first, err := RevertToPrevious(env.cfg)
	if err != nil || !first {
		t.Fatalf("first revert = %v, %v", first, err)
	}
	second, err := RevertToPrevious(env.cfg)
	if err != nil {
		t.Fatal(err)
	}
	if second {
		t.Fatal("a second revert was available; 4401 would never become terminal again")
	}
}

func TestAnEndpointThatNeverRotatedHasNothingToRevertTo(t *testing.T) {
	// The compatibility direction, on disk: an agent talking to a control plane
	// that never dispatches a rotation has no previous.json, so 4401 stays
	// terminal for it exactly as it always was.
	_, env := enrolled(t)
	reverted, err := RevertToPrevious(env.cfg)
	if err != nil {
		t.Fatal(err)
	}
	if reverted {
		t.Fatal("reverted with no rotation ever performed")
	}
}

func mustDER(t *testing.T, certPEM string) []byte {
	t.Helper()
	block, _ := pem.Decode([]byte(certPEM))
	if block == nil {
		t.Fatal("not PEM")
	}
	return block.Bytes
}
