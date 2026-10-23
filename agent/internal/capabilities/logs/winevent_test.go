package logs

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// The Windows normalisation, against the recorded documents in testdata/.
//
// THOSE DOCUMENTS ARE HAND-WRITTEN, NOT CAPTURED -- testdata/README.md says so
// and says exactly which parts of them a reviewer should and should not trust.
// No Windows endpoint was available to this build. The structure they encode is
// EvtRender's published EventXml shape; the per-event-id field inventory is
// certainly incomplete relative to a real Windows 11 box.

func fixture(t *testing.T, name string) []byte {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatalf("fixture: %v", err)
	}
	return raw
}

func parseFixture(t *testing.T, name string) (wire.SecurityEvent, bool) {
	t.Helper()
	ev, ok, err := parseWinEvent(fixture(t, name), "")
	if err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	return ev, ok
}

func TestASuccessfulNetworkLogonBecomesTheEvidenceRowAnAuditorReads(t *testing.T) {
	// FALSIFYING OBSERVATION: the field values, one at a time. Not "no error".
	ev, ok := parseFixture(t, "4624-network-logon.xml")
	if !ok {
		t.Fatal("4624 was dropped as outside the taxonomy; it is the taxonomy's headline event")
	}
	for _, tc := range []struct{ name, got, want string }{
		{"event_type", ev.EventType, "logon_success"},
		{"outcome", ev.Outcome, "success"},
		{"username", ev.Username, `TOESHEE\jsmith`},
		{"source", ev.Source, "win:4624"},
		// SEVEN fractional digits in, SIX out. `ingest_events` calls
		// datetime.fromisoformat per event with no per-event error handling, and
		// Python raises on seven -- which would fail the WHOLE 500-event message.
		{"ts", ev.TS, "2026-07-18T09:12:03.123456Z"},
		{"event_uid", ev.EventUID, "win:184392:20260718T091203Z"},
	} {
		if tc.got != tc.want {
			t.Errorf("%s = %q, want %q", tc.name, tc.got, tc.want)
		}
	}
	// The investigation fields. A logon evidence row without the source address
	// and the logon type answers "somebody logged in" and nothing else.
	for k, want := range map[string]any{
		"IpAddress": "10.20.7.42",
		"LogonType": "3",
		"event_id":  4624,
		"record_id": "184392",
		"channel":   "Security",
	} {
		if got := ev.Raw[k]; got != want {
			t.Errorf("raw[%q] = %v, want %v", k, got, want)
		}
	}
	// ABSENT fields are OMITTED, not emitted empty: `WorkstationName` is "-" in
	// this record, which is Windows for "not recorded". An empty string there
	// would be indistinguishable from a genuinely blank value.
	if _, present := ev.Raw["WorkstationName"]; present {
		t.Errorf("raw carries WorkstationName = %v for a record whose value is \"-\"",
			ev.Raw["WorkstationName"])
	}
}

func TestAFailedLogonCarriesTheSubStatusThatSaysWhy(t *testing.T) {
	ev, ok := parseFixture(t, "4625-failed-logon.xml")
	if !ok {
		t.Fatal("4625 was dropped")
	}
	if ev.EventType != "logon_failure" || ev.Outcome != "failure" {
		t.Fatalf("event_type=%q outcome=%q", ev.EventType, ev.Outcome)
	}
	if ev.Username != `TOESHEE\administrator` {
		t.Fatalf("username = %q; the account that FAILED is the target, not the (empty) subject",
			ev.Username)
	}
	// 0xc000006a is "bad password" and 0xc0000064 is "no such user". Telling
	// password spraying from user enumeration is the whole reason this field is
	// in the allowlist.
	if ev.Raw["SubStatus"] != "0xc000006a" {
		t.Errorf("raw[SubStatus] = %v; without it a brute force and a user enumeration look "+
			"identical", ev.Raw["SubStatus"])
	}
	if ev.Raw["IpAddress"] != "10.20.7.199" {
		t.Errorf("raw[IpAddress] = %v", ev.Raw["IpAddress"])
	}
}

func TestTheEventIDElementIsReadWithAndWithoutItsQualifiersAttribute(t *testing.T) {
	// Windows renders <EventID>4624</EventID> and
	// <EventID Qualifiers="0">4625</EventID> depending on the provider, and both
	// appear in the Security channel. A struct that only handled the bare form
	// would drop the other half of the evidence with no error at all.
	//
	// FALSIFYING OBSERVATION: the qualified fixture parsing as anything other
	// than 4625.
	ev, ok := parseFixture(t, "4625-failed-logon.xml") // this fixture is the qualified form
	if !ok || ev.Source != "win:4625" {
		t.Fatalf("the Qualifiers form parsed as ok=%v source=%q", ok, ev.Source)
	}
}

