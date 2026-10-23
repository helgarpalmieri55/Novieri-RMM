package capabilities_test

import (
	"context"
	"errors"
	"os"
	"runtime"
	"sort"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/budget"
	"github.com/toeshee/it-suite/agent/internal/capabilities"
	"github.com/toeshee/it-suite/agent/internal/capabilities/inventory"
	"github.com/toeshee/it-suite/agent/internal/capabilities/remote"
	"github.com/toeshee/it-suite/agent/internal/capabilities/site"
	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// The registry is the join between the signed-job runner and the code that
// actually touches the host. Two properties are load-bearing and both are
// asserted here rather than described:
//
//  1. A type that is not registered LOOKS UP FALSE. That is the whole mechanism
//     behind the runner's `unknown_type` refusal (jobs/verify.go), and it is
//     what IPAM's dispatcher reads as "this agent cannot do that".
//  2. Registration is inert. Building the registry runs no capability, opens
//     nothing, and spawns no goroutine -- CLAUDE.md's "heavy capabilities stay
//     dormant and only allocate/initialize when a job for them actually runs".

// unregisteredType is a job type this build will never implement, used wherever
// a test needs "a type the registry does not know".
//
// It is deliberately NOT a real-but-unimplemented job type. This test used
// "script.run" until script.run was implemented, at which point both tests
// using it failed -- correctly, but for a reason that had nothing to do with
// what they assert. A sentinel that a future commit can make real is a test
// that expires. The `no.such.capability` prefix is not in
// app/jobs/registry.py's namespace and is not going to be.
const unregisteredType = "no.such.capability.this.build.will.never.run"

func TestLookupOfAnUnregisteredTypeReturnsFalse(t *testing.T) {
	// FALSIFIER: a registry that answered `true, nil` for an unknown type would
	// hand the runner a nil handler, and the runner would refuse anyway -- so
	// the observable here is the BOOL, not an absence of panic.
	h, ok := capabilities.Default().Lookup(unregisteredType)
	if ok {
		t.Fatalf("Lookup(%q) = true; nothing may be registered for a type this build cannot run", unregisteredType)
	}
	if h != nil {
		t.Fatalf("Lookup(%q) returned a non-nil handler alongside ok=false", unregisteredType)
	}
}

func TestTheDefaultRegistryCarriesExactlyTheCompiledInCapabilities(t *testing.T) {
	got := capabilities.Names()
	want := []string{
		"agent.cert.install", "agent.cert.renew",
		// Agent push install, cycle B2 (docs/modules/agent-distribution/
		// spec-push-install.md §4, §11): the probe reads a host key with no
		// credential, the install verifies it and places the agent.
		"agent.push_install",
		// Windows push over WinRM (plan 2026-09-28-windows-push-winrm Task 4B,
		// §I6): an ADVERTISE-ONLY marker, not a job type. It tells the control
		// plane this build's pushinstall speaks the Kerberos-only WinRM
		// transport, so a site server without it is skipped for a winrm target.
		// Review Minor 1: this build's SSH push hands the placed binary its
		// enroll URL (`install --enroll-url`); an older one does not, and the
		// control plane skips it as an SSH originator.
		"agent.push_install.enroll_url",
		"agent.push_install.winrm",
		"agent.push_probe",
		// Reject's uninstall (install-link spec 2026-09-28 §1.5): Windows, Linux and macOS.
		"agent.uninstall",
		// Signed self-update (spec-self-update.md).
		"agent.update",
		"app.deploy", "backup.restore", "backup.run",
		"health.check.now", "health.policy.apply",
		"hello", "inventory.collect", "job.cancel", "logs.ship", "net.sweep",
		"patch.apply", "patch.reboot", "patch.scan", "provision.enroll",
		"script.run", "site.wake",
	}
	if runtime.GOOS == "windows" {
		// Screen share has a helper on Windows only (phase 1).
		want = append(want, "remote.screen.open")
		sort.Strings(want)
	}
	if len(got) != len(want) {
		t.Fatalf("Names() = %v; want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("Names() = %v; want %v", got, want)
		}
	}
	if !sort.StringsAreSorted(got) {
		t.Fatalf("Names() = %v; not sorted -- it is advertised at enrollment and compared by operators", got)
	}
	for _, name := range want {
		if advertiseOnlyMarkers[name] {
			// A marker is advertised, never dispatched: TestAdvertiseOnly...
			// asserts it looks up FALSE.
			continue
		}
		if _, ok := capabilities.Default().Lookup(name); !ok {
			t.Fatalf("Names() advertises %q but Lookup does not find it; advertising a job type this "+
				"binary cannot run is worse than advertising none", name)
		}
	}
}

func TestNamesIsACopyTheCallerCannotUseToMutateTheRegistry(t *testing.T) {
	// FALSIFIER: returning the backing slice lets `enroll` -- or anything else
	// that advertises capabilities -- rewrite the registry's own view of what
	// this binary can do. Observable: the second call's contents.
	first := capabilities.Names()
	if len(first) == 0 {
		t.Fatal("no capabilities registered; this test would be vacuous")
	}
	first[0] = "script.run"
	second := capabilities.Names()
	if second[0] == "script.run" {
		t.Fatalf("Names() handed out its backing array: second call = %v", second)
	}
}

// advertiseOnlyMarkers are names Names() advertises that are NOT job types.
var advertiseOnlyMarkers = map[string]bool{
	"agent.push_install.winrm":      true,
	"agent.push_install.enroll_url": true,
}

// TestNamesAdvertisesTheWinRMPushCapabilityDistinctly is plan Task 4B (§I6):
// the control plane must be able to tell a WinRM-capable site server from one
// that only pushes over SSH, so the agent advertises `agent.push_install.winrm`
// IN ADDITION TO `agent.push_install`, and both push handlers stay registered.
//
// FALSIFIER: dropping the AdvertiseOnly line removes the name from Names(); a
// marker that was registered as a handler instead would make Lookup succeed and
// the runner would accept a job of that "type".
func TestNamesAdvertisesTheWinRMPushCapabilityDistinctly(t *testing.T) {
	names := capabilities.Names()
	has := func(n string) bool {
		for _, x := range names {
			if x == n {
				return true
			}
		}
		return false
	}
	for _, n := range []string{"agent.push_install", "agent.push_install.winrm", "agent.push_probe"} {
		if !has(n) {
			t.Errorf("Names() = %v; missing %q", names, n)
		}
	}
	for _, jt := range []string{"agent.push_probe", "agent.push_install"} {
		if _, ok := capabilities.Lookup(jt); !ok {
			t.Errorf("%q is no longer registered as a handler", jt)
		}
	}
	// The marker is NOT a job type: a job claiming it is refused unknown_type.
	if h, ok := capabilities.Lookup("agent.push_install.winrm"); ok || h != nil {
		t.Fatal("agent.push_install.winrm looked up as a handler; it is an advertisement, not a job type")
	}
	// And it is not in the job-type view the HeavyKeys integrity check reads.
	for _, jt := range capabilities.Types() {
		if advertiseOnlyMarkers[jt] {
			t.Fatalf("Types() carries the advertise-only marker %q", jt)
		}
	}
}

func TestAdvertiseOnlyRefusesEmptyAndDuplicateNames(t *testing.T) {
	r := capabilities.New()
	if err := r.AdvertiseOnly(""); err == nil {
		t.Fatal("AdvertiseOnly(\"\") = nil; want an error")
	}
	if err := r.Register("x.y", noop); err != nil {
		t.Fatal(err)
	}
	if err := r.AdvertiseOnly("x.y"); !errors.Is(err, capabilities.ErrDuplicate) {
		t.Fatalf("AdvertiseOnly of a registered job type: err = %v; want ErrDuplicate", err)
	}
	if err := r.AdvertiseOnly("x.y.marker"); err != nil {
		t.Fatal(err)
	}
	if err := r.AdvertiseOnly("x.y.marker"); !errors.Is(err, capabilities.ErrDuplicate) {
		t.Fatalf("second AdvertiseOnly: err = %v; want ErrDuplicate", err)
	}
	if err := r.Register("x.y.marker", noop); !errors.Is(err, capabilities.ErrDuplicate) {
		t.Fatalf("Register over a marker: err = %v; want ErrDuplicate", err)
	}
	if got := r.Names(); len(got) != 2 || got[0] != "x.y" || got[1] != "x.y.marker" {
		t.Fatalf("Names() = %v; want [x.y x.y.marker]", got)
	}
	if got := r.Types(); len(got) != 1 || got[0] != "x.y" {
		t.Fatalf("Types() = %v; want [x.y]", got)
	}
}

func TestRegisteringTheSameTypeTwiceIsAnError(t *testing.T) {
	// Two capabilities claiming one job type is an ambiguity in the command
	// path. It must be impossible to resolve silently.
	r := capabilities.New()
	if err := r.Register("inventory.collect", noop); err != nil {
		t.Fatalf("first Register: %v", err)
	}
	err := r.Register("inventory.collect", noop)
	if !errors.Is(err, capabilities.ErrDuplicate) {
		t.Fatalf("second Register: err = %v; want ErrDuplicate", err)
	}
}

func TestRegisteringNothingUsefulIsAnError(t *testing.T) {
	for _, tc := range []struct {
		name    string
		jobType string
		handler capabilities.Handler
	}{
		{"empty job type", "", noop},
		{"nil handler", "inventory.collect", nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if err := capabilities.New().Register(tc.jobType, tc.handler); err == nil {
				t.Fatalf("Register(%q, %v) = nil; want an error", tc.jobType, tc.handler != nil)
			}
		})
	}
}

