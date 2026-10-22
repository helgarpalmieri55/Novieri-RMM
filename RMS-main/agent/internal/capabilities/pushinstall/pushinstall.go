// Package pushinstall implements the `agent.push_probe` and
// `agent.push_install` capabilities
// (docs/modules/agent-distribution/spec-push-install.md §4, §7, §11).
//
// # This is the most dangerous code in the repository, and it is written that way
//
// A push authenticates to a machine NOBODY HAS ENROLLED, using a privileged
// credential, and makes it run this platform's agent. Mechanism for mechanism
// that is lateral movement (spec §1). It exists because onboarding hundreds of
// endpoints by hand is a real problem, and it is written so its worst case is
// small, stated, and bounded by something other than care at the moment of the
// click.
//
// # Invariant #1: an ordinary signed job, verified before this runs
//
// Both handlers take a jobs.VerifiedJob and nothing else. The runner has already
// checked the Ed25519 signature over the canonical bytes against the key pinned
// at enrollment, the target device, freshness and nonce, and that the type is
// registered — before either function is entered. There is no second path in.
//
// # Invariant #2 / 2a(a): the agent connects OUTBOUND, the endpoint never decides
//
// The site server dials the target; the control plane never does. The ONE
// address this capability may reach is `target_address` in the SIGNED params,
// refused if the params somehow name a different one (falsifier 4). There is no
// discovery, no fallback address, no retry against a neighbour — exactly
// `site.wake`'s rule ("the endpoint never decides what it may touch").
//
// # The host key is the crux (spec §7.3)
//
// On first contact there is no way to authenticate the target. So a push is two
// jobs, not one: `agent.push_probe` fetches the host-key fingerprint carrying NO
// CREDENTIAL, the operator confirms it, and the confirmed key is signed into the
// install. THIS CAPABILITY NEVER ACCEPTS A HOST KEY BLINDLY for an install and
// NEVER DOES TRUST-ON-FIRST-USE: `Open` verifies the live key against the signed
// `host_key_fingerprint` inside the handshake, before any secret is sent, and
// aborts on any mismatch. The probe, which presents no secret, is the only path
// that learns an unknown key.
//
// # The artefact is verified before a byte reaches the target (falsifier 6)
//
// The release URL is presigned by the server; the agent downloads it and checks
// its SHA-256 against the signed digest BEFORE transferring anything.
//
// # The credential (invariant #3, falsifiers 3, 14)
//
// The secret is held in a []byte ZEROED before the result is sent, never placed
// in argv on either machine, and in no log line at any level. Go strings cannot
// be zeroed, so the copy inside the signed params (redacted on the server's API
// and scrubbed from its `jobs.params` on terminal, §6.4) is the residual; this
// capability adds no further copy that outlives the job.
//
// # Dormancy (spec §11 idle budget)
//
// The zero value of Service is usable and is what `Process` is. It resolves its
// wiring at JOB TIME: nothing here opens a socket, a file or a goroutine while
// the agent is idle, and budget_test.go measures that rather than asserting it.
package pushinstall

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync/atomic"

	"github.com/toeshee/it-suite/agent/internal/install/layout"
	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// JobTypeProbe and JobTypeInstall match app/modules/agent_push/jobtypes.py.
const (
	JobTypeProbe   = "agent.push_probe"
	JobTypeInstall = "agent.push_install"
)

// CapabilityWinRM is ADVERTISED, never dispatched: it tells the control plane
// this build's push capabilities speak the Kerberos-only WinRM transport (plan
// 2026-09-28-windows-push-winrm Task 4B, §I6). The server's `_site_server_for`
// reads it from `agents.capabilities` to choose a WinRM-capable originator for
// a Windows target. It is not a job type: a job claiming it is `unknown_type`.
const CapabilityWinRM = "agent.push_install.winrm"

// CapabilityEnrollURL is the advertise-only marker that this build's SSH push
// passes the signed enroll_url to the placed binary's install mode (review
// Minor 1). The server's `_site_server_for` requires it of an SSH originator.
const CapabilityEnrollURL = "agent.push_install.enroll_url"

