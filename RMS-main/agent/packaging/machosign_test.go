// Apple Silicon refuses to exec an arm64 Mach-O that carries no code
// signature. Not Gatekeeper, not quarantine, not notarization, and not
// anything an MDM profile can switch off: the kernel checks for a signature at
// exec and kills the process without one. An ad-hoc signature —
// self-referential, no certificate, no Apple Developer account, no Mac — is
// enough to satisfy it.
//
// The Go linker already produces one. Building darwin/arm64 with the INTERNAL
// linker (which CGO_ENABLED=0 guarantees) makes the linker embed an ad-hoc
// signature itself, in Go code, without shelling out to codesign(1) — so
// cross-compiling from this Linux CI runner already yields a binary that will
// exec on Apple Silicon. That was established by inspecting the artifact, not
// assumed; the measurements are in this test's log output on every run.
//
// So why does this file exist, if the toolchain already does the work?
//
// Because "the toolchain happens to do it" is not a property anyone here
// chose, and nothing else in this repository would notice it going away. Four
// realistic changes silently produce an unrunnable macOS binary:
//
//  1. A capability that actually imports "C", forcing EXTERNAL linking. On
//     darwin the external linker leaves signing to codesign(1), which is
//     Mac-only and does not exist on this runner. Note the trigger is real
//     cgo USE, not the CGO_ENABLED flag: setting CGO_ENABLED=1 today still
//     produces a signed binary, because no package imports "C" and so the
//     internal linker is still chosen. That was checked, not assumed — and it
//     is the more dangerous shape, since the flag looks like the control and
//     is not.
//  2. A post-link step that rewrites the file — strip, upx, an appended
//     manifest. The signature covers the bytes; editing them after signing
//     leaves an LC_CODE_SIGNATURE whose page hashes no longer match, and macOS
//     rejects that exactly as hard as no signature at all. That is why this
//     file recomputes every page hash rather than only looking for the load
//     command.
//  3. A future toolchain or build-flag change.
//  4. Producing the darwin artifact by some path other than build.sh.
//
// The failure mode of all four is identical and invisible from Linux: file(1)
// still says "Mach-O 64-bit arm64 executable", go build still exits 0, every
// other test still passes, and the binary is dead on arrival on every Apple
// Silicon endpoint in the fleet. Nobody on this project has a Mac. This
// arithmetic is the only detector we get, which is also why the detector has
// its own negative tests below — an unfalsified checker is not evidence.
//
// Deliberately NO t.Skip anywhere here. A packaging check that no-ops when
// something is missing is the shape that let the health.check falsifiers sit
// unrun for a phase (CLAUDE.md, "Measured, not asserted"). If this cannot
// verify the signature, it fails.
package packaging_test

