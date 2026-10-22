//go:build windows

package backup

// Windows volume snapshots: the Volume Shadow Copy Service, in process.
//
// # THIS FILE HAS NEVER EXECUTED
//
// No VSS call in this file has run. This repository's CI is Linux, there is no
// Windows runner, there is no Windows endpoint enrolled against this agent, and
// there is no Volume Shadow Copy Service anywhere this code has touched.
// `GOOS=windows go vet ./internal/capabilities/backup/` and
// `GOOS=windows go test -c -o /dev/null ./internal/capabilities/backup/`
// type-check and compile it, and that is the whole of its verification.
//
// # What that verification actually catches, stated precisely
//
// It catches: a symbol that does not exist in `golang.org/x/sys/windows`, a
// helper called with the wrong number or type of arguments, a struct field
// referenced by a name the struct does not have, an unused import, a dead
// local. Those are real defects and this file has been through them.
//
// It does NOT catch what the sequence below can most plausibly get wrong:
//
//   - **A mis-ordered vtable.** `com_windows.go` says it outright -- "a wrong
//     vtable INDEX compiles fine; a wrong SIGNATURE does not". Method dispatch
//     here is by FIELD NAME into `iVssBackupComponentsVtbl`, so a wrong index
//     is not spellable; what is spellable, and compiles perfectly, is a
//     `iVssBackupComponentsVtbl` whose FIELDS ARE DECLARED IN THE WRONG ORDER
//     or with one entry missing. That struct is the load-bearing claim in this
//     file and no compiler checks it. Every one of the 51 slots is declared,
//     including the 37 never called, precisely because the offsets of the ones
//     that ARE called depend on all of them. The design spec's §3.1a originally
//     offered "a wrong vtable index ... fails `GOOS=windows go vet`" as one of
//     three grounds for writing this file at all. That is true of an index
//     literal, which this file does not use, and false of the declaration order
//     that replaced it; the ground has since been WITHDRAWN in §3.1a, leaving
//     the override resting on two grounds and a larger residual risk than it
//     first claimed. This bullet is the reason it was withdrawn.
//   - **A right-shaped call with wrong semantics.** Every argument here is a
//     `uintptr`, so the compiler cannot tell a `VSS_ID*` from a `BOOL`, and
//     cannot tell VSS_CTX_BACKUP from VSS_CTX_FILE_SHARE_BACKUP.
//   - **Anything about the platform.** Not the C++ name mangling of
//     `CreateVssBackupComponents`, not the x64 convention for a 16-byte struct
//     passed by value, not `\\?\GLOBALROOT\Device\...` being openable by Go's
//     `os` package, not whether VSS needs the MTA.
//
// # What would falsify it
//
// **Confirming it** takes a real `backup.run` dispatched to a Windows endpoint
// that returns `succeeded`, carrying a manifest whose entries restore intact --
// i.e. a `mode=verify` restore of that backup point re-hashing every chunk
// clean. Until that has happened once, every claim in this file is
// documentation-derived and must be described that way, not reported as
// verified. `docs/modules/backup/agent-engine.md` §11 is where the absence is
// recorded.
//
// **Refuting it** is the more useful half, so here is what specific field
// failures would point at. Each names ONE suspect, which is the whole reason to
// write them down: the first Windows run will produce a refusal reason and
// nobody will be able to debug it from here.
//
//   - `VSS_E_BAD_STATE` (0x80042301) from `PrepareForBackup` -> the placement of
//     `SetBackupState`. §4 puts it after `AddToSnapshotSet`; MSDN's requester
//     samples put it before `GatherWriterMetadata`. See takeSnapshotSet.
//   - "vssapi.dll does not export CreateVssBackupComponents under the name this
//     build looks for" -> TWO suspects, because `LazyProc.Find` cannot tell them
//     apart: either `vssapi.dll` could not be LOADED at all (absent, or blocked),
//     or it loaded and the hand-decoded C++ mangled name is wrong. Check the
//     library first; it is the cheaper of the two to rule out.
//   - A writer-side `E_ACCESSDENIED` surfacing in the sequence's own HRESULTs ->
//     the deliberately omitted `CoInitializeSecurity`, below. NOTE THE NARROWNESS
//     OF THAT OBSERVATION: writer STATE is not observable from this agent at all,
//     because it never calls GatherWriterStatus. A writer denied access that does
//     not fail a requester call is invisible here.
//   - An access violation, or a call that plainly does something other than its
//     name, at ANY point -> `iVssBackupComponentsVtbl`'s declaration order. This
//     is the failure with the widest blast radius and the least evidence.
//   - A garbage or empty shadow copy device path out of a `GetSnapshotProperties`
//     that returned S_OK -> `vssSnapshotProp`'s field offsets.
//   - `backup.run` refusing with "marshals COM arguments for the windows/amd64
//     calling convention only" -> not a bug; the arm64/386 scope below.
//   - The snapshot succeeding but `verifyPresents` refusing with **"the snapshot
//     does not present the root"** (snapshot.go's message for a translated path
//     that would not OPEN) -> Go's `os` package and
//     `\\?\GLOBALROOT\Device\...`, or `shadowPath`'s trailing separator. The
//     message matters: these two suspects make the path unopenable, which is a
//     different refusal from the one below.
//   - `verifyPresents` refusing with **"presents no entries under %s, but the
//     live filesystem has %d"** -> a DIFFERENT fault. That path opened and read
//     empty, so the device object is fine and the mapping is not: suspect
//     `volumeOf`/`volumeRelative` putting a root under the wrong volume's shadow
//     copy. It is the Windows analogue of the APFS Full-Disk-Access case
//     verifyPresents was written for.
//   - A shadow copy, System Restore point or third-party backup copy that this
//     agent did not create disappearing across a `backup.run` -> `deleteSnapshots`
//     scoping: the `vssObjectSnapshotSet` constant or the `setID` it is paired
//     with. This is the file's worst outcome and the one with no compile-time
//     guard; see deleteSnapshotSet for why `E_INVALIDARG` is the load-bearing
//     mitigation rather than the constant being right.
//   - A backup point that restores files which are individually intact but
//     MUTUALLY INCONSISTENT -- a database that will not open, a log that does not
//     match its data file, an application-managed directory caught mid-write ->
//     the unchecked writer status, below. This is the one gap in this file's own
//     torn-data guarantee and it produces NO error anywhere; it is only visible
//     in the restored bytes.
//
// # What this file does that it cannot justify from evidence
//
// Three departures from the VSS requester documentation and from
// `agent-engine.md` §4, listed here rather than left in a comment beside the
// code that makes them.
//
//   - **`CoInitializeSecurity` is NOT called, and that is a documented risk.**
//     The VSS requester guidance and Microsoft's own `vshadow` sample both call
//     `CoInitializeSecurity(NULL, -1, NULL, NULL, RPC_C_AUTHN_LEVEL_PKT_PRIVACY,
//     RPC_C_IMP_LEVEL_IDENTIFY, NULL, EOAC_NONE, NULL)` before creating the
//     backup components object, and omitting it is a documented cause of
//     writer-side access failures. It is omitted anyway, deliberately:
//     `CoInitializeSecurity` is **process-wide**, not per-thread and not
//     per-apartment, and `patch/com_windows.go` uses COM in this same SYSTEM
//     process. A snapshot provider that silently reset process-wide COM security
//     as a side effect of taking a backup would be doing to the process what a
//     capability calling `slog.SetDefault` does to its logging -- reaching
//     outside its own module to change global state nobody else consented to.
//     Adding it is a decision for whoever owns COM initialization across this
//     binary, not for this file. The cost is stated in the falsifier list above:
//     a writer-side `E_ACCESSDENIED` in the field points here first.
//   - **`FreeWriterMetadata` is called before `PrepareForBackup`,** which §4's
//     sequence does not contain and MSDN's samples do not do -- they free at end
//     of session. The argument for the earlier call is that this requester
//     selects no components and never reads the documents, so they are per-writer
//     memory held for the life of a whole backup. THAT ARGUMENT ASSUMES VSS ITSELF
//     DOES NOT READ REQUESTER-SIDE METADATA DURING `PrepareForBackup` OR
//     `DoSnapshotSet`, AND NOTHING HERE CAN CHECK THAT ASSUMPTION. If a real
//     endpoint fails in either call, moving the free into `finish()` after
//     `BackupComplete` is the first thing to try.
//   - **WRITER STATUS IS NEVER CHECKED, AND THIS IS THE ONE GAP IN THIS FILE'S
//     OWN GUARANTEE.** `GatherWriterStatus`, `GetWriterStatusCount`,
//     `GetWriterStatus` and `FreeWriterStatus` are declared in the vtable at
//     slots 16-19 and NOTHING CALLS THEM. The requester pattern -- including the
//     `vshadow` sample cited above -- queries writer status after
//     `PrepareForBackup` and again after `DoSnapshotSet`, because **a writer
//     that fails to freeze does not necessarily fail the requester's call**. So
//     `DoSnapshotSet` can return VSS_S_ASYNC_FINISHED, this file can hand back a
//     shadow copy, and a writer inside it can have failed to quiesce -- yielding
//     a backup point whose files are individually intact and mutually
//     inconsistent, reported as `succeeded`.
//
//     That is the same failure this package refuses everywhere else. It is why
//     `defaultOpenSnapshot` refuses rather than reading the live filesystem, why
//     `verifyPresents` exists, and why `awaitAsync` treats an unrecognised
//     SUCCESS code as a failure. The application-consistency half of that
//     guarantee is currently asserted by silence.
//
//     The calls are NOT added, for `CoInitializeSecurity`'s reason: four more
//     unverifiable vtable dispatches in a COM sequence running as SYSTEM, to
//     detect a condition nobody here can test the detection of, buys crash
//     surface against a check we could not confirm was working. **This is the
//     single highest priority for the first real-hardware validation**: run a
//     `backup.run` against a machine with a live SQL Server or Exchange writer
//     and restore it, because nothing short of the restored bytes will show it.
//
// # Everything else this file could not confirm
//
// The full list, inline, because this header is the only copy that ships with
// the code. (The task report at `.git/sdd/task-5-report.md` carries the same
// list with more prose, but `.git/` does not survive a clone, so nothing here
// depends on it.)
//
//  1. **The `iVssBackupComponentsVtbl` declaration order.** No `vsbackup.h`
//     exists anywhere this code has been compiled. Recollection of its
//     declaration order, corroborated only by recollection of another project's
//     binding. The file's single point of failure.
//  2. **The mangled export name**
//     `?CreateVssBackupComponents@@YAJPEAPEAVIVssBackupComponents@@@Z` --
//     hand-decoded, never checked against `dumpbin /exports`. Mitigated: it
//     resolves through `LazyProc.Find`, so a wrong name refuses cleanly.
//  3. **`VssFreeSnapshotProperties`' export name** -- probed, with a per-member
//     `CoTaskMemFree` fallback. If `VSS_PWSZ` is not COM-task-allocator memory
//     that fallback is a heap error rather than a leak.
//  4. **The VSS constants**: `VSS_CTX_BACKUP` = 0x0, `VSS_BT_FULL` = 1,
//     `VSS_OBJECT_SNAPSHOT_SET` = 2, `VSS_S_ASYNC_FINISHED` = 0x0004230A.
//  5. **Platform behaviour no compiler sees**: that VSS wants the MTA rather
//     than com_windows.go's STA; that `GetVolumePathName` returns the form
//     `AddToSnapshotSet` accepts; that Go's `os` package can open
//     `\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopyN\...`; that
//     `bForceDelete` is scoped to the named set rather than the machine; and the
//     `VSS_SNAPSHOT_PROP` field offsets (review re-derived the padding to 128
//     bytes independently, which corroborates but does not observe).
//  6. **`CoInitializeSecurity` is omitted** -- see above.
//  7. **`FreeWriterMetadata`'s placement before `PrepareForBackup`** -- see
//     above.
//  8. **Where VSS considers a writer engaged**, i.e. the `AbortBackup` boundary.
//     Drawn at `StartSnapshotSet` on an asymmetric-cost argument, not on
//     evidence; an earlier revision of this file drew it four calls later and
//     asserted the opposite as fact. See finish().
//  9. **Writer status is never checked** -- see above. The only entry on this
//     list whose failure produces no error at all.
//
// One meta-item belongs with them: the design spec's original claim that
// compile-time checks catch a wrong vtable index was FALSE for this idiom, and
// has been withdrawn from §3.1a. See the vtable bullet further up.
//
// # A recorded decision was overridden to write this
//
// `docs/modules/backup/agent-engine.md` §4 judged writing an
// `IVssBackupComponents` binding from documentation alone "not a risk worth
// taking", because it is hundreds of lines of COM vtable calls first executed
// on a customer's machine, where an index off by one is a crash in a SYSTEM
// service. A reader arriving with that warning in hand is arriving correctly:
// the risk is real and is not dismissed here.
//
// It was overridden deliberately and on the record -- see
// `docs/superpowers/specs/2026-08-10-snapshot-providers-design.md` §3.1a. That
// section originally gave three grounds; the middle one, that compile-time
// checking mitigates the named failure mode, was withdrawn once this file
// showed it does not (see the vtable bullet above). TWO grounds remain:
// `patch/com_windows.go` already ships such a binding in the same SYSTEM
// service under the same kind of header, and the alternative leaves
// `backup.run` refusing on the entire Windows fleet indefinitely. Both stand,
// and the case for this file is weaker than it was when the work started.
// §4 records the change.
//
// # Why COM and not a shell-out
//
// Not `vssadmin`, not `diskshadow.exe`, not PowerShell, on any path including
// ones this code does not take. `com_windows.go` records the reason and
// `agent-engine.md` §2 pins it as a promise to an EDR team: a SYSTEM process
// spawning a shell is among the loudest behavioural signals an endpoint can
// emit, and `vssadmin delete shadows` in particular is a signature-grade
// ransomware indicator.
//
// THIS PROVIDER spawns no processes. That is narrower than the claim this
// comment used to make ("no processes, on any platform"), which stopped being
// true when the LVM and APFS providers landed: those shell out to `findmnt`,
// `lvs`, `lvcreate`, `mount`, `umount`, `lvremove`, `mkdir`, `tmutil` and
// `mount_apfs`. `agent-engine.md` §2 carries the per-platform list. The
// Windows half of the promise is the half that is load-bearing for an EDR
// team, and it is intact: nothing on any path here spawns anything.
//
// # Why the logic is not split out for testing, unlike LVM and APFS
//
// `snapshot_lvm.go` and `snapshot_apfs.go` carry NO build tag: they shell out
// through the injected `snapRunner` seam, so every decision they make is
// exercised by a real `go test ./internal/capabilities/backup/`, which passes
// when run locally on Linux -- not, currently, by CI, which has an agent job
// covering this package but is red for reasons outside it and deferred (see
// snapshot_darwin.go's header for the detail). Only the dispatch that selects
// those providers needs a tag. That is not available here. This provider calls
// `golang.org/x/sys/windows`, which builds on Windows and nowhere else, so the
// logic cannot live in an untagged file and there is no seam an injected fake
// could stand behind. Both the logic and the dispatch are therefore in this
// one file, and the compile checks above are the entire verification.
//
// # Scope: windows/amd64 only
//
// The file BUILDS for every windows GOARCH -- it must, or `snapshot_other.go`'s
// build tag would leave windows/386 and windows/arm64 with no PlatformSnapshot
// at all -- but openVSS REFUSES anywhere but amd64. Several VSS methods take a
// 16-byte VSS_ID by value, and how a 16-byte aggregate is passed is the one
// thing that differs between the three Windows ABIs: by hidden pointer on amd64,
// as four stack slots on x86 stdcall, in two registers on arm64. One
// marshalling cannot serve all three, and a wrong one is exactly the silent
// SYSTEM-service defect §4 warned about, so the other two refuse. See openVSS.

