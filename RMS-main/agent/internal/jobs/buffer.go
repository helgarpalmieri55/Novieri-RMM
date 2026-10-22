package jobs

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"sync"

	"github.com/toeshee/it-suite/agent/internal/config"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// Buffer is the bounded, persisted outbound queue an agent uses while it is
// disconnected.
//
// # Why this exists
//
// An endpoint does its work whether or not the gateway is reachable. A laptop
// that runs a signed job on a train and then loses its uplink still owes the
// control plane an answer, and a job with no answer sits until it expires and
// tells the operator nothing about what happened on the device.
//
// # Bounded by BYTES, and dropping by priority
//
// A queue bounded by count still fills a disk if the entries are large; a queue
// bounded by nothing is a disk-exhaustion primitive reachable from a chatty
// capability. When the cap is exceeded the LOWEST-PRIORITY entries go first,
// oldest within a priority. A job_result is the answer to a signed job; a
// job_log is commentary about it. Dropping the answer to keep the commentary
// would be exactly backwards, and a plain oldest-first rule does that whenever
// the answer was produced before the chatter.
//
// # At-least-once, and why that is safe
//
// An entry is removed only after SendFrame returns nil, so a crash between the
// send and the rewrite re-sends it. VERIFIED against app/jobs/engine.py rather
// than recalled: _ALLOWED[JobStatus.succeeded] is the empty set (engine.py:50),
// so a duplicate job_result raises InvalidTransition in _transition
// (engine.py:119-120), and gateway/main.py:212 wraps handle_agent_message in
// `except Exception` which logs and keeps the connection. health_report and
// security_events are documented at-least-once and deduped server-side. A
// duplicate therefore costs a log line on the server, and the alternative --
// at-most-once -- costs an answer.
//
// # Idle cost
//
// Zero. No goroutine, no timer. Enqueue appends one line (O(1)); the file is
// rewritten only when entries are dropped or flushed.
type Buffer struct {
	path     string
	maxBytes int64
	log      *slog.Logger

	// flushMu serializes Flush ENTIRELY -- snapshot, send, and removal -- and
	// is always taken before mu.
	//
	// Two callers in Flush at once is not exotic on this agent: every
	// capability worker sends through the same Outbox, every job sends at
	// least twice (`running`, then its result), and Outbox.Send flushes on
	// every one. Before the Dispatcher existed, Send was effectively
	// single-threaded off the read goroutine and this could not happen; it is
	// now the default configuration.
	//
	// It costs nothing. The socket write behind SendFrame is already
	// serialized on transport.Conn.out, so concurrent flushes never gained
	// throughput -- they only interleaved two callers' frames on the wire,
	// which contradicts the FIFO ordering Outbox's doc comment promises ("a
	// job's answer never overtakes the answer before it").
	//
	// It is NOT the whole fix: removal is by identity below, because
	// enforceCapLocked (called from Enqueue, which holds only mu) can drop an
	// entry from the MIDDLE while a flush is in flight, and a positional
	// removal then deletes something that was never sent.
	flushMu sync.Mutex

	mu      sync.Mutex
	entries []bufEntry
	bytes   int64
	// nextSeq numbers entries so a flush can remove exactly what it sent.
	nextSeq uint64
	// flushed counts frames handed to a connection, for tests that need to
	// distinguish "sent" from "enqueued".
	flushed int
}

// DefaultBufferBytes is 8 MiB: room for thousands of results and reports on a
// multi-day outage, and small enough that a thousand agents cannot fill a
// modest disk between them.
const DefaultBufferBytes int64 = 8 << 20

// ErrNotBuffered is returned by Enqueue for a message kind that is deliberately
// not persisted. It is not a failure: the caller drops the message.
var ErrNotBuffered = errors.New("jobs: this message kind is not buffered")

// Priorities. Higher survives longer.
const (
	prioCommentary = 0 // job_log -- narration about a job
	prioRoutine    = 1 // job_status, health_report, security_events
	prioAnswer     = 2 // job_result -- the answer to a signed job
)