// stagedArtefactPath and tokenFilePath are where the transferred artefact and
// the single-use enrolment token are placed on the target. FIXED and compiled in
// — neither is ever taken from the job params, which carry no path, no command
// and no argument (spec §4.2, clause (b)). The token goes to a 0600 file and
// NEVER into argv (packaging/README.md, falsifier 14). They are the placed
// binary's install mode's paths too (internal/install/layout).
const (
	stagedArtefactPath = layout.UnixStagedBinary
	tokenFilePath      = layout.UnixStagedToken
	tokenFileMode      = 0o600
	artefactFileMode   = 0o755
)

// PrepareStagingCommand makes a FRESH root-only staging directory for the two
// files (scp does not create directories). A constant, like `uname -s -m`: it
// names a compiled-in path and nothing from the params.
//
// Review I3: whatever is at the path first is removed -- a directory someone
// else created, or a symlink, which `rm -rf` removes without following (a
// symlink operand without a trailing slash; verified with GNU coreutils, and
// POSIX rm does not traverse symlinks) -- and the directory is made with
// `mkdir -m 0700` and NO -p, so a directory re-created between the two by
// anyone else is a failure rather than something to write into. The placed
// binary's install mode checks it again (root-owned, 0700, not a symlink)
// before reading anything in it. Exported for the Linux e2e.
const PrepareStagingCommand = "rm -rf " + layout.UnixPushStagingDir + " && mkdir -m 0700 " + layout.UnixPushStagingDir

// CleanupStagingCommand removes the two staged files after a FAILED install,
// best effort (review Minor 6): a failure before the install mode ran -- or
// one inside it that it could not clean up after -- must not leave the
// single-use token or the artefact lying in the staging directory. A
// constant, like the others. A successful install mode deletes both itself.
const CleanupStagingCommand = "rm -f " + layout.UnixStagedToken + " " + layout.UnixStagedBinary

// ErrHostKey and ErrAuth let a Dialer's Open distinguish the two first-contact
// failures the install result reports differently (§7.3): a host-key mismatch is
// "the target is not who we pinned", an auth failure is "the credential did not
// work". A Dialer wraps whichever occurred; the Service classifies with
// errors.Is.
var (
	ErrHostKey = errors.New("pushinstall: host-key verification failed")
	ErrAuth    = errors.New("pushinstall: authentication failed")
)

// probeParams implements AgentPushProbeParams.schema.json. Unknown members are
// ignored (encoding/json default). **THERE IS NO CREDENTIAL FIELD, AND THERE MAY
// NEVER BE ONE** (falsifier 22).
type probeParams struct {
	TargetAddress string `json:"target_address"`
	Transport     string `json:"transport"`
	// DNSName is required for winrm (Kerberos needs HTTP/<fqdn>); unused for
	// ssh. A name, not a credential: the winrm probe is credential-free too.
	DNSName string `json:"dns_name"`
}

// installParams implements AgentPushInstallParams.schema.json.
type installParams struct {
	TargetAddress      string     `json:"target_address"`
	ExpectedMAC        string     `json:"expected_mac"`
	Transport          string     `json:"transport"`
	DNSName            string     `json:"dns_name"`
	OS                 string     `json:"os"`
	Arch               string     `json:"arch"`
	Release            release    `json:"release"`
	EnrollToken        string     `json:"enroll_token"`
	Credential         credential `json:"credential"`
	HostKeyFingerprint string     `json:"host_key_fingerprint"`
	// EnrollURL is the console's bare https origin from the server's
	// configuration (ITS_PUBLIC_CONSOLE_URL), set on winrm installs. The
	// winrm path enrolls against it and NEVER against the presigned release
	// URL's origin (plan W2, superseded 2026-09-29).
	EnrollURL string `json:"enroll_url"`
}

