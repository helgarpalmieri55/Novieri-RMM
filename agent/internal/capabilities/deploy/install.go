package deploy

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// `app.deploy` (docs/modules/app-deploy/spec.md §6, AD-07).

// deployParams implements AppDeployParams.schema.json.
//
// The three nullable members are POINTERS because `null` and `""` are
// different statements, and the difference is load-bearing: "there is no
// artifact" is what makes a package manager-sourced, while "the artifact ref
// is the empty string" is a malformed job. Collapsing them would make the
// second look like the first, which is precisely the direction that skips a
// hash check.
//
// PackageIdentifier is a pointer for the same reason and one more: it is the
// ADDITIVE member (server-side migration 0052), so `null` is the state an
// older control plane produces, and it has to be distinguishable from a
// present-but-empty value. See packageIdentifier for what each state does.
//
// DeferralHours is a pointer because its schema default (24) is not its zero
// value. It is carried and NOT acted on — see the note in `deploy`.
type deployParams struct {
	Action            string         `json:"action"`
	InstallerType     string         `json:"installer_type"`
	ArtifactRef       *string        `json:"artifact_ref"`
	ArtifactSHA256    *string        `json:"artifact_sha256"`
	PackageIdentifier *string        `json:"package_identifier"`
	InstallArgs       *string        `json:"install_args"`
	DetectionRule     map[string]any `json:"detection_rule"`
	UninstallRule     map[string]any `json:"uninstall_rule"`
	DeferralHours     *int           `json:"deferral_hours"`
}

// deployResult implements AppDeployResult.schema.json: a LIST of outcomes,
// even though a deploy job is one device and one package and therefore always
// produces exactly one. The list is the contract
// (app/modules/deploy/dispatch.py::ingest_deploy_result reads `outcomes[0]`
// and has an explicit branch for the empty list), so it is emitted as a list.
type deployResult struct {
	Outcomes []installOutcome `json:"outcomes"`
}

// The wire enum, partitioned. EVERY member of AppDeployParams.installer_type
// is in exactly one of these two sets, and schema_test.go asserts that
// partition against the exported schema file rather than against this comment.
//
// THE PARTITION IS THE SECURITY CONTROL. Which branch a job takes is a
// function of `installer_type` ALONE — never of whether `artifact_ref` happens
// to be present. That direction matters and the opposite is the bug: a branch
// selected by "is there an artifact?" turns a NULLED-OUT artifact_ref into a
// way to reach an execution path that does no hash verification. Here, an
// artifact-backed type with no artifact is REFUSED, and a manager-sourced type
// that carries an artifact is REFUSED, so there is no job shape that reaches a
// process spawn without either a verified hash or no artifact at all.
var (
	artifactBacked = map[string]struct{}{
		"msi": {}, "exe": {}, "pkg": {}, "dmg": {}, "deb": {}, "rpm": {}, "script": {},
	}
	managerSourced = map[string]struct{}{
		"winget": {}, "choco": {},
	}
	installerTypes = func() map[string]struct{} {
		all := map[string]struct{}{}
		for k := range artifactBacked {
			all[k] = struct{}{}
		}
		for k := range managerSourced {
			all[k] = struct{}{}
		}
		return all
	}()
	actions = map[string]struct{}{"install": {}, "update": {}, "uninstall": {}}
)

const (
	// stagedName is the name a verified artifact is renamed to. The extension
	// is load-bearing on Windows: msiexec insists on `.msi`, and CreateProcess
	// will not run a file without an executable extension.
	stagedMSI = "package.msi"
	stagedEXE = "package.exe"
	stagedPKG = "package.pkg"
	// partName is where bytes land BEFORE the hash is checked. Nothing ever
	// executes this path.
	partName = "download.part"
	// sha256Hex is the length of a hex-encoded SHA-256.
	sha256Hex = 64
)

