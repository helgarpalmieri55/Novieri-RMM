// Package provision implements the `provision.enroll` capability
// (docs/modules/provisioning/spec.md §9, §15).
//
// # This capability VERIFIES a baseline. It does not enroll, and it does not
// # re-enroll.
//
// The job type is named for the workflow step it closes, not for what it does:
// by the time a signed `provision.enroll` job can arrive, THIS AGENT IS ALREADY
// ENROLLED. That is not an assumption, it is the only way the job got here. The
// job travelled the mTLS gateway connection, which needs the certificate
// enrollment issued; it was verified against the Ed25519 key pinned at
// enrollment; and the control plane only builds it after
// `bind_provisioned_device` has already advanced the run to `enrolled`
// (app/modules/provisioning/enroll.py). Enrollment is the PRECONDITION of this
// job, never its work.
//
// So this package contains no enrollment path, and that absence is enforced
// rather than promised. `no_second_enrollment_test.go` walks this package's own
// source and fails on any call to `enroll.Enroll`, `enroll.LoadOrEnroll`,
// `enroll.GenerateCSR`, `enroll.Install`, `enroll.RevertToPrevious` or any
// HTTP-client construction. An agent that could be made to enroll a second
// identity by a job is an agent whose device identity is not an identity: the
// control plane would hold two devices for one machine, each with its own
// certificate, and revoking one would leave the other commanding the endpoint.
// The only enroll symbols this package may use are the READ-ONLY ones --
// `enroll.Load` and `enroll.ExpiryState` -- which is how `agent_healthy` is
// answered.
//
// # Invariant #3: there are no secrets in these params, by construction
//
// `docs/modules/provisioning/spec.md` §7 is the crux of that module: domain-join
// and local-admin credentials are vault-sealed and decrypted only inside the
// answer-file renderer. The credential path to the endpoint is the RENDERED
// ANSWER FILE consumed during imaging, and it is deleted when the run reaches a
// terminal state (§7, §13) -- that is the "short-lived rendered answer file"
// lifetime, and it is entirely upstream of this job.
//
// `dispatch_provision_enroll` strips every sealed credential field from
// `post_config` before signing (`_non_secret_post_config`, keyed by
// `repository._CREDENTIAL_PATHS`), so `ProvisionEnrollParams.post_config`
// carries no plaintext credential and no `{"__sealed__": ...}` reference. This
// package therefore has no secret to protect, and it keeps it that way: it
// reads exactly two members out of `post_config` (`hostname`, and whether
// `domain_join` is present with its `domain`), never logs `post_config`, never
// writes it anywhere, and holds it only for the duration of the job.
// `secrets_test.go` asserts that against a params document with credentials
// planted in it -- because "the server strips them" is a property of the
// server, and an agent that logged whatever arrived would leak the day that
// stopped being true.
//
// # Verification, not application (a deliberate narrowing, recorded in §9.1)
//
// Spec §5 step 6 says "verify/apply post-config"; §9 is titled "Post-config
// verification" and its result fields are all past-tense observations
// (`hostname_set`, `domain_joined`, `agent_healthy`). This package implements
// the VERIFY half only, and docs/modules/provisioning/spec.md §9.1 records why:
// renaming or domain-joining a machine from here would mean spawning
// privileged, reboot-requiring, trust-breaking commands (`Rename-Computer`,
// `Add-Computer`, `dsconfigad`) whose only authority is a params document --
// and doing it AFTER the answer file already did it correctly, on a machine
// that is by then domain-joined, where a rename breaks the machine account's
// trust relationship. The answer file is where post-config is applied; this job
// is where the control plane finds out whether it worked.
//
// # Processes spawned: NONE
//
// On both fleet platforms this capability spawns nothing at all, which matters
// because a SYSTEM-privileged process spawning `powershell.exe` is one of the
// loudest signals an EDR has:
//
//   - Windows: `os.Hostname` (GetComputerNameEx) and
//     `netapi32.dll!NetGetJoinInformation` -- two library calls.
//   - macOS: `os.Hostname` and one `os.Stat` of the Active Directory
//     OpenDirectory configuration plist.
//
// # Dormancy
//
// Registration is a map insert. `Process` is the zero value of Service; it
// resolves the host seam and the agent version at JOB TIME. Nothing here opens
// a file, dials anything, starts a goroutine or takes a timer-wheel entry until
// a verified job arrives. budget_test.go measures that rather than describing
// it.
package provision

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/toeshee/it-suite/agent/internal/config"
	"github.com/toeshee/it-suite/agent/internal/enroll"
	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// JobType matches app/modules/provisioning/handler.py's registration. A
