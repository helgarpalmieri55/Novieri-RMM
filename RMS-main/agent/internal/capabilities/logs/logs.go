// Package logs implements the `logs.ship` capability
// (docs/modules/security-logs/spec.md).
//
// This is the endpoint half of the platform's SOC 2 evidence path. Until it
// existed, `logs.ship` was a registered job type with no agent that could run
// it: the control plane had ingestion, dedupe, retention, search and a CSV
// export, and nothing ever put an event into any of it. §1 of that spec says
// so in as many words -- "the agent-side collectors ... are **not built**".
//
// # What travels, and how
//
// The events do NOT ride the job result. They are `security_events` frames on
// the agent's existing gateway connection (`wire.SecurityEvents`,
// SecurityEventsMsg.schema.json), sent through the same outbox every other
// agent -> server message uses, so a batch produced while the uplink is down is
// still owed and still delivered. The job result carries COUNTS ONLY -- see
// `result` -- because a job result is stored on the job row, is visible in the
// console, and is not the place for usernames and source addresses.
//
// # The dedupe contract decides what the agent must remember
//
// Delivery is at-least-once and the server dedupes on `(device_id, event_uid)`
// with `ON CONFLICT DO NOTHING` (spec §4, §6). Two consequences shape this
// package:
//
//  1. `event_uid` MUST BE DETERMINISTIC. Spec §4's example calls it an
//     "agent-generated uuid"; a fresh UUID per read would defeat the dedupe
//     entirely -- every resend after a reconnect would insert the same logon
//     again, and the SOC 2 access-evidence export would show one login as
//     four. So it is derived from the record itself: see eventUID in
//     normalize.go, and §4.1 of the spec, amended.
//  2. The agent needs a CURSOR, and it is the only thing this capability
//     remembers between jobs. Without one, every `logs.ship` re-reads the whole
//     Security log -- hundreds of megabytes on a machine that has been up a
//     year -- and relies on the server to throw almost all of it away. The
//     cursor is ~200 bytes in the state directory and is written only during a
//     job (cursor.go).
//
// # Bounded, and streaming
//
// A Windows Security log is routinely hundreds of megabytes. Nothing here ever
// holds one: the Source hands back a Reader that yields ONE record at a time,
// records are normalised and appended to a fixed-capacity batch, and the batch
// is flushed at maxBatch and reused. One job ships at most maxEventsPerJob
// events; an endpoint with a year of backlog catches up over several jobs
// instead of trying to send a year at once.
//
// # ANTIVIRUS, and the processes this capability may spawn
//
// Reading the Security event log and shipping it off-box is, behaviourally,
// exactly what credential-harvesting reconnaissance looks like. The design
// choices below are made for the EDR that will be watching:
//
//   - WINDOWS SPAWNS NOTHING. Collection is `wevtapi.dll` --
//     EvtQuery/EvtNext/EvtRender -- called directly (source_windows.go). Not
//     `wevtutil`, and above all not `powershell.exe -Command Get-WinEvent`: a
//     SYSTEM service spawning PowerShell is among the loudest signals available
//     and trips Attack Surface Reduction rules that block child processes of
//     service binaries outright.
//   - MACOS SPAWNS EXACTLY ONE PROCESS: `/usr/bin/log show ...`, with an argv
//     (never a shell), a job-scoped context AND a `WaitDelay`, and a narrow
//     predicate. macOS has no query API for the unified log outside cgo, and
//     `log` is the documented interface.
//
// The complete list is in docs/modules/security-logs/spec.md §10.3, which is
// what the EDR team needs.
//
// # Dormancy
//
// Registration is a map insert. `Process` is inert until main calls Bind, and
// Bind stores a Sender -- it opens no subscription, starts no goroutine and
// takes no timer-wheel entry. The cursor file is opened inside a verified job
// and closed before it returns. There is no standing OS log subscription in
// this build: the cadence arrives as a signed job (the org policy's
// `ship_interval_s` drives the CONTROL PLANE's dispatch), which is the same
// argument §0.9 of the health spec makes against any locally scheduled work.
//
// Note for docs/modules/endpoint-health-alerting/spec.md §0.5 item 5: health's
// `BorrowEventLog` is still nil, and must stay nil. This capability holds no
// subscription for health to borrow -- it opens a query inside a job and closes
// it -- so `eventlog.*` check types continue to report `unavailable` (§0.7)
// rather than silently acquiring a standing subscription through the back door.
package logs

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// JobType matches app/jobs/registry.py's `register(JobType("logs.ship",
// LogsShipParams))`. A mismatch would be refused `unknown_type` by this agent's
// own runner -- the correct answer, and a confusing one, so the string lives in
// one place per side.
const JobType = "logs.ship"