func (s *Service) deploy(ctx context.Context, raw json.RawMessage) (*deployResult, error) {
	p, err := parseDeployParams(raw)
	if err != nil {
		return nil, err
	}

	// `deferral_hours` IS CARRIED AND NOT ACTED ON. The spec's §7 says user
	// notification/deferral is "honored where the OS engine supports it", and
	// this build has no user-notification channel at all — there is no agent
	// UI, no toast, and no session to show one in when running as SYSTEM.
	// Silently ignoring it is the honest behaviour; pretending to defer would
	// be worse. Recorded in docs/modules/app-deploy/agent-capability.md.
	_ = p.DeferralHours

	packageID, err := p.packageIdentifier()
	if err != nil {
		return nil, err
	}
	identifier := clampIdentifier(packageID)

	// ------------------------------------------------------------------
	// Detection, BEFORE anything is downloaded. A device already in the
	// desired state costs no bandwidth and no process (AD-06).
	// ------------------------------------------------------------------
	stat := s.stat()
	before := evaluate(p.DetectionRule, stat)
	if desiredStateReached(p.Action, before) {
		return &deployResult{Outcomes: []installOutcome{{
			Identifier:    identifier,
			State:         stateNotApplicable,
			ExitCode:      nil,
			Log:           "the detection rule already reports the desired state; nothing was executed",
			LogRef:        nil,
			DetectedAfter: detectedAfter(before),
		}}}, nil
	}

	extra, err := splitArgs(p.InstallArgs)
	if err != nil {
		return nil, err
	}

	sc, err := newScratch(s.opts.ScratchRoot)
	if err != nil {
		return nil, err
	}
	defer func() {
		if cerr := sc.Close(); cerr != nil {
			s.logger().Warn("app.deploy: the job's scratch directory could not be removed",
				"package", identifier)
		}
	}()

	// ------------------------------------------------------------------
	// The artifact. `stage` returns a path ONLY when the SHA-256 in the
	// signed params matches the bytes that were fetched.
	// ------------------------------------------------------------------
	var artifactPath string
	if _, backed := artifactBacked[p.InstallerType]; backed {
		artifactPath, err = s.stage(ctx, sc, p)
		if err != nil {
			return nil, err
		}
	}

	cmd, err := planInstall(s.goos(), p, artifactPath, packageID, extra, s.lookPath())
	if err != nil {
		return nil, err
	}
	cmd.Dir = sc.dir

	// NOTHING JOB-SUPPLIED BEYOND THE ALREADY-VALIDATED IDENTIFIER. Not the
	// artifact ref, not the install args, not the argv.
	s.logger().Info("app.deploy: executing a verified deployment",
		"package", identifier, "action", p.Action, "installer_type", p.InstallerType,
		"program", cmd.Label)

	out, execErr := s.executor()(ctx, cmd)
	if execErr != nil {
		return nil, execErr
	}

	after := evaluate(p.DetectionRule, stat)
	return &deployResult{Outcomes: []installOutcome{deployOutcome(identifier, out, after)}}, nil
}

// deployOutcome maps a finished installer onto InstallOutcome.
//
// `installed` and `failed` are deploy's vocabulary
// (app/modules/deploy/dispatch.py::_DEPLOY_OUTCOME_STATES, which also allows
// `not_applicable`). `installed` is used for a successful UNINSTALL too, which
// reads oddly and is correct: the server's `deployment_results.state` domain
// has no `uninstalled`, and `succeeded` would be coerced to `failed` with the
// original recorded as an anomaly.
func deployOutcome(identifier string, out Outcome, after detection) installOutcome {
	code := out.ExitCode
	state := stateFailed
	if !out.TimedOut && installerSucceeded(code) {
		state = stateInstalled
	}
	log := wireLog(out)
	if out.TimedOut {
		log = "[the job's deadline passed and the installer was killed]\n" + log
	}
	return installOutcome{
		Identifier:    identifier,
		State:         state,
		ExitCode:      &code,
		Log:           log,
		LogRef:        nil, // there is no agent-facing artifact-upload route
		DetectedAfter: detectedAfter(after),
	}
}

