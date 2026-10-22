package backup

import (
	"bytes"
	"encoding/hex"
	"testing"
)

// THE OFFICIAL BLAKE3 TEST VECTORS.
//
// Taken verbatim from the BLAKE3 team's `test_vectors/test_vectors.json`
// (github.com/BLAKE3-team/BLAKE3), unkeyed mode, first 32 bytes of each
// extended output. The input for length N is the 251-byte repeating sequence
// 0,1,2,…,250,0,1,… truncated to N bytes, which is what that file specifies.
//
// WHY THESE AND NOT A GOLDEN FILE OF OUR OWN: a chunk id is the repository's
// dedup key AND its integrity check. An implementation that is consistently
// wrong hashes, dedups and verifies perfectly against itself and produces a
// repository no other BLAKE3 implementation — including a future server-side
// verifier, or a forensic tool during an incident — can read. Vectors from
// outside this repository are the only thing that can falsify that.
//
// The lengths are chosen by the BLAKE3 team to straddle every boundary the
// construction has: 0 (empty), 1 (partial block), 63/64/65 (block), 1023/1024/
// 1025 (chunk), 2048/4096/8192 (subtree merges), 102400 (deep tree). A table
// of three convenient small inputs would exercise one code path and read as
// coverage.
var blake3Vectors = []struct {
	inputLen int
	hash     string
}{
	{0, "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"},
	{1, "2d3adedff11b61f14c886e35afa036736dcd87a74d27b5c1510225d0f592e213"},
	{2, "7b7015bb92cf0b318037702a6cdd81dee41224f734684c2c122cd6359cb1ee63"},
	{3, "e1be4d7a8ab5560aa4199eea339849ba8e293d55ca0a81006726d184519e647f"},
	{4, "f30f5ab28fe047904037f77b6da4fea1e27241c5d132638d8bedce9d40494f32"},
	{5, "b40b44dfd97e7a84a996a91af8b85188c66c126940ba7aad2e7ae6b385402aa2"},
	{6, "06c4e8ffb6872fad96f9aaca5eee1553eb62aed0ad7198cef42e87f6a616c844"},
	{7, "3f8770f387faad08faa9d8414e9f449ac68e6ff0417f673f602a646a891419fe"},
	{8, "2351207d04fc16ade43ccab08600939c7c1fa70a5c0aaca76063d04c3228eaeb"},
	{63, "e9bc37a594daad83be9470df7f7b3798297c3d834ce80ba85d6e207627b7db7b"},
	{64, "4eed7141ea4a5cd4b788606bd23f46e212af9cacebacdc7d1f4c6dc7f2511b98"},
	{65, "de1e5fa0be70df6d2be8fffd0e99ceaa8eb6e8c93a63f2d8d1c30ecb6b263dee"},
	{127, "d81293fda863f008c09e92fc382a81f5a0b4a1251cba1634016a0f86a6bd640d"},
	{128, "f17e570564b26578c33bb7f44643f539624b05df1a76c81f30acd548c44b45ef"},
	{129, "683aaae9f3c5ba37eaaf072aed0f9e30bac0865137bae68b1fde4ca2aebdcb12"},
	{1023, "10108970eeda3eb932baac1428c7a2163b0e924c9a9e25b35bba72b28f70bd11"},
	{1024, "42214739f095a406f3fc83deb889744ac00df831c10daa55189b5d121c855af7"},
	{1025, "d00278ae47eb27b34faecf67b4fe263f82d5412916c1ffd97c8cb7fb814b8444"},
	{2048, "e776b6028c7cd22a4d0ba182a8bf62205d2ef576467e838ed6f2529b85fba24a"},
	{2049, "5f4d72f40d7a5f82b15ca2b2e44b1de3c2ef86c426c95c1af0b6879522563030"},
	{3072, "b98cb0ff3623be03326b373de6b9095218513e64f1ee2edd2525c7ad1e5cffd2"},
	{3073, "7124b49501012f81cc7f11ca069ec9226cecb8a2c850cfe644e327d22d3e1cd3"},
	{4096, "015094013f57a5277b59d8475c0501042c0b642e531b0a1c8f58d2163229e969"},
	{4097, "9b4052b38f1c5fc8b1f9ff7ac7b27cd242487b3d890d15c96a1c25b8aa0fb995"},
	{5120, "9cadc15fed8b5d854562b26a9536d9707cadeda9b143978f319ab34230535833"},
	{5121, "628bd2cb2004694adaab7bbd778a25df25c47b9d4155a55f8fbd79f2fe154cff"},
	{6144, "3e2e5b74e048f3add6d21faab3f83aa44d3b2278afb83b80b3c35164ebeca205"},
	{6145, "f1323a8631446cc50536a9f705ee5cb619424d46887f3c376c695b70e0f0507f"},
	{7168, "61da957ec2499a95d6b8023e2b0e604ec7f6b50e80a9678b89d2628e99ada77a"},
	{7169, "a003fc7a51754a9b3c7fae0367ab3d782dccf28855a03d435f8cfe74605e7817"},
	{8192, "aae792484c8efe4f19e2ca7d371d8c467ffb10748d8a5a1ae579948f718a2a63"},
	{8193, "bab6c09cb8ce8cf459261398d2e7aef35700bf488116ceb94a36d0f5f1b7bc3b"},
	{16384, "f875d6646de28985646f34ee13be9a576fd515f76b5b0a26bb324735041ddde4"},
	{31744, "62b6960e1a44bcc1eb1a611a8d6235b6b4b78f32e7abc4fb4c6cdcce94895c47"},
	{102400, "bc3e3d41a1146b069abffad3c0d44860cf664390afce4d9661f7902e7943e085"},
}

