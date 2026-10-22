package provision

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
// `docs/kernel/wire-protocol/ProvisionEnrollParams.schema.json` and
// `ProvisionEnrollResult.schema.json` are generated from the Pydantic models in
// `app/modules/provisioning/schema.py` by `scripts/export_wire_schema.py`, and
// `backend/tests/test_wire_schema_export.py` keeps them current with those
// models. This package's structs are built against THOSE FILES -- not against a
// reading of schema.py -- so the chain is model -> schema -> struct with a guard
// on each link.
//
// internal/wire has the same guard for the message envelopes, but it covers
// only the types internal/wire implements. ProvisionEnrollParams and
// ProvisionEnrollResult are job params and a job result, unmarshalled and built
// by this capability, so without this file the `provision.enroll` contract would
// have no Go-side guard at all.
//
// WHAT THIS CATCHES THAT A REVIEW DOES NOT: a control-plane change that adds a
// required params member this agent does not read, adds a result member the
// extra="forbid" model would then reject us for omitting, widens `state` beyond
// success|failed, or -- the one that matters most for invariant #3 -- adds a
// credential-bearing member to the params. Any of those changes a file here and
// turns this package red, which is what a compatibility decision is supposed to
// feel like.

const schemaDir = "../../../../docs/kernel/wire-protocol"

type jsonSchema struct {
	Title                string                `json:"title"`
	Type                 string                `json:"type"`
	Properties           map[string]jsonSchema `json:"properties"`
	Required             []string              `json:"required"`
	AdditionalProperties *bool                 `json:"additionalProperties"`
	Enum                 []json.RawMessage     `json:"enum"`
	Default              json.RawMessage       `json:"default"`
	AnyOf                []jsonSchema          `json:"anyOf"`
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
			t.Fatalf("%s.%s is omitempty. ProvisionEnrollResult's optional members default to "+
				"NULL, so emitting the explicit null is exactly the default -- while omitempty "+
				"would make a dropped key and a null key two spellings of one fact that can "+
				"silently diverge", typ.Name(), f.Name)
		}
	}
	return names, pointer
}

func TestTheParamsStructImplementsExactlyTheExportedProvisionEnrollParams(t *testing.T) {
	s := loadSchema(t, "ProvisionEnrollParams")
	got, pointer := jsonTags(t, reflect.TypeOf(params{}))

	for prop := range s.Properties {
		if !got[prop] {
			t.Errorf("ProvisionEnrollParams property %q has no field in the Go params struct; the "+
				"server may already be sending it", prop)
		}
	}
	for name := range got {
		if _, ok := s.Properties[name]; !ok {
			t.Errorf("the Go params struct reads %q, which the exported schema does not define", name)
		}
	}

	// A REQUIRED property must not be a pointer and an OPTIONAL one must be.
	// `expected_agent_version` defaults to null, and "the control plane did not
	// pin a version" is a different statement from "it pinned the empty string";
	// collapsing them would turn every job from a control plane that omits the
	// member into a version-mismatch failure.
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
			t.Errorf("params.%s is optional with a null schema default and is NOT a pointer; "+
				"'absent' and 'empty' would become the same statement", name)
		}
	}

	if s.AdditionalProperties == nil || *s.AdditionalProperties {
		t.Error("ProvisionEnrollParams is no longer additionalProperties:false; the server used " +
			"to forbid extra members")
	}
}

func TestTheOSFamilyEnumIsTheExportedOneAndThisPackageAnswersToAllOfIt(t *testing.T) {
	// The agent compares `os_family` against its own GOOS mapping and REFUSES a
	// mismatch. If the server's vocabulary grew a value this mapping cannot
	// produce, every provisioning run on that platform would be refused with a
	// reason that looks like a bug in the endpoint.
	//
	// FALSIFYING OBSERVATION: an enum entry no GOOS in the agent's own table
	// maps to.
	s := loadSchema(t, "ProvisionEnrollParams")
	var enum []string
	for _, raw := range s.Properties["os_family"].Enum {
		var v string
		if err := json.Unmarshal(raw, &v); err != nil {
			t.Fatalf("os_family enum entry %s: %v", raw, err)
		}
		enum = append(enum, v)
	}
	sort.Strings(enum)
	if want := []string{"linux", "macos", "windows"}; !reflect.DeepEqual(enum, want) {
		t.Fatalf("ProvisionEnrollParams.os_family enum = %v, want %v. This package maps GOOS to "+
			"exactly those three and refuses anything else", enum, want)
	}

	// The agent-side half of the mapping, pinned to the same three answers
	// enroll.osFamily produces at enrollment time. darwin -> macos is the one
	// that bites: a Mac answering "darwin" would refuse every macOS run.
	for goos, want := range map[string]string{"windows": "windows", "darwin": "macos", "linux": "linux"} {
		if got := osFamilyFor(goos); got != want {
			t.Errorf("osFamilyFor(%q) = %q, want %q", goos, got, want)
		}
	}
	if got := osFamilyFor("plan9"); got == "windows" || got == "macos" || got == "linux" {
		t.Errorf("osFamilyFor(\"plan9\") = %q; a GOOS with no server-side family must match "+
			"NOTHING, not the first entry in the enum", got)
	}
}

