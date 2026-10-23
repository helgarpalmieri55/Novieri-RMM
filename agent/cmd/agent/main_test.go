package main

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/chunkreply"
	"github.com/toeshee/it-suite/agent/internal/readreply"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

func quietLog() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// fakeConn is the connSender the test attaches in place of a real
// *transport.Conn. It only needs a Send method: both chunkreply.Sender and
// readreply.Sender are exactly that method, structurally, so this one type
// satisfies both without any conversion.
type fakeConn struct{}

func (fakeConn) Send(context.Context, any) error { return nil }

// waitUntil polls cond until it is true or timeout elapses. It never blocks
// past timeout, so a registry that never reaches the awaited state (e.g.
// because a planted deletion skipped an Attach) cannot hang the test -- the
// caller proceeds and the missing behaviour shows up as a wrong Query result
// instead of a stuck test run.
func waitUntil(timeout time.Duration, cond func() bool) {
	deadline := time.Now().Add(timeout)
	for !cond() {
		if time.Now().After(deadline) {
			return
		}
		time.Sleep(time.Millisecond)
	}
}

// runQuery runs fn in its own goroutine and returns its error, or ok=false if
// fn had not returned within timeout. Bounding every Query call this way
// means a planted deletion that leaves a registry blocked (a skipped Detach)
// fails this test in `timeout`, not in the registry's own hour-long Deadline.
func runQuery(fn func() error, timeout time.Duration) (err error, ok bool) {
	done := make(chan error, 1)
	go func() { done <- fn() }()
	select {
	case err := <-done:
		return err, true
	case <-time.After(timeout):
		return nil, false
	}
}

// TestReplyBindersAttachAndDetachBothRegistries drives replyBinders -- the
// SAME function run's OnConnected and OnDisconnect call, not a parallel copy
// of its wiring -- and asserts observable registry behaviour rather than
// binder shape. That is deliberate: a test that only checked "two binders
// came back" would still pass if one of the four Attach/Detach calls inside
// replyBinders were deleted, since the binder slice itself would be
// unchanged. Instead:
//
//   - before Attach runs, Query on each registry must fail ErrNoConnection.
//   - after Attach runs, a Query that goes pending must fail
//     ErrDisconnected as soon as Detach runs -- not after its one-hour
//     Deadline.
//   - after Detach runs, a fresh Query must fail ErrNoConnection again,
//     immediately.
//
// Deleting chunkAttach's, chunkDetach's, readAttach's, or readDetach's
// single line inside replyBinders leaves the function compiling (an empty
// function literal is legal Go) but turns exactly one of these assertions
// red, within the bounded waits above -- never a hang.
func TestReplyBindersAttachAndDetachBothRegistries(t *testing.T) {
	replies := chunkreply.New(quietLog())
	readGrants := readreply.New(quietLog())
	// Long enough that the registry's own Deadline is never what ends a
	// wait in this test -- Detach must be what ends it.
	replies.Deadline = time.Hour
	readGrants.Deadline = time.Hour

	binders := replyBinders(replies, readGrants)
	if len(binders) != 2 {
		t.Fatalf("replyBinders returned %d binders, want 2", len(binders))
	}

	// -- Before Attach: no connection, so Query must refuse immediately. --
	if err, ok := runQuery(func() error {
		_, err := replies.Query(context.Background(), wire.ChunkQuery{JobID: "j", QueryID: "pre"})
		return err
	}, 2*time.Second); !ok {
		t.Fatal("chunkreply.Query before Attach did not return within 2s")
	} else if !errors.Is(err, chunkreply.ErrNoConnection) {
		t.Fatalf("chunkreply.Query before Attach: err = %v, want ErrNoConnection", err)
	}
	if err, ok := runQuery(func() error {
		_, err := readGrants.Query(context.Background(), wire.ReadQuery{JobID: "j", QueryID: "pre"})
		return err
	}, 2*time.Second); !ok {
		t.Fatal("readreply.Query before Attach did not return within 2s")
	} else if !errors.Is(err, readreply.ErrNoConnection) {
		t.Fatalf("readreply.Query before Attach: err = %v, want ErrNoConnection", err)
	}

	// -- Attach, exactly as OnConnected does: one connection handed to every
	// binder. --
	for _, b := range binders {
		b.attach(fakeConn{})
	}

	chunkResult := make(chan error, 1)
	go func() {
		_, err := replies.Query(context.Background(), wire.ChunkQuery{JobID: "j", QueryID: "pending"})
		chunkResult <- err
	}()
	readResult := make(chan error, 1)
	go func() {
		_, err := readGrants.Query(context.Background(), wire.ReadQuery{JobID: "j", QueryID: "pending"})
		readResult <- err
	}()
	// Give both a chance to land in "pending" -- but do not wait forever: if
	// Attach was skipped, the query above already failed instead of going
	// pending, and chunkResult/readResult already hold that answer.
	waitUntil(2*time.Second, func() bool {
		return replies.Pending() > 0 && readGrants.Pending() > 0
	})

	// -- Detach, exactly as OnDisconnect does: both pending waits must fail
	// PROMPTLY, not wait out the one-hour Deadline. --
	cause := errors.New("connection lost")
	for _, b := range binders {
		b.detach(cause)
	}

	select {
	case err := <-chunkResult:
		if !errors.Is(err, chunkreply.ErrDisconnected) {
			t.Errorf("chunkreply pending Query result = %v, want ErrDisconnected", err)
		}
	case <-time.After(5 * time.Second):
		t.Error("chunkreply pending Query did not return within 5s of Detach")
	}
	select {
	case err := <-readResult:
		if !errors.Is(err, readreply.ErrDisconnected) {
			t.Errorf("readreply pending Query result = %v, want ErrDisconnected", err)
		}
	case <-time.After(5 * time.Second):
		t.Error("readreply pending Query did not return within 5s of Detach")
	}

	// -- After Detach: back to no connection, so a fresh Query must refuse
	// immediately, not merely eventually (and never block on a connection
	// Detach should have cleared). --
	if err, ok := runQuery(func() error {
		_, err := replies.Query(context.Background(), wire.ChunkQuery{JobID: "j", QueryID: "post"})
		return err
	}, 2*time.Second); !ok {
		t.Error("chunkreply.Query after Detach did not return within 2s (Detach left a connection attached)")
	} else if !errors.Is(err, chunkreply.ErrNoConnection) {
		t.Errorf("chunkreply.Query after Detach: err = %v, want ErrNoConnection", err)
	}
	if err, ok := runQuery(func() error {
		_, err := readGrants.Query(context.Background(), wire.ReadQuery{JobID: "j", QueryID: "post"})
		return err
	}, 2*time.Second); !ok {
		t.Error("readreply.Query after Detach did not return within 2s (Detach left a connection attached)")
	} else if !errors.Is(err, readreply.ErrNoConnection) {
		t.Errorf("readreply.Query after Detach: err = %v, want ErrNoConnection", err)
	}
}

