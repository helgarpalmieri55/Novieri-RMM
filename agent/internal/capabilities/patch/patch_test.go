package patch

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"reflect"
	"runtime"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// The capability itself: everything that happens between a verified job and a
// document the server will accept, with no OS involved.
//
// This is where the seam pays for itself. The parts that decide what gets
// installed, what gets refused, what the outcome says and what the document
// looks like are exercised in full on Linux, on every push, against a fake
// engine -- so the untested surface is only the three files that actually touch
// an OS, and only the parts of those that a Linux runner cannot reach.

// ---------------------------------------------------------------------------
// A fake engine
// ---------------------------------------------------------------------------

type fakeEngine struct {
	family     string
	available  []entry
	installed  []string
	availErr   error
	instErr    error
	installErr map[string]error

	// installs records EVERY entry Install was called with, which is how the
	// injection assertions below say "nothing ran" rather than "nothing bad
	// ran".
	installs []entry
	reboot   bool
	closed   int
}

func (f *fakeEngine) Family() string { return f.family }
func (f *fakeEngine) Available(ctx context.Context) ([]entry, error) {
	return f.available, f.availErr
}
func (f *fakeEngine) Installed(ctx context.Context) ([]string, error) {
	return f.installed, f.instErr
}
func (f *fakeEngine) Install(ctx context.Context, e entry) error {
	f.installs = append(f.installs, e)
	return f.installErr[e.key()]
}
func (f *fakeEngine) RebootRequired(ctx context.Context) bool { return f.reboot }
func (f *fakeEngine) Close()                                  { f.closed++ }

func quiet() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

var testEpoch = time.Date(2026, 8, 6, 12, 0, 0, 0, time.UTC)

func serviceWith(eng *fakeEngine) *Service {
	return New(Options{
		NewEngine: func(context.Context) (engine, error) { return eng, nil },
		Clock:     func() time.Time { return testEpoch },
		Logger:    quiet(),
	})
}

