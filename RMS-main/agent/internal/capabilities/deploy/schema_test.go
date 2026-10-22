package deploy

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
// `docs/kernel/wire-protocol/{AppDeploy,ScriptRun}{Params,Result}.schema.json`
// are generated from the Pydantic models by `scripts/export_wire_schema.py`,
// and `backend/tests/test_wire_schema_export.py` keeps them current with those
// models. This package's structs are built against THOSE FILES — not against a
// reading of schema.py — so the chain is model -> schema -> struct with a guard
// on each link.
//
// Copied from internal/capabilities/site/schema_test.go, which caught real
// drift, and extended in two directions this contract needs and site.wake does
// not: `$defs` resolution (both result schemas reference InstallOutcome
// through `$ref`), and an assertion that the installer_type ENUM is exactly
// partitioned by artifactBacked/managerSourced.
//
// WHAT THIS CATCHES THAT A REVIEW DOES NOT: a control-plane change that adds an
// installer_type this agent silently drops into neither partition (and
// therefore into the wrong branch), that adds a required member to
// AppDeployParams a deployed agent does not send back, that renames
// `artifact_sha256`, or that changes InstallOutcome's state enum out from
// under the two vocabularies this package emits.

const schemaDir = "../../../../docs/kernel/wire-protocol"

type jsonSchema struct {
	Title                string                `json:"title"`
	Type                 string                `json:"type"`
	Properties           map[string]jsonSchema `json:"properties"`
	Required             []string              `json:"required"`
	AdditionalProperties *bool                 `json:"additionalProperties"`
	Enum                 []json.RawMessage     `json:"enum"`
	Default              json.RawMessage       `json:"default"`
	Ref                  string                `json:"$ref"`
	Defs                 map[string]jsonSchema `json:"$defs"`
	AnyOf                []jsonSchema          `json:"anyOf"`
	Items                *jsonSchema           `json:"items"`
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

// jsonTags returns the json tag names a struct marshals, and which are
// pointers. It fatals on a missing tag or an `omitempty`, both of which would
// make the Go struct's wire shape different from the one a reader sees.
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
			t.Fatalf("%s.%s is omitempty. These contracts are extra=\"forbid\" with explicit "+
				"defaults, and an omitted key is not the same statement as an explicit null — "+
				"`detected_after` in particular is three-valued", typ.Name(), f.Name)
		}
	}
	return names, pointer
}

// assertSameProperties is the both-directions check: no schema property
// missing from the struct, no struct field the schema does not define.
func assertSameProperties(t *testing.T, title string, s jsonSchema, got map[string]bool) {
	t.Helper()
	for prop := range s.Properties {
		if !got[prop] {
			t.Errorf("%s property %q has no field in the Go struct; the server may already be "+
				"sending it", title, prop)
		}
	}
	for name := range got {
		if _, ok := s.Properties[name]; !ok {
			t.Errorf("the Go struct names %q, which %s does not define. The server model is "+
				"extra=\"forbid\": an unexpected member fails the WHOLE job at the result "+
				"handler", name, title)
		}
	}
	if s.AdditionalProperties == nil || *s.AdditionalProperties {
		t.Errorf("%s is no longer additionalProperties:false; the server used to forbid extra "+
			"members", title)
	}
}

// ---------------------------------------------------------------------------
// ScriptRunParams / ScriptRunResult
// ---------------------------------------------------------------------------

