package health

import (
	"strconv"
	"strings"
)

// valueKind is HealthCheckSpec's `value_kind`, which is NOT on the wire: it
// comes from the check-type registry on both sides. The agent needs it to know
// which comparator is legal and how to render an observed value.
type valueKind uint8

const (
	kindNumeric valueKind = iota
	kindBoolean
	kindEnum
)

// render says how an observed value becomes the STRING that travels in
// `health_report.value` (§5.1: it is a string by design, §2.4's reasoning).
//
// The distinction between renderPercent and renderCount is §4.1's: a percentage
// is "3.1" and an event count is "7", and an agent that sent "7.0" for a count
// would make the operator's threshold comparison read as a float where the
// registry says integer.
type render uint8

const (
	renderPercent render = iota // strconv.FormatFloat(v, 'f', 1, 64) -- §5.1's "3.1"
	renderCount                 // base-10 integer: byte counts, event counts
	renderBool                  // "true" / "false"
	renderEnum                  // the member name, e.g. "running"
)

// checkType is one row of §4.1's table, agent-side.
//
// # Why this list is duplicated here rather than derived from the job
//
// app/modules/health/jobtypes.py says it outright: "a bound that lives only in
// the database does not travel with the job". The same holds for the registry.
// The signed policy carries `check_type`, `comparator` and `threshold` as
// strings; without a local registry the agent would have to believe whatever
// pairing arrived, and "disk encryption > true" would be evaluated rather than
// refused. §4.1's own sentence is that the registry "is the only thing between"
// those two outcomes.
//
// It is a CLOSED set, and adding a type is a code change on both sides plus an
// edit to §4.1 -- deliberately not a configuration row (§0.1).
type checkType struct {
	name    string
	kind    valueKind
	how     render
	cadence string // the registry's cadence; see the note on drift below
	// enum holds the two members of an enum type. §4.1's amendment: exactly
	// two, and enum[0] is the member `threshold_bool = true` denotes.
	enum [2]string
	// server marks the one check type the AGENT never evaluates (§0.6).
	server bool
}

// checkTypes mirrors backend/app/modules/health/registry.py::CHECK_TYPES.
//
// The `cadence` column here is the registry's, and it is NOT what the tick
// engine schedules on: the engine uses the cadence in the SIGNED policy, which
// the server emitted from its own copy of this registry (§5.2 -- "cadence is
// emitted from the check-type REGISTRY and not from the row"). A disagreement
// between the two is a version skew, and obeying the signed value is the only
// answer that keeps an old agent working against a newer server.
var checkTypes = map[string]checkType{
	"cpu.busy_pct":              {name: "cpu.busy_pct", kind: kindNumeric, how: renderPercent, cadence: "tick"},
	"memory.used_pct":           {name: "memory.used_pct", kind: kindNumeric, how: renderPercent, cadence: "tick"},
	"disk.free_pct":             {name: "disk.free_pct", kind: kindNumeric, how: renderPercent, cadence: "tick"},
	"disk.free_bytes":           {name: "disk.free_bytes", kind: kindNumeric, how: renderCount, cadence: "tick"},
	"service.state":             {name: "service.state", kind: kindEnum, how: renderEnum, cadence: "tick", enum: [2]string{"running", "stopped"}},
	"process.present":           {name: "process.present", kind: kindBoolean, how: renderBool, cadence: "tick"},
	"disk.smart_ok":             {name: "disk.smart_ok", kind: kindBoolean, how: renderBool, cadence: "posture"},
	"posture.disk_encryption":   {name: "posture.disk_encryption", kind: kindBoolean, how: renderBool, cadence: "posture"},
	"posture.antivirus_healthy": {name: "posture.antivirus_healthy", kind: kindBoolean, how: renderBool, cadence: "posture"},
	"posture.firewall_enabled":  {name: "posture.firewall_enabled", kind: kindBoolean, how: renderBool, cadence: "posture"},
	"eventlog.disk_error":       {name: "eventlog.disk_error", kind: kindNumeric, how: renderCount, cadence: "tick"},
	"eventlog.service_crash":    {name: "eventlog.service_crash", kind: kindNumeric, how: renderCount, cadence: "tick"},
	"eventlog.kernel_fault":     {name: "eventlog.kernel_fault", kind: kindNumeric, how: renderCount, cadence: "tick"},
	// §0.6: an agent that is offline cannot report that it is offline. The
	// server filters this type out of every push; if one arrives anyway it is
	// DROPPED with a WARN rather than evaluated, because inventing an
	// evaluation for it is inventing a fact.
	"agent.offline": {name: "agent.offline", kind: kindNumeric, how: renderCount, cadence: "posture", server: true},
}

