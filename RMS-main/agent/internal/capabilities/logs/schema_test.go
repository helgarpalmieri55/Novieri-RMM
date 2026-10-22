package logs

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// THE EXPORTED SCHEMA IS THE CONTRACT, AND THIS FILE IS WHERE THAT IS ENFORCED
// RATHER THAN ASSERTED.
//
// # A FINDING, RECORDED HERE BECAUSE THIS IS WHERE IT BITES
//
// There is NO `LogsShipParams.schema.json` and no `LogsShipResult.schema.json`
// in docs/kernel/wire-protocol/. `scripts/export_wire_schema.py` exports the
// job-params models of the modules that have them; `logs.ship` is registered in
// app/jobs/registry.py:148 with an EMPTY `LogsShipParams` and NO result
// handler, and neither model was ever exported.
//
// So for the params half, the Go side is built against the Pydantic model
// directly (`class LogsShipParams(BaseModel): model_config =
// ConfigDict(extra="forbid")` -- no fields), and this file pins the ONE
// property that model has: emptiness. That is checked here by asserting that
// this package reads no params member at all, which is the agent-side
// equivalent and is the property that actually matters -- a params member on
// `logs.ship` would be a second, unversioned way to configure what an agent
// reads off an endpoint.
//
// For the RESULT half there is no server-side model at all, so there is nothing
// to drift from: the result is stored on the job row and read by a human. What
// is pinned instead is that it carries no event content (logs_test.go).
//
// What IS exported, and what this file checks against the file, is
// `SecurityEventsMsg.schema.json` -- the message that carries the evidence.
// internal/wire implements `wire.SecurityEvents` and internal/wire/schema_test.go
// guards that struct against the schema; what THIS file adds is the guard on
// the VALUES this package puts in it: the taxonomy, the batch bound, and the
// three length limits. A struct that matches the schema and an event_type the
// server has never heard of would pass every test in internal/wire and lose
// 500 events per batch in production.

const schemaDir = "../../../../docs/kernel/wire-protocol"

type jsonSchema struct {
	Title      string                `json:"title"`
	Defs       map[string]jsonSchema `json:"$defs"`
	Properties map[string]jsonSchema `json:"properties"`
	Required   []string              `json:"required"`
	Enum       []json.RawMessage     `json:"enum"`
	Const      json.RawMessage       `json:"const"`
	MaxItems   *int                  `json:"maxItems"`
	MinItems   *int                  `json:"minItems"`
	MaxLength  *int                  `json:"maxLength"`
	MinLength  *int                  `json:"minLength"`
	Default    json.RawMessage       `json:"default"`
}

func loadSchema(t *testing.T, title string) jsonSchema {
	t.Helper()
	path := filepath.Join(schemaDir, title+".schema.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("the exported contract is missing: %v", err)
	}
	var s jsonSchema
	if err := json.Unmarshal(raw, &s); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
	if s.Title != title {
		t.Fatalf("%s carries title %q", path, s.Title)
	}
	return s
}

func TestLogsShipHasNoExportedParamsOrResultSchemaAndThatIsRecorded(t *testing.T) {
	// This test EXISTS TO FAIL LOUDLY IF THAT CHANGES. The moment
	// `LogsShipParams.schema.json` appears, this package must be built against
	// it like every other capability is -- and the most likely reason for it to
	// appear is that somebody gave `logs.ship` a parameter, which is exactly the
	// change that needs a Go-side guard.
	//
	// FALSIFYING OBSERVATION: either file existing.
	for _, title := range []string{"LogsShipParams", "LogsShipResult"} {
		path := filepath.Join(schemaDir, title+".schema.json")
		if _, err := os.Stat(path); err == nil {
			t.Fatalf("%s now exists. `logs.ship` has grown a params or result contract; this "+
				"package must be built against the exported file and schema_test.go's premise "+
				"-- that there is nothing to build against -- is no longer true", path)
		}
	}
}

