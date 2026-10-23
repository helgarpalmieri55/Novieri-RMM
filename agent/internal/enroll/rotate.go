// Certificate rotation on the endpoint (docs/modules/agent/cert-renewal.md §4).
//
// # This is renewal BEFORE expiry, and it cannot be anything else
//
// The certificate IS the mTLS credential. Once NotAfter has passed the
// handshake fails, so there is no WebSocket, so there is no channel down which
// a replacement could travel -- the gateway closes an expired certificate 4401
// before a single byte of application protocol is read (app/gateway/main.go's
// `agent.cert_not_after < now`). A renewal that waited for expiry would be a
// job nobody can deliver.
//
// So the control plane starts rotation at two-thirds of the certificate's life
// (day 243 of 365) and retries on every connect. Nothing in this file decides
// WHEN: rotation is server-initiated and arrives as two signed jobs. This file
// is what those jobs do on disk.
//
// **Do not "fix" this to happen on expiry.** The failure that produces is not a
// failed rotation, it is a fleet that cannot be recovered over the network.
//
// # Invariant #3: the private key never leaves the endpoint
//
// A new keypair is generated HERE, at renewal, exactly as at enrollment. Only
// the CSR -- the public half plus a proof of possession -- is transmitted. A
// design in which the server generates the key and ships it down in a job
// payload would put every endpoint's private key in the control plane's memory,
// its logs and its job table.
//
// # The swap is a transaction, because half a swap does not start
//
// An endpoint that lost power between writing the new key and writing the new
// certificate would come back with a key and a certificate that do not pair;
// `build` refuses that, `LoadOrEnroll` refuses to re-enroll over it, and the
// agent would not start. So:
//
//  1. previous.json  {cert, chain, key}  <- the identity being replaced
//  2. rotation.json  the whole new identity  <- COMMIT MARKER
//  3. agent.key      <- agent.key.pending
//  4. identity.json  <- the new identity
//  5. remove rotation.json, then agent.key.pending
//
// Every write is atomic (temp + rename). rotation.json is removed BEFORE the
// pending key, so `rotation.json exists` implies `agent.key.pending exists`,
// and RecoverRotation can tell "finish it" from "unwind it".
package enroll

import (
	"crypto"
	"crypto/rand"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
	"os"

	"crypto/ed25519"

	"github.com/toeshee/it-suite/agent/internal/config"
)

// ErrNoPendingKey is returned when an install arrives with no CSR behind it.
//
// It is a real state, not an impossibility: an operator can clear the data
// directory, and a control plane that has an unconsumed CSR on file will sign
// it and send the certificate down anyway. Refusing is correct -- the endpoint
// has no key for that certificate -- and the failed job makes the server
// consume the dead CSR and ask for a fresh one (server §0.7).
var ErrNoPendingKey = errors.New("rotate: no pending key; this endpoint did not produce that CSR")

// previousIdentity is the certificate a rotation replaced, kept so a rotation
// the control plane never recorded can be undone (§4.4).
type previousIdentity struct {
	CertPEM  string `json:"cert_pem"`
	ChainPEM string `json:"chain_pem"`
	KeyPEM   string `json:"key_pem"`
}

// GenerateCSR generates a fresh keypair, persists the private half as the
// PENDING key, and returns a CSR over it.
//
// A NEW keypair every rotation. Reusing the old one would renew the certificate
// and not the identity, which is most of the value of rotating at all.
//
// The CSR subject is EMPTY on purpose, exactly as at enrollment: the CA
// overwrites CN and O from the job's device_id/org_id and never reads the CSR
// subject, so anything here would be ignored while reading as meaningful.
func GenerateCSR(cfg config.Config) (string, error) {
	_, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return "", fmt.Errorf("rotate: generate key: %w", err)
	}
	csrDER, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{}, priv)
	if err != nil {
		return "", fmt.Errorf("rotate: build CSR: %w", err)
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(priv)
	if err != nil {
		return "", err
	}
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER})
	if err := os.MkdirAll(cfg.DataDir, config.DirMode); err != nil {
		return "", err
	}
	// Overwrites any earlier pending key. A CSR the control plane never used is
	// dead the moment a newer one exists, and keeping both would mean guessing
	// which certificate a later install belongs to.
	if err := writeFile(cfg.PendingKeyPath(), keyPEM, config.KeyMode); err != nil {
		return "", err
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: csrDER})), nil
}

