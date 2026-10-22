// Package patch implements the `patch.scan` and `patch.apply` capabilities
// (docs/modules/patch/spec.md §10, docs/modules/patch/agent-capabilities.md).
//
// # What the control plane already does, and what this package adds
//
// The whole server slice shipped without an endpoint: approval rings,
// maintenance windows, per-OS policy resolution, the `patch_runs` ledger and
// both result handlers exist in app/modules/patch/, and every job they
// dispatched was refused `unknown_type` because no agent code was registered.
// This package is that endpoint and nothing else. It adds no policy, no local
// schedule and no second way in.
//
// # `patch.apply` runs as SYSTEM/root, and the signed envelope is its ONLY
// authorization
//
// There is no local policy file, no cached approval, no "already approved last
// week" fast path and no way to reach the install path except through
// jobs.VerifiedJob -- which the runner constructs only after checking the
// Ed25519 signature over the canonical bytes against the key pinned at
// enrollment, the target device, freshness, the nonce, and the type, in that
// order. Invariant #1 is structural here rather than promised: Apply's argument
// type cannot be manufactured outside internal/jobs (see jobs/verified.go).
//
// # Antivirus and EDR are a design constraint on this package
//
// This runs as SYSTEM on Windows 11 with Defender and probably a third-party
// EDR. A SYSTEM process spawning `powershell.exe` is one of the loudest
// behavioural signals an endpoint can emit and trips ASR rules outright, so:
//
//   - Windows uses the Windows Update Agent COM API in-process (wua_windows.go,
//     com_windows.go). No child process at all: no PowerShell, no UsoClient, no
//     wusa. The loudest thing this capability does on Windows is call
//     CoCreateInstance, which is what Windows Update itself does.
//   - macOS shells out to `/usr/sbin/softwareupdate` and Linux to
//     `/usr/bin/apt-get`, both with a fixed argv and never through a shell.
//     Those are the narrowest binaries that can do the work.
//   - NOTHING JOB-SUPPLIED EVER REACHES AN ARGUMENT OR A COM CALL. That is a
//     structural property of the engine seam, not a review rule: engine.Install
//     takes an `entry` and an entry is only ever produced by engine.Available,
//     i.e. by the OS describing itself. A job's `{source, identifier}` pair is
//     used ONLY as a lookup key into that OS-produced set -- a string equality
//     test, never a value that travels onward. patch_test.go asserts that a job
//     asking for `coreutils; rm -rf /` reaches no Install call at all.
//
// # Reboot
//
// `reboot` and `deferral_hours` are honoured when the control plane announces
// `result_version` 2, because only then can it read `reboot_scheduled_for`
// and tell a restarting device from a lost one. Under an older control plane
// this agent reports `reboot_required` and never restarts. See reboot.go.
//
// # Dormancy (CLAUDE.md's idle budget)
//
// Registration is a map insert. Nothing here opens a file, dials anything,
// starts a goroutine or takes a timer-wheel entry until a verified job arrives:
// `Process` is a zero-value Service that resolves its engine at JOB TIME, the
// engine is created inside the job and released when it returns, and there is
// no cache of a previous scan. budget_test.go measures that rather than
// describing it -- allocations, goroutines, file descriptors and retained heap
// across a hundredfold spread of jobs.
//
// # No local schedule
//
// The scan cadence is server-side (`scan_interval_hours`, spec §12's beat) for
// the same reason net.sweep's and site.wake's are: a locally scheduled patch
// apply is a standing authorization, signed once and replayed for weeks after
// the approval was revoked.
package patch

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"sync/atomic"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// Job types. These match app/modules/patch/handler.py's registrations exactly;
// a typo here is a capability the control plane dispatches to and the agent
// refuses `unknown_type` forever, which is the bug this package exists to fix.
const (
	JobTypeScan  = "patch.scan"
	JobTypeApply = "patch.apply"
)

// rfc3339 matches app/jobs/canonical.py::rfc3339 -- whole seconds, literal Z.
const rfc3339 = "2006-01-02T15:04:05Z"

