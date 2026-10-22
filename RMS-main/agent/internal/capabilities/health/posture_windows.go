//go:build windows

package health

// The posture sampler on Windows: BitLocker, Windows Security Center, the
// Windows firewall and SMART, read through in-process COM.
//
// # THIS FILE HAS NEVER EXECUTED
//
// No Windows machine exists in this project. What checks it, exactly:
// `GOOS=windows go vet ./...`, which type-checks this package's windows-tagged
// files INCLUDING its _test.go files, and a windows/amd64 and windows/arm64
// `go build` of the whole agent. Both run in the `agent` CI job. (CI's
// `GOOS=windows go test -c` covers `./internal/service` only; the equivalent
// for this package, `GOOS=windows go test -c ./internal/capabilities/health/`,
// is a local command, and `go vet` is what covers these files on every push.)
// All of it proves the file TYPE-CHECKS and nothing more. A wrong namespace
// string, a wrong member name, or a misread out-parameter compiles cleanly
// here and fails on a customer's machine.
//
// What those checks DO catch: a symbol that does not exist in
// `golang.org/x/sys/windows`, a helper called with the wrong number or type of
// arguments, a struct field referenced by a name the struct does not have, an
// unused import. Those are real defects and this file has been through them.
// What they do NOT catch is everything below.
//
// # Why COM and not a shell-out
//
// Not `manage-bde`, not `netsh`, not PowerShell, on any path including ones
// this code does not take. A SYSTEM-privileged process spawning a shell is
// among the loudest behavioural signals an endpoint can emit, and these probes
// run on a schedule, on every endpoint, forever.
//
// The precedent is `docs/modules/backup/agent-engine.md` §2, "Processes this
// capability may spawn", which records what an EDR team was told about BACKUP
// and is scoped to backup -- it is not a fleet-wide promise, and `deploy` runs
// scripts on Windows by design. What is honoured here is its reasoning, not a
// commitment it does not make: a scheduled, fleet-wide, SYSTEM-privileged probe
// is the worst possible shape for a shell-out, and unlike `deploy` it has an
// in-process alternative. This file spawns nothing.
//
// That claim is mechanical rather than editorial, and is checked as one:
// grepping this file for the standard library's process-spawning package, or
// for any call into it, returns NOTHING -- including from this comment, which
// is why it names neither string. The import block below is the other half of
// the check, and the sampler is constructed with `noProcessRunner` instead of
// `execPostureRunner`, so even a future probe that reached for the injected
// runner would get "not installed" back rather than a child process.
//
// # This file is READ-ONLY against the machine, by construction
//
// Every COM call it makes is a property read or a query. The only DISPATCH
// flags it ever passes are the GET and METHOD ones; the put-property flag is
// neither declared nor named anywhere below, so there is no spelling here that
// assigns a property, and a grep for it comes back empty. That matters more
// than it looks:
// `INetFwPolicy2` exposes `put_FirewallEnabled` one vtable slot after
// `get_FirewallEnabled`, and a health probe that turned a customer's firewall
// profile on or off as a side effect of an off-by-one would be the worst
// outcome this file could produce. See the deviation note below.
//
// # DELIBERATE DEVIATION FROM THE PLAN: late binding, not vtable offsets
//
// The plan for this task specified `IWbemLocator` (CLSID
// {4590F811-1D3A-11D0-891F-00AA004B2E24}, IID {DC12A687-737F-11CF-884D-00AA004B2E24})
// -> ConnectServer -> IWbemServices::ExecQuery -> IEnumWbemClassObject::Next ->
// IWbemClassObject::Get, and `INetFwPolicy2` (IID
// {98325047-C671-4174-8D81-DEFCD3F03186}) -> get_FirewallEnabled. Every
// NAMESPACE, CLASS, PROPERTY and RULE it specified is implemented exactly as
// written. The ACCESS MECHANISM is not: this file reaches the same objects
// through IDispatch late binding -- `WbemScripting.SWbemLocator` for the three
// WMI namespaces and `HNetCfg.FwPolicy2` (which derives from IDispatch) for the
// firewall -- and declares no custom vtable but IDispatch's own seven slots.
//
// Three reasons, in order of weight:
//
//  1. **The repository already decided this.** `patch/com_windows.go` §Scope:
//     "Late binding through IDispatch only ... a wrong vtable index is a silent
//     call into a different method with the wrong arguments, on a machine
//     nobody here can debug, running as SYSTEM. A wrong METHOD NAME through
//     IDispatch is a clean DISP_E_UNKNOWNNAME at runtime with the name in the
//     message." That is this situation exactly, in the same SYSTEM process.
//  2. **The failure modes are asymmetric.** A wrong member NAME here produces a
//     named error and therefore `unavailable`, which is a visible, countable
//     state that never opens or clears an alert (§0.7). A wrong vtable INDEX
//     produces a call into a different method, and for `INetFwPolicy2`
//     specifically the neighbouring slot MUTATES the firewall.
//  3. **It is not cheaper the other way.** `IWbemClassObject::Get` returns a
//     VARIANT and `get_FirewallEnabled` returns a VARIANT_BOOL, so the variant
//     and BSTR machinery below is owed either way; the vtable route would have
//     added five hand-transcribed interface layouts (IWbemLocator,
//     IWbemServices' 26 slots, IEnumWbemClassObject, IWbemClassObject's 27
//     slots, INetFwPolicy2's 29) on top of it, none of them checkable here.
//     `snapshot_windows.go` calls its one such struct "the load-bearing claim
//     in this file" and "the file's single point of failure"; this file has no
//     equivalent.
//
// The corroboration for the SWbem route is documentation-grade and no better:
// it is the shape `github.com/StackExchange/wmi` -- the most widely deployed Go
// WMI client -- uses (CreateObject "WbemScripting.SWbemLocator", ConnectServer,
// ExecQuery, Count, ItemIndex, then property reads by name). That is a reason
// to believe the sequence exists, not evidence that this transcription of it is
// right.
//
// If a real Windows run shows the scripting objects unusable in this process,
// the plan's `IWbemLocator` route is the fallback, and it is a rewrite of
// `wmiQueryColumn` and nothing else -- the four probes are written against that
// helper, not against COM.
//
// # COULD NOT BE CONFIRMED
//
// Each item below would be invisible until a real Windows run. This list is the
// deliverable a future reader on real hardware works from; it ships inline
// because `.git/` does not survive a clone.
//
//  1. **That `WbemScripting.SWbemLocator` is reachable at all from this
//     process** -- a SYSTEM service host, in an STA it entered itself -- and
//     that `ConnectServer`, `ExecQuery`, `Count`, `ItemIndex` and `ExecMethod_`
//     carry the argument order and types used here.
//  2. **That WMI properties are readable off an `SWbemObject` by their own
//     name** through DISPATCH_PROPERTYGET (`productState`, `PredictFailure`,
//     `ProtectionStatus`). If they are not, the fallback is `Properties_.Item(
//     name).Value`, three more late-bound calls in `wmiQueryColumn`.
//  3. **That `Win32_EncryptableVolume.GetProtectionStatus` returns
//     ProtectionStatus 1 for a BitLocker-protected volume on every supported
//     Windows build**, and that a `ReturnValue` of 0 is the only success. 2
//     ("unknown") is treated as `unavailable` here on the reading that it means
//     the status could not be determined; that reading is not confirmed.
//  4. **That the system drive letter from `GetSystemDirectory` is the right
//     volume to ask about** on a machine that boots from a non-C: volume, and
//     that `Win32_EncryptableVolume.DriveLetter` is spelled `C:` (no trailing
//     separator) so the WQL equality matches.
//  5. **That `productState`'s enabled/up-to-date bytes are where `avHealthy`
//     reads them.** Microsoft has never formally documented that bitfield. This
//     is the single most likely wrong thing in the file, which is why anything
//     outside the observed provider range is not interpreted at all. The shifts
//     now read the 0xPPSSUU folklore layout (scanner byte for enabled,
//     definition byte for out-of-date) after an earlier version was shipped
//     four bits high and flagged as suspect here; that earlier reading returned
//     a definite `false` -- an alert -- on every healthy Windows endpoint. The
//     corrected reading is STILL UNVERIFIED against real hardware. The worked
//     example and the one-machine falsifier are written out at avHealthy. Read
//     that before trusting this check either way.
//  6. **That "any healthy product means healthy" is the right aggregation**
//     when Security Center reports several AntiVirusProduct instances. Chosen
//     because Defender registers and reports itself disabled while a third-party
//     product is active, so requiring all of them would alarm on a correctly
//     protected machine. Never observed.
//  7. **That `INetFwPolicy2` answers `FirewallEnabled` for all three profile
//     types on a domain-joined machine**, and that the value arrives as VT_BOOL
//     rather than something this code declines to interpret.
//  8. **That `MSStorageDriver_FailurePredictStatus` exists in `root\WMI` on
//     machines whose storage driver implements it.** A driver that does not may
//     simply return no instances -- treated here as `unavailable`, never as
//     "no failure predicted".
//  9. **That COM initialisation succeeds inside the agent's service host**
//     without a prior apartment set by another capability. `patch` enters an
//     STA on its own locked thread and leaves it; `backup` enters an MTA on its
//     own. If a thread ever arrives here already in an MTA, CoInitializeEx
//     returns RPC_E_CHANGED_MODE, which this file treats as a failure ->
//     `unavailable`, and does NOT balance with CoUninitialize.
//  10. **That WMI works here without `CoInitializeSecurity`.** It is NOT called,
//     for `snapshot_windows.go`'s reason: it is PROCESS-WIDE, and a health probe
//     that reset process-wide COM security as a side effect of a tick would be
//     reaching outside its own module. The scripting API is documented to manage
//     its own proxy security; if a real run returns E_ACCESSDENIED from
//     ConnectServer or ExecQuery, this is the first suspect, and the fix is
//     per-proxy (`SWbemServices.Security_`), never process-wide.
//  11. **The IDispatch vtable itself** -- QueryInterface, AddRef, Release,
//     GetTypeInfoCount, GetTypeInfo, GetIDsOfNames, Invoke. It is the one
//     layout this file asserts. It is fixed by IDispatch's definition and is
//     shared with `patch/com_windows.go`, so it is the best-corroborated claim
//     here, but it is still a claim no test in this repository checks. Slots 0
//     (QueryInterface), 2 (Release), 5 (GetIDsOfNames) and 6 (Invoke) are the
//     ones actually called. Slot 0 is called on ONE path only -- a variant that
//     comes back VT_UNKNOWN, which the scripting objects are not expected to
//     produce -- and it is called because the alternative, casting an IUnknown
//     to an IDispatch and dispatching through slot 5 of a three-slot vtable, is
//     the wild call this whole file is arranged to make impossible. See
//     getObject.
//
// # What it holds between ticks: nothing
//
// Every probe opens its own apartment, creates its objects, releases them and
// uninitialises before returning. Nothing COM-allocated, and no locked OS
// thread, outlives a `Sample` call. That is what keeps four COM probes off the
// agent's idle footprint: they cost nothing at all between posture sub-ticks.
// The cost is four CoInitializeEx/CoUninitialize pairs per posture pass, which
// buys the property that a stuck or leaking probe cannot become resident.
//
// # What the job context cannot do here
//
// COM calls are synchronous and take no cancellation token, so the posture
// deadline in `posture.go` does not reach inside one. The `ctx` argument is
// accepted to satisfy `probeFunc` and is deliberately unused. The overlap guard
// in `postureSampler.Sample` is what keeps a slow pass from being entered
// twice.

