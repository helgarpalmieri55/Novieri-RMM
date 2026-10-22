package backup

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// PATH HANDLING, WHICH IS WHERE A RESTORE GETS DANGEROUS.
//
// A restore runs as SYSTEM (Windows) or root (Unix) and writes files whose
// names come out of a manifest. That is the classic archive-extraction attack
// surface — Zip Slip, CVE-2007-4559, and every re-discovery since — and it has
// two shapes, both of which are defended here and tested in security_test.go:
//
//  1. LEXICAL ESCAPE. An entry named `../../etc/cron.d/x`, or `/etc/shadow`, or
//     (on Windows) `..\..\Windows\System32\x`. Defended by relUnderRoot, which
//     rebuilds the path from components and refuses `..` rather than stripping
//     it — stripping is how `....//` bypasses get in.
//
//  2. SYMLINK ESCAPE, which a lexical check does not see at all. An entry
//     `data -> /etc` followed by `data/passwd` stays under the root at every
//     step lexically, and lands in /etc. Defended twice: symlinks are created
//     LAST, after every file and directory, so a symlinked parent never exists
//     while children are being written; and every path component is Lstat-ed
//     before it is used, so a symlink planted in the target by something else
//     is refused rather than followed.
//
// THE ROOT IS ALWAYS EXPLICIT. There is no restore-to-original path in this
// capability: the pinned contract's `target` is `{"path": ...}` for a restore
// and `{}` for a verify (api.py), so every byte written goes under one root
// that the control plane named or that this agent created for a scratch verify.
// That is not a limitation being worked around, it is what makes "nothing was
// written outside the target" a property that can be asserted at all.

// windowsReserved are the DOS device names. Windows resolves them in any
// directory, so a restored file called `CON` does not become a file — it
// becomes a write to the console device, and `LPT1` becomes a write to a
// printer port. The superscript-digit forms (`COM¹`) are reserved too.
//
// Refused on EVERY platform, not only Windows, so that the behaviour is
// identical on the machine that runs the tests and the machine that runs the
// fleet. A guard that only exists on the OS where it cannot be tested is a
// guard nobody has ever seen work.
var windowsReserved = map[string]struct{}{
	"con": {}, "prn": {}, "aux": {}, "nul": {}, "conin$": {}, "conout$": {},
	"com0": {}, "com1": {}, "com2": {}, "com3": {}, "com4": {}, "com5": {},
	"com6": {}, "com7": {}, "com8": {}, "com9": {},
	"com¹": {}, "com²": {}, "com³": {},
	"lpt0": {}, "lpt1": {}, "lpt2": {}, "lpt3": {}, "lpt4": {}, "lpt5": {},
	"lpt6": {}, "lpt7": {}, "lpt8": {}, "lpt9": {},
	"lpt¹": {}, "lpt²": {}, "lpt³": {},
}

// errUnsafeName marks an entry this agent will not create but which is not
// evidence of tampering — a macOS file literally named `CON`, or one with a
// trailing dot. The entry is skipped and reported in `mismatch`; the job
// continues.
//
// Distinct from a REFUSAL on purpose. A `..` in a manifest cannot have been
// produced by this package's writer, so it means the manifest or the object
// store is under someone else's control and the right answer is to stop. A
// reserved device name CAN be produced by a legitimate macOS source tree, and
// refusing the whole job over one would make a single oddly-named file cost a
// customer their entire restore.
var errUnsafeName = errors.New("backup: entry name cannot be created safely")

// toManifestPath converts an OS path to the manifest's spelling: forward
// slashes, everywhere, including Windows.
//
// `C:\Users\a\x` becomes `C:/Users/a/x`. The drive letter is KEPT because
// `browse_manifest` shows these paths to an operator who then picks a
// selection, and a manifest that had silently dropped the volume would offer
// two indistinguishable `/Users/a/x` entries from two different disks. On
// Unix a backslash is an ordinary filename character and stays one here.
func toManifestPath(p string) string {
	return filepath.ToSlash(p)
}

// pathRules is how a manifest path is read when it is mapped under a restore
// root. The rules are the RESTORING host's: whether `..\x` escapes is a
// property of the filesystem being written, not of the one that was read.
//
// The zero value means "this host", so production never sets it; tests set
// windowsPaths to prove the Windows rules on a Linux runner.
type pathRules uint8

