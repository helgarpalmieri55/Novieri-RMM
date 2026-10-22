// Package inventory implements the `inventory.collect` capability.
//
// # The contract
//
// The result of this job IS the inventory document, at the top level of
// `job_result.result`. app/modules/inventory/handler.py:27 does
// `InventoryDocument(**result)` on that dict, so nothing may be nested under a
// wrapper key. The document's shape is
// docs/kernel/wire-protocol/InventoryDocument.schema.json, exported from
// app/modules/inventory/schema.py; inventory_test.go validates the emitted
// document against that file rather than against a transcription of it.
//
// # Dormancy (CLAUDE.md's idle budget)
//
// Nothing in this package runs, allocates, or opens anything until Collect is
// called. There is no cache, no background refresh and no package-level state
// beyond the invocation counter Runs() reports -- which exists precisely so the
// registry's dormancy test can observe that this collector has NOT run.
// Registration is a map insert; the /proc reads, the interface enumeration and
// the per-OS shell-outs all happen inside the job and are released when it
// returns.
//
// # Invariant #1
//
// Collect takes a jobs.VerifiedJob and reads NOTHING from it.
// InventoryCollectParams is `extra="forbid"` and empty (app/jobs/registry.py),
// so there is nothing a job could supply -- and inventory_test.go walks this
// package's source to keep that true, because "no job-supplied string reaches
// an exec argument" is a property of the code rather than of any one test case.
package inventory

