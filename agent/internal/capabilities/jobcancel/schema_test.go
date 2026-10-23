package jobcancel_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/capabilities/jobcancel"
)

// THE EXPORTED SCHEMA IS THE CONTRACT, AND THIS FILE IS WHERE THAT IS ENFORCED
// FOR `job.cancel` RATHER THAN ASSERTED.
//
// `docs/kernel/wire-protocol/JobCancelParams.schema.json` and
// `JobCancelResult.schema.json` are generated from the Pydantic models by
// `scripts/export_wire_schema.py`, and `backend/tests/test_wire_schema_export.py`
// keeps them current with those models. This package's structs are built
// against THOSE FILES, so the chain is model -> schema -> struct with a guard
// on each link.
//
// internal/wire's own conformance test covers only the message envelopes; these
// two are job params and a job result, so they are listed in that file's
// `notImplemented` map and checked here instead. Without this file the
// interruption contract would have no Go-side guard at all -- and interruption
// is the cycle whose entire safety argument is a compatibility claim.
//
// WHAT THIS CATCHES THAT A REVIEW DOES NOT: a control plane that renames
// `target_job_id`, adds a required member the fleet does not send, or -- the
// one that matters most -- NARROWS the outcome enum. An agent that emits a
// value the server has stopped accepting has its answer dropped at
// `parse_agent_message`, and the job hangs to expiry with a `logger.warning`.

const schemaDir = "../../../../docs/kernel/wire-protocol"

type jsonSchema struct {
	Title                string                `json:"title"`
	Type                 string                `json:"type"`
	Properties           map[string]jsonSchema `json:"properties"`
	Required             []string              `json:"required"`
	AdditionalProperties *bool                 `json:"additionalProperties"`
	Enum                 []string              `json:"enum"`
}

func loadSchema(t *testing.T, title string) jsonSchema {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(schemaDir, title+".schema.json"))
	if err != nil {
		t.Fatalf("the exported contract is missing: %v", err)
	}
	var s jsonSchema
	if err := json.Unmarshal(raw, &s); err != nil {
		t.Fatal(err)
	}
	if s.Title != title {
		t.Fatalf("schema title is %q, want %q", s.Title, title)
	}
	return s
}

// jsonNames is the set of `json:` tags on a struct, which is what actually
// travels. Reading the tags rather than the field names is the point: a field
// renamed in Go without its tag changing is not a wire change, and one whose
// tag changes is, whatever the field is called.
func jsonNames(v any) []string {
	rt := reflect.TypeOf(v)
	out := make([]string, 0, rt.NumField())
	for i := 0; i < rt.NumField(); i++ {
		tag := rt.Field(i).Tag.Get("json")
		if tag == "" || tag == "-" {
			continue
		}
		for j := 0; j < len(tag); j++ {
			if tag[j] == ',' {
				tag = tag[:j]
				break
			}
		}
		out = append(out, tag)
	}
	sort.Strings(out)
	return out
}

func TestParamsCarryExactlyTheSignedTarget(t *testing.T) {
	s := loadSchema(t, "JobCancelParams")
	want := []string{"target_job_id"}
	got := make([]string, 0, len(s.Properties))
	for name := range s.Properties {
		got = append(got, name)
	}
	sort.Strings(got)
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("JobCancelParams carries %v, want %v -- a second field here is a "+
			"second input to a command path, and needs a review that knows it", got, want)
	}
	if !reflect.DeepEqual(s.Required, want) {
		t.Fatalf("required = %v, want %v", s.Required, want)
	}
	if s.AdditionalProperties == nil || *s.AdditionalProperties {
		t.Fatal("JobCancelParams is not extra=forbid server-side; an unknown member " +
			"would be signed and ignored rather than refused")
	}
	if got := jsonNames(jobcancel.ParamsShape{}); !reflect.DeepEqual(got, want) {
		t.Fatalf("this package's params struct carries %v, want %v", got, want)
	}
}

func TestTheResultCarriesTheTargetAndTheOutcome(t *testing.T) {
	s := loadSchema(t, "JobCancelResult")
	want := []string{"outcome", "target_job_id"}
	got := make([]string, 0, len(s.Properties))
	for name := range s.Properties {
		got = append(got, name)
	}
	sort.Strings(got)
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("JobCancelResult carries %v, want %v", got, want)
	}
	if got := jsonNames(jobcancel.ResultShape{}); !reflect.DeepEqual(got, want) {
		t.Fatalf("this package's result struct carries %v, want %v", got, want)
	}
}

func TestTheOutcomeEnumIsTheServersAndIncludesTheOneThisAgentNeverSends(t *testing.T) {
	s := loadSchema(t, "JobCancelResult")
	outcome, ok := s.Properties["outcome"]
	if !ok {
		t.Fatal("JobCancelResult has no `outcome` property")
	}
	got := append([]string(nil), outcome.Enum...)
	sort.Strings(got)
	want := []string{"already_finished", "dropped_before_start", "interrupted", "unknown"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("outcome enum is %v, want %v", got, want)
	}
	// `dropped_before_start` IS IN THE CONTRACT AND THIS AGENT NEVER EMITS IT
	// (internal/jobs/interrupt.go). It stays because narrowing an enum an agent
	// may already send is a break, and because a future agent that tracks its
	// own dispatcher queue can answer more precisely with no server change.
	// Removing it from the schema would turn that future agent's answer into a
	// dropped message and a job that hangs to expiry.
	for _, v := range want {
		if v == "dropped_before_start" {
			return
		}
	}
	t.Fatal("`dropped_before_start` has been dropped from the contract")
}
