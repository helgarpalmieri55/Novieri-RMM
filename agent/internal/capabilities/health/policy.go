package health

import (
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"time"
)

// The bounds from HealthPolicyApplyParams.schema.json, restated here.
//
// They are duplicated agent-side deliberately, and jobtypes.py's docstring says
// why in one sentence: "a bound that lives only in the database does not travel
// with the job". §0.5 falsifier 6 is "a profile permitted to exceed the caps",
// and a cap the agent does not check is a cap that protects the agent's idle
// budget only as long as the server is the only thing that ever signs a policy.
const (
	maxChecks          = 32
	minTickInterval    = 60 * time.Second
	maxTickInterval    = 3600 * time.Second
	minPostureInterval = 900 * time.Second
	minSummaryInterval = 300 * time.Second
	maxSummaryInterval = 86400 * time.Second
	minForTicks        = 1
	maxForTicks        = 60
	maxSelectorLen     = 200
	maxThresholdLen    = 64
)

// ErrPolicy is returned for a policy this agent refuses to install. The runner
// turns it into a `failed` job; the previous policy keeps running, which is the
// safe direction -- a device that was watching a disk keeps watching it.
var ErrPolicy = errors.New("health: policy refused")

// applyParams implements HealthPolicyApplyParams.schema.json.
//
// Unknown members are IGNORED (encoding/json's default), which is the inbound
// half of docs/kernel/wire-protocol/README.md's rule: a strictly additive
// server change must not take a deployed fleet offline. What is not tolerated
// is a value OUT OF BOUNDS on a member this agent does understand -- that is
// not a newer server being additive, it is a policy that would break §0.5.
type applyParams struct {
	PolicyVersion    int         `json:"policy_version"`
	TickIntervalS    int         `json:"tick_interval_s"`
	PostureIntervalS int         `json:"posture_interval_s"`
	SummaryIntervalS int         `json:"summary_interval_s"`
	Checks           []checkWire `json:"checks"`
}

// checkWire implements HealthPolicyApplyParams.schema.json $defs
// HealthCheckSpec.
type checkWire struct {
	CheckID    string `json:"check_id"`
	CheckType  string `json:"check_type"`
	Selector   string `json:"selector"`
	Comparator string `json:"comparator"`
	Threshold  string `json:"threshold"`
	ForTicks   int    `json:"for_ticks"`
	Cadence    string `json:"cadence"`
}

// checkSpec is one installed check: the signed strings plus what the registry
// says about them, resolved ONCE at install so a tick is a comparison and an
// integer write (§4.2).
//
// The threshold is parsed here rather than per tick. Not for the nanoseconds --
// for the failure mode: a threshold that does not parse is a policy this agent
// refuses at install, not a check that silently never fires.
type checkSpec struct {
	checkID   string
	checkType string
	selector  string

	comparator string
	threshold  string

	kind valueKind
	how  render

	thresholdNum  float64
	thresholdBool bool
	// enum is the registry's two members, copied so a summary line can render
	// the member name back out of the 12-byte state without a map lookup.
	enum [2]string

	forTicks int32
	posture  bool
	// known is false for a check type this build's registry does not carry --
	// a NEWER control plane that added one. It is installed and reports
	// `unavailable` (§0.7) rather than costing the device its whole policy.
	known bool
}

// policy is the whole installed policy. It is replaced wholesale (§5.2), never
// merged: two live copies of one check would double every report the endpoint
// sends.
type policy struct {
	version    int
	tick       time.Duration
	posturePer int // fast ticks per posture sub-tick
	summaryPer int // fast ticks per summary
	checks     []checkSpec
}

