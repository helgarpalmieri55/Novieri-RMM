package inventory

import (
	"bytes"
	"encoding/binary"
	"encoding/xml"
	"errors"
	"unicode/utf16"
)

// A minimal property-list reader: the TOP-LEVEL STRING values of a plist whose
// root is a dictionary, in either format macOS writes (XML or bplist00).
// That is all an Info.plist lookup needs; nested containers and non-string
// values are skipped. No cgo and no dependency: the module graph has no plist
// package, and this is small enough to own and fuzz-test (macsoftware_test.go).

var errNotPlist = errors.New("inventory: not a property list with a dictionary root")

func parsePlist(data []byte) (map[string]string, error) {
	if bytes.HasPrefix(data, []byte("bplist0")) {
		return parseBinaryPlist(data)
	}
	return parseXMLPlist(data)
}

func parseXMLPlist(data []byte) (map[string]string, error) {
	dec := xml.NewDecoder(bytes.NewReader(data))
	// Find the root dictionary: the first element inside <plist>.
	inPlist := false
	for {
		tok, err := dec.Token()
		if err != nil {
			return nil, errNotPlist
		}
		start, ok := tok.(xml.StartElement)
		if !ok {
			continue
		}
		if !inPlist {
			if start.Name.Local != "plist" {
				return nil, errNotPlist
			}
			inPlist = true
			continue
		}
		if start.Name.Local != "dict" {
			return nil, errNotPlist
		}
		break
	}
	out := map[string]string{}
	for {
		tok, err := dec.Token()
		if err != nil {
			return nil, errNotPlist
		}
		switch el := tok.(type) {
		case xml.EndElement:
			return out, nil // </dict>
		case xml.StartElement:
			if el.Name.Local != "key" {
				return nil, errNotPlist
			}
			key, err := xmlText(dec)
			if err != nil {
				return nil, errNotPlist
			}
			value, err := nextStart(dec)
			if err != nil {
				return nil, errNotPlist
			}
			if value.Name.Local == "string" {
				text, err := xmlText(dec)
				if err != nil {
					return nil, errNotPlist
				}
				out[key] = text
			} else if err := dec.Skip(); err != nil {
				return nil, errNotPlist
			}
		}
	}
}

// xmlText reads character data up to the current element's end tag.
func xmlText(dec *xml.Decoder) (string, error) {
	var b bytes.Buffer
	for {
		tok, err := dec.Token()
		if err != nil {
			return "", err
		}
		switch t := tok.(type) {
		case xml.CharData:
			b.Write(t)
		case xml.EndElement:
			return b.String(), nil
		case xml.StartElement:
			return "", errNotPlist
		}
	}
}

func nextStart(dec *xml.Decoder) (xml.StartElement, error) {
	for {
		tok, err := dec.Token()
		if err != nil {
			return xml.StartElement{}, err
		}
		switch t := tok.(type) {
		case xml.StartElement:
			return t, nil
		case xml.EndElement:
			return xml.StartElement{}, errNotPlist
		}
	}
}

// bplist is a bplist00 document with its trailer validated.
type bplist struct {
	data        []byte
	offsetSize  int
	refSize     int
	numObjects  uint64
	offsetTable uint64
}

func parseBinaryPlist(data []byte) (map[string]string, error) {
	const header, trailer = 8, 32
	if len(data) < header+trailer {
		return nil, errNotPlist
	}
	t := data[len(data)-trailer:]
	p := bplist{
		data:        data,
		offsetSize:  int(t[6]),
		refSize:     int(t[7]),
		numObjects:  binary.BigEndian.Uint64(t[8:16]),
		offsetTable: binary.BigEndian.Uint64(t[24:32]),
	}
	top := binary.BigEndian.Uint64(t[16:24])
	end := uint64(len(data) - trailer)
	if p.offsetSize < 1 || p.offsetSize > 8 || p.refSize < 1 || p.refSize > 8 ||
		p.numObjects == 0 || p.numObjects > uint64(len(data)) || top >= p.numObjects ||
		p.offsetTable < header || p.offsetTable > end ||
		p.numObjects*uint64(p.offsetSize) > end-p.offsetTable {
		return nil, errNotPlist
	}
	off, ok := p.object(top)
	if !ok || data[off]>>4 != 0xD {
		return nil, errNotPlist
	}
	count, pos, ok := p.length(off)
	if !ok || count > p.numObjects || pos+2*count*uint64(p.refSize) > p.offsetTable {
		return nil, errNotPlist
	}
	out := map[string]string{}
	for i := uint64(0); i < count; i++ {
		keyRef := p.uint(pos+i*uint64(p.refSize), p.refSize)
		valRef := p.uint(pos+(count+i)*uint64(p.refSize), p.refSize)
		key, ok := p.str(keyRef)
		if !ok {
			return nil, errNotPlist
		}
		if value, ok := p.str(valRef); ok {
			out[key] = value
		}
	}
	return out, nil
}

// object returns the byte offset of object ref, inside the object area.
func (p bplist) object(ref uint64) (uint64, bool) {
	if ref >= p.numObjects {
		return 0, false
	}
	off := p.uint(p.offsetTable+ref*uint64(p.offsetSize), p.offsetSize)
	if off < 8 || off >= p.offsetTable {
		return 0, false
	}
	return off, true
}

// length decodes an object's element count and returns where its payload
// starts. A low nibble of 0xF means the count follows as an int object.
func (p bplist) length(off uint64) (count, payload uint64, ok bool) {
	info := uint64(p.data[off] & 0x0F)
	if info != 0x0F {
		return info, off + 1, true
	}
	if off+1 >= p.offsetTable || p.data[off+1]>>4 != 0x1 {
		return 0, 0, false
	}
	size := uint64(1) << (p.data[off+1] & 0x0F)
	if size > 8 || off+2+size > p.offsetTable {
		return 0, 0, false
	}
	return p.uint(off+2, int(size)), off + 2 + size, true
}

// str decodes an ASCII (0x5) or UTF-16BE (0x6) string object.
func (p bplist) str(ref uint64) (string, bool) {
	off, ok := p.object(ref)
	if !ok {
		return "", false
	}
	kind := p.data[off] >> 4
	if kind != 0x5 && kind != 0x6 {
		return "", false
	}
	count, pos, ok := p.length(off)
	if !ok || count > p.offsetTable {
		return "", false
	}
	if kind == 0x5 {
		if pos+count > p.offsetTable {
			return "", false
		}
		return string(p.data[pos : pos+count]), true
	}
	if pos+2*count > p.offsetTable {
		return "", false
	}
	units := make([]uint16, count)
	for i := range units {
		units[i] = binary.BigEndian.Uint16(p.data[pos+2*uint64(i):])
	}
	return string(utf16.Decode(units)), true
}

// uint reads a big-endian unsigned integer of size bytes at off. Callers have
// bounds-checked the range against the offset table or trailer.
func (p bplist) uint(off uint64, size int) uint64 {
	var v uint64
	for i := 0; i < size; i++ {
		v = v<<8 | uint64(p.data[off+uint64(i)])
	}
	return v
}