type bufEntry struct {
	Priority int             `json:"p"`
	Frame    json.RawMessage `json:"f"`

	// seq identifies this entry within this process, so Flush removes what it
	// actually sent rather than the first N entries by position. It is
	// deliberately NOT persisted: it means nothing across a restart, and the
	// spool's own order is what survives.
	seq uint64
}

// OpenBuffer loads the buffer, or starts an empty one.
//
// Like the nonce store it never fails: an agent that will not start because of
// a bad spool file is an endpoint that never comes back. A line that does not
// parse -- the shape a crash mid-append leaves -- is discarded with a WARN.
func OpenBuffer(path string, maxBytes int64, log *slog.Logger) *Buffer {
	if log == nil {
		log = slog.Default()
	}
	if maxBytes <= 0 {
		maxBytes = DefaultBufferBytes
	}
	b := &Buffer{path: path, maxBytes: maxBytes, log: log}

	f, err := os.Open(path)
	if err != nil {
		if !os.IsNotExist(err) {
			b.log.Warn("outbound buffer could not be read; starting empty", "path", path, "err", err)
		}
		return b
	}
	defer f.Close()

	sc := bufio.NewScanner(f)
	// One entry can legitimately be large (an inventory document); the default
	// 64KiB scanner limit would truncate it into a "corrupt" line.
	sc.Buffer(make([]byte, 0, 64<<10), int(maxBytes))
	discarded := 0
	for sc.Scan() {
		line := bytes.TrimSpace(sc.Bytes())
		if len(line) == 0 {
			continue
		}
		var e bufEntry
		if err := json.Unmarshal(line, &e); err != nil || len(e.Frame) == 0 {
			discarded++
			continue
		}
		e.seq = b.nextSeq
		b.nextSeq++
		b.entries = append(b.entries, e)
		b.bytes += int64(len(e.Frame))
	}
	if err := sc.Err(); err != nil {
		discarded++
	}
	if discarded > 0 {
		b.log.Warn("outbound buffer held unreadable lines; they were discarded",
			"path", path, "lines", discarded)
	}
	b.enforceCapLocked()
	return b
}

// OpenBufferForConfig is the wiring used by cmd/agent.
func OpenBufferForConfig(c config.Config, log *slog.Logger) *Buffer {
	return OpenBuffer(c.BufferPath(), DefaultBufferBytes, log)
}

// Enqueue frames msg and appends it.
//
// Framing goes through wire.Encode, which refuses anything that is not one of
// the seven declared agent -> server kinds, so nothing can be persisted that the
// server would drop at parse_agent_message -- an entry like that could never be
// flushed and would occupy the bound forever.
func (b *Buffer) Enqueue(msg any) error {
	prio, ok := priorityOf(msg)
	if !ok {
		return fmt.Errorf("%w: %T", ErrNotBuffered, msg)
	}
	frame, err := wire.Encode(msg)
	if err != nil {
		return err
	}

	b.mu.Lock()
	defer b.mu.Unlock()
	e := bufEntry{Priority: prio, Frame: frame, seq: b.nextSeq}
	b.nextSeq++
	b.entries = append(b.entries, e)
	b.bytes += int64(len(frame))

	if dropped := b.enforceCapLocked(); dropped > 0 {
		b.rewriteLocked()
		return nil
	}
	b.appendLocked(e)
	return nil
}

// priorityOf maps a message to its survival class. A kind that is not listed is
// not buffered, and Heartbeat is the deliberate absence: it means "I am here
// NOW", the server records its OWN clock when it arrives (_touch_agent), and a
// heartbeat replayed after a reconnect asserts a liveness the reconnect itself
// already asserted. Spending a bounded budget on the one message with no value
// when late would be a mistake.
func priorityOf(msg any) (int, bool) {
	switch msg.(type) {
	case wire.JobResult:
		return prioAnswer, true
	case wire.JobStatus, wire.HealthReport, wire.SecurityEvents:
		return prioRoutine, true
	case wire.JobLog:
		return prioCommentary, true
	default:
		return 0, false
	}
}