// installerSucceeded maps an installer's exit code.
//
// 0 is success everywhere. 3010 is `ERROR_SUCCESS_REBOOT_REQUIRED` and 1641 is
// `ERROR_SUCCESS_REBOOT_INITIATED`: both are MSI/Windows-installer successes
// that the naive "zero means good" rule reports as failures, which is how a
// fleet-wide rollout of a package that needs a reboot shows up as a fleet-wide
// failure. They are accepted as `installed`, and the reboot itself remains the
// operator's decision — `msiQuiet` passes `/norestart` precisely so the
// endpoint never reboots on its own.
//
// NOTHING ELSE IS FORGIVEN. In particular 1605 ("this action is only valid for
// installed products") is NOT treated as success on an uninstall, even though
// it is tempting: it means the product was not there, which is a
// `not_applicable` the detection rule should have caught, and quietly calling
// it `installed` would hide a broken detection rule forever.
func installerSucceeded(code int) bool {
	switch code {
	case 0, 3010, 1641:
		return true
	}
	return false
}

// stage downloads the artifact, verifies its SHA-256, and only then produces a
// path a process can be spawned from.
//
// THIS IS THE FUNCTION AD-07 IS ABOUT, AND THE ORDERING IS THE CONTROL.
//
// Bytes are streamed into `download.part` while being hashed. That file is
// never named by any Command. The executable path — `package.msi`,
// `package.exe`, `package.pkg` — comes into existence ONLY by the os.Rename on
// the last line, and that line is reachable only through the comparison above
// it. So "the installer did not run" is not something a caller has to remember
// to check: on a mismatch the file the planner would name DOES NOT EXIST, and
// hash_test.go asserts exactly that, in addition to asserting the executor was
// never called.
//
// The download is not the authorization. Object storage is not a trust
// boundary — whoever can write the bucket can change the bytes — and what
// authorises this installer to run as SYSTEM is the server's Ed25519 signature
// over the params that NAMED THIS HASH.
func (s *Service) stage(ctx context.Context, sc *scratch, p deployParams) (string, error) {
	// Both of these are checked in parseDeployParams too. They are checked
	// AGAIN here because this is the function whose contract is "no path
	// without a verified hash", and a guarantee that depends on a caller
	// having validated something is not a guarantee.
	if p.ArtifactRef == nil || *p.ArtifactRef == "" {
		return "", jobs.Refuse("app.deploy: this installer_type is delivered as an artifact and " +
			"the job carries no artifact_ref; nothing was downloaded and nothing was executed")
	}
	want, err := expectedDigest(p.ArtifactSHA256)
	if err != nil {
		return "", err
	}

	body, err := s.fetcher()(ctx, *p.ArtifactRef)
	if err != nil {
		return "", err
	}
	defer body.Close()

	partPath := sc.path(partName)
	f, err := os.OpenFile(partPath, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return "", fmt.Errorf("app.deploy: the download could not be staged")
	}
	h := sha256.New()
	n, copyErr := io.Copy(io.MultiWriter(f, h), io.LimitReader(body, maxArtifactBytes+1))
	closeErr := f.Close()
	if copyErr != nil {
		// A transport failure is FAILED, not refused: the device is capable,
		// the network was not.
		return "", fmt.Errorf("app.deploy: the artifact download did not complete")
	}
	if closeErr != nil {
		return "", fmt.Errorf("app.deploy: the downloaded artifact could not be written")
	}
	if n > maxArtifactBytes {
		return "", fmt.Errorf("app.deploy: the artifact is larger than %d bytes", maxArtifactBytes)
	}

	got := h.Sum(nil)
	// Constant time, not because a timing oracle is a realistic attack on a
	// value the server publishes, but because there is no reason for the
	// comparison of a digest to ever be the variable-time one and a reader
	// should not have to work out which case this is.
	if subtle.ConstantTimeCompare(got, want) != 1 {
		// The two digests are NOT in the message. The expected one is
		// job-supplied and the computed one describes bytes an attacker may
		// control; both would be echoed into the server's audit log. The fact
		// that matters is that nothing ran.
		_ = os.Remove(partPath)
		return "", fmt.Errorf("app.deploy: the downloaded artifact does not match the SHA-256 " +
			"in the signed job params. The installer was NOT executed and the download has been " +
			"discarded")
	}

	staged := sc.path(stagedNameFor(p.InstallerType))
	if err := os.Rename(partPath, staged); err != nil {
		return "", fmt.Errorf("app.deploy: the verified artifact could not be staged")
	}
	if err := os.Chmod(staged, 0o700); err != nil {
		// Advisory on Windows; the scratch directory's DACL is what restricts
		// access there. On POSIX it is what makes an `exe`-equivalent
		// executable at all.
		return "", fmt.Errorf("app.deploy: the verified artifact could not be made executable")
	}
	return staged, nil
}

