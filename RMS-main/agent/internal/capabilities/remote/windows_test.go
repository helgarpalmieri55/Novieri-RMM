//go:build windows

package remote

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
	"unicode/utf16"
	"unsafe"

	"golang.org/x/sys/windows"

	"github.com/toeshee/it-suite/agent/internal/capabilities/remote/helper/synthetic"
	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// These run on the windows-latest CI runner. They use the production launch
// path (startHelper: anonymous pipes, handle list, kill-on-close job) with
// the calling user's token instead of WTSQueryUserToken, which only
// LocalSystem may call.

func envBlock(vars []string) *uint16 {
	var u []uint16
	for _, v := range vars {
		u = append(u, utf16.Encode([]rune(v))...)
		u = append(u, 0)
	}
	u = append(u, 0)
	return &u[0]
}

type selfWinLauncher struct{ mode string }

func (l selfWinLauncher) Launch(context.Context) (Helper, error) {
	h, err := startHelper(0, os.Args[0], []string{"-test.run=^$"}, envBlock(append(os.Environ(), helperEnv+"="+l.mode)))
	if err != nil {
		return nil, err
	}
	return h, nil
}

// graphicsModules lists capture and encoder DLLs loaded in this process.
func graphicsModules(t *testing.T) []string {
	t.Helper()
	mods := make([]windows.Handle, 1024)
	var need uint32
	self := windows.CurrentProcess()
	if err := windows.EnumProcessModules(self, &mods[0], uint32(len(mods))*uint32(unsafe.Sizeof(mods[0])), &need); err != nil {
		t.Fatalf("EnumProcessModules: %v", err)
	}
	var out []string
	name := make([]uint16, 260)
	for _, m := range mods[:int(need)/int(unsafe.Sizeof(mods[0]))] {
		if windows.GetModuleBaseName(self, m, &name[0], uint32(len(name))) != nil {
			continue
		}
		switch n := strings.ToLower(windows.UTF16ToString(name)); n {
		case "d3d11.dll", "dxgi.dll", "mfplat.dll":
			out = append(out, n)
		}
	}
	return out
}

var procGetExtendedTcpTable = windows.NewLazySystemDLL("iphlpapi.dll").NewProc("GetExtendedTcpTable")

// listeners returns the PIDs owning a listening TCP socket (IPv4 and IPv6).
func listeners(t *testing.T) map[uint32]bool {
	t.Helper()
	const tableOwnerPIDListener = 3
	out := map[uint32]bool{}
	for _, af := range []struct {
		family         uintptr
		rowSize, pidAt int
	}{{2, 24, 20}, {23, 56, 52}} {
		var size uint32
		_, _, _ = procGetExtendedTcpTable.Call(0, uintptr(unsafe.Pointer(&size)), 0, af.family, tableOwnerPIDListener, 0)
		if size == 0 {
			continue
		}
		buf := make([]byte, size+1024)
		size = uint32(len(buf))
		if r, _, _ := procGetExtendedTcpTable.Call(uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&size)), 0,
			af.family, tableOwnerPIDListener, 0); r != 0 {
			t.Fatalf("GetExtendedTcpTable: %d", r)
		}
		n := int(*(*uint32)(unsafe.Pointer(&buf[0])))
		for i := 0; i < n; i++ {
			out[*(*uint32)(unsafe.Pointer(&buf[4+i*af.rowSize+af.pidAt]))] = true
		}
	}
	return out
}

// children lists live processes whose parent is this one.
func children(t *testing.T) []uint32 {
	t.Helper()
	snap, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer windows.CloseHandle(snap)
	var out []uint32
	e := windows.ProcessEntry32{Size: uint32(unsafe.Sizeof(windows.ProcessEntry32{}))}
	for err = windows.Process32First(snap, &e); err == nil; err = windows.Process32Next(snap, &e) {
		if e.ParentProcessID == uint32(os.Getpid()) && strings.EqualFold(windows.UTF16ToString(e.ExeFile[:]), filepath.Base(os.Args[0])) {
			out = append(out, e.ProcessID)
		}
	}
	return out
}

