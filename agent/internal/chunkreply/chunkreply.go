// Package chunkreply correlates one inbound backup_chunk_grant to the flush
// that asked for it.
//
// # Why this exists at all
//
// The agent's transport is one-way: transport.Run reads frames and hands each
// to a handler. The backup dedup exchange (docs/modules/backup/spec.md §5.2)
// is request/response — a flush sends a query and must block until the grant
// bearing ITS ids comes back — and nothing else in this agent correlates a
// reply to a request.
//
// # Why silence is the hard part
//
// The control plane answers an unauthorized query with NOTHING (protocol.py:259):
// unknown job, another device's job, a job that is not a backup.run, an expired
// job, a finished run. A distinguishable error would be an oracle a compromised
// endpoint could walk. So a wait here MUST be bounded, or an endpoint whose job
// the server refuses hangs forever instead of failing a run an operator can see.
//
// # The idle budget
//
// A registry at rest is an empty map and a mutex. No goroutine, no timer, no
// socket, no allocation until a query is in flight.
package chunkreply

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// DefaultDeadline bounds one exchange. Generous for a server-side index lookup
// over at most 1024 ids, short enough that a refused job fails visibly rather
// than looking hung.
const DefaultDeadline = 60 * time.Second

// maxPending caps concurrent in-flight queries. One run flushes serially, so
// the realistic count is 1; the cap turns a bug that spawned unbounded flushes
// into an immediate, named failure rather than quiet memory growth on someone's
// desktop.
const maxPending = 16

var (
	ErrNoConnection   = errors.New("chunkreply: no gateway connection")
	ErrDisconnected   = errors.New("chunkreply: gateway connection lost while awaiting a grant")
	ErrTimeout        = errors.New("chunkreply: no grant returned before the deadline")
	ErrTooManyPending = errors.New("chunkreply: too many queries in flight")
	ErrDuplicateQuery = errors.New("chunkreply: query_id is already in flight")
)

// Sender is the write half of the agent's authenticated connection.
// *transport.Conn satisfies it. Deliberately narrow: this package can send a
// message and can do nothing else with the socket.
type Sender interface {
	Send(ctx context.Context, msg any) error
}

// key is (job_id, query_id). query_id alone is not unique: dedup.go mints it as
// a per-session counter that restarts at "q1" for every run, so a late grant
// from a dead run would otherwise match a new run's first flush.
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
	pending map[key]chan *wire.ChunkGrant
}

func New(log *slog.Logger) *Registry {
	if log == nil {
		log = slog.Default()
	}
	return &Registry{log: log, pending: map[key]chan *wire.ChunkGrant{}}
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
	waiters := make([]chan *wire.ChunkGrant, 0, len(r.pending))
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
		r.log.Warn("gateway connection lost with backup dedup queries in flight",
			"pending", len(waiters), "cause", cause)
	}
}

// Deliver routes an inbound grant. It reports whether anything was waiting.
//
// An unmatched grant is dropped rather than treated as an error: it is a late
// reply to a query that already timed out, which is expected rather than
// exceptional.
func (r *Registry) Deliver(g *wire.ChunkGrant) bool {
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
		// in the run's own logging. Logging a whole grant would log a bearer
		// credential.
		r.log.Debug("dropped a backup chunk grant that nothing was waiting for")
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

// Query runs one exchange. It satisfies backup.QueryChunks.
func (r *Registry) Query(ctx context.Context, q wire.ChunkQuery) (wire.ChunkGrant, error) {
	k := key{job: q.JobID, query: q.QueryID}

	r.mu.Lock()
	conn := r.conn
	if conn == nil {
		r.mu.Unlock()
		return wire.ChunkGrant{}, ErrNoConnection
	}
	if len(r.pending) >= maxPending {
		r.mu.Unlock()
		return wire.ChunkGrant{}, ErrTooManyPending
	}
	if _, dup := r.pending[k]; dup {
		r.mu.Unlock()
		return wire.ChunkGrant{}, fmt.Errorf("%w: %q", ErrDuplicateQuery, q.QueryID)
	}
	// Buffered: Deliver must never block on a waiter that has given up.
	ch := make(chan *wire.ChunkGrant, 1)
	r.pending[k] = ch
	r.mu.Unlock()

	if err := conn.Send(ctx, q); err != nil {
		r.forget(k)
		return wire.ChunkGrant{}, fmt.Errorf("chunkreply: sending the query: %w", err)
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
			return wire.ChunkGrant{}, ErrDisconnected
		}
		return *g, nil
	case <-ctx.Done():
		r.forget(k)
		return wire.ChunkGrant{}, ctx.Err()
	case <-timer.C:
		r.forget(k)
		// Named explicitly, because the most likely cause is not a slow server:
		// it is the control plane refusing this query in silence.
		return wire.ChunkGrant{}, fmt.Errorf(
			"%w after %s. The control plane answers an unauthorized dedup query with "+
				"silence rather than an error, so this is what a refused job looks like",
			ErrTimeout, deadline)
	}
}

func (r *Registry) forget(k key) {
	r.mu.Lock()
	delete(r.pending, k)
	r.mu.Unlock()
}
