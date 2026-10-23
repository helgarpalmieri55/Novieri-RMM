package logs

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// THE MACOS UNIFIED LOG, NORMALISED. Platform-independent on purpose, for the
// same reason winevent.go is: `log show --style ndjson` produces text, and
// everything after that is string handling that Linux CI can test against
// fixture lines.
//
// # This mapping is pattern-based, and that is a real limitation
//
// Windows hands over a structured record with a numeric event id; macOS hands
// over a log MESSAGE written by whichever process emitted it. So the taxonomy
// mapping here matches on the emitting process plus a message prefix, and it is
// therefore:
//
//   - VERSION-SENSITIVE. Apple rewords these messages between releases. A
//     reworded message stops matching and the event is silently dropped rather
//     than misclassified, which is the safer of the two failure modes but is
//     still an evidence gap.
//   - DELIBERATELY CONSERVATIVE. An unmatched line is dropped, never guessed
//     at: `ingest_events` rejects the WHOLE batch on an unknown event_type, and
//     a wrong classification in a SOC 2 evidence table is worse than a missing
//     row, because it is not visibly missing.
//
// docs/modules/security-logs/spec.md §10.4 records this as the weakest link in
// the macOS collector and names the alternative (an OSLog/cgo collector, which
// would cost the single static binary).

// logBinary is the absolute path, not a PATH lookup. A SYSTEM-privileged
// process resolving a bare name through PATH is a well-known privilege-
// escalation shape, and there is exactly one `log` this can mean.
//
//lint:ignore U1000 consumed by source_darwin.go; staticcheck runs at GOOS=linux
const logBinary = "/usr/bin/log"

// unifiedPredicate is the whole of what this agent asks macOS for.
//
// AN ALLOWLIST OF PROCESSES, and a short one. The unified log carries
// everything on the machine; a broad predicate here would turn an
// authentication-evidence feed into general host telemetry, which
// docs/00-vision.md puts out of scope -- and would ship it through a
// retention-governed table that was sized for "thousands of rows/day"
// (spec §2).
//
//   - opendirectoryd: authentication decisions, local and directory
//   - authd / authorizationhost / securityd: authorization prompts and results
//   - loginwindow: console logon and logoff
//   - sshd: remote logon
//   - sudo: privilege elevation
//   - screensharing: remote console
const unifiedPredicate = `process == "opendirectoryd" OR process == "authd" OR ` +
	`process == "authorizationhost" OR process == "loginwindow" OR ` +
	`process == "sshd" OR process == "sudo" OR process == "screensharingd"`

// unifiedWaitDelay is how long Wait may block after the context kills the
// process. See property 2 above.
//
//lint:ignore U1000 consumed by source_darwin.go; staticcheck runs at GOOS=linux
const unifiedWaitDelay = 2 * time.Second

// firstRunWindow is how far back a macOS agent with no cursor reads.
//
// A bounded first read rather than the whole store: an endpoint enrolling for
// the first time must not spend its first job reading months of history, and
// the evidence window the control plane cares about starts when the device
// became managed.
//
//lint:ignore U1000 consumed by source_darwin.go; staticcheck runs at GOOS=linux
const firstRunWindow = "24h"

// unifiedRecord is the subset of `log show --style ndjson` this agent reads.
type unifiedRecord struct {
	Timestamp        string `json:"timestamp"`
	ProcessImagePath string `json:"processImagePath"`
	Process          string `json:"process"`
	Subsystem        string `json:"subsystem"`
	Category         string `json:"category"`
	EventMessage     string `json:"eventMessage"`
	MessageType      string `json:"messageType"`
	ThreadID         uint64 `json:"threadID"`
	ProcessID        int    `json:"processID"`
}

// unifiedRule is one message pattern and what it means.
type unifiedRule struct {
	process   string
	contains  string
	eventType string
	outcome   string
	// user extracts the account from the message. Nil means "no account is
	// recoverable from this line", which is legal: `username` defaults to "".
	user func(msg string) string
}