func TestTheRegistrySatisfiesTheRunnersRegistryInterface(t *testing.T) {
	// A compile-time assertion written as a test so its failure names the
	// reason: the runner takes a jobs.Registry and nothing else, and a registry
	// that has drifted from that interface would otherwise fail in main.go.
	var _ jobs.Registry = capabilities.Default()
}

// ---------------------------------------------------------------------------
// Dormancy (CLAUDE.md's idle budget; plan Task 10 steps 1 and 6)
// ---------------------------------------------------------------------------

func TestBuildingTheRegistryRunsNoCapability(t *testing.T) {
	// FALSIFIER: a capability that does its setup at registration -- opening a
	// socket, reading /proc, priming a cache -- shows up as a non-zero counter
	// here. Observable: the counter, not "no error".
	var setups atomic.Int64
	spy := func(context.Context, jobs.VerifiedJob) (any, error) {
		setups.Add(1)
		return map[string]any{}, nil
	}
	r := capabilities.New()
	if err := r.Register("spy", spy); err != nil {
		t.Fatal(err)
	}
	if _, ok := r.Lookup("spy"); !ok {
		t.Fatal("Lookup(spy) = false right after Register")
	}
	if n := setups.Load(); n != 0 {
		t.Fatalf("the spy capability ran %d times during Register+Lookup; "+
			"registration and lookup must not invoke anything", n)
	}
	h, _ := r.Lookup("spy")
	if _, err := h(context.Background(), nil); err != nil {
		t.Fatal(err)
	}
	if n := setups.Load(); n != 1 {
		t.Fatalf("after one invocation the counter is %d; want 1 -- otherwise the previous "+
			"assertion is vacuous because the spy never runs at all", n)
	}
}

