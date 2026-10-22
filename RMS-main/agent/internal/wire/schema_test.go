package wire_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// schemaDir is the exported contract: docs/kernel/wire-protocol/*.schema.json,
// generated from the Pydantic models by scripts/export_wire_schema.py. The Go
// structs are built against THESE FILES, not against a reading of protocol.py,
// and backend/tests/test_wire_schema_export.py keeps the files current with the
// models. That is the whole chain: model -> schema -> struct, with a guard on
// each link.
const schemaDir = "../../../docs/kernel/wire-protocol"

type jsonSchema struct {
	Title      string                `json:"title"`
	Properties map[string]jsonSchema `json:"properties"`
	Required   []string              `json:"required"`
	Defs       map[string]jsonSchema `json:"$defs"`
	MaxItems   int                   `json:"maxItems"`
}

// binding ties one exported schema to the Go type that implements it.
type binding struct {
	typ reflect.Type
	// framing names schema properties the codec supplies rather than a struct
	// field. `v` and `kind` are written by wire.Encode and checked by
	// wire.Decode; making them settable fields would let a caller emit v:2.
	framing []string
	// nested maps a $defs title to the Go type implementing it.
	nested map[string]reflect.Type
}

var implemented = map[string]binding{
	"JobEnvelope": {
		typ:     reflect.TypeOf(wire.Envelope{}),
		framing: []string{"v", "kind"},
		nested:  map[string]reflect.Type{"JobPayload": reflect.TypeOf(wire.JobPayload{})},
	},
	"JobStatusMsg": {typ: reflect.TypeOf(wire.JobStatus{}), framing: []string{"v", "kind"}},
	"JobLogMsg":    {typ: reflect.TypeOf(wire.JobLog{}), framing: []string{"v", "kind"}},
	"JobResultMsg": {typ: reflect.TypeOf(wire.JobResult{}), framing: []string{"v", "kind"}},
	"HeartbeatMsg": {typ: reflect.TypeOf(wire.Heartbeat{}), framing: []string{"v", "kind"}},
	"SecurityEventsMsg": {
		typ:     reflect.TypeOf(wire.SecurityEvents{}),
		framing: []string{"v", "kind"},
		nested:  map[string]reflect.Type{"SecurityEventDoc": reflect.TypeOf(wire.SecurityEvent{})},
	},
	"HealthReportMsg": {
		typ:     reflect.TypeOf(wire.HealthReport{}),
		framing: []string{"v", "kind"},
		nested:  map[string]reflect.Type{"HealthReportLine": reflect.TypeOf(wire.HealthReportLine{})},
	},
	"BackupChunkQueryMsg": {
		typ:     reflect.TypeOf(wire.ChunkQuery{}),
		framing: []string{"v", "kind"},
	},
	"BackupChunkGrantMsg": {
		typ:     reflect.TypeOf(wire.ChunkGrant{}),
		framing: []string{"v", "kind"},
		nested:  map[string]reflect.Type{"BackupUploadGrant": reflect.TypeOf(wire.UploadGrant{})},
	},
}

