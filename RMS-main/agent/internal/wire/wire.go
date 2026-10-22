// Package wire implements agent wire protocol v1.
//
// Every type here implements an exported schema in docs/kernel/wire-protocol/,
// named in the comment above it. Those files -- generated from the Pydantic
// models by scripts/export_wire_schema.py and kept current by
// backend/tests/test_wire_schema_export.py -- are the contract, not
// app/gateway/protocol.py as read by a human.
//
// # This is a compatibility contract (docs/kernel/wire-protocol/README.md)
//
// A deployed agent is always older than the control plane it talks to. Living
// in one repository makes that easier to get wrong, not safer. Two rules are
// implemented here rather than merely documented:
//
//   - Inbound is loose. Unknown fields are ignored, at the top level and inside
//     the job payload, so a strictly additive server change does not take an
//     old fleet offline. What is NOT loose is `v` and `kind`: an unknown
//     version or an unrecognised kind is refused, because acting on a frame the
//     server has no code to send is worse than being offline.
//   - Outbound is exact. `_Msg` is extra="forbid" server-side, so an extra key
//     does not get ignored -- parse_agent_message drops the WHOLE message and
//     the only trace is a logger.warning (gateway/main.py:200-202). Encode
//     writes `v` and `kind` itself and refuses to frame any type that is not a
//     declared outbound message, so no caller can invent a kind.
package wire

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
)

// Version is the only protocol version this agent speaks. protocol.py types it
// as Literal[1] on both sides.
const Version = 1

// Message kinds. Server -> agent has exactly three: KindJob,
// KindBackupChunkGrant (the reply half of the backup dedup exchange), and
// KindBackupReadGrant (the reply half of the backup READ exchange). There is
// still no policy channel and no config channel (protocol.py:150-153): a grant
// answers a question this agent asked while running a signed job, and carries
// no instruction.
const (
	KindJob            = "job"
	KindJobStatus      = "job_status"
	KindJobLog         = "job_log"
	KindJobResult      = "job_result"
	KindHeartbeat      = "heartbeat"
	KindSecurityEvents = "security_events"
	KindHealthReport   = "health_report"
	// The backup dedup exchange (docs/modules/backup/spec.md §5.2). The FIRST
	// server -> agent kind other than KindJob, which is why Decode's contract
	// changes shape here rather than merely gaining a case.
	KindBackupChunkQuery = "backup_chunk_query"
	KindBackupChunkGrant = "backup_chunk_grant"
	// The READ half of the repository exchange, added because SigV4 query
	// signing cannot express a prefix-scoped READ and a restore's selection
	// can run to six figures (docs/modules/backup/spec.md §6.1 item 1).
	//
	// A SEPARATE kind pair rather than a mode field on the chunk query:
	// inbound parsing is deliberately loose, so an OLD control plane would
	// ignore a discriminator and answer with a WRITE grant, which a new agent
	// could misread as authorization it does not hold. An unknown kind fails
	// loudly instead -- the old server rejects the frame, nothing answers, and
	// readreply's bounded wait ends the restore with a stated reason.
	KindBackupReadQuery = "backup_read_query"
	KindBackupReadGrant = "backup_read_grant"
)

// Log levels (JobLogMsg.level).
const (
	LevelDebug = "debug"
	LevelInfo  = "info"
	LevelWarn  = "warn"
	LevelError = "error"
)

// Terminal job states (JobResultMsg.state).
const (
	StateSucceeded = "succeeded"
	StateFailed    = "failed"
	StateRefused   = "refused"
)

// Normalized security-event taxonomy (SecurityEventsMsg $defs SecurityEventDoc;
// docs/modules/security-logs/spec.md §2). The control plane accepts no others.
const (
	EventLogonSuccess       = "logon_success"
	EventLogonFailure       = "logon_failure"
	EventLogoff             = "logoff"
	EventAccountLockout     = "account_lockout"
	EventPrivilegeElevation = "privilege_elevation"
	EventAdminGroupChange   = "admin_group_change"
	EventPolicyChange       = "policy_change"
)

// ---------------------------------------------------------------------------
// Inbound: JobEnvelope.schema.json
// ---------------------------------------------------------------------------