func TestTheParamsStructImplementsExactlyTheExportedScriptRunParams(t *testing.T) {
	s := loadSchema(t, "ScriptRunParams")
	got, pointer := jsonTags(t, reflect.TypeOf(scriptParams{}))
	assertSameProperties(t, "ScriptRunParams", s, got)

	required := map[string]bool{}
	for _, r := range s.Required {
		required[r] = true
		if !got[r] {
			t.Errorf("required property %q is missing from the Go struct", r)
		}
	}
	// A REQUIRED property must not be a pointer; an OPTIONAL one with a
	// NON-ZERO default must be, or "absent" and "zero" become the same
	// statement. `timeout_s` defaults to 3600 and this package refuses 0, so
	// collapsing them would refuse every job from a control plane that omits
	// the member.
	for name := range got {
		if required[name] && pointer[name] {
			t.Errorf("scriptParams.%s is required by the schema and is a pointer", name)
		}
	}
	if !pointer["timeout_s"] {
		t.Error("scriptParams.timeout_s is not a pointer; its schema default is 3600, so an " +
			"absent member would read as timeout_s: 0 — which parseScriptParams refuses")
	}
	assertIntDefault(t, "ScriptRunParams", "timeout_s", s.Properties["timeout_s"].Default,
		defaultTimeoutS)

	// The interpreter enum is duplicated agent-side (script.go's
	// `interpreters`) because an enum that lives only in the server's model
	// does not travel with the job in any form the endpoint can enforce.
	// Duplication drifts, so it is pinned here.
	enum := stringEnum(t, s.Properties["interpreter"])
	if len(enum) != len(interpreters) {
		t.Fatalf("ScriptRunParams.interpreter enum is %v; this package knows %d interpreters",
			enum, len(interpreters))
	}
	for _, e := range enum {
		if _, ok := interpreters[e]; !ok {
			t.Errorf("the schema permits interpreter %q and this package does not know it; a "+
				"job carrying it is refused in the field and nothing here goes red", e)
		}
	}
}

func TestTheResultStructImplementsExactlyTheExportedScriptRunResult(t *testing.T) {
	s := loadSchema(t, "ScriptRunResult")
	got, _ := jsonTags(t, reflect.TypeOf(scriptResult{}))
	assertSameProperties(t, "ScriptRunResult", s, got)

	if len(s.Required) != 1 || s.Required[0] != "outcome" {
		t.Fatalf("ScriptRunResult now requires %v; this package emits exactly {outcome}",
			s.Required)
	}
	// A SINGLE outcome, not a list. app.deploy's is a list. Getting these the
	// wrong way round produces a result the server rejects at
	// handle_script_result and a job that fails for a reason nobody can see.
	if s.Properties["outcome"].Ref == "" {
		t.Error("ScriptRunResult.outcome is no longer a $ref to InstallOutcome")
	}
}

// ---------------------------------------------------------------------------
// AppDeployParams / AppDeployResult
// ---------------------------------------------------------------------------

func TestTheParamsStructImplementsExactlyTheExportedAppDeployParams(t *testing.T) {
	s := loadSchema(t, "AppDeployParams")
	got, pointer := jsonTags(t, reflect.TypeOf(deployParams{}))
	assertSameProperties(t, "AppDeployParams", s, got)

	required := map[string]bool{}
	for _, r := range s.Required {
		required[r] = true
		if !got[r] {
			t.Errorf("required property %q is missing from the Go struct", r)
		}
	}
	for name := range got {
		if required[name] && pointer[name] {
			t.Errorf("deployParams.%s is required by the schema and is a pointer", name)
		}
	}

	// The three NULLABLE members must be pointers, and this is the assertion
	// that matters most in this file. `artifact_ref` and `artifact_sha256`
	// being `*string` rather than `string` is what lets parseDeployParams tell
	// "there is no artifact" (manager-sourced) from "the artifact ref is empty"
	// (malformed) — and that distinction is the one that decides whether a hash
	// is checked.
	//
	// `package_identifier` joins them for a different reason: it is the
	// ADDITIVE member, so `null` is what an older control plane sends, and the
	// pointer is what lets packageIdentifier tell "this control plane does not
	// have the field" from "this control plane sent an empty one". Collapsing
	// them would not be a security bug here — it would be a compatibility one,
	// which is the other half of the same contract.
	for _, nullable := range []string{
		"artifact_ref", "artifact_sha256", "install_args", "package_identifier",
	} {
		if !pointer[nullable] {
			t.Errorf("deployParams.%s is not a pointer. The schema makes it nullable, and "+
				"collapsing null into \"\" is exactly the direction that lets a job with no "+
				"artifact_ref look like a job with an empty one", nullable)
		}
		if !isNullable(s.Properties[nullable]) {
			t.Errorf("AppDeployParams.%s is no longer nullable in the schema; this package "+
				"treats absence as meaningful", nullable)
		}
	}
	if !pointer["deferral_hours"] {
		t.Error("deployParams.deferral_hours is not a pointer; its schema default is 24")
	}
	assertIntDefault(t, "AppDeployParams", "deferral_hours",
		s.Properties["deferral_hours"].Default, 24)

	// The action enum, pinned to the copy in install.go.
	for _, a := range stringEnum(t, s.Properties["action"]) {
		if _, ok := actions[a]; !ok {
			t.Errorf("the schema permits action %q and this package refuses it", a)
		}
	}
	if n := len(stringEnum(t, s.Properties["action"])); n != len(actions) {
		t.Errorf("the schema defines %d actions and this package knows %d", n, len(actions))
	}
}

