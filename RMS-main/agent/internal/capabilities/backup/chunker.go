package backup

import (
	"io"
	"math/bits"
	"sync"
)

// Content-defined chunking (docs/modules/backup/spec.md §5).
//
// A Buzhash rolling hash over a 48-byte window; a boundary is emitted when the
// low log2(avg) bits of the hash are zero, bounded by min and max. Content
// DEFINED rather than fixed-size is the whole reason dedup survives an insert:
// prepending one byte to a 4 GiB file shifts every fixed-size block and
// re-uploads the file, while a content-defined boundary re-synchronises within
// one chunk.
//
// # This is a streaming chunker and that is a hard requirement
//
// CLAUDE.md's idle budget, and the specific failure it names for this
// capability: "a naive implementation reads whole files into memory". The
// buffer here is exactly `max` bytes — 4 MiB at the pinned defaults — and one
// chunker serves an entire backup run regardless of whether the tree contains a
// 40 GiB VM image. next() returns a SLICE INTO that buffer, valid only until
// the following call, so a run of ten thousand chunks allocates the buffer once
// and nothing per chunk.

// chunkWindow is the rolling-hash window, in bytes (spec §5).
const chunkWindow = 48

// buzTable is 256 pseudo-random 64-bit values, one per byte value, and
// buzRotTable is each of them pre-rotated by the window width so the
// leaving-byte term is a lookup rather than a rotate on the inner loop.
//
// COMPUTED LAZILY, ON THE FIRST JOB. A package-level initialiser would run at
// process start and leave 4 KiB resident on an agent that never takes a backup
// job, which is precisely the "no capability is always on" line in CLAUDE.md.
// A zero-value sync.Once and two nil pointers is what a dormant agent carries.
//
// The values are DERIVED, not random and not hand-written, because this table
// IS the repository's chunk domain: two agent builds with different tables
// produce different boundaries over identical bytes and dedup against each
// other not at all. splitmix64 from a fixed seed is reproducible by anyone
// reading this file, which a 256-entry hex blob is not.
var (
	buzOnce     sync.Once
	buzTable    [256]uint64
	buzRotTable [256]uint64
)

// buzSeed is arbitrary but FIXED FOREVER. Changing it starts a new chunk
// domain in every existing repository, exactly as changing avg/min/max does
// (spec §5) — except that unlike those it is not recorded in
// `backup_repositories.dedup_params`, so nothing would tell an operator it had
// happened. It is not a tunable.
const buzSeed = 0x6261636b75705f31 // "backup_1"

func buzInit() {
	buzOnce.Do(func() {
		x := uint64(buzSeed)
		for i := 0; i < 256; i++ {
			// splitmix64.
			x += 0x9E3779B97F4A7C15
			z := x
			z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9
			z = (z ^ (z >> 27)) * 0x94D049BB133111EB
			z ^= z >> 31
			buzTable[i] = z
			buzRotTable[i] = bits.RotateLeft64(z, chunkWindow)
		}
	})
}

// chunker splits a stream at content-defined boundaries.
//
// Not safe for concurrent use, and deliberately not: a backup run is one
// goroutine (see the dormancy note in backup.go), so a mutex here would guard
// nothing and cost something.
type chunker struct {
	r   io.Reader
	buf []byte
	off int  // start of the chunk being built
	end int  // valid bytes in buf
	eof bool // the reader has returned io.EOF

	min, max int
	mask     uint64
}

// newChunker allocates the ONE buffer a run holds. Callers reuse it across
// files via reset.
func newChunker(d dedupSpec) *chunker {
	buzInit()
	return &chunker{
		buf:  make([]byte, d.Max),
		min:  d.Min,
		max:  d.Max,
		mask: uint64(d.Avg - 1),
	}
}

// reset points the chunker at a new stream WITHOUT reallocating the buffer.
func (c *chunker) reset(r io.Reader) {
	c.r = r
	c.off = 0
	c.end = 0
	c.eof = false
}

// next returns the next chunk, or io.EOF when the stream is exhausted.
//
// THE RETURNED SLICE ALIASES THE INTERNAL BUFFER and is only valid until the
// next call. Every caller in this package hashes and encrypts it before calling
// again; a caller that retained it would be holding a window onto customer file
// contents that later gets overwritten with different customer file contents,
// which is a correctness bug before it is a footprint one.
func (c *chunker) next() ([]byte, error) {
	if c.off == c.end && c.eof {
		return nil, io.EOF
	}
	var h uint64
	i := c.off
	for {
		if i >= c.end {
			if c.eof {
				break
			}
			if err := c.fill(&i); err != nil {
				return nil, err
			}
			if i >= c.end {
				break
			}
		}
		out := uint64(0)
		if i-c.off >= chunkWindow {
			out = buzRotTable[c.buf[i-chunkWindow]]
		}
		h = bits.RotateLeft64(h, 1) ^ out ^ buzTable[c.buf[i]]
		i++

		n := i - c.off
		if n >= c.max {
			break
		}
		if n >= c.min && h&c.mask == 0 {
			break
		}
	}
	if i == c.off {
		return nil, io.EOF
	}
	chunk := c.buf[c.off:i]
	c.off = i
	return chunk, nil
}

// fill compacts the buffer and reads more.
//
// It takes `i` BY POINTER because compaction moves the bytes the caller is
// mid-scan over. Returning without adjusting it would silently rescan or skip a
// stretch of the file — a corruption that produces a valid-looking manifest.
func (c *chunker) fill(i *int) error {
	if c.off > 0 {
		copy(c.buf, c.buf[c.off:c.end])
		c.end -= c.off
		*i -= c.off
		c.off = 0
	}
	for c.end < len(c.buf) {
		n, err := c.r.Read(c.buf[c.end:])
		c.end += n
		if err == io.EOF {
			c.eof = true
			return nil
		}
		if err != nil {
			return err
		}
		if n > 0 {
			return nil
		}
	}
	return nil
}