// JobPayload implements JobEnvelope.schema.json $defs JobPayload.
//
// Params stays json.RawMessage deliberately. The agent must not round-trip
// params through map[string]any before verification: canonicalisation happens
// on the received bytes (internal/canonical), and every intermediate
// representation is a chance to change them. Each capability unmarshals Params
// into its own typed struct AFTER verification.
type JobPayload struct {
	ID        string          `json:"id"`
	Type      string          `json:"type"`
	Device    string          `json:"device"`
	Params    json.RawMessage `json:"params"`
	IssuedAt  string          `json:"issued_at"`
	ExpiresAt string          `json:"expires_at"`
	Nonce     string          `json:"nonce"`
}

// Envelope implements JobEnvelope.schema.json: the one server -> agent frame.
//
// `v` and `kind` are not fields. They are checked by Decode and are not
// settable, so nothing downstream can hold an Envelope that claims a version
// this agent does not speak.
type Envelope struct {
	Job JobPayload `json:"job"`
	Sig string     `json:"sig"`

	// RawJob is the exact bytes of the `job` member as received.
	//
	// `sig` is Ed25519 over canonical_bytes of the job object the SERVER built
	// (app/jobs/engine.py::_payload, _sign). Verification must canonicalise
	// what arrived, not a JobPayload marshalled back out: a field added by a
	// newer control plane is inside the signed payload, and rebuilding the
	// object would silently drop it and turn a valid job into a signature
	// mismatch on every endpoint in the fleet.
	RawJob json.RawMessage `json:"-"`
}

// frame is the minimum needed to route an inbound message: everything else is
// decoded only once `v` and `kind` are known good.
type frame struct {
	V    *int            `json:"v"`
	Kind *string         `json:"kind"`
	Job  json.RawMessage `json:"job"`
	Sig  string          `json:"sig"`
}

// Errors Decode returns. They are distinguishable because the transport treats
// them differently: a version mismatch is a deployment problem worth logging
// loudly and repeatedly, an unknown kind is a newer server and worth logging
// once.
var (
	ErrVersion = errors.New("unsupported protocol version")
	ErrKind    = errors.New("unknown message kind")
)

// Decode parses one inbound frame.
//
// It returns *Envelope for a job, *ChunkGrant for the reply half of the
// backup dedup exchange, or *ReadGrant for the reply half of the backup read
// exchange. Anything else -- an unknown kind, an agent -> server kind arriving
// inbound, a version other than 1 -- is an error: this agent never acts on a
// frame the control plane has no code to have sent.
//
// A caller therefore type-switches. Returning three types is the honest shape:
// making a grant an Envelope variant would let job-verification code receive
// something that carries no signature.
func Decode(b []byte) (any, error) {
	var f frame
	dec := json.NewDecoder(bytes.NewReader(b))
	if err := dec.Decode(&f); err != nil {
		return nil, fmt.Errorf("wire: malformed frame: %w", err)
	}
	if f.V == nil {
		return nil, fmt.Errorf("wire: %w: no version field", ErrVersion)
	}
	if *f.V != Version {
		return nil, fmt.Errorf("wire: %w: %d (this agent speaks %d)", ErrVersion, *f.V, Version)
	}
	if f.Kind == nil {
		return nil, fmt.Errorf("wire: %w: no kind field", ErrKind)
	}
	if *f.Kind == KindBackupChunkGrant {
		// Unmarshalled from the ORIGINAL bytes, not from `frame`: a grant's
		// fields are top-level, not nested under `job`. Unknown fields are
		// ignored here, which is the inbound-is-loose rule.
		var g ChunkGrant
		if err := json.Unmarshal(b, &g); err != nil {
			return nil, fmt.Errorf("wire: malformed chunk grant: %w", err)
		}
		return &g, nil
	}
	if *f.Kind == KindBackupReadGrant {
		var g ReadGrant
		if err := json.Unmarshal(b, &g); err != nil {
			return nil, fmt.Errorf("wire: undecodable backup_read_grant: %w", err)
		}
		return &g, nil
	}
	if *f.Kind != KindJob {
		return nil, fmt.Errorf("wire: %w: %q", ErrKind, *f.Kind)
	}
	if len(f.Job) == 0 {
		return nil, errors.New("wire: job envelope carries no job")
	}
	if f.Sig == "" {
		// Not "a job with nothing to check": a job that must not run. Refused
		// at the parse layer so no later code has to remember to (invariant #1).
		return nil, errors.New("wire: job envelope carries an empty sig")
	}
	env := &Envelope{Sig: f.Sig, RawJob: f.Job}
	if err := json.Unmarshal(f.Job, &env.Job); err != nil {
		return nil, fmt.Errorf("wire: malformed job payload: %w", err)
	}
	return env, nil
}

