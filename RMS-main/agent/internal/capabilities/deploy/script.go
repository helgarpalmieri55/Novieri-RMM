package deploy

import (
	"context"
	"encoding/json"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// `script.run` (docs/modules/app-deploy/spec.md §9, AD-02).
//
// The body travels inside the Ed25519-signed job envelope. By the time
// runScript is called, internal/jobs has already verified that signature over
// the canonical bytes — which INCLUDE the body — against the key pinned at
// enrollment. There is no local allowlist of permitted scripts, no cached
// approval, and no "we ran this last week" path: the only thing that
// authorises a body to run as SYSTEM/root is the server's signature on THIS
// dispatch, and it has already been checked.

// scriptParams implements ScriptRunParams.schema.json.
//
// Unknown members are ignored (encoding/json's default) — the inbound half of
// docs/kernel/wire-protocol/README.md's rule, so a strictly additive server
// change does not take a deployed fleet offline.
//
// TimeoutS is a POINTER because its schema default (3600) is not its zero
// value. Collapsing them would make an absent member read as `timeout_s: 0`,
// which this file refuses — turning every script run from a slightly older
// control plane into a refusal.
type scriptParams struct {
	Interpreter   string `json:"interpreter"`
	Body          string `json:"body"`
	ScriptID      string `json:"script_id"`
	ScriptVersion int    `json:"script_version"`
	TimeoutS      *int   `json:"timeout_s"`
}

// scriptResult implements ScriptRunResult.schema.json: exactly one outcome,
// required, not a list.
type scriptResult struct {
	Outcome installOutcome `json:"outcome"`
}

const (
	defaultTimeoutS = 3600
	// maxTimeoutS bounds what a job may ask for. A day is far beyond any
	// legitimate script and well beyond any job TTL the control plane issues;
	// the real bound is the job's own expires_at, which is applied on top of
	// this. The point of a second bound is that a control plane that started
	// sending `timeout_s: 2147483647` should be refused by the endpoint rather
	// than believed.
	maxTimeoutS = 24 * 3600
	// maxScriptBody bounds a body. Scripts are source; a megabyte of it is a
	// mistake or an attempt to make this agent allocate.
	maxScriptBody = 1 << 20
)

var interpreters = map[string]struct{}{
	"powershell": {}, "bash": {}, "python": {},
}

func (s *Service) runScript(ctx context.Context, raw json.RawMessage) (*scriptResult, error) {
	p, err := parseScriptParams(raw)
	if err != nil {
		return nil, err
	}

	// The identifier is what the operator sees next to the result. `script_id`
	// and `script_version` are the two things this agent knows about the
	// script — it is never told the NAME — so the identifier is built from
	// them, sanitised and clamped like any echoed string.
	identifier := clampIdentifier(p.ScriptID + "@v" + itoa(p.ScriptVersion))

	// One private, restricted directory per job, removed before this function
	// returns. See scratch.go for why the DIRECTORY's permissions matter more
	// than the file's.
	sc, err := newScratch(s.opts.ScratchRoot)
	if err != nil {
		return nil, err
	}
	defer func() {
		if cerr := sc.Close(); cerr != nil {
			// The path is NOT logged, and neither is anything about the body.
			s.logger().Warn("script.run: the job's scratch directory could not be removed",
				"script_id", identifier)
		}
	}()

	// The body is written to a FILE. Never -EncodedCommand, never -Command,
	// never `bash -c` — see plan.go's powershellArgs and planBash for why.
	scriptPath, err := sc.writeFile(scriptFileName(p.Interpreter), []byte(p.Body))
	if err != nil {
		return nil, err
	}

	cmd, err := planScript(s.goos(), p.Interpreter, scriptPath, s.lookPath())
	if err != nil {
		return nil, err
	}
	cmd.Dir = sc.dir

	runCtx, cancel := context.WithTimeout(ctx, p.timeout())
	defer cancel()

	// NOTHING ABOUT THE BODY OR ITS ARGUMENTS IS IN THIS RECORD. The script id,
	// its version, the interpreter and the program that will run it are the
	// agent's own facts; the body is the operator's secret.
	s.logger().Info("script.run: executing a verified script",
		"script_id", identifier, "interpreter", p.Interpreter, "program", cmd.Label)

	out, err := s.executor()(runCtx, cmd)
	if err != nil {
		// The process could not be STARTED. That is a failure of this device,
		// not a refusal: the interpreter resolved a moment ago and then would
		// not run, which is the kind of thing that fixes itself.
		return nil, err
	}

	return &scriptResult{Outcome: scriptOutcome(identifier, out)}, nil
}

// scriptOutcome maps a finished process onto InstallOutcome.
//
// `succeeded` and `failed` are script.run's whole vocabulary
// (app/modules/deploy/scripts.py::_SCRIPT_OUTCOME_STATES). Emitting
// `installed` or `not_applicable` here would be coerced to `failed` server-
// side with the original recorded as an anomaly, so a successful script would
// read as a failed one.
//
// EXIT CODE 0 IS THE ONLY SUCCESS. Not "0 or a code the script documented":
// this agent has no way to know a script's conventions, and treating a
// non-zero exit as success is how a failed remediation reports green.
func scriptOutcome(identifier string, out Outcome) installOutcome {
	code := out.ExitCode
	state := stateFailed
	if code == 0 && !out.TimedOut {
		state = stateSucceeded
	}
	log := wireLog(out)
	if out.TimedOut {
		// Prepended by the AGENT, in the agent's own words, so an operator
		// looking at exit code 0 and state `failed` is not left guessing. A
		// killed process often exits 0-ish on Windows.
		log = "[the job's deadline passed and the process was killed]\n" + log
	}
	return installOutcome{
		Identifier:    identifier,
		State:         state,
		ExitCode:      &code,
		Log:           log,
		LogRef:        nil, // there is no agent-facing artifact-upload route
		DetectedAfter: nil, // a script has no detection rule
	}
}

// parseScriptParams validates everything before anything is written to disk or
// executed, and REFUSES rather than failing on every violation.
//
// Refusal is right for this class for the reason internal/jobs' Refusal
// documents: a params document this agent will not act on is not transient. It
// is also why NOTHING JOB-SUPPLIED IS ECHOED into these messages — the reason
// lands in the server's hash-chained audit log, and the body is the single
// most credential-bearing string the control plane sends.
func parseScriptParams(raw json.RawMessage) (scriptParams, error) {
	var p scriptParams
	if err := json.Unmarshal(raw, &p); err != nil {
		return p, jobs.Refuse("script.run: params are not a ScriptRunParams; nothing was executed")
	}
	if _, ok := interpreters[p.Interpreter]; !ok {
		// The VALUE is not named. It is a job-supplied string.
		return p, jobs.Refuse("script.run: interpreter is not one of powershell, bash, python; " +
			"nothing was executed")
	}
	if p.Body == "" {
		return p, jobs.Refuse("script.run: the script body is empty; nothing was executed")
	}
	if len(p.Body) > maxScriptBody {
		return p, jobs.Refuse("script.run: the script body is larger than %d bytes; nothing was "+
			"executed", maxScriptBody)
	}
	if p.ScriptID == "" {
		return p, jobs.Refuse("script.run: script_id is empty, so the run could not be attributed " +
			"to a script; nothing was executed")
	}
	if !validPackageID(p.ScriptID) {
		// Same allowlist as a package identifier. script_id is a UUID string
		// from the server and passes trivially; anything that does not is
		// either not a script id or is trying to be something else, and it is
		// about to be echoed into an operator's console as the identifier.
		return p, jobs.Refuse("script.run: script_id is not a plain identifier; nothing was " +
			"executed")
	}
	if p.ScriptVersion < 0 {
		return p, jobs.Refuse("script.run: script_version is negative; nothing was executed")
	}
	if t := p.timeoutSeconds(); t <= 0 || t > maxTimeoutS {
		return p, jobs.Refuse("script.run: timeout_s is outside 1..%d; nothing was executed",
			maxTimeoutS)
	}
	return p, nil
}

func (p scriptParams) timeoutSeconds() int {
	if p.TimeoutS == nil {
		return defaultTimeoutS
	}
	return *p.TimeoutS
}

func (p scriptParams) timeout() time.Duration {
	return time.Duration(p.timeoutSeconds()) * time.Second
}

// itoa avoids pulling strconv in for one call site and keeps the allocation
// profile of the job path visible in one place.
func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}
