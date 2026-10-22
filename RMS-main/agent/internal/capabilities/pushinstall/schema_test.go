package pushinstall

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"testing"
)

// THE EXPORTED SCHEMA IS THE CONTRACT, AND THIS FILE ENFORCES IT.
//
// `docs/kernel/wire-protocol/AgentPush*.schema.json` are generated from the
// Pydantic models by `scripts/export_wire_schema.py`, and
// `backend/tests/test_wire_schema_export.py` keeps them current with those
// models. This package's structs are built against THOSE FILES. internal/wire's
// `notImplemented` map defers these four to this test (they are job params and
// results, unmarshalled/built by this capability, not message envelopes), so
// without this file the push contract would have no Go-side guard.
//
// WHAT THIS CATCHES THAT A REVIEW DOES NOT: a control-plane change that adds a
// credential field to the probe, drops the never-null `host_key_fingerprint`,
// widens the transport beyond `ssh`/`winrm`, or adds a member the agent's
// struct does not read. Each changes a file here and turns this package red -- which is what
// a compatibility decision is supposed to feel like (the wire-protocol README).

const schemaDir = "../../../../docs/kernel/wire-protocol"

type jsonSchema struct {
	Title                string                `json:"title"`
	Properties           map[string]jsonSchema `json:"properties"`
	Required             []string              `json:"required"`
	AdditionalProperties *bool                 `json:"additionalProperties"`
	Const                json.RawMessage       `json:"const"`
	Enum                 []string              `json:"enum"`
	Defs                 map[string]jsonSchema `json:"$defs"`
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
		t.Fatalf("title = %q", s.Title)
	}
	return s
}