// runsAtStartup and goroutinesAtStartup are read in TestMain, BEFORE any test
// in this package has run.
//
// They have to be: package init builds the real registry, and
// integration_test.go dispatches a real inventory.collect job -- Go runs a
// package's tests in source order, so a dormancy assertion made inside a test
// function would be reading a counter some earlier test had already moved. That
// is the "passes because something else happened" shape this module has been
// bitten by, and it is why the observation is taken at the only moment that can
// answer the question.
var (
	runsAtStartup       int64
	siteRunsAtStartup   int64
	goroutinesAtStartup int
)

func TestMain(m *testing.M) {
	// Touching the registry is what forces package init if it has not already
	// happened. Nothing else here runs anything.
	_ = capabilities.Default().Names()
	runsAtStartup = inventory.Runs()
	siteRunsAtStartup = site.Process.Runs()
	goroutinesAtStartup = runtime.NumGoroutine()
	os.Exit(m.Run())
}

func TestRegisteringSiteWakeOpensNoSocketAndRunsNothing(t *testing.T) {
	// `site.wake` is the capability whose whole safety story is "it does nothing
	// until a signed job arrives, and then it emits one datagram and stops". The
	// registration half of that is asserted here, against the REAL
	// site.Process rather than a spy, at the only moment that can answer the
	// question -- before any test in this package has run.
	//
	// FALSIFYING OBSERVATION: a non-zero run count once the registry has been
	// built. The socket/goroutine/timer-wheel half is asserted in
	// internal/capabilities/site (TestNothingIsOpenedBeforeAJobArrivesAndEverythingIsReleasedAfter,
	// TestTheCapabilityAddsNothingToTheTimerWheel), where a wake can be driven
	// through a spy without putting a datagram on anyone's wire.
	if siteRunsAtStartup != 0 {
		t.Fatalf("site.wake had already run %d times when the registry finished building; "+
			"CLAUDE.md: capabilities 'only allocate/initialize when a job for them actually runs'",
			siteRunsAtStartup)
	}
	if _, ok := capabilities.Default().Lookup(site.JobType); !ok {
		t.Fatalf("%s is not registered; every wake the control plane dispatches would be "+
			"refused unknown_type", site.JobType)
	}
}

