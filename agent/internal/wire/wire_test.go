package wire_test

import (
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

const goodEnvelope = `{"v":1,"kind":"job","job":{"id":"j1","type":"hello",` +
	`"device":"d1","params":{"message":"hi"},"issued_at":"2026-08-05T10:00:00Z",` +
	`"expires_at":"2026-08-05T10:05:00Z","nonce":"n1"},"sig":"AAAA"}`

func TestDecodeAcceptsAJobEnvelope(t *testing.T) {
	msg, err := wire.Decode([]byte(goodEnvelope))
	if err != nil {
		t.Fatal(err)
	}
	env, ok := msg.(*wire.Envelope)
	if !ok {
		t.Fatalf("Decode returned %T, want *wire.Envelope", msg)
	}
	if env.Sig != "AAAA" {
		t.Errorf("Sig = %q", env.Sig)
	}
	got := env.Job
	want := wire.JobPayload{
		ID: "j1", Type: "hello", Device: "d1",
		IssuedAt: "2026-08-05T10:00:00Z", ExpiresAt: "2026-08-05T10:05:00Z", Nonce: "n1",
	}
	if got.ID != want.ID || got.Type != want.Type || got.Device != want.Device ||
		got.IssuedAt != want.IssuedAt || got.ExpiresAt != want.ExpiresAt || got.Nonce != want.Nonce {
		t.Errorf("Job = %+v, want %+v", got, want)
	}
	if string(got.Params) != `{"message":"hi"}` {
		t.Errorf("Params = %s, want the raw bytes verbatim", got.Params)
	}
}

// The signature covers canonical_bytes of the job object the SERVER built. The
// agent must therefore canonicalise the bytes it received, not a struct it
// rebuilt from them: a field this build of the agent does not know about is
// still inside the signed payload, and reconstructing the object would drop it
// and turn a valid job into a signature mismatch.
func TestEnvelopeKeepsTheRawJobBytesForCanonicalisation(t *testing.T) {
	const withFutureField = `{"v":1,"kind":"job","job":{"id":"j1","type":"hello",` +
		`"device":"d1","params":{},"issued_at":"2026-08-05T10:00:00Z",` +
		`"expires_at":"2026-08-05T10:05:00Z","nonce":"n1","future_field":"x"},"sig":"AAAA"}`
	msg, err := wire.Decode([]byte(withFutureField))
	if err != nil {
		t.Fatalf("an unknown field inside job must be tolerated on inbound: %v", err)
	}
	env := msg.(*wire.Envelope)
	if !strings.Contains(string(env.RawJob), `"future_field":"x"`) {
		t.Fatalf("RawJob = %s; the unknown field was dropped, so the signature "+
			"over the server's bytes could never verify", env.RawJob)
	}
}

// Loosen inbound before you tighten outbound: a newer control plane may add an
// optional key to the frame, and an older agent that rejected the whole frame
// would take itself offline on a strictly additive change.
func TestDecodeIgnoresUnknownTopLevelFields(t *testing.T) {
	const withExtra = `{"v":1,"kind":"job","job":{"id":"j1","type":"hello",` +
		`"device":"d1","params":{},"issued_at":"2026-08-05T10:00:00Z",` +
		`"expires_at":"2026-08-05T10:05:00Z","nonce":"n1"},"sig":"AAAA","trace_id":"t"}`
	if _, err := wire.Decode([]byte(withExtra)); err != nil {
		t.Fatalf("unknown top-level field must be ignored on inbound: %v", err)
	}
}

func TestDecodeRejects(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want string
	}{
		{
			// protocol.py types `v` as Literal[1]. An agent that accepted v:2
			// would one day act on a frame the server would not have sent.
			name: "unknown version",
			raw:  strings.Replace(goodEnvelope, `"v":1`, `"v":2`, 1),
			want: "version",
		},
		{
			// Server -> agent has exactly one kind (protocol.py:150-153).
			// There is no policy channel and no config channel.
			name: "unknown kind",
			raw:  strings.Replace(goodEnvelope, `"kind":"job"`, `"kind":"nope"`, 1),
			want: "kind",
		},
		{
			// An agent -> server kind arriving inbound is not a job. Accepting
			// it would mean the agent believes in a server->agent message the
			// server has no code to send.
			name: "agent-to-server kind arriving inbound",
			raw:  `{"v":1,"kind":"heartbeat","ts":"2026-08-05T00:00:00Z"}`,
			want: "kind",
		},
		{name: "missing kind", raw: `{"v":1,"job":{},"sig":"a"}`, want: "kind"},
		{name: "missing version", raw: `{"kind":"job","job":{},"sig":"a"}`, want: "version"},
		{name: "not an object", raw: `[1,2,3]`, want: ""},
		{name: "empty", raw: ``, want: ""},
		{
			name: "job envelope with no signature",
			raw:  strings.Replace(goodEnvelope, `"sig":"AAAA"`, `"sig":""`, 1),
			want: "sig",
		},
		{
			name: "job envelope with no job",
			raw:  `{"v":1,"kind":"job","sig":"AAAA"}`,
			want: "job",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := wire.Decode([]byte(tc.raw))
			if err == nil {
				t.Fatalf("Decode accepted %s and returned %+v", tc.raw, got)
			}
			if tc.want != "" && !strings.Contains(err.Error(), tc.want) {
				t.Errorf("error %q does not mention %q", err, tc.want)
			}
		})
	}
}

