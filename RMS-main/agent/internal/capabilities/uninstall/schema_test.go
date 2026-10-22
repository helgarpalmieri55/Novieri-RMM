package uninstall_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/capabilities/uninstall"
)

// The exported schema is the contract (Task 9 generated it from the Pydantic
// models; backend/tests/test_wire_schema_export.py keeps it current). This is
// the Go-side guard, listed in internal/wire's notImplemented map.

const schemaDir = "../../../../docs/kernel/wire-protocol"

type jsonSchema struct {
	Title                string                     `json:"title"`
	Properties           map[string]json.RawMessage `json:"properties"`
	Required             []string                   `json:"required"`
	AdditionalProperties *bool                      `json:"additionalProperties"`
}

func load(t *testing.T, title string) jsonSchema {
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
		t.Fatalf("title = %q, want %q", s.Title, title)
	}
	return s
}

func tags(v any) []string {
	rt := reflect.TypeOf(v)
	out := []string{}
	for i := 0; i < rt.NumField(); i++ {
		tag := rt.Field(i).Tag.Get("json")
		for j := 0; j < len(tag); j++ {
			if tag[j] == ',' {
				tag = tag[:j]
				break
			}
		}
		out = append(out, tag)
	}
	return out
}

func TestTheParamsCarryNothing(t *testing.T) {
	s := load(t, "AgentUninstallParams")
	if len(s.Properties) != 0 {
		t.Fatalf("AgentUninstallParams carries %v; a field here is a second input to a "+
			"command path that deletes files, and needs a review that knows it", s.Properties)
	}
	if s.AdditionalProperties == nil || *s.AdditionalProperties {
		t.Fatal("AgentUninstallParams is not extra=forbid server-side")
	}
	if got := tags(uninstall.ParamsShape{}); len(got) != 0 {
		t.Fatalf("this package's params struct carries %v", got)
	}
}

func TestTheResultIsScheduled(t *testing.T) {
	s := load(t, "AgentUninstallResult")
	if _, ok := s.Properties["scheduled"]; !ok || len(s.Properties) != 1 {
		t.Fatalf("AgentUninstallResult properties = %v", s.Properties)
	}
	if !reflect.DeepEqual(s.Required, []string{"scheduled"}) {
		t.Fatalf("required = %v", s.Required)
	}
	if got := tags(uninstall.ResultShape{}); !reflect.DeepEqual(got, []string{"scheduled"}) {
		t.Fatalf("this package's result struct carries %v", got)
	}
}