// mismatch would be refused `unknown_type` by this agent's own runner -- the
// correct answer, and a confusing one, so the string lives in one place per
// side.
const JobType = "provision.enroll"

// Result states. `ProvisionEnrollResult.state` is Literal["success","failed"]
// server-side and the result model is extra="forbid", so a third value would
// raise ProvisioningValidationError in handle_provision_result and fail the
// whole job.
const (
	stateSuccess = "success"
	stateFailed  = "failed"
)

// unstampedVersion is main.go's own default for an unstamped build
// (`var version = "dev"`, set with -ldflags at release time). Duplicated here
// so a Service built without Options.Version compares against the same string
// the binary would print for `--version`, rather than against "".
const unstampedVersion = "dev"

// params implements ProvisionEnrollParams.schema.json.
//
// Unknown members are ignored (encoding/json's default): the inbound half of
// docs/kernel/wire-protocol/README.md's rule, so a strictly additive server
// change does not take a deployed fleet offline.
//
// ExpectedAgentVersion is a POINTER because its schema default is null and
// "the control plane did not pin a version" is a different statement from "the
// control plane pinned the empty string". Collapsing them would turn every job
// from a control plane that omits the member into a version-mismatch failure.
//
// PostConfig is map[string]json.RawMessage rather than map[string]any on
// purpose: this package reads exactly two members out of it and must never
// walk, re-encode or log the rest. Leaving the remainder as unparsed bytes is
// the cheapest way to make that structural -- see secrets_test.go.
type params struct {
	ProvisionRunID       string                     `json:"provision_run_id"`
	OSFamily             string                     `json:"os_family"`
	Hostname             string                     `json:"hostname"`
	PostConfig           map[string]json.RawMessage `json:"post_config"`
	ExpectedAgentVersion *string                    `json:"expected_agent_version"`
}

// result implements ProvisionEnrollResult.schema.json.
//
// DomainJoined and Error are pointers because their schema default is null and
// null is a real answer here: `domain_joined: null` means "this profile asked
// for no domain join, so there is nothing to confirm", which is not the same
// statement as `false` ("a join was asked for and this machine is not joined").
// `ingest_provision_result` writes both straight into the audit metadata, so
// the difference is what an auditor reads a year later.
//
// NO omitempty anywhere: the server model is extra="forbid" but its optional
// members default to null, and emitting the explicit null is exactly the
// default. A dropped key and a null key must not be able to diverge.
type result struct {
	State        string  `json:"state"`
	HostnameSet  bool    `json:"hostname_set"`
	DomainJoined *bool   `json:"domain_joined"`
	AgentHealthy bool    `json:"agent_healthy"`
	Error        *string `json:"error"`
}

// DomainStatus is what the OS says about directory membership.
//
// Supported=false means "this platform has no answer I trust", which is a
// different thing from "not joined" and must never be reported as `false`: a
// run whose profile asked for a domain join would be marked failed on a
// platform that simply cannot be asked.
type DomainStatus struct {
	Supported bool
	Joined    bool
	Domain    string
}

// Host is the OS seam, and the reason every branch below is unit-tested on a
// Linux container that is neither of the two fleet platforms.
//
// Both implementations are library calls; neither spawns a process. See the
// package comment.
type Host interface {
	// Hostname is the machine's own name as the OS reports it.
	Hostname() (string, error)
	// DomainStatus reports directory membership.
	DomainStatus() (DomainStatus, error)
}

// Options wires the capability. Every field has a production default; they
// exist so the comparison logic is testable without a Windows domain
// controller.
type Options struct {
	// Host is the OS seam. Defaults to this platform's implementation.
	Host Host
	// Health answers `agent_healthy`. Defaults to identityHealth: the persisted
	// identity loads and its certificate is currently valid.
	Health func(now time.Time) (bool, string)
	// Version is this build's version string, compared against the params'
	// `expected_agent_version`. Defaults to "dev", which is main.go's own value
	// for an unstamped build.
	Version string
	// Now is the clock the certificate validity window is read against.
	Now    func() time.Time
	Logger *slog.Logger
}

