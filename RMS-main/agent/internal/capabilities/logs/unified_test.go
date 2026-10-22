package logs

import (
	"bufio"
	"bytes"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The macOS unified-log normalisation, against testdata/unified-sample.ndjson.
//
// THOSE LINES ARE HAND-WRITTEN, NOT CAPTURED. No Mac was available to this
// build. The NDJSON key names are the documented `log show --style ndjson`
// members and the message strings are the conventional wordings (OpenSSH's are
// verbatim and stable; Apple's own are the ones that drift between releases).
// testdata/README.md says so, and docs/modules/security-logs/spec.md §10.4
// records message-pattern matching as the weakest link in this collector.

func unifiedLines(t *testing.T) [][]byte {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "unified-sample.ndjson"))
	if err != nil {
		t.Fatal(err)
	}
	var out [][]byte
	sc := bufio.NewScanner(bytes.NewReader(raw))
	for sc.Scan() {
		if line := strings.TrimSpace(sc.Text()); line != "" {
			out = append(out, []byte(line))
		}
	}
	return out
}

func TestTheUnifiedLogSampleNormalisesToTheExpectedFiveAnswers(t *testing.T) {
	// FALSIFYING OBSERVATION: a wrong event_type, a wrong username, or the
	// mDNSResponder line being accepted.
	lines := unifiedLines(t)
	if len(lines) != 5 {
		t.Fatalf("the sample has %d lines; this test names five", len(lines))
	}
	type want struct {
		ok        bool
		eventType string
		outcome   string
		user      string
	}
	wants := []want{
		{true, "logon_success", "success", "jsmith"},
		{true, "logon_failure", "failure", "administrator"},
		{true, "privilege_elevation", "success", "jsmith"},
		{true, "logon_failure", "failure", "jsmith"},
		{false, "", "", ""}, // mDNSResponder: not an authentication record
	}
	for i, line := range lines {
		ev, ok, err := parseUnifiedLine(line, "")
		if err != nil {
			t.Errorf("line %d: %v", i, err)
			continue
		}
		w := wants[i]
		if ok != w.ok {
			t.Errorf("line %d: ok = %v, want %v (message %q)", i, ok, w.ok, ev.Raw["message"])
			continue
		}
		if !ok {
			continue
		}
		if ev.EventType != w.eventType || ev.Outcome != w.outcome {
			t.Errorf("line %d: event_type=%q outcome=%q, want %q/%q",
				i, ev.EventType, ev.Outcome, w.eventType, w.outcome)
		}
		if ev.Username != w.user {
			t.Errorf("line %d: username = %q, want %q", i, ev.Username, w.user)
		}
		// Local time in, UTC out: an evidence table where half the rows are
		// local time cannot be sorted, and this fixture is +0100.
		if !strings.HasSuffix(ev.TS, "Z") {
			t.Errorf("line %d: ts = %q; every endpoint must send UTC", i, ev.TS)
		}
	}
}

