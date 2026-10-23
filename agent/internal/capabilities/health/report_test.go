package health

import (
	"encoding/json"
	"reflect"
	"sort"
	"strings"
	"testing"
	"unsafe"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// ---------------------------------------------------------------------------
// §5.1's message shape
// ---------------------------------------------------------------------------

func TestTheEncodedMessageCarriesExactlyVKindPolicyVersionAndReports(t *testing.T) {
	// §5.1: "Identity comes from the certificate-bound connection, NEVER from
	// the message." A `device` field here would be a second, forgeable source of
	// the same fact. `_Msg` is extra="forbid", so ANY extra key makes
	// parse_agent_message drop the whole message with only a logger.warning
	// behind it (gateway/main.py:200-202).
	//
	// FALSIFIER: the encoded key set differing from {v, kind, policy_version,
	// reports} -- printed.
	frame, err := wire.Encode(wire.HealthReport{
		PolicyVersion: 7,
		Reports: []wire.HealthReportLine{{
			CheckID: "c", Selector: "C:", Kind: "breach", State: "breaching",
			Value: "3.1", ObservedAt: "2026-08-05T12:00:00Z",
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(frame, &got); err != nil {
		t.Fatal(err)
	}
	keys := make([]string, 0, len(got))
	for k := range got {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	want := []string{"kind", "policy_version", "reports", "v"}
	if strings.Join(keys, ",") != strings.Join(want, ",") {
		t.Fatalf("encoded keys = %v, want %v", keys, want)
	}
	if got["kind"] != "health_report" {
		t.Fatalf("kind = %v", got["kind"])
	}
}

func TestPolicyVersionZeroIsEncodedRatherThanOmitted(t *testing.T) {
	// `policy_version` is required with minimum 0, and 0 is the legitimate value
	// on a freshly enrolled or a DISARMED agent. An omitempty here would make
	// ingest.py read "no version at all" -- and ingest.py:552 discards a report
	// whose version lags the acknowledged one by two, so the breaches of every
	// disarmed-then-rearmed device would vanish silently.
	frame, err := wire.Encode(wire.HealthReport{
		PolicyVersion: 0,
		Reports:       []wire.HealthReportLine{{CheckID: "c", Kind: "summary", State: "ok", ObservedAt: "t"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(frame), `"policy_version":0`) {
		t.Fatalf("frame = %s; policy_version 0 must be on the wire", frame)
	}
}

// ---------------------------------------------------------------------------
// The 1..64 bound
// ---------------------------------------------------------------------------

func TestChunkNeverEmitsAnEmptyBatchAndSplitsAtSixtyFour(t *testing.T) {
	// `reports` is min_length=1, max_length=64 (§5.1). Both bounds are the same
	// failure when broken: parse_agent_message drops the WHOLE message.
	//
	// Asserted on the chunker rather than through a policy because a policy that
	// could produce 65 lines cannot exist -- §0.11 caps a profile at 32 checks,
	// so a summary tick emits at most 32 summaries plus 32 edges. That is the
	// plan's "40-check policy" test being impossible; see report.go's note.
	if got := chunk(nil, maxReportsPerMessage); got != nil {
		t.Fatalf("chunk(nil) = %v; an empty batch is a message the server rejects whole", got)
	}
	if got := chunk([]wire.HealthReportLine{}, maxReportsPerMessage); got != nil {
		t.Fatalf("chunk(empty) = %v", got)
	}
	for _, n := range []int{1, 63, 64, 65, 128, 129} {
		lines := make([]wire.HealthReportLine, n)
		for i := range lines {
			lines[i].CheckID = "c"
		}
		batches := chunk(lines, maxReportsPerMessage)
		total := 0
		for _, b := range batches {
			if len(b) == 0 {
				t.Fatalf("n=%d produced an empty batch", n)
			}
			if len(b) > maxReportsPerMessage {
				t.Fatalf("n=%d produced a batch of %d, above the server's %d", n, len(b), maxReportsPerMessage)
			}
			total += len(b)
		}
		if total != n {
			t.Fatalf("n=%d: chunking lost %d lines", n, n-total)
		}
		wantBatches := (n + maxReportsPerMessage - 1) / maxReportsPerMessage
		if len(batches) != wantBatches {
			t.Fatalf("n=%d produced %d messages, want %d", n, len(batches), wantBatches)
		}
	}
}

func TestAFullSummaryPlusEveryPossibleEdgeStillFitsInOneMessage(t *testing.T) {
	// The arithmetic §5.1 states: 32 checks means at most 32 summary lines plus
	// at most 32 edges, which is exactly the 64 bound. This is what makes the
	// chunker unreachable under the caps -- and a test, because that arithmetic
	// is load-bearing and lives only in a sentence.
	r := newRig(t)
	checks := make([]checkJSON, 0, maxChecks)
	for i := 0; i < maxChecks; i++ {
		checks = append(checks, numericCheck(string(rune('a'+i)), "cpu.busy_pct", "gt", "90", 1))
	}
	r.apply(t, summaryEveryTick(1, checks...))
	r.tick.fallback = num(99) // every check breaches on this tick

	r.sched.tick(t)
	if n := r.sender.sends.Load(); n != 1 {
		t.Fatalf("%d messages for 32 breaches plus a 32-line summary, want 1", n)
	}
	if got := len(r.sender.messages[0].Reports); got != 64 {
		t.Fatalf("%d lines, want 64 (32 edges + 32 summaries)", got)
	}
}

// ---------------------------------------------------------------------------
// §0.5's twelve bytes, and §0.12's boundary
// ---------------------------------------------------------------------------

func TestTheCheckStateStructIsExactlyTheThreeFieldsSection05Names(t *testing.T) {
	// §0.5: "Per check, the agent holds a fixed-size struct -- consecutive
	// int32, state uint8, last float32 -- and NO SAMPLE HISTORY AT ALL."
	//
	// FALSIFYING OBSERVATION: a struct that grew a []float64. §0.12's boundary
	// is that a moving average is a time series, and a time series on the
	// endpoint is the first step to becoming the monitoring product -- so the
	// place to stop it is the struct, before anyone needs a window to put it in.
	ty := reflect.TypeOf(checkState{})
	if ty.NumField() != 3 {
		var names []string
		for i := 0; i < ty.NumField(); i++ {
			names = append(names, ty.Field(i).Name+" "+ty.Field(i).Type.String())
		}
		t.Fatalf("checkState has %d fields (%v); §0.5 names exactly three", ty.NumField(), names)
	}
	want := [][2]string{{"consecutive", "int32"}, {"state", "uint8"}, {"last", "float32"}}
	for i, w := range want {
		f := ty.Field(i)
		if f.Name != w[0] || f.Type.String() != w[1] {
			t.Fatalf("field %d is %s %s, want %s %s", i, f.Name, f.Type, w[0], w[1])
		}
	}
	if got := unsafe.Sizeof(checkState{}); got != 12 {
		t.Fatalf("unsafe.Sizeof(checkState{}) = %d, want 12", got)
	}
	// And nothing in it can hold a series: no slice, map, pointer or interface.
	for i := 0; i < ty.NumField(); i++ {
		switch ty.Field(i).Type.Kind() {
		case reflect.Slice, reflect.Map, reflect.Ptr, reflect.Interface, reflect.Chan:
			t.Fatalf("checkState.%s is a %s; §0.12: no sample history on the endpoint",
				ty.Field(i).Name, ty.Field(i).Type.Kind())
		}
	}
}

func TestAReportLineCarriesNoSeriesShapedField(t *testing.T) {
	// §5.1's wire rule: "If a future field would let a consumer reconstruct a
	// time series, it does not belong here." Six scalars, and the assertion is
	// on the encoded key set of a real line.
	b, err := json.Marshal(wire.HealthReportLine{
		CheckID: "c", Selector: "s", Kind: "summary", State: "ok",
		Value: "1.0", ObservedAt: "2026-08-05T12:00:00Z",
	})
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatal(err)
	}
	keys := make([]string, 0, len(got))
	for k, v := range got {
		keys = append(keys, k)
		if _, isArray := v.([]any); isArray {
			t.Fatalf("report line member %q is an array; a series on this wire is a series a "+
				"consumer can retain (§0.12)", k)
		}
	}
	sort.Strings(keys)
	want := "check_id,kind,observed_at,selector,state,value"
	if strings.Join(keys, ",") != want {
		t.Fatalf("line keys = %v, want %s", keys, want)
	}
}

func TestEveryEmittedStateAndKindIsOneTheServerAccepts(t *testing.T) {
	// `state` is Literal["ok","breaching","unavailable"] and `kind` is
	// Literal["breach","recovery","summary"] (§5.1). An unrecognised value is
	// not a lenient field: the message is dropped whole.
	states := map[string]bool{"ok": true, "breaching": true, "unavailable": true}
	for _, s := range []uint8{stateOK, stateBreaching, stateUnavailable, stateUnavailableWasBreaching} {
		if !states[reportedState(s)] {
			t.Fatalf("internal state %d reports as %q, which is not one of the three", s, reportedState(s))
		}
	}
	if reportedState(stateUnavailableWasBreaching) != "unavailable" {
		t.Fatal("the remembered-breach state must be indistinguishable from plain unavailable on the wire")
	}

	r := newRig(t)
	r.apply(t, summaryEveryTick(1,
		numericCheck("a", "cpu.busy_pct", "gt", "90", 1),
		numericCheck("b", "memory.used_pct", "gt", "90", 1),
	))
	r.tick.answers["cpu.busy_pct"] = num(99)
	r.tick.answers["memory.used_pct"] = unavailable()
	r.sched.tick(t)
	r.tick.answers["cpu.busy_pct"] = num(1)
	r.sched.tick(t)

	kindsOK := map[string]bool{"breach": true, "recovery": true, "summary": true}
	lines := r.sender.lines()
	if len(lines) == 0 {
		t.Fatal("no lines; this test would be vacuous")
	}
	for _, l := range lines {
		if !kindsOK[l.Kind] || !states[l.State] {
			t.Fatalf("line %+v carries a kind/state the server's Literal does not accept", l)
		}
	}
}
