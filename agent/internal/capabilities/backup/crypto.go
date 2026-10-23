package backup

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// Envelope encryption for repository objects (spec §5, §7), and the handling of
// the one secret this capability ever holds.
//
// # How the key gets here, and why that is invariant #3 rather than a hole
//
// The repository master key is generated server-side, sealed through the vault
// under the org DEK, and the app database holds only the sealed reference
// (repository.py::create_repository). At dispatch, dispatch_backup opens it and
// places it — base64 — inside `BackupRunParams.repo_key`, which travels inside
// the ED25519-SIGNED job envelope over mutual TLS to one device
// (dispatch.py::dispatch_backup, spec §7).
//
// So the key that reaches this endpoint is JOB-SCOPED by construction: it
// arrives with a job that carries a target device, a freshness window and a
// nonce, and it is the only form in which key material ever reaches an
// endpoint. THERE IS NO LONG-LIVED KEY ON THE AGENT and this package must never
// create one. Concretely, that means:
//
//   - the decoded key is never written to disk, in any file, under any name;
//   - it is never logged, at any level, and never formatted into an error, a
//     refusal reason or a job result (a refusal reason lands in the server's
//     hash-chained audit log);
//   - it does not outlive the job: repoKey.zero() overwrites the bytes and is
//     deferred at the top of both capabilities, so it runs on the error path
//     and on the panic path too.
//
// # What zeroing does NOT cover, stated rather than glossed
//
// The key also exists as a base64 STRING inside the params document, and Go
// strings are immutable — that copy cannot be overwritten and is released to
// the garbage collector when the job's frame is. Zeroing the decoded bytes
// removes the plaintext-key copy this package makes; it does not and cannot
// remove the encoded copy the runner already held. Closing that would mean
// changing how the runner owns frame bytes, which is kernel surface this
// module does not fork (CLAUDE.md: "modules register into the kernel; they do
// not fork it"). It is recorded here so nobody later reads `zero()` as a
// stronger claim than it is.

const (
	// aesKeyLen is 32: AES-256-GCM, per spec §5.
	aesKeyLen = 32
	// nonceLen is 12, and the wire layout is `nonce(12) || ciphertext || tag(16)`.
	// It MUST match app/modules/backup/restore.py::seal_manifest, which the
	// server uses to write and read manifests it produces or browses — a
	// different nonce width there and here is a manifest neither side can open.
	nonceLen = 12
	tagLen   = 16
)

// repoKey is the job-scoped repository master key.
type repoKey struct {
	raw  []byte
	aead cipher.AEAD
}

// decodeRepoKey parses `repo_key` and refuses anything that is not a 32-byte
// AES-256 key.
//
// REFUSES, and the reason never contains the value or its length in a form that
// distinguishes near-misses beyond what an operator needs. A key that does not
// decode is not a transient condition and retrying it forever would be worse
// than saying so once.
func decodeRepoKey(encoded string) (*repoKey, error) {
	if encoded == "" {
		return nil, jobs.Refuse("backup: the job carries no repo_key. Repository objects are " +
			"AES-256-GCM under a per-org key that only ever reaches an endpoint inside a signed " +
			"job; there is no unencrypted repository format and this agent will not write one")
	}
	raw, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return nil, jobs.Refuse("backup: repo_key is not base64")
	}
	if len(raw) != aesKeyLen {
		// The LENGTH is safe to state — it is a property of the key format, not
		// of the key — and it is the one fact that tells an operator whether the
		// vault handed back something truncated.
		n := len(raw)
		zeroBytes(raw)
		return nil, jobs.Refuse("backup: repo_key decodes to %d bytes, not %d (AES-256)",
			n, aesKeyLen)
	}
	block, err := aes.NewCipher(raw)
	if err != nil {
		zeroBytes(raw)
		return nil, jobs.Refuse("backup: repo_key is not a usable AES key")
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		zeroBytes(raw)
		return nil, jobs.Refuse("backup: AES-GCM is unavailable in this build")
	}
	return &repoKey{raw: raw, aead: aead}, nil
}

// zero overwrites the key material. Deferred by both capabilities so it runs on
// every path out, including a panic the runner recovers.
func (k *repoKey) zero() {
	if k == nil {
		return
	}
	zeroBytes(k.raw)
	k.raw = nil
	k.aead = nil
}

func zeroBytes(b []byte) {
	for i := range b {
		b[i] = 0
	}
}

// seal encrypts plaintext into dst, returning `nonce || ciphertext || tag`.
//
// dst is reused across chunks by the caller so a run of ten thousand chunks
// allocates one ciphertext buffer, not ten thousand. Passing dst=nil is fine
// and allocates.
//
// NO AAD, matching restore.py::seal_manifest, whose comment records the
// reasoning: the object's own reference identifies it, and an AAD that the two
// implementations spelled differently would be a manifest the server writes and
// the agent cannot open (or the reverse) with no error until the day of a
// restore.
func (k *repoKey) seal(dst, plaintext []byte) ([]byte, error) {
	if k == nil || k.aead == nil {
		return nil, errors.New("backup: seal after the repository key was zeroed")
	}
	need := nonceLen + len(plaintext) + tagLen
	if cap(dst) < need {
		dst = make([]byte, 0, need)
	}
	out := dst[:nonceLen]
	if _, err := rand.Read(out[:nonceLen]); err != nil {
		return nil, fmt.Errorf("backup: no randomness for a chunk nonce: %w", err)
	}
	// A RANDOM nonce, not a deterministic one. Spec §5: store-once by
	// plaintext id already gives dedup, so convergent encryption would buy
	// nothing and would leak which chunks two tenants share.
	return k.aead.Seal(out, out[:nonceLen], plaintext, nil), nil
}

// open is seal's inverse. dst is reused the same way.
func (k *repoKey) open(dst, blob []byte) ([]byte, error) {
	if k == nil || k.aead == nil {
		return nil, errors.New("backup: open after the repository key was zeroed")
	}
	if len(blob) < nonceLen+tagLen {
		return nil, fmt.Errorf("backup: repository object is %d bytes, shorter than a nonce and "+
			"a tag", len(blob))
	}
	need := len(blob) - nonceLen - tagLen
	if cap(dst) < need {
		dst = make([]byte, 0, need)
	}
	// A failure here is an authentication failure: the object was corrupted,
	// truncated, or written under a different key. The error says so WITHOUT
	// the object's reference, because a chunk reference is derived from
	// customer file contents.
	out, err := k.aead.Open(dst[:0], blob[:nonceLen], blob[nonceLen:], nil)
	if err != nil {
		return nil, errors.New("backup: a repository object failed authentication (wrong key, or " +
			"the object was modified in storage)")
	}
	return out, nil
}