// unifiedRules is the complete macOS mapping. Order matters: the first match
// wins, so more specific patterns come first.
//
// Every entry maps into the v1 taxonomy and nothing else. There is no rule that
// produces an event_type outside `logon_success`, `logon_failure`, `logoff`,
// `account_lockout`, `privilege_elevation`, `admin_group_change`,
// `policy_change` -- and unifiedTaxonomyIsClosed (unified_test.go) asserts that
// against the exported schema rather than trusting this comment.
var unifiedRules = []unifiedRule{
	// sshd: the clearest signals macOS produces, and identical in wording to
	// OpenSSH everywhere.
	{"sshd", "Accepted password for", "logon_success", "success", afterWord("for")},
	{"sshd", "Accepted publickey for", "logon_success", "success", afterWord("for")},
	{"sshd", "Accepted keyboard-interactive/pam for", "logon_success", "success", afterWord("for")},
	{"sshd", "Failed password for", "logon_failure", "failure", afterWord("for")},
	{"sshd", "Failed publickey for", "logon_failure", "failure", afterWord("for")},
	{"sshd", "Invalid user", "logon_failure", "failure", afterWord("user")},
	{"sshd", "Disconnected from user", "logoff", "n_a", afterWord("user")},
	{"sshd", "pam_unix(sshd:session): session closed for user", "logoff", "n_a", afterWord("user")},

	// sudo: privilege elevation. The classic syslog shape survives into the
	// unified log.
	{"sudo", "TTY=", "privilege_elevation", "success", beforeColon},
	{"sudo", "authentication failure", "logon_failure", "failure", afterEquals("user")},

	// loginwindow: console session start and end.
	{"loginwindow", "USER_PROCESS", "logon_success", "success", nil},
	{"loginwindow", "DEAD_PROCESS", "logoff", "n_a", nil},

	// opendirectoryd: the authoritative local/directory authentication result.
	{"opendirectoryd", "Authentication failed", "logon_failure", "failure", quotedNode},
	{"opendirectoryd", "Authentication succeeded", "logon_success", "success", quotedNode},
	{"opendirectoryd", "Password verification failed", "logon_failure", "failure", quotedNode},
	{"opendirectoryd", "account is disabled", "account_lockout", "n_a", quotedNode},
	{"opendirectoryd", "too many failed", "account_lockout", "n_a", quotedNode},

	// authd / authorizationhost: an authorization right being granted is a
	// privilege elevation in this taxonomy (the macOS analogue of 4672).
	{"authd", "succeeded to authorize right", "privilege_elevation", "success", nil},
	{"authd", "failed to authorize right", "logon_failure", "failure", nil},
	{"authorizationhost", "Authorization succeeded", "privilege_elevation", "success", nil},

	// screensharingd: remote console.
	{"screensharingd", "Authentication: SUCCEEDED", "logon_success", "success", afterEquals("user")},
	{"screensharingd", "Authentication: FAILED", "logon_failure", "failure", afterEquals("user")},
}

// parseUnifiedLine turns one NDJSON line into a wire event.
func parseUnifiedLine(payload []byte, position string) (wire.SecurityEvent, bool, error) {
	var rec unifiedRecord
	if err := json.Unmarshal(payload, &rec); err != nil {
		return wire.SecurityEvent{}, false, fmt.Errorf("unified log ndjson: %w", err)
	}

	proc := rec.Process
	if proc == "" {
		proc = lastPathElement(rec.ProcessImagePath)
	}
	msg := rec.EventMessage

	var matched *unifiedRule
	for i := range unifiedRules {
		r := &unifiedRules[i]
		if r.process == proc && strings.Contains(msg, r.contains) {
			matched = r
			break
		}
	}
	if matched == nil {
		// Not an authentication record this agent recognises. Dropped, never
		// guessed at: a wrong classification in a SOC 2 evidence table is worse
		// than a missing row because it is not visibly missing.
		return wire.SecurityEvent{}, false, nil
	}

	ts, err := normalizeUnifiedTime(rec.Timestamp)
	if err != nil {
		return wire.SecurityEvent{}, false, fmt.Errorf("timestamp %q: %w", rec.Timestamp, err)
	}

	user := ""
	if matched.user != nil {
		user = strings.TrimSpace(matched.user(msg))
	}

	out := wire.SecurityEvent{
		EventUID:  unifiedEventUID(rec, ts),
		TS:        ts,
		EventType: matched.eventType,
		Username:  user,
		Source:    clamp("mac:"+proc, maxSourceLen),
		Outcome:   matched.outcome,
		Raw: map[string]any{
			"process":   proc,
			"subsystem": rec.Subsystem,
			"category":  rec.Category,
			"pid":       rec.ProcessID,
			// The message IS the evidence on macOS -- there are no structured
			// fields behind it -- so it is carried verbatim, clamped by
			// clampRaw like every other value.
			"message": msg,
		},
	}
	if position != "" {
		out.Raw["position"] = position
	}
	return out, true, nil
}