func TestOnlyAnInstallerFileNameWithNoArgumentsIsInstallerMode(t *testing.T) {
	const code = "e2eLINKcode_0123456789-ABCDEFGHI"
	dl := `C:\Users\ana\Downloads\ITMS-Agent-Setup_itms.toeshee.com_` + code + ` (1).exe`
	for _, tc := range []struct {
		args []string
		exe  string
		want bool
	}{
		{nil, dl, true},
		{[]string{"--elevated-install"}, dl, true},
		// The installed service: no arguments, its own name. The service path
		// never reaches internal/install.
		{nil, `C:\Program Files\ITSuite\its-agent.exe`, false},
		{nil, "/usr/local/bin/its-agent", false},
		// THE CODE IS NEVER AN ARGUMENT: anything else on the command line is
		// not installer mode, whatever the file is called.
		{[]string{code}, dl, false},
		{[]string{"--elevated-install", code}, dl, false},
		{[]string{"--version"}, dl, false},
	} {
		if got := installerRequested(tc.args, tc.exe); got != tc.want {
			t.Errorf("installerRequested(%v, %q) = %v, want %v", tc.args, tc.exe, got, tc.want)
		}
	}
}

// The Linux and macOS install mode the SSH push runs against the binary it
// placed: `its-agent install --token-file ... --enroll-url ...`. Decided here
// from a constant, like installerRequested, so the service -- started with no
// arguments -- never calls into internal/install.
func TestPushInstallRequested(t *testing.T) {
	for _, tc := range []struct {
		args []string
		want bool
	}{
		{[]string{"install", "--token-file", "/opt/toeshee/enroll.token", "--enroll-url", "https://x"}, true},
		{[]string{"install"}, true},
		{nil, false},
		{[]string{}, false},
		{[]string{"--version"}, false},
		{[]string{"installer"}, false},
		{[]string{"--elevated-install"}, false},
		{[]string{"--token-file", "x", "install"}, false},
	} {
		if got := pushInstallRequested(tc.args); got != tc.want {
			t.Errorf("pushInstallRequested(%v) = %v, want %v", tc.args, got, tc.want)
		}
	}
}

// The MSI's two custom actions pass exactly one argument each, and nothing
// else is either mode: the service -- no arguments -- never reaches them.
func TestMsiModesAreExactlyTheirOneArgument(t *testing.T) {
	for _, tc := range []struct {
		args               []string
		install, uninstall bool
	}{
		{[]string{"--msi-install"}, true, false},
		{[]string{"--msi-uninstall"}, false, true},
		{nil, false, false},
		{[]string{}, false, false},
		{[]string{"--msi-install", "x"}, false, false},
		{[]string{"--msi-uninstall", "--msi-install"}, false, false},
		{[]string{"install"}, false, false},
		{[]string{"--elevated-install"}, false, false},
		{[]string{"--MSI-INSTALL"}, false, false},
	} {
		if got := msiInstallRequested(tc.args); got != tc.install {
			t.Errorf("msiInstallRequested(%v) = %v, want %v", tc.args, got, tc.install)
		}
		if got := msiUninstallRequested(tc.args); got != tc.uninstall {
			t.Errorf("msiUninstallRequested(%v) = %v, want %v", tc.args, got, tc.uninstall)
		}
	}
}
