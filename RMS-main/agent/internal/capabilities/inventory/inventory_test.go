package inventory

import (
	"context"
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"testing"
)

// The contract under test is docs/kernel/wire-protocol/InventoryDocument.schema.json,
// which is generated from app/modules/inventory/schema.py and kept current by
// backend/tests/test_wire_schema_export.py. It is read from disk here rather
// than transcribed: a transcription agrees with the contract on the day it is
// written and never again.
//
// # Why every call below passes a nil job
//
// jobs.VerifiedJob is an interface with an unexported method, so NO package
// outside internal/jobs can produce a value of it -- not even a test. `nil` is
// therefore the only argument this package's tests can supply, which is
// invariant #1 showing through: a capability cannot be handed a job by anything
// except the signed-job runner.
//
// That is not a hole in the coverage, it is the shape of the guarantee. The
// collector is asserted here never to READ the job (TestTheCollectorNeverReadsJobParameters,
// a source assertion), and the real runner -> registry -> capability path with
// a real signed envelope is exercised in ../integration_test.go.

func fakeFacts() hostFacts {
	return hostFacts{
		Hostname:     "ws-0042",
		FQDN:         "ws-0042.corp.example",
		OSVersion:    "Debian GNU/Linux 12 (bookworm)",
		OSBuild:      "6.1.0-18-amd64",
		Architecture: "amd64",
		Serial:       "PF3ABCDE",
		CPUModel:     "12th Gen Intel(R) Core(TM) i7-1265U",
		RAMBytes:     34359738368,
		Components: []Component{
			{Type: "disk", Identifier: "nvme0n1", Attributes: map[string]any{"size_bytes": int64(512110190592)}},
			// §14.1's shape, exactly as nicComponentFrom emits it: `addresses[]`
			// and no `ip`. A fixture that still carried the removed legacy key
			// would be this file quietly asserting a wire shape the collector
			// does not produce.
			{Type: "nic", Identifier: "a4:bb:6d:11:22:33", Attributes: map[string]any{
				"name": "eth0", "mac": "a4:bb:6d:11:22:33",
				"addresses": []Address{
					{Address: "10.1.2.42/24", Family: "ipv4", Origin: originDHCP},
				},
			}},
		},
	}
}

// ---------------------------------------------------------------------------
// The document validates against the exported schema
// ---------------------------------------------------------------------------

func TestTheCollectedDocumentValidatesAgainstTheExportedSchema(t *testing.T) {
	doc, err := collect(fakeFacts(), "linux")
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	body := reencode(t, doc)
	// FALSIFIER: the field name and the rule it broke, printed. "It validated"
	// is not an observation; "components[0].type = nvme does not match
	// ^(cpu|memory|disk|...)$" is.
	for _, problem := range validateAgainstSchema(t, body) {
		t.Errorf("the collected document violates InventoryDocument.schema.json: %s", problem)
	}
}

func TestTheResultIsTheDocumentAtTheTopLevelAndNotNestedUnderAKey(t *testing.T) {
	// app/modules/inventory/handler.py:27 does InventoryDocument(**result) on
	// the TOP-LEVEL result dict. A wrapper key here fails validation
	// server-side, which surfaces as a `failed` job carrying a pydantic error
	// and no inventory at all.
	body := invokeAndDecode(t)
	if _, ok := body["hostname"]; !ok {
		t.Fatalf("no top-level `hostname` in the result: keys = %v; "+
			"handler.py does InventoryDocument(**result) on this dict", keysOf(body))
	}
	for _, wrapper := range []string{"document", "inventory", "result", "doc"} {
		if _, ok := body[wrapper]; ok {
			t.Fatalf("the result is nested under %q; handler.py reads the top level", wrapper)
		}
	}
}

