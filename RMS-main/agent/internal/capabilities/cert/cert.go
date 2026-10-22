// Package cert is the endpoint half of automatic certificate renewal
// (docs/modules/agent/cert-renewal.md). Two capabilities, both reached only
// through the signed-job runner.
//
// # Invariant #1 gets no exemption here — least of all here
//
// These are the only job types whose effect is on the agent's OWN IDENTITY
// rather than on the host, which makes them the ones that most need the
// signature. They receive a jobs.VerifiedJob like every other capability: the
// runner has already checked the Ed25519 signature over the canonical bytes
// against the key pinned at enrollment, that the job targets this device, that
// it is fresh, that its nonce is unseen, and that the type is registered. There
// is no second path into either function.
//
// # Invariant #3: the private key never leaves the endpoint
//
// `agent.cert.renew` generates a fresh keypair on the endpoint and returns only
// a CSR. The control plane never sees a private key at renewal, exactly as it
// never sees one at enrollment.
//
// # Dormancy
//
// Registration is a map insert. Neither handler touches the disk, allocates a
// key, or resolves configuration until a verified job actually arrives — which
// is why `Process` resolves its config lazily rather than at package init.
package cert

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/toeshee/it-suite/agent/internal/config"
	"github.com/toeshee/it-suite/agent/internal/enroll"
	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// Job types. They match app/jobs/registry.py; a mismatch would be refused
// `unknown_type` by this agent's own runner, which is the correct answer but a
// confusing one, so the strings live in one place per side.
const (
	JobTypeRenew   = "agent.cert.renew"
	JobTypeInstall = "agent.cert.install"
)

// rfc3339 matches app/jobs/canonical.py::rfc3339 — whole seconds, literal Z.
const rfc3339 = "2006-01-02T15:04:05Z"

// installParams implements AgentCertInstallParams
// (docs/kernel/wire-protocol/AgentCertInstallParams.schema.json).
//
// `cert_serial` is on the wire and is deliberately NOT a field here: it exists
// so the control plane can record the serial string its own CA reported, and
// the endpoint has no use for it. Unknown members are ignored by encoding/json,
// which is what makes the server free to add more.
type installParams struct {
	CertPEM  string `json:"cert_pem"`
	ChainPEM string `json:"chain_pem"`
}

// Service holds what the two capabilities need: where the data directory is,
// and somewhere to log.
//
// The zero value is usable and is what `Process` is: it resolves the
// configuration from the environment the service manager started this process
// with, at job time. Tests construct one with New over a temp directory.
type Service struct {
	cfg *config.Config
	log *slog.Logger
}

// Process is the Service the default capability registry registers.
//
// Its configuration is resolved on FIRST USE, not at package init, so building
// the registry stays a pure map insert and nothing here touches the disk while
// the agent is idle.
var Process = &Service{}

// New returns a Service bound to an explicit configuration.
func New(cfg config.Config, log *slog.Logger) *Service {
	return &Service{cfg: &cfg, log: log}
}

func (s *Service) config() (config.Config, error) {
	if s.cfg != nil {
		return *s.cfg, nil
	}
	return config.Load()
}

func (s *Service) logger() *slog.Logger {
	if s.log != nil {
		return s.log
	}
	return slog.Default()
}

// Renew implements `agent.cert.renew`: generate a keypair, keep the private
// half, hand back the CSR.
//
// The job carries no parameters, and this reads none. Everything about who the
// certificate is for is the control plane's own knowledge — it signs the CSR
// for the device and org named on the job row it wrote, and never for anything
// the endpoint says.
func (s *Service) Renew(_ context.Context, job jobs.VerifiedJob) (any, error) {
	cfg, err := s.config()
	if err != nil {
		return nil, err
	}
	csrPEM, err := enroll.GenerateCSR(cfg)
	if err != nil {
		return nil, err
	}
	// The CSR is public. The key it was generated over is not, is not returned,
	// and is not logged — here or anywhere.
	s.logger().Info("certificate renewal: produced a CSR over a fresh keypair",
		"job_id", job.ID(), "device_id", job.Device())
	return map[string]any{"csr_pem": csrPEM}, nil
}

// Install implements `agent.cert.install`: validate the issued certificate and
// swap this endpoint onto it.
//
// The returned fingerprint is an ACKNOWLEDGEMENT, not a claim about identity:
// the control plane compares it with the fingerprint of the certificate IT
// issued and fails the job on any disagreement, so an agent cannot name its own
// new credential.
//
// A failure here leaves the working identity exactly as it was — that is the
// whole point of the transaction in enroll.Install — and the failed job makes
// the control plane discard the CSR and start again on the next connect.
func (s *Service) Install(_ context.Context, job jobs.VerifiedJob) (any, error) {
	cfg, err := s.config()
	if err != nil {
		return nil, err
	}
	var params installParams
	if err := json.Unmarshal(job.Params(), &params); err != nil {
		return nil, fmt.Errorf("cert: install params: %w", err)
	}
	if params.CertPEM == "" {
		return nil, fmt.Errorf("cert: install carries no certificate")
	}
	identity, err := enroll.Install(cfg, params.CertPEM, params.ChainPEM)
	if err != nil {
		return nil, err
	}
	leaf := identity.Cert.Leaf
	sum := sha256.Sum256(leaf.Raw)
	fingerprint := hex.EncodeToString(sum[:])
	s.logger().Info("certificate rotated",
		"job_id", job.ID(), "device_id", identity.DeviceID,
		"cert_fingerprint", fingerprint[:16],
		"not_after", leaf.NotAfter.UTC().Format(time.RFC3339))
	return map[string]any{
		"cert_fingerprint": fingerprint,
		"not_after":        leaf.NotAfter.UTC().Format(rfc3339),
	}, nil
}