import (
	"context"
	"fmt"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// ---------------------------------------------------------------------------
// vssapi.dll
// ---------------------------------------------------------------------------

var modvssapi = windows.NewLazySystemDLL("vssapi.dll")

// procCreateVssBackupComponents is resolved by its C++-MANGLED name.
//
// `CreateVssBackupComponents` is declared in `vsbackup.h` as an ordinary C++
// free function returning HRESULT and taking `IVssBackupComponents**`. It is
// not `extern "C"`, so `vssapi.dll` exports it decorated and
// `NewProc("CreateVssBackupComponents")` -- the spelling every other COM entry
// point in this tree uses -- would fail to resolve at first call.
//
// The name below decodes as: `?CreateVssBackupComponents@@` the function,
// `YA` a `__cdecl` free function (the 64-bit default; 32-bit x86 would be `YG`,
// `__stdcall`), `J` returning `long`, `PEAPEAVIVssBackupComponents@@` taking
// one `__ptr64` pointer to a `__ptr64` pointer to class
// `IVssBackupComponents`, `@Z` end of arguments. UNCONFIRMED against a real
// `dumpbin /exports vssapi.dll`; if it is wrong, the failure is a clean
// "cannot resolve" refusal at the first call rather than a wrong call.
var procCreateVssBackupComponents = modvssapi.NewProc(
	"?CreateVssBackupComponents@@YAJPEAPEAVIVssBackupComponents@@@Z")

// procVssFreeSnapshotProperties frees the six VSS_PWSZ members of a
// VSS_SNAPSHOT_PROP. Whether vssapi.dll exports it decorated or undecorated is
// something this file cannot check, so freeSnapshotProperties() probes for it
// and falls back to CoTaskMemFree per member -- see there.
var procVssFreeSnapshotProperties = modvssapi.NewProc("VssFreeSnapshotProperties")

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

// Named rather than inlined, for com_windows.go's reason: a bare 0x1 where 0x5
// belongs is a bug nobody spots in a call whose arguments are all uintptr.
const (
	// VSS_CTX_BACKUP and VSS_BACKUP_TYPE live in vss_sequence.go.

	// VSS_OBJECT_TYPE. VSS_OBJECT_UNKNOWN = 0, VSS_OBJECT_NONE = 1,
	// VSS_OBJECT_SNAPSHOT_SET = 2, VSS_OBJECT_SNAPSHOT = 3,
	// VSS_OBJECT_PROVIDER = 4.
	vssObjectSnapshotSet = 2

	// Async status codes. All three are SUCCESS HRESULTs (bit 31 clear), so
	// they must be compared by value and not through vssFailed.
	vssSAsyncPending   = 0x00042309
	vssSAsyncFinished  = 0x0004230A
	vssSAsyncCancelled = 0x0004230B

	// INFINITE, for IVssAsync::Wait -- see awaitAsync for why a bounded wait is
	// not used instead.
	vssInfinite = 0xFFFFFFFF
)

// ---------------------------------------------------------------------------
// IVssBackupComponents
// ---------------------------------------------------------------------------

// iVssBackupComponentsVtbl is the FULL `vsbackup.h` vtable of
// `IVssBackupComponents`, IUnknown's three slots first.
//
// ALL 51 SLOTS ARE DECLARED EVEN THOUGH FOURTEEN ARE CALLED. That is the point:
// the byte offset of `GetSnapshotProperties` is a function of every field
// before it, so a struct holding only the methods this file uses would put
// every one of them at the wrong offset. Adding, removing or reordering a
// single line in this struct silently redirects several calls to different
// methods with mismatched arguments, running as SYSTEM. Nothing in this
// repository can detect that; the index comments exist so a reviewer with
// `vsbackup.h` open can check the order by counting. The `<- called` markers
// number FOURTEEN, leaving 37 slots this file never dispatches through.
//
// Order transcribed from the `vsbackup.h` declaration order (a C++ vtable
// follows declaration order within a single-inheritance interface).
// UNCONFIRMED against a real header file: no `vsbackup.h` exists anywhere this
// code has been compiled.
type iVssBackupComponentsVtbl struct {
	// IUnknown
	QueryInterface uintptr // 0
	AddRef         uintptr // 1
	Release        uintptr // 2  <- called

	// IVssBackupComponents
	GetWriterComponentsCount      uintptr // 3
	GetWriterComponents           uintptr // 4
	InitializeForBackup           uintptr // 5  <- called
	SetBackupState                uintptr // 6  <- called
	InitializeForRestore          uintptr // 7
	SetRestoreState               uintptr // 8
	GatherWriterMetadata          uintptr // 9  <- called
	GetWriterMetadataCount        uintptr // 10
	GetWriterMetadata             uintptr // 11
	FreeWriterMetadata            uintptr // 12 <- called
	AddComponent                  uintptr // 13
	PrepareForBackup              uintptr // 14 <- called
	AbortBackup                   uintptr // 15 <- called
	GatherWriterStatus            uintptr // 16 <- called
	GetWriterStatusCount          uintptr // 17 <- called
	FreeWriterStatus              uintptr // 18 <- called
	GetWriterStatus               uintptr // 19 <- called
	SetBackupSucceeded            uintptr // 20
	SetBackupOptions              uintptr // 21
	SetSelectedForRestore         uintptr // 22
	SetRestoreOptions             uintptr // 23
	SetAdditionalRestores         uintptr // 24
	SetPreviousBackupStamp        uintptr // 25
	SaveAsXML                     uintptr // 26
	BackupComplete                uintptr // 27 <- called
	AddAlternativeLocationMapping uintptr // 28
	AddRestoreSubcomponent        uintptr // 29
	SetFileRestoreStatus          uintptr // 30
	AddNewTarget                  uintptr // 31
	SetRangesFilePath             uintptr // 32
	PreRestore                    uintptr // 33
	PostRestore                   uintptr // 34
	SetContext                    uintptr // 35 <- called
	StartSnapshotSet              uintptr // 36 <- called
	AddToSnapshotSet              uintptr // 37 <- called
	DoSnapshotSet                 uintptr // 38 <- called
	DeleteSnapshots               uintptr // 39 <- called
	ImportSnapshots               uintptr // 40
	BreakSnapshotSet              uintptr // 41
	GetSnapshotProperties         uintptr // 42 <- called
	Query                         uintptr // 43
	IsVolumeSupported             uintptr // 44
	DisableWriterClasses          uintptr // 45
	EnableWriterClasses           uintptr // 46
	DisableWriterInstances        uintptr // 47
	ExposeSnapshot                uintptr // 48
	RevertToSnapshot              uintptr // 49
	QueryRevertStatus             uintptr // 50
}

type iVssBackupComponents struct {
	vtbl *iVssBackupComponentsVtbl
}

// iVssAsyncVtbl is the full vtable of `IVssAsync`.
type iVssAsyncVtbl struct {
	QueryInterface uintptr // 0
	AddRef         uintptr // 1
	Release        uintptr // 2
	Cancel         uintptr // 3
	Wait           uintptr // 4
	QueryStatus    uintptr // 5
}

type iVssAsync struct {
	vtbl *iVssAsyncVtbl
}

// vssSnapshotProp is `VSS_SNAPSHOT_PROP`.
//
// The six string members are `VSS_PWSZ` -- `LPWSTR`, allocated by the COM task
// allocator -- and are declared here as `uintptr` and NOT as `*uint16`. That is
// com_windows.go's rule and its reasoning applies unchanged: these are not Go
// pointers, and putting a foreign address in a Go pointer-typed field would
// hand Go's collector an address to scan as if it owned it.
//
// The two `_ int32` fields are the C compiler's padding, made explicit. Each
// sits after a 4-byte `LONG` and before an 8-byte-aligned member, so on
// amd64/arm64 the struct would be laid out this way by both compilers anyway;
// they are written down so the layout is reviewable rather than inferred, and
// so a reader can see that the trailing size is 128 bytes and not 124. The
// callee writes into this struct, so a short or misaligned one is a memory
// corruption, not a wrong value.
type vssSnapshotProp struct {
	snapshotID           windows.GUID // 0   VSS_ID   m_SnapshotId
	snapshotSetID        windows.GUID // 16  VSS_ID   m_SnapshotSetId
	snapshotsCount       int32        // 32  LONG     m_lSnapshotsCount
	_                    int32        // 36  padding to the next pointer
	snapshotDeviceObject uintptr      // 40  VSS_PWSZ m_pwszSnapshotDeviceObject
	originalVolumeName   uintptr      // 48  VSS_PWSZ m_pwszOriginalVolumeName
	originatingMachine   uintptr      // 56  VSS_PWSZ m_pwszOriginatingMachine
	serviceMachine       uintptr      // 64  VSS_PWSZ m_pwszServiceMachine
	exposedName          uintptr      // 72  VSS_PWSZ m_pwszExposedName
	exposedPath          uintptr      // 80  VSS_PWSZ m_pwszExposedPath
	providerID           windows.GUID // 88  VSS_ID   m_ProviderId
	snapshotAttributes   int32        // 104 LONG     m_lSnapshotAttributes
	_                    int32        // 108 padding before the 8-byte timestamp
	creationTimestamp    int64        // 112 VSS_TIMESTAMP m_tsCreationTimestamp
	status               int32        // 120 VSS_SNAPSHOT_STATE m_eStatus
	_                    int32        // 124 trailing padding
}

// vssFailed tests an HRESULT the way com_windows.go's `failed` does: the high
// bit of the LOW 32 bits. A 64-bit register holding an HRESULT may carry
// anything above bit 31.
func vssFailed(hr uintptr) bool { return int32(hr) < 0 }

func vssErrf(op string, hr uintptr) error {
	return fmt.Errorf("%s failed (0x%08x)", op, uint32(hr))
}

// vssPtr reinterprets a foreign address as a pointer.
//
// Identical to com_windows.go's `comPtr` and for the identical reason, restated
// because this file cannot import it: `unsafe.Pointer(uintptr(x))` is what this
// means, and `go vet` rejects that spelling because a uintptr naming a GO HEAP
// address can go stale between the two conversions. These are not Go pointers
// -- they come from the COM task allocator, in memory Go's collector neither
// moves nor frees -- so there is no window in which the value can go stale.
func vssPtr(u uintptr) unsafe.Pointer {
	return *(*unsafe.Pointer)(unsafe.Pointer(&u))
}

func (b *iVssBackupComponents) release() {
	if b != nil {
		syscall.SyscallN(b.vtbl.Release, uintptr(unsafe.Pointer(b))) //nolint:errcheck
	}
}

func (a *iVssAsync) release() {
	if a != nil {
		syscall.SyscallN(a.vtbl.Release, uintptr(unsafe.Pointer(a))) //nolint:errcheck
	}
}

// awaitAsync waits for one IVssAsync to finish and returns its final status.
//
// It takes ownership of the pointer and releases it.
//
// The wait is INFINITE, and that is the API's constraint rather than a choice:
// `IVssAsync::Wait`'s `dwMilliseconds` is documented as reserved, with INFINITE
// the only value a caller should pass. Passing a slice and polling QueryStatus
// would be undefined behaviour in a file nothing can test, which is worse than
// the hazard it would fix. THE HAZARD IS REAL AND IS STATED: a VSS writer that
// never completes blocks the calling job for as long as it takes, and the job's
// context deadline is checked BETWEEN phases (see takeSnapshotSet) but cannot
// interrupt one Wait.
func awaitAsync(op string, a *iVssAsync) error {
	if a == nil {
		return fmt.Errorf("%s returned no IVssAsync to wait on", op)
	}
	defer a.release()

	if hr, _, _ := syscall.SyscallN(
		a.vtbl.Wait,
		uintptr(unsafe.Pointer(a)),
		uintptr(uint32(vssInfinite)),
	); vssFailed(hr) {
		return vssErrf(op+": IVssAsync::Wait", hr)
	}

	// QueryStatus(HRESULT *pHrResult, INT *pReserved). pReserved must be NULL.
	var status int32
	if hr, _, _ := syscall.SyscallN(
		a.vtbl.QueryStatus,
		uintptr(unsafe.Pointer(a)),
		uintptr(unsafe.Pointer(&status)),
		0,
	); vssFailed(hr) {
		return vssErrf(op+": IVssAsync::QueryStatus", hr)
	}

	// The operation's OWN result, which is not the same thing as QueryStatus
	// succeeding. A backup that treated "I successfully read the status" as "the
	// snapshot was taken" is exactly the class of bug that produces a backup
	// point over nothing that reports success.
	switch {
	case vssFailed(uintptr(uint32(status))):
		return vssErrf(op, uintptr(uint32(status)))
	case uint32(status) == vssSAsyncCancelled:
		return fmt.Errorf("%s was cancelled by the Volume Shadow Copy Service", op)
	case uint32(status) == vssSAsyncPending:
		return fmt.Errorf("%s is still pending after an infinite wait", op)
	case status != 0 && uint32(status) != vssSAsyncFinished:
		// An unrecognised SUCCESS code. Treated as a failure on purpose: a
		// snapshot this agent cannot confirm was taken must not be backed up
		// from, because the alternative is a backup point over a torn or empty
		// filesystem that reports success.
		return fmt.Errorf("%s returned the unrecognised status 0x%08x rather than "+
			"VSS_S_ASYNC_FINISHED", op, uint32(status))
	}
	return nil
}

// ---------------------------------------------------------------------------
// The fourteen vtable calls, one function each
// ---------------------------------------------------------------------------
//
// Every method below MUST be called on the COM thread -- see comThread.
//
// Every VSS_ID argument that C++ declares BY VALUE is passed here as a POINTER
// (`uintptr(unsafe.Pointer(&id))`), because a 16-byte aggregate is not 1, 2, 4
// or 8 bytes wide and the Windows x64 convention therefore passes it by a hidden
// pointer to a caller-owned temporary.
//
// THAT IS TRUE ON amd64 AND ON NO OTHER GOARCH THIS AGENT BUILDS FOR. On 32-bit
// x86, stdcall pushes the sixteen bytes as four stack slots. On arm64,
// AAPCS64 -- which the Windows ARM64 ABI follows -- passes a composite of 16
// bytes or less IN REGISTERS, up to two of them, and only goes indirect ABOVE 16
// bytes; a GUID is exactly 16, so it lands in two X registers and a pointer
// would be read as the GUID's first eight bytes. Both would be silently
// malformed calls. That is why openVSS refuses on any GOARCH but amd64 instead
// of making them.

func createVssBackupComponents() (*iVssBackupComponents, error) {
	// Find() fails for TWO different reasons and does not distinguish them: the
	// library could not be LOADED (vssapi.dll absent, or blocked), or it loaded
	// and does not carry this symbol. The message names both, because falsifier
	// 2 in the header keys off this exact string and sending a field engineer
	// after a hand-decoded mangled name when the real problem is a missing DLL
	// wastes the one debugging session anybody gets.
	if err := procCreateVssBackupComponents.Find(); err != nil {
		return nil, fmt.Errorf("vssapi.dll could not be loaded, or does not export "+
			"CreateVssBackupComponents under the name this build looks for: %w", err)
	}
	var b *iVssBackupComponents
	hr, _, _ := syscall.SyscallN(
		procCreateVssBackupComponents.Addr(),
		uintptr(unsafe.Pointer(&b)),
	)
	if vssFailed(hr) {
		return nil, vssErrf("CreateVssBackupComponents", hr)
	}
	if b == nil {
		return nil, fmt.Errorf("CreateVssBackupComponents returned no IVssBackupComponents")
	}
	return b, nil
}

// initializeForBackup passes a NULL BSTR: this requester restores nothing and
// has no saved document to resume from.
func (b *iVssBackupComponents) initializeForBackup() error {
	if hr, _, _ := syscall.SyscallN(b.vtbl.InitializeForBackup, uintptr(unsafe.Pointer(b)), 0); vssFailed(hr) {
		return vssErrf("IVssBackupComponents::InitializeForBackup", hr)
	}
	return nil
}

func (b *iVssBackupComponents) setContext(ctxFlags int32) error {
	if hr, _, _ := syscall.SyscallN(
		b.vtbl.SetContext,
		uintptr(unsafe.Pointer(b)),
		uintptr(uint32(ctxFlags)),
	); vssFailed(hr) {
		return vssErrf("IVssBackupComponents::SetContext", hr)
	}
	return nil
}

func (b *iVssBackupComponents) gatherWriterMetadata() error {
	var a *iVssAsync
	if hr, _, _ := syscall.SyscallN(
		b.vtbl.GatherWriterMetadata,
		uintptr(unsafe.Pointer(b)),
		uintptr(unsafe.Pointer(&a)),
	); vssFailed(hr) {
		return vssErrf("IVssBackupComponents::GatherWriterMetadata", hr)
	}
	return awaitAsync("IVssBackupComponents::GatherWriterMetadata", a)
}

// freeWriterMetadata releases the metadata documents GatherWriterMetadata
// collected.
//
// # ADDITION TO §4'S SEQUENCE, AND AN UNVERIFIED ONE
//
// §4 does not contain this call, and MSDN's requester samples make it at END OF
// SESSION rather than here, before PrepareForBackup. It is here because this
// requester selects no components and never reads the documents, so they are
// per-writer memory held for the length of a whole backup in a capability whose
// claim is that it returns to baseline.
//
// THAT REASONING ASSUMES VSS DOES NOT ITSELF READ REQUESTER-SIDE METADATA
// DURING PrepareForBackup OR DoSnapshotSet. Nothing in this repository can check
// that assumption -- it is a claim about the service's internals, not about this
// code -- so it is recorded as an assumption and not as an optimisation. If a
// real endpoint fails in either of those calls, moving this into finish() after
// BackupComplete is the first thing to try. The header lists it among the two
// things this file does that it cannot justify from evidence.
//
// Best effort: failing to free is not a reason to refuse a snapshot that was
// taken.
func (b *iVssBackupComponents) freeWriterMetadata() {
	syscall.SyscallN(b.vtbl.FreeWriterMetadata, uintptr(unsafe.Pointer(b))) //nolint:errcheck
}

func (b *iVssBackupComponents) startSnapshotSet() (vssGUID, error) {
	var setID windows.GUID
	if hr, _, _ := syscall.SyscallN(
		b.vtbl.StartSnapshotSet,
		uintptr(unsafe.Pointer(b)),
		uintptr(unsafe.Pointer(&setID)),
	); vssFailed(hr) {
		return vssGUID{}, vssErrf("IVssBackupComponents::StartSnapshotSet", hr)
	}
	return toVSSGUID(setID), nil
}

func toVSSGUID(g windows.GUID) vssGUID { return *(*vssGUID)(unsafe.Pointer(&g)) }

func fromVSSGUID(g vssGUID) windows.GUID { return *(*windows.GUID)(unsafe.Pointer(&g)) }

// addToSnapshotSet adds one volume. `volume` must be a volume mount point with
// a TRAILING SEPARATOR (`C:\`) or a volume GUID path -- the form
// GetVolumePathName returns.
func (b *iVssBackupComponents) addToSnapshotSet(volume string) (vssGUID, error) {
	// The volume name reaches VSS as a UTF-16 string this process owns for the
	// duration of the call. It originates from GetVolumePathName, never from
	// job parameters.
	name, err := windows.UTF16PtrFromString(volume)
	if err != nil {
		return vssGUID{}, err
	}

	// GUID_NULL, meaning "let VSS pick the provider for this volume". Pinning a
	// specific provider would tie the agent to one storage stack.
	//
	// A FRESH LOCAL PER CALL, never a shared package-level var. `ProviderId` is
	// declared BY VALUE in C++, and the x64 convention this file marshals for
	// (see the section banner above) passes such an argument by a pointer to a
	// CALLER-ALLOCATED TEMPORARY that the callee is entitled to write through.
	// Handing every call a pointer to the same package-level GUID would mean one
	// callee scribbling on its argument corrupts every AddToSnapshotSet the
	// process makes afterwards -- a cross-job defect from a shared variable that
	// exists only to save sixteen bytes.
	providerID := windows.GUID{}

	var snapID windows.GUID
	hr, _, _ := syscall.SyscallN(
		b.vtbl.AddToSnapshotSet,
		uintptr(unsafe.Pointer(b)),
		uintptr(unsafe.Pointer(name)),
		uintptr(unsafe.Pointer(&providerID)),
		uintptr(unsafe.Pointer(&snapID)),
	)
	// Keep both alive until the call has returned. Without this the collector may
	// free the UTF-16 buffer, or the provider GUID, while VSS is still reading
	// it: the only remaining reference is the uintptr above, which it does not
	// trace. `snapID` needs no KeepAlive because it is returned.
	runtime.KeepAlive(name)
	runtime.KeepAlive(&providerID)
	if vssFailed(hr) {
		return vssGUID{}, vssErrf("IVssBackupComponents::AddToSnapshotSet", hr)
	}
	return toVSSGUID(snapID), nil
}

// setBackupState is §4's `SetBackupState(false, false, VSS_BT_FULL)`, plus the
// fourth argument C++ supplies by default.
//
//   - bSelectComponents = false: this is a whole-volume file-level backup, not
//     a writer-component backup, so no AddComponent call follows and none is
//     owed.
//   - bBackupBootableSystemState = false: not a bare-metal backup.
//   - VSS_BT_FULL: a full backup. Anything else would ask writers to prepare
//     for an incremental this engine does not implement.
//   - bPartialFileSupport = false: this engine chunks whole files.
//
// THREE of the four are C++ `bool` -- one byte, zero-extended into a register --
// and 0 and 1 are the correct values for them, NOT VARIANT_TRUE's -1, which is a
// different type's convention and appears three files away in com_windows.go.
// The THIRD argument is not a bool at all: `VSS_BACKUP_TYPE` is an enum, passed
// here as vssBTCopy (5) for files (D5) or vssBTFull (1). Reading a `1` in this
// argument list as "true" is how an edit changes the backup type unseen.
func (b *iVssBackupComponents) setBackupState(backupType int32) error {
	if hr, _, _ := syscall.SyscallN(
		b.vtbl.SetBackupState,
		uintptr(unsafe.Pointer(b)),
		0, // bSelectComponents = false
		0, // bBackupBootableSystemState = false
		uintptr(uint32(backupType)),
		0, // bPartialFileSupport = false
	); vssFailed(hr) {
		return vssErrf("IVssBackupComponents::SetBackupState", hr)
	}
	return nil
}

func (b *iVssBackupComponents) prepareForBackup() error {
	var a *iVssAsync
	if hr, _, _ := syscall.SyscallN(
		b.vtbl.PrepareForBackup,
		uintptr(unsafe.Pointer(b)),
		uintptr(unsafe.Pointer(&a)),
	); vssFailed(hr) {
		return vssErrf("IVssBackupComponents::PrepareForBackup", hr)
	}
	return awaitAsync("IVssBackupComponents::PrepareForBackup", a)
}

func (b *iVssBackupComponents) doSnapshotSet() error {
	var a *iVssAsync
	if hr, _, _ := syscall.SyscallN(
		b.vtbl.DoSnapshotSet,
		uintptr(unsafe.Pointer(b)),
		uintptr(unsafe.Pointer(&a)),
	); vssFailed(hr) {
		return vssErrf("IVssBackupComponents::DoSnapshotSet", hr)
	}
	return awaitAsync("IVssBackupComponents::DoSnapshotSet", a)
}

// snapshotDevicePath reads the shadow copy device object for one snapshot id --
// `\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopyN` -- and frees the property
// block before returning, so nothing COM allocated outlives this call.
func (b *iVssBackupComponents) snapshotDevicePath(id vssGUID) (string, error) {
	snapID := fromVSSGUID(id)
	var prop vssSnapshotProp
	if hr, _, _ := syscall.SyscallN(
		b.vtbl.GetSnapshotProperties,
		uintptr(unsafe.Pointer(b)),
		uintptr(unsafe.Pointer(&snapID)),
		uintptr(unsafe.Pointer(&prop)),
	); vssFailed(hr) {
		return "", vssErrf("IVssBackupComponents::GetSnapshotProperties", hr)
	}
	// Copy into a Go string BEFORE freeing: the device path is the one thing
	// this whole file exists to produce, and after the free it is a dangling
	// pointer.
	device := windows.UTF16PtrToString((*uint16)(vssPtr(prop.snapshotDeviceObject)))
	freeSnapshotProperties(&prop)
	if device == "" {
		return "", fmt.Errorf("IVssBackupComponents::GetSnapshotProperties returned an empty " +
			"shadow copy device path")
	}
	return device, nil
}

// backupComplete tells every writer the backup is finished.
//
// It comes BEFORE DeleteSnapshots and the order is not interchangeable: a
// writer left waiting for BackupComplete keeps whatever it froze frozen, and
// deleting the shadow copies from under it is how a requester leaves a database
// writer in a bad state. §4 specifies this order.
func (b *iVssBackupComponents) backupComplete() error {
	var a *iVssAsync
	if hr, _, _ := syscall.SyscallN(
		b.vtbl.BackupComplete,
		uintptr(unsafe.Pointer(b)),
		uintptr(unsafe.Pointer(&a)),
	); vssFailed(hr) {
		return vssErrf("IVssBackupComponents::BackupComplete", hr)
	}
	return awaitAsync("IVssBackupComponents::BackupComplete", a)
}

// abortBackup is the way out when the sequence fails AFTER PrepareForBackup but
// BEFORE DoSnapshotSet succeeded. Without it the writers this requester froze
// are left waiting for a backup that will never complete. Best effort: it runs
// on a path that is already returning an error.
func (b *iVssBackupComponents) abortBackup() {
	syscall.SyscallN(b.vtbl.AbortBackup, uintptr(unsafe.Pointer(b))) //nolint:errcheck
}

// deleteSnapshotSet deletes THIS SNAPSHOT SET AND NOTHING ELSE.
//
// `setID` is the id StartSnapshotSet minted for this job, and
// VSS_OBJECT_SNAPSHOT_SET scopes the delete to that id. This is the single most
// destructive call in the file and the scoping is the whole of its safety: the
// machine may hold shadow copies belonging to Windows Server Backup, to a
// third-party backup product, or to System Restore, and deleting one of those
// would destroy a restore point this agent does not own. It is the Windows
// equivalent of the destruction snapshot.go's exact `snapTagPrefix` match
// exists to prevent on LVM, and it is why `vssadmin delete shadows` -- which
// takes /all -- is not merely an EDR hazard but the wrong operation.
//
// # What actually makes this safe, as opposed to what is merely believed
//
// The rest of this file hedges the MTA, the constants and the struct offsets;
// this function must not be the one place that asserts a semantic flat, because
// it is the one whose mistake is unrecoverable. So, separated:
//
// BELIEVED, NOT VERIFIED: that `bForceDelete = TRUE` applies to the snapshots in
// the NAMED SET only, and never widens the operation. It is TRUE because the
// alternative -- a shadow copy this agent created and can no longer name,
// consuming diff-area space until the service evicts it -- is a leak with no
// second chance at collection: nothing tags a VSS snapshot set, so a future run
// cannot find it (see openVSS on why there is no sweep). If that reading of
// `bForceDelete` is wrong, this is the worst defect in the file.
//
// ACTUALLY LOAD-BEARING, and the reason a reader should sleep: `DeleteSnapshots`
// accepts only VSS_OBJECT_SNAPSHOT and VSS_OBJECT_SNAPSHOT_SET for
// `eSourceObjectType`. Every other VSS_OBJECT_TYPE -- including
// VSS_OBJECT_UNKNOWN (0) and VSS_OBJECT_NONE (1), which are what a transcription
// error in `vssObjectSnapshotSet` would most plausibly produce -- is rejected
// with E_INVALIDARG. So the failure mode of getting that constant wrong is a
// REFUSED backup, not a widened delete. The safety here does not rest on this
// file having the number right; it rests on VSS refusing the numbers it does not
// mean. There is no equivalent guard on `setID`, which is why it is passed
// straight from `StartSnapshotSet` and stored nowhere a job can reach.
//
// The falsifier, since neither of the above is observable from here: a shadow
// copy, System Restore point or third-party backup copy that this agent did not
// create disappearing across a `backup.run`. Nothing in this repository will
// ever show that; only a real machine will.
func (b *iVssBackupComponents) deleteSnapshotSet(id vssGUID) error {
	setID := fromVSSGUID(id)
	var (
		deleted     int32
		notDeleted  windows.GUID
		forceDelete = 1 // BOOL
	)
	if hr, _, _ := syscall.SyscallN(
		b.vtbl.DeleteSnapshots,
		uintptr(unsafe.Pointer(b)),
		uintptr(unsafe.Pointer(&setID)),
		uintptr(uint32(vssObjectSnapshotSet)),
		uintptr(uint32(forceDelete)),
		uintptr(unsafe.Pointer(&deleted)),
		uintptr(unsafe.Pointer(&notDeleted)),
	); vssFailed(hr) {
		return vssErrf("IVssBackupComponents::DeleteSnapshots", hr)
	}
	return nil
}

// writerStatus is writer check (§4.1): GatherWriterStatus, then
// GetWriterStatusCount and GetWriterStatus per writer, SysFreeString on each
// name, FreeWriterStatus. Slots 16-19.
func (b *iVssBackupComponents) writerStatus() ([]writerStatus, error) {
	var a *iVssAsync
	if hr, _, _ := syscall.SyscallN(
		b.vtbl.GatherWriterStatus,
		uintptr(unsafe.Pointer(b)),
		uintptr(unsafe.Pointer(&a)),
	); vssFailed(hr) {
		return nil, vssErrf("IVssBackupComponents::GatherWriterStatus", hr)
	}
	if err := awaitAsync("IVssBackupComponents::GatherWriterStatus", a); err != nil {
		return nil, err
	}
	defer syscall.SyscallN(b.vtbl.FreeWriterStatus, uintptr(unsafe.Pointer(b))) //nolint:errcheck

	var count uint32
	if hr, _, _ := syscall.SyscallN(
		b.vtbl.GetWriterStatusCount,
		uintptr(unsafe.Pointer(b)),
		uintptr(unsafe.Pointer(&count)),
	); vssFailed(hr) {
		return nil, vssErrf("IVssBackupComponents::GetWriterStatusCount", hr)
	}
	out := make([]writerStatus, 0, count)
	for i := uint32(0); i < count; i++ {
		var (
			instance, writer windows.GUID
			name             *uint16
			state            int32
			failure          int32
		)
		hr, _, _ := syscall.SyscallN(
			b.vtbl.GetWriterStatus,
			uintptr(unsafe.Pointer(b)),
			uintptr(i),
			uintptr(unsafe.Pointer(&instance)),
			uintptr(unsafe.Pointer(&writer)),
			uintptr(unsafe.Pointer(&name)),
			uintptr(unsafe.Pointer(&state)),
			uintptr(unsafe.Pointer(&failure)),
		)
		if vssFailed(hr) {
			return nil, vssErrf("IVssBackupComponents::GetWriterStatus", hr)
		}
		ws := writerStatus{state: state, hr: failure}
		if name != nil {
			ws.name = windows.UTF16PtrToString(name)
			sysFreeString(name)
		}
		out = append(out, ws)
	}
	return out, nil
}

var procSysFreeString = windows.NewLazySystemDLL("oleaut32.dll").NewProc("SysFreeString")

func sysFreeString(s *uint16) {
	if procSysFreeString.Find() == nil {
		procSysFreeString.Call(uintptr(unsafe.Pointer(s))) //nolint:errcheck
	}
}

// freeSnapshotProperties frees the six VSS_PWSZ members of a
// VSS_SNAPSHOT_PROP.
//
// `VssFreeSnapshotProperties` is the documented way, but whether `vssapi.dll`
// exports it under that undecorated name or a C++-mangled one is exactly the
// kind of thing this file cannot check, and getting it wrong would leak six
// strings per volume per job. So the symbol is PROBED, and when it is absent
// each member is handed to `CoTaskMemFree` instead -- VSS_PWSZ is documented as
// allocated by the COM task allocator, which is all
// `VssFreeSnapshotProperties` itself does.
//
// UNCONFIRMED either way. If the probe resolves the wrong symbol, or if a
// VSS_PWSZ turns out not to come from CoTaskMemAlloc, this is a heap error and
// not a leak. Kept because the alternative is an unconditional call to a name
// this file cannot verify at all.
func freeSnapshotProperties(p *vssSnapshotProp) {
	if err := procVssFreeSnapshotProperties.Find(); err == nil {
		syscall.SyscallN(procVssFreeSnapshotProperties.Addr(), uintptr(unsafe.Pointer(p))) //nolint:errcheck
		return
	}
	for _, s := range [...]uintptr{
		p.snapshotDeviceObject,
		p.originalVolumeName,
		p.originatingMachine,
		p.serviceMachine,
		p.exposedName,
		p.exposedPath,
	} {
		if s != 0 {
			windows.CoTaskMemFree(vssPtr(s))
		}
	}
}

// ---------------------------------------------------------------------------
// One OS thread, for the life of the snapshot
// ---------------------------------------------------------------------------

// comThread runs every COM call in this file on ONE locked OS thread.
//
// # Why this exists, since it is the least obvious part of the file
//
// `CoInitializeEx` initializes COM FOR THE CALLING THREAD, and a Go goroutine
// does not own an OS thread: the scheduler may move it at any function call or
// channel operation. A snapshot's lifetime spans two separate entry points --
// openVSS takes it, `Snapshot.Release` tears it down after a whole backup has
// run -- so without an explicit thread the CoUninitialize would land on a
// different thread than the CoInitializeEx, and the teardown calls would come
// from a thread on which COM was never initialized. That is CO_E_NOTINITIALIZED
// at best, which would mean BackupComplete and DeleteSnapshots silently never
// happen: a shadow copy left on every endpoint after every backup, and writers
// left waiting.
//
// So: one goroutine, `runtime.LockOSThread`, CoInitializeEx on the way in,
// CoUninitialize as the last thing it does. It exists only while a snapshot is
// held -- a backup job is running by definition, so this costs the idle agent
// nothing.
//
// MTA (`COINIT_MULTITHREADED`) rather than the STA `patch/com_windows.go` uses,
// because that is what VSS documents for a requester. The two do not conflict:
// the apartment is a property of the thread, and this is a different thread.
// UNCONFIRMED, like everything else here.
type comThread struct {
	calls chan func()
	// stopped closes after CoUninitialize has run.
	stopped chan struct{}
	once    sync.Once
}

func startCOMThread() (*comThread, error) {
	t := &comThread{
		calls:   make(chan func()),
		stopped: make(chan struct{}),
	}
	ready := make(chan error, 1)

	go func() {
		runtime.LockOSThread()
		defer runtime.UnlockOSThread()
		defer close(t.stopped)

		// S_FALSE means COM was already initialized on this thread with the
		// same model. It is a success, and x/sys/windows still returns it as an
		// error because any non-zero HRESULT is one. A fresh thread should not
		// see it; accepted anyway rather than failing a snapshot over a success
		// code.
		if err := windows.CoInitializeEx(0, windows.COINIT_MULTITHREADED); err != nil &&
			err != syscall.Errno(windows.S_FALSE) {
			ready <- fmt.Errorf("CoInitializeEx(COINIT_MULTITHREADED): %w", err)
			return
		}
		initCOMSecurity()
		ready <- nil

		for fn := range t.calls {
			fn()
		}
		// LAST. Every COM pointer this thread created has been released by the
		// closure that ran above.
		windows.CoUninitialize()
	}()

	if err := <-ready; err != nil {
		<-t.stopped
		return nil, err
	}
	return t, nil
}

// do runs fn on the COM thread and blocks until it has returned.
//
// Callers must not call do after stop.
func (t *comThread) do(fn func()) {
	done := make(chan struct{})
	t.calls <- func() {
		defer close(done)
		fn()
	}
	<-done
}

// stop ends the COM thread and waits for CoUninitialize. Idempotent, because
// Snapshot.Release may be reached twice -- openVSS releases on a failed
// verifyPresents, and the job's own deferred Release runs regardless.
func (t *comThread) stop() {
	t.once.Do(func() { close(t.calls) })
	<-t.stopped
}

// ---------------------------------------------------------------------------
// Volumes and paths
// ---------------------------------------------------------------------------

// vssRoot pairs one backup root with the mount point of the volume holding it.
type vssRoot struct {
	root   string
	volume string
}

// resolveRoots maps each root to its volume and returns the DEDUPLICATED,
// order-preserving list of volumes to snapshot.
//
// Deduplication matters: several roots on `C:\` must produce ONE
// AddToSnapshotSet call. Adding the same volume twice is a wasted shadow copy
// at best and VSS_E_OBJECT_ALREADY_EXISTS at worst.
func resolveRoots(roots []string) ([]vssRoot, []string, error) {
	out := make([]vssRoot, 0, len(roots))
	var volumes []string
	seen := make(map[string]struct{}, len(roots))

	// Roots are named BY INDEX, never by path — see the convention in
	// snapshot.go. A refusal reason lands in the server's hash-chained audit
	// log, and a source path is customer data.
	for i, root := range roots {
		if !filepath.IsAbs(root) {
			// A relative root resolves against the service's working directory,
			// and there is no honest way to map it onto a shadow copy device.
			// snapshot.go's rule: refuse rather than mistranslate silently.
			//
			// validateRoots in the shared core has already refused this, and
			// openVSS calls it first; this is the second layer, kept because it
			// is one comparison and because this function is also the one place
			// that would notice if that call were ever dropped.
			return nil, nil, jobs.Refuse("backup.run: the source root at index %d is not absolute, "+
				"so it cannot be mapped onto a volume shadow copy", i)
		}
		volume, err := volumeOf(root)
		if err != nil {
			return nil, nil, jobs.Refuse("backup.run: cannot determine which volume holds the source "+
				"root at index %d, so VSS cannot snapshot it", i)
		}
		if _, ok := volumeRelative(root, volume); !ok {
			return nil, nil, jobs.Refuse("backup.run: the source root at index %d is not under the "+
				"volume Windows reports for it, so it cannot be mapped onto a shadow copy", i)
		}
		out = append(out, vssRoot{root: root, volume: volume})
		key := strings.ToLower(volume)
		if _, dup := seen[key]; !dup {
			seen[key] = struct{}{}
			volumes = append(volumes, volume)
		}
	}
	return out, volumes, nil
}

// volumeOf returns the mount point of the volume containing path -- `C:\`, or
// `C:\mnt\data\` for a volume mounted at a directory. The trailing separator is
// GetVolumePathName's own and is what AddToSnapshotSet wants.
func volumeOf(path string) (string, error) {
	p, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return "", err
	}
	// MAX_PATH+1 is GetVolumePathName's documented requirement for the buffer.
	buf := make([]uint16, windows.MAX_PATH+1)
	if err := windows.GetVolumePathName(p, &buf[0], uint32(len(buf))); err != nil {
		return "", err
	}
	volume := windows.UTF16ToString(buf)
	if volume == "" {
		return "", fmt.Errorf("GetVolumePathName(%q) returned an empty volume path", path)
	}
	return volume, nil
}