type release struct {
	Version string `json:"version"`
	SHA256  string `json:"sha256"`
	URL     string `json:"url"`
}

type credential struct {
	Username string `json:"username"`
	Secret   string `json:"secret"`
}

// probeResult implements AgentPushProbeResult.schema.json. The three winrm
// readiness booleans are omitempty so an ssh probe result is byte-for-byte
// what an older control plane has always received (the wire is a contract).
type probeResult struct {
	TargetAddress      string `json:"target_address"`
	Transport          string `json:"transport"`
	HostKeyFingerprint string `json:"host_key_fingerprint"`
	HostKeyType        string `json:"host_key_type"`
	Banner             string `json:"banner"`
	WinRMHTTPS         bool   `json:"winrm_https,omitempty"`
	WinRMHTTP          bool   `json:"winrm_http,omitempty"`
	KDCReachable       bool   `json:"kdc_reachable,omitempty"`
}

type installResult struct {
	TargetAddress string `json:"target_address"`
	Outcome       string `json:"outcome"`
	OSDetected    string `json:"os_detected"`
	ArchDetected  string `json:"arch_detected"`
	Detail        string `json:"detail"`
}

// Install outcome vocabulary, matching AgentPushInstallResult.schema.json's enum.
const (
	outcomeInstalled  = "installed"
	outcomeOSMismatch = "os_mismatch"
	outcomeUnreach    = "unreachable"
	outcomeAuthFailed = "auth_failed"
	outcomeFailed     = "failed"
)

// ProbeInfo is what a credential-free probe learns from the handshake.
type ProbeInfo struct {
	Fingerprint string
	KeyType     string
	Banner      string
}

// Session is an authenticated connection to the target. It never outlives the
// job (falsifier 2: no socket between jobs).
type Session interface {
	// Run executes a FIXED, compiled-in command (uname, the artefact's own
	// installer) — never anything from the job params.
	Run(ctx context.Context, cmd string) (string, error)
	// Put writes bytes to a path with a mode. The token goes to a 0600 file this
	// way, never through argv.
	Put(ctx context.Context, path string, data []byte, mode uint32) error
	Close() error
}

// Dialer is the transport seam.
//
// Probe opens a connection, reads the host key WITHOUT authenticating, and
// closes — safe on an unknown key because it presents no secret.
//
// Open verifies the host key against expectFingerprint AND authenticates IN THE
// SAME HANDSHAKE. That single handshake is what makes the verification sound:
// the connection the credential is presented on is the one whose key was
// checked, with no window between. A non-empty expectFingerprint that does not
// match is `ErrHostKey`, before the password is sent.
type Dialer interface {
	Probe(ctx context.Context, addr string) (ProbeInfo, error)
	Open(ctx context.Context, addr, expectFingerprint, username string, secret []byte) (Session, error)
}

// WinRMReadiness is what a credential-free winrm probe learns (design §2.4).
type WinRMReadiness struct {
	HTTPS        bool
	HTTP         bool
	KDCReachable bool
}

// WinRMCredential is the decrypted windows_domain account, unpacked from the
// signed params' credential (the secret is a JSON blob for winrm, plan Task 7).
// Password is a byte slice so the capability can zero its copy.
type WinRMCredential struct {
	Username string
	Password []byte
	KDC      string
	CABundle string
}

// WinRMDialer is the winrm transport seam, twin to Dialer.
//
// **Probe carries NO credential** (§C1/W3): it TCP-dials 5986/5985 at the
// signed address, derives the realm from the DNS name and checks KDC
// reachability by SRV + a plain TCP dial -- no TGT, no password.
//
// Run authenticates with Kerberos (the credential; NTLM, Basic and CredSSP do
// not exist in the client) to the signed ADDRESS, verifying the target by its
// HTTP/<fqdn> ticket, runs the fixed script, and feeds stdin (the enrolment
// token) to it. Resolved at job time; the idle agent holds neither.
type WinRMDialer interface {
	Probe(ctx context.Context, fqdn, addr string) (WinRMReadiness, error)
	Run(ctx context.Context, fqdn, addr string, cred WinRMCredential, script string, stdin []byte) (stdout string, exitCode int, err error)
}

