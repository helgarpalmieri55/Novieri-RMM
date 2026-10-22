package synthetic

import (
	"bytes"
	"fmt"
	"image"
	"image/jpeg"

	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// Canvas applies agent->viewer frames the way the browser viewer does: the
// update between FRAME_BEGIN and FRAME_END lands at FRAME_END. Tests compare
// it with the screen that was captured.
type Canvas struct {
	Img       *image.RGBA
	pending   []rsp.Frame
	inFrame   bool
	Frames    int
	LastEnd   uint32
	CursorX   int
	CursorY   int
	CursorVis bool
	Hello     *rsp.HelloBody
	States    []rsp.StateBody
}

// Apply feeds one frame.
func (c *Canvas) Apply(f rsp.Frame) error {
	switch f.Type {
	case rsp.Hello:
		var h rsp.HelloBody
		if err := rsp.DecodeJSON(f, &h); err != nil {
			return err
		}
		c.Hello = &h
		for _, m := range h.Monitors {
			if m.ID == h.Selected {
				c.Img = image.NewRGBA(image.Rect(0, 0, m.Width, m.Height))
			}
		}
	case rsp.State:
		var s rsp.StateBody
		if err := rsp.DecodeJSON(f, &s); err != nil {
			return err
		}
		c.States = append(c.States, s)
	case rsp.FrameBegin:
		c.inFrame, c.pending = true, nil
	case rsp.Tile, rsp.CopyRect:
		if !c.inFrame {
			return fmt.Errorf("%#x outside a frame", f.Type)
		}
		c.pending = append(c.pending, rsp.Frame{Type: f.Type, Seq: f.Seq, Body: append([]byte(nil), f.Body...)})
	case rsp.FrameEnd:
		if !c.inFrame || c.Img == nil {
			return fmt.Errorf("FRAME_END without FRAME_BEGIN or HELLO")
		}
		for _, p := range c.pending {
			if err := c.draw(p); err != nil {
				return err
			}
		}
		c.inFrame, c.pending = false, nil
		c.Frames++
		c.LastEnd = f.Seq
	case rsp.CursorPos:
		x, y, vis, err := rsp.DecodeCursorPos(f)
		if err != nil {
			return err
		}
		c.CursorX, c.CursorY, c.CursorVis = int(x), int(y), vis
	}
	return nil
}

func (c *Canvas) draw(f rsp.Frame) error {
	if f.Type == rsp.CopyRect {
		sx, sy, d, err := rsp.DecodeCopyRect(f)
		if err != nil {
			return err
		}
		src := image.NewRGBA(image.Rect(0, 0, int(d.W), int(d.H)))
		for y := 0; y < int(d.H); y++ {
			copy(src.Pix[y*src.Stride:][:int(d.W)*4], c.Img.Pix[(int(sy)+y)*c.Img.Stride+int(sx)*4:][:int(d.W)*4])
		}
		for y := 0; y < int(d.H); y++ {
			copy(c.Img.Pix[(int(d.Y)+y)*c.Img.Stride+int(d.X)*4:][:int(d.W)*4], src.Pix[y*src.Stride:][:int(d.W)*4])
		}
		return nil
	}
	r, codec, data, err := rsp.DecodeTile(f)
	if err != nil {
		return err
	}
	switch codec {
	case rsp.CodecFill:
		if len(data) != 3 {
			return fmt.Errorf("fill with %d bytes", len(data))
		}
		for y := int(r.Y); y < int(r.Y+r.H); y++ {
			for x := int(r.X); x < int(r.X+r.W); x++ {
				i := y*c.Img.Stride + x*4
				c.Img.Pix[i], c.Img.Pix[i+1], c.Img.Pix[i+2], c.Img.Pix[i+3] = data[0], data[1], data[2], 255
			}
		}
	case rsp.CodecJPEG:
		img, err := jpeg.Decode(bytes.NewReader(data))
		if err != nil {
			return err
		}
		if b := img.Bounds(); b.Dx() != int(r.W) || b.Dy() != int(r.H) {
			return fmt.Errorf("jpeg %v for a %dx%d tile", b, r.W, r.H)
		}
		for y := 0; y < int(r.H); y++ {
			for x := 0; x < int(r.W); x++ {
				cr, cg, cb, _ := img.At(x, y).RGBA()
				i := (int(r.Y)+y)*c.Img.Stride + (int(r.X)+x)*4
				c.Img.Pix[i], c.Img.Pix[i+1], c.Img.Pix[i+2], c.Img.Pix[i+3] = uint8(cr>>8), uint8(cg>>8), uint8(cb>>8), 255
			}
		}
	default:
		return fmt.Errorf("unknown codec %d", codec)
	}
	return nil
}

// MeanError is the mean absolute per-channel difference between the canvas
// and a BGRA frame, 0 for identical images.
func (c *Canvas) MeanError(pix []byte, stride int) float64 {
	var sum, n float64
	b := c.Img.Rect
	for y := 0; y < b.Dy(); y++ {
		for x := 0; x < b.Dx(); x++ {
			i := y*c.Img.Stride + x*4
			j := y*stride + x*4
			for k, v := range [3]byte{pix[j+2], pix[j+1], pix[j]} {
				d := float64(c.Img.Pix[i+k]) - float64(v)
				if d < 0 {
					d = -d
				}
				sum += d
				n++
			}
		}
	}
	return sum / n
}