// Service is the process-wide holder. The zero value is usable and is what
// `Process` is: it resolves its own defaults at JOB TIME, so registration stays
// a map insert and nothing here touches the OS while the agent is idle.
type Service struct {
	// mu guards opts. Bind writes it once from main before the transport is
	// running, and every job reads it -- but "before" is a claim about start-up
	// ordering, and the race detector is not interested in claims.
	mu   sync.RWMutex
	opts Options
	// runs counts completed verifications, so a dormancy assertion outside this
	// package can observe that this capability has NOT run. A single atomic
	// load.
	runs atomic.Int64
}

// Process is the Service the default capability registry registers.
var Process = &Service{}

// New returns a Service with explicit wiring. Tests use it; production uses
// Process plus Bind.
func New(o Options) *Service { return &Service{opts: o} }

// Bind wires the capability, in the shape `logs` and `health` already use.
//
// It exists for ONE field that cannot be defaulted correctly: `Options.Version`
// is compared against the params' `expected_agent_version`, and its default is
// "dev" -- which is right for an unstamped developer build and WRONG for every
// released binary, because `agent/packaging/linux/build-deb.sh` stamps
// `main.version` via `-ldflags`. Without this, a stamped 1.2.0 agent would
// report "dev" and fail every baseline version check, succeeding as a job and
// reporting a false negative, which is the worst of the available outcomes.
//
// Binding opens nothing and starts nothing: this capability reads the OS only
// inside a verified job, and that is unchanged.
func (s *Service) Bind(o Options) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.opts = o
}

func (s *Service) options() Options {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.opts
}

// Runs reports how many verifications this process has completed.
func (s *Service) Runs() int64 { return s.runs.Load() }

func (s *Service) logger() *slog.Logger {
	if o := s.options(); o.Logger != nil {
		return o.Logger
	}
	return slog.Default()
}

func (s *Service) host() Host {
	if o := s.options(); o.Host != nil {
		return o.Host
	}
	return osHost{}
}

func (s *Service) now() time.Time {
	if o := s.options(); o.Now != nil {
		return o.Now()
	}
	return time.Now()
}

func (s *Service) version() string {
	if o := s.options(); o.Version != "" {
		return o.Version
	}
	return unstampedVersion
}

func (s *Service) health() func(time.Time) (bool, string) {
	if o := s.options(); o.Health != nil {
		return o.Health
	}
	return identityHealth
}

// Enroll is the `provision.enroll` capability.
//
// It takes a jobs.VerifiedJob and nothing else (invariant #1): the runner has
// already checked the Ed25519 signature over the canonical bytes against the
// key pinned at enrollment, that the job targets THIS device, that it is fresh,
// that its nonce is unseen, and that the type is registered. There is no second
// path into this function, and -- see the package comment -- this function
// opens no path into enrollment.
func (s *Service) Enroll(_ context.Context, job jobs.VerifiedJob) (any, error) {
	res, err := s.run(job.Params())
	if err != nil {
		return nil, err
	}
	return res, nil
}

