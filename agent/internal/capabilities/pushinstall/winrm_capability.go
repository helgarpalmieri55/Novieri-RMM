package pushinstall

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"strings"

	"github.com/toeshee/it-suite/agent/internal/capabilities/pushinstall/winrm"
	"github.com/toeshee/it-suite/agent/internal/install/layout"
)

// The winrm branch of the two push capabilities (plan 2026-09-28-windows-push-
// winrm Task 7). The ORDER and the bounds are the ssh path's, transposed:
//
//   - the one address dialled is the signed target_address (agent falsifier 4);
//     the DNS name is only the Kerberos service name, never a thing to resolve
//     and dial instead;
//   - the probe presents NO credential (§C1): readiness only;
//   - the artefact is downloaded and its digest verified on the site server
//     BEFORE the credential is used (falsifier 6), and the target verifies the
//     same signed SHA-256 again before placing it;
//   - the script is compiled in and filled only with plan layout and signed
//     values (2a clause (b)); the enrolment token goes over stdin;
//   - the credential copy is zeroed on return (falsifier 3).
//
// host_key_fingerprint carries the sentinel "kerberos" on this path (W4) and
// is not consulted: the target is authenticated by a service ticket only the
// real computer account can open, and by the AP-REP it must return.

// validDNSName is W10 on the agent side: a real hostname with a domain part,
// never an IP literal (Kerberos needs HTTP/<fqdn>).
func validDNSName(name string) bool {
	n := strings.TrimSuffix(strings.TrimSpace(name), ".")
	return n != "" && net.ParseIP(n) == nil && strings.Contains(n, ".") && !strings.ContainsAny(n, "/\\@ :")
}

func (s *Service) probeWinRM(ctx context.Context, p probeParams) (any, error) {
	if !validDNSName(p.DNSName) {
		return nil, fmt.Errorf("push probe: winrm needs the target's DNS name (HTTP/<fqdn>), got %q", p.DNSName)
	}
	r, err := s.winrmDialer().Probe(ctx, p.DNSName, p.TargetAddress)
	if err != nil {
		return nil, fmt.Errorf("push probe: %s: %w", p.TargetAddress, err)
	}
	s.runs.Add(1)
	return probeResult{
		TargetAddress: p.TargetAddress,
		Transport:     "winrm",
		WinRMHTTPS:    r.HTTPS,
		WinRMHTTP:     r.HTTP,
		KDCReachable:  r.KDCReachable,
	}, nil
}

// winrmSecret is the windows_domain credential's secret as the control plane
// folds it (plan Task 4/7): {"secret", "kdc", "ca_bundle"} inside the one
// already-redacted, already-scrubbed PushCredential.secret. No wire change.
type winrmSecret struct {
	Secret   string `json:"secret"`
	KDC      string `json:"kdc"`
	CABundle string `json:"ca_bundle"`
}