// vectorInput builds the BLAKE3 test-vector input: bytes 0..250 repeating.
func vectorInput(n int) []byte {
	b := make([]byte, n)
	for i := range b {
		b[i] = byte(i % 251)
	}
	return b
}

func TestBlake3MatchesTheOfficialVectors(t *testing.T) {
	// FALSIFYING OBSERVATION: any case where the computed digest differs from
	// the BLAKE3 team's published one. That is a repository format nothing else
	// can read.
	for _, v := range blake3Vectors {
		in := vectorInput(v.inputLen)
		got := blake3Sum256(in)
		if hex.EncodeToString(got[:]) != v.hash {
			t.Errorf("BLAKE3-256 of the %d-byte vector input = %s, official vector says %s",
				v.inputLen, hex.EncodeToString(got[:]), v.hash)
		}
	}
}

func TestBlake3StreamsIdenticallyToTheOneShotFormAtEveryWriteBoundary(t *testing.T) {
	// The engine feeds the hasher whole chunks, but the hasher is reused across
	// them via Reset, and Reset getting the chunk state wrong is invisible to a
	// one-shot test. This splits each vector input at boundaries that straddle
	// the block (64) and chunk (1024) sizes.
	//
	// FALSIFYING OBSERVATION: a split producing a different digest from the
	// whole-input digest — which is what a Write that mishandles a partial block
	// or a chunk that spans two Writes produces.
	splits := []int{1, 63, 64, 65, 127, 1023, 1024, 1025, 4096}
	h := newBlake3()
	for _, v := range blake3Vectors {
		in := vectorInput(v.inputLen)
		for _, split := range splits {
			if split >= v.inputLen {
				continue
			}
			h.Reset()
			_, _ = h.Write(in[:split])
			_, _ = h.Write(in[split:])
			got := h.Sum256()
			if hex.EncodeToString(got[:]) != v.hash {
				t.Errorf("%d bytes written as %d+%d = %s, want %s",
					v.inputLen, split, v.inputLen-split, hex.EncodeToString(got[:]), v.hash)
			}
		}
	}
}

func TestAReusedHasherGivesTheSameAnswerAsAFreshOne(t *testing.T) {
	// Reset is the whole reason blake3Hasher is a struct rather than a
	// function. A Reset that left the CV stack populated would make the SECOND
	// chunk of a backup run hash differently from the first — and the run would
	// still complete, having written chunk objects under ids nothing can
	// reproduce.
	//
	// FALSIFYING OBSERVATION: the reused hasher naming a different digest.
	reused := newBlake3()
	for _, v := range blake3Vectors {
		in := vectorInput(v.inputLen)
		fresh := blake3Sum256(in)
		reused.Reset()
		_, _ = reused.Write(in)
		got := reused.Sum256()
		if got != fresh {
			t.Fatalf("after hashing earlier inputs, the %d-byte vector hashed to %x; a fresh "+
				"hasher gives %x", v.inputLen, got, fresh)
		}
	}
}

func TestASingleFlippedBitChangesTheChunkID(t *testing.T) {
	// FIXTURE BLINDNESS GUARD. Every other test here compares against a
	// constant; none of them would notice a "hash" that returned the same 32
	// bytes for everything, because the vectors would simply all fail — and a
	// reviewer reading a red suite learns nothing about WHICH property broke.
	// This one states the property directly.
	//
	// THE MUTATION: byte 500 of a 4 KiB input, 0xNN -> 0xNN^0x01. Asserted to
	// have actually changed the input before the digest is compared, because a
	// mutation that did not apply makes this test pass while measuring nothing.
	//
	// FALSIFYING OBSERVATION: two different plaintexts sharing a chunk id. That
	// is not a hash bug in the abstract — it is silent data loss, because
	// store-once-by-id (spec §5) skips the second chunk and the file that
	// referenced it restores as the first one's bytes.
	h := newBlake3()
	original := vectorInput(4096)
	mutated := bytes.Clone(original)
	mutated[500] ^= 0x01
	if bytes.Equal(original, mutated) {
		t.Fatal("the mutation did not apply; this test would pass while measuring nothing")
	}
	if original[500] == mutated[500] {
		t.Fatalf("byte 500 is %#x in both copies; the mutation did not apply", original[500])
	}

	a := chunkID(h, original)
	b := chunkID(h, mutated)
	if a == b {
		t.Fatalf("two plaintexts differing at byte 500 (%#x vs %#x) share chunk id %s; "+
			"store-once-by-id would drop one of them and restore the other's bytes in its place",
			original[500], mutated[500], a)
	}
	if len(a) != 64 {
		t.Fatalf("chunk id %q is %d hex characters, want 64 (BLAKE3-256)", a, len(a))
	}
}

func TestTheEmptyChunkIDIsTheKnownEmptyDigest(t *testing.T) {
	// An empty file has zero chunks, not one empty chunk (see manifest.go), but
	// the id function must still be correct on the empty input: getting this
	// wrong is the classic off-by-one in a merkle construction.
	const want = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
	if got := chunkID(newBlake3(), nil); got != want {
		t.Errorf("chunkID(empty) = %s, official vector says %s", got, want)
	}
}
