// Package config resolves the agent's process configuration from the
// environment and defines the data-directory layout.
//
// The three environment variables are fixed by scripts/demo_e2e_agent.py,
// which launches the real binary with exactly ITS_AGENT_ENROLL_URL,
// ITS_AGENT_ENROLL_TOKEN and ITS_AGENT_DATA_DIR. They are an interface, not an
// implementation detail.
//
// There is one non-environment input, and it is the credential: the enrollment
// token may instead arrive as a 0600 file at EnrollTokenPath(), which is how
// every installer under agent/packaging/ hands it over. The environment wins if
// both are set. The file is deleted the moment it has bought a certificate.
//
// There is deliberately no configuration that affects job verification. The
// signing key, the CA bundle and the gateway URL all arrive from enrollment
// and are persisted under DataDir; none of them is settable from the
// environment, so no operator-supplied value can weaken the checks in
// docs/03-security-model.md (invariant #1).
package config

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// Environment variable names. Fixed by scripts/demo_e2e_agent.py:191-198.
const (
	EnvEnrollURL   = "ITS_AGENT_ENROLL_URL"
	EnvEnrollToken = "ITS_AGENT_ENROLL_TOKEN"
	EnvDataDir     = "ITS_AGENT_DATA_DIR"
)

// File names inside DataDir. Each is written by exactly one package.
const (
	identityFile = "identity.json" // device_id, gateway_url, cert + chain PEM
	keyFile      = "agent.key"     // device private key, mode 0600
	pinsFile     = "pins.json"     // ca_bundle_pem, job_signing_public_key_pem
	nonceFile    = "nonces.json"   // bounded replay store
	bufferFile   = "outbound.log"  // bounded offline outbound buffer

	// The enrollment token an INSTALLER left behind, mode 0600. Written by
	// agent/packaging/*, read once here, and deleted by enroll.LoadOrEnroll the
	// moment it has bought a certificate. See TokenMode below.
	enrollTokenFile = "enroll-token"

	// Certificate rotation (docs/modules/agent/cert-renewal.md §4.2). Three
	// files that exist only DURING a rotation, or between one and the next.
	pendingKeyFile = "agent.key.pending" // the key a CSR was built over
	rotationFile   = "rotation.json"     // the commit marker for the swap
	previousFile   = "previous.json"     // the identity that was replaced
)

// DirMode is the mode the data directory is created with: the device private
// key lives there, so it is not group- or world-readable (invariant #3).
const DirMode os.FileMode = 0o700

// KeyMode is the mode of the device private key file.
const KeyMode os.FileMode = 0o600

// TokenMode is the mode the enrollment token file must have: owner only.
//
// It is enforced rather than assumed. An enrollment token mints a device
// identity in the tenant that issued it, so a token file any local user can
// read is the same class of disclosure as a readable private key (invariant
// #3) — and unlike a key, it is written by a shell script, where a forgotten
// umask is one line away.
const TokenMode os.FileMode = 0o600

// maxTokenFile bounds the read. A token is tens of bytes; anything larger is a
// pointed-at-the-wrong-file mistake, and this keeps it from becoming a memory
// one.
const maxTokenFile = 4096

// ErrNoEnrollment is returned when the agent has neither a persisted identity
// nor the enrollment credentials it would need to obtain one.
var ErrNoEnrollment = errors.New(
	"no persisted identity and no enrollment credentials: set " +
		EnvEnrollURL + " and " + EnvEnrollToken + " for a first run",
)

// Config is the resolved process configuration.
type Config struct {
	// EnrollURL is the API base URL, with no trailing slash. Enrollment posts
	// to EnrollURL + "/enroll/v1" — the enrollment router is mounted with no
	// prefix (app/main.py), so it is NOT under /api/v1.
	EnrollURL string
	// EnrollToken is the single-use enrollment token. Empty once enrolled.
	EnrollToken string
	// DataDir holds the identity, the key, the pins and the replay store.
	DataDir string
}

// Load resolves the configuration from the environment, falling back to the
// installer's token file for the one value that is a credential.
//
// It fails only when the agent can neither load an identity nor obtain one: an
// already-enrolled agent needs no enrollment URL or token, because the token
// was consumed at first boot and cannot be replayed.
func Load() (Config, error) {
	c := Config{
		EnrollURL:   strings.TrimRight(strings.TrimSpace(os.Getenv(EnvEnrollURL)), "/"),
		EnrollToken: strings.TrimSpace(os.Getenv(EnvEnrollToken)),
		DataDir:     strings.TrimSpace(os.Getenv(EnvDataDir)),
	}
	if c.DataDir == "" {
		c.DataDir = defaultDataDir()
	}
	abs, err := filepath.Abs(c.DataDir)
	if err != nil {
		return Config{}, err
	}
	c.DataDir = abs

	// The token file is consulted ONLY when there is no identity yet.
	//
	// That is not an optimisation. A spent token file with the wrong mode must
	// not stop an enrolled agent from starting — a service that refuses to boot
	// over a credential it no longer needs is an outage caused by hygiene.
	// enroll.LoadOrEnroll deletes the leftover instead.
	if !c.Enrolled() && c.EnrollToken == "" {
		tok, err := readTokenFile(c.EnrollTokenPath())
		if err != nil {
			return Config{}, err
		}
		c.EnrollToken = tok
	}

	if !c.Enrolled() && (c.EnrollURL == "" || c.EnrollToken == "") {
		return Config{}, ErrNoEnrollment
	}
	return c, nil
}