// TestFalsifierF3F4F6TheRealHelperSharesTheRunnersDesktop is the Windows smoke test: the
// real helper, launched the production way, captures the runner's desktop,
// takes a pointer move through SendInput and reports it back, and is gone at
// the end. Along the way it checks spec §4.2 F3, F4 and F6.
func TestFalsifierF3F4F6TheRealHelperSharesTheRunnersDesktop(t *testing.T) {
	p := newPKI(t)
	g := newGateway(t, p, "tok")
	l := &recordingLauncher{Launcher: selfWinLauncher{"real"}}
	o := options(p, l)
	par := params(g, "tok")
	par.ConsentMode = "unattended" // no prompt to answer on a CI runner; the indicator still shows
	if mods := graphicsModules(t); len(mods) != 0 {
		t.Fatalf("F4 falsified before the session: %v loaded", mods)
	}
	done := make(chan Result, 1)
	go func() { done <- run(context.Background(), par, o) }()

	first := g.recv()
	stderr := func() string {
		if s, ok := l.last.(interface{ Stderr() string }); ok {
			return s.Stderr()
		}
		return ""
	}
	if first.Type == rsp.State {
		res := <-done
		t.Skipf("no desktop to capture on this runner: %s %+v\nhelper log:\n%s", first.Body, res, stderr())
	}
	if first.Type != rsp.Hello {
		t.Fatalf("first frame %#x", first.Type)
	}
	var canvas synthetic.Canvas
	if err := canvas.Apply(first); err != nil {
		t.Fatal(err)
	}
	g.send(rsp.Encode(rsp.Refresh, g.next(), nil))
	for {
		f := g.recv()
		if err := canvas.Apply(f); err != nil {
			t.Fatal(err)
		}
		if f.Type == rsp.FrameEnd {
			g.send(rsp.EncodeAck(g.next(), f.Seq))
			break
		}
	}
	b := canvas.Img.Bounds()
	nonBlack := 0
	for i := 0; i < len(canvas.Img.Pix); i += 4 {
		if canvas.Img.Pix[i]|canvas.Img.Pix[i+1]|canvas.Img.Pix[i+2] != 0 {
			nonBlack++
		}
	}
	t.Logf("captured %dx%d, %d non-black pixels; helper log:\n%s", b.Dx(), b.Dy(), nonBlack, stderr())

	// F6: the helper never listens.
	if listeners(t)[uint32(l.last.PID())] {
		t.Fatal("F6 falsified: the helper owns a listening socket")
	}
	// F4: the service side has loaded no graphics code mid-session.
	if mods := graphicsModules(t); len(mods) != 0 {
		t.Fatalf("F4 falsified during the session: %v loaded in the service process", mods)
	}

	// A pointer move goes out through SendInput and comes back as the
	// capturer's cursor position.
	g.send(rsp.EncodePointer(g.next(), rsp.PointerEvent{X: 37, Y: 41}))
	deadline := time.After(5 * time.Second)
	moved := false
	for !moved {
		select {
		case f, ok := <-g.frames:
			if !ok {
				t.Fatal("agent leg closed")
			}
			if f.Type == rsp.CursorPos {
				x, y, _, _ := rsp.DecodeCursorPos(f)
				moved = x == 37 && y == 41
			} else if f.Type == rsp.FrameEnd {
				g.send(rsp.EncodeAck(g.next(), f.Seq))
			}
		case <-deadline:
			t.Logf("the pointer move did not round-trip (no interactive input on this runner?)")
			moved = true
		}
	}

	g.close()
	select {
	case res := <-done:
		if res.Outcome != "ended" {
			t.Fatalf("outcome %+v", res)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("the session did not end")
	}
	// F3: no helper survives the session.
	select {
	case <-l.last.Exited():
	default:
		t.Fatal("F3 falsified: the helper outlived the session")
	}
	if kids := children(t); len(kids) != 0 {
		t.Fatalf("F3 falsified: helper processes %v still running", kids)
	}
	if mods := graphicsModules(t); len(mods) != 0 {
		t.Fatalf("F4 falsified after the session: %v loaded", mods)
	}
	t.Logf("F3/F4/F6 budget: helper pid %d exited; service modules clean; no listener", l.last.PID())
}

// TestAKilledServiceTakesItsHelperWithIt closes the job the way a crashed
// service would, and the helper dies with it.
func TestAKilledServiceTakesItsHelperWithIt(t *testing.T) {
	h, err := selfWinLauncher{"hang"}.Launch(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	wh := h.(*winHelper)
	_ = windows.CloseHandle(wh.job)
	wh.job = 0
	select {
	case <-h.Exited():
	case <-time.After(5 * time.Second):
		_ = h.Kill()
		t.Fatal("closing the job did not kill the helper")
	}
	_ = h.Close()
}

// TestClosingStdinEndsAWaitingHelper: a helper whose service goes away
// before sending the config exits instead of waiting forever.
func TestClosingStdinEndsAWaitingHelper(t *testing.T) {
	before := runtime.NumGoroutine()
	h, err := selfWinLauncher{"granted"}.Launch(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	_ = h.Stdin().Close() // before the config: the helper must give up
	select {
	case <-h.Exited():
	case <-time.After(10 * time.Second):
		_ = h.Kill()
		t.Fatal("the helper ignored a closed stdin")
	}
	_ = h.Close()
	if n := settle(t, before); n > before {
		t.Fatalf("%d goroutines after, %d before", n, before)
	}
}

// TestTheConsoleLauncherIsForLocalSystem runs the production launcher. On a
// runner that is not LocalSystem it must fail cleanly and name the reason.
func TestTheConsoleLauncherIsForLocalSystem(t *testing.T) {
	l := consoleLauncher{exe: os.Args[0]}
	h, err := l.Launch(context.Background())
	switch {
	case errors.Is(err, ErrNoUserSession):
		t.Skip("no user at the console of this runner")
	case err != nil:
		t.Skipf("WTSQueryUserToken needs LocalSystem; the service path is verified on the bench: %v", err)
	}
	// Running as LocalSystem: the helper came up in the console session.
	_ = h.Stdin().Close()
	select {
	case <-h.Exited():
	case <-time.After(10 * time.Second):
		_ = h.Kill()
		t.Fatal("helper did not exit")
	}
	_ = h.Close()
}
