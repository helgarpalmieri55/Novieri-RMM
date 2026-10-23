package logs

import (
	"encoding/xml"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/toeshee/it-suite/agent/internal/wire"
)

// WINDOWS SECURITY EVENTS, NORMALISED. This file is platform-independent on
// purpose.
//
// `EvtRender(EvtRenderEventXml)` hands back a UTF-16 XML document and nothing
// else; everything after that is string handling. Keeping the parse and the
// taxonomy mapping out of source_windows.go is what lets Linux CI test the part
// that decides what evidence reaches the control plane -- the event-id mapping,
// the username assembly, the timestamp normalisation, the `raw` construction --
// against recorded documents, on a container with no Event Log.
//
// What is NOT tested here is the retrieval: EvtQuery/EvtNext/EvtRender are in
// source_windows.go and have never executed in this repository.
//
// # The taxonomy is filtered ON THE AGENT, and that is not an optimisation
//
// `ingest_events` REJECTS THE WHOLE BATCH on an event_type outside the v1
// taxonomy (spec §6) and audits `security_logs.ingest_rejected`. A single
// unmapped event id shipped alongside 499 good ones therefore discards all 500.
// So this file's job is as much to DROP records as to convert them, and
// `eventTaxonomy` is the entire set of records this agent will ever ship.
//
// # The event ids
//
// From docs/modules/security-logs/spec.md §2, which names them explicitly:
// 4624/4625/4634/4647/4740/4672/4728/4732/4756/4719. Nothing else is collected.
// Widening this map widens what leaves the endpoint, and it is a decision, not
// a tweak: 4688 (process creation) or 4663 (object access) would turn a logon
// evidence feed into general host telemetry, which docs/00-vision.md puts out
// of scope.

// winChannel is the only channel this agent reads. Named here because
// source_windows.go builds its query from it and because "which channel" is the
// first question an EDR reviewer asks.
const winChannel = "Security"

// taxonomyEntry is one event id's mapping.
type taxonomyEntry struct {
	eventType string
	outcome   string
	// principal names which set of EventData fields carries the human this
	// event is about. Windows is not consistent about it, and getting it wrong
	// produces evidence attributed to the wrong person -- SYSTEM instead of the
	// account that failed to log in.
	principal principalSource
}

type principalSource int

const (
	// principalTarget: the account being acted on (TargetUserName /
	// TargetDomainName). Logons, logoffs, lockouts.
	principalTarget principalSource = iota
	// principalSubject: the account doing the acting (SubjectUserName /
	// SubjectDomainName). Privilege assignment, policy change.
	principalSubject
	// principalMember: the account being ADDED to a group (MemberName, which is
	// a distinguished name, or MemberSid). The group itself is in
	// TargetUserName -- so reading the target here would record the group as
	// the user.
	principalMember
)

// eventTaxonomy is the complete set of Windows Security events this agent
// ships, and the v1 taxonomy each maps to.
var eventTaxonomy = map[int]taxonomyEntry{
	4624: {"logon_success", "success", principalTarget},
	4625: {"logon_failure", "failure", principalTarget},
	4634: {"logoff", "n_a", principalTarget},
	4647: {"logoff", "n_a", principalTarget},
	4740: {"account_lockout", "n_a", principalTarget},
	4672: {"privilege_elevation", "success", principalSubject},
	4728: {"admin_group_change", "success", principalMember},
	4732: {"admin_group_change", "success", principalMember},
	4756: {"admin_group_change", "success", principalMember},
	4719: {"policy_change", "success", principalSubject},
}

// winEvent is the subset of the rendered XML this agent reads.
//
// Note what is absent: Provider GUID, Keywords, Task, Opcode, Execution,
// Correlation. They are in the document and they stay there -- an evidence feed
// that carried every attribute of every record would be host telemetry, and
// this one is scoped to "who logged in, from where, and did it work".
type winEvent struct {
	XMLName xml.Name `xml:"Event"`
	System  struct {
		EventID     winEventID `xml:"EventID"`
		TimeCreated struct {
			SystemTime string `xml:"SystemTime,attr"`
		} `xml:"TimeCreated"`
		EventRecordID string `xml:"EventRecordID"`
		Channel       string `xml:"Channel"`
		Computer      string `xml:"Computer"`
	} `xml:"System"`
	EventData struct {
		Data []winData `xml:"Data"`
	} `xml:"EventData"`
}

