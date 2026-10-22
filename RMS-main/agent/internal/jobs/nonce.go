package jobs

import (
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/toeshee/it-suite/agent/internal/config"
)

// NonceFile is the persisted replay store: check 4 of the signed-job runner.
//
// It implements NonceStore. (The plan names the produced type `jobs.NonceStore`;
// that name is taken by the INTERFACE the verifier depends on, because the
// verifier must not depend on a file. This is the one implementation.)
//
// # Why it is on disk
//
// Freshness alone does not stop a replay: a job is replayable for its whole
// TTL, which is 600s for net.sweep and 3600s by default (JobEngine
// DEFAULT_TTL_S). An agent that restarts inside that window -- and restarting
// an agent is something an attacker with local access can arrange -- would run
// the job again. TestAReplayIsRefusedAcrossARestartOfTheWholeRunner is the
// observation.
//
// # Why it is bounded, and what happens at the bound
//
// A store that grows without limit is a disk-exhaustion primitive reachable
// from the control plane. At the cap the SOONEST-EXPIRING entry is evicted,
// because it is the one whose job freshness will refuse first anyway. If the
// newcomer is itself the soonest-expiring, nothing can be evicted without
// forgetting something more replayable than it -- so the store FAILS CLOSED and
// reports the nonce as seen. That branch is the difference between "the cap is
// a bound" and "the cap is a switch that turns replay protection off once you
// flood past it"; TestAFullStoreThatCannotMakeRoomFailsClosed asserts it on the
// capability's invocation counter, not on a return value.
//
// # Idle cost
//
// Zero. There is no goroutine, no timer and no background compaction: the file
// is rewritten only when an entry is added, evicted or pruned, and Prune is
// driven by the scheduler's daily tick rather than by a ticker of its own.
type NonceFile struct {
	path string
	log  *slog.Logger

	mu      sync.Mutex
	entries map[string]time.Time
	cap     int
}

// NonceCap bounds the store. 10,000 in-window nonces is far beyond any real
// dispatch rate to one endpoint (it would mean ten thousand jobs inside one
// TTL) and costs well under a megabyte on disk.
const NonceCap = 10_000

// OpenNonceStore loads the store, or starts an empty one.
//
// It never fails. A corrupt or unreadable file yields an EMPTY store and a
// WARN, which is the same security posture as a freshly enrolled agent: the
// alternatives are worse in both directions. Failing open silently would
// disable replay protection with nothing observable; refusing to start would
// turn one bad file into an endpoint that never comes back.
func OpenNonceStore(path string, log *slog.Logger) *NonceFile {
	if log == nil {
		log = slog.Default()
	}
	n := &NonceFile{path: path, log: log, entries: map[string]time.Time{}, cap: NonceCap}

	raw, err := os.ReadFile(path)
	switch {
	case os.IsNotExist(err):
		return n
	case err != nil:
		n.log.Warn("replay history could not be read; starting with an empty nonce store",
			"path", path, "err", err)
		return n
	}

	var doc nonceDoc
	if err := json.Unmarshal(raw, &doc); err != nil {
		n.log.Warn("replay history is corrupt and has been discarded; starting empty",
			"path", path, "err", err, "bytes", len(raw))
		return n
	}
	for nonce, exp := range doc.Nonces {
		t, err := time.Parse(time.RFC3339, exp)
		if err != nil {
			n.log.Warn("replay history holds an unparseable expiry; that entry is discarded",
				"path", path, "err", err)
			continue
		}
		n.entries[nonce] = t
	}
	return n
}

// OpenNonceStoreForConfig is the wiring used by cmd/agent.
func OpenNonceStoreForConfig(c config.Config, log *slog.Logger) *NonceFile {
	return OpenNonceStore(c.NoncePath(), log)
}

type nonceDoc struct {
	Nonces map[string]string `json:"nonces"`
}

// SeenOrRecord reports whether this nonce must be treated as already seen, and
// records it otherwise. One operation, deliberately: see NonceStore.
func (n *NonceFile) SeenOrRecord(nonce string, expiresAt time.Time) bool {
	n.mu.Lock()
	defer n.mu.Unlock()

	if _, ok := n.entries[nonce]; ok {
		return true
	}
	if len(n.entries) >= n.cap {
		victim, soonest := "", time.Time{}
		for k, v := range n.entries {
			if victim == "" || v.Before(soonest) {
				victim, soonest = k, v
			}
		}
		if victim == "" || !soonest.Before(expiresAt) {
			// Nothing can be evicted without forgetting a nonce that stays
			// replayable for longer than this one. Fail closed.
			n.log.Warn("replay store is full of entries that outlive this job; refusing it",
				"cap", n.cap, "expires_at", expiresAt.Format(rfc3339))
			return true
		}
		delete(n.entries, victim)
	}
	n.entries[nonce] = expiresAt
	n.persistLocked()
	return false
}

// Prune drops entries whose window has closed and returns how many went.
//
// Safe because freshness already refuses a job past its expires_at: an expired
// nonce cannot be usefully replayed. Called from the scheduler's daily tick, so
// it costs nothing when idle.
func (n *NonceFile) Prune(now time.Time) int {
	n.mu.Lock()
	defer n.mu.Unlock()
	removed := 0
	for k, exp := range n.entries {
		if !exp.After(now) {
			delete(n.entries, k)
			removed++
		}
	}
	if removed > 0 {
		n.persistLocked()
	}
	return removed
}

// Len is the number of recorded nonces.
func (n *NonceFile) Len() int {
	n.mu.Lock()
	defer n.mu.Unlock()
	return len(n.entries)
}

// persistLocked writes the store atomically: temp file, fsync, rename.
//
// A partially-written store is worse than a missing one -- it loads as corrupt
// and takes the whole history with it -- so the rename is what makes the new
// contents visible, and it is the last step.
//
// A write failure is logged at ERROR and the in-memory record is KEPT. That is
// deliberate and it is the one place this file does not fail closed: a full or
// read-only disk would otherwise refuse every job on the endpoint, which is a
// self-inflicted outage, while the exposure is limited to replays that straddle
// a restart on a machine whose disk is already broken.
func (n *NonceFile) persistLocked() {
	doc := nonceDoc{Nonces: make(map[string]string, len(n.entries))}
	for k, v := range n.entries {
		doc.Nonces[k] = v.UTC().Format(time.RFC3339)
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		n.log.Error("replay store could not be encoded; it will not survive a restart", "err", err)
		return
	}
	if err := writeFileAtomic(n.path, raw); err != nil {
		n.log.Error("replay store could not be written; it will not survive a restart",
			"path", n.path, "err", err)
	}
}

// writeFileAtomic writes b to path via a temp file in the same directory.
// config.KeyMode-adjacent: 0600, because the data directory holds the device
// key and nothing in it is group- or world-readable (invariant #3).
func writeFileAtomic(path string, b []byte) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, filepath.Base(path)+".tmp-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer os.Remove(name) // no-op once the rename succeeds
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return err
	}
	if _, err := tmp.Write(b); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(name, path)
}