// Bounds. Every one of these CLAMPS rather than rejects, for the reason
// inventory's do: a 400-character update title is a reason to send 300
// characters, not a reason for the device to have no patch state at all. The
// exception is maxApplyBatch, which is a refusal -- see applyRun.
const (
	// maxPatches bounds one scan document. A WSUS-managed machine that has not
	// been patched since install can offer hundreds; four thousand is far past
	// anything real and still bounded.
	maxPatches = 4096

	// maxInstalled bounds `installed_identifiers`. This is small ON PURPOSE and
	// the reason is server-side: ingest.py resolves every installed identifier
	// that is not also in `patches` with its own SELECT, so an agent that
	// reported its entire package database would cost the control plane one
	// query per row per device per scan. See agent-capabilities.md §3.
	maxInstalled = 512

	// maxApplyBatch bounds one apply job. dispatch_apply batches a device's
	// eligible patches into one job, so this is the ceiling on how much work a
	// single signed envelope can authorize.
	maxApplyBatch = 512

	maxIdentifier = 200
	maxTitle      = 300
	maxCVE        = 32
	// maxErrorText bounds what an OS error contributes to `last_error`. The
	// text is the OS's, not this agent's, so it is bounded and stripped of
	// control characters before it goes anywhere near an operator's console.
	maxErrorText = 500

	// scanTimeout and applyTimeout bound the capability independently of the
	// job's own expires_at; whichever is sooner wins. A patch apply is genuinely
	// slow -- a cumulative update is minutes -- so applyTimeout is generous, and
	// it exists to stop a hung installer holding the capability open forever
	// rather than to be a realistic budget.
	scanTimeout  = 10 * time.Minute
	applyTimeout = 2 * time.Hour
)

// severities is PatchEntry.severity's closed vocabulary.
//
// The server's model is a Literal and `extra="forbid"`, and a ValidationError
// is raised on the WHOLE document -- so one unrecognised severity costs the
// device its entire scan. Anything not in here becomes "unknown", which is a
// true statement, rather than failing the document.
var severities = map[string]bool{
	"critical": true, "important": true, "moderate": true, "low": true, "unknown": true,
}

// rebootPolicies is PatchApplyParams.reboot's closed vocabulary.
var rebootPolicies = map[string]bool{"never": true, "if_required": true, "forced": true}

// ---------------------------------------------------------------------------
// The wire contract
// ---------------------------------------------------------------------------

// scanParams implements PatchScanParams.schema.json. schema_test.go pins it to
// result_version alone: a new member would be one this package ignores.
type scanParams struct {
	// ResultVersion is absent (1) from a control plane that predates the
	// reboot and app members.
	ResultVersion *int `json:"result_version"`
}

// parseScanParams returns the document version this control plane reads.
func parseScanParams(raw json.RawMessage) (int, error) {
	if len(raw) == 0 {
		return 1, nil
	}
	var p scanParams
	if err := json.Unmarshal(raw, &p); err != nil {
		return 0, jobs.Refuse("patch.scan: params are not a PatchScanParams; nothing was scanned")
	}
	if p.ResultVersion == nil {
		return 1, nil
	}
	return *p.ResultVersion, nil
}

// entry implements PatchScanDocument's $defs/PatchEntry.
//
// The optional members are POINTERS (or a nil-able slice) because the server
// declares them `str | None = None` and this package must be able to say
// "absent". A plain string would make "no KB article" indistinguishable from
// "the KB article is the empty string", and the latter is a lie the catalog
// would store.
type entry struct {
	Source       string   `json:"source"`
	Identifier   string   `json:"identifier"`
	Title        string   `json:"title"`
	Severity     string   `json:"severity"`
	KB           *string  `json:"kb"`
	CVE          []string `json:"cve"`
	SupersededBy *string  `json:"superseded_by"`
}

// key is the `<source>:<identifier>` spelling ingest.py uses for
// `installed_identifiers` and the one this package matches apply requests on.
func (e entry) key() string { return e.Source + ":" + e.Identifier }