const (
	hostPaths pathRules = iota
	posixPaths
	windowsPaths
)

func (r pathRules) windows() bool {
	if r == hostPaths {
		return runtime.GOOS == "windows"
	}
	return r == windowsPaths
}

// relUnderRoot turns a manifest path into a relative, slash-separated path that
// is safe to join to a restore root.
//
// It REBUILDS rather than sanitises. Every component is examined and either
// accepted, dropped (`.`, empty) or rejected; nothing is substituted or
// stripped, because strip-and-retry is what every `....//`-style bypass
// exploits. `/data/x` becomes `data/x`; `C:/Users/a` becomes `C/Users/a`: the
// volume becomes a directory, so two disks cannot collide under one root, and
// the result is a legal path on every OS.
//
// Windows rules add three things: `\` is a separator; a `\\?\C:\` long-path
// prefix means the drive it names; and a name NTFS cannot store (`<>"|?*`,
// control characters) is skipped. Unix rules refuse a backslash outright.
func (r pathRules) relUnderRoot(p string) (string, error) {
	if p == "" {
		return "", jobs.Refuse("backup.restore: the manifest contains an entry with an empty path")
	}
	if strings.ContainsRune(p, 0) {
		return "", jobs.Refuse("backup.restore: a manifest entry path contains a NUL byte. This " +
			"cannot come from this agent's own writer; the manifest or the object store is not " +
			"under the control plane's control. Nothing was written")
	}
	win := r.windows()
	if win {
		p = strings.ReplaceAll(p, `\`, "/")
		var err error
		if p, err = stripWin32Prefix(p); err != nil {
			return "", err
		}
	} else if strings.ContainsRune(p, '\\') {
		// On Windows this would be a separator: `..\..\Windows\System32\x` is
		// one harmless component here and a traversal on the fleet's primary
		// OS. Refusing it keeps one manifest from meaning two different trees
		// on two platforms.
		return "", jobs.Refuse("backup.restore: a manifest entry path contains a backslash. On " +
			"Windows that is a path separator, so such an entry is a traversal wearing a " +
			"filename's clothes; this agent's writer emits forward slashes only. Nothing was " +
			"written")
	}
	// `//server/share` is a UNC path: a restore that honoured one would write
	// to another machine over SMB, using this endpoint's SYSTEM credentials.
	// That is a different authorization question from "restore to this device"
	// and the control plane has no way to express it.
	if strings.HasPrefix(p, "//") {
		return "", refuseUNC()
	}

	parts := strings.Split(p, "/")
	out := make([]string, 0, len(parts))
	for i, c := range parts {
		switch {
		case c == "" || c == ".":
			continue
		case c == "..":
			return "", jobs.Refuse("backup.restore: a manifest entry path contains a `..` " +
				"component. This agent's writer never emits one, so the manifest or the object " +
				"store has been modified. Nothing was written")
		}
		if i == 0 && len(c) == 2 && c[1] == ':' && isDriveLetter(c[0]) {
			// `C:` -> `C`. Anywhere other than the first component a colon is
			// refused below, because `x:stream` is an NTFS alternate data
			// stream and writing one is writing somewhere the manifest does not
			// name.
			out = append(out, string(c[0]))
			continue
		}
		if strings.ContainsRune(c, ':') {
			return "", jobs.Refuse("backup.restore: a manifest entry path component contains a " +
				"colon. On NTFS that names an alternate data stream, which is a destination the " +
				"entry does not appear to name. Nothing was written")
		}
		if err := safeComponent(c); err != nil {
			return "", err
		}
		if win && !windowsStorable(c) {
			return "", errUnsafeName
		}
		out = append(out, c)
	}
	if len(out) == 0 {
		return "", jobs.Refuse("backup.restore: a manifest entry path names no file")
	}
	return strings.Join(out, "/"), nil
}

func refuseUNC() error {
	return jobs.Refuse("backup.restore: a manifest entry path is a UNC path. Restoring " +
		"one would write to another host over SMB with this endpoint's machine credentials. " +
		"Nothing was written")
}

// stripWin32Prefix removes a `//?/` long-path prefix in front of a drive
// letter (slashes already normalised). Every other namespace form -- `UNC`, a
// volume GUID, GLOBALROOT, the `//./` device namespace -- names something
// other than a directory tree, and this agent's writer records none of them.
func stripWin32Prefix(p string) (string, error) {
	if !strings.HasPrefix(p, "//?/") && !strings.HasPrefix(p, "//./") {
		return p, nil
	}
	rest := p[4:]
	if p[2] == '?' && len(rest) >= 2 && rest[1] == ':' && isDriveLetter(rest[0]) &&
		(len(rest) == 2 || rest[2] == '/') {
		return rest, nil
	}
	if len(rest) >= 3 && strings.EqualFold(rest[:3], "UNC") && (len(rest) == 3 || rest[3] == '/') {
		return "", refuseUNC()
	}
	return "", jobs.Refuse("backup.restore: a manifest entry path is a Windows device or volume " +
		"namespace path, which this agent's writer never records. Nothing was written")
}

func isDriveLetter(b byte) bool {
	return (b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z')
}

// windowsStorable reports whether NTFS can hold c under that name. Separators
// and the colon are the caller's; these are the rest of Win32's forbidden set.
func windowsStorable(c string) bool {
	for i := 0; i < len(c); i++ {
		switch b := c[i]; {
		case b < 0x20, b == '<', b == '>', b == '"', b == '|', b == '?', b == '*':
			return false
		}
	}
	return true
}

// safeComponent rejects names Windows does not store as written. Returns
// errUnsafeName, which the caller skips rather than refuses — see that
// variable's comment.
func safeComponent(c string) error {
	base := c
	if i := strings.IndexByte(base, '.'); i > 0 {
		base = base[:i]
	}
	// Windows ignores spaces before the extension: `CON .txt` is the console.
	base = strings.TrimRight(base, " ")
	if _, bad := windowsReserved[strings.ToLower(base)]; bad {
		return errUnsafeName
	}
	// Windows silently strips a trailing dot or space, so `foo.` and `foo`
	// become the same file: restoring both would have the second overwrite the
	// first, quietly, with different contents.
	if strings.HasSuffix(c, ".") || strings.HasSuffix(c, " ") {
		return errUnsafeName
	}
	return nil
}

// safeJoin is the second, independent containment check.
//
// relUnderRoot has already refused every component that could escape, so this
// can only fail if that function has a bug — which is exactly why it is here.
// Two checks that would both have to be wrong is the difference between a
// defence and an assumption.
func safeJoin(root, rel string) (string, error) {
	dest := filepath.Join(root, filepath.FromSlash(rel))
	cleanRoot := filepath.Clean(root)
	if dest != cleanRoot && !strings.HasPrefix(dest, cleanRoot+string(filepath.Separator)) {
		return "", jobs.Refuse("backup.restore: a manifest entry resolves outside the restore " +
			"root. Nothing was written")
	}
	return dest, nil
}

// ensureDirUnderRoot creates rel (a slash path, relative to root) as a
// directory chain, refusing to traverse a symlink at any level.
//
// os.MkdirAll would be one line and would happily walk through a symlink,
// which is the whole of attack shape 2 above.
func ensureDirUnderRoot(root, rel string, mode os.FileMode) error {
	cur := root
	for _, c := range strings.Split(rel, "/") {
		if c == "" {
			continue
		}
		cur = filepath.Join(cur, c)
		fi, err := os.Lstat(cur)
		switch {
		case err == nil && fi.Mode()&fs.ModeSymlink != 0:
			return jobs.Refuse("backup.restore: a component of the restore path is a symbolic " +
				"link. Following it would write outside the restore root, which is the whole " +
				"of the escape a lexical path check does not see. Nothing further was written")
		case err == nil && fi.IsDir():
			continue
		case err == nil:
			return jobs.Refuse("backup.restore: a component of the restore path already exists " +
				"and is not a directory. Nothing further was written")
		case os.IsNotExist(err):
			if err := os.Mkdir(cur, mode); err != nil && !os.IsExist(err) {
				return err
			}
		default:
			return err
		}
	}
	return nil
}

// assertParentNotSymlink re-checks the chain immediately before a write.
//
// ensureDirUnderRoot created it, so under this package's own control nothing
// can have changed — but a restore root is an ordinary directory on a machine
// with other processes on it, and TOCTOU is exactly the window a local attacker
// waits for. Cheap: one Lstat per component, on a path already in the dentry
// cache.
func assertParentNotSymlink(root, rel string) error {
	cur := root
	parts := strings.Split(rel, "/")
	for _, c := range parts[:len(parts)-1] {
		if c == "" {
			continue
		}
		cur = filepath.Join(cur, c)
		fi, err := os.Lstat(cur)
		if err != nil {
			return err
		}
		if fi.Mode()&fs.ModeSymlink != 0 {
			return jobs.Refuse("backup.restore: a parent directory of a restore destination " +
				"became a symbolic link while the restore was running. Nothing further was written")
		}
	}
	return nil
}

// ---------------------------------------------------------------------------
// Selection and exclusion
// ---------------------------------------------------------------------------

// normaliseSelection puts the operator's chosen paths into selectionKey form.
func (r pathRules) normaliseSelection(sel []string) []string {
	out := make([]string, 0, len(sel))
	for _, s := range sel {
		s = strings.TrimRight(r.selectionKey(strings.TrimSpace(s)), "/")
		if s == "" {
			continue
		}
		out = append(out, s)
	}
	return out
}

// selectionKey is the one spelling selections and entry paths are compared
// in, and BOTH sides go through it. Normalising only the selection is how a
// hostile `..\x` entry went unselected -- and so unrefused -- on Windows.
//
// Under Windows rules `\` is `/`, a `\\?\` prefix before a drive is dropped
// and the drive letter is upper-cased. Under Unix rules a path is already in
// manifest spelling and is returned as is.
func (r pathRules) selectionKey(p string) string {
	if !r.windows() {
		return p
	}
	p = strings.ReplaceAll(p, `\`, "/")
	if strings.HasPrefix(p, "//?/") && len(p) >= 6 && p[5] == ':' && isDriveLetter(p[4]) {
		p = p[4:]
	}
	if len(p) >= 2 && p[1] == ':' && p[0] >= 'a' && p[0] <= 'z' {
		p = string(p[0]-'a'+'A') + p[1:]
	}
	return p
}

// selects reports whether an entry path, already in selectionKey form, is
// covered by the selection.
//
// A selection entry covers itself and everything beneath it, and `beneath` is
// tested with an explicit `/` — `HasPrefix(p, s)` alone would make a selection
// of `/data/report` also select `/data/report-2024-salaries.xlsx`.
func selects(selection []string, p string) bool {
	for _, s := range selection {
		if p == s || strings.HasPrefix(p, s+"/") {
			return true
		}
	}
	return false
}

// excluded lives in match.go.

// ---------------------------------------------------------------------------
// Redaction
// ---------------------------------------------------------------------------

// redact strips file paths out of an error so it can be logged.
//
// CLAUDE.md and the module brief: backed-up data is customer data, and NOTHING
// about file contents or paths goes in a log line. The standard library makes
// that easy to violate by accident — `os.Open` returns a *PathError whose
// Error() is "open /home/alice/tax-return-2025.pdf: permission denied", and
// `log.Warn("...", "err", err)` on an endpoint puts a customer's filename into
// a log file that ships to a SIEM.
//
// So every log call in this package passes errors through here, and
// security_test.go asserts over a whole run+restore that no source path, no
// file content and no key material appears in any emitted record.
//
// The result keeps the operation and the OS's own words, which differ by
// platform ("no such file or directory" on Unix, "The system cannot find the
// path specified." on Windows), and still unwraps to the underlying errno, so
// errors.Is(redact(err), fs.ErrNotExist) holds wherever it held before.
func redact(err error) error {
	if err == nil {
		return nil
	}
	var pe *os.PathError
	if errors.As(err, &pe) {
		return &redactedError{op: pe.Op, err: redact(pe.Err)}
	}
	var le *os.LinkError
	if errors.As(err, &le) {
		return &redactedError{op: le.Op, err: redact(le.Err)}
	}
	// os.PathError IS fs.PathError (a type alias), so the case above covers
	// io/fs errors too; there is no third case to write.
	return err
}

// redactedError is a path error with the path removed.
type redactedError struct {
	op  string
	err error
}

func (e *redactedError) Error() string { return e.op + ": " + e.err.Error() }
func (e *redactedError) Unwrap() error { return e.err }
