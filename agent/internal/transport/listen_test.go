package transport

import (
	"bufio"
	"context"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// Invariant #2 (CLAUDE.md): agents connect OUTBOUND ONLY, over mutual TLS. The
// server never opens an inbound connection to an endpoint, and the agent has no
// listening port. These two tests assert that at the two levels it can be
// broken at: the running process, and the source that will be running tomorrow.

// ---------------------------------------------------------------------------
// Level 1: the running process
// ---------------------------------------------------------------------------

func TestAgentOpensNoListeningSocket(t *testing.T) {
	// Falsifying observation: if the transport ever bound a port, this test
	// reports the extra LISTEN entry with its port number. "No error was
	// returned" is not an observation; a bound port is.
	if runtime.GOOS != "linux" {
		t.Skip("reads /proc/net/tcp; CI is linux")
	}
	pki := newTestPKI(t)
	g := newFakeGateway(t, pki)

	// The snapshot is taken AFTER the fake gateway is listening, so the only
	// new LISTEN entry a failure can report is one the agent bound.
	before := listeningPorts(t)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	conn, err := Dial(ctx, pki.options(g.url()))
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer conn.Close()
	if err := conn.Send(ctx, wire.Heartbeat{TS: "2026-08-05T12:00:00Z"}); err != nil {
		t.Fatalf("Send: %v", err)
	}
	select {
	case <-g.received:
	case <-time.After(5 * time.Second):
		t.Fatal("the connection never carried a frame; this test would be vacuous")
	}

	if extra := added(before, listeningPorts(t)); len(extra) != 0 {
		t.Fatalf("agent bound %v; invariant #2 forbids any listening port", extra)
	}
}

// listeningPorts returns the local ports in TCP_LISTEN (st == 0A) that are
// bound by THIS PROCESS.
//
// Scoped by socket inode, not by namespace. /proc/net/tcp lists every socket
// in the network namespace, and `go test ./...` runs package binaries
// CONCURRENTLY -- so an unscoped read reports the enroll package's httptest
// server as a port this agent bound. That was observed: a run of the full
// module under -race failed with `agent bound [tcp:81A3]`, a port belonging to
// another test binary. An invariant test that fails for reasons outside the
// thing it guards gets deleted, so it is scoped here rather than tolerated.
//
// TCP only, deliberately. A bound UDP socket has no LISTEN state, so the
// /proc/net/udp equivalent cannot distinguish "a service" from the Go
// resolver's connected sockets. UDP and raw sockets are covered by the
// source-level walker below, which is the check Task 13 will have to amend in
// the open.
func listeningPorts(t *testing.T) map[string]bool {
	t.Helper()
	mine := ownSocketInodes(t)
	out := map[string]bool{}
	for _, path := range []string{"/proc/net/tcp", "/proc/net/tcp6"} {
		f, err := os.Open(path)
		if err != nil {
			continue
		}
		scanner := bufio.NewScanner(f)
		scanner.Scan() // header
		for scanner.Scan() {
			fields := strings.Fields(scanner.Text())
			// sl local rem st tx:rx tr:when retrnsmt uid timeout inode
			if len(fields) < 10 || fields[3] != "0A" || !mine[fields[9]] {
				continue
			}
			local := fields[1]
			i := strings.LastIndex(local, ":")
			if i < 0 {
				continue
			}
			out[fmt.Sprintf("%s:%s", filepath.Base(path), local[i+1:])] = true
		}
		f.Close()
	}
	return out
}

// ownSocketInodes reads this process's open sockets from /proc/self/fd, where
// each socket fd is a symlink to "socket:[<inode>]".
func ownSocketInodes(t *testing.T) map[string]bool {
	t.Helper()
	entries, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		t.Fatalf("cannot read /proc/self/fd: %v", err)
	}
	inodes := map[string]bool{}
	for _, e := range entries {
		target, err := os.Readlink(filepath.Join("/proc/self/fd", e.Name()))
		if err != nil {
			continue // the fd closed under us; not a socket we can attribute
		}
		if rest, ok := strings.CutPrefix(target, "socket:["); ok {
			inodes[strings.TrimSuffix(rest, "]")] = true
		}
	}
	return inodes
}