// scanDocument implements PatchScanDocument.schema.json.
//
// THE RESULT IS THIS DOCUMENT AT THE TOP LEVEL. handler.py does
// `PatchScanDocument(**result)`, so nothing may be nested under a wrapper key.
type scanDocument struct {
	OSFamily string `json:"os_family"`
	// RebootRequired is nil when this OS cannot say outside an install.
	RebootRequired *bool `json:"reboot_required"`
	// BootedAt lets the control plane confirm that a restart happened.
	BootedAt *string `json:"booted_at"`
	// AppSources says, per app package manager, whether it was scanned; nil
	// where this OS has none.
	AppSources []appSource `json:"app_sources"`
	// Patches and InstalledIdentifiers are never nil on the wire. A nil Go
	// slice marshals to `null`, and pydantic rejects `null` for `list[...]` --
	// so a device with nothing outstanding would fail its own clean scan. The
	// constructor initialises both, and patch_test.go asserts the empty scan
	// emits `[]`.
	Patches              []entry  `json:"patches"`
	InstalledIdentifiers []string `json:"installed_identifiers"`
	ScannedAt            string   `json:"scanned_at"`
}

// scanDocumentV1 is what a control plane below result_version 2 accepts; it
// rejects any member it does not know.
type scanDocumentV1 struct {
	OSFamily             string   `json:"os_family"`
	Patches              []entry  `json:"patches"`
	InstalledIdentifiers []string `json:"installed_identifiers"`
	ScannedAt            string   `json:"scanned_at"`
}

func (d *scanDocument) forVersion(v int) any {
	if v >= resultVersion {
		return d
	}
	// App updates are v2 as well: an older control plane has no Apps view and
	// no reason to catalogue them.
	patches := make([]entry, 0, len(d.Patches))
	for _, e := range d.Patches {
		if !appSourceNames[e.Source] {
			patches = append(patches, e)
		}
	}
	return &scanDocumentV1{
		OSFamily: d.OSFamily, Patches: patches,
		InstalledIdentifiers: d.InstalledIdentifiers, ScannedAt: d.ScannedAt,
	}
}

// applyParams implements PatchApplyParams.schema.json.
//
// `patches` is a list of free-form string maps server-side
// (`list[dict[str, str]]`); apply.py fills each with exactly `{"source": ...,
// "identifier": ...}`. This package reads those two keys and ignores the rest,
// which is the inbound half of docs/kernel/wire-protocol/README.md's rule.
//
// DeferralHours is a POINTER because its schema default (24) is not its zero
// value, and "no deferral_hours" from a slightly older control plane must not
// read as "defer for zero hours".
type applyParams struct {
	Patches       []map[string]string `json:"patches"`
	Reboot        string              `json:"reboot"`
	DeferralHours *int                `json:"deferral_hours"`
	// RebootNotAfter is the end of the maintenance window the apply was
	// dispatched in; an unattended restart must land before it.
	RebootNotAfter *string `json:"reboot_not_after"`
	ResultVersion  *int    `json:"result_version"`
}

func (p applyParams) resultVersion() int {
	if p.ResultVersion == nil {
		return 1
	}
	return *p.ResultVersion
}

const defaultDeferralHours = 24

func (p applyParams) deferralHours() int {
	if p.DeferralHours == nil {
		return defaultDeferralHours
	}
	return *p.DeferralHours
}

// applyOutcome implements PatchApplyResult's $defs/PatchApplyOutcome.
//
// NOTE FOR A LATER CYCLE: docs/kernel/wire-protocol/InstallOutcome.schema.json
// is the go-forward shared install-result contract (app-deploy AD-03), and its
// own description says `patch.apply` "will migrate its module-specific
// PatchApplyOutcome onto this contract in a LATER, DEFERRED cleanup". Until
// app/modules/patch/schema.py makes that move, the server is `extra="forbid"`
// on PatchApplyOutcome: emitting InstallOutcome's `exit_code`, `log`,
// `detected_after` or its `not_applicable`/`succeeded` states would fail the
// whole job at handle_apply_result. So this struct is PatchApplyOutcome, and
// schema_test.go pins it to the file that is actually in force.
type applyOutcome struct {
	Source     string  `json:"source"`
	Identifier string  `json:"identifier"`
	State      string  `json:"state"`
	Error      *string `json:"error"`
}