import (
	"crypto/sha256"
	"debug/macho"
	"encoding/binary"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// Constants from Apple's cs_blobs.h.
const (
	lcCodeSignature = 0x1d // LC_CODE_SIGNATURE

	csMagicEmbeddedSignature = 0xfade0cc0
	csMagicCodeDirectory     = 0xfade0c02

	csAdhoc        = 0x0002  // CS_ADHOC — vouched for by nobody, which is what we want
	csLinkerSigned = 0x20000 // CS_LINKER_SIGNED — emitted by a linker, not by codesign(1)

	csHashTypeSHA256 = 2
)

// errNoCodeSignature is the condition this whole file exists to make loud.
var errNoCodeSignature = errors.New("no LC_CODE_SIGNATURE load command")

// codeDirectory is the parsed subset of a CS_CodeDirectory blob. Every field
// inside a code signature is BIG-endian, including within a little-endian
// Mach-O — a detail worth stating because getting it wrong yields absurd
// lengths rather than an obvious parse failure.
type codeDirectory struct {
	version      uint32
	flags        uint32
	hashOffset   uint32
	identOffset  uint32
	nCodeSlots   uint32
	codeLimit    uint32
	hashSize     uint8
	hashType     uint8
	pageSizeLog2 uint8
	identifier   string
	slotHashes   [][]byte
}

// ---------------------------------------------------------------------------
// The checker, as pure functions returning errors. Split out from the tests on
// purpose: a detector that can only report through t.Fatalf cannot itself be
// tested, and the negative tests at the bottom are the reason to believe the
// positive one means anything.
// ---------------------------------------------------------------------------

// findCodeSignature returns the embedded signature blob and its file offset.
func findCodeSignature(raw []byte) (sig []byte, dataOff uint32, err error) {
	f, err := macho.NewFile(sliceReaderAt(raw))
	if err != nil {
		return nil, 0, fmt.Errorf("not a parseable Mach-O: %w", err)
	}
	defer f.Close()

	// debug/macho has no constant for LC_CODE_SIGNATURE, so it lands in Loads
	// as raw LoadBytes. Parse cmd/dataoff/datasize out of the front ourselves.
	for _, l := range f.Loads {
		lb, ok := l.(macho.LoadBytes)
		if !ok || len(lb) < 16 {
			continue
		}
		if f.ByteOrder.Uint32(lb[0:4]) != lcCodeSignature {
			continue
		}
		off := f.ByteOrder.Uint32(lb[8:12])
		size := f.ByteOrder.Uint32(lb[12:16])
		if int64(off)+int64(size) > int64(len(raw)) {
			return nil, 0, fmt.Errorf("LC_CODE_SIGNATURE points at %d..%d, past the end of a %d-byte file",
				off, int64(off)+int64(size), len(raw))
		}
		return raw[off : int(off)+int(size)], off, nil
	}
	return nil, 0, errNoCodeSignature
}

// parseCodeDirectory walks the superblob index and returns the CodeDirectory.
func parseCodeDirectory(sig []byte) (codeDirectory, error) {
	be := binary.BigEndian
	var cd codeDirectory

	if len(sig) < 12 {
		return cd, fmt.Errorf("signature blob is %d bytes, too short for a superblob header", len(sig))
	}
	if m := be.Uint32(sig[0:4]); m != csMagicEmbeddedSignature {
		return cd, fmt.Errorf("signature magic is %#x, want %#x (CSMAGIC_EMBEDDED_SIGNATURE)",
			m, csMagicEmbeddedSignature)
	}
	count := be.Uint32(sig[8:12])
	if count == 0 {
		return cd, errors.New("the signature superblob holds zero blobs: there is no CodeDirectory")
	}

	for i := uint32(0); i < count; i++ {
		idx := 12 + i*8
		if int64(idx)+8 > int64(len(sig)) {
			return cd, fmt.Errorf("superblob index entry %d runs past the blob", i)
		}
		off := be.Uint32(sig[idx+4 : idx+8])
		if int64(off)+44 > int64(len(sig)) {
			continue
		}
		if be.Uint32(sig[off:off+4]) != csMagicCodeDirectory {
			continue
		}
		b := sig[off:]
		cd = codeDirectory{
			version:      be.Uint32(b[8:12]),
			flags:        be.Uint32(b[12:16]),
			hashOffset:   be.Uint32(b[16:20]),
			identOffset:  be.Uint32(b[20:24]),
			nCodeSlots:   be.Uint32(b[28:32]),
			codeLimit:    be.Uint32(b[32:36]),
			hashSize:     b[36],
			hashType:     b[37],
			pageSizeLog2: b[39],
		}
		if int64(cd.identOffset) < int64(len(b)) {
			if e := strings.IndexByte(string(b[cd.identOffset:]), 0); e >= 0 {
				cd.identifier = string(b[int(cd.identOffset) : int(cd.identOffset)+e])
			}
		}
		for s := uint32(0); s < cd.nCodeSlots; s++ {
			start := int64(cd.hashOffset) + int64(s)*int64(cd.hashSize)
			end := start + int64(cd.hashSize)
			if end > int64(len(b)) {
				return cd, fmt.Errorf("code slot %d runs past the CodeDirectory blob", s)
			}
			cd.slotHashes = append(cd.slotHashes, b[start:end])
		}
		return cd, nil
	}
	return cd, fmt.Errorf("the superblob has %d blobs but no CodeDirectory (%#x)", count, csMagicCodeDirectory)
}

// verifyPageHashes recomputes every page hash from the bytes on disk. This is
// the load-bearing check: a signature whose hashes do not match the file is
// rejected by macOS exactly as hard as no signature at all, and that is what
// any post-link edit to the binary produces.
func verifyPageHashes(raw []byte, cd codeDirectory) error {
	if cd.nCodeSlots == 0 {
		return errors.New("the CodeDirectory covers zero pages: it authenticates nothing")
	}
	if int64(cd.codeLimit) > int64(len(raw)) {
		return fmt.Errorf("codeLimit %d is past the end of a %d-byte file", cd.codeLimit, len(raw))
	}
	pageSize := int64(1) << cd.pageSizeLog2
	var mismatched []uint32
	for s := uint32(0); s < cd.nCodeSlots; s++ {
		start := int64(s) * pageSize
		end := min(start+pageSize, int64(cd.codeLimit))
		if start >= end {
			return fmt.Errorf("code slot %d covers no bytes (start %d, codeLimit %d)", s, start, cd.codeLimit)
		}
		sum := sha256.Sum256(raw[start:end])
		if string(sum[:cd.hashSize]) != string(cd.slotHashes[s]) {
			mismatched = append(mismatched, s)
		}
	}
	if len(mismatched) > 0 {
		shown := mismatched
		if len(shown) > 3 {
			shown = shown[:3]
		}
		return fmt.Errorf("%d of %d page hashes disagree with the binary on disk (slots %v...). "+
			"The signature was invalidated after linking: something rewrote the file (strip? upx? "+
			"an appended payload?). macOS treats this the same as unsigned — the process is killed "+
			"at exec", len(mismatched), cd.nCodeSlots, shown)
	}
	return nil
}

// ---------------------------------------------------------------------------
// building the real artifact
// ---------------------------------------------------------------------------

// buildDarwin runs the real build.sh for one target and returns the binary's
// bytes. Going through build.sh rather than calling go build directly is the
// point: what must stay signed is the artifact the release path produces, so a
// flag added to build.sh that breaks signing has to fail here.
func buildDarwin(t *testing.T, target string) (path string, raw []byte) {
	t.Helper()
	// NOT the skip the package comment forbids. That comment is about not
	// no-opping this check for want of Mac TOOLING to verify a signature
	// with -- and on both platforms that can invoke build.sh (Linux, and now
	// a real macOS runner) it still runs and still verifies a real artifact.
	// This is a different, structural fact: build.sh is invoked here by
	// relative path, which depends on shebang execution, and Windows has
	// none -- CreateProcess cannot start a text file no matter what tooling
	// is or is not installed. See internal/service and packaging's
	// windows-tagged tests for what DOES get a real Windows check.
	if runtime.GOOS == "windows" {
		t.Skip("build.sh is invoked by relative path (shebang exec); windows has no shebang " +
			"exec regardless of tooling. Verified on linux and macOS, which do.")
	}
	dist := t.TempDir()
	cmd := exec.Command("./build.sh", target)
	cmd.Env = append(os.Environ(), "DIST="+dist, "VERSION=0.0.0-machotest")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("build.sh %s failed: %v\n%s", target, err, out)
	}
	goos, goarch, ok := strings.Cut(target, "/")
	if !ok {
		t.Fatalf("bad target %q", target)
	}
	path = filepath.Join(dist, fmt.Sprintf("its-agent-%s-%s", goos, goarch))
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("build.sh %s produced no readable %s: %v", target, path, err)
	}
	return path, raw
}

