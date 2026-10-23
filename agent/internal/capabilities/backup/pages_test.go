package backup

import (
	"bytes"
	"fmt"
	"strings"
	"testing"
)

// Manifest v2 pages (spec-server-backup.md §5.2).

type capturedPage struct {
	id   string
	data []byte
}

func pageRun(t *testing.T, max int, entries []entryV2) (*pageWriter, []capturedPage) {
	t.Helper()
	var out []capturedPage
	pw := newPageWriter(max, newBlake3(), func(id string, data []byte) {
		out = append(out, capturedPage{id, data})
	})
	for i := range entries {
		e := entries[i]
		if err := pw.add(&e); err != nil {
			t.Fatal(err)
		}
	}
	pw.cut()
	return pw, out
}

func synthEntries(n int, prefix string) []entryV2 {
	out := make([]entryV2, 0, n)
	for i := 0; i < n; i++ {
		e := entryV2{manifestEntry: newEntry(fmt.Sprintf("%s/d%03d/f%05d.txt", prefix, i/100, i), kindFile,
			0o644, int64(i), 1.5)}
		out = append(out, e)
	}
	return out
}

func TestPagesAreEntryAlignedBoundedAndRoundTrip(t *testing.T) {
	const max = 64 << 10
	in := synthEntries(5000, "/srv")
	pw, pages := pageRun(t, max, in)
	if len(pw.pages) < 2 {
		t.Fatalf("5000 entries made %d pages; the test does not exercise a boundary", len(pw.pages))
	}
	var got []entryV2
	idx := 0
	for _, pg := range pages {
		if len(pg.data) > max {
			t.Fatalf("a page is %d bytes, over the %d-byte object bound", len(pg.data), max)
		}
		if !bytes.HasSuffix(pg.data, []byte{'\n'}) {
			t.Fatal("a page does not end on an entry boundary")
		}
		if chunkID(newBlake3(), pg.data) != pg.id {
			t.Fatal("a page's id is not the hash of its bytes")
		}
		es, err := parseEntryPage(pg.data)
		if err != nil {
			t.Fatal(err)
		}
		ix := pw.index[idx]
		if ix.Entries != len(es) || ix.First != es[0].Path || ix.Last != es[len(es)-1].Path || ix.Page != idx {
			t.Errorf("page_index[%d] = %+v, page holds %d entries %s..%s", idx, ix, len(es), es[0].Path,
				es[len(es)-1].Path)
		}
		idx++
		got = append(got, es...)
	}
	if len(got) != len(in) || pw.entryCount != int64(len(in)) {
		t.Fatalf("%d entries in, %d out (count %d)", len(in), len(got), pw.entryCount)
	}
	for i := range in {
		if got[i].Path != in[i].Path || got[i].Size != in[i].Size {
			t.Fatalf("entry %d changed: %+v", i, got[i])
		}
	}
}

func TestAnUnchangedTreeProducesTheSamePagesAndAChangeResyncs(t *testing.T) {
	// The manifest dedups day to day only if an insertion early in the tree
	// does not shift every later page boundary.
	const max = 16 << 10
	base := synthEntries(6000, "/srv")
	_, a := pageRun(t, max, base)
	_, b := pageRun(t, max, base)
	if len(a) != len(b) {
		t.Fatal("the same entries paged differently")
	}
	for i := range a {
		if a[i].id != b[i].id {
			t.Fatal("the same entries produced a different page")
		}
	}
	changed := append([]entryV2{}, base[:100]...)
	changed = append(changed, entryV2{manifestEntry: newEntry("/srv/d000/inserted", kindFile, 0o644, 1, 1)})
	changed = append(changed, base[100:]...)
	_, c := pageRun(t, max, changed)
	ids := map[string]bool{}
	for _, p := range a {
		ids[p.id] = true
	}
	shared := 0
	for _, p := range c {
		if ids[p.id] {
			shared++
		}
	}
	if shared < len(a)/2 {
		t.Fatalf("after one inserted entry only %d of %d pages were unchanged; boundaries did not resync",
			shared, len(a))
	}
	t.Logf("pages: %d before, %d after one insertion, %d shared", len(a), len(c), shared)
}

func TestALargeFilesChunkIDsMoveToIDPagesAndBack(t *testing.T) {
	const max = 4096 // testDedup's max: ~15 ids inline, 63 per id page
	e := entryV2{manifestEntry: newEntry("/srv/big.vhdx", kindFile, 0o644, 0, 1)}
	for i := 0; i < 200; i++ {
		e.ChunkIDs = append(e.ChunkIDs, strings.Repeat(fmt.Sprintf("%02x", i%256), 32))
	}
	want := append([]string(nil), e.ChunkIDs...)
	pw, pages := pageRun(t, max, []entryV2{e})
	byID := map[string][]byte{}
	for _, p := range pages {
		byID[p.id] = p.data
	}
	es, err := parseEntryPage(byID[pw.pages[0]])
	if err != nil || len(es) != 1 {
		t.Fatalf("entry page: %v %v", es, err)
	}
	if len(es[0].ChunkIDs) != 0 || len(es[0].ChunkPages) < 2 {
		t.Fatalf("200 ids stayed inline (%d) or went to %d pages", len(es[0].ChunkIDs), len(es[0].ChunkPages))
	}
	var got []string
	for _, pid := range es[0].ChunkPages {
		data := byID[pid]
		if len(data) > max {
			t.Fatalf("an id page is %d bytes", len(data))
		}
		ids, err := parseIDPage(data)
		if err != nil {
			t.Fatal(err)
		}
		got = append(got, ids...)
	}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatal("the id pages do not give back the file's chunk list in order")
	}
}

func TestAMalformedIDPageIsRejected(t *testing.T) {
	for _, bad := range []string{"abc\n", strings.Repeat("a", 64) + "x"} {
		if _, err := parseIDPage([]byte(bad)); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

func TestAnEntryLargerThanAnyObjectFailsLoudly(t *testing.T) {
	pw := newPageWriter(1024, newBlake3(), func(string, []byte) {})
	e := entryV2{manifestEntry: newEntry("/"+strings.Repeat("x", 2000), kindFile, 0o644, 0, 1)}
	if err := pw.add(&e); err == nil {
		t.Fatal("an entry over the object bound was accepted")
	}
}