// applyResult implements PatchApplyResult.schema.json.
type applyResult struct {
	Results        []applyOutcome `json:"results"`
	RebootRequired bool           `json:"reboot_required"`
	AppliedAt      string         `json:"applied_at"`
	// RebootScheduledFor is when the OS will restart; nil when this job
	// scheduled no restart.
	RebootScheduledFor *string `json:"reboot_scheduled_for"`
	UserPresent        *bool   `json:"user_present"`
}

// applyResultV1 is what a control plane below result_version 2 accepts.
type applyResultV1 struct {
	Results        []applyOutcome `json:"results"`
	RebootRequired bool           `json:"reboot_required"`
	AppliedAt      string         `json:"applied_at"`
}

func (r *applyResult) forVersion(v int) any {
	if v >= resultVersion {
		return r
	}
	return &applyResultV1{Results: r.Results, RebootRequired: r.RebootRequired, AppliedAt: r.AppliedAt}
}

// ---------------------------------------------------------------------------
// The service
// ---------------------------------------------------------------------------

// Options wires the capability. Every field has a production default; they
// exist so the whole of this package's logic -- parsing, filtering, matching,
// outcome mapping and error classification -- is exercised on a machine that
// has neither a Windows Update Agent nor `softwareupdate`.
//
// NewEngine's type mentions unexported types on purpose. It is a TEST SEAM, not
// an extension point: no package outside this one can build an `engine`, so
// there is no way to substitute the thing that actually touches the OS in a
// shipped binary.
type Options struct {
	// NewEngine resolves the per-OS engine. Called INSIDE a verified job and
	// never at registration.
	NewEngine func(ctx context.Context) (engine, error)
	Clock     func() time.Time
	Logger    *slog.Logger
	// NewRestarter resolves the per-OS restart half, inside a job.
	NewRestarter func() restarter
	// BootTime reports when this machine last started.
	BootTime func(now time.Time) (time.Time, bool)
}

// Service is the process-wide holder. The zero value is usable and is what
// `Process` is: it resolves its own defaults at JOB TIME, so registration stays
// a map insert and nothing here touches the OS while the agent is idle.
type Service struct {
	opts Options
	// scans and applies count completed jobs, so a dormancy assertion outside
	// this package can observe that this capability has NOT run. Two atomic
	// loads, nothing else.
	scans   atomic.Int64
	applies atomic.Int64
}

// Process is the Service the default capability registry registers.
var Process = &Service{}

// New returns a Service with explicit wiring. Tests use it; production uses
// Process.
func New(o Options) *Service { return &Service{opts: o} }

// Scans and Applies report how many of each this process has completed.
func (s *Service) Scans() int64   { return s.scans.Load() }
func (s *Service) Applies() int64 { return s.applies.Load() }

func (s *Service) logger() *slog.Logger {
	if s.opts.Logger != nil {
		return s.opts.Logger
	}
	return slog.Default()
}

func (s *Service) bootTime() (time.Time, bool) {
	if s.opts.BootTime != nil {
		return s.opts.BootTime(s.now())
	}
	return bootTime(s.now())
}

func (s *Service) now() time.Time {
	if s.opts.Clock != nil {
		return s.opts.Clock()
	}
	return time.Now()
}

// resolveEngine builds the per-OS engine, at job time.
//
// A host with no engine REFUSES rather than fails, and the distinction is the
// one jobs.Refuse documents: `failed` is "this went wrong, try again",
// `refused` is "this agent will not do this". A FreeBSD box, or a Linux box
// with no apt, will never grow a patch engine by being retried -- that is a
// permanent incapacity and the control plane should hear it as one. A busy
// package database, an unreachable WSUS or a held dpkg lock is the opposite:
// those return plain errors from Available/Install and the job is `failed`.
func (s *Service) resolveEngine(ctx context.Context) (engine, error) {
	newEngine := s.opts.NewEngine
	if newEngine == nil {
		newEngine = osEngine
	}
	return newEngine(ctx)
}

// ---------------------------------------------------------------------------
// patch.scan
// ---------------------------------------------------------------------------

