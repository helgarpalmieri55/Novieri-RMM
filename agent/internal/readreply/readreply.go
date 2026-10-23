// Package readreply correlates one inbound backup_read_grant to the fetch that
// asked for it.
//
// # Why this is a duplicate of chunkreply and not a generalisation of it
//
// The two registries are the same shape over different payloads. Merging them
// behind a type parameter would put backup.run and backup.restore on one piece
// of code, so a regression in the shared half would break the capability that
// TAKES backups, not merely the one that returns them. The write path is
// measured and proven; this one is new. The convention is stated at
// internal/capabilities/netsweep/netsweep_test.go:1240.
//
// # Why silence is the hard part
//
// The control plane answers an unauthorized read query with NOTHING, for the
// reason BackupChunkGrantMsg states: a distinguishable error would be an oracle
// a compromised endpoint could walk. So a wait here MUST be bounded, or a
// restore whose job the server refuses hangs forever instead of failing where
// an operator can see it.
//
// # The idle budget
//
// A registry at rest is an empty map and a mutex. No goroutine, no timer, no
// socket, no allocation until a query is in flight.
package readreply

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// DefaultDeadline bounds one exchange. Generous for the server-side work of
// presigning a batch of at most 256 reads (backupstore's readBatchMax), short
// enough that a refused job fails visibly rather than looking hung.
const DefaultDeadline = 60 * time.Second

// maxPending caps concurrent in-flight queries. A restore fetches serially, so
// the realistic count is 1; the cap turns a bug that spawned unbounded batches
// into a named failure rather than quiet memory growth.
const maxPending = 16

var (
	ErrNoConnection   = errors.New("readreply: no gateway connection")
	ErrDisconnected   = errors.New("readreply: gateway connection lost while awaiting a grant")
	ErrTimeout        = errors.New("readreply: no grant returned before the deadline")
	ErrTooManyPending = errors.New("readreply: too many queries in flight")
	ErrDuplicateQuery = errors.New("readreply: query_id is already in flight")
)

// Sender is the write half of the agent's authenticated connection.
// *transport.Conn satisfies it. Deliberately narrow: this package can send a
// message and can do nothing else with the socket.
type Sender interface {
	Send(ctx context.Context, msg any) error
}

// key is (job_id, query_id). query_id alone is not unique: backupstore mints it
// as a per-store counter that restarts at "r1" for every restore, so a late grant
// from a finished restore would otherwise match a new restore's first batch.
type key struct{ job, query string }

type Registry struct {
	// Deadline bounds one exchange. Zero means DefaultDeadline. Set before use
	// and not intended to change while queries are in flight -- NOT read
	// under the lock (Query reads it after releasing r.mu, at the point it
	// starts the timer), so that "not intended to change" is a usage
	// convention this field relies on the caller to honour, not something
	// the mutex enforces.
	Deadline time.Duration

	log *slog.Logger

	mu      sync.Mutex
	conn    Sender
	pending map[key]chan *wire.ReadGrant
}

func New(log *slog.Logger) *Registry {
	if log == nil {
		log = slog.Default()
	}
	return &Registry{log: log, pending: map[key]chan *wire.ReadGrant{}}
}

// Attach installs the live connection. Called from transport's OnConnected.
func (r *Registry) Attach(s Sender) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.conn = s
}

// Detach drops the connection and fails every pending wait.
//
// Immediately, rather than letting each burn its deadline: a grant is answered
// on the connection the query went out on, so a reply cannot arrive after this.
func (r *Registry) Detach(cause error) {
	r.mu.Lock()
	waiters := make([]chan *wire.ReadGrant, 0, len(r.pending))
	for k, ch := range r.pending {
		waiters = append(waiters, ch)
		delete(r.pending, k)
	}
	r.conn = nil
	r.mu.Unlock()

	for _, ch := range waiters {
		close(ch)
	}
	if len(waiters) > 0 {
		r.log.Warn("gateway connection lost with backup read queries in flight",
			"pending", len(waiters), "cause", cause)
	}
}

// Deliver routes an inbound grant. It reports whether anything was waiting.
//
// An unmatched grant is dropped rather than treated as an error: it is a late
// reply to a query that already timed out, which is expected rather than
// exceptional.
func (r *Registry) Deliver(g *wire.ReadGrant) bool {
	if g == nil {
		return false
	}
	k := key{job: g.JobID, query: g.QueryID}
	r.mu.Lock()
	ch, ok := r.pending[k]
	if ok {
		delete(r.pending, k)
	}
	r.mu.Unlock()
	if !ok {
		// Neither id is logged: query_id is agent-minted and job_id is already
		// in the restore's own logging. Logging a whole grant would log a bearer
		// credential.
		r.log.Debug("dropped a backup read grant that nothing was waiting for")
		return false
	}
	ch <- g
	return true
}

// Pending reports how many queries are in flight. For tests and diagnostics.
func (r *Registry) Pending() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.pending)
}

// Query runs one exchange. It satisfies backupstore.ReadGranter.
func (r *Registry) Query(ctx context.Context, q wire.ReadQuery) (wire.ReadGrant, error) {
	k := key{job: q.JobID, query: q.QueryID}

	r.mu.Lock()
	conn := r.conn
	if conn == nil {
		r.mu.Unlock()
		return wire.ReadGrant{}, ErrNoConnection
	}
	if len(r.pending) >= maxPending {
		r.mu.Unlock()
		return wire.ReadGrant{}, ErrTooManyPending
	}
	if _, dup := r.pending[k]; dup {
		r.mu.Unlock()
		return wire.ReadGrant{}, fmt.Errorf("%w: %q", ErrDuplicateQuery, q.QueryID)
	}
	// Buffered: Deliver must never block on a waiter that has given up.
	ch := make(chan *wire.ReadGrant, 1)
	r.pending[k] = ch
	r.mu.Unlock()

	if err := conn.Send(ctx, q); err != nil {
		r.forget(k)
		return wire.ReadGrant{}, fmt.Errorf("readreply: sending the query: %w", err)
	}

	deadline := r.Deadline
	if deadline <= 0 {
		deadline = DefaultDeadline
	}
	timer := time.NewTimer(deadline)
	defer timer.Stop()

	select {
	case g, ok := <-ch:
		if !ok {
			return wire.ReadGrant{}, ErrDisconnected
		}
		return *g, nil
	case <-ctx.Done():
		r.forget(k)
		return wire.ReadGrant{}, ctx.Err()
	case <-timer.C:
		r.forget(k)
		// Named explicitly, because the most likely cause is not a slow server:
		// it is the control plane refusing this query in silence.
		return wire.ReadGrant{}, fmt.Errorf(
			"%w after %s. The control plane answers an unauthorized read query with "+
				"silence rather than an error, so this is what a refused restore looks like",
			ErrTimeout, deadline)
	}
}

func (r *Registry) forget(k key) {
	r.mu.Lock()
	delete(r.pending, k)
	r.mu.Unlock()
}
