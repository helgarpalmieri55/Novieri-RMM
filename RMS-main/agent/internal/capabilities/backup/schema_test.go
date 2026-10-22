package backup

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strconv"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// THE EXPORTED SCHEMA IS THE CONTRACT, AND THIS FILE IS WHERE THAT IS ENFORCED
// RATHER THAN ASSERTED.
//
// Copied in shape from internal/capabilities/site/schema_test.go, which caught
// real drift. The seven files under docs/kernel/wire-protocol/ are generated
// from app/modules/backup/schema.py by scripts/export_wire_schema.py, and
// backend/tests/test_wire_schema_export.py keeps them current with those
// models. This package's structs are built against THOSE FILES, so the chain is
// model -> schema -> struct with a guard on each link.
//
// internal/wire has the same guard for the message envelopes, but only for the
// types it implements; these seven are job params, job results and the pinned
// repository format, so without this file they would have no Go-side guard.
//
// WHAT THIS CATCHES THAT A REVIEW DOES NOT: a control-plane change that adds a
// required params member an old agent does not read, renames `chunk_ids`,
// widens `snapshot`'s enum, or adds a member to a result model that is
// `extra="forbid"` — where an added field does not get ignored, it raises in
// the result handler and app/jobs/engine.py::complete converts the WHOLE job to
// failed.

const schemaDir = "../../../../docs/kernel/wire-protocol"

type jsonSchema struct {
	Title      string                `json:"title"`
	Type       string                `json:"type"`
	Properties map[string]jsonSchema `json:"properties"`
	Required   []string              `json:"required"`
	// `additionalProperties` is a BOOL on every model here (extra="forbid"
	// renders `false`) but a SCHEMA on a `dict[str, str]` member -- Pydantic
	// emits `{"type": "string"}` for BackupUploadGrant.fields. Raw, with
	// `closed` doing the reading, because a *bool cannot hold both and
	// unmarshalling would fail on the object rather than on anything that
	// matters.
	AdditionalProperties json.RawMessage       `json:"additionalProperties"`
	Enum                 []json.RawMessage     `json:"enum"`
	Const                json.RawMessage       `json:"const"`
	Default              json.RawMessage       `json:"default"`
	Items                *jsonSchema           `json:"items"`
	MaxItems             *int                  `json:"maxItems"`
	Pattern              string                `json:"pattern"`
	Ref                  string                `json:"$ref"`
	Defs                 map[string]jsonSchema `json:"$defs"`
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

// closed reports whether a schema forbids members it does not declare, which
// is the `extra="forbid"` half of the contract: an added member on a result
// model does not get ignored, it raises in the server's result handler and
// fails the WHOLE job.
func closed(s jsonSchema) bool {
	var b bool
	return json.Unmarshal(s.AdditionalProperties, &b) == nil && !b
}

// jsonTags returns the json tag names a struct marshals and which are
// pointers.
//
// `required` is the schema's own required list, so the omitempty check below
// can be SCHEMA-AWARE rather than a blanket ban. The danger omitempty poses is
// scoped to REQUIRED fields: every model here is extra="forbid" with Pydantic
// defaults, so an omitted key that the schema requires does not mean 'unset'
// — it means the server silently substitutes its own value for the one this
// agent measured. A field the schema does NOT require carries no such risk:
// an omitted key and an explicit zero value are the same fact to a model
// built with `default_factory`, and extra="forbid" only rejects UNDECLARED
// keys, never absent optional ones. Every model this package owned until
// wire.ChunkQuery/wire.ChunkGrant had all-required fields, so the blanket
// rule and this narrower one agreed by coincidence; wire.ChunkQuery.Check and
// .Stored (and wire.ChunkGrant.New) are the first fields in scope that are
// genuinely optional, which is what exposed the difference. This mirrors
// internal/wire/schema_test.go's TestRequiredOutboundPropertiesAreNeverOmitempty,
// so the two packages stop disagreeing about one contract.
func jsonTags(t *testing.T, typ reflect.Type, required []string) (names map[string]bool, pointer map[string]bool) {
	t.Helper()
	req := make(map[string]bool, len(required))
	for _, r := range required {
		req[r] = true
	}
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
		if strings.Contains(tag, "omitempty") && req[name] {
			t.Fatalf("%s.%s is omitempty on a REQUIRED field. This model is extra=\"forbid\" with "+
				"Pydantic defaults, so an omitted key here does not mean 'unset' — it means the "+
				"server silently substitutes its own value for the one this agent measured",
				typ.Name(), f.Name)
		}
	}
	return names, pointer
}

