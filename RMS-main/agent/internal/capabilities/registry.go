// Package capabilities is the agent's map from a signed job type to the code
// that runs it.
//
// # Where it sits
//
// The runner (internal/jobs) verifies a frame -- signature over canonical bytes
// against the pinned Ed25519 key, target device, freshness, nonce, known type,
// in that order -- and only then looks a handler up here. A type this registry
// does not know is refused `unknown_type` BEFORE anything runs, which is both
// the correct answer for a capability this build does not have and the answer
// IPAM's dispatcher already reads as "this agent cannot do that"
// (docs/modules/ipam/spec.md §5.3).
//
// The dependency runs capabilities -> jobs and never back: jobs declares
// Handler and Registry as an interface precisely so it does not import this
// package.
//
// # Why the default registry is immutable rather than a mutable global
//
// CLAUDE.md allows "no global mutable state except the capability registry,
// which is written once at init and read-only thereafter". The plan's Task 10
// spelled that as a package-level `Register` that any init() may call. This
// implements the stronger version: the default registry is built once, by
// mustBuild, from a list that is visible in one place, and there is no exported
// way to add to it afterwards. A capability that is not in that list is not in
// the binary's answer to "what can you do?", and there is no code path that can
// change that at runtime.
//
// A duplicate registration PANICS at init. Two capabilities claiming one job
// type is an ambiguity in the command path, and a binary with one must not
// start.
//
// # Dormancy
//
// Registration is a map insert. Nothing here opens a file, dials anything or
// starts a goroutine, and no capability is invoked until a verified job arrives
// -- CLAUDE.md's "heavy capabilities stay dormant and only allocate/initialize
// when a job for them actually runs". registry_test.go asserts that with a
// counter rather than describing it.
package capabilities

