package jobs

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func newStore(t *testing.T, log *slog.Logger) (*NonceFile, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "nonces.json")
	if log == nil {
		log = slog.New(slog.NewTextHandler(&logBuf{}, nil))
	}
	return OpenNonceStore(path, log), path
}

func TestASecondSightingOfANonceIsReportedSeen(t *testing.T) {
	n, _ := newStore(t, nil)
	exp := baseNow.Add(time.Hour)

	if n.SeenOrRecord("a", exp) {
		t.Fatal("a nonce this store has never seen was reported seen")
	}
	if !n.SeenOrRecord("a", exp) {
		t.Fatal("a recorded nonce was not reported seen: every job could be replayed once more")
	}
	if n.SeenOrRecord("b", exp) {
		t.Fatal("recording 'a' made 'b' look seen")
	}
}

func TestTheReplayStoreSurvivesAProcessRestart(t *testing.T) {
	// The falsifying observation is a nonce that is forgotten across a restart:
	// an agent that reboots between two deliveries of the same job would run it
	// twice, and rebooting is something an attacker with local access can cause.
	path := filepath.Join(t.TempDir(), "nonces.json")
	log := slog.New(slog.NewTextHandler(&logBuf{}, nil))
	exp := baseNow.Add(time.Hour)

	first := OpenNonceStore(path, log)
	if first.SeenOrRecord("survivor", exp) {
		t.Fatal("first sighting reported seen")
	}

	second := OpenNonceStore(path, log)
	if !second.SeenOrRecord("survivor", exp) {
		t.Fatal("the nonce was forgotten across a restart; replay protection is per-process only")
	}
}

func TestPruneRemovesExpiredEntriesAndOnlyThose(t *testing.T) {
	// The store is bounded by the fact that freshness already refuses anything
	// past expires_at: an expired nonce cannot be usefully replayed, so it can
	// be dropped. Anything still in its window must NOT be.
	n, _ := newStore(t, nil)
	n.SeenOrRecord("gone-1", baseNow.Add(-time.Hour))
	n.SeenOrRecord("gone-2", baseNow.Add(-time.Second))
	n.SeenOrRecord("kept-1", baseNow.Add(time.Second))
	n.SeenOrRecord("kept-2", baseNow.Add(time.Hour))

	if removed := n.Prune(baseNow); removed != 2 {
		t.Fatalf("Prune removed %d, want 2", removed)
	}
	if n.Len() != 2 {
		t.Fatalf("store holds %d entries after Prune, want 2", n.Len())
	}
	if n.SeenOrRecord("kept-1", baseNow.Add(time.Second)) == false {
		t.Fatal("Prune dropped a nonce that was still inside its freshness window")
	}
	// And the pruning is durable, not just in memory.
	reopened := OpenNonceStore(n.path, slog.New(slog.NewTextHandler(&logBuf{}, nil)))
	if reopened.Len() != 2 {
		t.Fatalf("reopened store holds %d entries, want 2", reopened.Len())
	}
}

func TestAtTheCapTheStoreEvictsTheSoonestExpiringEntry(t *testing.T) {
	n, _ := newStore(t, nil)
	n.cap = 4
	for i, d := range []time.Duration{time.Minute, 2 * time.Minute, 3 * time.Minute, 4 * time.Minute} {
		if n.SeenOrRecord(string(rune('a'+i)), baseNow.Add(d)) {
			t.Fatalf("filling the store reported entry %d seen", i)
		}
	}
	// A newcomer that outlives the soonest-expiring entry displaces it.
	if n.SeenOrRecord("z", baseNow.Add(10*time.Minute)) {
		t.Fatal("a recordable nonce was refused")
	}
	if n.Len() != 4 {
		t.Fatalf("store holds %d entries, cap is 4", n.Len())
	}
	if !n.SeenOrRecord("z", baseNow.Add(10*time.Minute)) {
		t.Fatal("the newly recorded nonce was not remembered")
	}
	// "a" -- the soonest-expiring -- is the one that went. Losing it is safe:
	// it expires first, so freshness refuses its job before a replay could use
	// the gap.
	if n.SeenOrRecord("a", baseNow.Add(time.Minute)) {
		t.Log("evicted entry 'a' is forgotten, as designed")
	} else {
		t.Fatal("'a' was not the entry evicted")
	}
}