// parsePolicy validates a signed `health.policy.apply` and resolves it.
//
// Every refusal names the bound it broke. §0.5 falsifier 6's falsifying
// observation is that the tick engine ends up ARMED with an over-cap policy, so
// this function returning an error is only half the guarantee -- install() is
// what must not run, and the test asserts the sampler's counter rather than the
// error.
func parsePolicy(raw json.RawMessage) (*policy, []string, error) {
	var p applyParams
	if err := json.Unmarshal(raw, &p); err != nil {
		return nil, nil, fmt.Errorf("%w: params are not a HealthPolicyApplyParams: %w", ErrPolicy, err)
	}
	if p.PolicyVersion < 0 {
		return nil, nil, fmt.Errorf("%w: policy_version %d is below 0", ErrPolicy, p.PolicyVersion)
	}
	tick := time.Duration(p.TickIntervalS) * time.Second
	if tick < minTickInterval || tick > maxTickInterval {
		return nil, nil, fmt.Errorf("%w: tick_interval_s %d is outside %d..%d",
			ErrPolicy, p.TickIntervalS, int(minTickInterval.Seconds()), int(maxTickInterval.Seconds()))
	}
	posture := time.Duration(p.PostureIntervalS) * time.Second
	if posture < minPostureInterval {
		return nil, nil, fmt.Errorf("%w: posture_interval_s %d is below %d",
			ErrPolicy, p.PostureIntervalS, int(minPostureInterval.Seconds()))
	}
	summary := time.Duration(p.SummaryIntervalS) * time.Second
	if summary < minSummaryInterval || summary > maxSummaryInterval {
		return nil, nil, fmt.Errorf("%w: summary_interval_s %d is outside %d..%d",
			ErrPolicy, p.SummaryIntervalS, int(minSummaryInterval.Seconds()), int(maxSummaryInterval.Seconds()))
	}
	if len(p.Checks) > maxChecks {
		return nil, nil, fmt.Errorf("%w: %d checks, above §0.11's cap of %d",
			ErrPolicy, len(p.Checks), maxChecks)
	}

	pol := &policy{
		version: p.PolicyVersion,
		tick:    tick,
		// Sub-tick counters, not second entries on the wheel (§0.5's
		// mechanism). Rounded DOWN to at least one so a posture interval that
		// is not a multiple of the tick fires slightly early rather than never.
		posturePer: subTicks(posture, tick),
		summaryPer: subTicks(summary, tick),
		checks:     make([]checkSpec, 0, len(p.Checks)),
	}

	var dropped []string
	for i := range p.Checks {
		spec, drop, err := resolveCheck(&p.Checks[i])
		if err != nil {
			return nil, nil, err
		}
		if drop != "" {
			dropped = append(dropped, drop)
			continue
		}
		pol.checks = append(pol.checks, spec)
	}
	return pol, dropped, nil
}

// subTicks is how many fast ticks fit in a sub-tick interval, at least one.
func subTicks(interval, tick time.Duration) int {
	n := int(interval / tick)
	if n < 1 {
		return 1
	}
	return n
}