// stagedNameFor is the on-disk name of a verified artifact. Only the three
// types this build has an engine for can reach here; the others were refused
// by platformAllows before any download started.
func stagedNameFor(installerType string) string {
	switch installerType {
	case "msi":
		return stagedMSI
	case "pkg":
		return stagedPKG
	}
	return stagedEXE
}

// expectedDigest parses `artifact_sha256` into raw bytes.
//
// A malformed digest is a REFUSAL, and it is important that it is not a
// lenient parse: "close enough" on the value that decides whether a binary
// runs as SYSTEM is the same as no check at all. Exactly 64 hex characters,
// case-insensitive (the server emits lowercase from hashlib.hexdigest, and
// accepting uppercase is docs/kernel/wire-protocol/README.md's "loosen inbound
// before you tighten outbound"), nothing else.
func expectedDigest(raw *string) ([]byte, error) {
	if raw == nil {
		return nil, jobs.Refuse("app.deploy: this installer_type is delivered as an artifact and " +
			"the job carries no artifact_sha256. This agent does not execute an artifact it " +
			"cannot verify; nothing was downloaded and nothing was executed")
	}
	text := strings.ToLower(strings.TrimSpace(*raw))
	if len(text) != sha256Hex {
		return nil, jobs.Refuse("app.deploy: artifact_sha256 is not 64 hex characters; nothing " +
			"was downloaded and nothing was executed")
	}
	sum, err := hex.DecodeString(text)
	if err != nil {
		return nil, jobs.Refuse("app.deploy: artifact_sha256 is not hexadecimal; nothing was " +
			"downloaded and nothing was executed")
	}
	return sum, nil
}

// parseDeployParams validates everything before anything is downloaded or
// executed, and REFUSES rather than failing on every violation.
func parseDeployParams(raw json.RawMessage) (deployParams, error) {
	var p deployParams
	if err := json.Unmarshal(raw, &p); err != nil {
		return p, jobs.Refuse("app.deploy: params are not an AppDeployParams; nothing was executed")
	}
	if _, ok := actions[p.Action]; !ok {
		return p, jobs.Refuse("app.deploy: action is not one of install, update, uninstall; " +
			"nothing was executed")
	}
	if _, ok := installerTypes[p.InstallerType]; !ok {
		return p, jobs.Refuse("app.deploy: installer_type is not one this contract defines; " +
			"nothing was executed")
	}

	// THE PARTITION GUARD. See the comment on artifactBacked: the branch is a
	// function of installer_type alone, and these two checks are what make a
	// job that disagrees with its own type unrepresentable rather than
	// merely unlikely.
	_, backed := artifactBacked[p.InstallerType]
	if backed {
		if p.ArtifactRef == nil || *p.ArtifactRef == "" {
			return p, jobs.Refuse("app.deploy: installer_type is delivered as an artifact and " +
				"artifact_ref is absent; nothing was executed")
		}
		if _, err := expectedDigest(p.ArtifactSHA256); err != nil {
			return p, err
		}
	} else {
		// A manager-sourced package that carries an artifact is refused rather
		// than having its artifact ignored. Ignoring it would mean a job could
		// name an installer AND a hash and reach a code path that downloads
		// and runs neither — or worse, that a future edit which started
		// honouring artifact_ref on this branch would inherit no hash check.
		if p.ArtifactRef != nil {
			return p, jobs.Refuse("app.deploy: a manager-sourced installer_type must carry no " +
				"artifact_ref. This agent will not run a manager install for a job that also " +
				"names an artifact; nothing was executed")
		}
		if p.ArtifactSHA256 != nil {
			return p, jobs.Refuse("app.deploy: a manager-sourced installer_type must carry no " +
				"artifact_sha256; nothing was executed")
		}
	}
	return p, nil
}