func TestAFullStoreThatCannotMakeRoomFailsClosed(t *testing.T) {
	// FALSIFYING OBSERVATION: the spy's invocation counter.
	//
	// The store is full of nonces that all outlive the incoming job, so nothing
	// can be evicted without forgetting something MORE replayable than the
	// newcomer. The only safe answer is "treat it as seen" -- refuse. If that
	// branch returned false instead (record nothing, let it through), the same
	// job delivered twice would RUN twice, and an attacker able to flood the
	// store past its cap would have switched replay protection off for the
	// whole fleet. Asserting on the return value alone would not catch that:
	// assert on whether the capability ran.
	s := newSigner(t)
	h := newHarness(t, s)
	store, _ := newStore(t, nil)
	store.cap = 2
	store.SeenOrRecord("filler-1", baseNow.Add(24*time.Hour))
	store.SeenOrRecord("filler-2", baseNow.Add(24*time.Hour))
	h.runner.verifier.nonces = store

	frame := s.frame(t, validJob("n-cap"))
	h.runner.Handle(context.Background(), frame)
	h.runner.Handle(context.Background(), frame)

	// Anchored on BOTH deliveries' own terminal frames before reading the
	// spy's counter, not on a synchronous sample right after the two Handle
	// calls return: since Task 6 wired jobs.Dispatcher into Handle, a
	// delivery the fail-closed branch failed to refuse would dispatch to a
	// worker goroutine rather than run inline, so an immediate read of
	// spy.count() could catch it before either async run (or before both)
	// had completed -- a false PASS on exactly the property this test exists
	// to falsify (see nonce.go's "Why it is bounded" section). Waiting for
	// two job_result frames total means both deliveries have reached a
	// terminal state -- refused synchronously if the fail-closed branch
	// works, or run and reported if it doesn't -- before anything else here
	// is read.
	h.sender.awaitKindCount(t, "job_result", 2, 2*time.Second)

	if n := h.spy.count(); n != 0 {
		t.Fatalf("capability ran %d time(s) against a replay store that could not record; "+
			"a nonce flood past the cap disables replay protection", n)
	}
	for _, f := range h.sender.frames() {
		if f["kind"] == "job_result" {
			if got := reasonOf(t, f); !strings.HasPrefix(got, string(ReasonReplayedNonce)) {
				t.Fatalf("reason %q, want %q", got, ReasonReplayedNonce)
			}
		}
	}
}

func TestACorruptStoreStartsEmptyAndSaysSoLoudly(t *testing.T) {
	// A corrupt store that failed OPEN would silently disable replay
	// protection: every job's nonce would look new. It starts empty instead,
	// which is the same security posture as a fresh agent -- and the WARN is
	// what makes "we lost the history" visible rather than invisible.
	path := filepath.Join(t.TempDir(), "nonces.json")
	if err := os.WriteFile(path, []byte(`{"nonces": {"a": "not-a-t`), 0o600); err != nil {
		t.Fatal(err)
	}
	logs := &logBuf{}
	n := OpenNonceStore(path, slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug})))

	if n.Len() != 0 {
		t.Fatalf("corrupt store loaded %d entries", n.Len())
	}
	if !strings.Contains(logs.String(), "replay history") {
		t.Fatalf("the lost replay history left no trace in the log:\n%s", logs.String())
	}
	// It is usable, not wedged: an agent must not boot-loop on a bad file.
	if n.SeenOrRecord("fresh", baseNow.Add(time.Hour)) {
		t.Fatal("a fresh nonce was refused by a recovered store")
	}
}

func TestTheStoreIsWrittenAtomicallyAndPrivately(t *testing.T) {
	n, path := newStore(t, nil)
	n.SeenOrRecord("a", baseNow.Add(time.Hour))

	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS == "windows" {
		// os.Chmod(0o600) on NTFS only toggles the read-only attribute, so
		// Mode().Perm() cannot observe an owner-only POSIX mode on this OS.
		// The rename-based-write check below is still meaningful here and
		// runs unconditionally.
		t.Log("skipping the 0600 assertion on windows: POSIX permission bits are not observable via os.Stat")
	} else if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf("nonce store is mode %o, want 0600", perm)
	}
	// No temp file left behind: a rename-based write leaves exactly one file.
	entries, err := os.ReadDir(filepath.Dir(path))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		var names []string
		for _, e := range entries {
			names = append(names, e.Name())
		}
		t.Fatalf("data dir holds %v; an atomic write leaves only the store", names)
	}
}

func TestAReplayIsRefusedAcrossARestartOfTheWholeRunner(t *testing.T) {
	// The end-to-end version of the restart test: two Runners over one data
	// directory, and the falsifying observation is the capability counter.
	s := newSigner(t)
	dir := t.TempDir()
	path := filepath.Join(dir, "nonces.json")
	log := slog.New(slog.NewTextHandler(&logBuf{}, nil))
	spy := newSpy(t)
	frame := s.frame(t, validJob("n-restart"))

	// build returns its own fakeSender too: each call gets a fresh Runner (a
	// fresh Dispatcher included), so there is no single sender shared across
	// both deliveries to anchor a wait on unless the caller keeps one.
	build := func() (*Runner, *fakeSender) {
		sender := &fakeSender{}
		r, err := New(Options{
			BaseContext: t.Context(),
			DeviceID:    testDeviceID,
			SigningKey:  s.pub,
			Nonces:      OpenNonceStore(path, log),
			Registry:    mapRegistry{"hello": spy.handle},
			Sender:      sender,
			Now:         func() time.Time { return baseNow },
			Logger:      log,
		})
		if err != nil {
			t.Fatal(err)
		}
		return r, sender
	}

	// Each Handle call is anchored on ITS OWN terminal frame before the next
	// one starts, rather than firing both and sampling spy.count() once at
	// the end: since Task 6 a genuine delivery dispatches to a worker
	// goroutine rather than running inline, so an immediate read after both
	// Handle calls return depends on how far each Dispatcher worker happened
	// to get -- and if the persisted-nonce check were broken and let the
	// SECOND delivery dispatch too, a sample taken the instant the counter
	// first reached 1 could miss a second run about to make it 2. Waiting for
	// each delivery's own job_result closes that gap for both the flakiness
	// risk (the correct case) and the false-pass risk (the broken case).
	r1, sender1 := build()
	r1.Handle(context.Background(), frame)
	sender1.only(t, "job_result")

	r2, sender2 := build()
	r2.Handle(context.Background(), frame)
	sender2.only(t, "job_result")

	if n := spy.count(); n != 1 {
		t.Fatalf("capability ran %d times across a restart; want 1", n)
	}
}