// Scan is the `patch.scan` capability.
//
// It reads NOTHING from the job. PatchScanParams is empty and `extra="forbid"`
// server-side, so there is nothing a job could supply -- and that is why the
// scan half of this package has no way at all to be influenced by a signed
// envelope's contents beyond the fact that one arrived.
func (s *Service) Scan(ctx context.Context, job jobs.VerifiedJob) (any, error) {
	ctx, cancel := context.WithTimeout(ctx, scanTimeout)
	defer cancel()

	version, err := parseScanParams(job.Params())
	if err != nil {
		return nil, err
	}
	doc, err := s.scan(ctx)
	if err != nil {
		return nil, err
	}
	return doc.forVersion(version), nil
}

// scan is the whole capability; Scan is the jobs.Handler shim over it.
//
// The COUNTER lives here rather than in Scan so it is observable from a test
// that does not have to manufacture a jobs.VerifiedJob -- which is
// deliberately impossible outside internal/jobs. It increments only on
// SUCCESS: a counter that moved on a refusal would make "this capability has
// never run" unobservable, and that is the exact statement the dormancy
// assertions need.
func (s *Service) scan(ctx context.Context) (*scanDocument, error) {
	eng, err := s.resolveEngine(ctx)
	if err != nil {
		return nil, err
	}
	defer eng.Close()

	available, err := eng.Available(ctx)
	if err != nil {
		// A plain error: the job is `failed`, not `refused`. An engine that
		// exists but could not answer this time is a transient condition -- the
		// package index was locked, the update server did not respond -- and
		// asking to be taken out of rotation for it would be wrong.
		return nil, fmt.Errorf("%s: %w", JobTypeScan, err)
	}

	// `installed_identifiers` is best-effort BY DESIGN and never fatal: it is a
	// reconciliation aid, and a scan that reported nothing outstanding because
	// the installed-list query failed would be a worse answer than one that
	// reported what is outstanding and left the aid empty.
	installed, err := eng.Installed(ctx)
	if err != nil {
		s.logger().Warn("patch.scan: the installed-update list is unavailable; the document still "+
			"carries what is outstanding", "err", err)
		installed = nil
	}

	doc := buildScanDocument(eng.Family(), available, installed, s.now())
	if !rebootBlind(eng) {
		required := eng.RebootRequired(ctx)
		doc.RebootRequired = &required
	}
	doc.AppSources = appSourcesOf(eng)
	if booted, ok := s.bootTime(); ok {
		at := booted.UTC().Format(rfc3339)
		doc.BootedAt = &at
	}
	s.scans.Add(1)
	return doc, nil
}

// buildScanDocument normalises whatever the engine produced into a document the
// server will accept. Separate from scan() because THIS is the part worth
// testing exhaustively and it needs no OS at all.
func buildScanDocument(family string, available []entry, installed []string, now time.Time) *scanDocument {
	doc := &scanDocument{
		OSFamily: family,
		// Never nil. See the struct comment: `null` fails pydantic and would
		// cost a clean device its whole scan.
		Patches:              make([]entry, 0, len(available)),
		InstalledIdentifiers: make([]string, 0, len(installed)),
		ScannedAt:            now.UTC().Format(rfc3339),
	}

	seen := make(map[string]bool, len(available))
	for _, e := range available {
		if len(doc.Patches) >= maxPatches {
			break
		}
		e = normalizeEntry(e)
		if e.Source == "" || e.Identifier == "" {
			// An entry with no key cannot be approved, dispatched or
			// reconciled; it would occupy a catalog row nothing can ever
			// reference. Dropping it is the honest answer.
			continue
		}
		if seen[e.key()] {
			continue
		}
		seen[e.key()] = true
		doc.Patches = append(doc.Patches, e)
	}

	installedSeen := make(map[string]bool, len(installed))
	for _, id := range installed {
		if len(doc.InstalledIdentifiers) >= maxInstalled {
			break
		}
		id = clamp(strings.TrimSpace(id), maxIdentifier+maxIdentifier)
		if id == "" || !strings.Contains(id, ":") || installedSeen[id] {
			// ingest.py splits on the first ":" and skips a key without one, so
			// sending one is pure noise on the wire and one wasted SELECT.
			continue
		}
		installedSeen[id] = true
		doc.InstalledIdentifiers = append(doc.InstalledIdentifiers, id)
	}
	return doc
}