// Invariant #1 support: an empty `sig` is not a job with no signature to check,
// it is a job that must not run. Decode refuses it at the parse layer so no
// later code has to remember to.
func TestEncodeSetsTheFramingItself(t *testing.T) {
	b, err := wire.Encode(wire.Heartbeat{TS: "2026-08-05T00:00:00Z"})
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatal(err)
	}
	if got["v"] != float64(1) {
		t.Errorf("v = %v, want 1", got["v"])
	}
	if got["kind"] != "heartbeat" {
		t.Errorf("kind = %v, want heartbeat", got["kind"])
	}
}

func TestEncodeRejectsAnythingThatIsNotAnOutboundMessage(t *testing.T) {
	// The outbound set is closed: `Encode` will not frame an arbitrary value,
	// so a caller cannot invent a message kind the server does not parse.
	for _, v := range []any{map[string]any{"kind": "job"}, "hello", 42, nil, wire.Envelope{}} {
		if b, err := wire.Encode(v); err == nil {
			t.Errorf("Encode(%T) succeeded and produced %s", v, b)
		}
	}
}

func TestEncodeRoundTripsThroughTheServersRequiredShapes(t *testing.T) {
	cases := []struct {
		name string
		msg  any
		want string
	}{
		{
			name: "job_status carries the only state the server accepts",
			msg:  wire.JobStatus{JobID: "j1"},
			want: `{"v":1,"kind":"job_status","job_id":"j1","state":"running"}`,
		},
		{
			name: "job_log defaults are left to the server",
			msg:  wire.JobLog{JobID: "j1", Msg: "hello"},
			want: `{"v":1,"kind":"job_log","job_id":"j1","msg":"hello"}`,
		},
		{
			name: "job_log with an explicit level",
			msg:  wire.JobLog{JobID: "j1", Level: wire.LevelWarn, Msg: "hello"},
			want: `{"v":1,"kind":"job_log","job_id":"j1","level":"warn","msg":"hello"}`,
		},
		{
			// dispatch.py:334-337 reads result["reason"] for a refusal, so a
			// refusal without one is reported to the operator as the bare
			// default "agent refused".
			name: "job_result refusal carries a reason",
			msg: wire.JobResult{JobID: "j1", State: wire.StateRefused,
				Result: map[string]any{"reason": "signature invalid"}},
			want: `{"v":1,"kind":"job_result","job_id":"j1","state":"refused",` +
				`"result":{"reason":"signature invalid"}}`,
		},
		{
			name: "heartbeat",
			msg:  wire.Heartbeat{TS: "2026-08-05T10:00:00Z"},
			want: `{"v":1,"kind":"heartbeat","ts":"2026-08-05T10:00:00Z"}`,
		},
		{
			// policy_version is required and is legitimately 0 on a freshly
			// enrolled agent; ingest.py:552 discards a report whose version is
			// below acknowledged-1, so dropping the key is not a small thing.
			name: "health_report at policy version zero",
			msg: wire.HealthReport{PolicyVersion: 0, Reports: []wire.HealthReportLine{
				{CheckID: "disk.free_pct", Selector: "/", Kind: "breach",
					State: "breaching", Value: "3.1", ObservedAt: "2026-08-05T10:00:00Z"},
			}},
			want: `{"v":1,"kind":"health_report","policy_version":0,"reports":` +
				`[{"check_id":"disk.free_pct","selector":"/","kind":"breach",` +
				`"state":"breaching","value":"3.1","observed_at":"2026-08-05T10:00:00Z"}]}`,
		},
		{
			name: "security_events with only the required fields",
			msg: wire.SecurityEvents{Events: []wire.SecurityEvent{
				{EventUID: "u1", TS: "2026-08-05T10:00:00Z", EventType: wire.EventLogonFailure},
			}},
			want: `{"v":1,"kind":"security_events","events":` +
				`[{"event_uid":"u1","ts":"2026-08-05T10:00:00Z","event_type":"logon_failure"}]}`,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := wire.Encode(tc.msg)
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != tc.want {
				t.Errorf("Encode\n got %s\nwant %s", got, tc.want)
			}
		})
	}
}