// packageIdentifier resolves the string this deployment is called by.
//
// `winget` and `choco` need a package IDENTIFIER on the command line —
// `Mozilla.Firefox`, `googlechrome`. THERE ARE TWO SOURCES, IN ORDER, AND THE
// ORDER IS THE MIXED-VERSION CONTRACT:
//
//  1. `package_identifier`, the explicit member. It did not exist when this
//     capability was written and was added additively and OPTIONALLY
//     (AppDeployParams.schema.json; server-side `packages.package_identifier`
//     + `artifacts.manager_package_identifier`).
//  2. `detection_rule.name`, which is what this agent read before the member
//     existed, and is exactly what `manager_package_identifier` itself falls
//     back to for a catalog row nobody has revisited.
//
// Both directions of the mixed fleet CLAUDE.md warns about are covered by that
// ordering, and neither is hypothetical — a fleet is always running an agent
// older than its control plane:
//
//   - NEW CONTROL PLANE -> OLD AGENT. The member is unknown, encoding/json
//     drops it, and the old agent derives the identifier from
//     `detection_rule.name`. The server's fallback computes the same string
//     for a row with no explicit value, so nothing changes in the field.
//   - NEW AGENT -> OLD CONTROL PLANE. The member is absent, which is `null`,
//     which is source 2 — the path this agent already had. THIS AGENT DOES NOT
//     REQUIRE THE FIELD, and TestFieldAbsentFallsBackToDetectionRuleName is
//     the guard on that. Requiring it would have been the outage.
//
// Only when BOTH are unusable is this a refusal, and the refusal names both
// sources. The agent still does not invent an identifier and still does not
// fish one out of `install_args`.
//
// For an artifact-backed package no identifier is needed to install, so the
// installer_type and action are used — nothing job-supplied at all. A
// `package_identifier` on such a job is IGNORED rather than refused, which is
// the opposite of what parseDeployParams does with a stray `artifact_ref`, and
// the difference is not an inconsistency: an artifact_ref on a manager job is
// refused because tolerating it is the direction that could one day reach an
// execution path with no hash check, whereas this member selects no branch at
// all and consumes nothing. Refusing an inert additive member is how an agent
// turns a harmless server-side change into a fleet-wide outage.
func (p deployParams) packageIdentifier() (string, error) {
	if _, manager := managerSourced[p.InstallerType]; !manager {
		return p.InstallerType + "/" + p.Action, nil
	}

	name := p.managerIdentifier()
	if name == "" {
		return "", jobs.Refuse("app.deploy: a manager-sourced deployment needs a package " +
			"identifier and this job carries no usable one. This agent reads package_identifier, " +
			"then falls back to detection_rule.name on a package_installed rule, and will not " +
			"invent an identifier or parse one out of install_args; nothing was executed")
	}
	if !validPackageID(name) {
		// The VALUE is not named: it is job-supplied and this reason lands in
		// the server's hash-chained audit log.
		return "", jobs.Refuse("app.deploy: the package identifier is not a plain identifier " +
			"(letters, digits and . _ - / \\ : = + , @ # { }, not starting with - or /); " +
			"nothing was executed")
	}
	return name, nil
}

// managerIdentifier returns the first usable of the two sources, or "".
//
// Trimmed, because a value that is only whitespace is not an identifier and
// `winget install --id "   "` is not a question anyone meant to ask. An
// explicit member that is present but blank falls THROUGH to the detection
// rule rather than refusing outright: the two are alternative spellings of the
// same fact, and a control plane that sent an empty string alongside a good
// rule meant the rule.
func (p deployParams) managerIdentifier() string {
	if p.PackageIdentifier != nil {
		if explicit := strings.TrimSpace(*p.PackageIdentifier); explicit != "" {
			return explicit
		}
	}
	name, _ := p.DetectionRule["name"].(string)
	return strings.TrimSpace(name)
}