// volumeRelative returns root's path relative to its volume mount point, with a
// LEADING separator, or the empty string when root IS the volume root.
//
// The comparison is case-insensitive because Windows paths are, and because the
// root comes from a job while the volume comes from GetVolumePathName: `c:\data`
// and `C:\` must match.
//
// BOTH SEPARATORS ARE ACCEPTED AND THE RESULT IS ALWAYS BACKSLASHED. Windows
// treats `/` and `\` interchangeably in an ordinary path, so a policy may
// legitimately spell a root `C:/data` -- but a `\\?\`-prefixed device path is
// exactly where that stops being true: the prefix turns OFF path parsing, so a
// forward slash reaches the object manager as a literal character and the open
// fails. Left unhandled, `C:/data` would produce `...ShadowCopy1\/data` and
// refuse with "the snapshot presents no entries", which points nowhere near the
// slash that caused it. Canonicalising here is cheaper than a refusal an
// operator cannot decode.
func volumeRelative(root, volume string) (string, bool) {
	const sep = `\`
	r := strings.ReplaceAll(root, "/", sep)
	v := strings.TrimRight(strings.ReplaceAll(volume, "/", sep), sep)
	if v == "" || len(r) < len(v) || !strings.EqualFold(r[:len(v)], v) {
		return "", false
	}
	rest := r[len(v):]
	// The match must land on a path BOUNDARY. Without this, a volume mounted at
	// `C:\mnt\data` would swallow `C:\mnt\database` and map it into the wrong
	// shadow copy. resolveRoots always derives the volume from the root itself,
	// so the pair should be consistent -- this is the check that keeps "should"
	// from being load-bearing.
	if rest != "" && !strings.HasPrefix(rest, sep) {
		return "", false
	}
	rel := strings.TrimPrefix(rest, sep)
	if rel == "" {
		return "", true
	}
	return sep + rel, true
}

// shadowPath builds the path inside a shadow copy that corresponds to one root.
//
// `filepath.Join` is deliberately NOT used: it calls Clean, whose treatment of
// a `\\?\`-prefixed path is one more thing this file cannot test. Concatenating
// a device object name with a rel that is either empty or begins with a
// separator has nothing to resolve.
//
// The trailing separator in the empty-rel case is not cosmetic: a shadow copy
// device object cannot be opened as a directory without it, which is the
// difference between backing up a whole volume and refusing on it.
func shadowPath(device, rel string) string {
	if rel == "" {
		return device + string(filepath.Separator)
	}
	return device + rel
}

// The sequence itself (order, writer checks, teardown) is vss_sequence.go,
// driven through vssBackend so it is tested on every OS.

// ---------------------------------------------------------------------------
// The Snapshot
// ---------------------------------------------------------------------------

// vssSnapshot is a multiSnapshot for translation, with its own Release.
//
// The embedded Snapshot supplies Translate -- newMultiSnapshot's
// longest-origin-first prefix resolution is the same on every platform and is
// not reimplemented here. Release is overridden because VSS's teardown is ONE
// operation over the whole snapshot set, not the per-volume teardown func
// multiSnapshot calls: there is no per-volume unmount, and calling
// BackupComplete once per volume would be wrong.
type vssSnapshot struct {
	Snapshot
	release func() error
	set     *vssSet

	once sync.Once
	err  error
}

// Release tears down the snapshot set exactly once and reports the first
// failure. Idempotent because both openVSS's verifyPresents failure path and
// the job's own deferred Release can reach it.
func (s *vssSnapshot) Release() error {
	s.once.Do(func() { s.err = s.release() })
	return s.err
}

func (s *vssSnapshot) mechanism(string) string { return mechVSS }

// writerReport is complete after Release, which ran writer check 3.
func (s *vssSnapshot) writerReport() ([]writerDoc, []string) {
	return s.set.writers, s.set.warnings
}

// openVSS takes a Volume Shadow Copy Service snapshot covering roots.
//
// # There is no orphan sweep here, and that is deliberate
//
// The other two providers sweep before they create: LVM by the exact
// `snapTagPrefix` name it gave its own snapshots, APFS by reclaiming
// mountpoints under its own directory. NEITHER SHAPE IS AVAILABLE ON WINDOWS. A
// VSS snapshot set carries no name a requester chooses -- StartSnapshotSet
// mints a GUID -- so a crashed agent leaves behind a set this agent cannot
// recognise on the next run, and the only way to "sweep" would be to enumerate
// every shadow copy on the machine and delete the ones that look unfamiliar.
// That would destroy System Restore points, Windows Server Backup copies and
// any third-party backup product's shadow copies: the exact destruction
// snapshot.go's exact-prefix rule and snapshot_apfs.go's refusal to touch Time
// Machine snapshots both exist to prevent, and worse here because a shadow copy
// is somebody's restore point.
//
// So the leak is accepted and stated: an agent killed between DoSnapshotSet and
// Release leaves one shadow copy set per volume behind, and nothing in this
// agent will ever collect it. An earlier draft of the design spec argued that
// VSS_CTX_BACKUP copies are non-persistent and therefore self-releasing on
// process exit, which would close this gap; that claim was WITHDRAWN as
// unverified (spec §3.1) and is not relied on here.
func openVSS(ctx context.Context, roots []string) (Snapshot, error) {
	// Every VSS_ID argument below is passed BY POINTER, per the Windows x64
	// convention for a 16-byte aggregate passed by value. That convention is
	// amd64's alone: x86 stdcall pushes the sixteen bytes as four stack slots,
	// and arm64 passes a <=16-byte composite in two registers. On either one
	// these calls would be silently malformed -- the volume argument shifted, a
	// pointer read as a GUID -- in a SYSTEM service, which is the precise
	// failure mode agent-engine.md §4 warned about.
	//
	// So it refuses, rather than making the call and rather than falling back to
	// the live filesystem. windows/arm64 IS a real target this refusal turns off:
	// stated plainly here because a Surface-class fleet would hit it, and the fix
	// is a second argument-marshalling path written by somebody who can run it,
	// not a guess made here.
	if arch := runtime.GOARCH; arch != "amd64" {
		return nil, jobs.Refuse("backup.run: this agent's VSS provider marshals COM arguments for "+
			"the windows/amd64 calling convention only, and this build targets windows/%s, where "+
			"those calls would be malformed. Backing up the live filesystem instead would produce "+
			"a backup point that restores torn files and reports success", arch)
	}
	// The shared core's rules, applied here so all three providers agree: an
	// EMPTY root list, a relative root, or one beginning with `-` is refused
	// before anything touches it. The empty-list check used to be a separate
	// copy right here; it moved into validateRoots so the other two providers
	// get it too -- openAPFS was leaking a mount without it. Two copies of one
	// refusal is how they drift apart.
	if err := validateRoots(roots); err != nil {
		return nil, err
	}

	// Volume resolution touches no COM, so it happens before a thread is
	// locked and before anything is created: a root that cannot be mapped
	// refuses without leaving a shadow copy behind. Same reasoning as
	// openLVM's "refuse BEFORE anything is created".
	mapped, volumes, err := resolveRoots(roots)
	if err != nil {
		return nil, err
	}

	com, err := startCOMThread()
	if err != nil {
		// An OPERATIONAL failure, not a refusal (§9, and the same
		// classification the LVM and APFS providers now apply): failing to
		// initialize COM on a Windows endpoint is a condition of this run, not
		// a standing incapacity of the device. A refusal would leave the
		// `backup_runs` row on `running` with no error text.
		return nil, snapFailuref("COM could not be initialized to reach the Volume Shadow Copy "+
			"Service: %s", err)
	}

	var (
		set     *vssSet
		takeErr error
	)
	opts := snapOptionsFrom(ctx)
	com.do(func() {
		b, err := createVssBackupComponents()
		if err != nil {
			takeErr = err
			return
		}
		set, takeErr = takeSnapshotSet(ctx, b, volumes, vssBackupType(opts.vssType), opts.failOnWriter)
	})
	if takeErr != nil {
		com.stop()
		// NOT run through clean(), unlike the refusals above. clean() is for
		// JOB-SUPPLIED bytes, and it truncates at 40 characters -- which here
		// would discard the HRESULT, the one piece of the message an operator
		// can act on. Nothing job-supplied reaches this text: every error the
		// COM sequence produces is a constant method name plus a hexadecimal
		// HRESULT (vssErrf), and the only paths that could interpolate a
		// job-supplied root -- resolveRoots and openVSS's mapping loop -- name
		// their roots by INDEX and never reach here. If a future call in
		// takeSnapshotSet starts embedding a root or a volume name in its
		// error, that string must be removed AT ITS SOURCE.
		//
		// An operational failure rather than a refusal, for the reason
		// startCOMThread's failure above gives: a writer timing out or a
		// shadow copy failing to be created is transient.
		return nil, snapFailuref("the Volume Shadow Copy Service did not produce a snapshot: %w", takeErr)
	}

	release := func() error {
		var err error
		com.do(func() { err = set.finish() })
		// Ends the thread and runs CoUninitialize, after every COM pointer has
		// been released.
		com.stop()
		return err
	}

	vols := make([]volSnapshot, 0, len(mapped))
	// `mapped` is in `roots` order, so i IS the root's index in the job's
	// source list -- which is how these messages name it, per snapshot.go's
	// convention.
	for i, m := range mapped {
		device, ok := set.devices[strings.ToLower(m.volume)]
		if !ok {
			// Cannot happen -- resolveRoots built `volumes` from these same
			// mount points -- but a missing device would silently translate to
			// the LIVE path, which is the one outcome this whole file exists to
			// prevent.
			_ = release()
			return nil, snapFailuref("no shadow copy was created for the volume holding the source "+
				"root at index %d", i)
		}
		rel, ok := volumeRelative(m.root, m.volume)
		if !ok {
			_ = release()
			return nil, snapFailuref("the source root at index %d could not be mapped into its "+
				"shadow copy", i)
		}
		vols = append(vols, volSnapshot{
			origin: m.root,
			// The manifest keeps recording the ORIGINAL path; only the reads go
			// through here. A manifest full of
			// \\?\GLOBALROOT\Device\HarddiskVolumeShadowCopyN paths would be
			// meaningless to an operator and unrestorable afterwards
			// (agent-engine.md §4).
			translated: shadowPath(device, rel),
			mech:       mechVSS,
			// teardown is nil on purpose: VSS tears the whole set down at once,
			// through vssSnapshot.Release, not per volume.
		})
	}

	s := &vssSnapshot{Snapshot: newMultiSnapshot(vols), release: release, set: set}
	if err := verifyPresents(roots, s); err != nil {
		// A refusal here must not leave the snapshot set on the endpoint.
		_ = s.Release()
		return nil, err
	}
	return s, nil
}

// PlatformSnapshot takes a VSS snapshot on Windows.
//
// The dispatch shape is snapshot_linux.go's and snapshot_darwin.go's:
// requireKind first, so a job asking for `lvm` or `apfs` on a Windows endpoint
// is refused with the server's mistake in the job record rather than silently
// served the locally correct mechanism.
//
// Unlike the other two this takes no tag: pidTag() exists so an orphan sweep
// can recognise the agent's own mountpoints, and VSS has neither -- see
// openVSS's note on why there is no sweep.
func PlatformSnapshot(ctx context.Context, kind string, roots []string) (Snapshot, error) {
	if err := requireKind(kind, snapshotVSS); err != nil {
		return nil, err
	}
	return openVSS(ctx, roots)
}

var procCoInitializeSecurity = windows.NewLazySystemDLL("ole32.dll").NewProc("CoInitializeSecurity")

const (
	rpcCAuthnLevelPktPrivacy = 6
	rpcCImpLevelIdentify     = 2
	eoacNone                 = 0
)

// initCOMSecurity sets process-wide COM security so VSS writers, which run as
// other accounts, can call back into this requester. Once per process: if
// anything already set it (RPC_E_TOO_LATE), that setting stands. The spec
// places this in main.go (§4.1); here it runs only when a backup does.
func initCOMSecurity() {
	if procCoInitializeSecurity.Find() != nil {
		return
	}
	// Any failure, RPC_E_TOO_LATE included, leaves the process's existing
	// setting in force; the writer checks report what that costs.
	procCoInitializeSecurity.Call(0, ^uintptr(0), 0, 0, //nolint:errcheck
		rpcCAuthnLevelPktPrivacy, rpcCImpLevelIdentify, 0, eoacNone, 0)
}