func (s *Service) installWinRM(ctx context.Context, p installParams) (any, error) {
	if !validDNSName(p.DNSName) {
		return nil, fmt.Errorf("push install: winrm needs the target's DNS name (HTTP/<fqdn>), got %q", p.DNSName)
	}
	if p.OS != "windows" {
		return nil, fmt.Errorf("push install: transport winrm is for os windows, params say %q", p.OS)
	}
	if p.Credential.Username == "" || p.Credential.Secret == "" {
		return nil, fmt.Errorf("push install: incomplete credential in signed params")
	}
	// WHERE THE TARGET ENROLLS: the signed enroll_url, a bare https origin, or
	// no install. There is deliberately no fallback to the presigned release
	// URL's origin (plan W2, superseded): with external object storage that
	// origin is the storage provider, which would be sent the single-use
	// enrollment token.
	if err := checkConsole(p.EnrollURL); err != nil {
		return nil, fmt.Errorf("push install: no usable enroll_url in signed params: %w", err)
	}
	console := p.EnrollURL
	// THE REALM IS THE ACCOUNT'S (review I1), and the target must be inside it:
	// refused here, before anything is downloaded or dialled -- winrm.Login
	// refuses the same again before any KDC is contacted.
	realm, rerr := winrm.RealmOfAccount(p.Credential.Username)
	if rerr != nil {
		return nil, fmt.Errorf("push install: %w", rerr)
	}
	if !winrm.InRealm(p.DNSName, realm) {
		return nil, fmt.Errorf("push install: %s is not in the account's realm %s", p.DNSName, realm)
	}
	var ws winrmSecret
	if err := json.Unmarshal([]byte(p.Credential.Secret), &ws); err != nil || ws.Secret == "" {
		// Never echo the value: it is the credential.
		return nil, fmt.Errorf("push install: the winrm credential secret is not the expected JSON object")
	}
	password := []byte(ws.Secret)
	defer zero(password)
	stdin := []byte(p.EnrollToken + "\n")
	defer zero(stdin)
	if strings.TrimSpace(p.EnrollToken) == "" || strings.ContainsAny(p.EnrollToken, "\r\n") {
		return nil, fmt.Errorf("push install: no usable enrolment token in signed params")
	}

	result := func(outcome, detail, arch string) installResult {
		r := installResult{TargetAddress: p.TargetAddress, Outcome: outcome, Detail: detail}
		if arch != "" {
			r.OSDetected, r.ArchDetected = "windows", arch
		}
		return r
	}

	plan := layout.NewPlan("", "", console)
	script, err := BuildInstallScript(plan, p.Release, p.Arch, console)
	if err != nil {
		return result(outcomeFailed, err.Error(), ""), nil
	}

	// Download and VERIFY the digest BEFORE the credential is used (falsifier 6).
	artefact, derr := s.downloader().Download(ctx, p.Release.URL)
	if derr != nil {
		return result(outcomeFailed, fmt.Sprintf("artefact download: %v", derr), ""), nil
	}
	if serr := verifyDigest(artefact, p.Release.SHA256); serr != nil {
		return result(outcomeFailed, serr.Error(), ""), nil
	}

	cred := WinRMCredential{Username: p.Credential.Username, Password: password, KDC: ws.KDC, CABundle: ws.CABundle}
	stdout, code, rerr := s.winrmDialer().Run(ctx, p.DNSName, p.TargetAddress, cred, script, stdin)
	if rerr != nil {
		switch {
		case errors.Is(rerr, ErrAuth):
			return result(outcomeAuthFailed, rerr.Error(), ""), nil
		default:
			return result(outcomeUnreach, rerr.Error(), ""), nil
		}
	}
	arch := scriptArch(stdout)
	switch {
	case code == scriptExitArchMismatch:
		return result(outcomeOSMismatch, fmt.Sprintf(
			"arch mismatch: operator declared %q, target is %q; refusing to install the wrong binary", p.Arch, arch), arch), nil
	case code == scriptExitDigestMismatch:
		return result(outcomeFailed, "the artefact the target downloaded does not match the signed SHA-256; nothing was placed", arch), nil
	case code == scriptExitNoToken:
		return result(outcomeFailed, "the enrolment token did not reach the target's install script", arch), nil
	case code == scriptExitUnsafeDirectory:
		return result(outcomeFailed, "a directory of the agent's layout on the target is a reparse point "+
			"(a junction or link); nothing was installed through it", arch), nil
	case code == scriptExitForeignRoot:
		return result(outcomeFailed, "%ProgramData%\\ITSuite on the target is owned by an account other than Administrators or SYSTEM; "+
			"it was left untouched and nothing was installed. An administrator must check that folder and remove it", arch), nil
	case code == scriptExitInstalledMismatch:
		return result(outcomeFailed, "the installed its-agent.exe does not match the signed SHA-256 after it was placed; "+
			"it was deleted and the service was not started", arch), nil
	case code == scriptExitNoLayout:
		return result(outcomeFailed, "the target's session has no %ProgramFiles% or %ProgramData% to install under", arch), nil
	case code != 0 || !strings.Contains(stdout, scriptMarkerInstalled):
		// stderr is never read back (it echoes script lines); the exit code is
		// the whole account.
		return result(outcomeFailed, fmt.Sprintf("the install script exited %d without completing", code), arch), nil
	}
	s.runs.Add(1)
	return result(outcomeInstalled, "", arch), nil
}

// scriptArch reads the arch marker the script prints first.
func scriptArch(stdout string) string {
	for _, line := range strings.Split(stdout, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, scriptMarkerArch) {
			return normalizeArch(strings.TrimPrefix(line, scriptMarkerArch))
		}
	}
	return ""
}