import (
	"errors"
	"fmt"
	"sort"

	"github.com/toeshee/it-suite/agent/internal/capabilities/backup"
	"github.com/toeshee/it-suite/agent/internal/capabilities/cert"
	"github.com/toeshee/it-suite/agent/internal/capabilities/deploy"
	"github.com/toeshee/it-suite/agent/internal/capabilities/health"
	"github.com/toeshee/it-suite/agent/internal/capabilities/inventory"
	"github.com/toeshee/it-suite/agent/internal/capabilities/jobcancel"
	"github.com/toeshee/it-suite/agent/internal/capabilities/logs"
	"github.com/toeshee/it-suite/agent/internal/capabilities/netsweep"
	"github.com/toeshee/it-suite/agent/internal/capabilities/patch"
	"github.com/toeshee/it-suite/agent/internal/capabilities/provision"
	"github.com/toeshee/it-suite/agent/internal/capabilities/pushinstall"
	"github.com/toeshee/it-suite/agent/internal/capabilities/remote"
	"github.com/toeshee/it-suite/agent/internal/capabilities/selfupdate"
	"github.com/toeshee/it-suite/agent/internal/capabilities/site"
	"github.com/toeshee/it-suite/agent/internal/capabilities/uninstall"
	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// Handler is a capability: it receives a job the runner has verified and a
// context bounded by that job's expires_at.
//
// It is an ALIAS for jobs.Handler, not a new type, so *Registry satisfies
// jobs.Registry without an adapter and a capability written against either name
// is the same function.
type Handler = jobs.Handler

// ErrDuplicate is returned when a job type is registered twice.
var ErrDuplicate = errors.New("capabilities: job type already registered")

// Registry maps a job type to its capability.
//
// Zero value is not usable; use New. Once built it is only read, so it needs no
// lock: the default registry is finished before main runs and never changes.
type Registry struct {
	handlers map[string]Handler
	// markers are advertise-only names: listed by Names() so the control plane
	// can see what this build can do, but NOT job types -- Lookup never finds
	// them, so a job claiming one is refused `unknown_type` like any other.
	markers map[string]struct{}
}

// New returns an empty registry. Exported for tests and for anything that needs
// a registry that is not this build's.
func New() *Registry {
	return &Registry{handlers: make(map[string]Handler), markers: make(map[string]struct{})}
}

// Register adds a capability.
//
// It refuses an empty job type, a nil handler and a duplicate. All three are
// programming errors, and the default registry turns them into a panic at init
// -- a binary whose command path is ambiguous must not start.
func (r *Registry) Register(jobType string, h Handler) error {
	if jobType == "" {
		return errors.New("capabilities: a capability with no job type can never be dispatched")
	}
	if h == nil {
		return fmt.Errorf("capabilities: %q has a nil handler; the runner would refuse it as "+
			"unknown_type after advertising it", jobType)
	}
	if _, exists := r.handlers[jobType]; exists {
		return fmt.Errorf("%w: %q", ErrDuplicate, jobType)
	}
	if _, exists := r.markers[jobType]; exists {
		return fmt.Errorf("%w: %q is already advertised as a marker", ErrDuplicate, jobType)
	}
	r.handlers[jobType] = h
	return nil
}

// AdvertiseOnly adds a name to Names() WITHOUT a handler.
//
// It exists for a capability that is a property of a job type rather than a
// job type of its own -- `agent.push_install.winrm` says "this build's
// agent.push_install speaks the Kerberos-only WinRM transport" (plan
// 2026-09-28-windows-push-winrm Task 4B, §I6), which the control plane reads to
// skip a site server too old to push to Windows. Lookup never finds a marker,
// so a job of that "type" is still refused `unknown_type` before anything runs,
// and Types() -- the job-type view -- does not list it.
//
// Same refusals as Register: an empty name, and a name that is already a job
// type or a marker, are programming errors.
func (r *Registry) AdvertiseOnly(name string) error {
	if name == "" {
		return errors.New("capabilities: an advertised capability needs a name")
	}
	if _, exists := r.handlers[name]; exists {
		return fmt.Errorf("%w: %q is a registered job type", ErrDuplicate, name)
	}
	if _, exists := r.markers[name]; exists {
		return fmt.Errorf("%w: %q", ErrDuplicate, name)
	}
	r.markers[name] = struct{}{}
	return nil
}

// Lookup implements jobs.Registry.
//
// The bool is the whole mechanism behind the runner's `unknown_type` refusal.
// It allocates nothing: this runs on the path of every inbound job frame.
func (r *Registry) Lookup(jobType string) (Handler, bool) {
	h, ok := r.handlers[jobType]
	return h, ok
}

// Names lists the registered job types AND the advertise-only markers, sorted.
//
// This is what the agent advertises at enrollment (`agents.capabilities`), so
// it must be exactly what the binary can actually do -- never a hand-written
// list. It returns a COPY: a caller that mutated the result would be rewriting
// the registry's own account of itself.
func (r *Registry) Names() []string {
	out := make([]string, 0, len(r.handlers)+len(r.markers))
	for name := range r.handlers {
		out = append(out, name)
	}
	for name := range r.markers {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

// Types lists the registered job types, sorted -- Names without the
// advertise-only markers. It is the registered-job-type view that reads
// naturally next to jobs.DefaultHeavyKeys (a "job type" set), which is what
// the HeavyKeys-integrity test compares it against.
//
// Returns a COPY, same as Names: a caller that mutated the result would be
// rewriting the registry's own account of itself.
func (r *Registry) Types() []string {
	out := make([]string, 0, len(r.handlers))
	for name := range r.handlers {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

// defaultRegistry is this build's capabilities, built once at package
// initialisation and never modified.
var defaultRegistry = mustBuild()

// Default returns this build's registry.
func Default() *Registry { return defaultRegistry }

// Lookup is Default().Lookup.
func Lookup(jobType string) (Handler, bool) { return defaultRegistry.Lookup(jobType) }

// Names is Default().Names.
func Names() []string { return defaultRegistry.Names() }

// Types is Default().Types, exposed at package level -- mirroring Lookup and
// Names above -- so a test in package capabilities_test, which cannot call
// the package-private mustBuild, can still assert every
// jobs.DefaultHeavyKeys entry matches a registered job type.
func Types() []string { return defaultRegistry.Types() }

// mustBuild is the one list of what this binary can do.
//
// Adding a capability is one line here plus its package. There is no init()
// side-channel and no build tag that adds one, so this list and `go doc` agree
// with the running binary by construction.
func mustBuild() *Registry {
	r := New()
	must(r.Register("hello", Hello))
	must(r.Register("inventory.collect", inventory.Collect))
	// Certificate rotation (docs/modules/agent/cert-renewal.md). These are the
	// first capabilities with process state -- they read and write the data
	// directory -- and `cert.Process` resolves that directory from the SAME
	// source main did (internal/config, i.e. the environment this process was
	// started with) at JOB TIME, not here. So registration stays a map insert
	// and nothing touches the disk until a verified job arrives.
	//
	// They are registered unconditionally: an agent that advertised renewal
	// only when some precondition held would be an agent whose certificate
	// silently stops rotating, which is the failure this whole design exists to
	// remove.
	must(r.Register(cert.JobTypeRenew, cert.Process.Renew))
	must(r.Register(cert.JobTypeInstall, cert.Process.Install))
	// Interruption (docs/superpowers/specs/2026-08-22-job-lifecycle-design.md
	// §3.1). A cancel that reaches an endpoint is an ORDINARY SIGNED JOB and
	// gets no exemption from invariant #1 -- an unsigned one would be a
	// fleet-wide denial-of-service primitive, one frame per running job.
	//
	// Registered unconditionally and inert until main calls
	// `jobcancel.Process.Bind(runner)`, on the same precedent as health and
	// backup above: an agent that advertised interruptibility only when some
	// start-up ordering worked out would be an agent whose cancels silently
	// stop being honoured. Unbound it REFUSES and names why -- which is a
	// different and more useful answer than `unknown_type`, because that would
	// tell the operator this build cannot be interrupted at all.
	//
	// `capabilityKey("job.cancel")` is `"job"`: its own Dispatcher queue, and
	// deliberately NOT in `jobs.DefaultHeavyKeys`, so a cancel is never queued
	// behind the heavy job it is cancelling and never waits on the heavy
	// semaphore. That property is why the type is named `job.cancel` rather
	// than `backup.cancel`.
	must(r.Register(jobcancel.JobType, jobcancel.Process.Cancel))
	// The health edge tick (docs/modules/endpoint-health-alerting/spec.md §0.4,
	// §5.2, §5.3). Both are registered unconditionally and both are inert until
	// main calls health.Process.Bind: registration stays a map insert, and an
	// agent that advertised the capability only when some precondition held
	// would be an agent whose policy silently stops arriving.
	must(r.Register(health.JobTypePolicyApply, health.Process.Apply))
	must(r.Register(health.JobTypeCheckNow, health.Process.CheckNow))
	// Job-scoped ICMP/ARP discovery (docs/modules/ipam/spec.md §5). Advertising
	// it is what makes IPAM's PREFERRED dispatch path work -- `select_sweeper`
	// reads `agents.capabilities` first (§5.3 step 1) and only falls back to
	// dispatching blind and reading a `refused` job as the answer.
	//
	// Registration opens no socket. netsweep.Process resolves its own wiring at
	// JOB TIME and holds nothing between jobs (§5.5: "dormant by default").
	must(r.Register(netsweep.JobType, netsweep.Process.Sweep))
	// Wake-on-LAN from a promoted site server (docs/modules/site-server/spec.md
	// §5). Registering it is what makes the module's headline feature work end
	// to end: the control plane's `POST /site-servers/{id}/wake` and
	// provisioning's `wake_on_lan` both create a `site.wake` job, and until this
	// line existed every one of them was refused `unknown_type`.
	//
	// Registration opens no socket and takes no timer-wheel entry. site.Process
	// resolves its own wiring at JOB TIME and holds nothing between jobs; the
	// UDP socket is dialled inside the verified job and closed in a defer.
	//
	// Registered unconditionally, like the rest: an agent that advertised the
	// capability only on a host that looked like a site server would be an agent
	// whose promotion silently does not take effect. Whether this device may
	// wake anything is the CONTROL PLANE's decision -- it dispatches a signed
	// job or it does not -- and it is not one the endpoint gets a vote in.
	must(r.Register(site.JobType, site.Process.Wake))
	// Agent push install (docs/modules/agent-distribution/spec-push-install.md
	// §4, §7, §11). Two capabilities, one package: `agent.push_probe` reads a
	// target's host key carrying NO credential, and `agent.push_install`
	// verifies that confirmed key, authenticates, verifies the artefact digest
	// before transferring, places the agent and the single-use enrolment token,
	// and zeroes the credential.
	//
	// Registration opens no socket and dials nothing: pushinstall.Process
	// resolves its SSH dialer and HTTP downloader at JOB TIME and holds neither
	// between jobs (spec §11 dormancy, budget_test.go). Registered
	// unconditionally like the rest -- whether this device SHOULD push is the
	// control plane's decision, carried in a signed job to a declared site
	// server, and not a vote the endpoint gets.
	//
	// This is the ONE package in the agent that connects OUTBOUND to a machine
	// that is not an endpoint of this platform (2a clause (a)); invariant #2's
	// no-inbound-listener rule is untouched, and internal/transport's
	// listen_test.go still forbids a listening port anywhere.
	must(r.Register(pushinstall.JobTypeProbe, pushinstall.Process.Probe))
	must(r.Register(pushinstall.JobTypeInstall, pushinstall.Process.Install))
	// Windows push over WinRM (plan 2026-09-28-windows-push-winrm Task 4B,
	// §I6). NOT a job type: the WinRM transport lives inside the two push
	// capabilities above. The marker tells the control plane this build's
	// pushinstall speaks the Kerberos-only WinRM transport, so for a Windows
	// target `_site_server_for` skips a site server whose (older) agent does
	// not advertise it, rather than sending it a job it would refuse. The
	// client is pure Go, so it is advertised on every GOOS: a Linux site
	// server pushes to Windows as well as a Windows one does.
	must(r.AdvertiseOnly(pushinstall.CapabilityWinRM))
	// Review Minor 1: this build's SSH push hands the placed binary its enroll
	// URL (`its-agent install --enroll-url`). An older build ran `install
	// --token-file` alone, which installs an agent that never learns where to
	// enroll, so the control plane uses a site server as an SSH originator only
	// when it advertises this marker. Advertise-only, like the WinRM one.
	must(r.AdvertiseOnly(pushinstall.CapabilityEnrollURL))
	// --- The eight that closed the gap between what the control plane can
	// dispatch and what an endpoint can do -------------------------------
	//
	// Before these lines the server could dispatch 16 job types and the agent
	// answered 8 of them; every patch, deployment, backup, provisioning check
	// and log shipment was refused `unknown_type`. The modules were fully built
	// server-side -- approval rings, maintenance windows, repository formats --
	// and had nothing on the other end.
	//
	// All eight follow the same discipline as the eight above: registration is
	// a map insert, each package resolves its own wiring at JOB TIME, and none
	// of them opens a file, dials anything or starts a goroutine here. Their
	// budget tests measure that rather than asserting it.
	//
	// Registered unconditionally, for the reason the entries above give: an
	// agent that advertised a capability only when some local precondition held
	// would be an agent that silently stops answering, and whether this device
	// SHOULD do any of these is the control plane's decision, carried in a
	// signed job.
	//
	// `app.deploy` (artifact-backed): this agent's fetcher (deploy/fetch.go)
	// handles an `https://` `artifact_ref` and refuses an `s3://` one by name
	// -- it holds no object-store credential and must not (invariant #3).
	// That is what the code shows and no more: whether a real deployment
	// completes end to end depends on what `artifact_ref` the control plane
	// actually sends a device, which is a server-side fact nothing in this
	// repository verifies, so this comment does not claim it either way.
	// Registering it unconditionally is still right -- a refusal naming what
	// is missing is a better answer than `unknown_type`, and it is the answer
	// the control plane can act on.
	//
	// `backup.run` / `backup.restore`: every seam is bound in main.go, and
	// NEITHER HAS BEEN OBSERVED COMPLETING END TO END on any platform.
	// main.go's `backup.Process.Bind` supplies all three: `QueryChunks` (the
	// dedup exchange, spec §5.2, which returns a short-lived prefix-scoped
	// POST grant on the agent's own mTLS channel), `OpenStore` bound to
	// `backupstore.Opener(readGrants)` (spends that write grant, and for a
	// restore the batched per-object GET grants of the
	// `backup_read_query`/`backup_read_grant` exchange), and `OpenSnapshot`
	// bound to `backup.PlatformSnapshot` (VSS / LVM / APFS by GOOS). The
	// dedup-exchange deadlock against the single-reader transport is fixed:
	// jobs run on `jobs.Dispatcher` workers, not the read goroutine (spec
	// §6.1 item 5). What remains (spec §6.1 item 4, agent-engine.md §11,
	// HANDOFF.md §5): a Linux root not on an LVM logical volume still refuses;
	// the one real-LVM test skips everywhere this project has run; the VSS
	// and APFS providers have never executed, and VSS never checks writer
	// status, so it can report success over torn application data; and the
	// restore leg of `scripts/verify_backup_run.py` has never run against a
	// real object store. The plan that closes this is
	// docs/modules/backup/spec-server-backup.md.
	must(r.Register(deploy.JobTypeScript, deploy.Process.Run))
	must(r.Register(deploy.JobTypeDeploy, deploy.Process.Deploy))
	must(r.Register(patch.JobTypeScan, patch.Process.Scan))
	must(r.Register(patch.JobTypeApply, patch.Process.Apply))
	// Restart inside the maintenance window, dispatched by the control plane
	// when a device still needs one. Shares the "patch" queue, so it never
	// overlaps an apply.
	must(r.Register(patch.JobTypeReboot, patch.Process.Reboot))
	must(r.Register(backup.JobTypeRun, backup.Process.Run))
	must(r.Register(backup.JobTypeRestore, backup.Process.Restore))
	must(r.Register(provision.JobType, provision.Process.Enroll))
	// `logs.ship` is the one that needs main.go to call `logs.Process.Bind`;
	// unbound it answers `logs.ErrUnbound` and reads nothing. Registered
	// unconditionally anyway, on health's precedent -- the alternative is a
	// binary whose advertised capabilities depend on start-up ordering.
	must(r.Register(logs.JobType, logs.Process.Ship))
	// Signed self-update (spec-self-update.md). Inert until main binds it; the
	// only capability allowed to return jobs.ErrDeferred.
	must(r.Register(selfupdate.JobType, selfupdate.Process.Update))
	// Reject (install-link spec 2026-09-28 §1.5). The agent removes itself:
	// a detached helper stops and deletes the service and removes the binary
	// and the data directory. Registered only where this build knows how to
	// remove itself -- this list is the enrollment advertisement, and the
	// server revokes an agent that does not advertise it instead of asking.
	// Registration is a map insert; main binds the launcher.
	if uninstall.Supported {
		must(r.Register(uninstall.JobType, uninstall.Process.Run))
	}
	// Screen share (docs/modules/remote-control/spec-screen-share.md §2.3).
	// Registered only where this build has a helper -- Windows in phase 1 --
	// because this list is also the enrollment advertisement, and the server
	// answers "agent too old to share its screen" for a device that does not
	// advertise it. `remote.Supported` is a constant chosen by GOOS, so the
	// list is still fixed per binary. Registration is a map insert; main binds
	// the TLS identity and the launcher, and nothing runs until a signed job.
	if remote.Supported {
		must(r.Register(remote.JobType, remote.Process.Open))
	}
	return r
}

func must(err error) {
	if err != nil {
		// At init, before anything is listening. A binary whose command path is
		// ambiguous must not start.
		panic(err)
	}
}
