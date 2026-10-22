package site

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// THE EXPORTED SCHEMA IS THE CONTRACT, AND THIS FILE IS WHERE THAT IS ENFORCED
// RATHER THAN ASSERTED.
//
// `docs/kernel/wire-protocol/SiteWakeParams.schema.json` and
// `SiteWakeResult.schema.json` are generated from the Pydantic models by
// `scripts/export_wire_schema.py`, and `backend/tests/test_wire_schema_export.py`
// keeps them current with those models. This package's structs are built
// against THOSE FILES -- not against a reading of schema.py -- so the chain is
// model -> schema -> struct with a guard on each link.
//
// internal/wire has the same guard for the message envelopes, but it covers
// only the types internal/wire implements: SiteWakeParams and SiteWakeResult
// are job params and a job result, unmarshalled and built by this capability,
// so they are listed in that file's `notImplemented` map and checked here
// instead. Without this file the site.wake contract would have no Go-side
// guard at all.
//
// WHAT THIS CATCHES THAT A REVIEW DOES NOT: a control-plane change that widens
// `port`, raises `repeat`'s maximum, adds a required member, or -- the one that
// matters most -- adds `woke` back to the result. Any of those changes a file
// here and turns this package red, which is what a compatibility decision is
// supposed to feel like.

const schemaDir = "../../../../docs/kernel/wire-protocol"

type jsonSchema struct {
	Title                string                `json:"title"`
	Type                 string                `json:"type"`
	Properties           map[string]jsonSchema `json:"properties"`
	Required             []string              `json:"required"`
	AdditionalProperties *bool                 `json:"additionalProperties"`
	Enum                 []json.RawMessage     `json:"enum"`
	Default              json.RawMessage       `json:"default"`
	Minimum              *int                  `json:"minimum"`
	Maximum              *int                  `json:"maximum"`
	Format               string                `json:"format"`
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

// jsonTags returns the json tag names a struct marshals, and which are pointers.
func jsonTags(t *testing.T, typ reflect.Type) (names map[string]bool, pointer map[string]bool) {
	t.Helper()
	names, pointer = map[string]bool{}, map[string]bool{}
	for i := 0; i < typ.NumField(); i++ {
		f := typ.Field(i)
		tag := f.Tag.Get("json")
		if tag == "" || tag == "-" {
			t.Fatalf("%s.%s has no json tag; wire structs name every field explicitly",
				typ.Name(), f.Name)
		}
		name := strings.Split(tag, ",")[0]
		names[name] = true
		pointer[name] = f.Type.Kind() == reflect.Ptr
		if strings.Contains(tag, "omitempty") {
			t.Fatalf("%s.%s is omitempty; SiteWakeResult is extra=\"forbid\" and every one of "+
				"its members is required, so an omitted key fails the whole job", typ.Name(), f.Name)
		}
	}
	return names, pointer
}

func TestTheParamsStructImplementsExactlyTheExportedSiteWakeParams(t *testing.T) {
	s := loadSchema(t, "SiteWakeParams")
	got, pointer := jsonTags(t, reflect.TypeOf(params{}))

	for prop := range s.Properties {
		if !got[prop] {
			t.Errorf("SiteWakeParams property %q has no field in the Go params struct; the "+
				"server may already be sending it", prop)
		}
	}
	for name := range got {
		if _, ok := s.Properties[name]; !ok {
			t.Errorf("the Go params struct reads %q, which the exported schema does not define", name)
		}
	}

	// A REQUIRED property must not be a pointer and an OPTIONAL one must be.
	// The optional numbers have non-zero schema defaults, so a plain int would
	// make "absent" read as 0 -- which portAllowed then refuses, turning every
	// wake from a control plane that omits the member into a refusal.
	required := map[string]bool{}
	for _, r := range s.Required {
		required[r] = true
		if !got[r] {
			t.Errorf("required property %q is missing from the Go params struct", r)
		}
	}
	for name := range got {
		if required[name] && pointer[name] {
			t.Errorf("params.%s is required by the schema and is a pointer", name)
		}
		if !required[name] && !pointer[name] {
			t.Errorf("params.%s is optional with a non-zero schema default and is NOT a pointer; "+
				"'absent' and 'zero' would become the same statement", name)
		}
	}

	if s.AdditionalProperties == nil || *s.AdditionalProperties {
		t.Error("SiteWakeParams is no longer additionalProperties:false; the server used to " +
			"forbid extra members")
	}
}

func TestThePortEnumAndRepeatBoundsAreTheExportedSchemasAndNotThisPackagesOpinion(t *testing.T) {
	// The bounds are duplicated agent-side (site.go) because a bound that lives
	// only in the server's model does not travel with the job in any form the
	// endpoint can enforce. Duplication drifts, so this test pins the copy to
	// the exported file.
	//
	// FALSIFYING OBSERVATION: a schema enum or bound that this package's
	// constants disagree with.
	s := loadSchema(t, "SiteWakeParams")

	port := s.Properties["port"]
	var enum []int
	for _, raw := range port.Enum {
		var v int
		if err := json.Unmarshal(raw, &v); err != nil {
			t.Fatalf("port enum entry %s: %v", raw, err)
		}
		enum = append(enum, v)
	}
	sort.Ints(enum)
	if len(enum) != 2 || enum[0] != 7 || enum[1] != 9 {
		t.Fatalf("SiteWakeParams.port enum = %v; this package permits exactly 7 and 9, and a "+
			"free destination port turns a wake into an arbitrary-UDP-datagram primitive", enum)
	}
	for _, p := range enum {
		if !portAllowed(p) {
			t.Errorf("the schema permits port %d and portAllowed refuses it", p)
		}
	}
	for _, p := range []int{0, 1, 8, 22, 69, 4011, 65535} {
		if portAllowed(p) {
			t.Errorf("portAllowed permits port %d, which the schema enum does not", p)
		}
	}
	assertDefault(t, "port", port.Default, defaultPort)

	repeat := s.Properties["repeat"]
	if repeat.Minimum == nil || *repeat.Minimum != minRepeat {
		t.Errorf("SiteWakeParams.repeat minimum = %v; this package uses %d", repeat.Minimum, minRepeat)
	}
	if repeat.Maximum == nil || *repeat.Maximum != maxRepeat {
		t.Errorf("SiteWakeParams.repeat maximum = %v; this package uses %d", repeat.Maximum, maxRepeat)
	}
	assertDefault(t, "repeat", repeat.Default, defaultRepeat)
}

func assertDefault(t *testing.T, name string, raw json.RawMessage, want int) {
	t.Helper()
	if len(raw) == 0 {
		t.Errorf("SiteWakeParams.%s no longer declares a default; this package assumes %d", name, want)
		return
	}
	var got int
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Errorf("SiteWakeParams.%s default %s: %v", name, raw, err)
		return
	}
	if got != want {
		t.Errorf("SiteWakeParams.%s default is %d and this package uses %d; an agent that "+
			"substituted a different default would send to somewhere the server did not intend",
			name, got, want)
	}
}