// unifiedEventUID derives the idempotency key for a macOS record.
//
// Same contract as winEventUID and for the same reason: the server dedupes on
// `(device_id, event_uid)` and delivery is at-least-once, so a random uid would
// make every resend a duplicate row in the access-evidence export.
//
// There is no record id in the unified log, so the key is a hash of the fields
// that together identify one emission: the timestamp (microsecond precision),
// the emitting process and thread, and the message. Two genuinely distinct
// events that agree on all four are indistinguishable in the source as well,
// so collapsing them loses nothing that was ever there.
//
// `--start` is INCLUSIVE, so the record at the cursor boundary is re-read on
// the next job by construction. That is precisely the case a deterministic uid
// absorbs: the server sees the same key and does nothing.
func unifiedEventUID(rec unifiedRecord, ts string) string {
	h := sha256.New()
	h.Write([]byte(ts))
	h.Write([]byte{0})
	h.Write([]byte(rec.Process))
	h.Write([]byte{0})
	h.Write([]byte(fmt.Sprintf("%d/%d", rec.ProcessID, rec.ThreadID)))
	h.Write([]byte{0})
	h.Write([]byte(rec.EventMessage))
	return "mac:" + hex.EncodeToString(h.Sum(nil))[:40]
}

// normalizeUnifiedTime converts `log show`'s timestamp into the shape
// `datetime.fromisoformat` accepts.
//
// `log show` emits local time with an offset and SIX fractional digits, e.g.
// "2026-07-18 09:12:03.123456+0100" -- a space instead of a T, and an offset
// with no colon. Python 3.12's fromisoformat would actually take that, but the
// server stores what arrives and every other endpoint sends UTC with a Z; an
// evidence table where half the rows are local time is one nobody can sort.
// Converted to UTC here.
func normalizeUnifiedTime(ts string) (string, error) {
	s := strings.TrimSpace(ts)
	if s == "" {
		return "", fmt.Errorf("empty")
	}
	for _, layout := range []string{
		"2006-01-02 15:04:05.999999-0700",
		"2006-01-02 15:04:05.999999Z0700",
		"2006-01-02T15:04:05.999999-0700",
		time.RFC3339Nano,
		time.RFC3339,
	} {
		if t, err := time.Parse(layout, s); err == nil {
			return t.UTC().Format("2006-01-02T15:04:05.000000Z"), nil
		}
	}
	return "", fmt.Errorf("no known layout")
}

// unifiedStart turns a stored cursor position into a `--start` argument.
//
// The position IS the last record's timestamp in `log show`'s own local format,
// so it is passed straight back. An empty or unparseable cursor yields "" and
// Open falls back to firstRunWindow rather than reading the whole store.
func unifiedStart(position string) string {
	s := strings.TrimSpace(position)
	if s == "" {
		return ""
	}
	// Validated, not trusted: this string becomes a process ARGUMENT. It is
	// argv rather than a shell word so there is nothing to inject, but an
	// unparseable value would make `log show` fail the whole job, and falling
	// back to a bounded window is better than failing.
	if _, err := normalizeUnifiedTime(s); err != nil {
		return ""
	}
	return s
}

