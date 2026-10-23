package jobs

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
)

// ErrQueueFull is returned by Submit when a capability's queue is at depth.
// The caller turns it into a refusal: the server must record `refused` with a
// reason rather than watch the job time out.
var ErrQueueFull = errors.New("jobs: capability queue is full")

type DispatcherOptions struct {
	// BaseContext is the context every job runs under. It MUST be the process
	// context -- the one service.Run cancels on "stop" -- and MUST NOT be the
	// transport's connection context.
	//
	// This is the whole reason Submit does not take a context. It used to: the
	// read goroutine passed the ctx it was called with, which serve derives per
	// connection and cancels on return. That was harmless only while Handle
	// blocked for the job's whole duration, because then serve could not return
	// until the job was finished. It no longer blocks, so with a connection
	// context a network blip -- the ordinary event the reconnect backoff exists
	// for -- cancelled every running capability mid-execution and every queued
	// job before it started. A 40-minute patch.apply died to a ten-second
	// interruption, and a queued HEAVY job was dropped by exec1 below with no
	// job_result at all.
	//
	// A running job does not need the connection: results survive an outage in
	// the outbound Buffer and are flushed on reconnect, and the job's own
	// expires_at still bounds it (Runner.run). The connection-scoped unblocking
	// that IS wanted lives elsewhere and is unaffected -- chunkreply.Detach, on
	// OnDisconnect, fails pending reply waits because those replies genuinely
	// can no longer arrive.
	//
	// Required: NewDispatcher refuses a nil BaseContext rather than substituting
	// context.Background(). The seam is where the process context enters this
	// package, and a default would let a caller silently not have one.
	BaseContext context.Context
	// QueueDepth bounds each capability's queue.
	QueueDepth int
	// HeavyKeys names the capability keys that share HeavyBudget.
	HeavyKeys map[string]bool
	// HeavyBudget caps how many heavy capabilities execute at once.
	HeavyBudget int
	// Exec runs one verified job. Injected so this package's tests do not
	// need a signing key; the agent passes Runner.run.
	Exec func(context.Context, VerifiedJob, Handler)
	// Logger defaults to slog.Default().
	Logger *slog.Logger
}

// Dispatcher runs verified jobs off the transport's read goroutine.
//
// Concurrent ACROSS capabilities, strictly serial WITHIN one. That split is the
// whole design: job ordering only ever mattered per capability, and holding the
// read goroutine to get it cost the agent every inbound message that arrives
// while a job runs -- including the reply the job itself is waiting for.
type Dispatcher struct {
	base   context.Context
	mu     sync.Mutex
	queues map[string]chan work
	depth  int
	heavy  map[string]bool
	sem    chan struct{}
	exec   func(context.Context, VerifiedJob, Handler)
	log    *slog.Logger
}

type work struct {
	job VerifiedJob
	h   Handler
}

func NewDispatcher(o DispatcherOptions) (*Dispatcher, error) {
	if o.BaseContext == nil {
		return nil, fmt.Errorf("%w: no base context (a job whose lifetime is the connection's dies "+
			"on the next network blip)", ErrIncomplete)
	}
	if o.QueueDepth <= 0 {
		return nil, fmt.Errorf("%w: queue depth must be positive", ErrIncomplete)
	}
	if o.Exec == nil {
		return nil, fmt.Errorf("%w: no exec (a dispatcher that cannot run a job is not one)", ErrIncomplete)
	}
	d := &Dispatcher{
		base:   o.BaseContext,
		queues: make(map[string]chan work),
		depth:  o.QueueDepth,
		heavy:  o.HeavyKeys,
		exec:   o.Exec,
		log:    o.Logger,
	}
	if d.log == nil {
		d.log = slog.Default()
	}
	if o.HeavyBudget > 0 {
		d.sem = make(chan struct{}, o.HeavyBudget)
	}
	return d, nil
}

// Submit queues a job for its capability. It NEVER blocks: the caller is the
// transport's read goroutine, and blocking it is the defect this type exists
// to remove.
//
// It deliberately takes NO context. The only context a job may run under is
// BaseContext (see DispatcherOptions); the caller's would be the connection's,
// and a job cancelled by a reconnect is the bug this signature makes
// unwritable.
func (d *Dispatcher) Submit(job VerifiedJob, h Handler) error {
	key := capabilityKey(job.Type())

	d.mu.Lock()
	defer d.mu.Unlock()

	q, ok := d.queues[key]
	if !ok {
		q = make(chan work, d.depth)
		d.queues[key] = q
		go d.worker(key, q)
	}
	select {
	case q <- work{job: job, h: h}:
		return nil
	default:
		return ErrQueueFull
	}
}

// Pending reports queued (not running) jobs for a key. Test-facing.
func (d *Dispatcher) Pending(key string) int {
	d.mu.Lock()
	defer d.mu.Unlock()
	if q, ok := d.queues[key]; ok {
		return len(q)
	}
	return 0
}

// worker drains one capability's queue and exits when it is empty, so an idle
// agent runs no goroutine per capability.
//
// The exit decision is made UNDER THE SAME MUTEX Submit uses. Without that, a
// Submit that observes the queue in the map can hand work to a worker already
// past its last read, and the job is lost with no error anywhere.
func (d *Dispatcher) worker(key string, q chan work) {
	for {
		select {
		case w := <-q:
			d.exec1(w)
		default:
			d.mu.Lock()
			if len(q) > 0 {
				d.mu.Unlock()
				continue
			}
			delete(d.queues, key)
			d.mu.Unlock()
			return
		}
	}
}

func (d *Dispatcher) exec1(w work) {
	// A panic anywhere in the worker -- not just inside the capability, which
	// Runner.invoke already recovers -- would otherwise take the whole agent
	// process down, because this is no longer the read goroutine's stack.
	defer func() {
		if p := recover(); p != nil {
			d.log.Error("dispatcher worker panicked",
				"job_type", w.job.Type(), "panic", fmt.Sprint(p))
		}
	}()

	// Heavy capabilities share a budget. Light ones never touch the semaphore,
	// so a full heavy budget cannot delay inventory or health.
	if d.sem != nil && d.heavy[capabilityKey(w.job.Type())] {
		select {
		case d.sem <- struct{}{}:
			defer func() { <-d.sem }()
		case <-d.base.Done():
			// The PROCESS is stopping while this job waited for a slot -- not
			// merely the connection, which no longer reaches here. Dropping it
			// is what is left: the control plane expires the job itself.
			//
			// Stated plainly because it is a real gap rather than a clean exit:
			// this path sends NO job_result, so the operator sees an expiry with
			// no cause. It is bounded to shutdown now, where a result would have
			// nowhere to go anyway; before BaseContext existed it fired on every
			// reconnect, which is what made it a defect.
			d.log.Warn("heavy job abandoned before it started",
				"job_type", w.job.Type(), "err", d.base.Err())
			return
		}
	}

	d.exec(d.base, w.job, w.h)
}