// enforceCapLocked drops entries until the byte budget holds, lowest priority
// first and oldest within a priority. Returns how many went.
func (b *Buffer) enforceCapLocked() int {
	dropped := 0
	for b.bytes > b.maxBytes && len(b.entries) > 0 {
		// Find the oldest entry of the lowest priority present.
		victim, lowest := -1, prioAnswer+1
		for i, e := range b.entries {
			if e.Priority < lowest {
				victim, lowest = i, e.Priority
			}
		}
		if victim < 0 {
			break
		}
		b.bytes -= int64(len(b.entries[victim].Frame))
		b.log.Warn("outbound buffer full; frame dropped",
			"priority", b.entries[victim].Priority, "bytes", len(b.entries[victim].Frame),
			"cap_bytes", b.maxBytes)
		b.entries = append(b.entries[:victim], b.entries[victim+1:]...)
		dropped++
	}
	return dropped
}

// Flush sends every entry in order, removing each only after the connection has
// accepted it. It stops at the first failure and keeps the remainder.
//
// # Why the whole function is under flushMu, and why removal is by identity
//
// It used to do neither, and lost entries under concurrency -- silently, and
// preferentially the ones this type exists to protect. Two shapes:
//
//	Concurrent flushes, positional removal:
//	  F1 snapshots [A]. F2 snapshots [A, B].
//	  F2 sends both, relocks, removes 2 -> entries = [].
//	  A worker enqueues C (a job_result) -> entries = [C].
//	  F1 relocks with sent=1, removes entries[:1] -> C is gone, unsent,
//	  and counted in `flushed`.
//
//	Enqueue racing one flush, positional removal:
//	  entries = [A(log), B(result)]. A flush snapshots both and sends A.
//	  Enqueue(C) pushes past maxBytes; enforceCapLocked drops A, the lowest
//	  priority -> entries = [B, C].
//	  The flush relocks with sent=2 and removes entries[:2] -> C is gone.
//
// flushMu closes the first and gives the wire back its FIFO ordering; removing
// by seq closes the second, which flushMu cannot reach because Enqueue holds
// only mu. Duplicate TRANSMISSION is fine here (the package comment documents
// at-least-once, and the server rejects a duplicate job_result with one log
// line); losing an answer is not.
func (b *Buffer) Flush(ctx context.Context, conn FrameSender) error {
	b.flushMu.Lock()
	defer b.flushMu.Unlock()

	b.mu.Lock()
	pending := make([]bufEntry, len(b.entries))
	copy(pending, b.entries)
	b.mu.Unlock()

	sent := 0
	var sendErr error
	for _, e := range pending {
		if err := ctx.Err(); err != nil {
			sendErr = err
			break
		}
		if err := conn.SendFrame(ctx, e.Frame); err != nil {
			sendErr = err
			break
		}
		sent++
	}

	if sent > 0 {
		b.mu.Lock()
		delivered := make(map[uint64]struct{}, sent)
		for _, e := range pending[:sent] {
			delivered[e.seq] = struct{}{}
		}
		kept := b.entries[:0]
		removed := 0
		for _, e := range b.entries {
			if _, ok := delivered[e.seq]; ok {
				b.bytes -= int64(len(e.Frame))
				removed++
				continue
			}
			kept = append(kept, e)
		}
		b.entries = kept
		b.flushed += removed
		b.rewriteLocked()
		b.mu.Unlock()
	}
	return sendErr
}

// Len is the number of buffered entries.
func (b *Buffer) Len() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.entries)
}

// Bytes is the buffered payload size.
func (b *Buffer) Bytes() int64 {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.bytes
}

func (b *Buffer) appendLocked(e bufEntry) {
	line, err := json.Marshal(e)
	if err != nil {
		b.log.Error("outbound entry could not be encoded", "err", err)
		return
	}
	f, err := os.OpenFile(b.path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		b.log.Error("outbound buffer could not be appended to; it will not survive a restart",
			"path", b.path, "err", err)
		return
	}
	defer f.Close()
	if _, err := f.Write(append(line, '\n')); err != nil {
		b.log.Error("outbound buffer append failed", "path", b.path, "err", err)
	}
}