// numericComparators is registry.py::NUMERIC_COMPARATORS. Boolean and enum
// types take `is` and nothing else.
var numericComparators = map[string]bool{
	"gt": true, "gte": true, "lt": true, "lte": true, "eq": true, "neq": true,
}

// eventlogPrefix marks the types that ride the security-logs capability's
// subscription (§4.1†, §0.5 item 5).
const eventlogPrefix = "eventlog."

// Sample is one observation. It is returned BY VALUE and carries no slice, no
// map and no pointer to anything the sampler keeps, so a quiet tick allocates
// nothing (§0.5's mechanism; internal/budget asserts it).
//
// `Available == false` is §0.7's third state and is a real answer: "this host
// cannot answer this check" is different from "this check is fine" and
// different from "this check is breaching". It never advances the consecutive
// counter.
type Sample struct {
	Available bool
	Num       float64
	Bool      bool
	// Enum is a member name from the registry ("running"), never a free string
	// the host produced. It points at a package constant, so assigning it costs
	// no allocation.
	Enum string
}

// unavailable is the sample a host returns for a check it cannot answer.
func unavailable() Sample { return Sample{} }

// Sampler reads one check's current value.
//
// checkType and selector are the SIGNED strings from the policy. A sampler
// never receives anything else, and in particular never receives a command, a
// script or a path the server composed -- §5.2: "the only strings that travel
// are a check id, a registry type name, a selector and a threshold".
type Sampler interface {
	Sample(checkType, selector string) Sample
}

// matches reports whether the sample satisfies the check's BREACH predicate.
//
// # The asymmetry between numeric and state comparators, resolved
//
// For a numeric check the comparator and threshold are the breach predicate:
// §9.1's alert payload is `observed_value: "3.1", threshold: "10",
// comparator: "lt"`, which is an OPEN alert -- so `lt 10` breaching at 3.1.
//
// For boolean and enum the only legal comparator is `is`, and §4.1's amendment
// defines the threshold as the HEALTHY value: "`threshold_bool = true` means
// 'healthy is running'". So `is` breaches when the sample is NOT the threshold.
// The spec states the two conventions in two places and never side by side;
// this is where they meet, and the resolution is §4.1's own sentence rather
// than a guess. A test names it.
func (s Sample) matches(c *checkSpec) bool {
	if !s.Available {
		return false
	}
	switch c.kind {
	case kindNumeric:
		switch c.comparator {
		case "gt":
			return s.Num > c.thresholdNum
		case "gte":
			return s.Num >= c.thresholdNum
		case "lt":
			return s.Num < c.thresholdNum
		case "lte":
			return s.Num <= c.thresholdNum
		case "eq":
			return s.Num == c.thresholdNum
		case "neq":
			return s.Num != c.thresholdNum
		}
		return false
	case kindBoolean:
		return s.Bool != c.thresholdBool
	case kindEnum:
		return s.Enum != c.threshold
	}
	return false
}

// maxValueLen is HealthReportLine.value's max_length (§5.1). A line above it is
// dropped by parse_agent_message and the whole message with it, so the value is
// truncated here and the truncation logged.
const maxValueLen = 64

// renderValue turns a sample into `health_report.value`.
//
// It allocates -- FormatFloat does -- and that is why it is called only when a
// line is actually being emitted. The quiet path never reaches it, which is
// what internal/budget's zero-allocation assertion is measuring.
func renderValue(s Sample, c *checkSpec) string {
	if !s.Available {
		return ""
	}
	switch c.how {
	case renderPercent:
		return clampValue(strconv.FormatFloat(s.Num, 'f', 1, 64))
	case renderCount:
		return clampValue(strconv.FormatInt(int64(s.Num), 10))
	case renderBool:
		if s.Bool {
			return "true"
		}
		return "false"
	case renderEnum:
		return clampValue(s.Enum)
	}
	return ""
}

// clampValue truncates on a rune boundary. Truncating mid-rune produces invalid
// UTF-8, which the gateway's JSON decode would reject -- costing the whole
// message rather than one long value.
func clampValue(v string) string {
	if len(v) <= maxValueLen {
		return v
	}
	n := maxValueLen
	for n > 0 && !utf8Start(v[n]) {
		n--
	}
	return v[:n]
}

func utf8Start(b byte) bool { return b&0xC0 != 0x80 }

// selectorLooksLikeAll is a convenience for samplers: `*` means "worst" for the
// disk checks and "all" for smart (§4.1's selector column).
func selectorIsWildcard(selector string) bool { return strings.TrimSpace(selector) == "*" }
