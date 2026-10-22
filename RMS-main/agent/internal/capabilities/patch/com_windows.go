//go:build windows

package patch

import (
	"fmt"
	"strings"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Minimal in-process COM, and NOTHING ELSE.
//
// # Why this file exists at all
//
// The alternative to it is `powershell.exe -Command "Get-WindowsUpdate ..."`,
// and that alternative is unacceptable on the fleet this agent ships to. A
// process running as SYSTEM that spawns PowerShell is among the loudest
// behavioural signals an endpoint can emit: it is what Defender's ASR rule
// "Block process creations originating from PSExec and WMI commands" and most
// third-party EDR behavioural chains are built to catch, and an RMM agent that
// trips those on every patch window is an RMM agent that gets uninstalled by
// the security team. Calling the Windows Update Agent's COM API in-process is
// what Windows Update itself does; it spawns nothing, writes no script to disk,
// and reads to an EDR as ordinary use of a documented API.
//
// # THIS FILE HAS NEVER EXECUTED
//
// Nothing here has run. This repository's CI is Linux, there is no Windows
// runner, and there is no Windows Update Agent to run it against. `go vet` and
// `go build` with GOOS=windows type-check it and that is the whole of the
// verification it has -- which is a real check (a wrong vtable INDEX compiles
// fine; a wrong SIGNATURE does not) and a small one.
// docs/modules/patch/agent-capabilities.md §6 lists this plainly rather than
// letting a green Linux suite imply otherwise.
//
// # Scope
//
// Late binding through IDispatch only: GetIDsOfNames then Invoke. No custom
// vtable offsets into IUpdateSession, IUpdateSearcher or IUpdateInstaller, and
// that is deliberate -- a wrong vtable index is a silent call into a different
// method with the wrong arguments, on a machine nobody here can debug, running
// as SYSTEM. A wrong METHOD NAME through IDispatch is a clean DISP_E_UNKNOWNNAME
// at runtime with the name in the message.

var (
	modole32    = windows.NewLazySystemDLL("ole32.dll")
	modoleaut32 = windows.NewLazySystemDLL("oleaut32.dll")

	procCoCreateInstance = modole32.NewProc("CoCreateInstance")
	procCLSIDFromProgID  = modole32.NewProc("CLSIDFromProgID")

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

	dispatchMethod         = 0x1
	dispatchPropertyGet    = 0x2
	dispatchPropertyPut    = 0x4
	dispatchPropertyPutRef = 0x8

	dispidPropertyPut = -3

	vtEmpty    = 0
	vtNull     = 1
	vtI2       = 2
	vtI4       = 3
	vtBSTR     = 8
	vtDispatch = 9
	vtBool     = 11
	vtVariant  = 12
	vtUnknown  = 13
	vtI8       = 20
	vtInt      = 22
	vtUI4      = 19

	localeSystemDefault = 0x0800

	varTrue  = -1 // VARIANT_TRUE is 0xFFFF, i.e. -1 as an int16
	varFalse = 0
)

// iidNull and iidIDispatch. GUIDFromString is used rather than a hand-written
// composite literal because the byte order of a GUID's last two fields is a
// classic transcription error and this way the compiler is not the only reader.
var (
	iidNull      = windows.GUID{}
	iidIDispatch = mustGUID("{00020400-0000-0000-C000-000000000046}")
)

func mustGUID(s string) windows.GUID {
	g, err := windows.GUIDFromString(s)
	if err != nil {
		// A malformed constant in this file. Panicking at first use is correct:
		// there is no recovery and no configuration involved.
		panic("patch: malformed GUID constant " + s)
	}
	return g
}

// ---------------------------------------------------------------------------
// IDispatch
// ---------------------------------------------------------------------------

type iDispatchVtbl struct {
	QueryInterface   uintptr
	AddRef           uintptr
	Release          uintptr
	GetTypeInfoCount uintptr
	GetTypeInfo      uintptr
	GetIDsOfNames    uintptr
	Invoke           uintptr
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
		syscall.SyscallN(d.vtbl.Release, uintptr(unsafe.Pointer(d)))
	}
}

// dispID resolves a member name once per call. WUA objects are small and this
// path runs a few hundred times per scan at most, so the cache that would
// normally go here is not worth the state it would add to a capability whose
// whole point is to hold nothing.
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
	if failed(hr) {
		return 0, fmt.Errorf("the Windows Update Agent has no member %q (0x%08x)", name, uint32(hr))
	}
	return id, nil
}