func TestTheResultStructImplementsExactlyTheExportedSiteWakeResult(t *testing.T) {
	s := loadSchema(t, "SiteWakeResult")
	got, pointer := jsonTags(t, reflect.TypeOf(result{}))

	for prop := range s.Properties {
		if !got[prop] {
			t.Errorf("SiteWakeResult property %q has no field in the Go result struct", prop)
		}
	}
	for name := range got {
		if _, ok := s.Properties[name]; !ok {
			t.Errorf("the Go result struct emits %q, which the schema does not define. The server "+
				"is extra=\"forbid\": handle_wake_result raises SiteWakeRejected and the WHOLE "+
				"job fails", name)
		}
		if pointer[name] {
			t.Errorf("result.%s is a pointer; every SiteWakeResult member is required", name)
		}
	}
	for _, r := range s.Required {
		if !got[r] {
			t.Errorf("required result property %q is missing from the Go result struct", r)
		}
	}
	if len(s.Required) != 3 {
		t.Errorf("SiteWakeResult now requires %v; this package builds exactly {sent, broadcast, "+
			"sent_at}", s.Required)
	}

	// §0.9, pinned in the direction that matters. The absence of `woke` is a
	// decision -- "a result field that cannot be true information is worse than
	// a missing one" -- and a schema that grew one would mean the control plane
	// had decided a magic packet is acknowledged, which it is not.
	for _, forbidden := range []string{"woke", "reachable", "retries", "rtt_ms", "responded"} {
		if _, ok := s.Properties[forbidden]; ok {
			t.Errorf("SiteWakeResult now defines %q. A magic packet is unauthenticated and "+
				"unacknowledged: there is no reply to wait for, so this field cannot carry true "+
				"information (spec §0.9)", forbidden)
		}
	}

	if f := s.Properties["sent_at"].Format; f != "date-time" {
		t.Errorf("SiteWakeResult.sent_at format = %q, want date-time; this package emits %q",
			f, rfc3339)
	}
}