func TestAGroupChangeNamesTheMemberAddedAndNotTheGroup(t *testing.T) {
	// THE MISATTRIBUTION THIS TEST EXISTS FOR. On 4728/4732/4756, Windows puts
	// the GROUP in TargetUserName and the account being added in MemberName.
	// Reading the target -- the obvious thing, and what every other event id in
	// the taxonomy wants -- records "Administrators" as the user who was added
	// to Administrators, which is an evidence row that says nothing and looks
	// fine.
	//
	// FALSIFYING OBSERVATION: username containing "Administrators".
	ev, ok := parseFixture(t, "4732-admin-group-change.xml")
	if !ok {
		t.Fatal("4732 was dropped")
	}
	if ev.EventType != "admin_group_change" {
		t.Fatalf("event_type = %q", ev.EventType)
	}
	if strings.Contains(ev.Username, "Administrators") {
		t.Fatalf("username = %q -- that is the GROUP. The account added is in MemberName", ev.Username)
	}
	if ev.Username != `Builtin\jsmith` {
		t.Fatalf("username = %q, want %q (the CN of the MemberName DN)", ev.Username, `Builtin\jsmith`)
	}
	// The group is still evidence; it is in raw, where it belongs.
	if ev.Raw["TargetUserName"] != "Administrators" {
		t.Errorf("raw[TargetUserName] = %v; the group must still be recorded", ev.Raw["TargetUserName"])
	}
}

func TestAPrivilegeAssignmentNamesTheSubjectAndNotAnEmptyTarget(t *testing.T) {
	// 4672 has no Target* fields at all. Reading the target would produce an
	// empty username on the one event type that says "this session got
	// administrative privileges".
	ev, ok := parseFixture(t, "4672-privilege.xml")
	if !ok {
		t.Fatal("4672 was dropped")
	}
	if ev.EventType != "privilege_elevation" {
		t.Fatalf("event_type = %q", ev.EventType)
	}
	if ev.Username != `NT AUTHORITY\SYSTEM` {
		t.Fatalf("username = %q, want the SUBJECT", ev.Username)
	}
	if !strings.Contains(ev.Raw["PrivilegeList"].(string), "SeTcbPrivilege") {
		t.Errorf("raw[PrivilegeList] = %v; which privileges were assigned is the content of "+
			"this event", ev.Raw["PrivilegeList"])
	}
}

func TestAnEventOutsideTheTaxonomyIsDroppedWithoutAnError(t *testing.T) {
	// THE MOST CONSEQUENTIAL DROP IN THIS PACKAGE. `ingest_events` rejects the
	// WHOLE BATCH on an event_type outside the taxonomy (spec §6), so a single
	// 4688 shipped alongside 499 good records discards all 500 and audits an
	// ingest_rejected. The filter has to be here.
	//
	// It is also a privacy decision: the 4688 fixture's CommandLine contains a
	// password in plaintext, which is what a process-creation feed routinely
	// carries. This agent does not collect process creation.
	//
	// FALSIFYING OBSERVATION: ok=true, or an error (a common, expected record
	// must not look like a malfunction), or the command line surviving anywhere.
	ev, ok, err := parseWinEvent(fixture(t, "4688-outside-taxonomy.xml"), "184394")
	if err != nil {
		t.Fatalf("a 4688 produced an error (%v); it is an ordinary record this agent does not "+
			"collect, not a malformed one", err)
	}
	if ok {
		t.Fatalf("a 4688 was accepted as %q. Shipping it would make the server reject the whole "+
			"batch -- and its CommandLine is %q", ev.EventType, ev.Raw["CommandLine"])
	}
	if ev.Raw != nil || ev.Username != "" {
		t.Fatalf("a dropped record still produced content: %+v", ev)
	}
}

func TestTheTaxonomyIsExactlyTheTenEventIDsTheSpecNames(t *testing.T) {
	// Widening this map widens what leaves every endpoint in the fleet. 4688
	// (process creation, with command lines) or 4663 (object access) would turn
	// an authentication evidence feed into general host telemetry, which
	// docs/00-vision.md puts out of scope and which the retention table was not
	// sized for.
	//
	// FALSIFYING OBSERVATION: an id in the map that the spec does not name, or
	// one the spec names that is missing.
	want := map[int]bool{
		4624: true, 4625: true, 4634: true, 4647: true, 4740: true,
		4672: true, 4728: true, 4732: true, 4756: true, 4719: true,
	}
	for id := range eventTaxonomy {
		if !want[id] {
			t.Errorf("event id %d is collected and docs/modules/security-logs/spec.md §2 does "+
				"not name it", id)
		}
	}
	for id := range want {
		if _, ok := eventTaxonomy[id]; !ok {
			t.Errorf("the spec names event id %d and it is not collected", id)
		}
	}
}