// invoke calls a member.
//
// Arguments are pushed in REVERSE ORDER, which is DISPPARAMS' contract and the
// single easiest thing to get wrong here. A two-argument call with the order
// swapped does not fail; it does something else.
//
// The caller owns the returned variant and must clear it.
func (d *iDispatch) invoke(name string, flags uint16, args ...any) (variant, error) {
	var result variant

	id, err := d.dispID(name)
	if err != nil {
		return result, err
	}

	vargs := make([]variant, len(args))
	// Every BSTR allocated for this call is freed on the way out, whatever
	// happens. A leaked BSTR is process-lifetime memory in an agent whose whole
	// claim is that it returns to baseline.
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
	namedPut := int32(dispidPropertyPut)
	if flags&(dispatchPropertyPut|dispatchPropertyPutRef) != 0 {
		params.rgdispidNamedArgs = &namedPut
		params.cNamedArgs = 1
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
	if failed(hr) {
		// The EXCEPINFO description is where the Windows Update Agent puts the
		// sentence a human can act on ("The service cannot be started", "0x
		// 8024402C"), so it is carried through into the outcome's `error`
		// rather than discarded in favour of a bare HRESULT.
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

func failed(hr uintptr) bool { return int32(hr) < 0 }

// comPtr reinterprets a VARIANT's union payload as a pointer.
//
// `unsafe.Pointer(uintptr(x))` is what this means, and `go vet` rejects that
// spelling for a reason that does not apply here: a uintptr naming a GO HEAP
// address can go stale between the two conversions if the collector moves the
// object, so vet insists on a single expression. THESE ARE NOT GO POINTERS. A
// BSTR comes from SysAllocString and an interface pointer from the COM runtime;
// both live in memory Go's collector neither moves nor frees, so there is no
// window in which the value can go stale.
//
// Storing them in a Go pointer-typed field instead -- the change that would
// make vet happy the obvious way -- is the ACTUALLY dangerous option: the
// collector would then scan a foreign address as if it were a Go pointer.
func comPtr(v int64) unsafe.Pointer {
	u := uintptr(v)
	return *(*unsafe.Pointer)(unsafe.Pointer(&u))
}

// ---------------------------------------------------------------------------
// Variants
// ---------------------------------------------------------------------------

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
	case int:
		return variant{vt: vtI4, val: int64(int32(v))}, nil
	case bool:
		if v {
			return variant{vt: vtBool, val: varTrue}, nil
		}
		return variant{vt: vtBool, val: varFalse}, nil
	case *iDispatch:
		return variant{vt: vtDispatch, val: int64(uintptr(unsafe.Pointer(v)))}, nil
	default:
		return variant{}, fmt.Errorf("patch: %T is not a COM argument this agent constructs", a)
	}
}

func clearVariant(v *variant) {
	if v == nil || v.vt == vtEmpty {
		return
	}
	syscall.SyscallN(procVariantClear.Addr(), uintptr(unsafe.Pointer(v)))
	v.vt = vtEmpty
	v.val = 0
}

// asString reads a variant expected to hold a BSTR. A VT_NULL or VT_EMPTY is
// the empty string and not an error: WUA leaves MsrcSeverity empty on most
// updates, and treating that as a failure would fail the scan on an ordinary
// machine.
func asString(v variant) string {
	switch v.vt {
	case vtBSTR:
		return bstrToString((*uint16)(comPtr(v.val)))
	case vtEmpty, vtNull:
		return ""
	}
	return ""
}

func asInt32(v variant) int32 {
	switch v.vt {
	case vtI2:
		return int32(int16(v.val))
	case vtI4, vtInt, vtUI4, vtI8:
		return int32(v.val)
	case vtBool:
		if v.val != 0 {
			return 1
		}
		return 0
	}
	return 0
}

func asBool(v variant) bool {
	switch v.vt {
	case vtBool:
		return v.val != 0
	case vtI2, vtI4, vtInt, vtUI4, vtI8:
		return v.val != 0
	}
	return false
}

// asDispatch takes ownership of an interface pointer the callee returned. The
// caller releases it.
func asDispatch(v variant) *iDispatch {
	if v.vt != vtDispatch && v.vt != vtUnknown {
		return nil
	}
	if v.val == 0 {
		return nil
	}
	return (*iDispatch)(comPtr(v.val))
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
	if r == 0 {
		return nil, fmt.Errorf("patch: SysAllocString returned NULL")
	}
	return (*uint16)(comPtr(int64(r))), nil
}

func freeBSTR(b *uint16) {
	if b != nil {
		syscall.SyscallN(procSysFreeString.Addr(), uintptr(unsafe.Pointer(b)))
	}
}

// bstrToString reads a BSTR by its LENGTH PREFIX and not by scanning for a NUL.
//
// A BSTR may legitimately contain an embedded NUL, and an update title from a
// hostile or merely broken WSUS is not a thing to trust a terminator from.
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

// createObject is CoCreateInstance by ProgID.
//
// ProgID rather than a hard-coded CLSID because the ProgIDs
// ("Microsoft.Update.Session") are the documented, stable surface and a
// mistyped CLSID is a 16-byte constant nobody can review. The ProgID strings
// are compile-time constants in this package -- never anything from a job.
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
	if failed(hr) {
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
	if failed(hr) || obj == nil {
		return nil, fmt.Errorf("CoCreateInstance(%s) failed (0x%08x)", progID, uint32(hr))
	}
	return obj, nil
}

// ---------------------------------------------------------------------------
// Small helpers over invoke
// ---------------------------------------------------------------------------

// getProperty reads a property and clears the variant for the scalar cases.
func (d *iDispatch) getString(name string) (string, error) {
	v, err := d.invoke(name, dispatchPropertyGet)
	if err != nil {
		return "", err
	}
	defer clearVariant(&v)
	return asString(v), nil
}

func (d *iDispatch) getInt(name string) (int32, error) {
	v, err := d.invoke(name, dispatchPropertyGet)
	if err != nil {
		return 0, err
	}
	defer clearVariant(&v)
	return asInt32(v), nil
}

func (d *iDispatch) getBool(name string) (bool, error) {
	v, err := d.invoke(name, dispatchPropertyGet)
	if err != nil {
		return false, err
	}
	defer clearVariant(&v)
	return asBool(v), nil
}

// getObject returns an interface the CALLER must release. The variant itself is
// deliberately NOT cleared: clearing a VT_DISPATCH releases the interface, so
// doing both would return a pointer whose refcount already went to zero.
func (d *iDispatch) getObject(name string, args ...any) (*iDispatch, error) {
	v, err := d.invoke(name, dispatchPropertyGet|dispatchMethod, args...)
	if err != nil {
		return nil, err
	}
	obj := asDispatch(v)
	if obj == nil {
		return nil, fmt.Errorf("%s returned no object", name)
	}
	return obj, nil
}

// callMethod invokes a method whose result is discarded.
func (d *iDispatch) callMethod(name string, args ...any) error {
	v, err := d.invoke(name, dispatchMethod, args...)
	if err != nil {
		return err
	}
	clearVariant(&v)
	return nil
}

// putRef assigns an INTERFACE-valued property (`installer.Updates = coll`).
// PROPERTYPUTREF and not PROPERTYPUT: the latter would ask the callee to copy a
// value, which is not what an IUpdateCollection assignment means.
func (d *iDispatch) putRef(name string, value *iDispatch) error {
	v, err := d.invoke(name, dispatchPropertyPutRef, value)
	if err != nil {
		return err
	}
	clearVariant(&v)
	return nil
}

// putBool assigns a scalar property (`installer.ForceQuiet = true`).
func (d *iDispatch) putBool(name string, value bool) error {
	v, err := d.invoke(name, dispatchPropertyPut, value)
	if err != nil {
		return err
	}
	clearVariant(&v)
	return nil
}

// stringCollection reads an IStringCollection property (KBArticleIDs, CveIDs)
// into a Go slice, releasing everything it touched.
//
// An ABSENT property is not an error: CveIDs does not exist on the IUpdate
// interface of older WUA builds, and a scan that failed on a Windows Server
// 2016 box because it could not read a CVE list would be a scan that reports
// nothing at all about a machine that needs patching.
func (d *iDispatch) stringCollection(name string) []string {
	coll, err := d.getObject(name)
	if err != nil || coll == nil {
		return nil
	}
	defer coll.release()

	count, err := coll.getInt("Count")
	if err != nil || count <= 0 {
		return nil
	}
	const maxCollection = 256
	if count > maxCollection {
		count = maxCollection
	}
	out := make([]string, 0, count)
	for i := int32(0); i < count; i++ {
		v, err := coll.invoke("Item", dispatchPropertyGet|dispatchMethod, i)
		if err != nil {
			continue
		}
		s := asString(v)
		clearVariant(&v)
		if s != "" {
			out = append(out, s)
		}
	}
	return out
}
