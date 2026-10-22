package patch

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// Restarts: `patch.apply`'s reboot policy and the `patch.reboot` job.
//
// The agent never restarts on its own initiative. A restart happens only inside
// a verified job -- a `patch.apply` whose policy asks for one, or a
// `patch.reboot` the control plane dispatched inside the device's maintenance
// window -- and the result says when it will happen, so the control plane can
// tell a restarting device from a lost one.
//
// There is no user-session helper, so the warning is the OS's own restart
// notification: `shutdown /r /t` on Windows, `shutdown -r +minutes` on macOS
// and Linux. The message is this agent's text; nothing job-supplied reaches the
// command line except numbers this package computed and bounded.

// JobTypeReboot matches app/modules/patch/handler.py's registration.
const JobTypeReboot = "patch.reboot"

// resultVersion is the PatchApplyParams / PatchScanParams `result_version` at
// which the control plane understands the members this build adds. Below it
// this agent emits the v1 documents and never restarts, because an older
// control plane rejects unknown members and cannot see a restart.
const resultVersion = 2

const (
	// minUserWarning is the least notice a signed-in user gets, whatever the
	// deferral says.
	minUserWarning = 15 * time.Minute
	// maxUserWarning bounds a deferral; past a week the notice is noise.
	maxUserWarning = 7 * 24 * time.Hour
	// unattendedGrace is the notice when nobody is signed in, or on a server.
	unattendedGrace = 5 * time.Minute
)

// session is who is on the machine when the restart is planned.
type session struct {
	UserPresent bool
	// Workstation is a laptop or desktop. Servers restart only inside their
	// maintenance window and never wait for a user.
	Workstation bool
}

// restarter is the per-OS half. Created inside a job, never cached.
type restarter interface {
	Session(ctx context.Context) session
	// Schedule asks the OS to restart after grace, with message in the OS's
	// own restart notification.
	Schedule(ctx context.Context, grace time.Duration, message string) error
}

// restartPlan is what planRestart decided.
type restartPlan struct {
	Grace   time.Duration
	At      time.Time
	Session session
	// TooLate is set when the restart would land after notAfter.
	TooLate bool
}

// planRestart decides when to restart. A signed-in user on a workstation gets
// the deferral as notice (at least 15 minutes, at most a week) and the window
// end does not apply: a laptop follows its user. Everyone else gets five
// minutes, and only if that still lands inside the window.
func planRestart(now time.Time, s session, deferralHours int, notAfter *time.Time) restartPlan {
	if s.UserPresent && s.Workstation {
		grace := time.Duration(deferralHours) * time.Hour
		if grace < minUserWarning {
			grace = minUserWarning
		}
		if grace > maxUserWarning {
			grace = maxUserWarning
		}
		return restartPlan{Grace: grace, At: now.Add(grace), Session: s}
	}
	p := restartPlan{Grace: unattendedGrace, At: now.Add(unattendedGrace), Session: s}
	if notAfter != nil && p.At.After(*notAfter) {
		p.TooLate = true
	}
	return p
}

// restartMessage is the text the OS shows. Local time, because it is read by
// the person at the machine.
func restartMessage(at time.Time) string {
	local := at.Local()
	return "Updates installed by your IT team need a restart. This computer will restart at " +
		local.Format("15:04 on Mon 2 Jan") + ". Save your work; you can restart sooner yourself."
}

// graceMinutes rounds up, so the restart is never earlier than planned.
func graceMinutes(grace time.Duration) int {
	m := int((grace + time.Minute - 1) / time.Minute)
	if m < 1 {
		m = 1
	}
	return m
}

// parseNotAfter reads an optional RFC3339 bound. Absent is nil; malformed is a
// refusal, because a restart bound this agent cannot read is not one it may
// guess at.
func parseNotAfter(raw *string, jobType string) (*time.Time, error) {
	if raw == nil || strings.TrimSpace(*raw) == "" {
		return nil, nil
	}
	t, err := time.Parse(time.RFC3339, *raw)
	if err != nil {
		return nil, jobs.Refuse("%s: not_after is not an RFC3339 time; nothing was restarted", jobType)
	}
	return &t, nil
}