// EnrollTokenPath is the file an installer writes the enrollment token to.
//
// It exists because the two obvious places to put a token are both worse: argv
// is world-readable through /proc/<pid>/cmdline for the life of the process,
// and a systemd Environment= line is printed by `systemctl show` and persists
// in the unit file long after the token is spent.
func (c Config) EnrollTokenPath() string { return filepath.Join(c.DataDir, enrollTokenFile) }

// ConsumeEnrollToken deletes the token file. An absent file is success: this is
// called on every boot, and after the first one there is nothing to delete.
func (c Config) ConsumeEnrollToken() error {
	if err := os.Remove(c.EnrollTokenPath()); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

// readTokenFile reads the token, refusing one that others can read.
//
// Absent is not an error — most boots have no token file — but present and
// group- or world-readable is, and the error names the file so the operator can
// fix the thing that is wrong rather than hunt for it.
func readTokenFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return "", nil
		}
		return "", err
	}
	defer f.Close()

	info, err := f.Stat()
	if err != nil {
		return "", err
	}
	// Windows has no meaningful unix mode bits: os.Stat synthesises 0666/0444
	// from the read-only attribute, so this check there would reject every file
	// while measuring nothing. The Windows installer sets an ACL instead
	// (agent/packaging/windows/install.ps1).
	if runtime.GOOS != "windows" && info.Mode().Perm()&^TokenMode != 0 {
		return "", fmt.Errorf(
			"%s is mode %04o: an enrollment token must be %04o (owner only) — "+
				"it mints a device identity in your tenant and this one is readable by others",
			path, info.Mode().Perm(), TokenMode)
	}

	b, err := io.ReadAll(io.LimitReader(f, maxTokenFile))
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(b)), nil
}

// Enrolled reports whether an identity has already been persisted.
func (c Config) Enrolled() bool {
	_, err := os.Stat(c.IdentityPath())
	return err == nil
}

// IdentityPath is the persisted device identity (device_id, gateway_url, certs).
func (c Config) IdentityPath() string { return filepath.Join(c.DataDir, identityFile) }

// KeyPath is the device private key. Written KeyMode, never logged, never sent.
func (c Config) KeyPath() string { return filepath.Join(c.DataDir, keyFile) }

// PinsPath holds the pinned CA bundle and the pinned job-signing public key.
func (c Config) PinsPath() string { return filepath.Join(c.DataDir, pinsFile) }

// NoncePath is the persisted nonce store backing replay rejection.
func (c Config) NoncePath() string { return filepath.Join(c.DataDir, nonceFile) }

// BufferPath is the bounded outbound buffer used while disconnected.
func (c Config) BufferPath() string { return filepath.Join(c.DataDir, bufferFile) }

// PendingKeyPath is the private key `agent.cert.renew` generated and built a
// CSR over, waiting for the certificate that `agent.cert.install` will carry.
// Written KeyMode, never logged, never sent -- invariant #3 holds at renewal
// exactly as at enrollment, and only the CSR ever leaves the endpoint.
func (c Config) PendingKeyPath() string { return filepath.Join(c.DataDir, pendingKeyFile) }

// RotationPath is the commit marker for an identity swap. Its presence means a
// swap was interrupted; enroll.RecoverRotation finishes or unwinds it before
// anything reads the identity. See docs/modules/agent/cert-renewal.md §4.2.
func (c Config) RotationPath() string { return filepath.Join(c.DataDir, rotationFile) }

// PreviousIdentityPath holds the certificate and key a rotation replaced.
//
// It is what makes a lost acknowledgement survivable: if the control plane
// never recorded the install, the gateway closes the new certificate 4401 and
// the agent reverts to this one rather than being locked out for good (§4.4).
func (c Config) PreviousIdentityPath() string { return filepath.Join(c.DataDir, previousFile) }

func defaultDataDir() string {
	if runtime.GOOS == "windows" {
		base := os.Getenv("ProgramData")
		if base == "" {
			base = `C:\ProgramData`
		}
		return filepath.Join(base, "ITSuite", "agent")
	}
	return filepath.Join("/var", "lib", "its-agent")
}