// run is the whole capability, minus the VerifiedJob wrapper so tests and the
// budget harness can drive it with raw params.
//
// # Why "the baseline was not achieved" is a SUCCEEDED job with state="failed"
//
// This is the single most consequential decision in this file, and it is not
// stylistic. `handle_provision_result` is registered as the job type's
// RESULT HANDLER, and app/jobs/registry.py's contract is explicit: a result
// handler is "invoked by JobEngine.complete() after a SUCCEEDED result". A job
// that fails or is refused therefore never reaches `ingest_provision_result`,
// and the `ProvisionRun` stays on `enrolled` forever -- no `complete`, no
// `failed`, no `finished_at`, no `error` for the operator to read, and the
// answer-file cleanup that keys off a terminal state never runs, so a
// secret-bearing artifact lingers.
//
// So a machine that came up without its hostname is a SUCCEEDED job carrying
// `state: "failed"`. The job succeeded -- the verification ran and produced a
// truthful answer -- and the RUN failed. `ingest_provision_result` coerces
// exactly that into `run.state = "failed"` with the error text, which is the
// designed terminal path.
//
// `Refuse` is kept for the one case that is not a baseline miss but a
// mis-addressed job: params that are not a ProvisionEnrollParams at all, or an
// `os_family` that is not this machine's. Those say "this agent will not answer
// this question", and leaving the run visibly stuck on `enrolled` is the
// correct outcome for a run that bound the wrong device -- marking it `failed`
// would file a binding bug as a provisioning outcome.
func (s *Service) run(raw json.RawMessage) (*result, error) {
	p, err := parseParams(raw)
	if err != nil {
		return nil, err
	}

	// A run that bound the wrong device. Refused, not failed: see above.
	if want, have := p.OSFamily, thisOSFamily(); want != have {
		return nil, jobs.Refuse("provision.enroll: this job's os_family is %q and this endpoint "+
			"is %q. Nothing was verified: a post-config check against the wrong OS would produce "+
			"a confident, wrong answer, and this run has bound a device it did not image",
			sanitizeFamily(want), have)
	}

	var reasons []string

	// ---- hostname -------------------------------------------------------
	hostnameSet := true
	if p.Hostname != "" {
		actual, herr := s.host().Hostname()
		switch {
		case herr != nil:
			hostnameSet = false
			reasons = append(reasons, "the OS did not report a hostname")
		case !hostnameMatches(p.Hostname, actual):
			hostnameSet = false
			// The EXPECTED name is the control plane's own string and the ACTUAL
			// is this machine's; neither is a credential, and the operator
			// cannot fix this without both.
			reasons = append(reasons, fmt.Sprintf("hostname is %q, expected %q", actual, p.Hostname))
		}
	}
	// p.Hostname == "" is the profile asking for no rename. There is no unmet
	// requirement, so hostname_set stays true: reporting false for a
	// requirement nobody made would fail every run of every profile that does
	// not rename its machines.

	// ---- domain join ----------------------------------------------------
	var domainJoined *bool
	if want, requested := requestedDomain(p.PostConfig); requested {
		st, derr := s.host().DomainStatus()
		switch {
		case derr != nil || !st.Supported:
			// NOT reported as `false`. "I cannot be asked" is not "not joined",
			// and the difference is what an auditor reads in the audit metadata
			// a year later.
			reasons = append(reasons, "this endpoint cannot report directory membership")
		default:
			joined := st.Joined && (want == "" || domainMatches(want, st.Domain))
			domainJoined = &joined
			if !joined {
				reasons = append(reasons, fmt.Sprintf("domain membership is %q, expected %q",
					st.Domain, want))
			}
		}
	}

	// ---- agent health ---------------------------------------------------
	healthy, why := s.health()(s.now())
	if !healthy {
		reasons = append(reasons, why)
	}

	// ---- the pinned version ---------------------------------------------
	if p.ExpectedAgentVersion != nil && *p.ExpectedAgentVersion != "" {
		if got := s.version(); got != *p.ExpectedAgentVersion {
			reasons = append(reasons, fmt.Sprintf("agent version is %q, expected %q",
				got, sanitizeVersion(*p.ExpectedAgentVersion)))
		}
	}

	res := &result{
		State:        stateSuccess,
		HostnameSet:  hostnameSet,
		DomainJoined: domainJoined,
		AgentHealthy: healthy,
	}
	if len(reasons) > 0 {
		res.State = stateFailed
		msg := strings.Join(reasons, "; ")
		res.Error = &msg
	}

	// COUNTS AND STATE ONLY. `post_config` is never logged, whatever it turned
	// out to contain -- see the package comment on invariant #3.
	s.logger().Info("provision.enroll verified the post-config baseline",
		"state", res.State, "hostname_set", res.HostnameSet, "agent_healthy", res.AgentHealthy)
	s.runs.Add(1)
	return res, nil
}

// parseParams refuses a document that is not a ProvisionEnrollParams.
//
// Refused and not failed: a params document this agent cannot read is not a
// transient condition. The reason is the agent's own sentence -- no
// job-supplied string is formatted into it.
func parseParams(raw json.RawMessage) (params, error) {
	var p params
	if err := json.Unmarshal(raw, &p); err != nil {
		return p, jobs.Refuse("provision.enroll: params are not a ProvisionEnrollParams")
	}
	if p.OSFamily == "" {
		return p, jobs.Refuse("provision.enroll: params carry no os_family, which the exported " +
			"schema requires; nothing was verified")
	}
	return p, nil
}