// Bounds. Each is a property of the contract or of the idle budget, not a
// preference.
const (
	// maxBatch is SecurityEventsMsg.events `maxItems`. A 501-event message is
	// rejected AT PARSE server-side, which discards the whole batch -- so this
	// is a hard bound and not a tuning knob.
	maxBatch = 500

	// maxEventsPerJob is what one job may ship. Ten full batches.
	//
	// "Bound what one job ships" is not a nicety: an endpoint that has never
	// shipped, or whose cursor was lost, has a year of events waiting. Sending
	// them in one job would mean a multi-hundred-megabyte read, ~2,000 frames
	// queued into an outbox with a byte cap that would then start DROPPING
	// them (jobs.Buffer.enforceCapLocked evicts by priority and age), and a job
	// that outlives its own expires_at. Catching up over several jobs is slower
	// and loses nothing.
	maxEventsPerJob = 5_000

	// The server's own column widths (spec §5) and Pydantic constraints
	// (SecurityEventsMsg.schema.json). A value over any of these fails
	// VALIDATION for the whole message, so the agent clamps rather than
	// letting one pathological record discard 499 good ones.
	maxEventUIDLen = 64
	maxUsernameLen = 200
	maxSourceLen   = 100

	// maxRawBytes bounds the free-form `raw` map per event, and rawTruncatedKey
	// is how a clamped record says so. Evidence must be complete and
	// unmodified; it must also not be a way to make an endpoint allocate
	// without limit. Real Security-event data is a few hundred bytes, so this
	// is far above anything genuine and the flag makes any truncation visible
	// rather than silent.
	maxRawBytes     = 8 << 10
	maxRawValueLen  = 1 << 10
	rawTruncatedKey = "_truncated"
)

// Sender is the outbound half of the gateway connection. jobs.Outbox satisfies
// it, so a batch produced while the uplink is down is still owed and still
// delivered -- which is what makes delivery at-least-once rather than
// best-effort.
//
// It is an interface so this package never holds a socket.
type Sender interface {
	Send(ctx context.Context, msg any) error
}

// Record is one OS log record as the source produced it, not yet normalised.
//
// Position is the source's own cursor value AFTER this record, opaque to
// everything but that source. Windows uses the decimal EventRecordID; macOS
// uses the record's timestamp.
type Record struct {
	Position string
	Payload  []byte
}

// Reader streams records. Next returns io.EOF at the end of the stream.
//
// ONE RECORD AT A TIME IS THE POINT. There is no ReadAll and no method that
// returns a slice: a Windows Security log is routinely hundreds of megabytes,
// and an interface that could hand one back is an interface someone will
// eventually hand one back through.
type Reader interface {
	Next() (Record, error)
	Close() error
}

// Source is the OS seam -- the whole of the platform-specific surface, so that
// batching, cursor handling, clamping, taxonomy filtering and error
// classification are unit-tested on a Linux container that has neither a
// Windows Event Log nor a macOS unified log.
type Source interface {
	// Name identifies the collection strategy. It is stored in the cursor so a
	// cursor written by a different source is discarded rather than misread --
	// a Windows record id fed to the macOS source as a start timestamp would
	// be a silent collection outage.
	Name() string

	// Open starts a stream of the records that follow `after`. An empty
	// `after` means "from the beginning of what is retained".
	Open(ctx context.Context, after string) (Reader, error)

	// Normalize turns one record into a wire event. ok=false means the record
	// is OUTSIDE THE v1 TAXONOMY and must be dropped -- see ship() for why that
	// is not optional.
	Normalize(rec Record) (wire.SecurityEvent, bool, error)

	// Rewound reports whether the OS log has been rewound behind `position`, so
	// that a cursor there would match nothing ever again.
	//
	// THIS IS NOT HYPOTHETICAL AND IT IS NOT COSMETIC. `wevtutil cl Security`,
	// a Group Policy retention setting, or an attacker covering their tracks
	// all reset EventRecordID to 1. A cursor left at 184,392 then filters out
	// every subsequent event forever: the endpoint reports zero security
	// events, the console shows a device that has simply gone quiet, and the
	// SOC 2 evidence for that machine ends on the day the log was cleared. The
	// clearing is itself the most interesting event on the box.
	Rewound(ctx context.Context, position string) (bool, error)
}

