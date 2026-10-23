package backup

import (
	"encoding/binary"
	"encoding/hex"
	"math/bits"
)

// BLAKE3-256, implemented here rather than imported.
//
// # Why in-tree
//
// docs/modules/backup/spec.md §5 pins the chunk id as `BLAKE3-256(plaintext
// chunk)` hex. Go's standard library has no BLAKE3, and CLAUDE.md's agent
// convention is "single static binary, minimal dependencies" — the agent's
// entire module graph today is one WebSocket library and golang.org/x/sys.
// Adding a third-party hash for one 200-line function, in a binary that runs
// as SYSTEM on every endpoint in the fleet, buys a supply-chain surface far
// larger than the code it saves.
//
// This is the sequential reference construction (BLAKE3 §2.1-§2.6), not an
// optimised one: no SIMD, no multi-threading, no XOF. The only output width
// this repository needs is 32 bytes, so the extendable-output machinery is
// absent by design — a function that is not compiled cannot be wrong.
//
// # It is pinned to the official vectors, not to itself
//
// blake3_test.go runs the 35 cases from the BLAKE3 team's own
// `test_vectors.json` (input lengths 0 … 102400, the standard 251-byte
// repeating input). A hash that agreed only with itself would be a repository
// format nothing else can read: the chunk id IS the dedup key and the integrity
// check, so an implementation that is self-consistently wrong produces a
// repository that verifies clean and cannot be restored by anything else, ever.
// The vectors are the falsifier.
//
// # What "wrong" costs here
//
// A chunk id is the dedup key. Two different plaintexts hashing alike is a
// silent data-loss bug: the second chunk is skipped as already-stored
// (first-writer-wins, §5) and the file that referenced it restores as the
// first one's bytes. That is why restore re-hashes every chunk it decrypts and
// compares against the manifest rather than trusting the object store.

const (
	blake3BlockLen = 64
	blake3ChunkLen = 1024

	// The maximum tree depth. Input is bounded by 2^64 bytes = 2^54 chunks, so
	// the CV stack can never need more than 54 entries.
	blake3MaxDepth = 54
)

// Domain-separation flags (BLAKE3 §2.1 table 3). KEYED_HASH, DERIVE_KEY_CONTEXT
// and DERIVE_KEY_MATERIAL are deliberately absent: this package only ever hashes
// in the unkeyed mode, and a flag that cannot be set is a mode that cannot be
// entered by accident.
const (
	flagChunkStart uint32 = 1 << 0
	flagChunkEnd   uint32 = 1 << 1
	flagParent     uint32 = 1 << 2
	flagRoot       uint32 = 1 << 3
)

// blake3IV is the SHA-256 initialisation vector, which BLAKE3 reuses.
var blake3IV = [8]uint32{
	0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A,
	0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19,
}

// msgPermutation is applied between rounds (BLAKE3 §2.2).
var msgPermutation = [16]int{2, 6, 3, 10, 7, 0, 4, 13, 1, 11, 12, 5, 9, 14, 15, 8}

// g is the quarter-round.
func g(s *[16]uint32, a, b, c, d int, mx, my uint32) {
	s[a] = s[a] + s[b] + mx
	s[d] = bits.RotateLeft32(s[d]^s[a], -16)
	s[c] = s[c] + s[d]
	s[b] = bits.RotateLeft32(s[b]^s[c], -12)
	s[a] = s[a] + s[b] + my
	s[d] = bits.RotateLeft32(s[d]^s[a], -8)
	s[c] = s[c] + s[d]
	s[b] = bits.RotateLeft32(s[b]^s[c], -7)
}

func blake3Round(s *[16]uint32, m *[16]uint32) {
	// Columns.
	g(s, 0, 4, 8, 12, m[0], m[1])
	g(s, 1, 5, 9, 13, m[2], m[3])
	g(s, 2, 6, 10, 14, m[4], m[5])
	g(s, 3, 7, 11, 15, m[6], m[7])
	// Diagonals.
	g(s, 0, 5, 10, 15, m[8], m[9])
	g(s, 1, 6, 11, 12, m[10], m[11])
	g(s, 2, 7, 8, 13, m[12], m[13])
	g(s, 3, 4, 9, 14, m[14], m[15])
}

// compress is BLAKE3's compression function. It returns the full 16-word state:
// callers that want a chaining value take the first eight words.
func compress(cv *[8]uint32, block *[16]uint32, counter uint64, blockLen, flags uint32) [16]uint32 {
	s := [16]uint32{
		cv[0], cv[1], cv[2], cv[3], cv[4], cv[5], cv[6], cv[7],
		blake3IV[0], blake3IV[1], blake3IV[2], blake3IV[3],
		uint32(counter), uint32(counter >> 32), blockLen, flags,
	}
	m := *block
	for r := 0; r < 7; r++ {
		blake3Round(&s, &m)
		if r < 6 {
			var p [16]uint32
			for i := 0; i < 16; i++ {
				p[i] = m[msgPermutation[i]]
			}
			m = p
		}
	}
	for i := 0; i < 8; i++ {
		s[i] ^= s[i+8]
		s[i+8] ^= cv[i]
	}
	return s
}

func wordsFromBlock(dst *[16]uint32, src *[blake3BlockLen]byte) {
	for i := 0; i < 16; i++ {
		dst[i] = binary.LittleEndian.Uint32(src[i*4:])
	}
}

// blake3Output is a node whose bytes have not been produced yet: either a chunk
// or a parent. The distinction between a chaining value and a root hash is only
// the ROOT flag, so both are derived from the same struct.
type blake3Output struct {
	inputCV  [8]uint32
	block    [16]uint32
	counter  uint64
	blockLen uint32
	flags    uint32
}