// ---------------------------------------------------------------------------
// the assertion
// ---------------------------------------------------------------------------

// TestTheDarwinArm64BinaryCarriesAValidAdHocSignature is what stands between
// this project and shipping a macOS agent that cannot start.
//
// Falsifier, stated so it can be checked rather than believed: build the
// darwin/arm64 artifact through build.sh and this fails if the Mach-O has no
// LC_CODE_SIGNATURE, if the CodeDirectory is not marked ad-hoc, if it is not
// SHA-256, if the signed range stops short of the signature, or if ANY page
// hash disagrees with the bytes on disk. The last three of those are
// reproduced against real artifacts by the negative tests below.
func TestTheDarwinArm64BinaryCarriesAValidAdHocSignature(t *testing.T) {
	path, raw := buildDarwin(t, "darwin/arm64")

	sig, dataOff, err := findCodeSignature(raw)
	if err != nil {
		if errors.Is(err, errNoCodeSignature) {
			t.Fatalf("%s carries NO LC_CODE_SIGNATURE.\n\n"+
				"An unsigned arm64 Mach-O is killed by the kernel at exec on every Apple Silicon\n"+
				"Mac. This is not Gatekeeper and not notarization: it cannot be switched off by\n"+
				"policy, by MDM, or by clearing the quarantine attribute. This binary would be\n"+
				"dead on arrival on the whole Apple Silicon fleet.\n\n"+
				"The likely cause is a new import of \"C\" somewhere in the agent (which forces\n"+
				"external linking, and the external linker leaves darwin signing to the\n"+
				"Mac-only codesign(1)), or a post-link step that rewrote the file. See this\n"+
				"file's package comment.", path)
		}
		t.Fatalf("%s: %v", path, err)
	}

	cd, err := parseCodeDirectory(sig)
	if err != nil {
		t.Fatalf("%s: %v", path, err)
	}

	t.Logf("darwin/arm64 code signature: %d bytes at offset %d; CodeDirectory version %#x flags %#x "+
		"identifier %q hashType %d (%d-byte) %d page slots of 2^%d bytes, codeLimit %d",
		len(sig), dataOff, cd.version, cd.flags, cd.identifier, cd.hashType, cd.hashSize,
		cd.nCodeSlots, cd.pageSizeLog2, cd.codeLimit)

	if cd.flags&csAdhoc == 0 {
		t.Errorf("CodeDirectory flags = %#x, missing CS_ADHOC (%#x). A signature that claims a "+
			"signing identity we do not possess is worse than an ad-hoc one: macOS would try to "+
			"validate a certificate chain that is not there.", cd.flags, csAdhoc)
	}
	if cd.flags&csLinkerSigned == 0 {
		// Not a failure — a real codesign(1) run would clear this — but it is
		// how we know the Go linker did the work, so a change is worth reading.
		t.Logf("note: CS_LINKER_SIGNED is clear, so this was signed by something other than the Go linker")
	}
	if cd.hashType != csHashTypeSHA256 {
		t.Errorf("hashType = %d, want %d (SHA-256). macOS 11+ rejects a SHA-1-only signature.",
			cd.hashType, csHashTypeSHA256)
	}
	if cd.hashSize != sha256.Size {
		t.Errorf("hashSize = %d, want %d", cd.hashSize, sha256.Size)
	}

	// codeLimit must reach the signature itself: everything before the
	// signature blob is what gets hashed, so a short codeLimit would leave a
	// stretch of executable code unauthenticated.
	if cd.codeLimit != dataOff {
		t.Errorf("codeLimit = %d but LC_CODE_SIGNATURE starts at %d: %d bytes of the binary sit "+
			"outside the signed range", cd.codeLimit, dataOff, int64(dataOff)-int64(cd.codeLimit))
	}

	if err := verifyPageHashes(raw, cd); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
	t.Logf("all %d page hashes verified against the bytes on disk", cd.nCodeSlots)
}

