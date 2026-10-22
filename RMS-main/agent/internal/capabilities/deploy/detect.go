package deploy

import "os"

// AGENT-SIDE DETECTION (spec AD-06: "evaluated server-side against inventory
// before dispatch and re-checked agent-side").
//
// The grammar is closed (app/modules/deploy/packages.py::RULE_TYPES):
// `file_exists`, `registry_value`, `package_installed`, `command_exit_zero`.
// THIS BUILD IMPLEMENTS ONE OF THEM, and reports "I could not evaluate that"
// for the other three rather than guessing.
//
// Why `detected_after` is nullable and why that matters: InstallOutcome's
// `detected_after` is `bool | None`, and the three-valued shape is the whole
// reason the honest answer exists. Collapsing "cannot evaluate" into `false`
// would report every successful install of a `registry_value`-detected package
// as "installed but not detected", which is an operator chasing a
// non-existent problem on every device in the fleet.
//
// Why `command_exit_zero` is NOT implemented, and is not an oversight:
// it is a SECOND arbitrary-execution path. The command would arrive inside
// `detection_rule`, which is an `additionalProperties: true` free-form dict —
// so unlike `script.run`'s body, which is a first-class signed field with a
// server-side authoring flow, RBAC gate (`deploy.script.manage`) and an audit
// trail per version, a detection command is an unreviewed string in a JSON
// blob attached to a package. Both are covered by the same signature, so this
// is not a signing question; it is a question of how many places in this
// system can put a program on an endpoint, and the answer should stay small.
// Implementing it is a deliberate decision for a later cycle, recorded in
// docs/modules/app-deploy/agent-capability.md.

// detection is the tri-state answer.
type detection struct {
	// Known reports whether this build could evaluate the rule at all.
	Known bool
	// Matched is meaningful only when Known.
	Matched bool
}

// unknownDetection is the answer for an empty rule, an unrecognised type, or a
// type this build does not implement.
var unknownDetection = detection{}

// evaluate runs a detection rule.
//
// `rule` came out of the signed params, so it is authorised — but it is a
// free-form dict, so every value is type-asserted rather than assumed, and a
// rule whose fields are the wrong shape is UNKNOWN rather than false. "The
// rule was malformed" and "the package is absent" are different facts and only
// one of them justifies an install.
func evaluate(rule map[string]any, stat Stat) detection {
	if len(rule) == 0 {
		return unknownDetection
	}
	kind, _ := rule["type"].(string)
	switch kind {
	case "file_exists":
		path, ok := rule["path"].(string)
		if !ok || path == "" {
			return unknownDetection
		}
		return detection{Known: true, Matched: stat(path)}
	}
	// registry_value, package_installed, command_exit_zero, and anything a
	// future server invents.
	return unknownDetection
}

// desiredStateReached reports whether the device is ALREADY where `action`
// wants it, mirroring app/modules/deploy/detect.py::device_in_desired_state:
// for install/update that is "the rule matches"; for uninstall it is "the rule
// does not match".
//
// It returns false whenever the rule could not be evaluated. That is the same
// deliberately permissive default the server uses — "not confirmed to be in
// the desired state" — and it errs toward doing the work rather than toward
// silently skipping a deployment an operator asked for.
func desiredStateReached(action string, d detection) bool {
	if !d.Known {
		return false
	}
	if action == "uninstall" {
		return !d.Matched
	}
	return d.Matched
}

// detectedAfter renders a post-execution detection as InstallOutcome's
// nullable bool.
func detectedAfter(d detection) *bool {
	if !d.Known {
		return nil
	}
	m := d.Matched
	return &m
}

// osStat is the production Stat. It reports existence and nothing else — not
// type, not mode. A `file_exists` rule that matched a directory is the rule's
// author's problem, and inventing a "must be a regular file" clause the server
// does not document would make this agent disagree with
// app/modules/deploy/detect.py about what the same rule means.
func osStat(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}