func TestEveryInstallerTypeInTheSchemaIsInExactlyOnePartition(t *testing.T) {
	// THE SECURITY-CRITICAL SCHEMA ASSERTION.
	//
	// install.go chooses between "download and hash-verify" and "ask a package
	// manager" by looking the installer_type up in exactly one of two maps. A
	// value in NEITHER map is refused, which is safe. A value in BOTH would be
	// a contradiction. But the case that actually bites is a control plane that
	// adds a new installer_type — say `flatpak` — which this build then refuses
	// in the field while this suite stays green.
	//
	// FALSIFYING OBSERVATION: a schema enum member this package does not
	// classify, or a member this package classifies twice, or a member of
	// either partition that the schema no longer defines.
	s := loadSchema(t, "AppDeployParams")
	enum := stringEnum(t, s.Properties["installer_type"])
	if len(enum) == 0 {
		t.Fatal("AppDeployParams.installer_type has no enum; this test would be vacuous")
	}

	for _, e := range enum {
		_, a := artifactBacked[e]
		_, m := managerSourced[e]
		switch {
		case a && m:
			t.Errorf("installer_type %q is in BOTH partitions; which branch a job takes would "+
				"depend on map iteration order", e)
		case !a && !m:
			t.Errorf("installer_type %q is in NEITHER partition. parseDeployParams refuses it, "+
				"so every deployment of that type fails in the field and nothing here goes red. "+
				"Classify it — and if it is artifact-backed, it needs a hash check", e)
		}
	}
	for e := range installerTypes {
		if !contains(enum, e) {
			t.Errorf("this package classifies installer_type %q, which the schema no longer "+
				"defines", e)
		}
	}

	// And the partition must not be empty on either side, or the test above
	// passes trivially after somebody deletes a map.
	if len(artifactBacked) == 0 || len(managerSourced) == 0 {
		t.Fatal("one of the partitions is empty; the classification is no longer a partition")
	}
}

func TestTheResultStructImplementsExactlyTheExportedAppDeployResult(t *testing.T) {
	s := loadSchema(t, "AppDeployResult")
	got, _ := jsonTags(t, reflect.TypeOf(deployResult{}))
	assertSameProperties(t, "AppDeployResult", s, got)

	if len(s.Required) != 1 || s.Required[0] != "outcomes" {
		t.Fatalf("AppDeployResult now requires %v; this package emits exactly {outcomes}",
			s.Required)
	}
	// A LIST. ingest_deploy_result reads outcomes[0] and has an explicit branch
	// for the empty list.
	items := s.Properties["outcomes"].Items
	if items == nil || items.Ref == "" {
		t.Error("AppDeployResult.outcomes is no longer a list of $ref InstallOutcome")
	}
}

// ---------------------------------------------------------------------------
// InstallOutcome, the shared contract (AD-03)
// ---------------------------------------------------------------------------

