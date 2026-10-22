package logs

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func tempStore(t *testing.T) (fileCursorStore, string) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, cursorFile)
	return fileCursorStore{path: path}, path
}

func TestAnAgentThatHasNeverShippedHasAnEmptyCursorAndNotAnError(t *testing.T) {
	// "Never shipped" is an ordinary state -- every endpoint is in it once --
	// and reporting it as an error would make the very first `logs.ship` on
	// every device in the fleet fail.
	//
	// FALSIFYING OBSERVATION: an error, or a non-empty position.
	store, _ := tempStore(t)
	c, err := store.Load()
	if err != nil {
		t.Fatalf("Load with no file: %v", err)
	}
	if c.Position != "" || c.Source != "" {
		t.Fatalf("a fresh agent's cursor is %+v", c)
	}
}

func TestTheCursorRoundTripsAndIsOwnerReadableOnly(t *testing.T) {
	// 0600, inside the 0700 data directory. The cursor is not a secret -- it is
	// a record id -- but it is a statement about what this agent has ALREADY
	// SHIPPED. An unprivileged local process that could rewrite it could make
	// the agent skip an arbitrary range of the Security log: a gap in the
	// evidence trail, chosen by whoever wrote the file, invisible from the
	// console.
	//
	// FALSIFYING OBSERVATION: a mode with any group or other bit set.
	store, path := tempStore(t)
	want := Cursor{V: cursorVersion, Source: "windows-eventlog", Position: "184392",
		UpdatedAt: "2026-08-06T12:00:00Z"}
	if err := store.Save(want); err != nil {
		t.Fatalf("Save: %v", err)
	}
	got, err := store.Load()
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got != want {
		t.Fatalf("round trip: got %+v, want %+v", got, want)
	}
	if runtime.GOOS == "windows" {
		// os.Chmod(0o600) on NTFS only toggles the read-only attribute, so
		// Stat().Mode().Perm() cannot observe an owner-only POSIX mode here.
		// The property this test is defending (no other local principal can
		// read or rewrite the cursor) is a Windows ACL question, unasked and
		// unanswered on this path today -- not proven by a Perm() check that
		// this OS cannot make meaningful.
		t.Skip("POSIX permission bits are not observable via os.Stat on windows: linux/macOS only")
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf("the cursor file is mode %o; anything readable or writable beyond the owner "+
			"lets a local process choose a gap in this device's evidence trail", perm)
	}
}

func TestNoTemporaryFileSurvivesASave(t *testing.T) {
	// The save is a temp file plus a rename, because a truncated JSON document
	// after a power cut mid-write is read as corrupt and restarts collection
	// from the beginning of the retained log. What must not happen is a temp
	// file per job accumulating in the data directory forever.
	//
	// FALSIFYING OBSERVATION: more than one file in the directory after ten
	// saves.
	store, path := tempStore(t)
	for i := 0; i < 10; i++ {
		if err := store.Save(Cursor{Source: "windows-eventlog", Position: "1"}); err != nil {
			t.Fatal(err)
		}
	}
	entries, err := os.ReadDir(filepath.Dir(path))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		var names []string
		for _, e := range entries {
			names = append(names, e.Name())
		}
		t.Fatalf("ten saves left %d files: %v", len(entries), names)
	}
}

func TestACursorFromAnUnrecognisedVersionIsRejectedRatherThanMisread(t *testing.T) {
	// A future shape read by an older agent as if it were this one would produce
	// a position that means something else -- which is a silent collection
	// outage, the worst failure this file can have. Rejecting restarts
	// collection instead, and the server dedupes the re-read away.
	//
	// FALSIFYING OBSERVATION: v:2 loading successfully.
	store, path := tempStore(t)
	raw, _ := json.Marshal(map[string]any{"v": 2, "source": "windows-eventlog", "position": "1"})
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Load(); err == nil {
		t.Fatal("a v:2 cursor was accepted by a v:1 agent")
	} else if !strings.Contains(err.Error(), "version") {
		t.Errorf("the error %q does not say what was wrong", err)
	}
}