// Install validates an issued certificate and swaps this endpoint's identity
// onto it, returning the new identity.
//
// FOUR CHECKS BEFORE ANYTHING IS WRITTEN, and each closes a distinct hole:
//
//   - the leaf's public key is the PENDING key's. This is what stops a
//     certificate issued over somebody else's key being installed here — a
//     certificate the endpoint cannot use, which would brick it on the next
//     dial. Stated honestly: `build`'s X509KeyPair would also reject that
//     case, with "certificate and key do not form a pair" — measured by
//     deleting this check and watching which message appeared. It is kept
//     because it names the actual problem, and because it fires BEFORE
//     anything is written; the falsifying test asserts the reason text, not
//     merely that an error occurred, so the two are distinguishable.
//   - the leaf's CN is this device's own device_id. The job was already
//     verified to target this device; this refuses a certificate that names a
//     different one even so.
//   - the leaf chains to the CA BUNDLE PINNED AT ENROLLMENT, with client-auth
//     usage. A control plane cannot hand this endpoint an identity from another
//     root. If the root genuinely changes that is a re-deployment, which is the
//     answer pinning has always given.
//   - `build` re-derives everything the agent will later depend on, so a
//     certificate that cannot produce a working agent never reaches the disk.
func Install(cfg config.Config, certPEM, chainPEM string) (*Identity, error) {
	current, err := Load(cfg)
	if err != nil {
		return nil, fmt.Errorf("rotate: current identity is unusable: %w", err)
	}
	pins, err := loadPins(cfg)
	if err != nil {
		return nil, err
	}
	// The PEM bytes as persisted, not re-encoded from the parsed leaf: what
	// previous.json holds has to be exactly what worked, byte for byte.
	var currentPersisted persistedIdentity
	if err := readJSON(cfg.IdentityPath(), &currentPersisted); err != nil {
		return nil, err
	}
	currentKeyPEM, err := os.ReadFile(cfg.KeyPath())
	if err != nil {
		return nil, err
	}

	pendingKeyPEM, err := os.ReadFile(cfg.PendingKeyPath())
	if err != nil {
		if os.IsNotExist(err) {
			return nil, ErrNoPendingKey
		}
		return nil, err
	}
	pendingBlock, _ := pem.Decode(pendingKeyPEM)
	if pendingBlock == nil {
		return nil, errors.New("rotate: the pending key is not PEM")
	}
	pendingKey, err := x509.ParsePKCS8PrivateKey(pendingBlock.Bytes)
	if err != nil {
		return nil, fmt.Errorf("rotate: pending key: %w", err)
	}

	leafBlock, _ := pem.Decode([]byte(certPEM))
	if leafBlock == nil {
		return nil, errors.New("rotate: cert_pem is not PEM")
	}
	leaf, err := x509.ParseCertificate(leafBlock.Bytes)
	if err != nil {
		return nil, fmt.Errorf("rotate: issued certificate: %w", err)
	}

	signer, ok := pendingKey.(crypto.Signer)
	if !ok {
		return nil, errors.New("rotate: the pending key cannot sign")
	}
	comparable, ok := signer.Public().(interface{ Equal(crypto.PublicKey) bool })
	if !ok || !comparable.Equal(leaf.PublicKey) {
		return nil, errors.New(
			"rotate: the issued certificate is not over this endpoint's pending key")
	}
	if leaf.Subject.CommonName != current.DeviceID {
		return nil, fmt.Errorf(
			"rotate: the issued certificate names device %q, not this one",
			leaf.Subject.CommonName)
	}

	intermediates := x509.NewCertPool()
	intermediates.AppendCertsFromPEM([]byte(chainPEM))
	if _, err := leaf.Verify(x509.VerifyOptions{
		Roots:         current.CACerts,
		Intermediates: intermediates,
		KeyUsages:     []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
	}); err != nil {
		return nil, fmt.Errorf(
			"rotate: the issued certificate does not chain to the CA pinned at enrollment: %w", err)
	}

	next := &persistedIdentity{
		DeviceID:   current.DeviceID,
		GatewayURL: current.GatewayURL,
		CertPEM:    certPEM,
		ChainPEM:   chainPEM,
	}
	installed, err := build(next, pins, pendingKeyPEM)
	if err != nil {
		return nil, err
	}

	// --- the transaction (see the package comment) ---
	if err := writeJSON(cfg.PreviousIdentityPath(), &previousIdentity{
		CertPEM:  currentPersisted.CertPEM,
		ChainPEM: currentPersisted.ChainPEM,
		KeyPEM:   string(currentKeyPEM),
	}); err != nil {
		return nil, err
	}
	if err := writeJSON(cfg.RotationPath(), next); err != nil {
		return nil, err
	}
	if err := commitRotation(cfg, next, pendingKeyPEM); err != nil {
		return nil, err
	}
	return installed, nil
}