// assertExactProperties is the both-directions check: no schema property
// without a Go field (the server may already be sending it) and no Go field
// without a schema property (the server would reject the document).
func assertExactProperties(t *testing.T, title string, s jsonSchema, typ reflect.Type) (
	names, pointer map[string]bool,
) {
	t.Helper()
	names, pointer = jsonTags(t, typ, s.Required)
	for prop := range s.Properties {
		if !names[prop] {
			t.Errorf("%s property %q has no field in %s", title, prop, typ.Name())
		}
	}
	for name := range names {
		if _, ok := s.Properties[name]; !ok {
			t.Errorf("%s reads/emits %q, which %s does not define", typ.Name(), name, title)
		}
	}
	for _, r := range s.Required {
		if !names[r] {
			t.Errorf("%s requires %q and %s has no such field", title, r, typ.Name())
		}
	}
	if !closed(s) {
		t.Errorf("%s is no longer additionalProperties:false", title)
	}
	return names, pointer
}

// assertExactPropertiesAllowing is assertExactProperties for a model the
// format-2 server release widens with ext's own members (spec-server-backup.md
// §5). Each may be absent from the schema (a server that never sends format 2)
// or present and NOT required (one that does; an old agent never sends or reads
// it). Required, it would be an outage for every agent in the field.
func assertExactPropertiesAllowing(t *testing.T, title string, s jsonSchema, typ, ext reflect.Type) (
	names, pointer map[string]bool,
) {
	t.Helper()
	req := map[string]bool{}
	for _, r := range s.Required {
		req[r] = true
	}
	filtered := s
	filtered.Properties = make(map[string]jsonSchema, len(s.Properties))
	for k, v := range s.Properties {
		filtered.Properties[k] = v
	}
	for i := 0; i < ext.NumField(); i++ {
		f := ext.Field(i)
		if f.Anonymous {
			continue
		}
		name := strings.Split(f.Tag.Get("json"), ",")[0]
		if _, ok := filtered.Properties[name]; !ok {
			continue
		}
		if req[name] {
			t.Errorf("%s REQUIRES the format-2 member %q; no agent before format 2 sends it", title, name)
		}
		delete(filtered.Properties, name)
	}
	return assertExactProperties(t, title, filtered, typ)
}

// enumOf reads a string enum out of a property.
func enumOf(t *testing.T, s jsonSchema) []string {
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

func assertEnum(t *testing.T, what string, got []string, want ...string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("%s enum is %v; this package handles exactly %v", what, got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("%s enum is %v; this package handles exactly %v", what, got, want)
		}
	}
}

