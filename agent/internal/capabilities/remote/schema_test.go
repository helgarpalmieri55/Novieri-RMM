package remote

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// The exported schemas are the contract with the server
// (app/modules/remote/jobtypes.py via scripts/export_wire_schema.py). These
// tests hold Params and Result to them.

const schemaDir = "../../../../docs/kernel/wire-protocol"

type schema struct {
	Title      string            `json:"title"`
	Properties map[string]schema `json:"properties"`
	Required   []string          `json:"required"`
	Enum       []string          `json:"enum"`
	Minimum    *int              `json:"minimum"`
	Maximum    *int              `json:"maximum"`
	Ref        string            `json:"$ref"`
	Defs       map[string]schema `json:"$defs"`
}

func load(t *testing.T, title string) schema {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(schemaDir, title+".schema.json"))
	if err != nil {
		t.Fatal(err)
	}
	var s schema
	if err := json.Unmarshal(raw, &s); err != nil {
		t.Fatal(err)
	}
	return s
}

func tags(v any) []string {
	rt := reflect.TypeOf(v)
	var out []string
	for i := 0; i < rt.NumField(); i++ {
		if tag := strings.Split(rt.Field(i).Tag.Get("json"), ",")[0]; tag != "" && tag != "-" {
			out = append(out, tag)
		}
	}
	sort.Strings(out)
	return out
}

func keys(m map[string]schema) []string {
	var out []string
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func TestParamsReadEveryMemberOfTheSchema(t *testing.T) {
	s := load(t, "RemoteScreenOpenParams")
	if got, want := tags(Params{}), keys(s.Properties); !reflect.DeepEqual(got, want) {
		t.Fatalf("Params members %v; schema %v", got, want)
	}
	consent := s.Defs["ScreenConsent"]
	if got, want := tags(Consent{}), keys(consent.Properties); !reflect.DeepEqual(got, want) {
		t.Fatalf("Consent members %v; schema %v", got, want)
	}
	for name, b := range map[string]struct {
		s        schema
		min, max int
	}{
		"max_duration_s": {s.Properties["max_duration_s"], minDurationS, maxDurationS},
		"timeout_s":      {consent.Properties["timeout_s"], minConsentS, maxConsentS},
	} {
		if b.s.Minimum == nil || b.s.Maximum == nil || *b.s.Minimum != b.min || *b.s.Maximum != b.max {
			t.Errorf("%s bounds: schema %v..%v, agent %d..%d", name, b.s.Minimum, b.s.Maximum, b.min, b.max)
		}
	}
	if modes := s.Properties["consent_mode"].Enum; !reflect.DeepEqual(modes, []string{"attended", "unattended"}) {
		t.Errorf("consent_mode enum %v", modes)
	}
}

func TestResultIsWhatTheServerAccepts(t *testing.T) {
	s := load(t, "RemoteScreenOpenResult")
	for _, tag := range tags(Result{}) {
		if _, ok := s.Properties[tag]; !ok {
			t.Errorf("Result member %q is not in the schema", tag)
		}
	}
	allowed := map[string]bool{}
	for _, v := range s.Properties["outcome"].Enum {
		allowed[v] = true
	}
	// Every outcome this package can produce, including through outcomeFor.
	produced := []string{"ended", "denied", "consent_timeout", "no_user_session", "relay_refused", "expired", "error"}
	for _, r := range []string{"completed", "ended_by_user", "no_user_session", "capture_unavailable",
		"permission_missing", "unsupported_display", "expired", "error", "anything else"} {
		produced = append(produced, outcomeFor(r))
	}
	for _, o := range produced {
		if !allowed[o] {
			t.Errorf("outcome %q is not in the schema's enum %v", o, s.Properties["outcome"].Enum)
		}
	}
}