func added(before, after map[string]bool) []string {
	var extra []string
	for k := range after {
		if !before[k] {
			extra = append(extra, k)
		}
	}
	sort.Strings(extra)
	return extra
}

// ---------------------------------------------------------------------------
// Level 2: the source
// ---------------------------------------------------------------------------

// forbidden names a call the agent's non-test code may never make, with the
// reason attached so a future reader knows what they are removing.
var forbidden = map[string]string{
	"net.Listen":             "binds a TCP/Unix listener",
	"net.ListenTCP":          "binds a TCP listener",
	"net.ListenUDP":          "binds a UDP socket",
	"net.ListenIP":           "binds a raw IP socket",
	"net.ListenPacket":       "binds a packet socket",
	"net.ListenUnix":         "binds a Unix listener",
	"net.ListenUnixgram":     "binds a Unix datagram socket",
	"net.ListenMulticastUDP": "joins a multicast group on a bound socket",
	"http.ListenAndServe":    "serves HTTP",
	"http.ListenAndServeTLS": "serves HTTPS",
	"http.Serve":             "serves HTTP on a listener",
	"http.ServeTLS":          "serves HTTPS on a listener",
	"icmp.ListenPacket":      "binds an ICMP socket",
	"quic.Listen":            "binds a QUIC listener",
	"websocket.Accept":       "upgrades an INBOUND websocket; the agent only dials",
}

// forbiddenTypes are composite literals / constructions that mean "a server".
var forbiddenTypes = map[string]string{
	"http.Server":      "an HTTP server has to bind a port to be useful",
	"net.ListenConfig": "a ListenConfig exists only to bind something",
}

// allowances is where an exception is declared IN THE OPEN: which file, which
// CALL, and why.
//
// It used to be keyed by file alone, and the walker skipped an allowed file
// entirely. That is too coarse and it was narrowed here rather than left for
// someone to discover: a file-wide exemption for netsweep's raw ICMP socket
// would also have permitted a `net.Listen`, an `http.Server` and a
// `websocket.Accept` in the same file, unchallenged, forever. Now the allowed
// file is still walked and every OTHER forbidden call in it still fails.
//
// Adding an entry is a security decision. The `why` is the argument, and it has
// to survive being read by someone who did not write it.
var allowances = map[string]map[string]string{
	"internal/capabilities/netsweep/probe_icmp.go": {
		"net.ListenPacket": "the raw ICMPv4 socket the net.sweep capability probes with. It is " +
			"opened INSIDE a verified job (after the signed scope_digest check), closed in a " +
			"defer when that job ends, and carries no port: nothing can connect to it and no " +
			"protocol an outsider can start runs on it. What it reads are echo REPLIES to echo " +
			"requests this process sent moments earlier, filtered by a per-job random " +
			"identifier; an unsolicited packet is discarded by that filter and the only effect " +
			"a matching one can have is to add its source address to a set, which is then " +
			"intersected with the signed scope. docs/modules/ipam/spec.md §5.5.",
	},
}

func TestNoAgentSourceFileOpensAListener(t *testing.T) {
	// Falsifying observation: the file, line, and the exact call. A source-level
	// walker is what catches the regression the /proc test cannot -- a listener
	// in a capability that no test happens to exercise.
	if violations := walkForListeners(t, moduleRoot(t)); len(violations) != 0 {
		t.Fatalf("invariant #2 (outbound only) violated:\n  %s", strings.Join(violations, "\n  "))
	}
}