func TestTheEventTypesThisAgentEmitsAreExactlyTheServersTaxonomy(t *testing.T) {
	// THE SHARPEST TEST IN THIS FILE. `ingest_events` rejects the WHOLE BATCH on
	// an event_type outside the taxonomy and audits `ingest_rejected`. An agent
	// that emitted one unknown type would lose 500 events of evidence per batch
	// and would look, from the endpoint, like it was working perfectly.
	//
	// FALSIFYING OBSERVATION: a type this package can emit that the exported
	// enum does not contain.
	s := loadSchema(t, "SecurityEventsMsg")
	doc, ok := s.Defs["SecurityEventDoc"]
	if !ok {
		t.Fatal("SecurityEventsMsg no longer defines SecurityEventDoc")
	}
	allowed := map[string]bool{}
	for _, raw := range doc.Properties["event_type"].Enum {
		var v string
		if err := json.Unmarshal(raw, &v); err != nil {
			t.Fatal(err)
		}
		allowed[v] = true
	}
	if len(allowed) != 7 {
		t.Fatalf("the exported taxonomy has %d members; spec §2 names seven", len(allowed))
	}

	emitted := map[string]bool{}
	for _, entry := range eventTaxonomy {
		emitted[entry.eventType] = true
	}
	for _, r := range unifiedRules {
		emitted[r.eventType] = true
	}
	for got := range emitted {
		if !allowed[got] {
			t.Errorf("this agent can emit event_type %q, which the exported taxonomy does not "+
				"contain. `ingest_events` would reject the WHOLE batch it appeared in", got)
		}
	}
}

func TestTheOutcomesThisAgentEmitsAreExactlyTheServersEnum(t *testing.T) {
	s := loadSchema(t, "SecurityEventsMsg")
	doc := s.Defs["SecurityEventDoc"]
	allowed := map[string]bool{}
	for _, raw := range doc.Properties["outcome"].Enum {
		var v string
		if err := json.Unmarshal(raw, &v); err != nil {
			t.Fatal(err)
		}
		allowed[v] = true
	}
	emitted := map[string]bool{}
	for _, entry := range eventTaxonomy {
		emitted[entry.outcome] = true
	}
	for _, r := range unifiedRules {
		emitted[r.outcome] = true
	}
	for got := range emitted {
		if !allowed[got] {
			var have []string
			for a := range allowed {
				have = append(have, a)
			}
			sort.Strings(have)
			t.Errorf("this agent emits outcome %q; the schema permits %v", got, have)
		}
	}
}

func TestTheBatchBoundAndTheLengthLimitsAreTheExportedSchemasAndNotThisPackagesOpinion(t *testing.T) {
	// These constants are duplicated agent-side on purpose: a bound that lives
	// only in the server's model does not travel with the job in any form the
	// endpoint can enforce, and every one of these is the difference between
	// losing ONE field and losing the WHOLE 500-event message (Pydantic
	// validates the message, not the event).
	//
	// FALSIFYING OBSERVATION: a schema bound this package's constants disagree
	// with.
	s := loadSchema(t, "SecurityEventsMsg")
	events := s.Properties["events"]
	if events.MaxItems == nil || *events.MaxItems != maxBatch {
		t.Errorf("SecurityEventsMsg.events maxItems = %s; this package flushes at %d",
			show(events.MaxItems), maxBatch)
	}
	if events.MinItems == nil || *events.MinItems != 1 {
		t.Errorf("SecurityEventsMsg.events minItems = %s; this package never sends an empty "+
			"batch and relies on that being illegal", show(events.MinItems))
	}

	doc := s.Defs["SecurityEventDoc"]
	for _, tc := range []struct {
		prop string
		have int
	}{
		{"event_uid", maxEventUIDLen},
		{"username", maxUsernameLen},
		{"source", maxSourceLen},
	} {
		got := doc.Properties[tc.prop].MaxLength
		if got == nil || *got != tc.have {
			t.Errorf("SecurityEventDoc.%s maxLength = %s; this package clamps at %d",
				tc.prop, show(got), tc.have)
		}
	}
	if ml := doc.Properties["event_uid"].MinLength; ml == nil || *ml != 1 {
		t.Errorf("SecurityEventDoc.event_uid minLength = %s; an empty uid would fail the whole "+
			"message and this package must never produce one", show(ml))
	}
}