// nullable reports whether a property's type is `X | null` (Pydantic renders
// that as an anyOf with a null branch).
func nullable(s jsonSchema) bool {
	for _, alt := range s.AnyOf {
		if alt.Type == "null" {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// backup.run
// ---------------------------------------------------------------------------

func TestRunParamsImplementsExactlyTheExportedBackupRunParams(t *testing.T) {
	s := loadSchema(t, "BackupRunParams")
	names, pointer := assertExactPropertiesAllowing(t, "BackupRunParams", s, reflect.TypeOf(runParams{}),
		reflect.TypeOf(runParamsV2{}))

	// Nothing here is a pointer, and the reason differs from site.wake's.
	// site.wake's optional members have NON-ZERO schema defaults, so "absent"
	// and "zero" are different statements there. Both optional members here
	// (`sources`, `dedup_params`) default to `{}` — which is precisely what the
	// zero value of their structs decodes to.
	for name := range names {
		if pointer[name] {
			t.Errorf("runParams.%s is a pointer; no BackupRunParams member needs to distinguish "+
				"absent from its zero value", name)
		}
	}

	// `auto` arrives with format 2 (spec-server-backup.md §4.4). The enum may
	// carry it or not yet; it must never carry a kind this agent cannot parse.
	enum := enumOf(t, s.Properties["snapshot"])
	handled := map[string]bool{snapshotVSS: true, snapshotLVM: true, snapshotAPFS: true, snapshotAuto: true}
	for _, v := range enum {
		if !handled[v] {
			t.Errorf("BackupRunParams.snapshot permits %q, which this agent refuses", v)
		}
	}
	// The Go constants must BE those strings, not merely resemble them.
	for _, k := range []string{snapshotVSS, snapshotLVM, snapshotAPFS} {
		var found bool
		for _, v := range enumOf(t, s.Properties["snapshot"]) {
			if v == k {
				found = true
			}
		}
		if !found {
			t.Errorf("this package uses snapshot kind %q, which the schema enum does not permit", k)
		}
	}

	// `repo_key` is the repository master key and it is REQUIRED. If it ever
	// stops being required, the control plane has changed how key material
	// reaches the endpoint and this capability's whole security story needs
	// re-reading before the field is dropped.
	var required bool
	for _, r := range s.Required {
		if r == "repo_key" {
			required = true
		}
	}
	if !required {
		t.Error("BackupRunParams no longer requires repo_key. Key delivery has changed shape; " +
			"see the security note in backup.go before adapting this struct")
	}
}

func TestRunResultImplementsExactlyTheExportedBackupRunResult(t *testing.T) {
	s := loadSchema(t, "BackupRunResult")
	_, pointer := assertExactPropertiesAllowing(t, "BackupRunResult", s, reflect.TypeOf(runResult{}),
		reflect.TypeOf(runResultV2{}))

	assertEnum(t, "BackupRunResult.state", enumOf(t, s.Properties["state"]), "success", "failed")

	// A nullable member must be a pointer and a non-nullable one must not be.
	// `manifest_ref` is the case that matters: ingest_backup_result checks it
	// for TRUTHINESS, so a success carrying "" lands the run on failed with
	// "success result missing manifest_ref". Emitting "" where nil was meant is
	// therefore a silent lost backup point.
	for name, p := range s.Properties {
		if _, ok := pointer[name]; !ok {
			continue // a format-2 member, checked by assertExactPropertiesAllowing
		}
		if nullable(p) && !pointer[name] {
			t.Errorf("BackupRunResult.%s is `%s | null` and runResult.%s is not a pointer",
				name, "string", name)
		}
		if !nullable(p) && pointer[name] {
			t.Errorf("BackupRunResult.%s is not nullable and runResult.%s is a pointer", name, name)
		}
	}
}

// ---------------------------------------------------------------------------
// backup.restore
// ---------------------------------------------------------------------------

func TestRestoreParamsImplementsExactlyTheExportedBackupRestoreParams(t *testing.T) {
	s := loadSchema(t, "BackupRestoreParams")
	names, pointer := assertExactPropertiesAllowing(t, "BackupRestoreParams", s,
		reflect.TypeOf(restoreParams{}), reflect.TypeOf(restoreParamsV2{}))

	// EVERY format-1 member is required here, so nothing may be a pointer and
	// nothing may be missing.
	if len(s.Required) != len(names) {
		t.Errorf("BackupRestoreParams requires %v but restoreParams has %d fields; every member "+
			"of this model is required", s.Required, len(names))
	}
	for name := range names {
		if pointer[name] {
			t.Errorf("restoreParams.%s is a pointer and every BackupRestoreParams member is "+
				"required", name)
		}
	}

	assertEnum(t, "BackupRestoreParams.mode", enumOf(t, s.Properties["mode"]), "restore", "verify")
	if modeRestore != "restore" || modeVerify != "verify" {
		t.Errorf("this package's mode constants are %q/%q", modeRestore, modeVerify)
	}
}

func TestRestoreResultImplementsExactlyTheExportedBackupRestoreResult(t *testing.T) {
	s := loadSchema(t, "BackupRestoreResult")
	_, pointer := assertExactPropertiesAllowing(t, "BackupRestoreResult", s,
		reflect.TypeOf(restoreResult{}), reflect.TypeOf(restoreResultV2{}))

	assertEnum(t, "BackupRestoreResult.state", enumOf(t, s.Properties["state"]),
		"success", "failed")

	// `mismatch` is `list[str] | null`. A nil Go slice marshals to null, which
	// is what "nothing differed" should say, so it is NOT a pointer — but
	// `error`, a `str | null`, must be.
	if pointer["mismatch"] {
		t.Error("restoreResult.Mismatch is a pointer; a nil slice already marshals to null")
	}
	if !pointer["error"] {
		t.Error("restoreResult.Error is not a pointer; BackupRestoreResult.error is `str | null` " +
			"and \"\" is not null")
	}
	if !nullable(s.Properties["mismatch"]) {
		t.Error("BackupRestoreResult.mismatch is no longer nullable; a nil slice would now be " +
			"rejected")
	}
}

// ---------------------------------------------------------------------------
// The pinned repository format
// ---------------------------------------------------------------------------

func TestManifestEntryImplementsExactlyTheExportedManifestEntry(t *testing.T) {
	s := loadSchema(t, "ManifestEntry")
	_, pointer := assertExactProperties(t, "ManifestEntry", s, reflect.TypeOf(manifestEntry{}))

	assertEnum(t, "ManifestEntry.kind", enumOf(t, s.Properties["kind"]), "file", "dir", "symlink")
	for _, k := range []string{kindFile, kindDir, kindSymlink} {
		var ok bool
		for _, v := range enumOf(t, s.Properties["kind"]) {
			if v == k {
				ok = true
			}
		}
		if !ok {
			t.Errorf("this package writes kind %q, which the schema enum does not permit", k)
		}
	}

	if !pointer["target"] {
		t.Error("manifestEntry.Target is not a pointer; ManifestEntry.target is `str | null` and " +
			"a symlink to \"\" is not the same as not-a-symlink")
	}
	if pointer["chunk_ids"] {
		t.Error("manifestEntry.ChunkIDs is a pointer")
	}
	// `chunk_ids: list[str] = []` does NOT accept null. A nil Go slice
	// marshals to null, so a dir entry built without an explicit empty slice
	// fails validation on the server for every directory in the tree.
	if nullable(s.Properties["chunk_ids"]) {
		t.Error("ManifestEntry.chunk_ids became nullable; newEntry's explicit empty slice can be " +
			"relaxed, and this test's premise is gone")
	}
	// `mtime` is a float, not an integer. Writing it as an int loses
	// sub-second resolution on every file in the fleet.
	if got := s.Properties["mtime"].Type; got != "number" {
		t.Errorf("ManifestEntry.mtime type = %q, want number; this package emits a float64", got)
	}
}

func TestFiletreeStreamImplementsExactlyTheExportedFiletreeStream(t *testing.T) {
	s := loadSchema(t, "FiletreeStream")
	assertExactProperties(t, "FiletreeStream", s, reflect.TypeOf(filetreeStream{}))

	var want string
	if err := json.Unmarshal(s.Properties["type"].Const, &want); err != nil {
		t.Fatalf("FiletreeStream.type has no const: %v", err)
	}
	if want != streamTypeFiletree {
		t.Errorf("FiletreeStream.type const is %q and this package writes %q", want,
			streamTypeFiletree)
	}
}

func TestBackupManifestImplementsExactlyTheExportedBackupManifest(t *testing.T) {
	s := loadSchema(t, "BackupManifest")
	assertExactProperties(t, "BackupManifest", s, reflect.TypeOf(backupManifest{}))

	if got := s.Properties["schema_version"].Type; got != "integer" {
		t.Errorf("BackupManifest.schema_version type = %q, want integer", got)
	}
	// The `streams` list is the forward-compatibility hinge (spec §5). If it
	// ever stops being a list, a later `block` or `named` stream can no longer
	// slot in and the format decision has been reversed.
	if got := s.Properties["streams"].Type; got != "array" {
		t.Errorf("BackupManifest.streams type = %q, want array. §5's forward-compatibility hinge "+
			"is that `streams` is a LIST: an image-level `block` stream is an added element, not "+
			"a format rewrite", got)
	}
}

func TestAManifestThisAgentWritesRoundTripsThroughTheSchemasShape(t *testing.T) {
	// The struct tags can be individually right and the DOCUMENT still wrong:
	// a nil slice marshalling to `null` where the server's model declares
	// `list[X] = []` is rejected, and no field-by-field check sees it.
	//
	// FALSIFYING OBSERVATION: a manifest built by this package whose JSON
	// carries `null` at a key the server model cannot accept it at.
	tgt := "../elsewhere"
	m := backupManifest{
		SchemaVersion: manifestSchemaVersion,
		Streams: []filetreeStream{{
			Type: streamTypeFiletree,
			Entries: []manifestEntry{
				newEntry("/a", kindDir, 0o755, 0, 1.5),
				newEntry("/a/b", kindFile, 0o644, 3, 2.25),
				func() manifestEntry {
					e := newEntry("/a/c", kindSymlink, 0o777, 0, 3)
					e.Target = &tgt
					return e
				}(),
			},
		}},
	}
	raw, err := json.Marshal(m)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	streams, _ := doc["streams"].([]any)
	if len(streams) != 1 {
		t.Fatalf("streams = %v", doc["streams"])
	}
	entries, _ := streams[0].(map[string]any)["entries"].([]any)
	if len(entries) != 3 {
		t.Fatalf("entries = %v", entries)
	}
	for i, e := range entries {
		obj := e.(map[string]any)
		ids, ok := obj["chunk_ids"]
		if !ok {
			t.Fatalf("entry %d has no chunk_ids key", i)
		}
		if ids == nil {
			t.Fatalf("entry %d emitted chunk_ids: null. ManifestEntry.chunk_ids is "+
				"`list[str] = []` server-side, which does not accept null: every directory in "+
				"the tree would fail validation", i)
		}
	}
	// The symlink's target must survive as a string, and the non-symlinks' as
	// null.
	if got := entries[2].(map[string]any)["target"]; got != "../elsewhere" {
		t.Errorf("symlink target marshalled as %#v", got)
	}
	if got := entries[1].(map[string]any)["target"]; got != nil {
		t.Errorf("a file entry emitted target %#v, want null", got)
	}
}

// ---------------------------------------------------------------------------
// The two UNDECLARED contracts: `sources`, `dedup_params`, `target`
// ---------------------------------------------------------------------------

func TestTheDedupDefaultsAreTheServersDefaultsAndNotThisPackagesOpinion(t *testing.T) {
	// `dedup_params` is `additionalProperties: true` with no declared
	// properties, so the exported schema pins NOTHING about its contents. The
	// real contract is app/modules/backup/repository.py::DEFAULT_DEDUP, which is
	// what every repository row is created with. This package's fallbacks must
	// equal it, because they are what a job with `dedup_params: {}` chunks with
	// — and a chunker parameter that disagrees forks the repository's chunk
	// domain silently (spec §5: "changing them starts a new chunk domain").
	//
	// FALSIFYING OBSERVATION: a number in repository.py that this package does
	// not use. That is a review-invisible dedup regression, not a style nit.
	const src = "../../../../backend/app/modules/backup/repository.py"
	raw, err := os.ReadFile(src)
	if err != nil {
		t.Fatalf("the server's dedup defaults are unreadable: %v", err)
	}
	block := regexp.MustCompile(`(?s)DEFAULT_DEDUP\s*=\s*\{(.*?)\}`).FindStringSubmatch(string(raw))
	if block == nil {
		t.Fatalf("DEFAULT_DEDUP is gone from %s; this test's premise is stale", src)
	}
	fields := map[string]string{}
	for _, m := range regexp.MustCompile(`"(\w+)"\s*:\s*("?[\w-]+"?)`).FindAllStringSubmatch(block[1], -1) {
		fields[m[1]] = strings.Trim(m[2], `"`)
	}
	if len(fields) != 5 {
		t.Fatalf("parsed %d members out of DEFAULT_DEDUP, want 5: %v", len(fields), fields)
	}

	got, err := dedupSpec{}.defaults()
	if err != nil {
		t.Fatalf("this package's own defaults do not validate: %v", err)
	}
	num := func(k string) int {
		v, err := strconv.Atoi(fields[k])
		if err != nil {
			t.Fatalf("DEFAULT_DEDUP[%q] = %q is not a number", k, fields[k])
		}
		return v
	}
	for _, c := range []struct {
		key  string
		want any
		got  any
	}{
		{"algo", fields["algo"], got.Algo},
		{"hash", fields["hash"], got.Hash},
		{"avg", num("avg"), got.Avg},
		{"min", num("min"), got.Min},
		{"max", num("max"), got.Max},
	} {
		if c.want != c.got {
			t.Errorf("DEFAULT_DEDUP[%q] is %v server-side and this package defaults to %v; a job "+
				"with dedup_params:{} would chunk into a different domain than every other agent",
				c.key, c.want, c.got)
		}
	}
}

func TestTheSourcesAndTargetShapesAreTheOnesTheServerActuallySends(t *testing.T) {
	// `sources` and `target` are also `additionalProperties: true` with nothing
	// declared. Their real shape lives in the server's own tests and in
	// api.py, so this reads those files rather than trusting a comment.
	//
	// FALSIFYING OBSERVATION: the server building a policy with a key other
	// than `paths`, or a restore target with a key other than `path`. Either
	// would make this agent back up nothing / restore nowhere while reporting
	// success — the exact failure `sourcesSpec` refuses on.
	for _, c := range []struct {
		file    string
		pattern string
		what    string
	}{
		{"../../../../backend/tests/test_backup_worker.py", `sources=\{"paths":`, "sources.paths"},
		{"../../../../backend/tests/test_backup_dispatch.py", `sources=\{"paths":`, "sources.paths"},
		{"../../../../backend/tests/test_backup_restore.py", `target=\{"path":`, "target.path"},
	} {
		raw, err := os.ReadFile(c.file)
		if err != nil {
			t.Fatalf("%s: %v", c.file, err)
		}
		if !regexp.MustCompile(c.pattern).Match(raw) {
			t.Errorf("%s no longer shows %s; this package reads that key and would silently "+
				"protect zero bytes / restore nothing", c.file, c.what)
		}
	}

	// And the shape is actually decoded, not merely named.
	var p runParams
	if err := json.Unmarshal([]byte(`{"repository_id":"r","storage_ref":"s","repo_key":"k",
		"snapshot":"vss","sources":{"paths":["/data"],"unknown":1},"dedup_params":{}}`), &p); err != nil {
		t.Fatalf("a params document in the server's own shape did not decode: %v", err)
	}
	roots, err := p.Sources.roots()
	if err != nil || len(roots) != 1 || roots[0] != "/data" {
		t.Fatalf("sources.paths decoded to %v (err %v)", roots, err)
	}

	// api.py::verify_point sends target={} and restore_point sends the
	// operator's {"path": ...}. Both must decode.
	var rp restoreParams
	if err := json.Unmarshal([]byte(`{"repository_id":"r","storage_ref":"s","manifest_ref":"m",
		"selection":[],"target":{},"mode":"verify","repo_key":"k"}`), &rp); err != nil {
		t.Fatalf("the verify params the server actually sends did not decode: %v", err)
	}
	if rp.Target.Path != "" {
		t.Errorf("target:{} decoded to path %q", rp.Target.Path)
	}
}

// ---------------------------------------------------------------------------
// The dedup exchange (spec §5.2)
// ---------------------------------------------------------------------------
//
// These two ARE message envelopes, so unlike everything above they carry `v`
// and `kind` in the schema and not in the Go struct — framing belongs to
// internal/wire, which does not implement these kinds yet
// (internal/wire/schema_test.go's `notImplemented` says so and why). Framing
// members are excluded EXPLICITLY and by name: a wildcard would also swallow a
// real member the control plane added.

var exchangeFraming = map[string]bool{"v": true, "kind": true}

// assertExactPayload is assertExactProperties with the envelope's framing
// members excluded by name.
func assertExactPayload(t *testing.T, title string, s jsonSchema, typ reflect.Type) (
	names, pointer map[string]bool,
) {
	t.Helper()
	names, pointer = jsonTags(t, typ, s.Required)
	for prop := range s.Properties {
		if exchangeFraming[prop] {
			continue
		}
		if !names[prop] {
			t.Errorf("%s property %q has no field in %s", title, prop, typ.Name())
		}
	}
	for name := range names {
		if _, ok := s.Properties[name]; !ok {
			t.Errorf("%s reads/emits %q, which %s does not define", typ.Name(), name, title)
		}
	}
	for _, r := range s.Required {
		if exchangeFraming[r] {
			continue
		}
		if !names[r] {
			t.Errorf("%s requires %q and %s has no such field", title, r, typ.Name())
		}
	}
	if !closed(s) {
		t.Errorf("%s is no longer additionalProperties:false", title)
	}
	return names, pointer
}

func TestChunkQueryImplementsExactlyTheExportedBackupChunkQueryMsg(t *testing.T) {
	s := loadSchema(t, "BackupChunkQueryMsg")
	assertExactPayload(t, "BackupChunkQueryMsg", s, reflect.TypeOf(wire.ChunkQuery{}))

	// THE BATCH BOUND IS PINNED TO THE SCHEMA, NOT TO A COMMENT. If the control
	// plane tightens `maxItems` and this agent keeps sending 1024, every
	// oversized batch is dropped at `parse_agent_message` — the agent gets no
	// answer at all, and a run that waits for one hangs rather than failing.
	for _, member := range []string{"check", "stored"} {
		got := s.Properties[member].MaxItems
		if got == nil {
			t.Fatalf("BackupChunkQueryMsg.%s has no maxItems; the batch is unbounded on the "+
				"server side and this agent's bound is now the only one", member)
		}
		if *got != maxChunkBatch {
			t.Errorf("BackupChunkQueryMsg.%s permits %d ids per batch and this agent sends up "+
				"to %d. A batch the control plane drops at the parse layer is answered by "+
				"silence, not by an error", member, *got, maxChunkBatch)
		}
	}

	// THE CHUNK ID PATTERN. The server rejects an id that is not 64 lowercase
	// hex characters, because the id becomes an object key. This asserts that
	// what `chunkID` actually produces satisfies it — a mismatch would refuse
	// every batch this agent ever sends, and it would refuse them silently.
	pattern := s.Properties["check"].Items.Pattern
	if pattern == "" {
		t.Fatal("BackupChunkQueryMsg.check items carry no pattern; the id that becomes an " +
			"object key is unconstrained on the wire")
	}
	re, err := regexp.Compile(pattern)
	if err != nil {
		t.Fatalf("the exported chunk-id pattern %q does not compile in Go: %v", pattern, err)
	}
	h := newBlake3()
	for _, sample := range [][]byte{nil, {}, []byte("a"), bytes.Repeat([]byte{0xff}, 1<<16)} {
		if id := chunkID(h, sample); !re.MatchString(id) {
			t.Errorf("chunkID produced %q, which the exported pattern %q rejects", id, pattern)
		}
	}
	// And the negative: a pattern that accepted anything would pass the loop
	// above while letting a traversal-shaped id onto the wire.
	for _, bad := range []string{"", strings.Repeat("../", 20) + "abcd", strings.ToUpper(chunkID(h, nil))} {
		if re.MatchString(bad) {
			t.Errorf("the exported pattern %q accepts %q", pattern, bad)
		}
	}
}

func TestChunkGrantImplementsExactlyTheExportedBackupChunkGrantMsg(t *testing.T) {
	s := loadSchema(t, "BackupChunkGrantMsg")
	assertExactPayload(t, "BackupChunkGrantMsg", s, reflect.TypeOf(wire.ChunkGrant{}))

	if got := s.Properties["new"].MaxItems; got == nil || *got != maxChunkBatch {
		t.Errorf("BackupChunkGrantMsg.new is bounded at %v; this agent expects at most %d",
			got, maxChunkBatch)
	}

	// The grant itself lives in $defs, exactly as HealthReportLine does inside
	// HealthReportMsg. Reached through the $ref rather than by assuming the
	// name, so a rename shows up here.
	ref := s.Properties["upload"].Ref
	const want = "#/$defs/BackupUploadGrant"
	if ref != want {
		t.Fatalf("BackupChunkGrantMsg.upload is %q, not %q", ref, want)
	}
	grant, ok := s.Defs["BackupUploadGrant"]
	if !ok {
		t.Fatal("BackupChunkGrantMsg carries no BackupUploadGrant definition")
	}
	grantSchema := jsonSchema{
		Title:                "BackupUploadGrant",
		Properties:           grant.Properties,
		Required:             grant.Required,
		AdditionalProperties: grant.AdditionalProperties,
	}
	assertExactPayload(t, "BackupUploadGrant", grantSchema, reflect.TypeOf(wire.UploadGrant{}))

	// `max_bytes` is the `content-length-range` the control plane signed into
	// the policy, and this agent checks a sealed chunk against it before
	// spending a round trip. If it stopped being required, the agent would read
	// zero and (see upload()) skip the check entirely.
	var required bool
	for _, r := range grant.Required {
		if r == "max_bytes" {
			required = true
		}
	}
	if !required {
		t.Error("BackupUploadGrant no longer requires max_bytes; this agent's local " +
			"size check silently becomes a no-op, see dedup.go::upload")
	}
}
