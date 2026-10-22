package patch

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
// docs/kernel/wire-protocol/Patch*.schema.json are generated from
// app/modules/patch/schema.py by scripts/export_wire_schema.py, and
// backend/tests/test_wire_schema_export.py keeps them current with those
// models. This package's structs are built against THOSE FILES -- not against
// a reading of schema.py -- so the chain is model -> schema -> struct with a
// guard on every link.
//
// The agent and the control plane live in one repository now, which makes it
// possible to change both sides in one commit and tempting to treat the
// envelope as an internal detail. CLAUDE.md is explicit that it is not: a
// running fleet is always mixed-version, and a field a new control plane
// requires and a deployed agent does not send is an outage no test suite shows
// you, because the suite only ever runs one version of each side. This file is
// the closest thing to a guard that exists -- it turns a schema change into a
// red build in the package that has to honour it.
//
// WHAT THIS CATCHES THAT A REVIEW DOES NOT: a widened severity vocabulary, a
// fourth reboot policy, a new required member on PatchScanDocument, a
// PatchApplyOutcome state this agent never emits, or the deferred
// PatchApplyOutcome -> InstallOutcome migration actually landing.

const schemaDir = "../../../../docs/kernel/wire-protocol"

type jsonSchema struct {
	Title                string                `json:"title"`
	Type                 string                `json:"type"`
	Properties           map[string]jsonSchema `json:"properties"`
	Defs                 map[string]jsonSchema `json:"$defs"`
	Required             []string              `json:"required"`
	AdditionalProperties json.RawMessage       `json:"additionalProperties"`
	Enum                 []json.RawMessage     `json:"enum"`
	Default              json.RawMessage       `json:"default"`
	Ref                  string                `json:"$ref"`
	Items                *jsonSchema           `json:"items"`
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

// def resolves one of a schema's $defs by name.
func def(t *testing.T, s jsonSchema, name string) jsonSchema {
	t.Helper()
	d, ok := s.Defs[name]
	if !ok {
		keys := make([]string, 0, len(s.Defs))
		for k := range s.Defs {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		t.Fatalf("%s no longer defines $defs/%s; it defines %v. If that is the deferred "+
			"PatchApplyOutcome -> InstallOutcome migration landing, this package's structs have to "+
			"move with it", s.Title, name, keys)
	}
	return d
}

// fieldSet describes what a Go struct puts on the wire.
//
// POINTERS AND SLICES ARE TRACKED SEPARATELY, and the distinction was forced by
// a failing assertion rather than foreseen. Both marshal to `null` when nil, so
// a first version of this file lumped them together -- and then rejected
// `installed_identifiers`, which is `list[str] = []` server-side and MUST be a
// Go slice. The difference is where the guarantee lives: a pointer expresses
// absence BY TYPE, while a slice expresses it only if the code forgets to
// initialise it. So a slice is permitted for a non-nullable member and owes a
// RUNTIME test that it is never nil -- which is
// TestAnEmptyScanEmitsEmptyListsAndNotNull, not this file.
type fieldSet struct {
	names   map[string]bool
	pointer map[string]bool
	list    map[string]bool
}

func (f fieldSet) nilable(name string) bool { return f.pointer[name] || f.list[name] }

func jsonTags(t *testing.T, typ reflect.Type) fieldSet {
	t.Helper()
	fs := fieldSet{names: map[string]bool{}, pointer: map[string]bool{}, list: map[string]bool{}}
	for i := 0; i < typ.NumField(); i++ {
		f := typ.Field(i)
		tag := f.Tag.Get("json")
		if tag == "" || tag == "-" {
			t.Fatalf("%s.%s has no json tag; wire structs name every field explicitly",
				typ.Name(), f.Name)
		}
		name := strings.Split(tag, ",")[0]
		if strings.Contains(tag, "omitempty") {
			// Every one of these documents is `extra="forbid"` on the SERVER,
			// but that is not why omitempty is banned. A REQUIRED member that
			// is omitted because it happens to be the zero value fails
			// validation on the whole document -- `reboot_required: false` is
			// a fact, not an absence, and dropping the key would cost a device
			// its whole apply result.
			t.Fatalf("%s.%s is omitempty; a member that vanishes when it is the zero value cannot "+
				"carry a false or an empty list, both of which are facts here", typ.Name(), f.Name)
		}
		fs.names[name] = true
		switch f.Type.Kind() {
		case reflect.Ptr, reflect.Interface:
			fs.pointer[name] = true
		case reflect.Slice, reflect.Map:
			fs.list[name] = true
		}
	}
	return fs
}

// forbidsExtra reports whether the schema says additionalProperties:false.
//
// The member is NOT always a bool. `PatchApplyParams.patches` is
// `list[dict[str, str]]`, and pydantic exports its item schema with
// `additionalProperties: {"type": "string"}` -- a VALUE SCHEMA, not a
// permission flag. A loader typed `*bool` fails to parse the file at all, which
// is how the first version of this test discovered that the server permits a
// patch reference to carry members beyond source/identifier.
func forbidsExtra(raw json.RawMessage) bool {
	if len(raw) == 0 {
		return false
	}
	var b bool
	if err := json.Unmarshal(raw, &b); err != nil {
		return false // an object: extra properties are permitted, with a type
	}
	return !b
}

// schemaAllowsNull reports whether a property's anyOf includes {"type":"null"},
// i.e. the server declares it `X | None`.
func schemaAllowsNull(p jsonSchema) bool {
	for _, alt := range p.AnyOf {
		if alt.Type == "null" {
			return true
		}
	}
	return false
}

// hasNonZeroDefault reports whether a property's schema default differs from
// the Go zero value its field would have.
//
// This is the distinction the FIRST version of this file got wrong, and the
// error was worth keeping the record of: it demanded that every optional member
// be a pointer, which failed on `reboot_required` -- optional, `default: false`.
// A pointer there would be wrong, not merely unnecessary: `reboot_required:
// false` is a FACT this agent asserts about the machine, and a nil pointer
// marshalling as `null` would fail the server's `bool` field.
//
// `deferral_hours` is the case the rule exists for: `default: 24`, so a plain
// int would make an absent member read as "defer for zero hours".
func hasNonZeroDefault(p jsonSchema) bool {
	if len(p.Default) == 0 {
		return false
	}
	switch strings.TrimSpace(string(p.Default)) {
	case "null", "false", "0", `""`, "[]", "{}":
		return false
	}
	return true
}

// assertShape is the common check: same property set, required members able to
// carry a value, and optional members able to say "absent" WHEN THAT IS A
// DIFFERENT STATEMENT FROM THE ZERO VALUE.
func assertShape(t *testing.T, s jsonSchema, typ reflect.Type, requiredMayBeNullable map[string]bool) {
	t.Helper()
	got := jsonTags(t, typ)

	for prop := range s.Properties {
		if !got.names[prop] {
			t.Errorf("%s property %q has no field in %s; the server may already be sending or "+
				"expecting it", s.Title, prop, typ.Name())
		}
	}
	for name := range got.names {
		if _, ok := s.Properties[name]; !ok {
			t.Errorf("%s emits/reads %q, which %s does not define. The server is extra=\"forbid\": "+
				"a ValidationError is raised on the WHOLE document and the job fails",
				typ.Name(), name, s.Title)
		}
	}

	required := map[string]bool{}
	for _, r := range s.Required {
		required[r] = true
		if !got.names[r] {
			t.Errorf("%s requires %q and %s has no such field", s.Title, r, typ.Name())
		}
	}
	for name := range got.names {
		prop := s.Properties[name]
		mustExpressAbsence := !required[name] && (schemaAllowsNull(prop) || hasNonZeroDefault(prop))
		switch {
		case required[name] && got.nilable(name) && !requiredMayBeNullable[name]:
			t.Errorf("%s.%s is required by %s and can be nil in Go; 'absent' would marshal as "+
				"null and fail validation", typ.Name(), name, s.Title)
		case mustExpressAbsence && !got.nilable(name):
			t.Errorf("%s.%s is optional in %s and either nullable or defaulted to a NON-ZERO "+
				"value, and the Go field cannot express absence; 'no value' and 'the zero value' "+
				"would become the same statement on the wire", typ.Name(), name, s.Title)
		case !mustExpressAbsence && !required[name] && got.pointer[name]:
			// A slice is fine here (see fieldSet); a POINTER is not. The
			// server declares a plain value with a zero default, so a nil
			// pointer would marshal as null and fail it.
			t.Errorf("%s.%s is optional in %s with a zero-valued default and is a POINTER in Go; "+
				"a nil one marshals as null where the server declares a plain value",
				typ.Name(), name, s.Title)
		}
	}

	if !forbidsExtra(s.AdditionalProperties) {
		t.Errorf("%s is no longer additionalProperties:false; the server used to forbid extra "+
			"members and this package relies on that being the server's problem, not a silent drop",
			s.Title)
	}
}

// ---------------------------------------------------------------------------
// patch.scan
// ---------------------------------------------------------------------------

func TestPatchScanParamsCarriesOnlyTheResultVersion(t *testing.T) {
	// The scan reads one thing from the job: which document the control plane
	// accepts. A new member here is one this package would ignore on every
	// device in the fleet, so it has to arrive with a red test.
	s := loadSchema(t, "PatchScanParams")
	names := make([]string, 0, len(s.Properties))
	for k := range s.Properties {
		names = append(names, k)
	}
	sort.Strings(names)
	if !reflect.DeepEqual(names, []string{"result_version"}) {
		t.Fatalf("PatchScanParams carries %v; this package reads only result_version", names)
	}
	assertShape(t, s, reflect.TypeOf(scanParams{}), nil)
	assertIntDefault(t, s, "result_version", resultVersion)
}

func TestTheRebootStructsImplementExactlyTheExportedContracts(t *testing.T) {
	assertShape(t, loadSchema(t, "PatchRebootParams"), reflect.TypeOf(rebootParams{}), nil)
	assertShape(t, loadSchema(t, "PatchRebootResult"), reflect.TypeOf(rebootResult{}), nil)
	got := enumStrings(t, loadSchema(t, "PatchRebootResult").Properties["state"])
	sort.Strings(got)
	want := []string{"not_needed", "scheduled", "window_too_short"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("PatchRebootResult.state is %v, this package emits %v", got, want)
	}
}

func TestTheVersionOneDocumentsAreASubsetOfTheExportedOnes(t *testing.T) {
	// An older control plane is extra="forbid"; the v1 structs must carry
	// nothing the v1 contract lacked, and nothing the current one lacks.
	for _, c := range []struct {
		title string
		typ   reflect.Type
		v1    []string
	}{
		{"PatchScanDocument", reflect.TypeOf(scanDocumentV1{}),
			[]string{"installed_identifiers", "os_family", "patches", "scanned_at"}},
		{"PatchApplyResult", reflect.TypeOf(applyResultV1{}),
			[]string{"applied_at", "reboot_required", "results"}},
	} {
		s := loadSchema(t, c.title)
		tags := jsonTags(t, c.typ)
		names := make([]string, 0, len(tags.names))
		for n := range tags.names {
			if _, ok := s.Properties[n]; !ok {
				t.Errorf("%s v1 emits %q, which the contract does not define", c.title, n)
			}
			names = append(names, n)
		}
		sort.Strings(names)
		if !reflect.DeepEqual(names, c.v1) {
			t.Errorf("%s v1 emits %v, want exactly %v", c.title, names, c.v1)
		}
	}
}

func TestTheScanDocumentStructImplementsExactlyTheExportedPatchScanDocument(t *testing.T) {
	s := loadSchema(t, "PatchScanDocument")
	// `patches` and `installed_identifiers` are required-and-nullable in Go
	// because they are SLICES. buildScanDocument never leaves either nil --
	// TestAnEmptyScanEmitsEmptyListsAndNotNull is the assertion that makes the
	// exemption safe rather than a hole.
	assertShape(t, s, reflect.TypeOf(scanDocument{}), map[string]bool{
		"patches":               true,
		"installed_identifiers": true,
	})
}

func TestThePatchEntryStructImplementsExactlyTheExportedPatchEntry(t *testing.T) {
	s := loadSchema(t, "PatchScanDocument")
	assertShape(t, def(t, s, "PatchEntry"), reflect.TypeOf(entry{}), nil)
}

func TestTheSeverityVocabularyIsTheExportedSchemasAndNotThisPackagesOpinion(t *testing.T) {
	// `severity` is a Literal server-side and the ValidationError it raises is
	// on the WHOLE document, so one severity this agent invents costs a device
	// its entire scan. That makes the vocabulary a contract, and a contract
	// restated in Go drifts unless it is pinned to the file.
	//
	// FALSIFYING OBSERVATION: a schema enum member this package's `severities`
	// map does not contain, or vice versa.
	s := def(t, loadSchema(t, "PatchScanDocument"), "PatchEntry")
	got := enumStrings(t, s.Properties["severity"])

	for _, v := range got {
		if !severities[v] {
			t.Errorf("the schema permits severity %q and this package maps it to \"unknown\"; a "+
				"real severity is being thrown away fleet-wide", v)
		}
	}
	if len(got) != len(severities) {
		t.Errorf("PatchEntry.severity enum is %v (%d members) and this package knows %d",
			got, len(got), len(severities))
	}
	// The one member whose ABSENCE would be a silent disaster: every mapping in
	// this package falls back to "unknown", so a schema that dropped it would
	// make every unrated update fail its document.
	if !contains(got, "unknown") {
		t.Error("PatchEntry.severity no longer permits \"unknown\". windowsSeverity, aptSeverity " +
			"and macSeverity ALL fall back to it, so every update Microsoft or Apple did not rate " +
			"would now fail the whole document")
	}
}

// ---------------------------------------------------------------------------
// patch.apply
// ---------------------------------------------------------------------------

func TestTheApplyParamsStructImplementsExactlyTheExportedPatchApplyParams(t *testing.T) {
	s := loadSchema(t, "PatchApplyParams")
	// `patches` is required and is a slice; it is only ever READ here, so a nil
	// one is an empty batch and parseApplyParams refuses it explicitly.
	assertShape(t, s, reflect.TypeOf(applyParams{}), map[string]bool{"patches": true})

	// deferral_hours' default is 24 and not 0, which is why the field is a
	// pointer. Pinning the number matters: an agent that substituted a
	// different default would be acting on a policy nobody set.
	assertIntDefault(t, s, "deferral_hours", defaultDeferralHours)
}

func TestTheRebootVocabularyIsTheExportedSchemasAndAFourthMemberIsAFleetEvent(t *testing.T) {
	// parseApplyParams REFUSES a reboot value it cannot name, so this enum is
	// not decoration: a fourth member added server-side takes every deployed
	// agent's patch apply offline until the fleet updates.
	// docs/kernel/wire-protocol/README.md already says a change in that
	// directory is a compatibility decision; this is one of the places where
	// that is literally true, and this test is where it becomes visible.
	//
	// FALSIFYING OBSERVATION: an enum member `rebootPolicies` does not contain.
	s := loadSchema(t, "PatchApplyParams")
	got := enumStrings(t, s.Properties["reboot"])

	for _, v := range got {
		if !rebootPolicies[v] {
			t.Errorf("PatchApplyParams.reboot permits %q and parseApplyParams REFUSES it. Every "+
				"apply job carrying that value is refused on every device in the fleet", v)
		}
	}
	if len(got) != len(rebootPolicies) {
		t.Errorf("PatchApplyParams.reboot enum is %v (%d members) and this package accepts %d",
			got, len(got), len(rebootPolicies))
	}
}

func TestTheApplyResultStructImplementsExactlyTheExportedPatchApplyResult(t *testing.T) {
	s := loadSchema(t, "PatchApplyResult")
	assertShape(t, s, reflect.TypeOf(applyResult{}), map[string]bool{"results": true})
}

func TestTheApplyOutcomeStructImplementsExactlyTheExportedPatchApplyOutcome(t *testing.T) {
	s := loadSchema(t, "PatchApplyResult")
	assertShape(t, def(t, s, "PatchApplyOutcome"), reflect.TypeOf(applyOutcome{}), nil)
}

func TestTheOutcomeStateVocabularyIsExactlyInstalledOrFailed(t *testing.T) {
	// ingest_apply_result branches on `state == "installed"` and treats
	// EVERYTHING ELSE as failed, writing `outcome.error` into
	// `device_patch_status.last_error`. So a third state added server-side
	// would not be a richer vocabulary; it would be silently reconciled as a
	// failure until app/modules/patch/apply.py grew a branch for it.
	//
	// FALSIFYING OBSERVATION: an enum member other than installed/failed.
	s := def(t, loadSchema(t, "PatchApplyResult"), "PatchApplyOutcome")
	got := enumStrings(t, s.Properties["state"])
	sort.Strings(got)
	if len(got) != 2 || got[0] != "failed" || got[1] != "installed" {
		t.Fatalf("PatchApplyOutcome.state enum = %v; this package emits exactly installed and "+
			"failed, and ingest_apply_result reads anything that is not \"installed\" as a "+
			"failure", got)
	}
}

// ---------------------------------------------------------------------------
// The migration that has NOT happened
// ---------------------------------------------------------------------------

func TestPatchApplyStillEmitsPatchApplyOutcomeAndNotInstallOutcome(t *testing.T) {
	// InstallOutcome.schema.json's own description says `patch.apply` "will
	// migrate its module-specific PatchApplyOutcome onto this contract in a
	// LATER, DEFERRED cleanup" (app-deploy spec AD-03). Until that lands, the
	// server is extra="forbid" on PatchApplyOutcome and emitting
	// InstallOutcome's members would fail the WHOLE job at handle_apply_result.
	//
	// This test exists because "the task said InstallOutcome" is exactly the
	// kind of instruction that gets followed past the contract that is actually
	// in force. It pins the direction: when the migration DOES land, this goes
	// red and names what to change.
	//
	// FALSIFYING OBSERVATION: PatchApplyResult referencing InstallOutcome, or
	// this package growing an InstallOutcome member.
	install := loadSchema(t, "InstallOutcome")
	installOnly := []string{"exit_code", "log", "log_ref", "detected_after"}
	for _, name := range installOnly {
		if _, ok := install.Properties[name]; !ok {
			t.Fatalf("InstallOutcome no longer defines %q; this test's premise is stale", name)
		}
	}

	outcome := def(t, loadSchema(t, "PatchApplyResult"), "PatchApplyOutcome")
	for _, name := range installOnly {
		if _, ok := outcome.Properties[name]; ok {
			t.Errorf("PatchApplyOutcome now carries %q. The deferred InstallOutcome migration has "+
				"landed and applyOutcome must move onto it -- including the extra states "+
				"not_applicable and succeeded, which applyOne currently never emits", name)
		}
	}

	// `source` is the member that distinguishes them, and it is the one this
	// package's whole matching scheme is keyed on. InstallOutcome has no
	// `source`: its identifier is opaque. If PatchApplyOutcome lost it, the
	// `<source>:<identifier>` key would stop being expressible.
	if _, ok := outcome.Properties["source"]; !ok {
		t.Error("PatchApplyOutcome lost `source`. applyOne keys every match on " +
			"`<source>:<identifier>`, and InstallOutcome has no source member at all")
	}
	if _, ok := install.Properties["source"]; ok {
		t.Error("InstallOutcome grew a `source`; the two contracts may now be closer than this " +
			"test assumes")
	}
}

// ---------------------------------------------------------------------------
// Apparatus
// ---------------------------------------------------------------------------

func enumStrings(t *testing.T, s jsonSchema) []string {
	t.Helper()
	if len(s.Enum) == 0 {
		t.Fatalf("%s is no longer an enum; this package restates its vocabulary in Go and needs "+
			"the file to pin it against", s.Title)
	}
	out := make([]string, 0, len(s.Enum))
	for _, raw := range s.Enum {
		var v string
		if err := json.Unmarshal(raw, &v); err != nil {
			t.Fatalf("enum member %s is not a string: %v", raw, err)
		}
		out = append(out, v)
	}
	sort.Strings(out)
	return out
}

func assertIntDefault(t *testing.T, s jsonSchema, prop string, want int) {
	t.Helper()
	p, ok := s.Properties[prop]
	if !ok {
		t.Fatalf("%s no longer defines %q", s.Title, prop)
	}
	if len(p.Default) == 0 {
		t.Fatalf("%s.%s no longer declares a default; this package assumes %d", s.Title, prop, want)
	}
	var got int
	if err := json.Unmarshal(p.Default, &got); err != nil {
		t.Fatalf("%s.%s default %s: %v", s.Title, prop, p.Default, err)
	}
	if got != want {
		t.Fatalf("%s.%s defaults to %d and this package substitutes %d when the member is absent; "+
			"an older control plane's jobs would be acted on under a policy nobody set",
			s.Title, prop, got, want)
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