func applyJSON(t *testing.T, reboot string, refs ...map[string]string) json.RawMessage {
	t.Helper()
	if refs == nil {
		refs = []map[string]string{}
	}
	raw, err := json.Marshal(map[string]any{"patches": refs, "reboot": reboot})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func ref(source, identifier string) map[string]string {
	return map[string]string{"source": source, "identifier": identifier}
}

// ---------------------------------------------------------------------------
// The scan document
// ---------------------------------------------------------------------------

func TestTheScanResultIsTheDocumentAtTheTopLevelAndNotNestedUnderAKey(t *testing.T) {
	// app/modules/patch/handler.py does `PatchScanDocument(**result)` on the
	// job result. A wrapper key would make every member unexpected and the
	// document is extra="forbid", so the whole job fails.
	//
	// FALSIFYING OBSERVATION: a marshalled result whose keys are not the
	// document's own.
	eng := &fakeEngine{family: "linux", available: []entry{
		{Source: sourceAPT, Identifier: "coreutils@2", Title: "coreutils 2", Severity: "important"},
	}}
	doc, err := serviceWith(eng).scan(context.Background())
	if err != nil {
		t.Fatalf("scan: %v", err)
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]json.RawMessage
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	want := []string{"app_sources", "booted_at", "installed_identifiers", "os_family", "patches",
		"reboot_required", "scanned_at"}
	keys := make([]string, 0, len(got))
	for k := range got {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	if !reflect.DeepEqual(keys, want) {
		t.Fatalf("the scan result marshals to keys %v, want exactly %v", keys, want)
	}
	if string(got["scanned_at"]) != `"2026-08-06T12:00:00Z"` {
		t.Errorf("scanned_at = %s; app/jobs/canonical.py::rfc3339 is whole seconds and a literal Z",
			got["scanned_at"])
	}
}

func TestAnEmptyScanEmitsEmptyListsAndNotNull(t *testing.T) {
	// THE ASSERTION THAT MAKES schema_test's SLICE EXEMPTION SAFE. A nil Go
	// slice marshals to `null`, and pydantic rejects `null` for
	// `list[PatchEntry]` -- so a fully-patched device would fail its own clean
	// scan, and the failure would look like a patching problem rather than a
	// serialisation one.
	//
	// FALSIFYING OBSERVATION: `"patches":null` or
	// `"installed_identifiers":null` in the marshalled document.
	doc, err := serviceWith(&fakeEngine{family: "macos"}).scan(context.Background())
	if err != nil {
		t.Fatalf("scan: %v", err)
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	for _, forbidden := range []string{`"patches":null`, `"installed_identifiers":null`} {
		if strings.Contains(string(raw), forbidden) {
			t.Fatalf("a device with nothing outstanding emitted %s, which pydantic rejects: %s",
				forbidden, raw)
		}
	}
	if !strings.Contains(string(raw), `"patches":[]`) {
		t.Fatalf("patches is not an empty list: %s", raw)
	}
}

func TestASeverityTheServerDoesNotKnowBecomesUnknownRatherThanFailingTheDocument(t *testing.T) {
	// One unrecognised severity raises a ValidationError on the WHOLE document,
	// costing the device its entire scan. Clamping to `unknown` is a true
	// statement; failing is not a proportionate answer to one odd word.
	//
	// FALSIFYING OBSERVATION: a severity outside the server's Literal reaching
	// the document.
	eng := &fakeEngine{family: "windows", available: []entry{
		{Source: sourceWindowsUpdate, Identifier: "KB1", Title: "a", Severity: "SEVERE"},
		{Source: sourceWindowsUpdate, Identifier: "KB2", Title: "b", Severity: "Critical"},
		{Source: sourceWindowsUpdate, Identifier: "KB3", Title: "c", Severity: ""},
	}}
	doc, _ := serviceWith(eng).scan(context.Background())
	got := map[string]string{}
	for _, e := range doc.Patches {
		got[e.Identifier] = e.Severity
		if !severities[e.Severity] {
			t.Fatalf("%q reached the document with severity %q", e.Identifier, e.Severity)
		}
	}
	if got["KB1"] != "unknown" || got["KB2"] != "critical" || got["KB3"] != "unknown" {
		t.Fatalf("severities = %v; want KB1 unknown (clamped), KB2 critical (case-normalised, "+
			"NOT clamped), KB3 unknown", got)
	}
}

func TestAnEntryWithNoKeyIsDroppedAndDuplicatesCollapse(t *testing.T) {
	// `(org_id, os_family, source, identifier)` is the catalog's unique
	// constraint. An entry with no source or no identifier occupies a row
	// nothing can reference; a duplicate is a second upsert of the same row.
	eng := &fakeEngine{family: "linux", available: []entry{
		{Source: sourceAPT, Identifier: "a@1", Title: "a", Severity: "low"},
		{Source: "", Identifier: "b@1", Title: "b", Severity: "low"},
		{Source: sourceAPT, Identifier: "", Title: "c", Severity: "low"},
		{Source: sourceAPT, Identifier: "a@1", Title: "a again", Severity: "critical"},
	}}
	doc, _ := serviceWith(eng).scan(context.Background())
	if len(doc.Patches) != 1 {
		t.Fatalf("got %d patches, want 1: %+v", len(doc.Patches), doc.Patches)
	}
	if doc.Patches[0].Severity != "low" {
		t.Errorf("the FIRST occurrence should win, so the duplicate cannot rewrite severity; got %q",
			doc.Patches[0].Severity)
	}
}

func TestAMissingTitleFallsBackToTheIdentifierBecauseTitleIsRequired(t *testing.T) {
	eng := &fakeEngine{family: "linux", available: []entry{
		{Source: sourceAPT, Identifier: "a@1", Title: "   ", Severity: "low"},
	}}
	doc, _ := serviceWith(eng).scan(context.Background())
	if doc.Patches[0].Title != "a@1" {
		t.Fatalf("title = %q; `title` is required server-side and an empty one fails the whole "+
			"document, so the identifier is used as a terse but true title", doc.Patches[0].Title)
	}
}

func TestAnInstalledIdentifierWithNoColonIsDroppedBeforeItCostsAQuery(t *testing.T) {
	// ingest.py splits every unresolved installed identifier on the FIRST ":"
	// and skips one without a colon -- after issuing nothing, but after
	// carrying it across the wire. Sending it is pure noise.
	eng := &fakeEngine{family: "windows", installed: []string{
		"windows_update:KB1", "KB2-with-no-source", "windows_update:KB1", "  ",
	}}
	doc, _ := serviceWith(eng).scan(context.Background())
	want := []string{"windows_update:KB1"}
	if !reflect.DeepEqual(doc.InstalledIdentifiers, want) {
		t.Fatalf("installed_identifiers = %v, want %v", doc.InstalledIdentifiers, want)
	}
}

func TestAFailedInstalledListIsLoggedAndTheScanStillReportsWhatIsOutstanding(t *testing.T) {
	// The reconciliation aid is best-effort BY DESIGN. A scan that reported
	// nothing outstanding because the installed-list query failed would be a
	// worse answer than one that reported what is outstanding.
	//
	// FALSIFYING OBSERVATION: an error from scan(), or an empty `patches`.
	eng := &fakeEngine{
		family:    "linux",
		available: []entry{{Source: sourceAPT, Identifier: "a@1", Title: "a", Severity: "low"}},
		instErr:   errors.New("dpkg database is locked"),
	}
	doc, err := serviceWith(eng).scan(context.Background())
	if err != nil {
		t.Fatalf("a failed installed-list failed the whole scan: %v", err)
	}
	if len(doc.Patches) != 1 || len(doc.InstalledIdentifiers) != 0 {
		t.Fatalf("got %+v", doc)
	}
}

func TestAFailedAvailableFailsTheScanAndTheEngineIsStillClosed(t *testing.T) {
	eng := &fakeEngine{family: "linux", availErr: errors.New("apt exploded")}
	_, err := serviceWith(eng).scan(context.Background())
	if err == nil {
		t.Fatal("a failed Available produced no error")
	}
	var refusal *jobs.Refusal
	if errors.As(err, &refusal) {
		t.Errorf("a transient engine failure was REFUSED: %q", refusal.Reason)
	}
	if eng.closed != 1 {
		t.Errorf("the engine was closed %d times; it is created inside the job and released when "+
			"it returns, on every path", eng.closed)
	}
}

func TestAHostWithNoEngineRefusesRatherThanFails(t *testing.T) {
	// jobs/verified.go: `refused` is "this agent will not do this" and takes
	// the device out of rotation for modules that read it; `failed` is "try
	// again". A host with no supported package manager will not grow one.
	//
	// FALSIFYING OBSERVATION: a plain error where a Refusal belongs.
	s := New(Options{
		NewEngine: func(context.Context) (engine, error) { return nil, errNoEngine("no apt-get") },
		Logger:    quiet(),
	})
	for name, err := range map[string]error{
		"scan":  mustErr(s.scan(context.Background())),
		"apply": mustErr(s.apply(context.Background(), applyJSON(t, "never", ref("apt", "a@1")))),
	} {
		var refusal *jobs.Refusal
		if !errors.As(err, &refusal) {
			t.Errorf("%s on a host with no engine returned %T (%v), want a *jobs.Refusal",
				name, err, err)
			continue
		}
		if !strings.Contains(refusal.Reason, runtime.GOOS) {
			t.Errorf("%s's refusal does not name the GOOS, which is the only actionable part an "+
				"operator gets: %q", name, refusal.Reason)
		}
	}
}

// ---------------------------------------------------------------------------
// patch.apply: the security-relevant half
// ---------------------------------------------------------------------------

func TestNothingJobSuppliedEverReachesTheEngine(t *testing.T) {
	// THE INJECTION ASSERTION, and it is about what did NOT happen.
	//
	// engine.Install takes an `entry`, and an entry is only ever produced by
	// engine.Available -- by the OS describing itself. A job's
	// {source, identifier} is a lookup key and nothing else. So a job asking
	// for a shell metacharacter reaches NO Install call at all: not a
	// sanitised one, not an escaped one, none.
	//
	// FALSIFYING OBSERVATION: any Install call for an identifier the fake
	// engine did not offer.
	offered := entry{Source: sourceAPT, Identifier: "coreutils@9.4", Title: "coreutils", Severity: "low"}
	eng := &fakeEngine{family: "linux", available: []entry{offered}}
	s := serviceWith(eng)

	hostile := []map[string]string{
		ref("apt", "coreutils; rm -rf /"),
		ref("apt", "$(reboot)"),
		ref("apt", "`id`"),
		ref("apt", "coreutils@9.4 && shutdown -h now"),
		ref("apt", "../../../etc/shadow"),
		ref("apt\x00", "coreutils@9.4"),
		ref("apt", strings.Repeat("A", 5000)),
	}
	res, err := s.apply(context.Background(), applyJSON(t, "never", hostile...))
	if err != nil {
		t.Fatalf("apply: %v", err)
	}

	if len(eng.installs) != 0 {
		t.Fatalf("the engine was asked to install %d things from a job that named NONE of what the "+
			"OS offered: %+v", len(eng.installs), eng.installs)
	}
	if len(res.Results) != len(hostile) {
		t.Fatalf("got %d outcomes for %d requests", len(res.Results), len(hostile))
	}
	for _, o := range res.Results {
		if o.State != "failed" {
			t.Errorf("%q reported state %q", o.Identifier, o.State)
		}
		if o.Error == nil || *o.Error == "" {
			t.Errorf("%q failed with no reason; it lands in device_patch_status.last_error", o.Identifier)
		}
	}

	// And the control case, which is what stops the test above passing for the
	// wrong reason: a job naming what the OS DID offer must reach Install.
	eng.installs = nil
	if _, err := s.apply(context.Background(), applyJSON(t, "never", ref("apt", "coreutils@9.4"))); err != nil {
		t.Fatalf("apply: %v", err)
	}
	if len(eng.installs) != 1 || eng.installs[0].Identifier != "coreutils@9.4" {
		t.Fatalf("a legitimate request installed %+v; without this case the assertion above would "+
			"pass on an engine that installs nothing at all", eng.installs)
	}
}

func TestTheEntryHandedToInstallIsTheOSsAndNotTheJobs(t *testing.T) {
	// Subtler than the injection case and worth its own test: the job supplies
	// only the KEY, so every OTHER member of the entry -- title, severity, kb,
	// cve -- must come from the OS. An implementation that constructed an entry
	// from the job's map would pass the injection test above and still let a
	// signed job dictate what the installer saw.
	//
	// FALSIFYING OBSERVATION: an installed entry whose title/severity are not
	// the OS's.
	offered := entry{
		Source: sourceWindowsUpdate, Identifier: "KB5034123",
		Title: "2026-08 Cumulative Update", Severity: "critical",
	}
	eng := &fakeEngine{family: "windows", available: []entry{offered}}
	raw, err := json.Marshal(map[string]any{
		"reboot": "if_required",
		"patches": []map[string]string{{
			"source": sourceWindowsUpdate, "identifier": "KB5034123",
			// Members the server's `dict[str, str]` permits and this agent
			// ignores. They must not reach the engine.
			"title": "Totally Safe Update", "severity": "low", "kb": "KB0000000",
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := serviceWith(eng).apply(context.Background(), raw); err != nil {
		t.Fatalf("apply: %v", err)
	}
	if len(eng.installs) != 1 {
		t.Fatalf("installs = %+v", eng.installs)
	}
	if got := eng.installs[0]; got.Title != offered.Title || got.Severity != offered.Severity {
		t.Fatalf("the engine was handed %+v; every member but the key must be the OS's own", got)
	}
}

func TestAPatchAlreadyInstalledIsReportedInstalledWithoutRunningAnything(t *testing.T) {
	// The window sweep runs hourly and a result can be lost on the way back, so
	// a re-dispatched apply must be idempotent rather than reporting a failure
	// for work that is already done.
	//
	// FALSIFYING OBSERVATION: an Install call for something the OS says is
	// already present.
	eng := &fakeEngine{
		family:    "windows",
		installed: []string{"windows_update:KB5034123"},
	}
	res, err := serviceWith(eng).apply(context.Background(),
		applyJSON(t, "if_required", ref(sourceWindowsUpdate, "KB5034123")))
	if err != nil {
		t.Fatalf("apply: %v", err)
	}
	if len(eng.installs) != 0 {
		t.Fatalf("an already-installed patch was installed again: %+v", eng.installs)
	}
	if len(res.Results) != 1 || res.Results[0].State != "installed" || res.Results[0].Error != nil {
		t.Fatalf("got %+v", res.Results)
	}
}

func TestOneFailingPatchDoesNotCostTheOthersTheirResult(t *testing.T) {
	// dispatch_apply batches ALL of a device's eligible patches into ONE job,
	// so a single failure must not abandon the batch. ingest_apply_result
	// reconciles each outcome independently.
	//
	// FALSIFYING OBSERVATION: fewer outcomes than requests, or an error from
	// apply() when one patch failed.
	eng := &fakeEngine{
		family: "windows",
		available: []entry{
			{Source: sourceWindowsUpdate, Identifier: "KB1", Title: "one", Severity: "critical"},
			{Source: sourceWindowsUpdate, Identifier: "KB2", Title: "two", Severity: "low"},
			{Source: sourceWindowsUpdate, Identifier: "KB3", Title: "three", Severity: "low"},
		},
		installErr: map[string]error{
			"windows_update:KB2": errors.New("0x80070005: access is denied"),
		},
	}
	res, err := serviceWith(eng).apply(context.Background(), applyJSON(t, "never",
		ref(sourceWindowsUpdate, "KB1"),
		ref(sourceWindowsUpdate, "KB2"),
		ref(sourceWindowsUpdate, "KB3"),
	))
	if err != nil {
		t.Fatalf("one failing patch failed the whole job: %v", err)
	}
	if len(res.Results) != 3 {
		t.Fatalf("got %d outcomes for 3 requests", len(res.Results))
	}
	states := map[string]string{}
	for _, o := range res.Results {
		states[o.Identifier] = o.State
	}
	if states["KB1"] != "installed" || states["KB2"] != "failed" || states["KB3"] != "installed" {
		t.Fatalf("states = %v; KB3 came AFTER the failure and must still have been attempted", states)
	}
	if len(eng.installs) != 3 {
		t.Fatalf("the engine saw %d installs; the batch stopped early", len(eng.installs))
	}
	for _, o := range res.Results {
		if o.Identifier == "KB2" {
			if o.Error == nil || !strings.Contains(*o.Error, "0x80070005") {
				t.Errorf("the failure did not carry the OS's own message, which is the whole "+
					"content of device_patch_status.last_error: %v", o.Error)
			}
		}
	}
}

func TestAnErrorMessageIsBoundedAndStrippedOfControlCharacters(t *testing.T) {
	// `error` lands in `last_error` and from there on an operator's screen. It
	// is not this agent's prose: it is whatever the OS said. A bare CR rewrites
	// a terminal line and an ANSI escape does worse.
	//
	// FALSIFYING OBSERVATION: an escape sequence or an unbounded string in the
	// outcome.
	nasty := "failed\r\x1b[2J\x1b[H" + strings.Repeat("x", maxErrorText*2)
	eng := &fakeEngine{
		family:     "linux",
		available:  []entry{{Source: sourceAPT, Identifier: "a@1", Title: "a", Severity: "low"}},
		installErr: map[string]error{"apt:a@1": errors.New(nasty)},
	}
	res, _ := serviceWith(eng).apply(context.Background(), applyJSON(t, "never", ref("apt", "a@1")))
	got := *res.Results[0].Error
	if strings.ContainsAny(got, "\r\x1b\n\x00") {
		t.Errorf("a control character survived into last_error: %q", got)
	}
	if len([]rune(got)) > maxErrorText {
		t.Errorf("last_error is %d runes, above the %d bound", len([]rune(got)), maxErrorText)
	}
	if !strings.HasPrefix(got, "failed") {
		t.Errorf("the OS's own words were lost: %q", got)
	}
}

func TestUnderAnOlderControlPlaneTheAgentReportsAndNeverRestarts(t *testing.T) {
	// A control plane that does not announce result_version 2 cannot read
	// reboot_scheduled_for, so a restart would look like a lost device. All
	// three policies install, report reboot_required, and restart nothing.
	for _, policy := range []string{"never", "if_required", "forced"} {
		eng := &fakeEngine{
			family:    "windows",
			available: []entry{{Source: sourceWindowsUpdate, Identifier: "KB1", Title: "x", Severity: "critical"}},
			reboot:    true,
		}
		r := &fakeRestarter{}
		svc := serviceWith(eng)
		svc.opts.NewRestarter = func() restarter { return r }
		res, err := svc.apply(context.Background(),
			applyJSON(t, policy, ref(sourceWindowsUpdate, "KB1")))
		if err != nil {
			t.Fatalf("apply under %q: %v", policy, err)
		}
		if !res.RebootRequired {
			t.Errorf("under reboot=%q the OS said a restart is needed and the result says false", policy)
		}
		if len(eng.installs) != 1 {
			t.Errorf("under reboot=%q the install did not run", policy)
		}
		if len(r.scheduled) != 0 {
			t.Errorf("under reboot=%q and no result_version the agent scheduled a restart", policy)
		}
	}
}

func TestApplyRefusesAParamsDocumentItWillNotActOn(t *testing.T) {
	// REFUSED and not failed for this whole class: a params document this agent
	// will not act on is not a transient condition, and no number of retries
	// makes it one. The reason travels into the server's hash-chained audit log
	// through JobEngine.refuse, so it must be the AGENT'S OWN SENTENCE.
	//
	// FALSIFYING OBSERVATION: a plain error, an install running, or a
	// job-supplied string echoed into the refusal.
	marker := "PLEASE-DO-NOT-ECHO-ME"
	for name, raw := range map[string]json.RawMessage{
		"not a params document at all": json.RawMessage(`"just a string"`),
		"no reboot policy":             applyJSON(t, "", ref("apt", "a@1")),
		"an unknown reboot policy":     applyJSON(t, "reboot_immediately", ref("apt", "a@1")),
		"an empty batch":               applyJSON(t, "never"),
		"a hostile source":             applyJSON(t, marker, ref("apt", "a@1")),
	} {
		eng := &fakeEngine{family: "linux", available: []entry{
			{Source: sourceAPT, Identifier: "a@1", Title: "a", Severity: "low"},
		}}
		_, err := serviceWith(eng).apply(context.Background(), raw)
		var refusal *jobs.Refusal
		if !errors.As(err, &refusal) {
			t.Errorf("%s returned %T (%v), want a *jobs.Refusal", name, err, err)
			continue
		}
		if strings.Contains(refusal.Reason, marker) {
			t.Errorf("%s echoed a job-supplied string into the refusal, which goes verbatim into "+
				"the audit log: %q", name, refusal.Reason)
		}
		if !strings.Contains(refusal.Reason, "nothing was installed") {
			t.Errorf("%s's refusal does not say that nothing was installed: %q", name, refusal.Reason)
		}
		if len(eng.installs) != 0 {
			t.Errorf("%s: %d installs ran before the refusal", name, len(eng.installs))
		}
	}
}

func TestABatchLargerThanAnythingDispatchAppyProducesIsRefused(t *testing.T) {
	refs := make([]map[string]string, maxApplyBatch+1)
	for i := range refs {
		refs[i] = ref("apt", fmt.Sprintf("pkg%d@1", i))
	}
	eng := &fakeEngine{family: "linux"}
	_, err := serviceWith(eng).apply(context.Background(), applyJSON(t, "never", refs...))
	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Fatalf("an oversized batch returned %T (%v)", err, err)
	}
	if len(eng.installs) != 0 {
		t.Fatal("the oversized batch was partly executed")
	}
}

func TestAnUnknownMemberOfAPatchReferenceIsIgnoredAndNotRejected(t *testing.T) {
	// The inbound half of docs/kernel/wire-protocol/README.md's rule: a
	// strictly additive server change must not take a deployed fleet offline.
	// `patches` is `list[dict[str, str]]` server-side, so the server can add a
	// member tomorrow and this agent must keep working.
	//
	// FALSIFYING OBSERVATION: a refusal, or a skipped install, because of an
	// unrecognised member.
	eng := &fakeEngine{family: "linux", available: []entry{
		{Source: sourceAPT, Identifier: "a@1", Title: "a", Severity: "low"},
	}}
	raw, err := json.Marshal(map[string]any{
		"reboot":         "never",
		"deferral_hours": 48,
		"patches": []map[string]string{
			{"source": "apt", "identifier": "a@1", "ring": "test", "approved_by": "someone"},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	res, err := serviceWith(eng).apply(context.Background(), raw)
	if err != nil {
		t.Fatalf("an additive server change broke this agent: %v", err)
	}
	if len(eng.installs) != 1 || res.Results[0].State != "installed" {
		t.Fatalf("installs=%+v results=%+v", eng.installs, res.Results)
	}
}

func TestDeferralHoursDefaultsToTwentyFourWhenTheServerOmitsIt(t *testing.T) {
	// The value is not acted on today (see the reboot test above), but the
	// default is still a contract: the day it IS acted on, an absent member
	// must not read as "defer for zero hours".
	var p applyParams
	if err := json.Unmarshal(applyJSON(t, "never", ref("apt", "a@1")), &p); err != nil {
		t.Fatal(err)
	}
	if p.DeferralHours != nil {
		t.Fatal("the fixture already carries deferral_hours; this test would prove nothing")
	}
	if got := p.deferralHours(); got != defaultDeferralHours {
		t.Fatalf("deferralHours() = %d, want %d", got, defaultDeferralHours)
	}
	twelve := 12
	p.DeferralHours = &twelve
	if got := p.deferralHours(); got != 12 {
		t.Fatalf("an explicit 12 read as %d", got)
	}
}

func TestTheEngineIsCreatedInsideTheJobAndClosedOnEveryPath(t *testing.T) {
	// Dormancy, at the level a unit test can see it: budget_test.go measures
	// the footprint, and this asserts the lifecycle that produces it.
	//
	// FALSIFYING OBSERVATION: an engine created before a job, or left open
	// after one.
	created := 0
	eng := &fakeEngine{family: "linux", available: []entry{
		{Source: sourceAPT, Identifier: "a@1", Title: "a", Severity: "low"},
	}}
	s := New(Options{
		NewEngine: func(context.Context) (engine, error) { created++; return eng, nil },
		Clock:     func() time.Time { return testEpoch },
		Logger:    quiet(),
	})
	if created != 0 {
		t.Fatal("constructing the Service created an engine")
	}
	if _, err := s.scan(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := s.apply(context.Background(), applyJSON(t, "never", ref("apt", "a@1"))); err != nil {
		t.Fatal(err)
	}
	if created != 2 || eng.closed != 2 {
		t.Fatalf("created %d engines and closed %d across one scan and one apply", created, eng.closed)
	}
}

func TestTheCapabilitiesTakeAVerifiedJobAndNothingElse(t *testing.T) {
	// A compile-time fact written as a test so its failure names the reason.
	// jobs.VerifiedJob is an interface with an UNEXPORTED method and no exported
	// implementation, so neither of these is reachable except through the
	// signed-job runner -- which checks the Ed25519 signature over the canonical
	// bytes against the key pinned at enrollment, the target device, freshness,
	// the nonce and the type, in that order, before it constructs one.
	//
	// `patch.apply` runs as SYSTEM/root. This is the structural half of "the
	// signed envelope is its only authorization": there is no local policy file,
	// no cached approval and no second entry point, because there is no way to
	// build the argument.
	var _ jobs.Handler = Process.Scan
	var _ jobs.Handler = Process.Apply
}

func TestTheCountersCountCompletedJobsAndNothingElse(t *testing.T) {
	// The counters exist so a dormancy assertion OUTSIDE this package can
	// observe that the capability has NOT run. One that incremented on a
	// refusal or a failure would make that statement unobservable.
	//
	// FALSIFYING OBSERVATION: a counter above zero after a failed scan or a
	// refused apply.
	failing := serviceWith(&fakeEngine{family: "linux", availErr: errors.New("nope")})
	if failing.Scans() != 0 || failing.Applies() != 0 {
		t.Fatal("a fresh Service has already run something")
	}
	_, _ = failing.scan(context.Background())
	if failing.Scans() != 0 {
		t.Fatalf("a FAILED scan incremented the counter to %d", failing.Scans())
	}

	refusing := serviceWith(&fakeEngine{family: "linux"})
	_, _ = refusing.apply(context.Background(), applyJSON(t, "not_a_policy", ref("apt", "a@1")))
	if refusing.Applies() != 0 {
		t.Fatalf("a REFUSED apply incremented the counter to %d", refusing.Applies())
	}

	ok := serviceWith(&fakeEngine{family: "linux", available: []entry{
		{Source: sourceAPT, Identifier: "a@1", Title: "a", Severity: "low"},
	}})
	if _, err := ok.scan(context.Background()); err != nil {
		t.Fatal(err)
	}
	if ok.Scans() != 1 || ok.Applies() != 0 {
		t.Fatalf("after one scan: scans=%d applies=%d", ok.Scans(), ok.Applies())
	}
	if _, err := ok.apply(context.Background(), applyJSON(t, "never", ref("apt", "a@1"))); err != nil {
		t.Fatal(err)
	}
	if ok.Scans() != 1 || ok.Applies() != 1 {
		t.Fatalf("after one scan and one apply: scans=%d applies=%d", ok.Scans(), ok.Applies())
	}
}

func TestClampTruncatesRunesAndNotBytes(t *testing.T) {
	// Truncating UTF-8 mid-rune produces invalid UTF-8, encoding/json replaces
	// it with U+FFFD, and a Japanese update title arrives as a row of
	// replacement characters in an operator's console.
	//
	// FALSIFYING OBSERVATION: invalid UTF-8 out of clamp.
	title := strings.Repeat("更新プログラム", 100) // 700 runes, 2100 bytes
	got := clamp(title, maxTitle)
	if n := len([]rune(got)); n != maxTitle {
		t.Fatalf("clamped to %d runes, want %d", n, maxTitle)
	}
	if strings.Contains(got, "�") {
		t.Fatal("clamp cut mid-rune and produced a replacement character")
	}
	raw, err := json.Marshal(got)
	if err != nil {
		t.Fatalf("the clamped title does not marshal: %v", err)
	}
	if strings.Contains(string(raw), `�`) {
		t.Fatal("the marshalled title carries a replacement character")
	}
}

// ---------------------------------------------------------------------------
// Apparatus
// ---------------------------------------------------------------------------

// mustErr keeps a two-value call readable in a table of error cases.
func mustErr(_ any, err error) error { return err }

func TestBoundedBufferWriteReportsTheFullLengthWithNilError(t *testing.T) {
	// io.Writer requires n == len(p) whenever err == nil. A buffer that
	// truncates p and then returns len(p) -- the TRUNCATED length -- breaks
	// that contract for any write that starts with room left but whose length
	// exceeds it.
	//
	// FALSIFYING OBSERVATION: n != len(p) for such a write, or a non-nil
	// error.
	b := &boundedBuffer{limit: 10}
	p := bytes.Repeat([]byte("A"), 100)
	n, err := b.Write(p)
	if err != nil {
		t.Fatalf("Write returned an error: %v, want nil", err)
	}
	if n != len(p) {
		t.Fatalf("Write(p) returned n=%d, want n=len(p)=%d -- io.Writer requires n==len(p) when err==nil",
			n, len(p))
	}
	if got := b.buf.Len(); got != 10 {
		t.Fatalf("buffered %d bytes, want the limit of 10 -- the cap must still hold the HEAD of the "+
			"input and drop the rest", got)
	}
}

func TestBoundedBufferSurvivesIoCopyPastTheLimit(t *testing.T) {
	// The real failure mode: exec.Cmd copies a non-*os.File Stdout/Stderr
	// through an internal io.Copy goroutine in 32 KiB chunks. If Write ever
	// reports n < len(p) with a nil error, io.Copy turns that into a "short
	// write" error and cmd.Run() reports the process as failed -- turning a
	// verbose but successful apt/dnf run into a failed patch job.
	//
	// FALSIFYING OBSERVATION: a non-nil error from io.Copy, or content beyond
	// the limit retained.
	b := &boundedBuffer{limit: 10}
	src := bytes.NewReader(bytes.Repeat([]byte("B"), 100))
	copied, err := io.Copy(b, src)
	if err != nil {
		t.Fatalf("io.Copy returned an error: %v, want nil -- this is the short-write regression", err)
	}
	if copied != 100 {
		t.Fatalf("io.Copy reported %d bytes copied, want 100", copied)
	}
	if got := b.buf.Len(); got != 10 {
		t.Fatalf("buffered %d bytes, want the limit of 10", got)
	}
}