// unifiedReader streams NDJSON lines from `log show`'s stdout.
//
// STREAMED, one line at a time, with a bounded line length. `log show` on a Mac
// that has been up for months produces a great deal of output, and a reader
// that accumulated it would be the memory bug the idle budget exists to
// prevent.
type unifiedReader struct {
	scanner *bufio.Scanner
	closer  io.Closer
	wait    func() error
	done    bool
}

// maxUnifiedLine bounds one NDJSON line. Unified-log messages are short; a
// megabyte is far above anything genuine and stops one pathological line from
// costing the whole budget.
const maxUnifiedLine = 1 << 20

func newUnifiedReader(stdout io.ReadCloser, wait func() error) *unifiedReader {
	sc := bufio.NewScanner(stdout)
	sc.Buffer(make([]byte, 0, 64<<10), maxUnifiedLine)
	return &unifiedReader{scanner: sc, closer: stdout, wait: wait}
}

func (r *unifiedReader) Next() (Record, error) {
	for r.scanner.Scan() {
		line := strings.TrimSpace(r.scanner.Text())
		if line == "" || !strings.HasPrefix(line, "{") {
			// `log show` prints a header line and occasional notices. Skipped,
			// not treated as an error.
			continue
		}
		// The POSITION is the record's own timestamp, read cheaply rather than
		// by unmarshalling twice. A line with no timestamp cannot advance the
		// cursor, which is correct: the cursor must never move past a record
		// whose place in time is unknown.
		return Record{Position: unifiedTimestampOf(line), Payload: []byte(line)}, nil
	}
	if err := r.scanner.Err(); err != nil {
		return Record{}, err
	}
	r.done = true
	return Record{}, io.EOF
}

func (r *unifiedReader) Close() error {
	if r.closer != nil {
		_ = r.closer.Close()
	}
	if r.wait != nil {
		// Reaping the process is what stops a zombie per job. WaitDelay bounds
		// how long this can take (source_darwin.go, property 2).
		_ = r.wait()
	}
	return nil
}

// unifiedTimestampOf lifts the `timestamp` member out of an NDJSON line without
// a full unmarshal, so a dropped record still advances the cursor cheaply.
func unifiedTimestampOf(line string) string {
	const key = `"timestamp":"`
	i := strings.Index(line, key)
	if i < 0 {
		return ""
	}
	rest := line[i+len(key):]
	j := strings.IndexByte(rest, '"')
	if j < 0 {
		return ""
	}
	return rest[:j]
}

// ---------------------------------------------------------------------------
// Message-shape extractors
// ---------------------------------------------------------------------------

// afterWord returns the token following `word`.
func afterWord(word string) func(string) string {
	return func(msg string) string {
		fields := strings.Fields(msg)
		for i, f := range fields {
			if f == word && i+1 < len(fields) {
				return fields[i+1]
			}
		}
		return ""
	}
}

// afterEquals returns the value of a `key=value` or `key="value"` pair.
func afterEquals(key string) func(string) string {
	return func(msg string) string {
		i := strings.Index(msg, key+"=")
		if i < 0 {
			return ""
		}
		rest := msg[i+len(key)+1:]
		rest = strings.TrimPrefix(rest, `"`)
		if j := strings.IndexAny(rest, ` ",;`); j >= 0 {
			return rest[:j]
		}
		return rest
	}
}

// beforeColon returns the leading token of a sudo line, which is the invoking
// user: `jsmith : TTY=ttys000 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/id`.
func beforeColon(msg string) string {
	i := strings.Index(msg, " : ")
	if i < 0 {
		return ""
	}
	return strings.TrimSpace(msg[:i])
}

// quotedNode returns the first single-quoted token, which is how
// opendirectoryd names the record it was asked about.
func quotedNode(msg string) string {
	i := strings.IndexByte(msg, '\'')
	if i < 0 {
		return ""
	}
	rest := msg[i+1:]
	j := strings.IndexByte(rest, '\'')
	if j < 0 {
		return ""
	}
	return rest[:j]
}

func lastPathElement(p string) string {
	if i := strings.LastIndexByte(p, '/'); i >= 0 {
		return p[i+1:]
	}
	return p
}