// Downloader fetches the agent artefact from the presigned URL — outbound from
// the site server to the object store, allowed (2a clause (a)); NOT a connection
// to the target.
type Downloader interface {
	Download(ctx context.Context, url string) ([]byte, error)
}

// Options wires the capability; defaults resolved at job time so the zero-value
// Service is dormant.
type Options struct {
	Dialer      Dialer
	WinRMDialer WinRMDialer
	Downloader  Downloader
}

// Service is the process-wide holder. Zero value is usable and is Process.
type Service struct {
	opts Options
	runs atomic.Int64
}

// Process is the Service the default capability registry registers.
var Process = &Service{}

// New returns a Service with explicit wiring. Tests use it; production uses
// Process (defaults resolved at job time).
func New(o Options) *Service { return &Service{opts: o} }

// Runs reports completed invocations, for the dormancy assertion.
func (s *Service) Runs() int64 { return s.runs.Load() }

func (s *Service) dialer() Dialer {
	if s.opts.Dialer != nil {
		return s.opts.Dialer
	}
	return defaultDialer{}
}

func (s *Service) winrmDialer() WinRMDialer {
	if s.opts.WinRMDialer != nil {
		return s.opts.WinRMDialer
	}
	return defaultWinRMDialer{}
}

func (s *Service) downloader() Downloader {
	if s.opts.Downloader != nil {
		return s.opts.Downloader
	}
	return defaultDownloader{}
}

// Probe is the `agent.push_probe` capability: read the host key, no credential.
func (s *Service) Probe(ctx context.Context, job jobs.VerifiedJob) (any, error) {
	return s.probe(ctx, job.Params())
}

// probe is the testable core: it takes the signed params bytes and nothing
// else, exactly as netsweep's `run` does, so the logic is exercised without a
// forged VerifiedJob (which is unforgeable by design).
func (s *Service) probe(ctx context.Context, raw json.RawMessage) (any, error) {
	var p probeParams
	if err := json.Unmarshal(raw, &p); err != nil {
		return nil, fmt.Errorf("push probe: bad params: %w", err)
	}
	if p.TargetAddress == "" {
		return nil, fmt.Errorf("push probe: no target address in signed params")
	}
	switch p.Transport {
	case "ssh", "": // "" is the schema default: ssh.
	case "winrm":
		return s.probeWinRM(ctx, p)
	default:
		return nil, fmt.Errorf("push probe: unsupported transport %q (ssh or winrm)", p.Transport)
	}
	info, err := s.dialer().Probe(ctx, p.TargetAddress)
	if err != nil {
		return nil, fmt.Errorf("push probe: %s: %w", p.TargetAddress, err)
	}
	s.runs.Add(1)
	return probeResult{
		TargetAddress:      p.TargetAddress,
		Transport:          "ssh",
		HostKeyFingerprint: info.Fingerprint,
		HostKeyType:        info.KeyType,
		Banner:             info.Banner,
	}, nil
}

// Install is the `agent.push_install` capability. The order is fixed and each
// step is a bound (see the package comment).
func (s *Service) Install(ctx context.Context, job jobs.VerifiedJob) (any, error) {
	return s.install(ctx, job.Params())
}