// resolveCheck validates one check and resolves it against the registry.
//
// The second return value is non-empty when the check is DROPPED rather than
// refused: today that is `agent.offline` and nothing else. Dropping one check
// costs the device that check; refusing the policy costs it every check, and
// §0.6's type is one the server already filters out, so its arrival is a
// server-side bug and not a reason to disarm the endpoint.
func resolveCheck(w *checkWire) (checkSpec, string, error) {
	if w.CheckID == "" {
		return checkSpec{}, "", fmt.Errorf("%w: a check with no check_id can never be reported", ErrPolicy)
	}
	if len(w.Selector) > maxSelectorLen {
		return checkSpec{}, "", fmt.Errorf("%w: check %s selector is %d bytes, above %d",
			ErrPolicy, w.CheckID, len(w.Selector), maxSelectorLen)
	}
	if len(w.Threshold) > maxThresholdLen {
		return checkSpec{}, "", fmt.Errorf("%w: check %s threshold is %d bytes, above %d",
			ErrPolicy, w.CheckID, len(w.Threshold), maxThresholdLen)
	}
	if w.ForTicks < minForTicks || w.ForTicks > maxForTicks {
		return checkSpec{}, "", fmt.Errorf("%w: check %s has for_ticks %d, outside %d..%d",
			ErrPolicy, w.CheckID, w.ForTicks, minForTicks, maxForTicks)
	}
	if w.Cadence != "tick" && w.Cadence != "posture" {
		return checkSpec{}, "", fmt.Errorf("%w: check %s has cadence %q, which is neither tick nor posture",
			ErrPolicy, w.CheckID, w.Cadence)
	}

	spec := checkSpec{
		// §5.2/policy_push.py:115: check_id is `str(check.id)`. It is echoed
		// VERBATIM -- never parsed, never normalised, never regenerated. The
		// server matches the report to a row by this exact string.
		checkID:    w.CheckID,
		checkType:  w.CheckType,
		selector:   w.Selector,
		comparator: w.Comparator,
		threshold:  w.Threshold,
		forTicks:   int32(w.ForTicks),
		posture:    w.Cadence == "posture",
	}

	entry, ok := checkTypes[w.CheckType]
	if !ok {
		// A newer control plane added a check type this build does not have.
		// INSTALLED and reported `unavailable` (§0.7), not refused: refusing
		// the policy would disarm every OTHER check on the device because the
		// server learned a new word. docs/kernel/wire-protocol/README.md:
		// loosen inbound before you tighten outbound.
		return spec, "", nil
	}
	if entry.server {
		return checkSpec{}, w.CheckID, nil
	}

	spec.known = true
	spec.kind = entry.kind
	spec.how = entry.how
	spec.enum = entry.enum

	switch entry.kind {
	case kindNumeric:
		if !numericComparators[w.Comparator] {
			return checkSpec{}, "", fmt.Errorf("%w: check %s is numeric (%s) and carries comparator %q; "+
				"legal comparators are gt|gte|lt|lte|eq|neq", ErrPolicy, w.CheckID, w.CheckType, w.Comparator)
		}
		v, err := strconv.ParseFloat(w.Threshold, 64)
		if err != nil {
			return checkSpec{}, "", fmt.Errorf("%w: check %s is numeric (%s) and its threshold %q is not a number",
				ErrPolicy, w.CheckID, w.CheckType, w.Threshold)
		}
		spec.thresholdNum = v
	case kindBoolean:
		if w.Comparator != "is" {
			return checkSpec{}, "", fmt.Errorf("%w: check %s is boolean (%s) and carries comparator %q; "+
				"§4.1: boolean and enum types take `is` and nothing else -- this is the rule that stops "+
				"\"disk encryption > true\"", ErrPolicy, w.CheckID, w.CheckType, w.Comparator)
		}
		switch w.Threshold {
		case "true":
			spec.thresholdBool = true
		case "false":
			spec.thresholdBool = false
		default:
			return checkSpec{}, "", fmt.Errorf("%w: check %s is boolean (%s) and its threshold is %q, "+
				"not true/false (registry.py::render_threshold emits exactly those)",
				ErrPolicy, w.CheckID, w.CheckType, w.Threshold)
		}
	case kindEnum:
		if w.Comparator != "is" {
			return checkSpec{}, "", fmt.Errorf("%w: check %s is enum (%s) and carries comparator %q; "+
				"§4.1: enum types take `is` and nothing else", ErrPolicy, w.CheckID, w.CheckType, w.Comparator)
		}
		if w.Threshold != entry.enum[0] && w.Threshold != entry.enum[1] {
			return checkSpec{}, "", fmt.Errorf("%w: check %s is enum (%s) and its threshold %q is not one of "+
				"%q/%q -- §4.1's amendment has render_threshold emit the MEMBER NAME so both sides compare "+
				"the same alphabet", ErrPolicy, w.CheckID, w.CheckType, w.Threshold, entry.enum[0], entry.enum[1])
		}
	}
	return spec, "", nil
}