// winEventID absorbs the two shapes Windows uses for <EventID>: a bare number,
// and a number with a `Qualifiers` attribute. Both appear in the Security
// channel depending on the provider, and a struct that only handled one of them
// would drop half the evidence with no error.
type winEventID struct {
	Value string `xml:",chardata"`
}

type winData struct {
	Name  string `xml:"Name,attr"`
	Value string `xml:",chardata"`
}

// parseWinEvent turns one rendered XML document into a wire event.
//
// ok=false means "outside the v1 taxonomy" -- a legitimate, common answer,
// since the Security channel carries hundreds of event ids this agent does not
// collect. An error means the document could not be read at all.
func parseWinEvent(payload []byte, position string) (wire.SecurityEvent, bool, error) {
	var ev winEvent
	if err := xml.Unmarshal(payload, &ev); err != nil {
		return wire.SecurityEvent{}, false, fmt.Errorf("rendered event xml: %w", err)
	}

	id, err := strconv.Atoi(strings.TrimSpace(ev.System.EventID.Value))
	if err != nil {
		return wire.SecurityEvent{}, false, fmt.Errorf("event id %q: %w", ev.System.EventID.Value, err)
	}
	entry, known := eventTaxonomy[id]
	if !known {
		return wire.SecurityEvent{}, false, nil
	}

	ts, err := normalizeWinTime(ev.System.TimeCreated.SystemTime)
	if err != nil {
		// A record with no usable timestamp is not evidence: `ingest_events`
		// calls datetime.fromisoformat on it, and a value that raises there
		// fails the WHOLE batch.
		return wire.SecurityEvent{}, false, fmt.Errorf("TimeCreated %q: %w",
			ev.System.TimeCreated.SystemTime, err)
	}

	fields := make(map[string]string, len(ev.EventData.Data))
	for _, d := range ev.EventData.Data {
		if d.Name == "" {
			continue
		}
		fields[d.Name] = strings.TrimSpace(d.Value)
	}

	recordID := strings.TrimSpace(ev.System.EventRecordID)
	if recordID == "" {
		recordID = position
	}

	out := wire.SecurityEvent{
		EventUID:  winEventUID(recordID, ts),
		TS:        ts,
		EventType: entry.eventType,
		Username:  winPrincipal(entry.principal, fields),
		// The spec's own example shape: `"source": "win:4624"`, "OS-native
		// channel/id". Only the Security channel is ever read, so the id alone
		// identifies the record type without spending characters on a constant.
		Source:  "win:" + strconv.Itoa(id),
		Outcome: entry.outcome,
		Raw:     winRaw(id, recordID, ev.System.Computer, fields),
	}
	return out, true, nil
}

// winEventUID derives the server's idempotency key.
//
// # It is DERIVED, not generated, and that is the whole dedupe contract
//
// Spec §4's example annotates `event_uid` as an "agent-generated uuid". A fresh
// UUID per read would make the server's `ON CONFLICT (device_id, event_uid) DO
// NOTHING` dedupe inert: every resend after a reconnect -- and resends are
// guaranteed, because the outbox replays after a dropped uplink -- would insert
// the same logon again, and the SOC 2 access-evidence CSV would show one login
// four times. A deterministic uid is what makes at-least-once delivery
// idempotent. Recorded as an amendment in spec §4.1.
//
// # Why the timestamp is in it as well as the record id
//
// `EventRecordID` is monotonic WITHIN A CHANNEL AND RESETS TO 1 WHEN THE LOG IS
// CLEARED. On a uid of `win:<recordID>` alone, the first 184,392 events after a
// `wevtutil cl Security` would collide with the events that preceded it and the
// server would silently DISCARD THEM ALL as duplicates -- destroying exactly
// the evidence that matters most, since clearing the Security log is itself the
// most interesting thing that happened on that box. Including the record's own
// timestamp makes a post-clear record #1 a different key from a pre-clear
// record #1, at a cost of thirteen characters.
//
// Compact time form ("20260718T091203Z") because event_uid is String(64) and
// the schema enforces maxLength 64.
func winEventUID(recordID, ts string) string {
	compact := ts
	if t, err := time.Parse(time.RFC3339Nano, ts); err == nil {
		compact = t.UTC().Format("20060102T150405Z")
	}
	return clamp("win:"+recordID+":"+compact, maxEventUIDLen)
}