import (
	"context"
	"errors"
	"fmt"
	"runtime"
	"sync/atomic"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// Collection is bounded so a pathological host cannot hold a job open until its
// expires_at. The job's own deadline still applies on top of this; whichever is
// sooner wins.
const collectTimeout = 30 * time.Second

// MaxCollectDuration is the longest inventory.collect's shell-outs can hold
// the job: collectTimeout, then at most waitDelay for a killed tool to be
// reaped. The in-process reads that follow are not bounded by it.
const MaxCollectDuration = collectTimeout + waitDelay

// Schema bounds from InventoryDocument.schema.json. Values are CLAMPED to them
// rather than rejected: a 400-character hostname is a reason to send 255
// characters, not a reason for the device to have no inventory at all.
const (
	maxHostname   = 255
	maxIdentifier = 200
	maxName       = 300
	maxComponents = 512
	maxSoftware   = 4096
)

// componentTypes is ComponentDoc.type's pattern, as a set.
//
// The pattern is CLOSED and the pydantic error it produces is on the whole
// document, so one unrecognised component type would cost the device its CPU,
// its memory and its NICs as well. Anything not in here is dropped by
// collect().
var componentTypes = map[string]bool{
	"cpu": true, "memory": true, "disk": true, "nic": true, "gpu": true,
	"display": true, "battery": true, "bios": true, "motherboard": true,
}

// osFamilies maps runtime.GOOS to InventoryDocument.os_family's vocabulary.
//
// `darwin` -> `macos` is the one that bites: the server's pattern is
// ^(windows|linux|macos)$ and an agent that shipped GOOS straight through would
// have every Mac in the estate fail ingestion.
var osFamilies = map[string]string{
	"windows": "windows",
	"linux":   "linux",
	"darwin":  "macos",
}

// runs counts completed invocations. See the package comment: the registry's
// dormancy assertion is written against this.
var runs atomic.Int64

// Runs reports how many times this capability has been invoked in this process.
//
// It is exported for one reason: the dormancy assertion in
// capabilities/registry_test.go lives outside this package, and a counter
// nobody can read proves nothing. It is a single atomic load and costs the idle
// path nothing.
func Runs() int64 { return runs.Load() }

// Document is InventoryDocument.schema.json.
//
// Nullable members are pointers with `omitempty` so an unknown fact is ABSENT
// rather than an empty string: `serial: ""` would overwrite a serial the
// control plane already holds, and the schema's `anyOf [string, null]` accepts
// absence.
type Document struct {
	Hostname     string         `json:"hostname"`
	FQDN         *string        `json:"fqdn,omitempty"`
	OSFamily     string         `json:"os_family"`
	OSVersion    string         `json:"os_version"`
	OSBuild      *string        `json:"os_build,omitempty"`
	Architecture string         `json:"architecture"`
	Serial       *string        `json:"serial,omitempty"`
	Hardware     Hardware       `json:"hardware"`
	Software     []Software     `json:"software"`
	Facts        map[string]any `json:"facts"`
}

// Hardware is HardwareDoc.
type Hardware struct {
	CPU        string      `json:"cpu"`
	RAMBytes   *int64      `json:"ram_bytes,omitempty"`
	Components []Component `json:"components"`
}

// Component is ComponentDoc. `attributes` is `additionalProperties: true`, so
// it is the one place in this document where the agent and the control plane
// have no schema between them -- see nic.go for the NIC block and the
// compatibility rule that governs it.
type Component struct {
	Type       string         `json:"type"`
	Identifier string         `json:"identifier"`
	Attributes map[string]any `json:"attributes"`
}

// Software is SoftwareDoc.
type Software struct {
	Name        string  `json:"name"`
	Version     string  `json:"version"`
	Publisher   string  `json:"publisher"`
	InstallDate *string `json:"install_date,omitempty"`
}

// hostFacts is everything collect() needs from the host: one gatherer per GOOS
// behind a build tag (facts_linux.go, facts_windows.go, facts_darwin.go), and a
// literal in the tests.
//
// Splitting the gathering from the document assembly is what makes the schema
// conformance testable on a machine that is not the target platform -- which
// matters, because this container is linux and three of the six cross-compile
// targets are not.
type hostFacts struct {
	Hostname     string
	FQDN         string
	OSVersion    string
	OSBuild      string
	Architecture string
	Serial       string
	CPUModel     string
	RAMBytes     int64
	Components   []Component
	Software     []Software
	// Facts becomes InventoryDocument.facts; see software.go for the keys.
	// A nil or "" value means unknown and is omitted.
	Facts map[string]any
}

// ErrNoFacts is returned when the host could not supply a fact the server
// requires. Failing here is deliberate: a document that fails
// `InventoryDocument(**result)` turns the job `failed` with a pydantic error in
// the job record, which is harder to read than a local failure naming the fact.
var ErrNoFacts = errors.New("inventory: the host did not supply a required fact")

// Collect is the `inventory.collect` capability.
//
// The job argument is deliberately unused: see the package comment. Its
// presence in the signature is invariant #1 -- this function is unreachable
// except through the signed-job runner, because jobs.VerifiedJob has no
// constructor outside internal/jobs.
func Collect(ctx context.Context, _ jobs.VerifiedJob) (any, error) {
	ctx, cancel := context.WithTimeout(ctx, collectTimeout)
	defer cancel()

	doc, err := collect(gather(ctx), runtime.GOOS)
	if err != nil {
		return nil, err
	}
	runs.Add(1)
	return doc, nil
}

// collect assembles the document and refuses to emit one the server would
// reject. Pure: everything host-shaped arrives in `facts`.
func collect(facts hostFacts, goosName string) (*Document, error) {
	family := osFamilyFor(goosName)
	if family == "" {
		return nil, fmt.Errorf("%w: no os_family for GOOS %q; the server's pattern is "+
			"^(windows|linux|macos)$", ErrNoFacts, goosName)
	}
	hostname := clamp(facts.Hostname, maxHostname)
	if hostname == "" {
		return nil, fmt.Errorf("%w: hostname", ErrNoFacts)
	}

	doc := &Document{
		Hostname:     hostname,
		FQDN:         optional(facts.FQDN, maxHostname),
		OSFamily:     family,
		OSVersion:    facts.OSVersion,
		OSBuild:      optional(facts.OSBuild, maxName),
		Architecture: facts.Architecture,
		Serial:       optional(facts.Serial, maxName),
		Hardware: Hardware{
			CPU:        facts.CPUModel,
			Components: components(facts.Components),
		},
		// `facts` is `additionalProperties: true`. Always an object, never
		// omitted, so the member has the type the schema declares.
		Facts: documentFacts(facts.Facts),
	}
	var truncated bool
	doc.Software, truncated = normalizeSoftware(facts.Software)
	if truncated {
		doc.Facts[factSoftwareTruncated] = true
	}
	if facts.RAMBytes > 0 {
		ram := facts.RAMBytes
		doc.Hardware.RAMBytes = &ram
	}
	return doc, nil
}

// components clamps and filters what the gatherer found.
func components(in []Component) []Component {
	out := make([]Component, 0, len(in))
	for _, c := range in {
		if len(out) == maxComponents {
			break
		}
		if !componentTypes[c.Type] {
			// Dropped, not passed through: the server's pattern is closed and
			// the resulting error is on the DOCUMENT, so one exotic component
			// would cost this device everything else it reported.
			continue
		}
		c.Identifier = clamp(c.Identifier, maxIdentifier)
		if c.Identifier == "" {
			continue // minLength 1
		}
		if c.Attributes == nil {
			c.Attributes = map[string]any{}
		}
		out = append(out, c)
	}
	return out
}

// osFamilyFor maps a GOOS to the server's vocabulary, or "" when there is no
// mapping. "" is a refusal, not a default: see collect().
func osFamilyFor(goosName string) string { return osFamilies[goosName] }

// optional returns nil for an empty fact so the member is ABSENT rather than an
// empty string. Overwriting a stored serial with "" is a worse answer than
// saying nothing.
func optional(value string, max int) *string {
	value = clamp(value, max)
	if value == "" {
		return nil
	}
	return &value
}

// clamp truncates to a byte length, on a rune boundary. Truncating mid-rune
// would produce invalid UTF-8, which json.Marshal renders as U+FFFD -- a
// silently corrupted hostname rather than a short one.
func clamp(s string, max int) string {
	if len(s) <= max {
		return s
	}
	for max > 0 && !isRuneStart(s[max]) {
		max--
	}
	return s[:max]
}

func isRuneStart(b byte) bool { return b&0xC0 != 0x80 }
