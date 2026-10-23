package helper

import (
	"context"
	"errors"
	"fmt"
	"image"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"

	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// DXGI Desktop Duplication through raw COM vtables: no cgo, no new module.
// Vtable slots are the interface's position in the SDK headers (IUnknown 0-2,
// IDXGIObject 3-6, then the interface's own methods in declaration order).
const (
	slotFactoryEnumAdapters = 7  // IDXGIFactory::EnumAdapters
	slotAdapterEnumOutputs  = 7  // IDXGIAdapter::EnumOutputs
	slotOutputGetDesc       = 7  // IDXGIOutput::GetDesc
	slotOutput1Duplicate    = 22 // IDXGIOutput1::DuplicateOutput
	slotDuplGetDesc         = 7  // IDXGIOutputDuplication::GetDesc
	slotDuplAcquire         = 8  // ::AcquireNextFrame
	slotDuplDirtyRects      = 9  // ::GetFrameDirtyRects
	slotDuplMoveRects       = 10 // ::GetFrameMoveRects
	slotDuplPointerShape    = 11 // ::GetFramePointerShape
	slotDuplReleaseFrame    = 14 // ::ReleaseFrame
	slotDeviceCreateTex2D   = 5  // ID3D11Device::CreateTexture2D
	slotContextMap          = 14 // ID3D11DeviceContext::Map
	slotContextUnmap        = 15 // ID3D11DeviceContext::Unmap
	slotContextCopyResource = 47 // ID3D11DeviceContext::CopyResource
)

const (
	dxgiErrorNotFound          = 0x887A0002
	dxgiErrorAccessLost        = 0x887A0026
	dxgiErrorWaitTimeout       = 0x887A0027
	dxgiErrorSessionDisconnect = 0x887A0028
	eAccessDenied              = 0x80070005

	d3dDriverTypeUnknown   = 0
	d3d11CreateBGRASupport = 0x20
	d3d11SDKVersion        = 7
	dxgiFormatB8G8R8A8     = 87
	d3d11UsageStaging      = 3
	d3d11CPUAccessRead     = 0x20000
	d3d11MapRead           = 1
	dxgiRotationIdentity   = 1
)

var (
	iidDXGIFactory1   = windows.GUID{Data1: 0x770aae78, Data2: 0xf26f, Data3: 0x4dba, Data4: [8]byte{0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87}}
	iidDXGIOutput1    = windows.GUID{Data1: 0x00cddea8, Data2: 0x939b, Data3: 0x4b83, Data4: [8]byte{0xa3, 0x40, 0xa6, 0x85, 0x22, 0x66, 0x66, 0xcc}}
	iidD3D11Texture2D = windows.GUID{Data1: 0x6f15aaf2, Data2: 0xd208, Data3: 0x4e89, Data4: [8]byte{0x9a, 0xb4, 0x48, 0x95, 0x35, 0xd3, 0x4f, 0x9c}}
)

type outputDesc struct {
	DeviceName [32]uint16
	Desktop    rect
	Attached   int32
	Rotation   uint32
	Monitor    uintptr
}

type duplDesc struct {
	Width, Height, RefreshNum, RefreshDen, Format, Scanline, Scaling uint32
	Rotation                                                         uint32
	InSystemMemory                                                   int32
}

type frameInfo struct {
	LastPresentTime         int64
	LastMouseUpdateTime     int64
	AccumulatedFrames       uint32
	RectsCoalesced          int32
	ProtectedContentMasked  int32
	PointerX, PointerY      int32
	PointerVisible          int32
	TotalMetadataBufferSize uint32
	PointerShapeBufferSize  uint32
}

type texture2DDesc struct {
	Width, Height, MipLevels, ArraySize, Format uint32
	SampleCount, SampleQuality                  uint32
	Usage, BindFlags, CPUAccess, Misc           uint32
}

type mappedSubresource struct {
	Data       unsafe.Pointer
	RowPitch   uint32
	DepthPitch uint32
}

type moveRect struct {
	Src point
	Dst rect
}

type shapeInfo struct {
	Type, Width, Height, Pitch uint32
	HotSpot                    point
}

type dxgiCapturer struct {
	device, ctx, dup, tex com
	w, h                  int
	frame                 Frame
	acquired              bool
	meta                  []byte
	shape                 []byte
}

func openDXGI(m rsp.Monitor) (c *dxgiCapturer, err error) {
	if !has(procCreateDXGIFactory1, procD3D11CreateDevice) {
		return nil, errProc
	}
	var f unsafe.Pointer
	if r, _, _ := procCreateDXGIFactory1.Call(uintptr(unsafe.Pointer(&iidDXGIFactory1)), uintptr(unsafe.Pointer(&f))); failed(r) {
		return nil, fmt.Errorf("CreateDXGIFactory1: %w", hresult(r))
	}
	factory := com{f}
	defer factory.release()

	for ai := uintptr(0); ; ai++ {
		var a unsafe.Pointer
		r := factory.call(slotFactoryEnumAdapters, ai, uintptr(unsafe.Pointer(&a)))
		if uint32(r) == dxgiErrorNotFound {
			return nil, errors.New("no DXGI output covers the monitor")
		}
		if failed(r) {
			return nil, fmt.Errorf("EnumAdapters: %w", hresult(r))
		}
		adapter := com{a}
		c, err := openOnAdapter(adapter, m)
		adapter.release()
		if c != nil || err != nil {
			return c, err
		}
	}
}

// openOnAdapter duplicates the output of adapter that shows m, or returns
// (nil, nil) when this adapter has no such output. The device is created on
// that adapter, which hybrid-GPU laptops require.
func openOnAdapter(adapter com, m rsp.Monitor) (*dxgiCapturer, error) {
	for oi := uintptr(0); ; oi++ {
		var o unsafe.Pointer
		r := adapter.call(slotAdapterEnumOutputs, oi, uintptr(unsafe.Pointer(&o)))
		if uint32(r) == dxgiErrorNotFound {
			return nil, nil
		}
		if failed(r) {
			return nil, fmt.Errorf("EnumOutputs: %w", hresult(r))
		}
		output := com{o}
		var d outputDesc
		output.call(slotOutputGetDesc, uintptr(unsafe.Pointer(&d)))
		match := d.Attached != 0 && int(d.Desktop.Left) == m.X && int(d.Desktop.Top) == m.Y &&
			int(d.Desktop.Right-d.Desktop.Left) == m.Width && int(d.Desktop.Bottom-d.Desktop.Top) == m.Height
		if !match {
			output.release()
			continue
		}
		defer output.release()
		if d.Rotation > dxgiRotationIdentity {
			return nil, errors.New("rotated output; duplication frames would need rotating")
		}
		return duplicate(adapter, output)
	}
}

func duplicate(adapter, output com) (c *dxgiCapturer, err error) {
	c = &dxgiCapturer{}
	defer func() {
		if err != nil {
			_ = c.Close()
			c = nil
		}
	}()
	var dev, ctx unsafe.Pointer
	r, _, _ := procD3D11CreateDevice.Call(uintptr(adapter.p), d3dDriverTypeUnknown, 0, d3d11CreateBGRASupport,
		0, 0, d3d11SDKVersion, uintptr(unsafe.Pointer(&dev)), 0, uintptr(unsafe.Pointer(&ctx)))
	if failed(r) {
		return c, fmt.Errorf("D3D11CreateDevice: %w", hresult(r))
	}
	c.device, c.ctx = com{dev}, com{ctx}

	out1, err := output.query(&iidDXGIOutput1)
	if err != nil {
		return c, fmt.Errorf("IDXGIOutput1: %w", err)
	}
	defer out1.release()
	var dup unsafe.Pointer
	r = out1.call(slotOutput1Duplicate, uintptr(c.device.p), uintptr(unsafe.Pointer(&dup)))
	switch {
	case uint32(r) == eAccessDenied || uint32(r) == dxgiErrorSessionDisconnect:
		return c, ErrDesktopUnavailable
	case failed(r):
		return c, fmt.Errorf("DuplicateOutput: %w", hresult(r))
	}
	c.dup = com{dup}

	var dd duplDesc
	c.dup.call(slotDuplGetDesc, uintptr(unsafe.Pointer(&dd)))
	c.w, c.h = int(dd.Width), int(dd.Height)
	td := texture2DDesc{Width: dd.Width, Height: dd.Height, MipLevels: 1, ArraySize: 1, Format: dxgiFormatB8G8R8A8,
		SampleCount: 1, Usage: d3d11UsageStaging, CPUAccess: d3d11CPUAccessRead}
	var tex unsafe.Pointer
	if r := c.device.call(slotDeviceCreateTex2D, uintptr(unsafe.Pointer(&td)), 0, uintptr(unsafe.Pointer(&tex))); failed(r) {
		return c, fmt.Errorf("CreateTexture2D: %w", hresult(r))
	}
	c.tex = com{tex}
	c.frame = Frame{Width: c.w, Height: c.h, Stride: c.w * 4, Pix: make([]byte, c.w*c.h*4)}
	return c, nil
}

func (c *dxgiCapturer) Kind() string { return "dxgi" }

func (c *dxgiCapturer) Close() error {
	if c.acquired {
		c.dup.call(slotDuplReleaseFrame)
		c.acquired = false
	}
	c.tex.release()
	c.dup.release()
	c.ctx.release()
	c.device.release()
	c.tex, c.dup, c.ctx, c.device = com{}, com{}, com{}, com{}
	return nil
}

// Next blocks in AcquireNextFrame, which returns when the desktop or the
// pointer changes, or at the timeout: nothing polls.
func (c *dxgiCapturer) Next(ctx context.Context, timeout time.Duration) (Update, error) {
	if c.acquired {
		c.dup.call(slotDuplReleaseFrame)
		c.acquired = false
	}
	if err := ctx.Err(); err != nil {
		return Update{}, err
	}
	var fi frameInfo
	var res unsafe.Pointer
	r := c.dup.call(slotDuplAcquire, uintptr(timeout/time.Millisecond), uintptr(unsafe.Pointer(&fi)), uintptr(unsafe.Pointer(&res)))
	switch {
	case uint32(r) == dxgiErrorWaitTimeout:
		return Update{}, nil
	case uint32(r) == dxgiErrorAccessLost || uint32(r) == eAccessDenied:
		return Update{}, ErrReopen
	case failed(r):
		return Update{}, fmt.Errorf("AcquireNextFrame: %w", hresult(r))
	}
	c.acquired = true
	resource := com{res}
	defer resource.release()

	var u Update
	if fi.LastMouseUpdateTime != 0 {
		u.Cursor = &CursorState{X: int(fi.PointerX), Y: int(fi.PointerY), Visible: fi.PointerVisible != 0}
	}
	if fi.PointerShapeBufferSize > 0 {
		u.Shape = c.pointerShape(fi.PointerShapeBufferSize)
	}
	if fi.LastPresentTime == 0 {
		return u, nil // only the pointer moved
	}
	moves, dirty, ok := c.metadata(fi.TotalMetadataBufferSize)
	if !ok {
		dirty = nil // unknown: compare everything
	}
	tex, err := resource.query(&iidD3D11Texture2D)
	if err != nil {
		return Update{}, fmt.Errorf("frame texture: %w", err)
	}
	c.ctx.call(slotContextCopyResource, uintptr(c.tex.p), uintptr(tex.p))
	tex.release()
	var mapped mappedSubresource
	if r := c.ctx.call(slotContextMap, uintptr(c.tex.p), 0, d3d11MapRead, 0, uintptr(unsafe.Pointer(&mapped))); failed(r) {
		return Update{}, fmt.Errorf("map the staging texture: %w", hresult(r))
	}
	src := unsafe.Slice((*byte)(mapped.Data), int(mapped.RowPitch)*c.h)
	row := c.w * 4
	for y := 0; y < c.h; y++ {
		copy(c.frame.Pix[y*row:][:row], src[y*int(mapped.RowPitch):][:row])
	}
	c.ctx.call(slotContextUnmap, uintptr(c.tex.p), 0)
	u.Frame, u.Dirty, u.Moves = &c.frame, dirty, moves
	return u, nil
}

func (c *dxgiCapturer) metadata(size uint32) ([]Move, []image.Rectangle, bool) {
	if size == 0 {
		return nil, nil, false
	}
	if uint32(len(c.meta)) < size {
		c.meta = make([]byte, size)
	}
	var used uint32
	var moves []Move
	if r := c.dup.call(slotDuplMoveRects, uintptr(len(c.meta)), uintptr(unsafe.Pointer(&c.meta[0])), uintptr(unsafe.Pointer(&used))); failed(r) {
		return nil, nil, false
	}
	n := int(used) / int(unsafe.Sizeof(moveRect{}))
	for _, mr := range unsafe.Slice((*moveRect)(unsafe.Pointer(&c.meta[0])), n) {
		moves = append(moves, Move{Src: image.Pt(int(mr.Src.X), int(mr.Src.Y)),
			Dst: image.Rect(int(mr.Dst.Left), int(mr.Dst.Top), int(mr.Dst.Right), int(mr.Dst.Bottom))})
	}
	dirty := []image.Rectangle{}
	if r := c.dup.call(slotDuplDirtyRects, uintptr(len(c.meta)), uintptr(unsafe.Pointer(&c.meta[0])), uintptr(unsafe.Pointer(&used))); failed(r) {
		return nil, nil, false
	}
	n = int(used) / int(unsafe.Sizeof(rect{}))
	for _, d := range unsafe.Slice((*rect)(unsafe.Pointer(&c.meta[0])), n) {
		dirty = append(dirty, image.Rect(int(d.Left), int(d.Top), int(d.Right), int(d.Bottom)))
	}
	return moves, dirty, true
}

func (c *dxgiCapturer) pointerShape(size uint32) *CursorShape {
	if uint32(len(c.shape)) < size {
		c.shape = make([]byte, size)
	}
	var used uint32
	var si shapeInfo
	r := c.dup.call(slotDuplPointerShape, uintptr(len(c.shape)), uintptr(unsafe.Pointer(&c.shape[0])),
		uintptr(unsafe.Pointer(&used)), uintptr(unsafe.Pointer(&si)))
	if failed(r) {
		return nil
	}
	h := int(si.Height)
	if si.Type == ShapeMonochrome {
		h /= 2
	}
	img, ok := DecodePointerShape(int(si.Type), int(si.Width), h, int(si.Pitch), c.shape[:used])
	if !ok {
		return nil
	}
	return &CursorShape{HotX: int(si.HotSpot.X), HotY: int(si.HotSpot.Y), Image: img}
}