// walkForListeners is the walker, factored out so its own guarantees can be
// tested. A walker nobody has ever seen report a violation is a walker nobody
// knows works.
func walkForListeners(t *testing.T, root string) []string {
	t.Helper()
	fset := token.NewFileSet()
	var violations []string

	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			if name := info.Name(); name == "testdata" || name == ".git" || strings.HasPrefix(name, "_") {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		rel, _ := filepath.Rel(root, path)
		// The allowed file is STILL WALKED. Only the named call is permitted in
		// it; every other forbidden call still fails. See `allowances`.
		allowed := allowances[filepath.ToSlash(rel)]
		file, perr := parser.ParseFile(fset, path, nil, parser.SkipObjectResolution)
		if perr != nil {
			return perr
		}
		ast.Inspect(file, func(n ast.Node) bool {
			var name string
			switch node := n.(type) {
			case *ast.CallExpr:
				name = selectorName(node.Fun)
				if why, bad := forbidden[name]; bad && allowed[name] == "" {
					violations = append(violations, fmt.Sprintf(
						"%s:%d calls %s (%s)", rel, fset.Position(node.Pos()).Line, name, why))
				}
			case *ast.CompositeLit:
				name = selectorName(node.Type)
				if why, bad := forbiddenTypes[name]; bad && allowed[name] == "" {
					violations = append(violations, fmt.Sprintf(
						"%s:%d constructs %s (%s)", rel, fset.Position(node.Pos()).Line, name, why))
				}
			}
			return true
		})
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(violations)
	return violations
}

func TestAnAllowedFileIsStillWalkedForEveryOtherForbiddenCall(t *testing.T) {
	// THE POINT OF NARROWING THE ALLOWANCE. It used to be keyed by file, and an
	// allowed file was skipped whole -- so netsweep's raw ICMP socket would have
	// bought a permanent exemption for `net.Listen`, `http.Server` and
	// `websocket.Accept` in the same file.
	//
	// FALSIFYING OBSERVATION: zero violations for a file that holds BOTH the
	// allowed call and a forbidden one. That is what the old, file-keyed
	// allowance produced.
	root := t.TempDir()
	rel := "internal/capabilities/netsweep/probe_icmp.go"
	if _, ok := allowances[rel]; !ok {
		t.Fatalf("the allowance for %s is gone; this test is about that entry", rel)
	}
	dir := filepath.Join(root, filepath.Dir(rel))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	src := "package netsweep\n\nimport \"net\"\n\n" +
		"func allowed() { _, _ = net.ListenPacket(\"ip4:icmp\", \"0.0.0.0\") }\n" +
		"func sneaked() { _, _ = net.Listen(\"tcp\", \":8080\") }\n"
	if err := os.WriteFile(filepath.Join(root, rel), []byte(src), 0o600); err != nil {
		t.Fatal(err)
	}

	violations := walkForListeners(t, root)
	if len(violations) != 1 {
		t.Fatalf("walker reported %v; want exactly one violation (the net.Listen), with the "+
			"allowed net.ListenPacket permitted", violations)
	}
	if !strings.Contains(violations[0], "net.Listen (") {
		t.Fatalf("the reported violation is %q; want the net.Listen", violations[0])
	}
}

func TestEveryAllowanceIsStillUsed(t *testing.T) {
	// A stale allowance is a permanent hole nobody is defending. If the file is
	// gone, or no longer makes the call, the entry must go too.
	//
	// FALSIFYING OBSERVATION: an allowance whose file does not exist, or whose
	// permitted call does not appear in it.
	root := moduleRoot(t)
	for rel, calls := range allowances {
		body, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(rel)))
		if err != nil {
			t.Fatalf("allowance names %s, which does not exist: %v", rel, err)
		}
		for call, why := range calls {
			if why == "" {
				t.Fatalf("the allowance for %s in %s carries no reason", call, rel)
			}
			if !strings.Contains(string(body), call+"(") {
				t.Fatalf("%s is allowed to call %s and does not; a stale allowance is a hole "+
					"nobody is defending", rel, call)
			}
		}
	}
}

func TestTheListenerWalkerActuallyWalksSomething(t *testing.T) {
	// A walker pointed at an empty directory passes forever. Assert it sees the
	// non-test source files it is supposed to be guarding.
	root := moduleRoot(t)
	n := 0
	_ = filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() && strings.HasSuffix(path, ".go") && !strings.HasSuffix(path, "_test.go") {
			n++
		}
		return nil
	})
	if n < 4 {
		t.Fatalf("walked %d non-test .go files under %s; the root is wrong", n, root)
	}
}

func selectorName(e ast.Expr) string {
	switch t := e.(type) {
	case *ast.SelectorExpr:
		if ident, ok := t.X.(*ast.Ident); ok {
			return ident.Name + "." + t.Sel.Name
		}
	case *ast.StarExpr:
		return selectorName(t.X)
	}
	return ""
}

// moduleRoot walks up from this test file to the directory holding go.mod.
func moduleRoot(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatal("no go.mod above the test's working directory")
		}
		dir = parent
	}
}
