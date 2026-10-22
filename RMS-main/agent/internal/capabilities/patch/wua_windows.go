//go:build windows

package patch

import (
	"context"
	"errors"
	"fmt"
	"runtime"
	"syscall"

	"golang.org/x/sys/windows"
)

// The Windows engine: the Windows Update Agent, in process.
//
// # NOTHING IN THIS FILE HAS EVER EXECUTED
//
// See com_windows.go's comment. `go vet`/`go build` with GOOS=windows type-check
// it; no runner in this repository can do more, and
// docs/modules/patch/agent-capabilities.md §6 says so rather than letting a
// green Linux suite imply otherwise. Everything about Windows patching that is
// a DECISION rather than a syscall was moved into winupdate.go, which is
// build-tag-free and fully exercised on Linux.
//
// # No child process, at all
//
// Not `powershell.exe`, not `UsoClient.exe`, not `wusa.exe`. The first is the
// loudest thing a SYSTEM process can do on a machine with EDR; the second
// cannot report a per-update outcome (it is fire-and-forget and returns
// immediately, so an agent built on it would have to invent the results the
// wire contract asks for); the third installs a .msu file this agent does not
// have and has no business downloading itself. The COM API is the only one of
// the four that can both enumerate and report.
//
// # What the job context can and cannot do here
//
// COM calls are SYNCHRONOUS and take no cancellation token. A WUA search
// against a slow WSUS can take minutes and the job's ctx cannot interrupt it;
// the deadline is honoured BETWEEN updates, not inside a call. That is a real
// limitation and it is stated rather than papered over: the alternative --
// running the search on another goroutine and abandoning it -- would leave a
// COM apartment and its interface pointers alive with nothing owning them,
// which is exactly the resident cost the idle budget forbids.

// WUA ProgIDs. Compile-time constants; nothing job-supplied is ever passed to
// createObject.
const (
	progIDSession    = "Microsoft.Update.Session"
	progIDUpdateColl = "Microsoft.Update.UpdateColl"
	progIDSystemInfo = "Microsoft.Update.SystemInfo"
)

// wuaSearchCriteria is ONE search covering both halves of the scan.
//
// `IsHidden=0` is NOT in the criteria even though it could be: the hidden flag
// is read per update and filtered in winupdate.go, where the decision is
// visible and tested, rather than hidden in a string only a Windows machine can
// evaluate.
const wuaSearchCriteria = "IsInstalled=0 or IsInstalled=1"

// maxWUAResults bounds one search. A machine joined to a WSUS with a decade of
// approved updates can return thousands; the scan document is capped at
// maxPatches anyway, and this stops the COM enumeration itself from being the
// unbounded part.
const maxWUAResults = 2048

// IInstallationResult.ResultCode / IDownloadResult.ResultCode
// (OperationResultCode). 2 is orcSucceeded, 3 is orcSucceededWithErrors.
const (
	orcSucceeded           = 2
	orcSucceededWithErrors = 3
)

type wuaEngine struct {
	session *iDispatch
	// updates maps `<source>:<identifier>` to the IUpdate the scan produced.
	// Install looks its target up HERE and nowhere else, which is what makes
	// "the job's identifier is only ever a map key" true on Windows too.
	updates map[string]*iDispatch
	// installedCache is the installed half of the SAME search Available ran.
	// It is a []string of `<source>:<identifier>` and not a set of COM
	// pointers: nothing is ever installed from it, so nothing needs to be
	// retained for it.
	installedCache []string
	// rebootPending records what this job's own installs reported.
	rebootPending bool
	// comInitialized and threadLocked track what Close has to undo.
	comInitialized bool
	threadLocked   bool
}

// newWUAEngine initialises COM and creates the update session.
//
// runtime.LockOSThread is not optional. A COM apartment belongs to a THREAD,
// and Go moves goroutines between threads freely; without the lock, the second
// COM call in a job could run on a thread that never called CoInitializeEx and
// would fail with CO_E_NOTINITIALIZED. Every method on this engine is called
// from the one goroutine that ran the job, so locking here and unlocking in
// Close covers the whole apartment's life.
func newWUAEngine() (*wuaEngine, error) {
	e := &wuaEngine{updates: map[string]*iDispatch{}}

	runtime.LockOSThread()
	e.threadLocked = true

	if err := coInitialize(); err != nil {
		e.Close()
		// REFUSED, not failed: a machine whose COM apartment cannot be entered
		// in the mode this agent needs will not start being able to on a retry.
		return nil, errNoEngine("the COM apartment could not be initialised: " + err.Error())
	}
	e.comInitialized = true

	session, err := createObject(progIDSession)
	if err != nil {
		e.Close()
		// Also refused. The Windows Update Agent is a component of Windows; a
		// machine where it is not registered has had it removed or broken, and
		// that is a permanent incapacity from this agent's point of view.
		return nil, errNoEngine("the Windows Update Agent is not available: " + err.Error())
	}
	e.session = session
	return e, nil
}