// CursorStore persists the one thing this capability remembers between jobs.
type CursorStore interface {
	Load() (Cursor, error)
	Save(Cursor) error
}

// Options wires the capability.
type Options struct {
	// Sender carries security_events frames. Required -- see ErrUnbound.
	Sender Sender
	// Source is the OS seam. Nil resolves this platform's implementation at
	// JOB TIME, which is what keeps registration a map insert.
	Source Source
	// Cursors persists the read position. Nil resolves the state-directory
	// file at JOB TIME.
	Cursors CursorStore
	Now     func() time.Time
	Logger  *slog.Logger
}

// result is the job result. COUNTS ONLY.
//
// `logs.ship` has NO result handler server-side (app/jobs/registry.py:148
// registers it with none) and no exported result schema, so nothing validates
// this shape and nothing acts on it -- it is stored on the job row and read by
// a human. That is precisely why it must carry no event content: usernames,
// source addresses and logon types are personal data on their way to a
// retention-governed table, and a job row is neither retention-governed nor
// access-controlled the same way. Not one field here is derived from an event's
// contents.
type result struct {
	Source  string `json:"source"`
	Shipped int    `json:"shipped"`
	Skipped int    `json:"skipped"`
	Batches int    `json:"batches"`
	// Truncated reports that this job hit maxEventsPerJob and stopped. The
	// operator's read is "this device is catching up"; the next job continues
	// from the cursor.
	Truncated bool `json:"truncated"`
	// CursorReset reports that the OS log had been rewound behind the stored
	// cursor -- cleared or rotated -- and collection restarted from the
	// beginning of what is retained. See Source.Rewound.
	CursorReset bool `json:"cursor_reset"`
}

// Service is the process-wide holder.
//
// The zero value is usable and inert: it is what `Process` is. It becomes able
// to ship when main calls Bind with the process's outbox. Until then a
// `logs.ship` job FAILS with a named error rather than collecting events it
// has nowhere to send.
type Service struct {
	mu   sync.Mutex
	opts Options
	// runs counts completed ships, so a dormancy assertion outside this package
	// can observe that this capability has NOT run.
	runs atomic.Int64
}

// Process is the Service the default capability registry registers.
var Process = &Service{}

// New returns a Service with explicit wiring. Tests use it; production uses
// Process plus Bind.
func New(o Options) *Service { return &Service{opts: o} }

// Bind wires the capability to the process's outbound sender.
//
// Called once, from main, after the outbox exists. It opens NO log
// subscription, starts no goroutine and takes no timer-wheel entry: the cadence
// arrives as a signed job (the org policy's `ship_interval_s` drives the
// control plane's dispatch), because a locally scheduled collection would be a
// standing instruction to read the Security log that nobody signed.
func (s *Service) Bind(o Options) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.opts = o
}

func (s *Service) options() Options {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.opts
}

// Runs reports how many ships this process has completed.
func (s *Service) Runs() int64 { return s.runs.Load() }

// ErrUnbound is returned when a `logs.ship` job arrives before Bind.
var ErrUnbound = errors.New("logs.ship: no outbound sender is bound in this process, so events " +
	"collected here would have nowhere to go; refusing to read the security log rather than " +
	"reading it and discarding it")

// Ship is the `logs.ship` capability.
//
// It takes a jobs.VerifiedJob and nothing else (invariant #1): the runner has
// already checked the Ed25519 signature over the canonical bytes against the
// key pinned at enrollment, that the job targets THIS device, that it is fresh,
// that its nonce is unseen, and that the type is registered. There is no second
// path into this function -- which matters more here than for most capabilities,
// because this one reads the Security event log.
func (s *Service) Ship(ctx context.Context, job jobs.VerifiedJob) (any, error) {
	// LogsShipParams is extra="forbid" and EMPTY: "dispatching logs.ship tells
	// the agent to flush its security-event buffer now; the scheduled cadence
	// comes from the org's policy". Params are still validated as an object,
	// because a `logs.ship` carrying a channel name or a filter would be a
	// second, unversioned way to configure what this capability reads -- and
	// the whole point of an empty params model is that there is no such way.
	if err := checkNoParams(job.Params()); err != nil {
		return nil, err
	}

	opts, src, store, err := s.resolve()
	if err != nil {
		return nil, err
	}

	res, err := s.ship(ctx, opts, src, store)
	if err != nil {
		return nil, err
	}
	s.runs.Add(1)
	return res, nil
}