func (s *Service) restarter() restarter {
	if s.opts.NewRestarter != nil {
		return s.opts.NewRestarter()
	}
	return osRestarter()
}

// scheduleRestart plans and, unless it would land outside the window, asks the
// OS to restart. It returns the plan and whether a restart was scheduled.
func (s *Service) scheduleRestart(
	ctx context.Context, deferralHours int, notAfter *time.Time,
) (restartPlan, bool, error) {
	r := s.restarter()
	plan := planRestart(s.now(), r.Session(ctx), deferralHours, notAfter)
	if plan.TooLate {
		return plan, false, nil
	}
	if err := r.Schedule(ctx, plan.Grace, restartMessage(plan.At)); err != nil {
		return plan, false, err
	}
	s.logger().Info("patch: restart scheduled", "at", plan.At.UTC().Format(rfc3339),
		"user_present", plan.Session.UserPresent, "workstation", plan.Session.Workstation)
	return plan, true, nil
}

// ---------------------------------------------------------------------------
// patch.reboot
// ---------------------------------------------------------------------------

// rebootParams implements PatchRebootParams.schema.json.
type rebootParams struct {
	// OnlyIfRequired skips the restart when the OS says none is pending.
	// Absent means true.
	OnlyIfRequired *bool   `json:"only_if_required"`
	DeferralHours  *int    `json:"deferral_hours"`
	NotAfter       *string `json:"not_after"`
}

// rebootResult implements PatchRebootResult.schema.json.
type rebootResult struct {
	// State is scheduled, not_needed or window_too_short.
	State        string  `json:"state"`
	ScheduledFor *string `json:"scheduled_for"`
	UserPresent  bool    `json:"user_present"`
	Workstation  bool    `json:"workstation"`
}

// Reboot is the `patch.reboot` capability.
func (s *Service) Reboot(ctx context.Context, job jobs.VerifiedJob) (any, error) {
	ctx, cancel := context.WithTimeout(ctx, scanTimeout)
	defer cancel()
	return s.reboot(ctx, job.Params())
}

func (s *Service) reboot(ctx context.Context, raw json.RawMessage) (*rebootResult, error) {
	var p rebootParams
	if err := json.Unmarshal(raw, &p); err != nil {
		return nil, jobs.Refuse("patch.reboot: params are not a PatchRebootParams; nothing was restarted")
	}
	deferral := defaultDeferralHours
	if p.DeferralHours != nil {
		deferral = *p.DeferralHours
	}
	if deferral < 0 {
		return nil, jobs.Refuse("patch.reboot: deferral_hours is negative; nothing was restarted")
	}
	notAfter, err := parseNotAfter(p.NotAfter, JobTypeReboot)
	if err != nil {
		return nil, err
	}

	if p.OnlyIfRequired == nil || *p.OnlyIfRequired {
		required, known := s.pendingReboot(ctx)
		if known && !required {
			return &rebootResult{State: "not_needed"}, nil
		}
	}

	plan, scheduled, err := s.scheduleRestart(ctx, deferral, notAfter)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", JobTypeReboot, err)
	}
	res := &rebootResult{
		State:       "window_too_short",
		UserPresent: plan.Session.UserPresent,
		Workstation: plan.Session.Workstation,
	}
	if scheduled {
		at := plan.At.UTC().Format(rfc3339)
		res.State, res.ScheduledFor = "scheduled", &at
	}
	return res, nil
}

// pendingReboot asks the OS engine whether a restart is pending. known is
// false when this OS cannot say (macOS outside an install).
func (s *Service) pendingReboot(ctx context.Context) (required, known bool) {
	eng, err := s.resolveEngine(ctx)
	if err != nil {
		return false, false
	}
	defer eng.Close()
	if rebootBlind(eng) {
		return false, false
	}
	return eng.RebootRequired(ctx), true
}

// rebootBlindAtScan marks an engine that can only report a restart its own
// installs asked for, so outside an apply it cannot say either way.
type rebootBlindAtScan interface{ rebootBlindAtScan() }