// structTags returns the json tag names a struct marshals.
func structTags(typ reflect.Type) []string {
	var out []string
	for i := 0; i < typ.NumField(); i++ {
		tag := typ.Field(i).Tag.Get("json")
		if tag == "" || tag == "-" {
			continue
		}
		name, _, _ := cut(tag, ",")
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

func cut(s, sep string) (before, after string, found bool) {
	for i := 0; i+len(sep) <= len(s); i++ {
		if s[i:i+len(sep)] == sep {
			return s[:i], s[i+len(sep):], true
		}
	}
	return s, "", false
}

func schemaProps(s jsonSchema) []string {
	var out []string
	for k := range s.Properties {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// --------------------------------------------------------------------------- //
// The probe: no credential, ever
// --------------------------------------------------------------------------- //

func TestProbeParamsMatchTheContractAndCarryNoCredential(t *testing.T) {
	s := loadSchema(t, "AgentPushProbeParams")
	// additionalProperties:false is what makes "no field could carry a
	// credential" a property of the model (falsifier 22).
	if s.AdditionalProperties == nil || *s.AdditionalProperties {
		t.Fatal("probe params must forbid additional properties")
	}
	props := schemaProps(s)
	if !equalStrings(props, []string{"dns_name", "target_address", "transport"}) {
		t.Fatalf("probe params properties = %v; a credential field would appear here", props)
	}
	for _, p := range props {
		if p == "credential" || p == "secret" || p == "password" {
			t.Fatalf("the probe schema carries %q -- it must present no credential", p)
		}
	}
	// The Go struct reads exactly these.
	if !equalStrings(structTags(reflect.TypeOf(probeParams{})), []string{"dns_name", "target_address", "transport"}) {
		t.Fatal("probeParams struct diverges from the contract")
	}
	assertTransportEnum(t, s)
}

func TestProbeResultMatchesTheContract(t *testing.T) {
	s := loadSchema(t, "AgentPushProbeResult")
	want := structTags(reflect.TypeOf(probeResult{}))
	if !equalStrings(schemaProps(s), want) {
		t.Fatalf("probe result properties %v != struct %v", schemaProps(s), want)
	}
}

// --------------------------------------------------------------------------- //
// The install: the confirmed key is required, the credential is username+secret
// --------------------------------------------------------------------------- //

func TestInstallParamsMatchTheContract(t *testing.T) {
	s := loadSchema(t, "AgentPushInstallParams")
	if s.AdditionalProperties == nil || *s.AdditionalProperties {
		t.Fatal("install params must forbid additional properties (no install_args, no script)")
	}
	// host_key_fingerprint is REQUIRED and never null (§4.1, §7.3): the agent
	// verifies against it, so it cannot be optional.
	if !contains(s.Required, "host_key_fingerprint") {
		t.Fatal("host_key_fingerprint must be required -- the install verifies against it")
	}
	for _, must := range []string{"target_address", "transport", "os", "arch", "release", "enroll_token", "credential"} {
		if !contains(s.Required, must) {
			t.Fatalf("%q must be required", must)
		}
	}
	// enroll_url is ADDITIVE (W2 superseded): present in the contract, never
	// required, so a control plane that does not send it is still valid wire.
	if _, ok := s.Properties["enroll_url"]; !ok {
		t.Fatal("install params lack enroll_url -- the winrm path enrolls against it")
	}
	if contains(s.Required, "enroll_url") {
		t.Fatal("enroll_url must stay optional on the wire (additive)")
	}
	// No command/script/argument field at the top level (falsifier 9).
	for _, forbidden := range []string{"install_args", "script", "command", "release_url", "interpreter", "args"} {
		if _, ok := s.Properties[forbidden]; ok {
			t.Fatalf("install params carry %q -- clause (b) forbids any command payload", forbidden)
		}
	}
	// The Go struct reads exactly the schema's properties.
	got := structTags(reflect.TypeOf(installParams{}))
	if !equalStrings(got, schemaProps(s)) {
		t.Fatalf("installParams struct %v != contract %v", got, schemaProps(s))
	}
	assertTransportEnum(t, s)

	// The credential $def is exactly username + secret and nothing else
	// (invariant #3: no free-form member where a secret could be smuggled).
	cred, ok := s.Defs["PushCredential"]
	if !ok {
		t.Fatal("PushCredential $def missing")
	}
	if cred.AdditionalProperties == nil || *cred.AdditionalProperties {
		t.Fatal("PushCredential must forbid additional properties")
	}
	if !equalStrings(schemaProps(cred), []string{"secret", "username"}) {
		t.Fatalf("PushCredential properties = %v", schemaProps(cred))
	}
	if !equalStrings(structTags(reflect.TypeOf(credential{})), []string{"secret", "username"}) {
		t.Fatal("credential struct diverges from the contract")
	}
	// The release $def carries version, sha256, url -- all three required.
	rel, ok := s.Defs["PushRelease"]
	if !ok {
		t.Fatal("PushRelease $def missing")
	}
	for _, must := range []string{"version", "sha256", "url"} {
		if !contains(rel.Required, must) {
			t.Fatalf("PushRelease.%s must be required", must)
		}
	}
}

func TestInstallResultMatchesTheContract(t *testing.T) {
	s := loadSchema(t, "AgentPushInstallResult")
	if !equalStrings(schemaProps(s), structTags(reflect.TypeOf(installResult{}))) {
		t.Fatalf("install result properties %v != struct %v",
			schemaProps(s), structTags(reflect.TypeOf(installResult{})))
	}
}

func assertTransportEnum(t *testing.T, s jsonSchema) {
	t.Helper()
	tp, ok := s.Properties["transport"]
	if !ok {
		t.Fatal("no transport property")
	}
	// `ssh` (Linux/macOS) or `winrm` (Windows, Kerberos only), plan
	// 2026-09-28-windows-push-winrm Task 1: the schema pins `transport` to
	// exactly that enum, so any further widening changes this file.
	if len(tp.Const) != 0 {
		t.Fatalf("transport is still a const %s; the widened contract is an enum", tp.Const)
	}
	if !equalStrings(tp.Enum, []string{"ssh", "winrm"}) {
		t.Fatalf("transport enum = %v; want exactly [ssh winrm]", tp.Enum)
	}
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func contains(hay []string, needle string) bool {
	for _, h := range hay {
		if h == needle {
			return true
		}
	}
	return false
}
