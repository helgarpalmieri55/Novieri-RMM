package helper

import (
	"bytes"
	"image"
	"image/jpeg"
	"runtime"
	"sync"

	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// TileSize is the diff grid. Changed tiles in a row are merged into runs of
// up to maxRunTiles before JPEG encoding, so a changed line of text is one
// message rather than eight.
const (
	TileSize    = 64
	maxRunTiles = 8
)

// Tile is one TILE frame's content.
type Tile struct {
	Rect  rsp.Rect
	Codec byte
	Data  []byte
}

// Copy is one COPYRECT frame's content.
type Copy struct {
	SrcX, SrcY uint16
	Dst        rsp.Rect
}

// Encoded is one screen update: copies first, then tiles, in raster order.
type Encoded struct {
	Copies []Copy
	Tiles  []Tile
}

// Empty reports whether there is nothing to send.
func (e Encoded) Empty() bool { return len(e.Copies) == 0 && len(e.Tiles) == 0 }

// Tiler turns captured frames into dirty-rect tiles: it keeps the image the
// viewer has, compares each new frame against it inside the capturer's dirty
// hints, sends solid tiles as fills and everything else as JPEG.
type Tiler struct {
	w, h    int
	prev    []byte // BGRA, stride w*4: what the viewer shows after the last update
	valid   bool
	workers int
	scratch []*scratch
}

type scratch struct {
	rgba *image.RGBA
	buf  bytes.Buffer
}

// NewTiler returns a tiler that encodes with up to workers goroutines, all
// joined before Encode returns.
func NewTiler(workers int) *Tiler {
	if workers < 1 {
		workers = 1
	}
	if n := runtime.NumCPU(); workers > n {
		workers = n
	}
	t := &Tiler{workers: workers}
	for i := 0; i < workers; i++ {
		t.scratch = append(t.scratch, &scratch{rgba: image.NewRGBA(image.Rect(0, 0, TileSize*maxRunTiles, TileSize))})
	}
	return t
}

// Snapshot is a copy of what the viewer should be showing, or nil before the
// first frame. It lets REFRESH resend the screen without waiting for a change.
func (t *Tiler) Snapshot() *Frame {
	if !t.valid {
		return nil
	}
	return &Frame{Width: t.w, Height: t.h, Stride: t.w * 4, Pix: append([]byte(nil), t.prev...)}
}

// Invalidate makes the next Encode send the whole frame (REFRESH, monitor
// switch, capture reopened).
func (t *Tiler) Invalidate() { t.valid = false }

type job struct {
	r    image.Rectangle
	tile Tile
}

// Encode compares f against the viewer's image and returns what changed. On
// full (or after Invalidate, or a size change) every tile is sent and moves
// are ignored.
func (t *Tiler) Encode(f *Frame, dirty []image.Rectangle, moves []Move, full bool, quality int) Encoded {
	if f.Width != t.w || f.Height != t.h || t.prev == nil {
		t.w, t.h = f.Width, f.Height
		t.prev = make([]byte, t.w*t.h*4)
		t.valid = false
	}
	if !t.valid {
		full = true
	}
	bounds := image.Rect(0, 0, t.w, t.h)
	var out Encoded
	if !full {
		for _, m := range moves {
			if c, ok := t.applyMove(m, bounds); ok {
				out.Copies = append(out.Copies, c)
			}
			// Compare the destination whether or not the copy applied: a copy
			// that could not be expressed must still reach the viewer as tiles.
			if dirty != nil {
				dirty = append(dirty[:len(dirty):len(dirty)], m.Dst)
			}
		}
	}

	cols := (t.w + TileSize - 1) / TileSize
	rows := (t.h + TileSize - 1) / TileSize
	changed := make([]bool, cols*rows)
	solid := make([]int32, cols*rows) // packed RGB+1, 0 = not solid
	t.parallel(rows, func(row, _ int) {
		for col := 0; col < cols; col++ {
			r := tileRect(col, row, bounds)
			if !full && dirty != nil && !intersectsAny(r, dirty) {
				continue
			}
			if full || !t.equal(f, r) {
				changed[row*cols+col] = true
				if c, ok := solidColor(f, r); ok {
					solid[row*cols+col] = c + 1
				}
			}
		}
	})

	var jobs []job
	for row := 0; row < rows; row++ {
		for col := 0; col < cols; {
			i := row*cols + col
			if !changed[i] {
				col++
				continue
			}
			if solid[i] != 0 {
				c := solid[i] - 1
				jobs = append(jobs, job{tile: Tile{
					Rect: toRect(tileRect(col, row, bounds)), Codec: rsp.CodecFill,
					Data: []byte{byte(c >> 16), byte(c >> 8), byte(c)},
				}})
				col++
				continue
			}
			start := col
			for col < cols && col-start < maxRunTiles && changed[row*cols+col] && solid[row*cols+col] == 0 {
				col++
			}
			r := tileRect(start, row, bounds).Union(tileRect(col-1, row, bounds))
			jobs = append(jobs, job{r: r, tile: Tile{Rect: toRect(r), Codec: rsp.CodecJPEG}})
		}
	}

	t.parallel(len(jobs), func(i, w int) {
		if jobs[i].tile.Codec == rsp.CodecJPEG {
			jobs[i].tile.Data = t.jpeg(f, jobs[i].r, quality, t.scratch[w])
		}
	})
	for _, j := range jobs {
		out.Tiles = append(out.Tiles, j.tile)
	}
	for row := 0; row < rows; row++ {
		for col := 0; col < cols; col++ {
			if changed[row*cols+col] {
				t.store(f, tileRect(col, row, bounds))
			}
		}
	}
	t.valid = true
	return out
}

// parallel runs fn(i, worker) for i in [0, n) on up to t.workers goroutines
// and returns when all are done.
func (t *Tiler) parallel(n int, fn func(i, worker int)) {
	workers := t.workers
	if workers > n {
		workers = n
	}
	if workers <= 1 {
		for i := 0; i < n; i++ {
			fn(i, 0)
		}
		return
	}
	var wg sync.WaitGroup
	var mu sync.Mutex
	next := 0
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for {
				mu.Lock()
				i := next
				next++
				mu.Unlock()
				if i >= n {
					return
				}
				fn(i, w)
			}
		}(w)
	}
	wg.Wait()
}