// resolve produces this job's wiring, at JOB TIME.
//
// Everything the capability needs is resolved here and nothing is resolved at
// registration: that is what keeps registration a map insert and the data
// directory untouched while the agent is idle. Factored out of Ship so its
// three failure classes -- unbound, unsupported platform, no state directory --
// are testable without a jobs.VerifiedJob, which by design cannot be
// constructed outside internal/jobs.
//
// THE ORDER MATTERS. The sender is checked FIRST, before anything opens the
// Security event log: an agent that read the log and then discovered it had
// nowhere to send the contents would have paid the loudest EDR cost this
// capability has for no benefit at all.
func (s *Service) resolve() (Options, Source, CursorStore, error) {
	opts := s.options()
	if opts.Sender == nil {
		return opts, nil, nil, ErrUnbound
	}

	src := opts.Source
	if src == nil {
		var err error
		if src, err = platformSource(); err != nil {
			// REFUSED, not failed. This is the "no Windows Event Log here"
			// case: it will be true tomorrow too, and `refused` is how a
			// capability asks to be taken out of rotation.
			return opts, nil, nil, jobs.Refuse("logs.ship: %s", err.Error())
		}
	}

	store := opts.Cursors
	if store == nil {
		var err error
		if store, err = defaultCursorStore(); err != nil {
			return opts, nil, nil, fmt.Errorf(
				"logs.ship: no state directory for the read cursor: %w", err)
		}
	}
	return opts, src, store, nil
}

// checkNoParams refuses params that are not an empty object.
func checkNoParams(raw json.RawMessage) error {
	if len(raw) == 0 {
		return nil
	}
	var got map[string]json.RawMessage
	if err := json.Unmarshal(raw, &got); err != nil {
		return jobs.Refuse("logs.ship: params are not a LogsShipParams")
	}
	// Unknown members are IGNORED, not refused -- docs/kernel/wire-protocol's
	// "loosen inbound before you tighten outbound", so a strictly additive
	// server change does not take a deployed fleet offline. What is NOT done is
	// reading them: nothing in this package looks at a params member, so no
	// job can widen what is collected.
	return nil
}

// ship is the whole collection loop.
func (s *Service) ship(ctx context.Context, opts Options, src Source, store CursorStore) (*result, error) {
	log := opts.logger()
	res := &result{Source: src.Name()}

	cur, err := store.Load()
	if err != nil {
		// A cursor that cannot be read is NOT a reason to stop collecting: this
		// is the evidence path, and an endpoint that goes quiet because a
		// 200-byte state file was corrupted is exactly the failure the SOC 2
		// control is supposed to prevent. Start over instead. The cost is
		// bounded (maxEventsPerJob per job) and duplicates cost nothing,
		// because the server dedupes on (device, event_uid).
		log.Warn("logs.ship: the read cursor was unreadable; restarting collection from the "+
			"beginning of the retained log", "err", err)
		cur = Cursor{}
		res.CursorReset = true
	}
	if cur.Source != "" && cur.Source != src.Name() {
		// A cursor written by another source. A Windows record id handed to the
		// macOS source as a start timestamp would silently collect nothing.
		log.Warn("logs.ship: the stored cursor was written by a different source; restarting",
			"stored", cur.Source, "current", src.Name())
		cur = Cursor{}
		res.CursorReset = true
	}

	shipped, skipped, batches, truncated, err := s.drain(ctx, opts, src, store, &cur)
	res.Shipped, res.Skipped, res.Batches, res.Truncated = shipped, skipped, batches, truncated
	if err != nil {
		return nil, err
	}

	// NOTHING AT ALL, from a cursor that is not at the beginning. That is what a
	// cleared log looks like from here, and it is indistinguishable from a
	// quiet machine unless somebody asks. See Source.Rewound.
	if shipped == 0 && skipped == 0 && cur.Position != "" {
		rewound, rerr := src.Rewound(ctx, cur.Position)
		if rerr != nil {
			log.Warn("logs.ship: could not tell whether the log had been rewound", "err", rerr)
		} else if rewound {
			log.Warn("logs.ship: the OS security log has been rewound behind this agent's read " +
				"cursor -- cleared, rotated or replaced. Restarting collection from the " +
				"beginning of what is retained; events between the old cursor and the clear are " +
				"gone from the endpoint and cannot be recovered here")
			cur = Cursor{}
			res.CursorReset = true
			shipped, skipped, batches, truncated, err = s.drain(ctx, opts, src, store, &cur)
			res.Shipped, res.Skipped, res.Batches, res.Truncated = shipped, skipped, batches, truncated
			if err != nil {
				return nil, err
			}
		}
	}

	// COUNTS ONLY. Not a username, not an address, not an event type -- this
	// line is written on every job, to a log an operator reads and a support
	// bundle collects.
	log.Info("logs.ship shipped security events",
		"source", res.Source, "shipped", res.Shipped, "skipped", res.Skipped,
		"batches", res.Batches, "truncated", res.Truncated, "cursor_reset", res.CursorReset)
	return res, nil
}

