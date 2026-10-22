//go:build windows

package logs

import (
	"context"
	"fmt"
	"io"
	"strconv"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// THE WINDOWS SECURITY EVENT LOG, READ THROUGH wevtapi.dll AND NOTHING ELSE.
//
// # NEVER EXECUTED IN THIS REPOSITORY
//
// Every syscall below is compiled by the `GOOS=windows go vet` step and by
// cross-compilation, and has run nowhere. The logic that consumes what it
// produces -- the XML parse, the taxonomy filter, the username assembly, the
// timestamp normalisation, the cursor arithmetic -- is in winevent.go and is
// fully unit-tested on Linux against recorded documents. The retrieval is
// unverified until it runs on a real endpoint.
//
// # Why not a shell-out (the antivirus constraint)
//
// Reading the Security log and shipping it off-box is behaviourally identical
// to credential-harvesting reconnaissance, and this agent runs as SYSTEM. The
// three ways to read the log, worst first:
//
//  1. `powershell.exe -Command Get-WinEvent ...` -- a SYSTEM service spawning
//     PowerShell is among the loudest signals an EDR has. It trips Attack
//     Surface Reduction rule d1e49aac (block process creations from PSExec and
//     WMI commands) and b2b3f03d-style child-process rules outright on many
//     fleets, and it is the exact pattern behind most "malicious PowerShell"
//     detections. It also costs ~60 MB of resident memory per invocation, which
//     alone puts it outside the idle budget.
//  2. `wevtutil qe Security /f:xml ...` -- far better: a signed Microsoft
//     binary that does one thing. Still a process creation per job, still
//     visible in every process-creation feed, and (this is the one that bites)
//     `exec.CommandContext` DOES NOT BOUND IT: Wait blocks until the stdout
//     pipe closes, and a wevtutil enumerating a 400 MB log holds it. Measured
//     in this module on the equivalent case: `sh -c "sleep 30"` under a 200 ms
//     deadline took 30.003 s until `cmd.WaitDelay` was set
//     (internal/capabilities/inventory/exec.go).
//  3. EvtQuery/EvtNext/EvtRender -- the API `wevtutil` and `Get-WinEvent` are
//     both built on. No process creation, no PowerShell, a handle that is
//     closed in a defer, and a query the OS evaluates so that records outside
//     the taxonomy never cross into this process at all.
//
// This file is (3). THIS CAPABILITY SPAWNS NO PROCESS ON WINDOWS.
//
// # The query does the filtering
//
// The XPath below is evaluated by the Event Log service, so 4688s and 4663s --
// the high-volume ids this agent does not collect -- are never rendered, never
// copied, and never allocated here. That is a real footprint decision: on a
// busy server the ids in the taxonomy are a small fraction of the channel.

const (
	// EvtQuery flags (winevt.h).
	evtQueryChannelPath      = 0x1
	evtQueryForwardDirection = 0x100
	evtQueryReverseDirection = 0x200

	// EvtRender flags.
	evtRenderEventXml = 1

	// evtNextBlock is how many event handles are fetched per EvtNext call.
	// Sixty-four handles is a few kilobytes of pointers; the RECORDS are
	// rendered one at a time and released one at a time, so this bounds the
	// syscall count without bounding memory to the size of the log.
	evtNextBlock = 64

	// evtNextTimeout is milliseconds EvtNext may wait. Zero-ish: this is a
	// query over an existing log, not a subscription, so there is nothing to
	// wait for. A generous timeout here would be a capability that blocks.
	evtNextTimeout = 1000
)

var (
	wevtapi       = windows.NewLazySystemDLL("wevtapi.dll")
	procEvtQuery  = wevtapi.NewProc("EvtQuery")
	procEvtNext   = wevtapi.NewProc("EvtNext")
	procEvtRender = wevtapi.NewProc("EvtRender")
	procEvtClose  = wevtapi.NewProc("EvtClose")
)

// platformSource returns the Windows source.
func platformSource() (Source, error) { return winSource{}, nil }

type winSource struct{}

func (winSource) Name() string { return "windows-eventlog" }

func (winSource) Normalize(rec Record) (wire.SecurityEvent, bool, error) {
	return parseWinEvent(rec.Payload, rec.Position)
}

// queryFor builds the XPath for records after `after`.
//
// Both halves matter. The EventRecordID predicate is the cursor -- without it
// every job re-reads the channel from the start. The EventID disjunction is the
// taxonomy -- without it the service renders every record and this process
// throws most of them away after paying for them.
func queryFor(after string) string {
	var b strings.Builder
	b.WriteString("*[System[")
	if n, err := strconv.ParseUint(strings.TrimSpace(after), 10, 64); err == nil && n > 0 {
		b.WriteString("EventRecordID>")
		b.WriteString(strconv.FormatUint(n, 10))
		b.WriteString(" and ")
	}
	b.WriteString("(")
	first := true
	for _, id := range taxonomyIDs() {
		if !first {
			b.WriteString(" or ")
		}
		first = false
		b.WriteString("EventID=")
		b.WriteString(strconv.Itoa(id))
	}
	b.WriteString(")]]")
	return b.String()
}

func (s winSource) Open(ctx context.Context, after string) (Reader, error) {
	return openWinQuery(queryFor(after), evtQueryChannelPath|evtQueryForwardDirection)
}

// Rewound answers "has the Security log been cleared behind my cursor?".
//
// It asks the channel for its NEWEST record with no cursor predicate and no
// event-id filter, in reverse direction, and compares. A newest record id below
// the stored cursor means the log was cleared or replaced: `wevtutil cl
// Security`, a retention policy that wrapped, or an attacker covering their
// tracks. Without this the cursor filters out every subsequent event forever
// and the device silently stops producing evidence.
func (s winSource) Rewound(ctx context.Context, position string) (bool, error) {
	r, err := openWinQuery("*", evtQueryChannelPath|evtQueryReverseDirection)
	if err != nil {
		return false, err
	}
	defer r.Close()
	rec, err := r.Next()
	if err == io.EOF {
		// An EMPTY channel with a non-empty cursor is the clearest possible
		// rewind: the log was cleared and nothing has been written since.
		return true, nil
	}
	if err != nil {
		return false, err
	}
	return winPositionLess(rec.Position, position), nil
}

// taxonomyIDs returns the collected event ids in a stable order, so the query
// string is deterministic and can be asserted.
func taxonomyIDs() []int {
	ids := make([]int, 0, len(eventTaxonomy))
	for id := range eventTaxonomy {
		ids = append(ids, id)
	}
	sortInts(ids)
	return ids
}

// winReader streams rendered event XML.
//
// ONE RECORD IS RESIDENT AT A TIME. EvtNext yields a block of HANDLES; each is
// rendered, converted, and closed before the next is touched, and the render
// buffer is REUSED across records. A 400 MB Security log costs this reader a
// few kilobytes.
type winReader struct {
	query   windows.Handle
	handles [evtNextBlock]windows.Handle
	n       int
	i       int
	buf     []byte
	closed  bool
}

func openWinQuery(query string, flags uint32) (*winReader, error) {
	path, err := windows.UTF16PtrFromString(winChannel)
	if err != nil {
		return nil, err
	}
	q, err := windows.UTF16PtrFromString(query)
	if err != nil {
		return nil, err
	}
	h, _, callErr := procEvtQuery.Call(
		0,
		uintptr(unsafe.Pointer(path)),
		uintptr(unsafe.Pointer(q)),
		uintptr(flags),
	)
	if h == 0 {
		return nil, fmt.Errorf("EvtQuery(%s): %w", winChannel, callErr)
	}
	return &winReader{query: windows.Handle(h), buf: make([]byte, 16<<10)}, nil
}

func (r *winReader) Next() (Record, error) {
	for {
		if r.i < r.n {
			h := r.handles[r.i]
			r.i++
			payload, err := r.render(h)
			procEvtClose.Call(uintptr(h))
			if err != nil {
				return Record{}, err
			}
			return Record{Position: recordIDOf(payload), Payload: payload}, nil
		}
		if err := r.fill(); err != nil {
			return Record{}, err
		}
	}
}

// fill fetches the next block of handles. io.EOF when the result set is done.
func (r *winReader) fill() error {
	var returned uint32
	ok, _, callErr := procEvtNext.Call(
		uintptr(r.query),
		uintptr(evtNextBlock),
		uintptr(unsafe.Pointer(&r.handles[0])),
		uintptr(evtNextTimeout),
		0,
		uintptr(unsafe.Pointer(&returned)),
	)
	if ok == 0 {
		if errno, isErrno := callErr.(windows.Errno); isErrno && errno == windows.ERROR_NO_MORE_ITEMS {
			return io.EOF
		}
		return fmt.Errorf("EvtNext: %w", callErr)
	}
	if returned == 0 {
		return io.EOF
	}
	r.n, r.i = int(returned), 0
	return nil
}

// render turns one event handle into UTF-8 XML, reusing r.buf.
func (r *winReader) render(h windows.Handle) ([]byte, error) {
	for attempt := 0; attempt < 2; attempt++ {
		var used, props uint32
		ok, _, callErr := procEvtRender.Call(
			0,
			uintptr(h),
			uintptr(evtRenderEventXml),
			uintptr(len(r.buf)),
			uintptr(unsafe.Pointer(&r.buf[0])),
			uintptr(unsafe.Pointer(&used)),
			uintptr(unsafe.Pointer(&props)),
		)
		if ok != 0 {
			return utf16BytesToUTF8(r.buf[:used]), nil
		}
		errno, isErrno := callErr.(windows.Errno)
		if !isErrno || errno != windows.ERROR_INSUFFICIENT_BUFFER {
			return nil, fmt.Errorf("EvtRender: %w", callErr)
		}
		// Grow ONCE, to exactly what the API asked for, and keep it for the
		// rest of the stream. A record that wants more than 1 MiB of XML is
		// not a logon record; refusing to grow past that is what stops one
		// hostile record from costing the whole idle budget.
		if used > (1 << 20) {
			return nil, fmt.Errorf("EvtRender: a single record wants %d bytes; refusing", used)
		}
		r.buf = make([]byte, used)
	}
	return nil, fmt.Errorf("EvtRender: buffer never sufficed")
}

func (r *winReader) Close() error {
	if r.closed {
		return nil
	}
	r.closed = true
	// Any handles fetched and not yet consumed are still ours to release.
	for i := r.i; i < r.n; i++ {
		procEvtClose.Call(uintptr(r.handles[i]))
	}
	procEvtClose.Call(uintptr(r.query))
	return nil
}

// utf16BytesToUTF8 converts the UTF-16LE buffer EvtRender writes.
//
// EvtRender reports `used` in BYTES including the terminating NUL, so the
// buffer is walked as pairs and stopped at the first zero unit rather than
// trusting the length -- a rendered document that ended exactly on the buffer
// boundary would otherwise pick up whatever the previous, longer record left
// behind, since the buffer is reused.
func utf16BytesToUTF8(b []byte) []byte {
	if len(b) < 2 {
		return nil
	}
	u16 := make([]uint16, 0, len(b)/2)
	for i := 0; i+1 < len(b); i += 2 {
		c := uint16(b[i]) | uint16(b[i+1])<<8
		if c == 0 {
			break
		}
		u16 = append(u16, c)
	}
	return []byte(windows.UTF16ToString(u16))
}