// coInitialize enters an STA, tolerating an apartment that already exists.
//
// S_FALSE (1) means "this thread was already initialised", which is success.
// The generated wrapper reports any non-zero HRESULT as an error, so the
// distinction has to be made here -- and getting it wrong would mean a second
// patch job on a re-used thread refusing for no reason.
func coInitialize() error {
	err := windows.CoInitializeEx(0, coinitApartmentThreaded|coinitDisableOLE1DDE)
	if err == nil {
		return nil
	}
	var errno syscall.Errno
	if errors.As(err, &errno) && uint32(errno) == 1 { // S_FALSE
		return nil
	}
	return err
}

func (e *wuaEngine) Family() string { return "windows" }

func (e *wuaEngine) Close() {
	for _, u := range e.updates {
		u.release()
	}
	e.updates = nil
	if e.session != nil {
		e.session.release()
		e.session = nil
	}
	if e.comInitialized {
		windows.CoUninitialize()
		e.comInitialized = false
	}
	if e.threadLocked {
		runtime.UnlockOSThread()
		e.threadLocked = false
	}
}

// Available runs the one search and flattens its results.
func (e *wuaEngine) Available(ctx context.Context) ([]entry, error) {
	searcher, err := e.session.getObject("CreateUpdateSearcher")
	if err != nil {
		return nil, fmt.Errorf("CreateUpdateSearcher: %w", err)
	}
	defer searcher.release()

	searchResult, err := searcher.getObject("Search", wuaSearchCriteria)
	if err != nil {
		// A plain error -> `failed`. This is the transient case: an unreachable
		// WSUS, a stopped Windows Update service, a metered connection. None of
		// them mean "this agent will never do this".
		return nil, fmt.Errorf("the update search failed: %w", err)
	}
	defer searchResult.release()

	collection, err := searchResult.getObject("Updates")
	if err != nil {
		return nil, fmt.Errorf("the search result carried no update collection: %w", err)
	}
	defer collection.release()

	count, err := collection.getInt("Count")
	if err != nil {
		return nil, fmt.Errorf("the update collection has no Count: %w", err)
	}
	if count > maxWUAResults {
		count = maxWUAResults
	}

	flattened := make([]wuaUpdate, 0, count)
	// keep holds the COM pointer for each flattened update, in the same order,
	// so the retained set can be chosen AFTER winupdate.go has decided which
	// ones are installable.
	keep := make([]*iDispatch, 0, count)

	for i := int32(0); i < count; i++ {
		if err := ctx.Err(); err != nil {
			// The deadline is honoured BETWEEN updates. See the file comment
			// for why it cannot be honoured inside a COM call.
			releaseAll(keep)
			return nil, err
		}
		item, err := collection.getObject("Item", i)
		if err != nil {
			continue
		}
		flattened = append(flattened, readWUAUpdate(item))
		keep = append(keep, item)
	}

	available, installed := partitionWindowsUpdates(flattened)
	e.installedCache = installed

	// Retain exactly the interfaces Install could need, release the rest. A
	// scan of a fully patched machine therefore holds nothing at all.
	wanted := make(map[string]bool, len(available))
	for _, a := range available {
		wanted[a.key()] = true
	}
	for i, u := range flattened {
		e2, ok := windowsEntry(u)
		if ok && wanted[e2.key()] && e.updates[e2.key()] == nil {
			e.updates[e2.key()] = keep[i]
			continue
		}
		keep[i].release()
	}
	return available, nil
}

// Installed returns what the same search already told us. It runs NO second
// search: see partitionWindowsUpdates for why one pass is the whole design.
func (e *wuaEngine) Installed(ctx context.Context) ([]string, error) {
	return e.installedCache, nil
}

// Install downloads and installs ONE update that Available produced.
//
// The update is looked up by key in a map this engine built from WUA's own
// enumeration. There is no search-by-name, no criteria built from the job, and
// no string from the signed envelope reaching any COM call.
func (e *wuaEngine) Install(ctx context.Context, ent entry) error {
	update := e.updates[ent.key()]
	if update == nil {
		return fmt.Errorf("this device is not being offered that update")
	}

	if accepted, err := update.getBool("EulaAccepted"); err == nil && !accepted {
		// Accepting on the operator's behalf is what the signed job authorizes:
		// approving a patch for a ring IS the acceptance decision, and there is
		// nobody at the console of a machine being patched at 03:00.
		if err := update.callMethod("AcceptEula"); err != nil {
			return fmt.Errorf("the update's licence terms could not be accepted: %w", err)
		}
	}

	coll, err := createObject(progIDUpdateColl)
	if err != nil {
		return fmt.Errorf("an update collection could not be created: %w", err)
	}
	defer coll.release()
	if err := coll.callMethod("Add", update); err != nil {
		return fmt.Errorf("the update could not be added to the install set: %w", err)
	}

	if err := e.download(coll); err != nil {
		return err
	}
	return e.install(coll)
}