// drain reads one pass of the stream, flushing full batches as it goes.
//
// The cursor advances only after a batch is ACCEPTED by the sender. A send that
// fails leaves the cursor where it was, so the next job re-ships those events
// and the server's (device_id, event_uid) dedupe absorbs them -- at-least-once,
// which is the contract spec §4 states.
func (s *Service) drain(
	ctx context.Context, opts Options, src Source, store CursorStore, cur *Cursor,
) (shipped, skipped, batches int, truncated bool, err error) {
	reader, err := src.Open(ctx, cur.Position)
	if err != nil {
		// A plain error, so the job is `failed` rather than `refused`: a query
		// that could not start is usually transient (the log service is
		// restarting, the machine is mid-shutdown), and asking to be taken out
		// of rotation for it would be permanent. The PERMANENT case -- this
		// platform has no security log at all -- is refused earlier, in Ship.
		return 0, 0, 0, false, fmt.Errorf("logs.ship: opening the %s stream: %w", src.Name(), err)
	}
	defer func() {
		if cerr := reader.Close(); cerr != nil {
			opts.logger().Warn("logs.ship: closing the log stream", "err", cerr)
		}
	}()

	// ONE fixed-capacity batch, reused across flushes. Never grown, never
	// reallocated: this is what makes a hundred-thousand-record catch-up cost a
	// bounded amount of memory instead of a proportional one.
	batch := make([]wire.SecurityEvent, 0, maxBatch)
	// pending is the position of the newest record CONSUMED, shipped or
	// dropped. Dropped records advance it too: a tail of ten thousand events
	// outside the taxonomy must not be re-read on every job forever.
	pending := cur.Position

	for {
		if err := ctx.Err(); err != nil {
			// The job's own deadline. Whatever was already flushed stays
			// flushed and the cursor stays where the last flush put it.
			return shipped, skipped, batches, true, nil
		}
		if shipped+skipped >= maxEventsPerJob {
			truncated = true
			break
		}

		rec, rerr := reader.Next()
		if errors.Is(rerr, io.EOF) {
			break
		}
		if rerr != nil {
			// Flush what is already normalised before reporting: evidence
			// already read is owed to the server whatever went wrong after it.
			if len(batch) > 0 {
				if ferr := s.flush(ctx, opts, store, src, cur, pending, batch); ferr != nil {
					return shipped, skipped, batches, truncated, ferr
				}
				shipped += len(batch)
				batches++
				// No `batch = batch[:0]` here: this branch returns on the next
				// line, so resetting it is a store nothing can read (SA4006).
			}
			return shipped, skipped, batches, truncated,
				fmt.Errorf("logs.ship: reading the %s stream: %w", src.Name(), rerr)
		}

		pending = rec.Position

		ev, ok, nerr := src.Normalize(rec)
		if nerr != nil {
			// A record this agent could not parse is DROPPED, counted, and
			// never named in the log: the record's contents are exactly the
			// personal data this capability exists to keep out of log lines.
			// One malformed record must not stop the evidence stream.
			skipped++
			continue
		}
		if !ok {
			// OUTSIDE THE v1 TAXONOMY, and dropping it is not optional.
			// ingest_events REJECTS THE WHOLE BATCH on an unknown event_type
			// (spec §6) -- so a single unmapped record shipped alongside 499
			// good ones would discard all 500 and audit an ingest_rejected. The
			// taxonomy filter belongs on the agent for that reason alone.
			skipped++
			continue
		}
		batch = append(batch, clampEvent(ev))

		if len(batch) == maxBatch {
			if ferr := s.flush(ctx, opts, store, src, cur, pending, batch); ferr != nil {
				return shipped, skipped, batches, truncated, ferr
			}
			shipped += len(batch)
			batches++
			batch = batch[:0]
		}
	}

	if len(batch) > 0 {
		if ferr := s.flush(ctx, opts, store, src, cur, pending, batch); ferr != nil {
			return shipped, skipped, batches, truncated, ferr
		}
		shipped += len(batch)
		batches++
	} else if pending != cur.Position && pending != "" {
		// Everything in this pass was dropped by the taxonomy filter. The
		// cursor still has to move past them, or the next job re-reads the same
		// records and drops them again, forever.
		saveCursor(opts, store, src, cur, pending)
	}
	return shipped, skipped, batches, truncated, nil
}