func TestALocalTimestampWithAnOffsetIsConvertedAndNotCarried(t *testing.T) {
	// FALSIFYING OBSERVATION: a wrong VALUE. 09:12:03+0100 is 08:12:03Z, and an
	// agent that carried the local reading would place every event an hour late
	// in the audit period -- across a DST boundary, inconsistently.
	for _, tc := range []struct{ in, want string }{
		{"2026-07-18 09:12:03.123456+0100", "2026-07-18T08:12:03.123456Z"},
		{"2026-01-18 09:12:03.000000+0000", "2026-01-18T09:12:03.000000Z"},
		{"2026-07-18 09:12:03.500000-0700", "2026-07-18T16:12:03.500000Z"},
	} {
		got, err := normalizeUnifiedTime(tc.in)
		if err != nil {
			t.Errorf("normalizeUnifiedTime(%q): %v", tc.in, err)
			continue
		}
		if got != tc.want {
			t.Errorf("normalizeUnifiedTime(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
	if _, err := normalizeUnifiedTime("18/07/2026 09:12"); err == nil {
		t.Error("an unparseable timestamp was accepted")
	}
}

func TestTheUnifiedEventUIDIsStableAcrossResendsAndDistinctBetweenRecords(t *testing.T) {
	// `log show --start` is INCLUSIVE, so the record at the cursor boundary is
	// re-read on the next job BY CONSTRUCTION. A random uid would insert it
	// again every time.
	//
	// FALSIFYING OBSERVATION: two reads of one line producing different uids, or
	// two different lines producing the same one.
	lines := unifiedLines(t)
	seen := map[string]int{}
	for i, line := range lines {
		a, ok, _ := parseUnifiedLine(line, "")
		if !ok {
			continue
		}
		b, _, _ := parseUnifiedLine(line, "")
		if a.EventUID != b.EventUID {
			t.Fatalf("line %d produced two uids, %q and %q; every resend would be a duplicate "+
				"row in the SOC 2 access-evidence export", i, a.EventUID, b.EventUID)
		}
		if prev, dup := seen[a.EventUID]; dup {
			t.Fatalf("lines %d and %d share uid %q; the server would silently discard the "+
				"second event", prev, i, a.EventUID)
		}
		seen[a.EventUID] = i
		if len(a.EventUID) > maxEventUIDLen {
			t.Errorf("line %d: uid is %d chars (limit %d)", i, len(a.EventUID), maxEventUIDLen)
		}
	}
	if len(seen) < 4 {
		t.Fatalf("only %d records were accepted; this test would be nearly vacuous", len(seen))
	}
}

func TestAnUnrecognisedMessageIsDroppedRatherThanGuessedAt(t *testing.T) {
	// The mapping is pattern-based and Apple rewords these strings between
	// releases. A reworded message must stop matching -- an evidence gap, and
	// visible as one -- rather than be classified by a looser rule into
	// something wrong. A wrong row in a SOC 2 evidence table is worse than a
	// missing one, because it is not visibly missing.
	//
	// FALSIFYING OBSERVATION: ok=true for a message from a collected process
	// that says nothing about authentication.
	line := []byte(`{"timestamp":"2026-07-18 09:12:03.000000+0000","process":"sshd",` +
		`"processID":1,"threadID":2,"eventMessage":"Server listening on 0.0.0.0 port 22."}`)
	ev, ok, err := parseUnifiedLine(line, "")
	if err != nil {
		t.Fatalf("an ordinary sshd line produced an error: %v", err)
	}
	if ok {
		t.Fatalf("an sshd startup notice was classified as %q", ev.EventType)
	}
}

func TestTheUnifiedReaderStreamsAndSkipsTheHeaderLines(t *testing.T) {
	// `log show` prints a header and occasional notices before the NDJSON. A
	// reader that treated them as errors would fail every job on a real Mac.
	//
	// FALSIFYING OBSERVATION: an error from a header line, or a record whose
	// position is not its own timestamp.
	body := "Filtering the log data using \"process == \\\"sshd\\\"\"\n" +
		"Timestamp                       Thread     Type\n" +
		string(unifiedLines(t)[0]) + "\n" +
		string(unifiedLines(t)[1]) + "\n"
	r := newUnifiedReader(io.NopCloser(strings.NewReader(body)), nil)
	defer r.Close()

	first, err := r.Next()
	if err != nil {
		t.Fatalf("Next: %v", err)
	}
	if first.Position != "2026-07-18 09:12:03.123456+0100" {
		t.Fatalf("position = %q; it must be the record's own timestamp, which is what becomes "+
			"the next --start", first.Position)
	}
	second, err := r.Next()
	if err != nil {
		t.Fatalf("Next: %v", err)
	}
	if second.Position == first.Position {
		t.Fatal("two records share a position; the cursor would not advance")
	}
	if _, err := r.Next(); err != io.EOF {
		t.Fatalf("Next at the end = %v, want io.EOF", err)
	}
}

func TestAStoredPositionIsValidatedBeforeItBecomesAProcessArgument(t *testing.T) {
	// The cursor becomes `--start <value>` on an argv. There is no shell and
	// therefore nothing to inject -- but an unparseable value would make `log
	// show` fail the whole job, forever, and falling back to a bounded window
	// is better than failing.
	//
	// FALSIFYING OBSERVATION: garbage surviving into the argument.
	if got := unifiedStart("2026-07-18 09:12:03.123456+0100"); got == "" {
		t.Error("a valid position was rejected")
	}
	for _, bad := range []string{"", "   ", "yesterday", "`whoami`", "--predicate", "1; rm -rf /"} {
		if got := unifiedStart(bad); got != "" {
			t.Errorf("unifiedStart(%q) = %q; it would become a process argument", bad, got)
		}
	}
}

func TestTheMacOSPredicateIsAnAllowlistOfAuthenticationProcesses(t *testing.T) {
	// The unified log carries everything on the machine. A broad predicate would
	// turn an authentication-evidence feed into general host telemetry --
	// out of scope per docs/00-vision.md -- and would push it through a
	// retention table sized for "thousands of rows/day".
	//
	// FALSIFYING OBSERVATION: a predicate that is not process-scoped, or one
	// naming a process with no authentication role.
	if strings.Contains(unifiedPredicate, "eventMessage CONTAINS") {
		t.Error("the predicate matches on message content; that is a content filter over the " +
			"whole machine's log, not a scope")
	}
	for _, want := range []string{"opendirectoryd", "sshd", "sudo", "loginwindow"} {
		if !strings.Contains(unifiedPredicate, want) {
			t.Errorf("the predicate does not include %q, so those events are never collected", want)
		}
	}
	// Every clause must be a process comparison. A single non-process clause
	// widens collection to everything that matches it.
	for _, clause := range strings.Split(unifiedPredicate, " OR ") {
		if !strings.HasPrefix(strings.TrimSpace(clause), "process == ") {
			t.Errorf("predicate clause %q is not process-scoped", strings.TrimSpace(clause))
		}
	}
	// And every process named must have a rule, or it is collected and dropped
	// -- cost with no evidence.
	for _, clause := range strings.Split(unifiedPredicate, " OR ") {
		name := strings.Trim(strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(clause),
			"process ==")), ` "`)
		found := false
		for _, r := range unifiedRules {
			found = found || r.process == name
		}
		if !found {
			t.Errorf("the predicate collects %q and no rule maps any of its messages; the "+
				"endpoint pays for records it always drops", name)
		}
	}
}