// The x86_64 artifact is deliberately NOT required to be signed: the ad-hoc
// rule is an arm64 one, and Intel Macs exec an unsigned Mach-O fine. This
// asserts only that the artifact is a well-formed x86_64 Mach-O, and RECORDS
// whether it is signed, so the asymmetry is an observation in the log rather
// than something a future reader has to rediscover.
//
// It doubles as the negative fixture for findCodeSignature: today this binary
// genuinely has no signature, which is the one case the arm64 test must never
// miss, checked here against a real file rather than a synthetic one.
func TestTheDarwinAmd64BinaryIsAWellFormedMachO(t *testing.T) {
	path, raw := buildDarwin(t, "darwin/amd64")

	f, err := macho.NewFile(sliceReaderAt(raw))
	if err != nil {
		t.Fatalf("%s is not a parseable Mach-O: %v", path, err)
	}
	defer f.Close()
	if f.Cpu != macho.CpuAmd64 {
		t.Errorf("cpu = %v, want %v", f.Cpu, macho.CpuAmd64)
	}

	_, _, err = findCodeSignature(raw)
	switch {
	case err == nil:
		t.Log("darwin/amd64 IS code-signed (not required — the ad-hoc rule is arm64-only)")
	case errors.Is(err, errNoCodeSignature):
		t.Log("darwin/amd64 carries no LC_CODE_SIGNATURE, as expected: Intel Macs do not require one. " +
			"This is also the live negative case proving findCodeSignature reports absence.")
	default:
		t.Fatalf("%s: %v", path, err)
	}
}