// ---------------------------------------------------------------------------
// Outbound: the eight agent -> server kinds
// ---------------------------------------------------------------------------

// outbound is implemented by exactly the eight agent -> server message types.
// The unexported method closes the set: no package can add a kind, and Encode
// refuses anything else.
type outbound interface{ kind() string }

// JobStatus implements JobStatusMsg.schema.json. `state` is "running" and
// nothing else -- the server types it as a const, and terminal states travel in
// a JobResult.
type JobStatus struct {
	JobID string `json:"job_id"`
	State string `json:"state"`
}

func (JobStatus) kind() string { return KindJobStatus }

// MarshalJSON pins State to the only value the server accepts, so a caller
// cannot emit a state that gets the message dropped.
func (s JobStatus) MarshalJSON() ([]byte, error) {
	type alias JobStatus
	return json.Marshal(alias{JobID: s.JobID, State: "running"})
}

// JobLog implements JobLogMsg.schema.json. Level is optional: omitted, the
// server applies "info".
//
// The device private key, and any credential, never travels here (invariant #3).
type JobLog struct {
	JobID string `json:"job_id"`
	Level string `json:"level,omitempty"`
	Msg   string `json:"msg"`
}

func (JobLog) kind() string { return KindJobLog }

// JobResult implements JobResultMsg.schema.json.
//
// For State == StateRefused the server reads Result["reason"], defaulting to
// "agent refused" (gateway/dispatch.py:334-337), so a refusal without one tells
// the operator nothing.
type JobResult struct {
	JobID       string         `json:"job_id"`
	State       string         `json:"state"`
	Result      map[string]any `json:"result,omitempty"`
	ArtifactRef *string        `json:"artifact_ref,omitempty"`
}

func (JobResult) kind() string { return KindJobResult }

// Heartbeat implements HeartbeatMsg.schema.json. TS is rfc3339 whole seconds
// with a literal Z (app/jobs/canonical.py::rfc3339).
type Heartbeat struct {
	TS string `json:"ts"`
}

func (Heartbeat) kind() string { return KindHeartbeat }

// SecurityEvent implements SecurityEventsMsg.schema.json $defs SecurityEventDoc.
type SecurityEvent struct {
	EventUID  string         `json:"event_uid"`
	TS        string         `json:"ts"`
	EventType string         `json:"event_type"`
	Username  string         `json:"username,omitempty"`
	Source    string         `json:"source,omitempty"`
	Outcome   string         `json:"outcome,omitempty"`
	Raw       map[string]any `json:"raw,omitempty"`
}

// SecurityEvents implements SecurityEventsMsg.schema.json. Events is bounded
// 1..500 server-side; at-least-once, deduped by the server on
// (device, event_uid).
type SecurityEvents struct {
	Events []SecurityEvent `json:"events"`
}

func (SecurityEvents) kind() string { return KindSecurityEvents }

// HealthReportLine implements HealthReportMsg.schema.json $defs
// HealthReportLine. Value is a STRING by design (spec §2.4): typing it
// numerically is the first step toward someone aggregating it over time.
type HealthReportLine struct {
	CheckID    string `json:"check_id"`
	Selector   string `json:"selector,omitempty"`
	Kind       string `json:"kind"`
	State      string `json:"state"`
	Value      string `json:"value,omitempty"`
	ObservedAt string `json:"observed_at"`
}

// HealthReport implements HealthReportMsg.schema.json.
//
// PolicyVersion is required and has minimum 0, and is legitimately 0 on a
// freshly enrolled agent -- so it must never be omitempty. A report whose
// version is below acknowledged-1 is discarded server-side
// (app/modules/health/ingest.py:552), and a dropped key reads as no version at
// all.
//
// Carries no device identity: the binding is the mTLS connection, and a
// `device` field here would be a second, forgeable source of the same fact.
type HealthReport struct {
	PolicyVersion int                `json:"policy_version"`
	Reports       []HealthReportLine `json:"reports"`
}

func (HealthReport) kind() string { return KindHealthReport }

// UploadGrant implements BackupChunkGrantMsg $defs BackupUploadGrant: an S3
// POST policy authorising writes under one repository's prefix.
//
// It is a BEARER CREDENTIAL for its lifetime and is treated as one: never
// logged, never written into a job result, and dead with the job. KeyPrefix
// and MaxBytes restate conditions signed INTO the policy — checking them
// locally saves a round trip on a write S3 would reject, and believing them
// changes nothing about what S3 accepts.
type UploadGrant struct {
	URL       string            `json:"url"`
	Fields    map[string]string `json:"fields"`
	KeyPrefix string            `json:"key_prefix"`
	MaxBytes  int64             `json:"max_bytes"`
}