func TestTheInventoryCollectorIsNotTouchedUntilAJobArrives(t *testing.T) {
	// The dormancy assertion against the REAL capability rather than a spy.
	//
	// FALSIFIER: a non-zero count after the registry has been built and before
	// any job has arrived. Observable: the count.
	if runsAtStartup != 0 {
		t.Fatalf("the inventory collector had already run %d times when the registry finished "+
			"building; CLAUDE.md: heavy capabilities 'only allocate/initialize when a job for "+
			"them actually runs'", runsAtStartup)
	}
	// And it is not vacuous: by now integration_test.go has dispatched a real
	// signed inventory.collect job, so the counter CAN move and did.
	if inventory.Runs() == 0 {
		t.Fatalf("the collector never ran in this package at all; the assertion above is vacuous")
	}
	if goroutinesAtStartup > 8 {
		t.Fatalf("%d goroutines once the registry was built; a capability started a poller at init",
			goroutinesAtStartup)
	}
}

func TestLookupAllocatesNothing(t *testing.T) {
	// Lookup is on the hot path of every inbound job frame. It is also the only
	// part of this package that runs when the agent is otherwise idle.
	//
	// FALSIFIER: AllocsPerOp > 0.
	r := budget.AssertNoAllocations(t, "capability lookup", func() func() {
		reg := capabilities.Default()
		return func() {
			if _, ok := reg.Lookup("inventory.collect"); !ok {
				panic("inventory.collect vanished from the registry")
			}
			if _, ok := reg.Lookup(unregisteredType); ok {
				panic(unregisteredType + " appeared in the registry")
			}
		}
	})
	budget.AssertNoGoroutineGrowth(t, r, 0)
}

func noop(context.Context, jobs.VerifiedJob) (any, error) { return map[string]any{}, nil }

// ---------------------------------------------------------------------------
// HeavyKeys integrity: jobs.DefaultHeavyKeys is keyed on bare capability
// prefixes -- {backup, patch, app, script} as shipped -- not full job types. A
// typo, or a capability later renamed or removed, would silently un-cap a
// heavy capability: no error, no test failure, just a resource guard that
// quietly stopped guarding.
//
// "deploy" is NOT a heavy key and never was one that matched: deploy's job
// types are app.deploy and script.run, and capabilityKey splits on the first
// '.', so the entry this test found named nothing. It is named here only
// because a reader looking for it deserves to be told where it went.
//
// WHAT THIS TEST CANNOT DO: catch an ADDITION. It iterates DefaultHeavyKeys
// into the registry, so it sees a key that matches nothing (a typo, a removal)
// and is blind to a heavy capability registered later and never added to the
// map. Catching that would need per-capability heaviness declarations, which
// the design deliberately rejected (§1.4) -- so the addition case is a review
// obligation, not a tested one.
// ---------------------------------------------------------------------------

func TestEveryHeavyKeyNamesARegisteredCapability(t *testing.T) {
	registered := capabilities.Types()
	for key := range jobs.DefaultHeavyKeys() {
		found := false
		for _, jt := range registered {
			if strings.HasPrefix(jt, key+".") || jt == key {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("HeavyKeys names %q, which no registered job type matches; "+
				"a typo here silently un-caps a heavy capability", key)
		}
	}
}

// TestFalsifierF1RegisteringScreenShareIsDormant is spec-screen-share.md §4.2
// F1: registering remote.screen.open allocates nothing on the lookup path,
// starts no goroutine and runs nothing. It registers the real handler into a
// fresh registry, so it runs on every OS, not only where the default registry
// carries it.
func TestFalsifierF1RegisteringScreenShareIsDormant(t *testing.T) {
	before := runtime.NumGoroutine()
	r := capabilities.New()
	if err := r.Register(remote.JobType, remote.Process.Open); err != nil {
		t.Fatal(err)
	}
	res := budget.AssertNoAllocations(t, "remote.screen.open dormant", func() func() {
		return func() {
			if _, ok := r.Lookup(remote.JobType); !ok {
				panic("remote.screen.open vanished")
			}
		}
	})
	budget.AssertNoGoroutineGrowth(t, res, 0)
	if n := runtime.NumGoroutine(); n > before {
		t.Fatalf("F1 falsified: %d goroutines after registering, %d before", n, before)
	}
	if _, ok := capabilities.Default().Lookup(remote.JobType); ok != remote.Supported {
		t.Fatalf("remote.screen.open registered=%v on %s; want %v", ok, runtime.GOOS, remote.Supported)
	}
	if jobs.DefaultHeavyKeys()["remote"] {
		t.Fatal("a screen session would hold a heavy slot for hours and starve patch and backup")
	}
	t.Logf("F1 budget: remote.screen.open lookup %d allocs/op; goroutines %d -> %d",
		res.AllocsPerOp, before, runtime.NumGoroutine())
}