// winPrincipal assembles `DOMAIN\user` from whichever field pair this event id
// carries its human in.
func winPrincipal(src principalSource, fields map[string]string) string {
	switch src {
	case principalMember:
		// MemberName is a distinguished name ("CN=jsmith,OU=Users,DC=corp,..."),
		// and the CN is the part a human reads. MemberSid is the fallback when
		// the DC did not resolve the name.
		if dn := fields["MemberName"]; dn != "" && dn != "-" {
			if cn := commonName(dn); cn != "" {
				return join(fields["TargetDomainName"], cn)
			}
			return dn
		}
		if sid := fields["MemberSid"]; sid != "" && sid != "-" {
			return sid
		}
		return join(fields["SubjectDomainName"], fields["SubjectUserName"])
	case principalSubject:
		return join(fields["SubjectDomainName"], fields["SubjectUserName"])
	default:
		if u := fields["TargetUserName"]; u != "" && u != "-" {
			return join(fields["TargetDomainName"], u)
		}
		return join(fields["SubjectDomainName"], fields["SubjectUserName"])
	}
}

func join(domain, user string) string {
	user = strings.TrimSpace(user)
	domain = strings.TrimSpace(domain)
	if user == "" || user == "-" {
		return ""
	}
	if domain == "" || domain == "-" {
		return user
	}
	return domain + `\` + user
}

// commonName pulls the CN out of a distinguished name.
func commonName(dn string) string {
	for _, part := range strings.Split(dn, ",") {
		part = strings.TrimSpace(part)
		if rest, ok := strings.CutPrefix(part, "CN="); ok {
			return rest
		}
		if rest, ok := strings.CutPrefix(part, "cn="); ok {
			return rest
		}
	}
	return ""
}

// winRawFields are the EventData members carried into `raw`.
//
// AN ALLOWLIST, NOT A DENYLIST, and the difference is the point. The Security
// channel's EventData varies by event id and by Windows version; a denylist
// would ship whatever Microsoft adds next, and some of what they have added
// before is a password-policy blob or a full command line. This list is what a
// logon investigation actually needs: who, from where, how, and why it failed.
var winRawFields = []string{
	"LogonType",         // 2 interactive, 3 network, 10 RDP -- the single most useful field
	"IpAddress",         // source address (spec's own example calls this out)
	"IpPort",            //
	"WorkstationName",   //
	"TargetUserSid",     //
	"SubjectUserName",   // who acted, when the principal is the target
	"SubjectDomainName", //
	"TargetUserName",    // for a group change this is the GROUP
	"TargetDomainName",  //
	"MemberName",        //
	"MemberSid",         //
	"AuthenticationPackageName",
	"LogonProcessName",
	"ProcessName",
	"Status",        // 4625: the NTSTATUS
	"SubStatus",     // 4625: why, precisely (bad password vs no such user)
	"FailureReason", //
	"PrivilegeList", // 4672: which privileges were assigned
	"CategoryId",    // 4719
	"SubcategoryId", //
	"AuditPolicyChanges",
}

// winRaw builds the `raw` document: the native identifiers, and the allowlisted
// EventData fields that were actually present.
//
// Absent fields are OMITTED rather than emitted empty. A `raw` full of `""`
// makes it impossible to tell "Windows did not record this" from "the field was
// blank", and the whole value of `raw` is that it is what the OS said.
func winRaw(id int, recordID, computer string, fields map[string]string) map[string]any {
	raw := make(map[string]any, 4+len(winRawFields))
	raw["event_id"] = id
	raw["record_id"] = recordID
	raw["channel"] = winChannel
	if computer != "" {
		raw["computer"] = computer
	}
	for _, name := range winRawFields {
		if v, ok := fields[name]; ok && v != "" && v != "-" {
			raw[name] = v
		}
	}
	return raw
}

// normalizeWinTime converts a Windows `SystemTime` into something
// `datetime.fromisoformat` accepts.
//
// TWO REAL CONVERSIONS, EACH OF WHICH WOULD OTHERWISE DISCARD A WHOLE BATCH:
//
//  1. Windows renders 100-nanosecond precision -- SEVEN fractional digits
//     ("2026-07-18T09:12:03.1234567Z"). `datetime.fromisoformat` accepts 0, 3
//     or 6 fractional digits and RAISES on seven. Since `ingest_events` calls
//     it per event with no per-event error handling, one such value fails the
//     whole message. Truncated to microseconds here.
//  2. Some providers render no fractional part and no zone. Parsed as UTC,
//     because the Event Log stores UTC and a local-time reading would shift
//     every timestamp in the evidence trail by the machine's offset.
func normalizeWinTime(systemTime string) (string, error) {
	s := strings.TrimSpace(systemTime)
	if s == "" {
		return "", fmt.Errorf("empty")
	}
	for _, layout := range []string{
		time.RFC3339Nano,
		"2006-01-02T15:04:05.9999999Z0700",
		"2006-01-02T15:04:05Z0700",
		"2006-01-02T15:04:05.999999999",
		"2006-01-02T15:04:05",
	} {
		if t, err := time.Parse(layout, s); err == nil {
			// Microseconds, literal Z. `.000000` is kept even when zero so the
			// server sees one shape from every endpoint.
			return t.UTC().Format("2006-01-02T15:04:05.000000Z"), nil
		}
	}
	return "", fmt.Errorf("no known layout")
}

// winPositionLess compares two EventRecordID positions numerically.
//
// NUMERICALLY, not lexically, and that is not a detail: "9" sorts after "10" as
// a string, so a lexical comparison would report a rewind on the tenth event of
// a machine's life and reset the cursor. Used by the Windows source's Rewound.
func winPositionLess(a, b string) bool {
	ai, aerr := strconv.ParseUint(strings.TrimSpace(a), 10, 64)
	bi, berr := strconv.ParseUint(strings.TrimSpace(b), 10, 64)
	if aerr != nil || berr != nil {
		return false
	}
	return ai < bi
}

// recordIDOf pulls <EventRecordID> out of a rendered document without a full
// unmarshal.
//
// It is the CURSOR POSITION, and it has to be available for a record whose
// taxonomy filter drops it -- otherwise a tail of uncollected records would
// never advance the cursor and every job would re-read them. A full
// xml.Unmarshal per record purely to find one integer would roughly double the
// per-record cost of the drop path, which is the common path.
func recordIDOf(payload []byte) string {
	const open, close = "<EventRecordID>", "</EventRecordID>"
	s := string(payload)
	i := strings.Index(s, open)
	if i < 0 {
		return ""
	}
	rest := s[i+len(open):]
	j := strings.Index(rest, close)
	if j < 0 {
		return ""
	}
	return strings.TrimSpace(rest[:j])
}

// sortInts is sort.Ints, wrapped so the Windows source does not import sort for
// one call.
//
// Kept HERE rather than moved into source_windows.go precisely because of that
// sentence: moving it would put `sort` in the Windows source's imports, which is
// the thing the wrapper exists to avoid.
//
//lint:ignore U1000 consumed by source_windows.go; staticcheck runs at GOOS=linux
func sortInts(v []int) { sort.Ints(v) }