func TestTheResultStructImplementsExactlyTheExportedProvisionEnrollResult(t *testing.T) {
	s := loadSchema(t, "ProvisionEnrollResult")
	got, pointer := jsonTags(t, reflect.TypeOf(result{}))

	for prop := range s.Properties {
		if !got[prop] {
			t.Errorf("ProvisionEnrollResult property %q has no field in the Go result struct", prop)
		}
	}
	for name := range got {
		if _, ok := s.Properties[name]; !ok {
			t.Errorf("the Go result struct emits %q, which the schema does not define. The server "+
				"model is extra=\"forbid\": handle_provision_result raises "+
				"ProvisioningValidationError and the WHOLE job fails", name)
		}
	}

	required := map[string]bool{}
	for _, r := range s.Required {
		required[r] = true
		if !got[r] {
			t.Errorf("required result property %q is missing from the Go result struct", r)
		}
	}
	for name := range got {
		if required[name] && pointer[name] {
			t.Errorf("result.%s is required by the schema and is a pointer; a nil would marshal "+
				"as null and fail validation", name)
		}
		if !required[name] && !pointer[name] {
			t.Errorf("result.%s is optional with a NULL default and is not a pointer. `null` and "+
				"`false` are different answers for domain_joined: null is 'nobody could check', "+
				"false is 'checked, not joined', and ingest_provision_result writes whichever "+
				"arrives into the hash-chained audit metadata", name)
		}
	}
}

func TestTheResultStateDomainIsExactlySuccessAndFailed(t *testing.T) {
	// `ingest_provision_result` treats "success" as complete and EVERYTHING
	// else as failed, so a third state added server-side would silently become
	// a failure here. It is also the value this package chooses between, and
	// the constants must be the schema's own spellings.
	//
	// FALSIFYING OBSERVATION: an enum that is not {success, failed}, or a
	// constant in this package that is not in it.
	s := loadSchema(t, "ProvisionEnrollResult")
	var enum []string
	for _, raw := range s.Properties["state"].Enum {
		var v string
		if err := json.Unmarshal(raw, &v); err != nil {
			t.Fatalf("state enum entry %s: %v", raw, err)
		}
		enum = append(enum, v)
	}
	sort.Strings(enum)
	if want := []string{"failed", "success"}; !reflect.DeepEqual(enum, want) {
		t.Fatalf("ProvisionEnrollResult.state enum = %v, want %v", enum, want)
	}
	for _, c := range []string{stateSuccess, stateFailed} {
		found := false
		for _, e := range enum {
			found = found || e == c
		}
		if !found {
			t.Errorf("this package emits state %q, which the exported schema does not permit", c)
		}
	}
}

func TestTheParamsCarryNoCredentialBearingMember(t *testing.T) {
	// Invariant #3, pinned in the direction that matters. `post_config` reaches
	// this job with every sealed credential field stripped
	// (`_non_secret_post_config`), so the params document is not supposed to be
	// able to carry a secret at all. A future schema that grew a `password`, a
	// `secret_ref` or a rendered `answer_file` would be moving credential
	// material onto the signed-job path and into the job row -- a decision that
	// belongs in review, not in a commit.
	//
	// FALSIFYING OBSERVATION: any of these names appearing as a params property.
	s := loadSchema(t, "ProvisionEnrollParams")
	for _, forbidden := range []string{
		"password", "secret", "secrets", "credential", "credentials",
		"answer_file", "answer_ref", "domain_password", "local_admin_password",
		"enrollment_token", "token", "sealed",
	} {
		if _, ok := s.Properties[forbidden]; ok {
			t.Errorf("ProvisionEnrollParams now defines %q. Provisioning credentials are "+
				"vault-sealed and reach the endpoint ONLY through the short-lived rendered "+
				"answer file (spec §7); a credential-bearing member here would put them in the "+
				"job row and in this agent's memory for the life of the job", forbidden)
		}
	}
}