// rewriteLocked replaces the spool with exactly what is in memory. Atomic, so a
// crash mid-rewrite leaves the previous spool rather than half of a new one --
// re-sending is safe (see the package comment); losing the file is not.
func (b *Buffer) rewriteLocked() {
	var out bytes.Buffer
	for _, e := range b.entries {
		line, err := json.Marshal(e)
		if err != nil {
			b.log.Error("outbound entry could not be encoded during rewrite", "err", err)
			continue
		}
		out.Write(line)
		out.WriteByte('\n')
	}
	if err := writeFileAtomic(b.path, out.Bytes()); err != nil {
		b.log.Error("outbound buffer could not be rewritten", "path", b.path, "err", err)
	}
}

// ---------------------------------------------------------------------------
// Outbox
// ---------------------------------------------------------------------------

// FrameSender carries an already-framed message. *transport.Conn satisfies it.
//
// The buffer stores FRAMES, not message values: a frame that was validated by
// wire.Encode at enqueue time cannot become invalid while it waits, and
// re-encoding a decoded message would be a second chance to change bytes.
type FrameSender interface {
	SendFrame(ctx context.Context, frame []byte) error
}

// Outbox is what the runner sends through. It satisfies Sender.
//
// Everything goes through the buffer first, so FIFO holds unconditionally:
// sending a new message directly while older ones wait would let a job's answer
// overtake the answer before it. The cost is one append per outbound message,
// and outbound messages are per-job -- not per-tick.
type Outbox struct {
	buf *Buffer
	log *slog.Logger

	mu   sync.Mutex
	conn FrameSender
}

// NewOutbox wraps a buffer.
func NewOutbox(buf *Buffer, log *slog.Logger) *Outbox {
	if log == nil {
		log = slog.Default()
	}
	return &Outbox{buf: buf, log: log}
}

// Send buffers msg and, if connected, tries to drain.
//
// It returns nil when the message was buffered but could not be transmitted:
// the runner's job is to produce an answer, not to own the connection, and a
// send failure here is recoverable by definition. A message kind that is not
// buffered (Heartbeat) is sent directly or dropped.
func (o *Outbox) Send(ctx context.Context, msg any) error {
	if err := o.buf.Enqueue(msg); err != nil {
		if !errors.Is(err, ErrNotBuffered) {
			return err
		}
		return o.sendDirect(ctx, msg)
	}
	if conn := o.current(); conn != nil {
		if err := o.buf.Flush(ctx, conn); err != nil {
			o.log.Debug("outbound flush failed; frames retained", "err", err)
		}
	}
	return nil
}

// sendDirect frames and writes without buffering. Used only for kinds that are
// worthless when late.
func (o *Outbox) sendDirect(ctx context.Context, msg any) error {
	conn := o.current()
	if conn == nil {
		return nil
	}
	frame, err := wire.Encode(msg)
	if err != nil {
		return err
	}
	if err := conn.SendFrame(ctx, frame); err != nil {
		o.log.Debug("unbuffered message dropped", "err", err, "msg", fmt.Sprintf("%T", msg))
	}
	return nil
}

// Attach installs a live connection and flushes what is waiting, in order,
// BEFORE anything new is produced against it.
func (o *Outbox) Attach(ctx context.Context, conn FrameSender) error {
	o.mu.Lock()
	o.conn = conn
	o.mu.Unlock()
	return o.buf.Flush(ctx, conn)
}

// Detach forgets the connection. Subsequent sends buffer.
func (o *Outbox) Detach() {
	o.mu.Lock()
	o.conn = nil
	o.mu.Unlock()
}

func (o *Outbox) current() FrameSender {
	o.mu.Lock()
	defer o.mu.Unlock()
	return o.conn
}
