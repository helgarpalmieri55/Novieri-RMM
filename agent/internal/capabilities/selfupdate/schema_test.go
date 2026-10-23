package selfupdate

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/update"
)

const schemaDir = "../../../../docs/kernel/wire-protocol"

type jsonSchema struct {
	Title      string                     `json:"title"`
	Properties map[string]json.RawMessage `json:"properties"`
	Required   []string                   `json:"required"`
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
	return s
}

func jsonTags(v any) []string {
	var out []string
	rt := reflect.TypeOf(v)
	for i := 0; i < rt.NumField(); i++ {
		name, _, _ := strings.Cut(rt.Field(i).Tag.Get("json"), ",")
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

func keys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// Every param the server can send is one this agent reads, and the required
// set is exactly what parse() insists on.
func TestParamsMatchTheExportedSchema(t *testing.T) {
	s := loadSchema(t, "AgentUpdateParams")
	if got, want := jsonTags(Params{}), keys(s.Properties); !reflect.DeepEqual(got, want) {
		t.Fatalf("Params fields %v != schema properties %v", got, want)
	}
	required := append([]string(nil), s.Required...)
	sort.Strings(required)
	want := []string{"arch", "artifact_sha256", "artifact_size", "artifact_url", "os", "target_version"}
	if !reflect.DeepEqual(required, want) {
		t.Fatalf("schema requires %v; parse() requires %v", required, want)
	}
}

// Every key this agent reports is one the schema names.
func TestResultKeysAreInTheExportedSchema(t *testing.T) {
	s := loadSchema(t, "AgentUpdateResult")
	st := &update.State{JobID: "j", FromVersion: "1.0.0", ToVersion: "1.1.0"}
	for _, body := range []map[string]any{
		update.ResultBody(st, update.OutcomeUpdated),
		{"reason": "no_confirmation"},
	} {
		for k := range body {
			if _, ok := s.Properties[k]; !ok {
				t.Errorf("the agent reports %q, which AgentUpdateResult does not define", k)
			}
		}
	}
}