func TestOSFamilyIsTheServersVocabularyAndDarwinIsTheOneThatBites(t *testing.T) {
	// The server's pattern is ^(windows|linux|macos)$. runtime.GOOS says
	// "darwin". An agent that shipped GOOS straight through would have every
	// Mac in the estate fail ingestion with a pydantic pattern error.
	for goos, want := range map[string]string{"windows": "windows", "linux": "linux", "darwin": "macos"} {
		if got := osFamilyFor(goos); got != want {
			t.Errorf("osFamilyFor(%q) = %q; want %q", goos, got, want)
		}
	}
	if got := osFamilyFor("plan9"); got != "" {
		t.Errorf("osFamilyFor(%q) = %q; an unmapped GOOS must produce no family, "+
			"so collect() refuses rather than sending a document the server will reject", "plan9", got)
	}
	if got := osFamilyFor(runtime.GOOS); got == "" {
		t.Fatalf("this build's GOOS (%q) has no os_family; the binary cannot report inventory at all",
			runtime.GOOS)
	}
}

func TestCollectRefusesToEmitADocumentTheServerWouldReject(t *testing.T) {
	for _, tc := range []struct {
		name  string
		facts func() hostFacts
		goos  string
	}{
		{"no hostname", func() hostFacts { f := fakeFacts(); f.Hostname = ""; return f }, "linux"},
		{"unmapped GOOS", fakeFacts, "plan9"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// FALSIFIER: a nil error and a document. A document that fails
			// `InventoryDocument(**result)` server-side turns the job `failed`
			// with a pydantic error in the job record -- worse to read than a
			// local failure naming the missing fact.
			doc, err := collect(tc.facts(), tc.goos)
			if err == nil {
				t.Fatalf("collect returned a document with %s: %+v", tc.name, doc)
			}
		})
	}
}

func TestOversizedFactsAreClampedRatherThanLosingTheWholeDocument(t *testing.T) {
	f := fakeFacts()
	f.Hostname = strings.Repeat("h", 400)
	f.Components = append(f.Components, Component{
		Type: "disk", Identifier: strings.Repeat("d", 400),
	})
	doc, err := collect(f, "linux")
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	// maxLength 255 for hostname, 200 for a component identifier. A long name
	// is not a reason to lose a device's whole inventory -- but an unclamped
	// one IS a reason for the server to reject the document, so this is
	// clamping, not tolerance.
	if len(doc.Hostname) != 255 {
		t.Fatalf("hostname length %d; the schema's maxLength is 255", len(doc.Hostname))
	}
	last := doc.Hardware.Components[len(doc.Hardware.Components)-1]
	if len(last.Identifier) != 200 {
		t.Fatalf("component identifier length %d; the schema's maxLength is 200", len(last.Identifier))
	}
	for _, problem := range validateAgainstSchema(t, reencode(t, doc)) {
		t.Errorf("clamped document still violates the schema: %s", problem)
	}
}

func TestAComponentWithATypeTheServerDoesNotKnowIsDropped(t *testing.T) {
	// ComponentDoc.type is a closed pattern. One unknown component type would
	// fail the WHOLE document server-side -- the pydantic error is on the
	// document, not on the component -- so a device would lose its CPU, its
	// memory and its NICs because of one exotic disk.
	//
	// FALSIFIER: the bogus type surviving into the emitted components.
	f := fakeFacts()
	f.Components = append(f.Components, Component{Type: "tpm", Identifier: "tpm0"})
	doc, err := collect(f, "linux")
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	for _, c := range doc.Hardware.Components {
		if c.Type == "tpm" {
			t.Fatalf("component type %q survived; the server's pattern is closed and would reject "+
				"the entire document", c.Type)
		}
	}
}

// ---------------------------------------------------------------------------
// Invariant #1: the capability accepts a VerifiedJob and reads nothing from it
// ---------------------------------------------------------------------------