// hostnameMatches compares the control plane's requested name against what the
// OS reports.
//
// Case-insensitive on the FIRST LABEL of each, because the two fleet platforms
// spell the same name differently and neither spelling is wrong:
//
//   - Windows returns the NetBIOS computer name, conventionally uppercase
//     ("WS-0142") whatever case the profile used.
//   - macOS returns whatever HostName/LocalHostName resolves to, frequently the
//     Bonjour form ("ws-0142.local") and on a bound Mac the AD FQDN
//     ("ws-0142.corp.toeshee.local").
//
// Comparing the full strings would mark a correctly provisioned Mac as failed
// on every run. Comparing the first label case-insensitively is the strictest
// rule that is true on both.
func hostnameMatches(want, actual string) bool {
	return strings.EqualFold(firstLabel(want), firstLabel(actual))
}

func firstLabel(s string) string {
	s = strings.TrimSpace(s)
	if i := strings.IndexByte(s, '.'); i >= 0 {
		return s[:i]
	}
	return s
}

// domainMatches compares the requested domain against the reported one,
// case-insensitively and tolerating the NetBIOS/DNS split: a profile that asks
// for "TOESHEE" is satisfied by "toeshee.local", and one that asks for
// "toeshee.local" is satisfied by "TOESHEE".
//
// NetGetJoinInformation returns the NetBIOS name on a domain-joined Windows
// machine while `post_config.domain_join.domain` is conventionally written in
// DNS form. Requiring string equality would report every correctly joined
// Windows machine in the fleet as not joined.
func domainMatches(want, actual string) bool {
	if want == "" || actual == "" {
		return false
	}
	return strings.EqualFold(firstLabel(want), firstLabel(actual))
}

// requestedDomain reports whether `post_config` asked for a domain join, and
// which domain.
//
// This and `hostname` are the ONLY members of post_config this package reads.
// The value is decoded out of the raw bytes of the `domain_join` member alone;
// the rest of the document is never unmarshalled, walked or re-encoded.
//
// Note what is NOT read: `domain_join.password` and `local_admin.password`.
// `_non_secret_post_config` strips both server-side before signing, and this
// function would not look at them if it did not.
func requestedDomain(post map[string]json.RawMessage) (string, bool) {
	rawJoin, ok := post["domain_join"]
	if !ok {
		return "", false
	}
	var join struct {
		Domain string `json:"domain"`
	}
	if err := json.Unmarshal(rawJoin, &join); err != nil {
		// A `domain_join` member that is not an object still means a join was
		// asked for; the domain is simply unstated, and DomainStatus.Joined
		// alone then decides.
		return "", true
	}
	return strings.TrimSpace(join.Domain), true
}

// identityHealth is the production answer to `agent_healthy`, and it is
// deliberately narrow.
//
// It uses the two READ-ONLY entry points of internal/enroll -- Load and
// ExpiryState -- and nothing else. It does not renew, does not rotate, does not
// generate a key and does not contact the control plane. Reaching this function
// already proves a great deal (a signed job arrived over a mutually
// authenticated connection and verified against the pinned key); what it adds
// is the one thing that proof does not cover: that the identity on disk is
// loadable and its certificate has not expired, so the agent will still be here
// tomorrow. A provisioning run that reported healthy over a certificate with
// two days left would be reporting the wrong thing.
func identityHealth(now time.Time) (bool, string) {
	cfg, err := config.Load()
	if err != nil {
		return false, "the agent's configuration could not be resolved"
	}
	id, err := enroll.Load(cfg)
	if err != nil {
		return false, "the agent's persisted identity could not be loaded"
	}
	_, state := enroll.ExpiryState(now, id)
	if state == enroll.StateExpired {
		return false, "the agent's certificate has expired"
	}
	return true, ""
}

// sanitizeFamily and sanitizeVersion bound a job-supplied string before it
// reaches a refusal reason or a result error, both of which travel into the
// server's hash-chained audit log.
//
// The server sanitises refusal reasons too, and these fields are already
// constrained by the exported schema -- but "the other side checks it" is how
// an unbounded string ends up in an audit record.
func sanitizeFamily(s string) string { return clamp(s, 16) }

func sanitizeVersion(s string) string { return clamp(s, 40) }

func clamp(s string, n int) string {
	s = strings.Map(func(r rune) rune {
		if r < 0x20 || r == 0x7f {
			return -1
		}
		return r
	}, s)
	if len(s) > n {
		return s[:n]
	}
	return s
}