func TestTheOutcomeStructImplementsExactlyTheExportedInstallOutcome(t *testing.T) {
	// Resolved through $defs on AppDeployResult rather than read from
	// InstallOutcome.schema.json, so what is checked is the definition the
	// result schema ACTUALLY references. Two files that agree today is not the
	// same as one file.
	s := loadSchema(t, "AppDeployResult")
	def, ok := s.Defs["InstallOutcome"]
	if !ok {
		t.Fatal("AppDeployResult no longer carries an InstallOutcome $def")
	}
	got, pointer := jsonTags(t, reflect.TypeOf(installOutcome{}))
	assertSameProperties(t, "InstallOutcome", def, got)

	required := map[string]bool{"identifier": false, "state": false}
	for _, r := range def.Required {
		if _, expected := required[r]; !expected {
			t.Errorf("InstallOutcome now requires %q, which this package may not always be able "+
				"to determine", r)
		}
		required[r] = true
	}
	for name, seen := range required {
		if !seen {
			t.Errorf("InstallOutcome no longer requires %q; this package always emits it", name)
		}
		if pointer[name] {
			t.Errorf("installOutcome.%s is required and is a pointer", name)
		}
	}

	// The three-valued members must be pointers. `detected_after` especially:
	// see detect.go for why "cannot evaluate" must not collapse into false.
	for _, nullable := range []string{"exit_code", "log_ref", "detected_after"} {
		if !pointer[nullable] {
			t.Errorf("installOutcome.%s is not a pointer, so this agent cannot say 'I do not "+
				"know'. For detected_after that turns every install of a package detected by a "+
				"rule this build cannot evaluate into 'installed but not detected'", nullable)
		}
		if !isNullable(def.Properties[nullable]) {
			t.Errorf("InstallOutcome.%s is no longer nullable", nullable)
		}
	}

	// The state enum, and the two vocabularies this package emits from it.
	enum := stringEnum(t, def.Properties["state"])
	sort.Strings(enum)
	want := []string{"failed", "installed", "not_applicable", "succeeded"}
	if strings.Join(enum, ",") != strings.Join(want, ",") {
		t.Fatalf("InstallOutcome.state enum = %v, want %v. This package emits {installed, "+
			"failed, not_applicable} for app.deploy and {succeeded, failed} for script.run, and "+
			"the server COERCES an out-of-domain value to failed", enum, want)
	}
	for _, s := range []string{stateInstalled, stateFailed, stateNotApplicable, stateSucceeded} {
		if !contains(enum, s) {
			t.Errorf("this package emits state %q, which the schema does not permit", s)
		}
	}
}

// ---------------------------------------------------------------------------
// Apparatus
// ---------------------------------------------------------------------------

func stringEnum(t *testing.T, s jsonSchema) []string {
	t.Helper()
	out := make([]string, 0, len(s.Enum))
	for _, raw := range s.Enum {
		var v string
		if err := json.Unmarshal(raw, &v); err != nil {
			t.Fatalf("enum entry %s is not a string: %v", raw, err)
		}
		out = append(out, v)
	}
	return out
}

// isNullable reports whether a property's schema admits null — the `anyOf`
// [{type: X}, {type: "null"}] shape Pydantic emits for `X | None`.
func isNullable(s jsonSchema) bool {
	for _, alt := range s.AnyOf {
		if alt.Type == "null" {
			return true
		}
	}
	return false
}

func assertIntDefault(t *testing.T, title, name string, raw json.RawMessage, want int) {
	t.Helper()
	if len(raw) == 0 {
		t.Errorf("%s.%s no longer declares a default; this package assumes %d", title, name, want)
		return
	}
	var got int
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Errorf("%s.%s default %s: %v", title, name, raw, err)
		return
	}
	if got != want {
		t.Errorf("%s.%s default is %d and this package uses %d; an agent that substituted a "+
			"different default would behave differently from the server's own reading of the "+
			"same job", title, name, got, want)
	}
}

func contains(haystack []string, needle string) bool {
	for _, h := range haystack {
		if h == needle {
			return true
		}
	}
	return false
}