import (
	"context"
	"errors"
	"fmt"
	"runtime"
	"strings"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

// ---------------------------------------------------------------------------
// What is read, named once
// ---------------------------------------------------------------------------

const (
	// WMI namespaces. Backslashed, which is the documented spelling.
	nsBitLocker      = `root\CIMV2\Security\MicrosoftVolumeEncryption`
	nsSecurityCenter = `root\SecurityCenter2`
	nsStorageDriver  = `root\WMI`

	// ProgIDs rather than hard-coded CLSIDs, per `patch/com_windows.go`: a
	// mistyped CLSID is a 16-byte constant nobody can review, while a mistyped
	// ProgID is a readable string that fails with its own name in the message.
	// Both are compile-time constants in this package; nothing from a policy or
	// a job ever reaches createObject.
	progIDWbemLocator = "WbemScripting.SWbemLocator"
	progIDFwPolicy2   = "HNetCfg.FwPolicy2"

	// NET_FW_PROFILE_TYPE2. ALL THREE must be enabled for `true` (design §4):
	// a machine whose public profile is off is not firewalled, whatever the
	// profile it happens to be on right now says.
	netFwProfileDomain  = 1
	netFwProfilePrivate = 2
	netFwProfilePublic  = 4

	// maxWMIInstances bounds one enumeration. A WMI class that returns thousands
	// of instances must cost a fixed amount here, not the agent's residency
	// claim. No posture class in this file has more than a handful of instances
	// on a real machine; a query that hits this cap is itself a signal that the
	// query is not the one this code thinks it is.
	maxWMIInstances = 64
)

// errNoProcessOnWindows is what the injected runner returns. See
// noProcessRunner.
var errNoProcessOnWindows = errors.New("health: the Windows posture probes spawn no processes")

// noProcessRunner is the postureRunner these probes are constructed with, and
// it runs nothing.
//
// The probes below ignore the runner entirely -- they are COM, not shell-outs --
// so the constructor could have been handed `execPostureRunner` and nothing
// would change today. This exists so that it stays true tomorrow: a probe added
// later that reaches for `run` gets `NotFound` back, reports `unavailable`, and
// still spawns nothing. The EDR promise in the header is then a property of the
// wiring rather than of each future author's memory.
func noProcessRunner(context.Context, string, ...string) postureResult {
	return postureResult{ExitCode: -1, NotFound: true, Err: errNoProcessOnWindows}
}

// newPlatformPosture returns the Windows posture sampler: all four checks,
// unlike Linux (no AV) and macOS (no AV, no SMART).
func newPlatformPosture() Sampler {
	return newPostureSampler(noProcessRunner, map[string]probeFunc{
		"posture.disk_encryption":   probeDiskEncryptionWindows,
		"posture.firewall_enabled":  probeFirewallWindows,
		"posture.antivirus_healthy": probeAntivirusWindows,
		"disk.smart_ok":             probeSMARTWindows,
	})
}

// ---------------------------------------------------------------------------
// The four probes
// ---------------------------------------------------------------------------

// probeDiskEncryptionWindows answers for the SYSTEM volume only, which is the
// same scope the Linux and macOS probes take (design §2.3): a per-volume answer
// needs its own check type with a selector, and §0.1 makes that a schema change.
//
// It asks `Win32_EncryptableVolume` about the drive letter the system directory
// is on, and reads GetProtectionStatus's out-parameter: 1 means protected, 0
// means not. Anything else -- including 2, "unknown" -- is `unavailable`.
func probeDiskEncryptionWindows(_ context.Context, _ postureRunner, _ string) Sample {
	sess, err := newCOMSession()
	if err != nil {
		return unavailable()
	}
	defer sess.close()

	drive, ok := systemDriveLetter()
	if !ok {
		return unavailable()
	}
	status, ok := bitLockerProtectionStatus(drive)
	if !ok {
		return unavailable()
	}
	switch status {
	case 0: // VOLUME_UNPROTECTED
		return boolSample(false)
	case 1: // VOLUME_PROTECTED
		return boolSample(true)
	}
	// 2 is documented as "the volume protection status cannot be determined",
	// and an undocumented fourth value would land here too. Reporting `false`
	// for either would be inferring "not encrypted" from "cannot tell", which
	// §5 forbids in both directions.
	return unavailable()
}

// probeAntivirusWindows reads Security Center's registered products.
//
// The aggregation is ANY-HEALTHY, and it is a judgement call recorded in the
// header: Windows Defender registers itself here and reports DISABLED while a
// third-party product is running, so requiring every instance to be healthy
// would report `false` on a correctly protected machine. `false` therefore
// means "every product whose state this code could interpret is unhealthy".
//
// A product whose `productState` is outside the attested range contributes
// NOTHING -- not a `false`, not a `true`. If no product's state can be
// interpreted, the answer is `unavailable`.
func probeAntivirusWindows(_ context.Context, _ postureRunner, _ string) Sample {
	sess, err := newCOMSession()
	if err != nil {
		return unavailable()
	}
	defer sess.close()

	values, ok := wmiQueryColumn(nsSecurityCenter,
		"SELECT productState FROM AntiVirusProduct", "productState")
	if !ok {
		return unavailable()
	}
	known, healthy := false, false
	for _, v := range values {
		state, ok := asUint32(v)
		if !ok {
			continue
		}
		h, k := avHealthy(state)
		if !k {
			continue
		}
		known = true
		if h {
			healthy = true
		}
	}
	if !known {
		return unavailable()
	}
	return boolSample(healthy)
}

// probeFirewallWindows requires ALL THREE profiles to be enabled for `true`.
//
// One profile reporting disabled is a definite `false` and returns immediately;
// a profile that cannot be read at all abandons the whole answer, because
// "two of three are on" is not something this check can report.
func probeFirewallWindows(_ context.Context, _ postureRunner, _ string) Sample {
	sess, err := newCOMSession()
	if err != nil {
		return unavailable()
	}
	defer sess.close()

	policy, err := createObject(progIDFwPolicy2)
	if err != nil {
		return unavailable()
	}
	defer policy.release()

	for _, profile := range [...]int32{netFwProfileDomain, netFwProfilePrivate, netFwProfilePublic} {
		// FirewallEnabled is a PARAMETERISED PROPERTY: the profile type is the
		// index. Read flags only, never the put flag -- see the header on
		// why the absence of a put spelling in this file is load-bearing.
		v, err := policy.invoke("FirewallEnabled", dispatchPropertyGet|dispatchMethod, profile)
		if err != nil {
			return unavailable()
		}
		on, ok := variantBool(v)
		clearVariant(&v)
		if !ok {
			return unavailable()
		}
		if !on {
			return boolSample(false)
		}
	}
	return boolSample(true)
}

// probeSMARTWindows aggregates over every drive whose storage driver answers
// `MSStorageDriver_FailurePredictStatus`. `PredictFailure` true means the drive
// predicts its own failure, so `disk.smart_ok` is the NEGATION of it.
//
// The aggregation rule is the Linux one, restated because the obvious rule is
// wrong: `false` if ANY readable drive predicts failure; `true` only if AT
// LEAST ONE drive was read and none predicted failure; `unavailable` if no
// drive could be read. "true when no failure was seen" would report a healthy
// machine on one where WMI answered nothing at all.
//
// A SELECTOR OTHER THAN `*` IS REFUSED rather than ignored. On Linux the
// selector is a device path (`/dev/sda`); there is no such spelling here, and
// silently answering about every drive when the policy asked about one is
// answering a different question. `unavailable` is visible and countable;
// a wrong answer is not.
func probeSMARTWindows(_ context.Context, _ postureRunner, selector string) Sample {
	if s := strings.TrimSpace(selector); s != "" && !selectorIsWildcard(selector) {
		return unavailable()
	}
	sess, err := newCOMSession()
	if err != nil {
		return unavailable()
	}
	defer sess.close()

	values, ok := wmiQueryColumn(nsStorageDriver,
		"SELECT PredictFailure FROM MSStorageDriver_FailurePredictStatus", "PredictFailure")
	if !ok {
		return unavailable()
	}
	read, failing := 0, false
	for _, v := range values {
		predicts, ok := asBool(v)
		if !ok {
			continue
		}
		read++
		if predicts {
			failing = true
		}
	}
	if read == 0 {
		return unavailable()
	}
	return boolSample(!failing)
}

// avHealthy interprets Security Center's productState.
//
// productState is a bitfield Microsoft has never formally documented. The
// layout this function assumes is the widely-circulated (unofficial) one: a
// three-byte value 0xPPSSUU, where PP is the provider, SS is the
// real-time-scanner byte (0x10 or 0x11 = ON), and UU is the definitions byte
// (0x00 = up to date, 0x10 = out of date):
//
//	(state >> 8) & 0x10 != 0  -> real-time protection enabled  (scanner byte)
//	 state       & 0x10 != 0  -> definitions OUT OF DATE        (definition byte)
//
// A value whose provider byte is outside the observed range is NOT
// interpreted: §4.1 warns that "AV health" invented from an unclear source
// "produces a check that is confidently wrong", and a confidently wrong
// `true` CLEARS an alert.
//
// # THIS BITFIELD IS FOLKLORE, AND SO IS THIS FIX
//
// Microsoft has never published productState's layout. Both the shifts above
// AND the previous version of this function (`(state>>12)&0x10` for enabled,
// `(state>>8)&0x10` for out-of-date -- four bits higher on each field) are
// folklore lifted from third-party write-ups; NEITHER has been verified
// against a real machine by this project. The previous version was shipped
// verbatim from this task's plan, flagged as suspected wrong by four bits;
// this version corrects it to the 0xPPSSUU reading, which is still unattested
// by this codebase and must be treated as a hypothesis, not a confirmed fact.
//
// The worked example that shows the difference is 0x061100 (397568), the
// value most commonly reported for a healthy Windows Defender (PP=0x06,
// SS=0x11, UU=0x00):
//
//	this version:    enabled   = (0x061100>>8)&0x10 = 0x0611&0x10 = 0x10 -> enabled
//	                 outOfDate = 0x061100&0x10      = 0             -> up to date
//	                 => healthy = true, known = true
//	previous version: enabled  = (0x061100>>12)&0x10 = 0x061&0x10 = 0 -> NOT enabled
//	                 => healthy = false, known = true
//
// The previous version returned a DEFINITE `false` on a correctly protected
// machine -- an alert on every healthy Windows endpoint, not a cleared one.
// That was the safer of the two directions to be wrong in (§5's asymmetry is
// about not clearing alerts), which is why it was tolerable to ship while
// flagged. This version is believed correct, but "believed" is doing the
// work in that sentence until the falsifier below has actually been run.
//
// THE FALSIFIER, and it is cheap: on one real Windows machine with Defender
// running and current, read `productState` from `root\SecurityCenter2` and
// confirm `avHealthy` returns `(true, true)`. With real-time protection
// disabled it must return `(false, true)`. With a value outside the assumed
// layout (guard below) it must return `known == false`. Until that has run on
// real hardware, this reading is unverified and header item 5 stands.
func avHealthy(state uint32) (healthy bool, known bool) {
	if state == 0 || state>>20 != 0 {
		return false, false
	}
	enabled := (state>>8)&0x10 != 0
	outOfDate := state&0x10 != 0
	return enabled && !outOfDate, true
}

// systemDriveLetter returns the drive the system directory is on, as `C:` --
// no trailing separator, which is the form Win32_EncryptableVolume.DriveLetter
// is documented to carry.
//
// NOT hard-coded to `C:`: a machine can boot from another volume, and this
// check would then be answering about a drive nobody asked about. Whether the
// SYSTEM DIRECTORY's volume is the right one to ask BitLocker about on such a
// machine is item 4 in the header's unconfirmed list.
//
// The returned value is interpolated into WQL below, so it is validated to
// exactly one ASCII letter and a colon here. It originates from the OS and
// never from a policy, but a query string assembled from anything at all is a
// place to state the constraint rather than assume it.
func systemDriveLetter() (string, bool) {
	dir, err := windows.GetSystemDirectory()
	if err != nil || len(dir) < 2 || dir[1] != ':' {
		return "", false
	}
	c := dir[0]
	if (c < 'A' || c > 'Z') && (c < 'a' || c > 'z') {
		return "", false
	}
	return strings.ToUpper(dir[:2]), true
}

// bitLockerProtectionStatus returns the raw ProtectionStatus for one drive.
//
// PRIMARY PATH: the documented METHOD, `GetProtectionStatus`, which is what the
// plan for this task specified. FALLBACK: `ProtectionStatus` is also documented
// as a PROPERTY of the same class, and the property read needs neither
// ExecMethod_ nor an out-parameters object.
//
// The fallback is a second chance at an answer, not a second opinion -- both
// come from the same WMI provider, so a machine that answers both cannot answer
// them differently in any way this code would notice. If a real Windows run
// shows the method path failing while the property works, swapping the two
// calls below is the whole fix.
//
// `drive` is validated by systemDriveLetter to `[A-Za-z]:` before it reaches
// the WQL below.
func bitLockerProtectionStatus(drive string) (uint32, bool) {
	if status, ok := bitLockerViaMethod(drive); ok {
		return status, true
	}
	value, ok := wmiQueryScalar(nsBitLocker,
		fmt.Sprintf("SELECT ProtectionStatus FROM Win32_EncryptableVolume WHERE DriveLetter = '%s'", drive),
		"ProtectionStatus")
	if !ok {
		return 0, false
	}
	return asUint32(value)
}

// bitLockerViaMethod calls GetProtectionStatus on the system volume and reads
// its out-parameters.
//
// A non-zero `ReturnValue` is NOT trusted for its ProtectionStatus: a method
// that reported failure has made no positive statement about anything, and the
// out-parameter it left behind is whatever the provider happened to write.
func bitLockerViaMethod(drive string) (uint32, bool) {
	services, err := wmiConnect(nsBitLocker)
	if err != nil {
		return 0, false
	}
	defer services.release()

	volume, ok := firstInstance(services,
		fmt.Sprintf("SELECT * FROM Win32_EncryptableVolume WHERE DriveLetter = '%s'", drive))
	if !ok {
		// No instance for the system drive. On a machine without the BitLocker
		// feature the class itself is absent and ExecQuery fails; both land
		// here, and both are `unavailable` rather than "not encrypted", because
		// neither is a statement about the volume.
		return 0, false
	}
	defer volume.release()

	out, err := volume.getObject("ExecMethod_", "GetProtectionStatus")
	if err != nil {
		return 0, false
	}
	defer out.release()

	if ret, err := out.getInt("ReturnValue"); err != nil || ret != 0 {
		return 0, false
	}
	status, err := out.getInt("ProtectionStatus")
	if err != nil || status < 0 {
		return 0, false
	}
	return uint32(status), true
}

// ---------------------------------------------------------------------------
// WMI, through the scripting objects
// ---------------------------------------------------------------------------

// wmiConnect returns the SWbemServices for one namespace on THIS machine.
//
// The caller releases it. `.` is the local machine and is a compile-time
// constant: this agent never queries WMI on another host, and a remote WMI call
// out of an RMM agent would be a lateral-movement shape whatever it was for.
func wmiConnect(namespace string) (*iDispatch, error) {
	locator, err := createObject(progIDWbemLocator)
	if err != nil {
		return nil, err
	}
	defer locator.release()
	return locator.getObject("ConnectServer", ".", namespace)
}

// wmiQueryColumn runs one WQL query and returns one property from every
// instance it returned.
//
// `ok` is false on ANY failure and on zero instances -- there is no partial
// answer, because every caller turns an absent value into `unavailable`
// anyway. Instances whose property is missing or of a type this code does not
// interpret are DROPPED rather than defaulted: a missing `productState` must
// not read as 0.
func wmiQueryColumn(namespace, wql, property string) ([]any, bool) {
	services, err := wmiConnect(namespace)
	if err != nil {
		return nil, false
	}
	defer services.release()

	set, err := services.getObject("ExecQuery", wql)
	if err != nil {
		return nil, false
	}
	defer set.release()

	count, err := set.getInt("Count")
	if err != nil || count <= 0 {
		return nil, false
	}
	if count > maxWMIInstances {
		count = maxWMIInstances
	}

	out := make([]any, 0, count)
	for i := int32(0); i < count; i++ {
		item, err := set.getObject("ItemIndex", i)
		if err != nil {
			continue
		}
		v, err := item.invoke(property, dispatchPropertyGet)
		if err == nil {
			if value, ok := variantValue(v); ok {
				out = append(out, value)
			}
			clearVariant(&v)
		}
		item.release()
	}
	return out, len(out) > 0
}

// wmiQueryScalar is wmiQueryColumn for a query expected to match one instance.
//
// It is the shape the plan for this task named. The column form is the one the
// AV and SMART probes need -- Security Center registers several products, and a
// machine has several disks -- so this is the wrapper rather than the other way
// round.
func wmiQueryScalar(namespace, wql, property string) (any, bool) {
	values, ok := wmiQueryColumn(namespace, wql, property)
	if !ok || len(values) == 0 {
		return nil, false
	}
	return values[0], true
}

// firstInstance returns the first object a query matched. The caller releases
// it.
func firstInstance(services *iDispatch, wql string) (*iDispatch, bool) {
	set, err := services.getObject("ExecQuery", wql)
	if err != nil {
		return nil, false
	}
	defer set.release()

	count, err := set.getInt("Count")
	if err != nil || count <= 0 {
		return nil, false
	}
	item, err := set.getObject("ItemIndex", int32(0))
	if err != nil {
		return nil, false
	}
	return item, true
}

// variantValue converts a VARIANT this code is willing to interpret into a Go
// value. An unrecognised VT is NOT coerced: `ok=false` becomes `unavailable`
// upstream, which is the entire asymmetry rule of §5 expressed once, here.
func variantValue(v variant) (any, bool) {
	switch v.vt {
	case vtBool:
		return v.val != 0, true
	case vtI2:
		return int64(int16(v.val)), true
	case vtI4, vtInt:
		return int64(int32(v.val)), true
	case vtI8:
		return v.val, true
	case vtUI1:
		return int64(uint8(v.val)), true
	case vtUI2:
		return int64(uint16(v.val)), true
	case vtUI4, vtUInt:
		return int64(uint32(v.val)), true
	case vtBSTR:
		return bstrToString((*uint16)(comPtr(v.val))), true
	}
	return nil, false
}

// asUint32 reads a value wmiQueryColumn produced as an unsigned 32-bit number.
//
// A negative integer is REFUSED rather than wrapped: `productState` is a
// uint32, and a negative value here would mean the variant was not what this
// code thinks it was. Wrapping it would manufacture a bit pattern avHealthy
// would then interpret.
func asUint32(v any) (uint32, bool) {
	switch n := v.(type) {
	case int64:
		if n < 0 || n > 0xFFFFFFFF {
			return 0, false
		}
		return uint32(n), true
	case string:
		// Security Center providers have been observed returning numeric
		// properties as strings through the scripting layer. UNCONFIRMED, and
		// parsed strictly: anything but a plain decimal number is refused.
		var parsed uint64
		if len(n) == 0 || len(n) > 10 {
			return 0, false
		}
		for i := 0; i < len(n); i++ {
			if n[i] < '0' || n[i] > '9' {
				return 0, false
			}
			parsed = parsed*10 + uint64(n[i]-'0')
		}
		if parsed > 0xFFFFFFFF {
			return 0, false
		}
		return uint32(parsed), true
	}
	return 0, false
}

// asBool reads a value wmiQueryColumn produced as a boolean. ONLY a real
// boolean counts: a number that happens to be 1 is not a positive statement
// that a drive predicts its own failure.
func asBool(v any) (bool, bool) {
	b, ok := v.(bool)
	return b, ok
}

// variantBool reads a VARIANT_BOOL out of a VARIANT, and accepts nothing else.
//
// VARIANT_TRUE is -1 and VARIANT_FALSE is 0, so any non-zero payload is true.
// A VT that is not VT_BOOL is refused: the firewall answer must be a boolean
// the callee actually gave, not an integer this code decided to read as one.
func variantBool(v variant) (bool, bool) {
	if v.vt != vtBool {
		return false, false
	}
	return v.val != 0, true
}

// ---------------------------------------------------------------------------
// The COM apartment
// ---------------------------------------------------------------------------

// comSession is an initialised COM apartment on a locked OS thread.
//
// runtime.LockOSThread is not optional, for `patch/wua_windows.go`'s reason: a
// COM apartment belongs to a THREAD, and Go moves goroutines between threads
// freely, so without the lock the second COM call in a probe could run on a
// thread that never called CoInitializeEx and would fail with
// CO_E_NOTINITIALIZED.
//
// One session per probe, torn down before the probe returns. `backup` keeps a
// COM thread alive for the length of a snapshot because its work spans two
// entry points; a posture probe does not, and holding an apartment between
// ticks would be resident cost for nothing.
type comSession struct {
	locked      bool
	initialized bool
}

func newCOMSession() (*comSession, error) {
	s := &comSession{}
	runtime.LockOSThread()
	s.locked = true
	if err := comInitialize(); err != nil {
		s.close()
		return nil, err
	}
	s.initialized = true
	return s, nil
}

// close undoes exactly what was done, and nothing else. CoUninitialize is NOT
// called when CoInitializeEx failed: balancing an initialisation that did not
// happen would decrement another apartment's reference count.
func (s *comSession) close() {
	if s.initialized {
		windows.CoUninitialize()
		s.initialized = false
	}
	if s.locked {
		runtime.UnlockOSThread()
		s.locked = false
	}
}

// comInitialize enters an STA, tolerating an apartment that already exists.
//
// STA rather than the MTA `backup` uses, to match `patch/wua_windows.go`: the
// scripting objects are apartment-agnostic, and a process whose two IDispatch
// users disagreed about the apartment model would be one RPC_E_CHANGED_MODE
// away from a permanently unavailable check on any re-used thread.
//
// S_FALSE (1) means "this thread was already initialised", which is success.
// The generated wrapper reports any non-zero HRESULT as an error, so the
// distinction has to be made here -- and getting it wrong would mean a probe on
// a re-used thread reporting `unavailable` for no reason.
func comInitialize() error {
	err := windows.CoInitializeEx(0, coinitApartmentThreaded|coinitDisableOLE1DDE)
	if err == nil {
		return nil
	}
	var errno syscall.Errno
	if errors.As(err, &errno) && uint32(errno) == 1 { // S_FALSE
		return nil
	}
	// Everything else, including RPC_E_CHANGED_MODE (0x80010106) from a thread
	// already in an MTA, is a failure -> `unavailable`. See header item 9.
	return err
}

// ---------------------------------------------------------------------------
// Minimal IDispatch
// ---------------------------------------------------------------------------
//
// A near-copy of `patch/com_windows.go`, which is unexported in its own
// package. Duplicated rather than shared, per the convention stated at
// internal/capabilities/netsweep/netsweep_test.go:1240 -- the same convention
// the postureRunner seam follows.
//
// What is deliberately NOT copied: `toVariant`'s bool and *iDispatch cases,
// `putBool`, `putRef`, and every property-put flag and helper. This file
// reads; it does not assign. See the header.

var (
	modole32 = windows.NewLazySystemDLL("ole32.dll")

	procCoCreateInstance = modole32.NewProc("CoCreateInstance")
	procCLSIDFromProgID  = modole32.NewProc("CLSIDFromProgID")

	modoleaut32 = windows.NewLazySystemDLL("oleaut32.dll")

	procSysAllocString = modoleaut32.NewProc("SysAllocString")
	procSysFreeString  = modoleaut32.NewProc("SysFreeString")
	procSysStringLen   = modoleaut32.NewProc("SysStringLen")
	procVariantClear   = modoleaut32.NewProc("VariantClear")
)

// COM constants. Named rather than inlined because a bare 0x2 in an Invoke call
// is unreadable and a bare 0x4 where 0x8 belongs is a bug nobody spots.
const (
	clsctxInprocServer = 0x1
	clsctxLocalServer  = 0x4

	coinitApartmentThreaded = 0x2
	coinitDisableOLE1DDE    = 0x4

	dispatchMethod      = 0x1
	dispatchPropertyGet = 0x2

	localeSystemDefault = 0x0800

	vtEmpty    = 0
	vtI2       = 2
	vtI4       = 3
	vtBSTR     = 8
	vtDispatch = 9
	vtBool     = 11
	vtUnknown  = 13
	vtUI1      = 17
	vtUI2      = 18
	vtUI4      = 19
	vtI8       = 20
	vtInt      = 22
	vtUInt     = 23
)

var iidNull = windows.GUID{}

// iidIDispatch. GUIDFromString rather than a hand-written composite literal
// because the byte order of a GUID's last two fields is a classic transcription
// error and this way the compiler is not the only reader.
var iidIDispatch = mustGUID("{00020400-0000-0000-C000-000000000046}")

func mustGUID(s string) windows.GUID {
	g, err := windows.GUIDFromString(s)
	if err != nil {
		// A malformed constant in this file. Panicking at first use is correct:
		// there is no recovery and no configuration involved.
		panic("health: malformed GUID constant " + s)
	}
	return g
}

// iDispatchVtbl is IDispatch's seven slots, in IDispatch's own order. This is
// the ONLY interface layout this file asserts; see header item 11.
type iDispatchVtbl struct {
	QueryInterface   uintptr // 0
	AddRef           uintptr // 1
	Release          uintptr // 2
	GetTypeInfoCount uintptr // 3
	GetTypeInfo      uintptr // 4
	GetIDsOfNames    uintptr // 5
	Invoke           uintptr // 6
}

type iDispatch struct {
	vtbl *iDispatchVtbl
}

// variant is a VARIANT.
//
// 24 bytes on amd64/arm64: an 8-byte header (vt plus three reserved words) and
// a 16-byte union. The trailing padding is NOT decorative -- DECIMAL and
// BRECORD make the union 16 bytes wide, and a 16-byte struct here would have
// VariantClear read past the end of a Go allocation.
type variant struct {
	vt         uint16
	wReserved1 uint16
	wReserved2 uint16
	wReserved3 uint16
	val        int64
	_          [8]byte
}

type dispParams struct {
	rgvarg            *variant
	rgdispidNamedArgs *int32
	cArgs             uint32
	cNamedArgs        uint32
}

type excepInfo struct {
	wCode             uint16
	wReserved         uint16
	bstrSource        *uint16
	bstrDescription   *uint16
	bstrHelpFile      *uint16
	dwHelpContext     uint32
	pvReserved        uintptr
	pfnDeferredFillIn uintptr
	scode             int32
}

func (d *iDispatch) release() {
	if d != nil {
		syscall.SyscallN(d.vtbl.Release, uintptr(unsafe.Pointer(d))) //nolint:errcheck
	}
}

func comFailed(hr uintptr) bool { return int32(hr) < 0 }

// comPtr reinterprets a foreign address as a pointer.
//
// `unsafe.Pointer(uintptr(x))` is what this means, and `go vet` rejects that
// spelling for a reason that does not apply here: a uintptr naming a GO HEAP
// address can go stale between the two conversions if the collector moves the
// object. THESE ARE NOT GO POINTERS. A BSTR comes from SysAllocString and an
// interface pointer from the COM runtime; both live in memory Go's collector
// neither moves nor frees.
//
// Storing them in a Go pointer-typed field instead -- the change that would
// make vet happy the obvious way -- is the ACTUALLY dangerous option: the
// collector would then scan a foreign address as if it owned it.
func comPtr(v int64) unsafe.Pointer {
	u := uintptr(v)
	return *(*unsafe.Pointer)(unsafe.Pointer(&u))
}

// dispID resolves a member name.
//
// This is where a WRONG NAME lands, and it is the reason this file uses late
// binding at all: the error carries the name, and the probe reports
// `unavailable` rather than calling something else.
func (d *iDispatch) dispID(name string) (int32, error) {
	namePtr, err := syscall.UTF16PtrFromString(name)
	if err != nil {
		return 0, err
	}
	var id int32
	hr, _, _ := syscall.SyscallN(
		d.vtbl.GetIDsOfNames,
		uintptr(unsafe.Pointer(d)),
		uintptr(unsafe.Pointer(&iidNull)),
		uintptr(unsafe.Pointer(&namePtr)),
		1,
		localeSystemDefault,
		uintptr(unsafe.Pointer(&id)),
	)
	runtime.KeepAlive(namePtr)
	if comFailed(hr) {
		return 0, fmt.Errorf("the object has no member %q (0x%08x)", name, uint32(hr))
	}
	return id, nil
}

// invoke calls a member.
//
// Arguments are pushed in REVERSE ORDER, which is DISPPARAMS' contract and the
// single easiest thing to get wrong here. A two-argument call with the order
// swapped does not fail; it does something else. (Every call in this file
// passes at most two.)
//
// The caller owns the returned variant and must clear it -- EXCEPT for the
// object case, see getObject.
func (d *iDispatch) invoke(name string, flags uint16, args ...any) (variant, error) {
	var result variant

	id, err := d.dispID(name)
	if err != nil {
		return result, err
	}

	vargs := make([]variant, len(args))
	// Every BSTR allocated for this call is freed on the way out, whatever
	// happens. A leaked BSTR is process-lifetime memory in an agent whose whole
	// claim is that it returns to baseline -- and these probes run on a
	// schedule, forever.
	defer func() {
		for i := range vargs {
			clearVariant(&vargs[i])
		}
	}()
	for i, a := range args {
		v, err := toVariant(a)
		if err != nil {
			return result, err
		}
		// Reverse.
		vargs[len(args)-1-i] = v
	}

	params := dispParams{cArgs: uint32(len(vargs))}
	if len(vargs) > 0 {
		params.rgvarg = &vargs[0]
	}

	var ex excepInfo
	var argErr uint32
	hr, _, _ := syscall.SyscallN(
		d.vtbl.Invoke,
		uintptr(unsafe.Pointer(d)),
		uintptr(id),
		uintptr(unsafe.Pointer(&iidNull)),
		localeSystemDefault,
		uintptr(flags),
		uintptr(unsafe.Pointer(&params)),
		uintptr(unsafe.Pointer(&result)),
		uintptr(unsafe.Pointer(&ex)),
		uintptr(unsafe.Pointer(&argErr)),
	)
	runtime.KeepAlive(vargs)
	if comFailed(hr) {
		// WMI puts the sentence a human can act on into the EXCEPINFO
		// description ("Invalid namespace", "Access denied"). None of it reaches
		// a Sample -- a posture probe answers with a tri-state and no text --
		// but it is read and freed here so the BSTRs are not leaked.
		detail := bstrToString(ex.bstrDescription)
		freeBSTR(ex.bstrSource)
		freeBSTR(ex.bstrDescription)
		freeBSTR(ex.bstrHelpFile)
		if detail != "" {
			return result, fmt.Errorf("%s failed (0x%08x): %s", name, uint32(hr), detail)
		}
		return result, fmt.Errorf("%s failed (0x%08x)", name, uint32(hr))
	}
	return result, nil
}

// toVariant converts the ONLY two argument types this file passes: a string
// (namespace, WQL, method name) and an int32 (a collection index, a firewall
// profile type).
func toVariant(a any) (variant, error) {
	switch v := a.(type) {
	case string:
		bstr, err := allocBSTR(v)
		if err != nil {
			return variant{}, err
		}
		return variant{vt: vtBSTR, val: int64(uintptr(unsafe.Pointer(bstr)))}, nil
	case int32:
		return variant{vt: vtI4, val: int64(v)}, nil
	default:
		return variant{}, fmt.Errorf("health: %T is not a COM argument this agent constructs", a)
	}
}

func clearVariant(v *variant) {
	if v == nil || v.vt == vtEmpty {
		return
	}
	syscall.SyscallN(procVariantClear.Addr(), uintptr(unsafe.Pointer(v))) //nolint:errcheck
	v.vt = vtEmpty
	v.val = 0
}

// getInt reads a numeric property and clears the variant.
func (d *iDispatch) getInt(name string) (int32, error) {
	v, err := d.invoke(name, dispatchPropertyGet)
	if err != nil {
		return 0, err
	}
	defer clearVariant(&v)
	switch v.vt {
	case vtI2:
		return int32(int16(v.val)), nil
	case vtI4, vtInt, vtUI4, vtUInt, vtI8, vtUI1, vtUI2:
		return int32(v.val), nil
	}
	// NOT coerced to 0. A `Count` that came back as something other than a
	// number means the object is not the one this code thinks it is, and a 0
	// would read as "the query matched nothing".
	return 0, fmt.Errorf("%s returned VARTYPE %d, not a number", name, v.vt)
}

// getObject returns an interface the CALLER must release. A VT_DISPATCH variant
// is deliberately NOT cleared: clearing one releases the interface, so doing
// both would return a pointer whose refcount already went to zero.
//
// # VT_UNKNOWN is QueryInterface'd, never cast
//
// An IUnknown is NOT an IDispatch. Casting one to *iDispatch and then calling
// dispID would dispatch through vtable SLOT 5 (GetIDsOfNames) on an object that
// may implement only IUnknown's three slots -- a call into whatever address
// happens to sit past the end of its vtable, in a SYSTEM process, on a machine
// nobody here can debug. That is the exact hazard this file's header says late
// binding was chosen to eliminate, so it cannot be reintroduced by an
// unchecked conversion.
//
// The scripting objects are documented to hand back VT_DISPATCH, so this branch
// is not expected to be taken at all. It is accepted rather than rejected
// because a COM server that returns an IUnknown which DOES implement IDispatch
// is legal and the QI costs one call; a server whose IUnknown does not is a
// clean E_NOINTERFACE here, i.e. `unavailable` (§0.7), never a wild call.
//
// The QI'd pointer is a SECOND reference, so the original is released. Together
// with the "do not clear a VT_DISPATCH" rule above, every path out of here owns
// exactly one reference, and header item 11's vtable claim is the only layout
// assumption left.
func (d *iDispatch) getObject(name string, args ...any) (*iDispatch, error) {
	v, err := d.invoke(name, dispatchPropertyGet|dispatchMethod, args...)
	if err != nil {
		return nil, err
	}
	if (v.vt != vtDispatch && v.vt != vtUnknown) || v.val == 0 {
		clearVariant(&v)
		return nil, fmt.Errorf("%s returned no object", name)
	}
	obj := (*iDispatch)(comPtr(v.val))
	if v.vt == vtUnknown {
		var disp *iDispatch
		hr, _, _ := syscall.SyscallN(
			obj.vtbl.QueryInterface,
			uintptr(unsafe.Pointer(obj)),
			uintptr(unsafe.Pointer(&iidIDispatch)),
			uintptr(unsafe.Pointer(&disp)),
		)
		// Release the IUnknown whether or not the QI succeeded: on success the
		// QI returned its own reference, and on failure this reference is the
		// only thing left holding an object nothing can use.
		obj.release()
		if comFailed(hr) || disp == nil {
			return nil, fmt.Errorf("%s returned an IUnknown that is not an IDispatch (0x%08x)",
				name, uint32(hr))
		}
		return disp, nil
	}
	return obj, nil
}

// ---------------------------------------------------------------------------
// BSTRs
// ---------------------------------------------------------------------------

func allocBSTR(s string) (*uint16, error) {
	utf16, err := syscall.UTF16PtrFromString(s)
	if err != nil {
		return nil, err
	}
	r, _, _ := syscall.SyscallN(procSysAllocString.Addr(), uintptr(unsafe.Pointer(utf16)))
	runtime.KeepAlive(utf16)
	if r == 0 {
		return nil, fmt.Errorf("health: SysAllocString returned NULL")
	}
	return (*uint16)(comPtr(int64(r))), nil
}

func freeBSTR(b *uint16) {
	if b != nil {
		syscall.SyscallN(procSysFreeString.Addr(), uintptr(unsafe.Pointer(b))) //nolint:errcheck
	}
}

// bstrToString reads a BSTR by its LENGTH PREFIX and not by scanning for a NUL.
//
// A BSTR may legitimately contain an embedded NUL, and a string coming out of a
// WMI provider is not a thing to trust a terminator from.
func bstrToString(b *uint16) string {
	if b == nil {
		return ""
	}
	n, _, _ := syscall.SyscallN(procSysStringLen.Addr(), uintptr(unsafe.Pointer(b)))
	if n == 0 {
		return ""
	}
	return strings.TrimRight(windows.UTF16ToString(unsafe.Slice(b, int(n))), "\x00")
}

// ---------------------------------------------------------------------------
// Object creation
// ---------------------------------------------------------------------------

// createObject is CoCreateInstance by ProgID, asking for IDispatch.
//
// The ProgID strings are compile-time constants in this package -- never
// anything from a job or a policy.
func createObject(progID string) (*iDispatch, error) {
	idPtr, err := syscall.UTF16PtrFromString(progID)
	if err != nil {
		return nil, err
	}
	var clsid windows.GUID
	hr, _, _ := syscall.SyscallN(
		procCLSIDFromProgID.Addr(),
		uintptr(unsafe.Pointer(idPtr)),
		uintptr(unsafe.Pointer(&clsid)),
	)
	runtime.KeepAlive(idPtr)
	if comFailed(hr) {
		return nil, fmt.Errorf("the COM class %s is not registered on this machine (0x%08x)",
			progID, uint32(hr))
	}

	var obj *iDispatch
	hr, _, _ = syscall.SyscallN(
		procCoCreateInstance.Addr(),
		uintptr(unsafe.Pointer(&clsid)),
		0,
		clsctxInprocServer|clsctxLocalServer,
		uintptr(unsafe.Pointer(&iidIDispatch)),
		uintptr(unsafe.Pointer(&obj)),
	)
	if comFailed(hr) || obj == nil {
		return nil, fmt.Errorf("CoCreateInstance(%s) failed (0x%08x)", progID, uint32(hr))
	}
	return obj, nil
}