// commitRotation performs steps 3-5. Split out because RecoverRotation redoes
// exactly these, idempotently, after an interrupted swap.
func commitRotation(cfg config.Config, next *persistedIdentity, keyPEM []byte) error {
	if err := writeFile(cfg.KeyPath(), keyPEM, config.KeyMode); err != nil {
		return err
	}
	if err := writeJSON(cfg.IdentityPath(), next); err != nil {
		return err
	}
	// rotation.json FIRST: it is the marker, and removing it before the pending
	// key preserves "marker present implies pending key present", which is the
	// only thing that lets RecoverRotation tell finish from unwind.
	if err := os.Remove(cfg.RotationPath()); err != nil && !os.IsNotExist(err) {
		return err
	}
	if err := os.Remove(cfg.PendingKeyPath()); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// RecoverRotation finishes or unwinds a swap that was interrupted.
//
// Called from LoadOrEnroll BEFORE anything is loaded. The common case is one
// os.Stat that finds nothing.
//
// When the marker is present and the pending key is there, the swap is redone
// from the marker -- every step is idempotent. When the marker is present and
// the pending key is NOT, the invariant is broken (disk damage, or a hand-edited
// data directory) and the identity is restored from previous.json: **fail back
// to the certificate that was working, never forward to one that might not be.**
func RecoverRotation(cfg config.Config) error {
	var next persistedIdentity
	if err := readJSON(cfg.RotationPath(), &next); err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		// An unreadable marker is not a reason to refuse to start: it names an
		// identity we cannot act on, so drop it and let whatever is on disk be
		// the identity. If that is broken, Load says so with a better message.
		return os.Remove(cfg.RotationPath())
	}
	keyPEM, err := os.ReadFile(cfg.PendingKeyPath())
	if err == nil {
		return commitRotation(cfg, &next, keyPEM)
	}
	if !os.IsNotExist(err) {
		return err
	}
	var prev previousIdentity
	if perr := readJSON(cfg.PreviousIdentityPath(), &prev); perr != nil {
		// Nothing to go back to either. Leave identity.json alone and clear the
		// marker; Load will report whatever is actually wrong.
		return os.Remove(cfg.RotationPath())
	}
	if err := restore(cfg, next.DeviceID, next.GatewayURL, &prev); err != nil {
		return err
	}
	return os.Remove(cfg.RotationPath())
}

// RevertToPrevious puts the endpoint back on the certificate a rotation
// replaced, and reports whether it did.
//
// WHY 4401 IS NOT UNCONDITIONALLY TERMINAL ANY MORE. The dangerous case is
// narrow and real: the agent installs, sends the result, and the connection
// dies before the control plane commits it. The endpoint then holds a
// certificate the control plane has never heard of, and the gateway's admission
// gate still names the old one -- which the endpoint has just replaced.
//
// This is bounded BY CONSTRUCTION: a revert deletes previous.json, so a second
// 4401 in the same situation is terminal exactly as before. A fleet whose
// certificates have genuinely expired still stops dialling, because the
// previous certificate has expired too.
//
// It does NOT circumvent revocation. A revoked agent's row is refused whatever
// fingerprint it presents, and the previous certificate's fingerprint is not in
// `agents` at all -- so the revert costs one extra dial and changes no verdict.
func RevertToPrevious(cfg config.Config) (bool, error) {
	var prev previousIdentity
	if err := readJSON(cfg.PreviousIdentityPath(), &prev); err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, err
	}
	var current persistedIdentity
	if err := readJSON(cfg.IdentityPath(), &current); err != nil {
		return false, err
	}
	if err := restore(cfg, current.DeviceID, current.GatewayURL, &prev); err != nil {
		return false, err
	}
	if err := os.Remove(cfg.PreviousIdentityPath()); err != nil && !os.IsNotExist(err) {
		return false, err
	}
	return true, nil
}

// restore writes a previous identity back over the current one.
func restore(cfg config.Config, deviceID, gatewayURL string, prev *previousIdentity) error {
	if prev.CertPEM == "" || prev.KeyPEM == "" {
		return errors.New("rotate: previous.json carries no usable identity")
	}
	if err := writeFile(cfg.KeyPath(), []byte(prev.KeyPEM), config.KeyMode); err != nil {
		return err
	}
	return writeJSON(cfg.IdentityPath(), &persistedIdentity{
		DeviceID:   deviceID,
		GatewayURL: gatewayURL,
		CertPEM:    prev.CertPEM,
		ChainPEM:   prev.ChainPEM,
	})
}

func loadPins(cfg config.Config) (*persistedPins, error) {
	var pins persistedPins
	if err := readJSON(cfg.PinsPath(), &pins); err != nil {
		return nil, err
	}
	return &pins, nil
}