// notImplemented is an EXPLICIT list, not a wildcard. A new schema file that
// appears in docs/kernel/wire-protocol/ and is named in neither map fails
// TestEveryExportedSchemaIsAccountedFor, which forces a decision instead of
// letting a new contract be silently ignored by the endpoint half.
//
// These are job params and result documents, not message envelopes: they are
// unmarshalled by whichever capability owns them, after verification, and the
// capabilities that own them are unbuilt.
var notImplemented = map[string]string{
	"HealthCheckNowParams":    "Task 12 (health.check.now capability)",
	"HealthPolicyApplyParams": "Task 12 (health.policy.apply capability)",
	"InventoryDocument":       "Tasks 10-11 (inventory.collect capability)",
	"NetSweepParams":          "Task 13 (net.sweep capability)",
	"NetSweepDocument":        "Task 13 (net.sweep capability)",
	"AppDeployParams":         "job params: unmarshalled by internal/capabilities/deploy, not internal/wire",
	"AppDeployResult":         "job result: built by internal/capabilities/deploy, not internal/wire",
	"ScriptRunParams":         "job params: unmarshalled by internal/capabilities/deploy, not internal/wire",
	"ScriptRunResult":         "job result: built by internal/capabilities/deploy, not internal/wire",
	"BackupManifest":          "manifest document: built by internal/capabilities/backup, not internal/wire",
	"BackupRestoreParams":     "job params: unmarshalled by internal/capabilities/backup, not internal/wire",
	"BackupRestoreResult":     "job result: built by internal/capabilities/backup, not internal/wire",
	"BackupRunParams":         "job params: unmarshalled by internal/capabilities/backup, not internal/wire",
	"BackupRunResult":         "job result: built by internal/capabilities/backup, not internal/wire",
	"FiletreeStream":          "manifest document: built by internal/capabilities/backup, not internal/wire",
	"ManifestEntry":           "manifest document: built by internal/capabilities/backup, not internal/wire",
	"InstallOutcome":          "job result: built by internal/capabilities/deploy, not internal/wire",
	"PatchApplyParams":        "job params: unmarshalled by internal/capabilities/patch, not internal/wire",
	"PatchApplyResult":        "job result: built by internal/capabilities/patch, not internal/wire",
	"PatchRebootParams":       "job params: unmarshalled by internal/capabilities/patch, not internal/wire",
	"PatchRebootResult":       "job result: built by internal/capabilities/patch, not internal/wire",
	"PatchScanDocument":       "job result: built by internal/capabilities/patch, not internal/wire",
	"PatchScanParams":         "job params: unmarshalled by internal/capabilities/patch, not internal/wire",
	"ProvisionEnrollParams": "job params: unmarshalled by internal/capabilities/provision, not " +
		"internal/wire",
	"ProvisionEnrollResult": "job result: built by internal/capabilities/provision, not internal/wire",
	// Unlike the other entries in this map, ProvisionPrepareEnvelope is not a
	// job params/result pair at all (backend/app/modules/provisioning/schema.py
	// says so explicitly): it is the site-server hand-off contract
	// (docs/modules/provisioning/spec.md §8), and the site-server deployable is
	// not part of this repository. No internal/capabilities package -- now or
	// planned -- owns it, so it keeps its own reason rather than borrowing the
	// "unmarshalled/built by" wording that would name a package that does not
	// consume this schema.
	"ProvisionPrepareEnvelope": "site-server hand-off contract: consumed by the site-server " +
		"deployable, not by any agent capability",
	"RemoteSessionOpenParams": "remote module: superseded by remote.screen.open for console sharing",
	// Screen share: the frames on the relay are internal/rsp, pinned by
	// docs/kernel/wire-protocol/rsp1-vectors.json.
	"RemoteScreenOpenParams": "job params: unmarshalled by internal/capabilities/remote, which " +
		"asserts against this schema in its own schema_test.go",
	"RemoteScreenOpenResult": "job result: built by internal/capabilities/remote, which asserts " +
		"against this schema in its own schema_test.go",
	// Certificate rotation IS built (internal/capabilities/cert), but its params
	// and result documents are unmarshalled by that capability into its own
	// structs after verification -- they are not message envelopes and have no
	// place in internal/wire. Same treatment as InventoryDocument above.
	"AgentCertRenewParams":   "job params: unmarshalled by internal/capabilities/cert, not internal/wire",
	"AgentCertRenewResult":   "job result: built by internal/capabilities/cert, not internal/wire",
	"AgentCertInstallParams": "job params: unmarshalled by internal/capabilities/cert, not internal/wire",
	"AgentCertInstallResult": "job result: built by internal/capabilities/cert, not internal/wire",
	// site.wake IS built (internal/capabilities/site). Same treatment as the
	// cert pair above -- these are job params and a job result, not message
	// envelopes -- but with one difference worth naming, because "not
	// implemented here" is exactly how a contract goes unguarded: these two have
	// their OWN conformance test against these same files, in
	// internal/capabilities/site/schema_test.go. It pins the port enum, the
	// repeat bounds, both defaults, and the ABSENCE of a `woke` field.
	// `job.cancel` IS built (internal/capabilities/jobcancel), and like the
	// cert pair and site.wake these two are job params and a job result rather
	// than message envelopes -- the whole compatibility argument for
	// interruption rests on that (a new job TYPE, refused loudly by an old
	// agent; NOT a new wire `kind`, which an old agent drops in silence). They
	// have their OWN conformance test against these same files, in
	// internal/capabilities/jobcancel/schema_test.go, which pins the outcome
	// enum -- including the one value this agent never emits.
	"JobCancelParams": "job params: unmarshalled by internal/capabilities/jobcancel, which " +
		"asserts against this schema in its own schema_test.go",
	"JobCancelResult": "job result: built by internal/capabilities/jobcancel, which asserts " +
		"against this schema in its own schema_test.go",
	"SiteWakeParams": "job params: unmarshalled by internal/capabilities/site, which asserts " +
		"against this schema in its own schema_test.go",
	"SiteWakeResult": "job result: built by internal/capabilities/site, which asserts against " +
		"this schema in its own schema_test.go",
	// Agent push install IS built (internal/capabilities/pushinstall), and like
	// the cert pair, site.wake and job.cancel these are job params and results
	// rather than message envelopes -- the whole compatibility argument rests on
	// that (new job TYPES refused loudly by an old agent, NOT new wire kinds
	// dropped in silence). They have their OWN conformance test against these
	// same files, in internal/capabilities/pushinstall/schema_test.go, which
	// pins the transport enum, the credential shape and the never-null
	// host_key_fingerprint.
	"AgentPushProbeParams": "job params: unmarshalled by internal/capabilities/pushinstall, " +
		"which asserts against this schema in its own schema_test.go",
	"AgentPushProbeResult": "job result: built by internal/capabilities/pushinstall, which " +
		"asserts against this schema in its own schema_test.go",
	"AgentPushInstallParams": "job params: unmarshalled by internal/capabilities/pushinstall, " +
		"which asserts against this schema in its own schema_test.go",
	"AgentPushInstallResult": "job result: built by internal/capabilities/pushinstall, which " +
		"asserts against this schema in its own schema_test.go",
	// Self-update: a job type, not a message envelope, so an old agent refuses
	// it `unknown_type` rather than dropping it.
	"AgentUpdateParams": "job params: unmarshalled by internal/capabilities/selfupdate, which " +
		"asserts against this schema in its own schema_test.go",
	"AgentUpdateResult": "job result: built by internal/update, which internal/capabilities/" +
		"selfupdate asserts against this schema in its own schema_test.go",
	// Reject's uninstall (install-link spec 2026-09-28 §1.5): a job type, not a
	// message envelope, so an agent older than 1.6.0 refuses it `unknown_type`.
	"AgentUninstallParams": "job params: unmarshalled by internal/capabilities/uninstall, which " +
		"asserts against this schema in its own schema_test.go",
	"AgentUninstallResult": "job result: built by internal/capabilities/uninstall, which asserts " +
		"against this schema in its own schema_test.go",
}