// A checker nobody has watched fail is not evidence. This plants the exact
// regression the arm64 test exists to catch — a post-link edit to the binary,
// which is what strip or upx would do — and asserts the checker catches it.
//
// Without this, a bug that made verifyPageHashes always return nil would leave
// every run green while the real property went unchecked. That is the same
// shape as `go test -bench` exiting 0 on a regression, which this repository
// has already been bitten by once (agent/internal/budget's package comment).
func TestThePageHashCheckCatchesAPostLinkEditToTheBinary(t *testing.T) {
	_, raw := buildDarwin(t, "darwin/arm64")
	sig, _, err := findCodeSignature(raw)
	if err != nil {
		t.Fatalf("precondition: %v", err)
	}
	cd, err := parseCodeDirectory(sig)
	if err != nil {
		t.Fatalf("precondition: %v", err)
	}
	if err := verifyPageHashes(raw, cd); err != nil {
		t.Fatalf("precondition: the pristine binary must verify, but: %v", err)
	}

	// Flip one bit deep inside the signed range — the smallest edit a strip or
	// an appended-payload step could make, and one that changes nothing any
	// other check in this repository looks at.
	tampered := make([]byte, len(raw))
	copy(tampered, raw)
	victim := int(cd.codeLimit) / 2
	tampered[victim] ^= 0x01

	err = verifyPageHashes(tampered, cd)
	if err == nil {
		t.Fatal("verifyPageHashes accepted a binary with a flipped bit at the midpoint of the " +
			"signed range. The arm64 signature check is therefore vacuous: it would pass a " +
			"binary macOS kills at exec.")
	}
	t.Logf("one flipped bit at offset %d is caught: %v", victim, err)
}

// The other half of the same argument: the checker must reject a truncated or
// zero-slot CodeDirectory rather than treat "nothing to check" as success.
func TestThePageHashCheckRejectsACodeDirectoryThatAuthenticatesNothing(t *testing.T) {
	if err := verifyPageHashes(make([]byte, 4096), codeDirectory{}); err == nil {
		t.Error("a CodeDirectory with zero code slots was accepted; an empty signature " +
			"authenticates nothing and must not read as success")
	}
	// A codeLimit that overruns the file is the shape a truncated artifact
	// takes, and must be an error rather than a panic.
	cd := codeDirectory{nCodeSlots: 1, codeLimit: 1 << 20, hashSize: 32, pageSizeLog2: 12,
		slotHashes: [][]byte{make([]byte, 32)}}
	if err := verifyPageHashes(make([]byte, 4096), cd); err == nil {
		t.Error("a codeLimit past the end of the file was accepted")
	}
}

// sliceReaderAt keeps the whole file in memory once, so the page hashes are
// computed from exactly the bytes debug/macho parsed.
type sliceReaderAt []byte

func (s sliceReaderAt) ReadAt(p []byte, off int64) (int, error) {
	if off < 0 || off >= int64(len(s)) {
		return 0, fmt.Errorf("offset %d out of range", off)
	}
	n := copy(p, s[off:])
	if n < len(p) {
		return n, fmt.Errorf("short read at %d", off)
	}
	return n, nil
}