// normalizeEntry clamps every member to its schema bound and forces `severity`
// into the closed vocabulary.
func normalizeEntry(e entry) entry {
	e.Source = clamp(strings.TrimSpace(e.Source), maxIdentifier)
	e.Identifier = clamp(strings.TrimSpace(e.Identifier), maxIdentifier)
	e.Title = clamp(strings.TrimSpace(e.Title), maxTitle)
	if e.Title == "" {
		// `title` is REQUIRED and a missing one fails the whole document. The
		// identifier is a true, if terse, title.
		e.Title = e.Identifier
	}
	e.Severity = strings.ToLower(strings.TrimSpace(e.Severity))
	if !severities[e.Severity] {
		e.Severity = "unknown"
	}
	if e.KB != nil {
		kb := clamp(strings.TrimSpace(*e.KB), maxIdentifier)
		if kb == "" {
			e.KB = nil
		} else {
			e.KB = &kb
		}
	}
	if e.SupersededBy != nil {
		sb := clamp(strings.TrimSpace(*e.SupersededBy), maxIdentifier)
		if sb == "" {
			e.SupersededBy = nil
		} else {
			e.SupersededBy = &sb
		}
	}
	if len(e.CVE) > 0 {
		cves := make([]string, 0, len(e.CVE))
		for _, c := range e.CVE {
			c = clamp(strings.TrimSpace(c), maxIdentifier)
			if c != "" && len(cves) < maxCVE {
				cves = append(cves, c)
			}
		}
		if len(cves) == 0 {
			e.CVE = nil
		} else {
			e.CVE = cves
		}
	} else {
		// A zero-length non-nil slice would marshal as `[]`, which claims "this
		// update has no CVEs" where nil correctly says "this source does not
		// report CVEs".
		e.CVE = nil
	}
	return e
}

// ---------------------------------------------------------------------------
// patch.apply
// ---------------------------------------------------------------------------

// Apply is the `patch.apply` capability.
//
// EVERY authorization this function has comes from its second argument. There
// is no local approval cache, no policy file, no environment variable and no
// second entry point; a jobs.VerifiedJob cannot be constructed outside
// internal/jobs, so there is no way to reach an installer from this package
// except through a job the runner verified.
func (s *Service) Apply(ctx context.Context, job jobs.VerifiedJob) (any, error) {
	ctx, cancel := context.WithTimeout(ctx, applyTimeout)
	defer cancel()

	res, err := s.apply(ctx, job.Params())
	if err != nil {
		return nil, err
	}
	_, p, _ := parseApplyParams(job.Params())
	return res.forVersion(p.resultVersion()), nil
}