// install is the testable core; see probe.
func (s *Service) install(ctx context.Context, raw json.RawMessage) (any, error) {
	var p installParams
	if err := json.Unmarshal(raw, &p); err != nil {
		return nil, fmt.Errorf("push install: bad params: %w", err)
	}
	if p.TargetAddress == "" {
		return nil, fmt.Errorf("push install: no target address in signed params")
	}
	switch p.Transport {
	case "ssh":
	case "winrm":
		return s.installWinRM(ctx, p)
	default:
		return nil, fmt.Errorf("push install: unsupported transport %q (ssh or winrm)", p.Transport)
	}
	// **NEVER TRUST-ON-FIRST-USE FOR AN INSTALL** (§7.3). An empty pinned key
	// means the server dispatched without a confirmed fingerprint — which the
	// refusal ladder prevents — so refuse rather than accept any key.
	if strings.TrimSpace(p.HostKeyFingerprint) == "" {
		return nil, fmt.Errorf(
			"push install: no host-key fingerprint in signed params; refusing rather than " +
				"accepting any key (trust-on-first-use with an administrative credential)")
	}
	if p.Credential.Username == "" || p.Credential.Secret == "" {
		return nil, fmt.Errorf("push install: incomplete credential in signed params")
	}
	// WHERE THE TARGET ENROLLS: the signed enroll_url (plan W2, superseded),
	// checked before anything is dialled -- the install command cannot carry a
	// value it would have to refuse after the credential was presented.
	command, err := InstallCommand(p.EnrollURL)
	if err != nil {
		return nil, fmt.Errorf("push install: no usable enroll_url in signed params: %w", err)
	}

	secret := []byte(p.Credential.Secret)
	defer zero(secret)

	sess, err := s.dialer().Open(ctx, p.TargetAddress, p.HostKeyFingerprint, p.Credential.Username, secret)
	if err != nil {
		switch {
		case errors.Is(err, ErrHostKey):
			return installResult{
				TargetAddress: p.TargetAddress, Outcome: outcomeUnreach,
				Detail: "host-key verification failed; the target is not the machine whose key was confirmed",
			}, nil
		case errors.Is(err, ErrAuth):
			return installResult{
				TargetAddress: p.TargetAddress, Outcome: outcomeAuthFailed,
				Detail: "authentication failed",
			}, nil
		default:
			return installResult{
				TargetAddress: p.TargetAddress, Outcome: outcomeUnreach,
				Detail: "unreachable",
			}, nil
		}
	}
	defer func() { _ = sess.Close() }()

	osDetected, archDetected, verr := verifyOSArch(ctx, sess, p.OS, p.Arch)
	if verr != nil {
		return installResult{
			TargetAddress: p.TargetAddress, Outcome: outcomeOSMismatch,
			OSDetected: osDetected, ArchDetected: archDetected, Detail: verr.Error(),
		}, nil
	}

	// Download and VERIFY the digest BEFORE a byte reaches the target (falsifier 6).
	artefact, derr := s.downloader().Download(ctx, p.Release.URL)
	if derr != nil {
		return installResult{
			TargetAddress: p.TargetAddress, Outcome: outcomeFailed,
			OSDetected: osDetected, ArchDetected: archDetected,
			Detail: fmt.Sprintf("artefact download: %v", derr),
		}, nil
	}
	if serr := verifyDigest(artefact, p.Release.SHA256); serr != nil {
		return installResult{
			TargetAddress: p.TargetAddress, Outcome: outcomeFailed,
			OSDetected: osDetected, ArchDetected: archDetected, Detail: serr.Error(),
		}, nil
	}

	if _, merr := sess.Run(ctx, PrepareStagingCommand); merr != nil {
		return installResult{
			TargetAddress: p.TargetAddress, Outcome: outcomeFailed,
			OSDetected: osDetected, ArchDetected: archDetected,
			Detail: fmt.Sprintf("staging directory: %v", merr),
		}, nil
	}
	// From here on the staging directory exists: every failure runs the
	// cleanup before it is reported (review Minor 6). Its own failure is not
	// reported over the one that caused it.
	failed := func(detail string) (any, error) {
		_, _ = sess.Run(ctx, CleanupStagingCommand)
		return installResult{
			TargetAddress: p.TargetAddress, Outcome: outcomeFailed,
			OSDetected: osDetected, ArchDetected: archDetected,
			Detail: detail,
		}, nil
	}
	if perr := sess.Put(ctx, stagedArtefactPath, artefact, artefactFileMode); perr != nil {
		return failed(fmt.Sprintf("artefact transfer: %v", perr))
	}
	if perr := sess.Put(ctx, tokenFilePath, []byte(p.EnrollToken), tokenFileMode); perr != nil {
		return failed(fmt.Sprintf("token placement: %v", perr))
	}
	if _, rerr := sess.Run(ctx, command); rerr != nil {
		return failed(fmt.Sprintf("install: %v", rerr))
	}

	s.runs.Add(1)
	return installResult{
		TargetAddress: p.TargetAddress, Outcome: outcomeInstalled,
		OSDetected: osDetected, ArchDetected: archDetected,
	}, nil
}