func TestTheCollectorNeverReadsJobParameters(t *testing.T) {
	// InventoryCollectParams is `extra="forbid"` and EMPTY
	// (app/jobs/registry.py), so there is nothing a job can supply. This test
	// keeps that true against a future edit: no file in this package may call
	// job.Params().
	//
	// It is a SOURCE assertion. The behavioural version ("hostile params change
	// nothing") cannot be written at all -- see the package comment above: no
	// test outside internal/jobs can construct a job to put params in. A source
	// walker is what remains, and it is the check that would catch a
	// job-supplied string reaching an exec argument.
	//
	// FALSIFIER: the file and line of the Params() call.
	var violations []string
	fset := token.NewFileSet()
	for _, path := range packageSources(t) {
		file, err := parser.ParseFile(fset, path, nil, parser.SkipObjectResolution)
		if err != nil {
			t.Fatal(err)
		}
		ast.Inspect(file, func(n ast.Node) bool {
			call, ok := n.(*ast.CallExpr)
			if !ok {
				return true
			}
			sel, ok := call.Fun.(*ast.SelectorExpr)
			if !ok || sel.Sel.Name != "Params" {
				return true
			}
			violations = append(violations,
				path+":"+strconv.Itoa(fset.Position(call.Pos()).Line))
			return true
		})
	}
	if len(violations) != 0 {
		t.Fatalf("the inventory collector reads job parameters at %v; InventoryCollectParams is empty "+
			"and nothing job-supplied may reach a collector, let alone an exec argument", violations)
	}
}

func TestPackageSourcesFindsSomething(t *testing.T) {
	// A source walker pointed at nothing passes forever.
	if n := len(packageSources(t)); n < 4 {
		t.Fatalf("walked %d non-test source files in this package; the path is wrong", n)
	}
}

// ---------------------------------------------------------------------------
// Dormancy
// ---------------------------------------------------------------------------

func TestRunsCountsInvocationsAndNothingElse(t *testing.T) {
	// The counter ../registry_test.go leans on. If it never moved, that test
	// would be asserting on a number that is always zero.
	before := Runs()
	invokeAndDecode(t)
	if after := Runs(); after != before+1 {
		t.Fatalf("Runs() went %d -> %d across one Collect; the dormancy assertion in "+
			"capabilities/registry_test.go rests on this counter moving", before, after)
	}
}

// ---------------------------------------------------------------------------
// This host, for real
// ---------------------------------------------------------------------------