// apply is the whole capability; Apply is the jobs.Handler shim over it. The
// counter is here for the reason scan's is.
func (s *Service) apply(ctx context.Context, raw json.RawMessage) (*applyResult, error) {
	requested, p, err := parseApplyParams(raw)
	if err != nil {
		return nil, err
	}

	eng, err := s.resolveEngine(ctx)
	if err != nil {
		return nil, err
	}
	defer eng.Close()

	// The OS describes itself FIRST, and everything that happens afterwards is
	// selected from that description. This ordering is the whole injection
	// story: no string from `raw` is ever handed to an installer, a shell or a
	// COM call -- it is only ever compared for equality against a key this
	// package computed from the OS's own output.
	available, err := eng.Available(ctx)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", JobTypeApply, err)
	}
	offered := make(map[string]entry, len(available))
	for _, e := range available {
		e = normalizeEntry(e)
		if e.Source != "" && e.Identifier != "" {
			offered[e.key()] = e
		}
	}

	installedSet := map[string]bool{}
	if installed, ierr := eng.Installed(ctx); ierr != nil {
		s.logger().Warn("patch.apply: the installed-update list is unavailable; a patch already "+
			"present will be attempted again rather than reported installed", "err", ierr)
	} else {
		for _, id := range installed {
			installedSet[id] = true
		}
	}

	outcomes := make([]applyOutcome, 0, len(requested))
	for _, ref := range requested {
		outcomes = append(outcomes, s.applyOne(ctx, eng, offered, installedSet, ref))
	}

	// Asked AFTER the installs, because an install is the usual reason the
	// answer changes.
	rebootRequired := eng.RebootRequired(ctx)
	res := &applyResult{
		Results:        outcomes,
		RebootRequired: rebootRequired,
		AppliedAt:      s.now().UTC().Format(rfc3339),
	}

	// Below result_version 2 the control plane cannot see a restart, so this
	// agent reports and does not act.
	wantRestart := p.Reboot == "forced" || (p.Reboot == "if_required" && rebootRequired)
	if wantRestart && p.resultVersion() >= resultVersion {
		// Validated in parseApplyParams.
		notAfter, _ := parseNotAfter(p.RebootNotAfter, JobTypeApply)
		plan, scheduled, rerr := s.scheduleRestart(ctx, p.deferralHours(), notAfter)
		present := plan.Session.UserPresent
		res.UserPresent = &present
		switch {
		case rerr != nil:
			// The installs stand; reboot_required still reaches the control
			// plane, which schedules the restart itself.
			s.logger().Warn("patch.apply: the restart could not be scheduled", "err", rerr)
		case scheduled:
			at := plan.At.UTC().Format(rfc3339)
			res.RebootScheduledFor = &at
		}
		if p.Reboot == "forced" {
			res.RebootRequired = true
		}
	}

	s.applies.Add(1)
	return res, nil
}

// patchRef is one `{source, identifier}` pair out of the signed params. It is
// a LOOKUP KEY and never anything else.
type patchRef struct{ Source, Identifier string }

func (r patchRef) key() string { return r.Source + ":" + r.Identifier }

// applyOne resolves one request against what the OS offers and installs it, or
// explains in the outcome why it did not.
//
// Every path returns an outcome. There is no path that returns an error and
// abandons the rest of the batch: dispatch_apply puts all of a device's
// eligible patches in ONE job, so a single bad reference must not cost the
// other fifteen their result.
func (s *Service) applyOne(
	ctx context.Context, eng engine, offered map[string]entry, installed map[string]bool, ref patchRef,
) applyOutcome {
	if ref.Source == "" || ref.Identifier == "" {
		return failedOutcome(ref, "the job named a patch with no source or no identifier; nothing "+
			"was installed for it")
	}

	// Already present. Checked BEFORE `offered` so a re-dispatched job -- the
	// window sweep runs hourly and a result can be lost on the way back -- is
	// idempotent instead of reporting a failure for work that is done.
	if installed[ref.key()] {
		return applyOutcome{Source: ref.Source, Identifier: ref.Identifier, State: "installed"}
	}

	e, ok := offered[ref.key()]
	if !ok {
		// The control plane approved something this device is not being
		// offered: superseded, already applied out of band, or applicable to a
		// different build. `failed` with a plain reason is the honest answer;
		// the alternative -- searching for it by name -- is exactly the "build
		// a command from a job-supplied string" this package refuses to do.
		return failedOutcome(ref, "this device is not being offered that update; it may be "+
			"superseded, already applied out of band, or not applicable to this build. Nothing "+
			"was installed for it")
	}

	if err := eng.Install(ctx, e); err != nil {
		return failedOutcome(ref, err.Error())
	}
	return applyOutcome{Source: ref.Source, Identifier: ref.Identifier, State: "installed"}
}

func failedOutcome(ref patchRef, reason string) applyOutcome {
	msg := sanitizeError(reason)
	return applyOutcome{
		Source:     ref.Source,
		Identifier: ref.Identifier,
		State:      "failed",
		Error:      &msg,
	}
}