// InstallCommand is the FIXED, compiled-in installer invocation: the placed
// binary's install mode (internal/install, `its-agent install`), which does
// what packaging/{linux,darwin}/install.sh do. A constant template of
// compiled-in paths with ONE value filled in, the signed enroll origin, which
// must pass layout.CheckEnrollOrigin -- letters, digits, dots, hyphens and a
// port, so nothing a shell reads as syntax -- and is single-quoted besides. The
// token is NEVER in it: the artefact reads it from its 0600 file (spec §4.2,
// clause (b), falsifier 14).
func InstallCommand(enrollURL string) (string, error) {
	if err := layout.CheckEnrollOrigin(enrollURL); err != nil {
		return "", err
	}
	return fmt.Sprintf("%s %s %s %s %s '%s'", stagedArtefactPath, layout.PushSubcommand,
		layout.FlagTokenFile, tokenFilePath, layout.FlagEnrollURL, enrollURL), nil
}

// verifyOSArch runs `uname -s -m` and compares against the operator's
// declaration. A mismatch is a clean, named failure (§7.2).
func verifyOSArch(ctx context.Context, sess Session, declaredOS, declaredArch string) (string, string, error) {
	out, err := sess.Run(ctx, "uname -s -m")
	if err != nil {
		return "", "", fmt.Errorf("uname failed: %w", err)
	}
	fields := strings.Fields(strings.TrimSpace(out))
	if len(fields) < 2 {
		return "", "", fmt.Errorf("uname returned %q, cannot read OS/arch", out)
	}
	gotOS := normalizeOS(fields[0])
	gotArch := normalizeArch(fields[1])
	if gotOS != declaredOS {
		return gotOS, gotArch, fmt.Errorf(
			"OS mismatch: operator declared %q, target is %q; refusing to install the wrong binary",
			declaredOS, gotOS)
	}
	if gotArch != declaredArch {
		return gotOS, gotArch, fmt.Errorf(
			"arch mismatch: operator declared %q, target is %q", declaredArch, gotArch)
	}
	return gotOS, gotArch, nil
}

func normalizeOS(uname string) string {
	switch strings.ToLower(strings.TrimSpace(uname)) {
	case "linux":
		return "linux"
	case "darwin":
		return "darwin"
	default:
		return strings.ToLower(strings.TrimSpace(uname))
	}
}

func normalizeArch(uname string) string {
	switch strings.ToLower(strings.TrimSpace(uname)) {
	case "x86_64", "amd64":
		return "amd64"
	case "aarch64", "arm64":
		return "arm64"
	default:
		return strings.ToLower(strings.TrimSpace(uname))
	}
}

// verifyDigest checks the artefact's SHA-256 against the signed hex digest, on
// the bytes the agent actually downloaded, before any transfer.
func verifyDigest(artefact []byte, wantHex string) error {
	sum := sha256.Sum256(artefact)
	got := hex.EncodeToString(sum[:])
	if !strings.EqualFold(got, wantHex) {
		return fmt.Errorf(
			"artefact SHA-256 %s does not match the signed digest %s; NOT transferring",
			got, wantHex)
	}
	return nil
}

// zero overwrites a secret buffer. Best-effort: Go strings cannot be zeroed, so
// the copy inside the (server-redacted, server-scrubbed) signed params is the
// residual this capability does not add to.
func zero(b []byte) {
	for i := range b {
		b[i] = 0
	}
}