func loadSchemas(t *testing.T) map[string]jsonSchema {
	t.Helper()
	files, err := filepath.Glob(filepath.Join(schemaDir, "*.schema.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(files) < 20 {
		t.Fatalf("only %d schemas found under %s; wrong path?", len(files), schemaDir)
	}
	out := map[string]jsonSchema{}
	for _, f := range files {
		raw, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		var s jsonSchema
		if err := json.Unmarshal(raw, &s); err != nil {
			t.Fatalf("%s: %v", f, err)
		}
		if s.Title == "" {
			t.Fatalf("%s has no title", f)
		}
		out[s.Title] = s
	}
	return out
}

func TestEveryExportedSchemaIsAccountedFor(t *testing.T) {
	schemas := loadSchemas(t)
	for title := range schemas {
		if _, ok := implemented[title]; ok {
			continue
		}
		if reason, ok := notImplemented[title]; ok && strings.TrimSpace(reason) != "" {
			continue
		}
		t.Errorf("schema %s is neither implemented nor listed in notImplemented with a reason", title)
	}
	// The reverse direction: an entry naming a schema that no longer exists is
	// a stale excuse, and a stale excuse is how a deleted contract goes
	// unnoticed.
	for title := range implemented {
		if _, ok := schemas[title]; !ok {
			t.Errorf("implemented names %s, which is not an exported schema", title)
		}
	}
	for title := range notImplemented {
		if _, ok := schemas[title]; !ok {
			t.Errorf("notImplemented names %s, which is not an exported schema", title)
		}
	}
}

// jsonFields returns the json tag names a struct type marshals, and which of
// them are optional (omitempty).
func jsonFields(t *testing.T, typ reflect.Type) (names map[string]bool, optional map[string]bool) {
	t.Helper()
	names, optional = map[string]bool{}, map[string]bool{}
	for i := 0; i < typ.NumField(); i++ {
		f := typ.Field(i)
		if !f.IsExported() {
			continue
		}
		tag := f.Tag.Get("json")
		if tag == "-" {
			continue
		}
		parts := strings.Split(tag, ",")
		name := parts[0]
		if name == "" {
			t.Fatalf("%s.%s has no json tag; wire structs must name every field explicitly",
				typ.Name(), f.Name)
		}
		names[name] = true
		for _, opt := range parts[1:] {
			if opt == "omitempty" {
				optional[name] = true
			}
		}
	}
	return names, optional
}

func TestEveryImplementedSchemaPropertyHasAGoField(t *testing.T) {
	schemas := loadSchemas(t)
	for title, b := range implemented {
		s := schemas[title]
		check := func(t *testing.T, s jsonSchema, typ reflect.Type, framing []string) {
			got, _ := jsonFields(t, typ)
			isFraming := map[string]bool{}
			for _, f := range framing {
				isFraming[f] = true
			}
			for prop := range s.Properties {
				if isFraming[prop] {
					continue
				}
				if !got[prop] {
					t.Errorf("%s: schema property %q has no field in Go type %s",
						title, prop, typ.Name())
				}
			}
			for name := range got {
				if _, ok := s.Properties[name]; !ok {
					t.Errorf("%s: Go type %s emits %q, which the schema does not define "+
						"(the server is extra=\"forbid\"; the whole message would be dropped)",
						title, typ.Name(), name)
				}
			}
			for _, req := range s.Required {
				if isFraming[req] {
					continue
				}
				if !got[req] {
					t.Errorf("%s: required property %q missing from %s", title, req, typ.Name())
				}
			}
		}
		t.Run(title, func(t *testing.T) { check(t, s, b.typ, b.framing) })
		for def, typ := range b.nested {
			nested, ok := s.Defs[def]
			if !ok {
				t.Errorf("%s: $defs has no %s", title, def)
				continue
			}
			t.Run(title+"/"+def, func(t *testing.T) { check(t, nested, typ, nil) })
		}
	}
}

// A required property that is omitempty would be dropped from the encoding
// whenever it holds its zero value, and the server rejects the message. The
// case that bites is HealthReportMsg.policy_version, which is required, has
// minimum 0, and is legitimately 0 on a freshly enrolled agent.
func TestRequiredOutboundPropertiesAreNeverOmitempty(t *testing.T) {
	schemas := loadSchemas(t)
	for title, b := range implemented {
		s := schemas[title]
		check := func(t *testing.T, s jsonSchema, typ reflect.Type) {
			_, optional := jsonFields(t, typ)
			for _, req := range s.Required {
				if optional[req] {
					t.Errorf("%s: %s.%s is required by the schema but tagged omitempty",
						title, typ.Name(), req)
				}
			}
		}
		check(t, s, b.typ)
		for def, typ := range b.nested {
			if nested, ok := s.Defs[def]; ok {
				check(t, nested, typ)
			}
		}
	}
}

// The encoded key set of every outbound message must be a subset of what the
// schema defines and a superset of what it requires. `_Msg` is extra="forbid"
// server-side: an extra key does not get ignored, it gets the WHOLE message
// dropped at parse_agent_message, and the only trace is a logger.warning on
// the server (gateway/main.py:200-202).
func TestOutboundEncodingsCarryExactlyThePermittedKeys(t *testing.T) {
	schemas := loadSchemas(t)
	cases := map[string]any{
		"JobStatusMsg": wire.JobStatus{JobID: "j"},
		"JobLogMsg":    wire.JobLog{JobID: "j", Msg: "m"},
		"JobResultMsg": wire.JobResult{JobID: "j", State: wire.StateSucceeded},
		"HeartbeatMsg": wire.Heartbeat{TS: "2026-08-05T00:00:00Z"},
		"SecurityEventsMsg": wire.SecurityEvents{Events: []wire.SecurityEvent{
			{EventUID: "u", TS: "2026-08-05T00:00:00Z", EventType: wire.EventLogonSuccess},
		}},
		"HealthReportMsg": wire.HealthReport{PolicyVersion: 0, Reports: []wire.HealthReportLine{
			{CheckID: "c", Kind: "summary", State: "ok", ObservedAt: "2026-08-05T00:00:00Z"},
		}},
		"BackupChunkQueryMsg": wire.ChunkQuery{JobID: "j", QueryID: "q"},
	}
	// Everything the agent can send must be exercised here, or a message kind
	// could ship without its key set ever being checked.
	if len(cases) != len(implemented)-2 { // -2: JobEnvelope and BackupChunkGrantMsg are inbound only
		t.Fatalf("%d outbound cases for %d implemented schemas (JobEnvelope and "+
			"BackupChunkGrantMsg are inbound)", len(cases), len(implemented))
	}
	for title, msg := range cases {
		t.Run(title, func(t *testing.T) {
			s := schemas[title]
			b, err := wire.Encode(msg)
			if err != nil {
				t.Fatal(err)
			}
			var got map[string]json.RawMessage
			if err := json.Unmarshal(b, &got); err != nil {
				t.Fatal(err)
			}
			for k := range got {
				if _, ok := s.Properties[k]; !ok {
					t.Errorf("encoded key %q is not in the schema: %s", k, b)
				}
			}
			for _, req := range s.Required {
				if _, ok := got[req]; !ok {
					t.Errorf("encoded message omits required key %q: %s", req, b)
				}
			}
			var keys []string
			for k := range got {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			t.Logf("%s -> %v", title, keys)
		})
	}
}

// MAX_CHUNK_BATCH is 1024 on both sides. A server that tightens the bound and
// an agent that does not would send batches the control plane drops at the
// parse layer, and the run would hang on an answer that never comes. Pinned to
// the exported schema so the two cannot drift silently.
func TestChunkBatchBoundMatchesTheSchema(t *testing.T) {
	schemas := loadSchemas(t)
	for _, tc := range []struct{ title, prop string }{
		{"BackupChunkQueryMsg", "check"},
		{"BackupChunkQueryMsg", "stored"},
		{"BackupChunkGrantMsg", "new"},
	} {
		got := schemas[tc.title].Properties[tc.prop].MaxItems
		if got != 1024 {
			t.Errorf("%s.%s maxItems = %d, want 1024", tc.title, tc.prop, got)
		}
	}
}