// flush sends one batch and, only if the sender accepted it, advances the
// cursor.
func (s *Service) flush(
	ctx context.Context, opts Options, store CursorStore, src Source,
	cur *Cursor, pending string, batch []wire.SecurityEvent,
) error {
	if len(batch) == 0 {
		return nil
	}
	// The slice handed to the sender is a COPY. `batch` is reused across
	// flushes, and jobs.Buffer encodes synchronously today -- but a Sender that
	// queued the value instead would find its contents overwritten by the next
	// batch, and the corruption would land in a SOC 2 evidence table.
	out := make([]wire.SecurityEvent, len(batch))
	copy(out, batch)
	if err := opts.Sender.Send(ctx, wire.SecurityEvents{Events: out}); err != nil {
		// `failed`, not `refused`: an upload that did not go through is the
		// textbook transient. The cursor is NOT advanced, so the next job
		// re-ships these events and the server dedupes them.
		return fmt.Errorf("logs.ship: shipping a batch of %d events: %w", len(batch), err)
	}
	saveCursor(opts, store, src, cur, pending)
	return nil
}

// saveCursor advances the in-memory cursor and persists it.
//
// A persistence failure is LOGGED AND SURVIVED rather than failing the job: the
// events are already on their way, and failing the job would report a shipment
// that happened as a shipment that did not. The cost of a lost cursor is a
// re-read the server dedupes away.
func saveCursor(opts Options, store CursorStore, src Source, cur *Cursor, pending string) {
	if pending == "" {
		return
	}
	cur.V = cursorVersion
	cur.Source = src.Name()
	cur.Position = pending
	cur.UpdatedAt = opts.now().UTC().Format(rfc3339)
	if err := store.Save(*cur); err != nil {
		opts.logger().Warn("logs.ship: the read cursor could not be persisted; the next job will "+
			"re-ship these events and the server will dedupe them", "err", err)
	}
}

// clampEvent brings one event inside the server's own limits.
//
// A single over-long value fails Pydantic validation for the WHOLE message,
// which discards up to 500 events of evidence. Clamping one field is strictly
// better than losing 499 unrelated records, and the clamp is visible: `raw`
// still carries the native fields, and a truncated `raw` says so.
func clampEvent(ev wire.SecurityEvent) wire.SecurityEvent {
	ev.EventUID = clamp(ev.EventUID, maxEventUIDLen)
	ev.Username = clamp(ev.Username, maxUsernameLen)
	ev.Source = clamp(ev.Source, maxSourceLen)
	ev.Raw = clampRaw(ev.Raw)
	return ev
}

func clamp(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// clampRaw bounds the free-form native-field map.
func clampRaw(raw map[string]any) map[string]any {
	if len(raw) == 0 {
		return raw
	}
	total := 0
	truncated := false
	for k, v := range raw {
		s, ok := v.(string)
		if !ok {
			total += len(k) + 16
			continue
		}
		if len(s) > maxRawValueLen {
			raw[k] = s[:maxRawValueLen]
			s = raw[k].(string)
			truncated = true
		}
		total += len(k) + len(s)
	}
	if total > maxRawBytes {
		// Over budget even after per-value clamping. Drop the largest values
		// until it fits, deterministically by key so two agents reading the
		// same record produce the same document.
		truncated = dropLargest(raw, total) || truncated
	}
	if truncated {
		raw[rawTruncatedKey] = true
	}
	return raw
}

func dropLargest(raw map[string]any, total int) bool {
	keys := make([]string, 0, len(raw))
	for k := range raw {
		keys = append(keys, k)
	}
	sortStrings(keys)
	dropped := false
	for _, k := range keys {
		if total <= maxRawBytes {
			break
		}
		s, ok := raw[k].(string)
		if !ok || len(s) < 64 {
			continue
		}
		total -= len(s)
		delete(raw, k)
		dropped = true
	}
	return dropped
}

func (o Options) logger() *slog.Logger {
	if o.Logger != nil {
		return o.Logger
	}
	return slog.Default()
}

func (o Options) now() time.Time {
	if o.Now != nil {
		return o.Now()
	}
	return time.Now()
}