func (t *Tiler) applyMove(m Move, bounds image.Rectangle) (Copy, bool) {
	dst := m.Dst.Intersect(bounds)
	src := image.Rectangle{Min: m.Src, Max: m.Src.Add(m.Dst.Size())}
	// Keep src and dst the same size after clipping either side.
	off := dst.Min.Sub(m.Dst.Min)
	src.Min = src.Min.Add(off)
	src.Max = src.Min.Add(dst.Size())
	if c := src.Intersect(bounds); c != src || dst.Empty() {
		return Copy{}, false
	}
	stride := t.w * 4
	rowBytes := dst.Dx() * 4
	if dst.Min.Y > src.Min.Y {
		for y := dst.Dy() - 1; y >= 0; y-- {
			copy(t.prev[(dst.Min.Y+y)*stride+dst.Min.X*4:][:rowBytes], t.prev[(src.Min.Y+y)*stride+src.Min.X*4:][:rowBytes])
		}
	} else {
		for y := 0; y < dst.Dy(); y++ {
			copy(t.prev[(dst.Min.Y+y)*stride+dst.Min.X*4:][:rowBytes], t.prev[(src.Min.Y+y)*stride+src.Min.X*4:][:rowBytes])
		}
	}
	return Copy{SrcX: uint16(src.Min.X), SrcY: uint16(src.Min.Y), Dst: toRect(dst)}, true
}

func (t *Tiler) equal(f *Frame, r image.Rectangle) bool {
	n := r.Dx() * 4
	for y := r.Min.Y; y < r.Max.Y; y++ {
		if !bytes.Equal(f.Pix[y*f.Stride+r.Min.X*4:][:n], t.prev[y*t.w*4+r.Min.X*4:][:n]) {
			return false
		}
	}
	return true
}

func (t *Tiler) store(f *Frame, r image.Rectangle) {
	n := r.Dx() * 4
	for y := r.Min.Y; y < r.Max.Y; y++ {
		copy(t.prev[y*t.w*4+r.Min.X*4:][:n], f.Pix[y*f.Stride+r.Min.X*4:][:n])
	}
}

// jpeg encodes r of f. The alpha channel is ignored; the desktop is opaque.
func (t *Tiler) jpeg(f *Frame, r image.Rectangle, quality int, s *scratch) []byte {
	img := s.rgba
	img.Rect = image.Rect(0, 0, r.Dx(), r.Dy())
	img.Stride = r.Dx() * 4
	for y := 0; y < r.Dy(); y++ {
		src := f.Pix[(r.Min.Y+y)*f.Stride+r.Min.X*4:][:r.Dx()*4]
		dst := img.Pix[y*img.Stride:][:r.Dx()*4]
		for i := 0; i < len(src); i += 4 {
			dst[i], dst[i+1], dst[i+2], dst[i+3] = src[i+2], src[i+1], src[i], 0xFF
		}
	}
	s.buf.Reset()
	_ = jpeg.Encode(&s.buf, img, &jpeg.Options{Quality: quality})
	return append([]byte(nil), s.buf.Bytes()...)
}

// solidColor returns the RGB of r if every pixel in it is the same colour.
func solidColor(f *Frame, r image.Rectangle) (int32, bool) {
	base := f.Pix[r.Min.Y*f.Stride+r.Min.X*4:]
	b, g, rd := base[0], base[1], base[2]
	for y := r.Min.Y; y < r.Max.Y; y++ {
		row := f.Pix[y*f.Stride+r.Min.X*4:][:r.Dx()*4]
		for i := 0; i < len(row); i += 4 {
			if row[i] != b || row[i+1] != g || row[i+2] != rd {
				return 0, false
			}
		}
	}
	return int32(rd)<<16 | int32(g)<<8 | int32(b), true
}

func tileRect(col, row int, bounds image.Rectangle) image.Rectangle {
	return image.Rect(col*TileSize, row*TileSize, (col+1)*TileSize, (row+1)*TileSize).Intersect(bounds)
}

func intersectsAny(r image.Rectangle, rs []image.Rectangle) bool {
	for _, d := range rs {
		if r.Overlaps(d) {
			return true
		}
	}
	return false
}

func toRect(r image.Rectangle) rsp.Rect {
	return rsp.Rect{X: uint16(r.Min.X), Y: uint16(r.Min.Y), W: uint16(r.Dx()), H: uint16(r.Dy())}
}

// qualityFor maps a QUALITY level to a JPEG quality.
func qualityFor(level byte) int {
	switch level {
	case rsp.QualityLow:
		return 40
	case rsp.QualityMedium:
		return 60
	case rsp.QualityHigh:
		return 85
	default:
		return 70
	}
}