// parseApplyParams validates the whole envelope before anything is opened, and
// REFUSES rather than fails on every violation.
//
// `refused` and not `failed` for this class for the reason jobs.Refuse
// documents: a params document this agent will not act on is not a transient
// condition, and no number of retries will make it one. The reason travels into
// the server's hash-chained audit log through JobEngine.refuse, so it is this
// agent's OWN SENTENCE -- no job-supplied string is formatted into it.
func parseApplyParams(raw json.RawMessage) ([]patchRef, applyParams, error) {
	var p applyParams
	if err := json.Unmarshal(raw, &p); err != nil {
		return nil, p, jobs.Refuse("patch.apply: params are not a PatchApplyParams; nothing was installed")
	}

	if !rebootPolicies[p.Reboot] {
		// Refused, not defaulted. `reboot` is the operator's stated intent for
		// a privileged, irreversible action on a machine somebody is using, and
		// an agent that cannot name the intent must not proceed with the
		// install either -- silently substituting a default would make the
		// console's account of what was authorized untrue.
		//
		// THE COMPATIBILITY COST IS REAL AND DELIBERATE: a fourth reboot value
		// added server-side takes every deployed agent's patch apply offline.
		// docs/kernel/wire-protocol/README.md already says a change in that
		// directory is a compatibility decision -- additive, or versioned, or
		// it breaks deployed agents -- and this is one of the places where that
		// is literally true.
		return nil, p, jobs.Refuse("patch.apply: the reboot policy is not one of never, " +
			"if_required or forced. This agent will not install under a policy it cannot name; " +
			"nothing was installed")
	}

	if _, err := parseNotAfter(p.RebootNotAfter, JobTypeApply); err != nil {
		return nil, p, err
	}
	if p.DeferralHours != nil && *p.DeferralHours < 0 {
		return nil, p, jobs.Refuse("patch.apply: deferral_hours is negative; nothing was installed")
	}

	if len(p.Patches) == 0 {
		// dispatch_apply skips a device with nothing eligible, so an empty
		// batch is a control-plane bug rather than a state of the world.
		return nil, p, jobs.Refuse("patch.apply: the job carries no patches; nothing was installed")
	}
	if len(p.Patches) > maxApplyBatch {
		return nil, p, jobs.Refuse("patch.apply: the job carries more than %d patches, which is "+
			"past anything dispatch_apply produces for one device; nothing was installed",
			maxApplyBatch)
	}

	refs := make([]patchRef, 0, len(p.Patches))
	for _, m := range p.Patches {
		// Exactly two keys are read. Any other member of the map -- and the
		// server's type permits any -- is IGNORED rather than rejected, which
		// is the inbound half of the wire-protocol rule: a strictly additive
		// server change must not take a deployed fleet offline.
		refs = append(refs, patchRef{
			Source:     clamp(strings.TrimSpace(m["source"]), maxIdentifier),
			Identifier: clamp(strings.TrimSpace(m["identifier"]), maxIdentifier),
		})
	}
	return refs, p, nil
}

// ---------------------------------------------------------------------------
// Text hygiene
// ---------------------------------------------------------------------------

// clamp truncates to n RUNES, never to n bytes.
//
// Truncating a UTF-8 string mid-rune produces invalid UTF-8, encoding/json
// replaces it with U+FFFD, and a Japanese update title would arrive as a row of
// replacement characters in an operator's console.
func clamp(s string, n int) string {
	if n <= 0 {
		return ""
	}
	runes := []rune(s)
	if len(runes) <= n {
		return s
	}
	return string(runes[:n])
}

// sanitizeError bounds and de-fangs text that came from the OS.
//
// The `error` member lands in `device_patch_status.last_error` and from there
// on an operator's screen. It is not this agent's prose: it is whatever apt,
// softwareupdate or the Windows Update Agent said, so it is stripped of control
// characters (a bare CR rewrites a terminal line; an ANSI escape does worse)
// and bounded.
func sanitizeError(s string) string {
	s = strings.Map(func(r rune) rune {
		switch {
		case r == '\n' || r == '\t':
			return ' '
		case r < 0x20 || r == 0x7f:
			return -1
		}
		return r
	}, s)
	s = strings.Join(strings.Fields(s), " ")
	if s == "" {
		return "the OS reported a failure with no message"
	}
	return clamp(s, maxErrorText)
}
