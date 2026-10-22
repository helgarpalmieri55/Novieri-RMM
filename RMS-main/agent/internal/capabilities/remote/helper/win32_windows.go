package helper

import (
	"errors"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Win32 entry points the helper uses. All are resolved lazily, so nothing is
// loaded until a session runs, and all of it lives in the helper process: the
// service never imports this package.
var (
	user32   = windows.NewLazySystemDLL("user32.dll")
	gdi32    = windows.NewLazySystemDLL("gdi32.dll")
	shcore   = windows.NewLazySystemDLL("shcore.dll")
	d3d11dll = windows.NewLazySystemDLL("d3d11.dll")
	dxgidll  = windows.NewLazySystemDLL("dxgi.dll")
	kernel32 = windows.NewLazySystemDLL("kernel32.dll")

	procSendInput                     = user32.NewProc("SendInput")
	procGetSystemMetrics              = user32.NewProc("GetSystemMetrics")
	procEnumDisplayMonitors           = user32.NewProc("EnumDisplayMonitors")
	procGetMonitorInfoW               = user32.NewProc("GetMonitorInfoW")
	procSetProcessDpiAwarenessContext = user32.NewProc("SetProcessDpiAwarenessContext")
	procSetProcessDPIAware            = user32.NewProc("SetProcessDPIAware")
	procGetDpiForSystem               = user32.NewProc("GetDpiForSystem")
	procGetDC                         = user32.NewProc("GetDC")
	procReleaseDC                     = user32.NewProc("ReleaseDC")
	procGetCursorInfo                 = user32.NewProc("GetCursorInfo")
	procRegisterClassExW              = user32.NewProc("RegisterClassExW")
	procCreateWindowExW               = user32.NewProc("CreateWindowExW")
	procDestroyWindow                 = user32.NewProc("DestroyWindow")
	procDefWindowProcW                = user32.NewProc("DefWindowProcW")
	procGetMessageW                   = user32.NewProc("GetMessageW")
	procTranslateMessage              = user32.NewProc("TranslateMessage")
	procDispatchMessageW              = user32.NewProc("DispatchMessageW")
	procPostMessageW                  = user32.NewProc("PostMessageW")
	procPostQuitMessage               = user32.NewProc("PostQuitMessage")
	procSendMessageW                  = user32.NewProc("SendMessageW")
	procShowWindow                    = user32.NewProc("ShowWindow")
	procSetWindowPos                  = user32.NewProc("SetWindowPos")
	procSetWindowTextW                = user32.NewProc("SetWindowTextW")
	procSetTimer                      = user32.NewProc("SetTimer")
	procKillTimer                     = user32.NewProc("KillTimer")
	procSetForegroundWindow           = user32.NewProc("SetForegroundWindow")
	procSetFocus                      = user32.NewProc("SetFocus")
	procEnableWindow                  = user32.NewProc("EnableWindow")
	procSetWindowDisplayAffinity      = user32.NewProc("SetWindowDisplayAffinity")
	procSetLayeredWindowAttributes    = user32.NewProc("SetLayeredWindowAttributes")
	procLoadCursorW                   = user32.NewProc("LoadCursorW")
	procIsDialogMessageW              = user32.NewProc("IsDialogMessageW")
	procFlashWindow                   = user32.NewProc("FlashWindow")

	procGetDpiForMonitor = shcore.NewProc("GetDpiForMonitor")

	procCreateCompatibleDC = gdi32.NewProc("CreateCompatibleDC")
	procCreateDIBSection   = gdi32.NewProc("CreateDIBSection")
	procSelectObject       = gdi32.NewProc("SelectObject")
	procDeleteObject       = gdi32.NewProc("DeleteObject")
	procDeleteDC           = gdi32.NewProc("DeleteDC")
	procBitBlt             = gdi32.NewProc("BitBlt")
	procCreateSolidBrush   = gdi32.NewProc("CreateSolidBrush")
	procCreateFontW        = gdi32.NewProc("CreateFontW")
	procSetTextColor       = gdi32.NewProc("SetTextColor")
	procSetBkMode          = gdi32.NewProc("SetBkMode")

	procD3D11CreateDevice  = d3d11dll.NewProc("D3D11CreateDevice")
	procCreateDXGIFactory1 = dxgidll.NewProc("CreateDXGIFactory1")

	procGetModuleHandleW = kernel32.NewProc("GetModuleHandleW")
)

type rect struct{ Left, Top, Right, Bottom int32 }

type point struct{ X, Y int32 }

// hresult reports a failed HRESULT as an error.
type hresult uint32

func (h hresult) Error() string { return "HRESULT 0x" + hex32(uint32(h)) }

func hex32(v uint32) string {
	const digits = "0123456789ABCDEF"
	b := make([]byte, 8)
	for i := 7; i >= 0; i-- {
		b[i] = digits[v&0xF]
		v >>= 4
	}
	return string(b)
}

func failed(r uintptr) bool { return int32(uint32(r)) < 0 }

// com is a COM interface pointer.
type com struct{ p unsafe.Pointer }

// call invokes vtable slot i with the object as the first argument.
func (c com) call(i int, args ...uintptr) uintptr {
	vtbl := *(**[64]uintptr)(c.p)
	a := append([]uintptr{uintptr(c.p)}, args...)
	r, _, _ := syscall.SyscallN(vtbl[i], a...)
	return r
}

func (c com) release() {
	if c.p != nil {
		c.call(2)
	}
}

func (c com) query(iid *windows.GUID) (com, error) {
	var out unsafe.Pointer
	if r := c.call(0, uintptr(unsafe.Pointer(iid)), uintptr(unsafe.Pointer(&out))); failed(r) {
		return com{}, hresult(r)
	}
	return com{out}, nil
}

var errProc = errors.New("helper: a required Windows function is missing")

// has reports whether every proc resolves, so a missing API is an error, not
// a panic.
func has(procs ...*windows.LazyProc) bool {
	for _, p := range procs {
		if p.Find() != nil {
			return false
		}
	}
	return true
}

func wstr(s string) *uint16 {
	p, _ := windows.UTF16PtrFromString(s)
	return p
}