func (e *wuaEngine) download(coll *iDispatch) error {
	downloader, err := e.session.getObject("CreateUpdateDownloader")
	if err != nil {
		return fmt.Errorf("CreateUpdateDownloader: %w", err)
	}
	defer downloader.release()

	if err := downloader.putRef("Updates", coll); err != nil {
		return fmt.Errorf("the download set could not be assigned: %w", err)
	}
	result, err := downloader.getObject("Download")
	if err != nil {
		return fmt.Errorf("the download failed: %w", err)
	}
	defer result.release()

	return checkOperationResult(result, "download")
}

func (e *wuaEngine) install(coll *iDispatch) error {
	installer, err := e.session.getObject("CreateUpdateInstaller")
	if err != nil {
		return fmt.Errorf("CreateUpdateInstaller: %w", err)
	}
	defer installer.release()

	if err := installer.putRef("Updates", coll); err != nil {
		return fmt.Errorf("the install set could not be assigned: %w", err)
	}
	// Best-effort: both properties arrived in later IUpdateInstaller versions
	// and an older WUA answers DISP_E_UNKNOWNNAME. Failing the install over a
	// property that only makes it quieter would be the wrong trade.
	_ = installer.putBool("ForceQuiet", true)
	_ = installer.putBool("AllowSourcePrompts", false)

	result, err := installer.getObject("Install")
	if err != nil {
		return fmt.Errorf("the install failed: %w", err)
	}
	defer result.release()

	if reboot, err := result.getBool("RebootRequired"); err == nil && reboot {
		e.rebootPending = true
	}
	return checkOperationResult(result, "install")
}

// checkOperationResult turns an OperationResultCode into an error or nil.
//
// orcSucceededWithErrors is treated as SUCCESS for this update, because the
// "with errors" refers to other members of the collection and this collection
// has exactly one member. The HResult is carried into the message when it is
// not: it is the number a Windows administrator will search for.
func checkOperationResult(result *iDispatch, what string) error {
	code, err := result.getInt("ResultCode")
	if err != nil {
		return fmt.Errorf("the %s reported no result code: %w", what, err)
	}
	if code == orcSucceeded || code == orcSucceededWithErrors {
		return nil
	}
	hres, _ := result.getInt("HResult")
	return fmt.Errorf("the %s did not succeed (result code %d, HRESULT 0x%08x)",
		what, code, uint32(hres))
}

// RebootRequired asks Windows, and falls back on what the installs reported.
//
// `Microsoft.Update.SystemInfo`'s RebootRequired is the machine-wide answer and
// the one that stays true across a job boundary; IInstallationResult's is about
// this install. Either being true is enough.
func (e *wuaEngine) RebootRequired(ctx context.Context) bool {
	if e.rebootPending {
		return true
	}
	sysInfo, err := createObject(progIDSystemInfo)
	if err != nil {
		return false
	}
	defer sysInfo.release()
	required, err := sysInfo.getBool("RebootRequired")
	if err != nil {
		return false
	}
	return required
}

// readWUAUpdate flattens one IUpdate. Every read is tolerant: a property this
// build of WUA does not expose leaves its field zero rather than failing the
// whole scan.
func readWUAUpdate(item *iDispatch) wuaUpdate {
	u := wuaUpdate{}
	u.Title, _ = item.getString("Title")
	u.MsrcSeverity, _ = item.getString("MsrcSeverity")
	u.IsInstalled, _ = item.getBool("IsInstalled")
	u.IsHidden, _ = item.getBool("IsHidden")
	u.BrowseOnly, _ = item.getBool("BrowseOnly")
	u.RebootRequired, _ = item.getBool("RebootRequired")
	u.KBArticleIDs = item.stringCollection("KBArticleIDs")
	u.CveIDs = item.stringCollection("CveIDs")
	if identity, err := item.getObject("Identity"); err == nil {
		u.UpdateID, _ = identity.getString("UpdateID")
		identity.release()
	}
	return u
}

func releaseAll(objs []*iDispatch) {
	for _, o := range objs {
		o.release()
	}
}