// Outbound framing is ordinary JSON, not canonical JSON -- but it must not be
// HTML-escaped either, because a log line containing "<" would otherwise reach
// the operator's console as "<". (The signature path is canonical bytes
// and lives in internal/canonical; this is only about readability.)
func TestEncodeDoesNotHTMLEscape(t *testing.T) {
	got, err := wire.Encode(wire.JobLog{JobID: "j1", Msg: `a<b>c&d`})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(got), `a<b>c&d`) {
		t.Fatalf("got %s, want the message unescaped", got)
	}
}

func TestEncodeFramesAChunkQuery(t *testing.T) {
	got, err := wire.Encode(wire.ChunkQuery{
		JobID: "job-1", QueryID: "q1", Check: []string{"aa"}, Stored: []string{"bb"},
	})
	if err != nil {
		t.Fatalf("Encode: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(got, &m); err != nil {
		t.Fatalf("not JSON: %v", err)
	}
	if m["v"] != float64(1) || m["kind"] != "backup_chunk_query" {
		t.Errorf("framing = v:%v kind:%v, want v:1 kind:backup_chunk_query", m["v"], m["kind"])
	}
	if m["job_id"] != "job-1" || m["query_id"] != "q1" {
		t.Errorf("identity = %v/%v, want job-1/q1", m["job_id"], m["query_id"])
	}
}

// An empty batch must still frame: `check` and `stored` are optional in the
// schema, and a query reporting only `stored` carries neither.
func TestEncodeChunkQueryOmitsEmptyBatches(t *testing.T) {
	got, err := wire.Encode(wire.ChunkQuery{JobID: "job-1", QueryID: "q1"})
	if err != nil {
		t.Fatalf("Encode: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(got, &m); err != nil {
		t.Fatalf("not JSON: %v", err)
	}
	if _, ok := m["check"]; ok {
		t.Error("empty check was emitted; the server is extra=forbid on shape, not on presence")
	}
	if m["job_id"] != "job-1" {
		t.Errorf("job_id = %v, want job-1", m["job_id"])
	}
}

func TestDecodeRoutesAChunkGrant(t *testing.T) {
	raw := []byte(`{"v":1,"kind":"backup_chunk_grant","job_id":"job-1","query_id":"q1",
		"new":["ab"],"recorded":2,"expires_at":"2026-08-09T00:00:00Z",
		"upload":{"url":"https://h/b","fields":{"key":"p/${filename}"},
		"key_prefix":"org/backup/repo/","max_bytes":1024}}`)
	msg, err := wire.Decode(raw)
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}
	g, ok := msg.(*wire.ChunkGrant)
	if !ok {
		t.Fatalf("Decode returned %T, want *wire.ChunkGrant", msg)
	}
	got := [4]any{g.JobID, g.QueryID, len(g.New), g.Upload.MaxBytes}
	want := [4]any{"job-1", "q1", 1, int64(1024)}
	if got != want {
		t.Errorf("grant = %v, want %v", got, want)
	}
}

// Inbound is loose: a field a newer control plane added must not take an old
// fleet offline.
func TestDecodeIgnoresUnknownGrantFields(t *testing.T) {
	raw := []byte(`{"v":1,"kind":"backup_chunk_grant","job_id":"j","query_id":"q",
		"recorded":0,"expires_at":"t","invented_by_a_newer_server":true,
		"upload":{"url":"https://h/b","fields":{},"key_prefix":"p/","max_bytes":1}}`)
	if _, err := wire.Decode(raw); err != nil {
		t.Fatalf("Decode rejected an additive field: %v", err)
	}
}

// The agent must never act on its own outbound kind arriving inbound.
func TestDecodeRefusesAnInboundChunkQuery(t *testing.T) {
	raw := []byte(`{"v":1,"kind":"backup_chunk_query","job_id":"j","query_id":"q"}`)
	if _, err := wire.Decode(raw); !errors.Is(err, wire.ErrKind) {
		t.Errorf("err = %v, want ErrKind", err)
	}
}

// The agent must never act on its own outbound kind arriving inbound.
func TestDecodeRefusesAnInboundReadQuery(t *testing.T) {
	raw := []byte(`{"v":1,"kind":"backup_read_query","job_id":"j","query_id":"q","suffixes":["chunks/ab"]}`)
	if _, err := wire.Decode(raw); !errors.Is(err, wire.ErrKind) {
		t.Errorf("err = %v, want ErrKind", err)
	}
}

// Encode must refuse a grant: it is inbound-only, and framing one would mean
// the agent could mint its own write authorization.
func TestEncodeRefusesAChunkGrant(t *testing.T) {
	if _, err := wire.Encode(wire.ChunkGrant{JobID: "j"}); err == nil {
		t.Error("Encode framed an inbound-only message")
	}
}

func TestDecodeReturnsAReadGrantForTheReadKind(t *testing.T) {
	frame := []byte(`{"v":1,"kind":"backup_read_grant","job_id":"j1","query_id":"q1",` +
		`"grants":[{"suffix":"chunks/ab/abcd","url":"https://store.example/o?sig=x"}],` +
		`"expires_at":"2026-08-13T10:00:00Z"}`)
	msg, err := wire.Decode(frame)
	if err != nil {
		t.Fatalf("Decode: %v", err)
	}
	g, ok := msg.(*wire.ReadGrant)
	if !ok {
		t.Fatalf("Decode returned %T, want *wire.ReadGrant", msg)
	}
	if g.QueryID != "q1" || len(g.Grants) != 1 || g.Grants[0].Suffix != "chunks/ab/abcd" {
		t.Fatalf("decoded = %+v, want one grant for chunks/ab/abcd with query_id q1", g)
	}
}

func TestAReadQueryEncodesAsAnOutboundFrame(t *testing.T) {
	// Encode refuses anything that does not implement `outbound`, so a
	// ReadQuery without a kind() method cannot be sent at all -- and a
	// json.Marshal test does not notice.
	b, err := wire.Encode(wire.ReadQuery{JobID: "j1", QueryID: "r1", Suffixes: []string{"chunks/ab/abcd"}})
	if err != nil {
		t.Fatalf("Encode(ReadQuery): %v", err)
	}
	got := string(b)
	if !strings.Contains(got, `"kind":"backup_read_query"`) {
		t.Fatalf("encoded = %s, want kind backup_read_query", got)
	}
	if !strings.Contains(got, `"suffixes":["chunks/ab/abcd"]`) {
		t.Fatalf("encoded = %s, want the suffixes array", got)
	}
}