// ChunkQuery implements BackupChunkQueryMsg.
//
// It names ONE thing: the job. No repository id, no storage prefix, no org.
// The server derives all of those from the job it signed, and an agent that
// could name its own repository could name someone else's — in a
// deduplicating store that is a cross-tenant existence oracle.
//
// Check and Stored are omitempty because the schema does not require them: a
// query that only reports uploads carries neither.
type ChunkQuery struct {
	JobID   string   `json:"job_id"`
	QueryID string   `json:"query_id"`
	Check   []string `json:"check,omitempty"`
	Stored  []string `json:"stored,omitempty"`
}

func (ChunkQuery) kind() string { return KindBackupChunkQuery }

// ChunkGrant implements BackupChunkGrantMsg: the dedup answer and the write
// grant for it.
//
// There is deliberately no error variant. Every authorization failure produces
// SILENCE plus a server-side audit record, never a distinguishable reply, so a
// compromised endpoint cannot walk the difference between "no such job" and
// "not your job". The consequence for this agent is that a caller MUST bound
// its wait: see internal/chunkreply.
type ChunkGrant struct {
	JobID     string      `json:"job_id"`
	QueryID   string      `json:"query_id"`
	New       []string    `json:"new,omitempty"`
	Recorded  int64       `json:"recorded"`
	Upload    UploadGrant `json:"upload"`
	ExpiresAt string      `json:"expires_at"`
}

// ReadQuery asks the control plane to sign a batch of object reads.
//
// Suffixes are RELATIVE to the repository prefix and are never absolute keys:
// the server joins each onto the prefix it derives from the job, so this agent
// cannot express a location outside its own repository. The boundary is a
// property of the construction rather than of remembering to validate.
type ReadQuery struct {
	JobID   string `json:"job_id"`
	QueryID string `json:"query_id"`
	// Suffixes is bounded at 256 by the server's parse layer; a larger batch
	// is rejected before any handler sees it.
	Suffixes []string `json:"suffixes"`
}

func (ReadQuery) kind() string { return KindBackupReadQuery }

// ReadGrantItem is one signed GET, for one suffix.
type ReadGrantItem struct {
	Suffix string `json:"suffix"`
	URL    string `json:"url"`
}

// ReadGrant is the reply.
//
// There is deliberately NO error variant, for the reason BackupChunkGrantMsg
// states on the write side: a reply that distinguished "no such job" from "not
// your job" would be an oracle a compromised endpoint could walk. Every
// authorization refusal is silence plus a server-side audit record, and the
// agent sees it as readreply's bounded timeout.
type ReadGrant struct {
	JobID     string          `json:"job_id"`
	QueryID   string          `json:"query_id"`
	Grants    []ReadGrantItem `json:"grants"`
	ExpiresAt string          `json:"expires_at"`
}

// Encode frames one outbound message.
//
// It writes `v` and `kind` itself: they are not struct fields, so no caller can
// emit v:2 or invent a kind. Anything that is not one of the eight declared
// outbound types is refused rather than framed.
func Encode(msg any) ([]byte, error) {
	m, ok := msg.(outbound)
	if !ok {
		return nil, fmt.Errorf("wire: %T is not an outbound message", msg)
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	// The server decodes JSON, so HTML escaping would be harmless on the wire
	// -- but a job_log reaching an operator's console as "<" is a
	// legibility bug with no upside. (Canonical bytes for signatures are a
	// different problem entirely; see internal/canonical.)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(msg); err != nil {
		return nil, err
	}
	body := bytes.TrimRight(buf.Bytes(), "\n")
	if len(body) < 2 || body[0] != '{' || body[len(body)-1] != '}' {
		return nil, fmt.Errorf("wire: %T did not marshal to a JSON object", msg)
	}
	out := make([]byte, 0, len(body)+32)
	out = append(out, `{"v":1,"kind":"`...)
	out = append(out, m.kind()...)
	out = append(out, '"')
	if len(body) > 2 { // not "{}"
		out = append(out, ',')
		out = append(out, body[1:len(body)-1]...)
	}
	out = append(out, '}')
	return out, nil
}
