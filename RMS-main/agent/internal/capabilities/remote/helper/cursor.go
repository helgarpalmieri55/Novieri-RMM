package helper

import (
	"bytes"
	"image"
	"image/color"
	"image/png"
)

// Pointer-shape types, as DXGI_OUTDUPL_POINTER_SHAPE_TYPE numbers them.
const (
	ShapeMonochrome  = 1
	ShapeColor       = 2
	ShapeMaskedColor = 4
)

// maxCursorSide bounds what a capturer may hand over; real cursors are 32-256.
const maxCursorSide = 256

// DecodePointerShape converts an OS pointer image to NRGBA. For monochrome
// shapes buf holds the AND mask above the XOR mask, so h is the visible
// height (half the bitmap's). Pixels the OS would invert against the screen
// are drawn black: a canvas cannot XOR, and black is what an inverted I-beam
// looks like on the white documents it usually sits over.
func DecodePointerShape(kind, w, h, pitch int, buf []byte) (*image.NRGBA, bool) {
	if w <= 0 || h <= 0 || w > maxCursorSide || h > maxCursorSide || pitch <= 0 {
		return nil, false
	}
	img := image.NewNRGBA(image.Rect(0, 0, w, h))
	switch kind {
	case ShapeMonochrome:
		if pitch*h*2 > len(buf) || pitch*8 < w {
			return nil, false
		}
		bit := func(row, x int) bool { return buf[row*pitch+x/8]&(0x80>>(x%8)) != 0 }
		for y := 0; y < h; y++ {
			for x := 0; x < w; x++ {
				and, xor := bit(y, x), bit(y+h, x)
				switch {
				case and && !xor:
					// transparent
				case !and && xor:
					img.SetNRGBA(x, y, color.NRGBA{255, 255, 255, 255})
				default:
					img.SetNRGBA(x, y, color.NRGBA{0, 0, 0, 255})
				}
			}
		}
	case ShapeColor, ShapeMaskedColor:
		if pitch*h > len(buf) || pitch < w*4 {
			return nil, false
		}
		for y := 0; y < h; y++ {
			row := buf[y*pitch:]
			for x := 0; x < w; x++ {
				b, g, r, a := row[x*4], row[x*4+1], row[x*4+2], row[x*4+3]
				if kind == ShapeMaskedColor {
					// Alpha is a mask: 0 replaces the screen, 0xFF XORs it.
					if a == 0 {
						a = 255
					} else if r|g|b == 0 {
						a = 0
					} else {
						r, g, b, a = 0, 0, 0, 255
					}
				}
				img.SetNRGBA(x, y, color.NRGBA{r, g, b, a})
			}
		}
	default:
		return nil, false
	}
	return img, true
}

// EncodeCursorPNG is the CURSOR_SHAPE payload (codec 2).
func EncodeCursorPNG(img *image.NRGBA) []byte {
	var buf bytes.Buffer
	_ = (&png.Encoder{CompressionLevel: png.BestSpeed}).Encode(&buf, img)
	return buf.Bytes()
}