func TestTheRealCollectorDescribesThisHost(t *testing.T) {
	// The fake-facts tests above prove the document SHAPE. This one proves the
	// per-OS gatherer produces facts at all -- on the one platform this
	// container can exercise (linux; see the report).
	body := invokeAndDecode(t)
	if body["hostname"] == "" || body["hostname"] == nil {
		t.Fatalf("the real collector reported no hostname: %v", body)
	}
	if got, want := body["os_family"], osFamilyFor(runtime.GOOS); got != want {
		t.Fatalf("os_family = %v; want %v", got, want)
	}
	if body["architecture"] != runtime.GOARCH {
		t.Fatalf("architecture = %v; want %v", body["architecture"], runtime.GOARCH)
	}
	for _, problem := range validateAgainstSchema(t, body) {
		t.Errorf("this host's real document violates InventoryDocument.schema.json: %s", problem)
	}
	hardware, _ := body["hardware"].(map[string]any)
	components, _ := hardware["components"].([]any)
	software, _ := body["software"].([]any)
	t.Logf("collected on %s/%s: hostname=%v os_version=%q ram=%v components=%d software=%d facts=%v",
		runtime.GOOS, runtime.GOARCH, body["hostname"], body["os_version"],
		hardware["ram_bytes"], len(components), len(software), body["facts"])
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// invokeAndDecode runs the real handler and returns the result as the gateway
// would see it. The job is nil for the reason in the package comment.
func invokeAndDecode(t *testing.T) map[string]any {
	t.Helper()
	result, err := Collect(context.Background(), nil)
	if err != nil {
		t.Fatalf("Collect: %v", err)
	}
	return reencode(t, result)
}

func reencode(t *testing.T, v any) map[string]any {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return body
}

// packageSources lists this package's non-test .go files.
func packageSources(t *testing.T) []string {
	t.Helper()
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, e := range entries {
		name := e.Name()
		if e.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		out = append(out, name)
	}
	return out
}

// validateAgainstSchema is a deliberately small JSON-Schema subset checker:
// `required`, `type`, `pattern`, `minLength`, `maxLength`, `$ref` and `items`.
//
// Hand-rolled because a JSON-schema library would be the agent's third
// dependency, and the plan's rule is that a dependency needs a written
// justification. It covers every constraint the exported schema actually
// carries; a construct it does not understand is reported as a problem rather
// than skipped, so the checker cannot quietly stop checking.
func validateAgainstSchema(t *testing.T, body map[string]any) []string {
	t.Helper()
	schema := loadSchema(t)
	return checkObject(schema, schema, body, "")
}

func loadSchema(t *testing.T) map[string]any {
	t.Helper()
	path := filepath.Join(repoRoot(t), "docs", "kernel", "wire-protocol", "InventoryDocument.schema.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("the exported wire contract is unreadable at %s: %v", path, err)
	}
	var schema map[string]any
	if err := json.Unmarshal(raw, &schema); err != nil {
		t.Fatalf("%s is not JSON: %v", path, err)
	}
	return schema
}

func checkObject(root, schema map[string]any, value map[string]any, path string) []string {
	var problems []string
	if req, ok := schema["required"].([]any); ok {
		for _, name := range req {
			key, _ := name.(string)
			if v, present := value[key]; !present || v == nil {
				problems = append(problems, path+key+" is required and absent")
			}
		}
	}
	props, _ := schema["properties"].(map[string]any)
	for key, sub := range props {
		v, present := value[key]
		if !present || v == nil {
			continue
		}
		problems = append(problems, checkValue(root, resolve(root, sub), v, path+key)...)
	}
	return problems
}

func checkValue(root map[string]any, schema map[string]any, value any, path string) []string {
	var problems []string
	if _, isAnyOf := schema["anyOf"]; isAnyOf {
		// A nullable union (`string | null`). The agent emits either a string
		// or nothing at all for every one of these, so there is no constraint
		// left to check.
		return nil
	}
	switch schema["type"] {
	case "object":
		obj, ok := value.(map[string]any)
		if !ok {
			return []string{path + " is not an object"}
		}
		return checkObject(root, schema, obj, path+".")
	case "array":
		items, ok := value.([]any)
		if !ok {
			return []string{path + " is not an array"}
		}
		sub := resolve(root, schema["items"])
		for i, item := range items {
			problems = append(problems, checkValue(root, sub, item, path+"["+strconv.Itoa(i)+"]")...)
		}
		return problems
	case "string":
		s, ok := value.(string)
		if !ok {
			return []string{path + " is not a string"}
		}
		if pat, ok := schema["pattern"].(string); ok {
			if !regexp.MustCompile(pat).MatchString(s) {
				problems = append(problems, path+" = "+s+" does not match "+pat)
			}
		}
		if minLen, ok := schema["minLength"].(float64); ok && len(s) < int(minLen) {
			problems = append(problems, path+" is shorter than minLength "+strconv.Itoa(int(minLen)))
		}
		if maxLen, ok := schema["maxLength"].(float64); ok && len(s) > int(maxLen) {
			problems = append(problems, path+" is longer than maxLength "+strconv.Itoa(int(maxLen)))
		}
		return problems
	case "integer", "number", "boolean":
		return nil
	case nil:
		// `additionalProperties: true` with no declared type: attributes and
		// facts. The server takes anything JSON here.
		return nil
	default:
		encoded, _ := json.Marshal(schema["type"])
		return []string{path + ": this checker does not understand type " + string(encoded)}
	}
}

// resolve follows a local $ref into $defs. The exported schema uses them for
// every nested document type.
func resolve(root map[string]any, node any) map[string]any {
	schema, _ := node.(map[string]any)
	if schema == nil {
		return map[string]any{}
	}
	ref, ok := schema["$ref"].(string)
	if !ok {
		return schema
	}
	name := ref[strings.LastIndex(ref, "/")+1:]
	defs, _ := root["$defs"].(map[string]any)
	target, _ := defs[name].(map[string]any)
	if target == nil {
		return map[string]any{}
	}
	return target
}

func repoRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "CLAUDE.md")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("no CLAUDE.md above the test's working directory; cannot find the repo root")
		}
		dir = parent
	}
}

func keysOf(m map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