func TestSevenDigitWindowsTimestampsAndBareOnesBothNormalise(t *testing.T) {
	// FALSIFYING OBSERVATION: a wrong VALUE. Each case names what Python would
	// do with the un-normalised form.
	for _, tc := range []struct{ in, want string }{
		// 100ns precision: fromisoformat raises on 7 fractional digits, failing
		// the whole message.
		{"2026-07-18T09:12:03.1234567Z", "2026-07-18T09:12:03.123456Z"},
		{"2026-07-18T09:12:03.000Z", "2026-07-18T09:12:03.000000Z"},
		{"2026-07-18T09:12:03Z", "2026-07-18T09:12:03.000000Z"},
		// An offset, converted rather than carried: an evidence table where half
		// the rows are local time cannot be sorted.
		{"2026-07-18T10:12:03+01:00", "2026-07-18T09:12:03.000000Z"},
		// No zone at all. The Event Log stores UTC; reading it as local would
		// shift every row by the machine's offset.
		{"2026-07-18T09:12:03", "2026-07-18T09:12:03.000000Z"},
	} {
		got, err := normalizeWinTime(tc.in)
		if err != nil {
			t.Errorf("normalizeWinTime(%q): %v", tc.in, err)
			continue
		}
		if got != tc.want {
			t.Errorf("normalizeWinTime(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
	if _, err := normalizeWinTime("not a time"); err == nil {
		t.Error("an unparseable SystemTime was accepted; ingest would raise on it and fail the " +
			"whole batch")
	}
}

func TestTheEventUIDSurvivesAClearedSecurityLog(t *testing.T) {
	// EventRecordID RESETS TO 1 WHEN THE SECURITY LOG IS CLEARED. On a uid of
	// `win:<recordID>` alone, every record after a `wevtutil cl Security` would
	// collide with a record from before it, and the server's
	// `ON CONFLICT DO NOTHING` would SILENTLY DISCARD it -- destroying exactly
	// the evidence that matters most, since clearing the Security log is the
	// most interesting thing that happens on a compromised box.
	//
	// FALSIFYING OBSERVATION: two records with the same record id and different
	// timestamps producing the same uid.
	before := winEventUID("1", "2026-07-18T09:12:03.000000Z")
	after := winEventUID("1", "2026-08-01T22:41:09.000000Z")
	if before == after {
		t.Fatalf("record #1 before and after a log clear share the uid %q; the second one would "+
			"be deduped away by the server and never appear in the evidence table", before)
	}
	// And the same record read twice must produce the SAME key, or at-least-once
	// delivery becomes at-least-once INSERTION.
	if a, b := winEventUID("184392", "2026-07-18T09:12:03.123456Z"),
		winEventUID("184392", "2026-07-18T09:12:03.123456Z"); a != b {
		t.Fatalf("the same record produced two uids, %q and %q; every resend after a reconnect "+
			"would insert a duplicate row into the SOC 2 access-evidence export", a, b)
	}
	if n := len(winEventUID("18446744073709551615", "2026-07-18T09:12:03.123456Z")); n > maxEventUIDLen {
		t.Fatalf("event_uid is %d characters; the schema caps it at %d and the column is "+
			"String(64)", n, maxEventUIDLen)
	}
}

func TestRecordIDIsReadableWithoutAFullUnmarshal(t *testing.T) {
	// recordIDOf is what advances the cursor past a DROPPED record. If it
	// disagreed with the parsed value, a tail of uncollected events would either
	// be re-read forever or skipped over.
	//
	// FALSIFYING OBSERVATION: a cheap read that differs from the parsed one.
	for _, name := range []string{"4624-network-logon.xml", "4688-outside-taxonomy.xml"} {
		raw := fixture(t, name)
		cheap := recordIDOf(raw)
		if cheap == "" {
			t.Errorf("%s: recordIDOf found nothing", name)
			continue
		}
		if ev, ok, _ := parseWinEvent(raw, ""); ok {
			if got := ev.Raw["record_id"]; got != cheap {
				t.Errorf("%s: recordIDOf = %q, the parsed record_id = %v", name, cheap, got)
			}
		}
	}
	if got := recordIDOf([]byte("<Event><System/></Event>")); got != "" {
		t.Errorf("recordIDOf on a document with no EventRecordID = %q", got)
	}
}

func TestPositionsAreComparedNumericallyAndNotLexically(t *testing.T) {
	// "9" > "10" as strings. A lexical comparison would report a rewind on the
	// tenth event of a machine's life, reset the cursor, and re-ship everything
	// -- forever, once per job.
	//
	// FALSIFYING OBSERVATION: winPositionLess("9","10") reporting false.
	if !winPositionLess("9", "10") {
		t.Error("winPositionLess(9, 10) = false; positions are being compared as strings")
	}
	if winPositionLess("10", "9") {
		t.Error("winPositionLess(10, 9) = true")
	}
	if winPositionLess("184392", "184392") {
		t.Error("a position is not less than itself")
	}
	if winPositionLess("", "10") || winPositionLess("x", "10") {
		t.Error("an unparseable position reported a rewind; that would reset the cursor on every job")
	}
}