func TestACorruptCursorIsAnErrorAndNotASilentZero(t *testing.T) {
	// ship() answers a Load error by restarting collection AND reporting
	// `cursor_reset` in the job result, so an operator can see why a device
	// suddenly re-shipped its history. A Load that swallowed the corruption and
	// returned a zero Cursor would restart collection with no result field
	// saying so.
	//
	// FALSIFYING OBSERVATION: a nil error from a truncated document.
	store, path := tempStore(t)
	if err := os.WriteFile(path, []byte(`{"v":1,"source":"windows-eve`), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Load(); err == nil {
		t.Fatal("a truncated cursor loaded without error; the reset would be invisible")
	}
}

func TestTheCursorIsTheOnlyFileThisCapabilityWritesAndItIsSmall(t *testing.T) {
	// It is the whole of this capability's footprint at rest. A cursor that grew
	// -- a list of shipped uids, say, which is the obvious way to implement
	// dedupe on the agent instead of relying on the server's -- would be an
	// unbounded state file on every endpoint.
	//
	// FALSIFYING OBSERVATION: a file above 1 KiB after a realistic save.
	store, path := tempStore(t)
	if err := store.Save(Cursor{Source: "macos-unified-log",
		Position: "2026-07-18 09:12:03.123456+0100", UpdatedAt: "2026-08-06T12:00:00Z"}); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Size() > 1024 {
		t.Fatalf("the cursor file is %d bytes; it holds a position and nothing else", info.Size())
	}
	t.Logf("cursor file: %d bytes", info.Size())
}

func TestSavingIsWhatAdvancesTheCursorAndItRecordsWhichSourceWroteIt(t *testing.T) {
	// The source name is what makes a foreign cursor detectable rather than
	// misread. A save that omitted it would leave a position with no way to tell
	// a Windows record id from a macOS timestamp.
	//
	// FALSIFYING OBSERVATION: a saved cursor with an empty source or version.
	store := &memCursor{}
	cur := Cursor{}
	saveCursor(Options{Now: fixedNow, Logger: quiet()}, store, &fakeLog{}, &cur, "184392")
	if store.cur.Source != "fake-eventlog" || store.cur.V != cursorVersion {
		t.Fatalf("saved %+v", store.cur)
	}
	if store.cur.Position != "184392" || cur.Position != "184392" {
		t.Fatalf("the in-memory cursor is %q and the stored one is %q", cur.Position, store.cur.Position)
	}
	// An empty pending position must not clobber a real one: it means "nothing
	// was consumed", and writing it would rewind the agent to the start of the
	// log on every quiet job.
	saveCursor(Options{Now: fixedNow, Logger: quiet()}, store, &fakeLog{}, &cur, "")
	if store.cur.Position != "184392" {
		t.Fatalf("an empty position overwrote the cursor with %q", store.cur.Position)
	}
}

func TestACursorThatCannotBePersistedDoesNotFailTheJob(t *testing.T) {
	// The events are already on their way when the cursor is written. Failing
	// the job would report a shipment that HAPPENED as one that did not, and the
	// control plane would show a device failing to ship while its events piled
	// up in the security_events table. The cost of a lost cursor is a re-read
	// the server dedupes away.
	//
	// FALSIFYING OBSERVATION: an error out of ship() when only Save failed.
	log := newFakeLog(600)
	store := &memCursor{saveEr: errWriteOnly{}}
	sender := &countingSender{}
	res, err := New(Options{}).ship(context.Background(),
		Options{Sender: sender, Now: fixedNow, Logger: quiet()}, log, store)
	if err != nil {
		t.Fatalf("ship failed because the cursor could not be written: %v", err)
	}
	if res.Shipped == 0 {
		t.Fatal("nothing was shipped")
	}
}

type errWriteOnly struct{}

func (errWriteOnly) Error() string { return "read-only file system" }