func (o *blake3Output) chainingValue() [8]uint32 {
	s := compress(&o.inputCV, &o.block, o.counter, o.blockLen, o.flags)
	var cv [8]uint32
	copy(cv[:], s[:8])
	return cv
}

// rootSum256 produces the 32-byte root hash. The root counter is 0 for the
// first output block, and there is only ever one: 32 bytes is half a block.
func (o *blake3Output) rootSum256() [32]byte {
	s := compress(&o.inputCV, &o.block, 0, o.blockLen, o.flags|flagRoot)
	var out [32]byte
	for i := 0; i < 8; i++ {
		binary.LittleEndian.PutUint32(out[i*4:], s[i])
	}
	return out
}

type blake3ChunkState struct {
	cv               [8]uint32
	chunkCounter     uint64
	block            [blake3BlockLen]byte
	blockLen         int
	blocksCompressed int
	flags            uint32
}

func (c *blake3ChunkState) reset(key [8]uint32, counter uint64, flags uint32) {
	c.cv = key
	c.chunkCounter = counter
	c.block = [blake3BlockLen]byte{}
	c.blockLen = 0
	c.blocksCompressed = 0
	c.flags = flags
}

func (c *blake3ChunkState) length() int {
	return blake3BlockLen*c.blocksCompressed + c.blockLen
}

func (c *blake3ChunkState) startFlag() uint32 {
	if c.blocksCompressed == 0 {
		return flagChunkStart
	}
	return 0
}

func (c *blake3ChunkState) update(input []byte) {
	for len(input) > 0 {
		if c.blockLen == blake3BlockLen {
			var bw [16]uint32
			wordsFromBlock(&bw, &c.block)
			out := compress(&c.cv, &bw, c.chunkCounter, blake3BlockLen, c.flags|c.startFlag())
			copy(c.cv[:], out[:8])
			c.blocksCompressed++
			c.block = [blake3BlockLen]byte{}
			c.blockLen = 0
		}
		n := copy(c.block[c.blockLen:], input)
		c.blockLen += n
		input = input[n:]
	}
}

func (c *blake3ChunkState) output() blake3Output {
	var bw [16]uint32
	wordsFromBlock(&bw, &c.block)
	return blake3Output{
		inputCV:  c.cv,
		block:    bw,
		counter:  c.chunkCounter,
		blockLen: uint32(c.blockLen),
		flags:    c.flags | c.startFlag() | flagChunkEnd,
	}
}

func parentOutput(left, right [8]uint32, key [8]uint32, flags uint32) blake3Output {
	var block [16]uint32
	copy(block[:8], left[:])
	copy(block[8:], right[:])
	return blake3Output{
		inputCV:  key,
		block:    block,
		counter:  0,
		blockLen: blake3BlockLen,
		flags:    flags | flagParent,
	}
}

// blake3Hasher is a reusable, allocation-free BLAKE3-256 hasher.
//
// REUSABLE IS THE POINT. A backup run hashes every chunk of every file; a
// hasher allocated per chunk would put ~1.8 KiB of garbage per chunk on the
// heap for no reason at all, which is exactly the class of regression
// budget_test.go exists to catch. The engine keeps one and calls Reset.
type blake3Hasher struct {
	chunk      blake3ChunkState
	key        [8]uint32
	cvStack    [blake3MaxDepth][8]uint32
	cvStackLen int
	flags      uint32
}

func newBlake3() *blake3Hasher {
	h := &blake3Hasher{key: blake3IV}
	h.Reset()
	return h
}

func (h *blake3Hasher) Reset() {
	h.chunk.reset(h.key, 0, h.flags)
	h.cvStackLen = 0
}

// pushCV merges the stack per BLAKE3 §2.6: a chunk's chaining value is combined
// with its left sibling whenever the total chunk count has a trailing zero bit.
func (h *blake3Hasher) pushCV(cv [8]uint32, totalChunks uint64) {
	for totalChunks&1 == 0 {
		h.cvStackLen--
		cv = func() [8]uint32 {
			o := parentOutput(h.cvStack[h.cvStackLen], cv, h.key, h.flags)
			return o.chainingValue()
		}()
		totalChunks >>= 1
	}
	h.cvStack[h.cvStackLen] = cv
	h.cvStackLen++
}

func (h *blake3Hasher) Write(input []byte) (int, error) {
	n := len(input)
	for len(input) > 0 {
		if h.chunk.length() == blake3ChunkLen {
			o := h.chunk.output()
			cv := o.chainingValue()
			total := h.chunk.chunkCounter + 1
			h.pushCV(cv, total)
			h.chunk.reset(h.key, total, h.flags)
		}
		want := blake3ChunkLen - h.chunk.length()
		if want > len(input) {
			want = len(input)
		}
		h.chunk.update(input[:want])
		input = input[want:]
	}
	return n, nil
}

func (h *blake3Hasher) Sum256() [32]byte {
	o := h.chunk.output()
	n := h.cvStackLen
	for n > 0 {
		n--
		o = parentOutput(h.cvStack[n], o.chainingValue(), h.key, h.flags)
	}
	return o.rootSum256()
}

// blake3Sum256 is the one-shot form.
func blake3Sum256(b []byte) [32]byte {
	h := newBlake3()
	_, _ = h.Write(b)
	return h.Sum256()
}

// chunkID is the repository's dedup key: lowercase hex of BLAKE3-256 over the
// PLAINTEXT chunk (spec §5). Hex and not base64 because the id is also a path
// component (`chunks/{id[:2]}/{id}`), and base64 is neither case-insensitive
// nor free of `/`.
func chunkID(h *blake3Hasher, plaintext []byte) string {
	h.Reset()
	_, _ = h.Write(plaintext)
	sum := h.Sum256()
	return hex.EncodeToString(sum[:])
}