// show renders an optional schema bound as its VALUE. Printing the pointer --
// which is what %v does, and what an earlier version of this file did -- turns
// every failure here into "maxItems = 0xc0002e92e0", which names nothing.
func show(v *int) string {
	if v == nil {
		return "absent"
	}
	return strconv.Itoa(*v)
}

func TestEveryEventThisPackageBuildsCarriesTheThreeRequiredMembers(t *testing.T) {
	// `event_uid`, `ts` and `event_type` are required. A missing one fails
	// validation for the whole message.
	//
	// FALSIFYING OBSERVATION: an empty required field on any fixture.
	s := loadSchema(t, "SecurityEventsMsg")
	required := s.Defs["SecurityEventDoc"].Required
	if want := []string{"event_uid", "ts", "event_type"}; !reflect.DeepEqual(required, want) {
		t.Fatalf("SecurityEventDoc requires %v, this test assumes %v", required, want)
	}
	for _, name := range []string{
		"4624-network-logon.xml", "4625-failed-logon.xml",
		"4672-privilege.xml", "4732-admin-group-change.xml",
	} {
		ev, ok := parseFixture(t, name)
		if !ok {
			t.Errorf("%s was dropped", name)
			continue
		}
		ev = clampEvent(ev)
		if ev.EventUID == "" || ev.TS == "" || ev.EventType == "" {
			t.Errorf("%s produced event_uid=%q ts=%q event_type=%q", name, ev.EventUID, ev.TS,
				ev.EventType)
		}
	}
}

func TestTheOnlyOutboundKindThisPackageUsesIsSecurityEvents(t *testing.T) {
	// wire.Encode writes `v` and `kind` itself and refuses to frame anything
	// that is not one of the seven declared outbound types, so this cannot be got
	// wrong silently. What it CAN be got wrong as is a capability that sends a
	// JobLog carrying a username -- which would be event content on a path with
	// none of the retention or access controls the security_events table has.
	//
	// FALSIFYING OBSERVATION: an encode failure, or this package referencing
	// another outbound type. The first half is checked here; the second is
	// checked by the log-line test in logs_test.go.
	if _, err := wire.Encode(wire.SecurityEvents{Events: []wire.SecurityEvent{{
		EventUID: "win:1:20260718T091203Z", TS: "2026-07-18T09:12:03.000000Z",
		EventType: "logon_success",
	}}}); err != nil {
		t.Fatalf("wire.Encode refused the message this package sends: %v", err)
	}
}

func TestTheDefaultsThisPackageReliesOnAreStillTheSchemasDefaults(t *testing.T) {
	// `username`, `source` and `outcome` all default server-side. This package
	// emits `outcome` on every event and may emit an EMPTY username (a macOS
	// rule with no extractor, a Windows record whose target is "-"), which is
	// legal only because the default is "".
	//
	// FALSIFYING OBSERVATION: a default that is no longer the empty string /
	// "n_a", which would make an omitted key mean something else.
	doc := loadSchema(t, "SecurityEventsMsg").Defs["SecurityEventDoc"]
	for prop, want := range map[string]string{"username": `""`, "source": `""`, "outcome": `"n_a"`} {
		got := strings.TrimSpace(string(doc.Properties[prop].Default))
		if got != want {
			t.Errorf("SecurityEventDoc.%s default = %s, want %s", prop, got, want)
		}
	}
}
