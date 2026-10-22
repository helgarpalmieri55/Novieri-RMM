package patch

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

type fakeRestarter struct {
	sess      session
	err       error
	scheduled []time.Duration
	messages  []string
}

func (f *fakeRestarter) Session(context.Context) session { return f.sess }
func (f *fakeRestarter) Schedule(_ context.Context, grace time.Duration, msg string) error {
	if f.err != nil {
		return f.err
	}
	f.scheduled = append(f.scheduled, grace)
	f.messages = append(f.messages, msg)
	return nil
}

func rebootService(eng *fakeEngine, r *fakeRestarter) *Service {
	s := serviceWith(eng)
	s.opts.NewRestarter = func() restarter { return r }
	s.opts.BootTime = func(now time.Time) (time.Time, bool) { return now.Add(-time.Hour), true }
	return s
}

func applyV2(t *testing.T, reboot string, deferral int, notAfter *string) json.RawMessage {
	t.Helper()
	body := map[string]any{
		"patches":        []map[string]string{ref(sourceWindowsUpdate, "KB1")},
		"reboot":         reboot,
		"deferral_hours": deferral,
		"result_version": 2,
	}
	if notAfter != nil {
		body["reboot_not_after"] = *notAfter
	}
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func kb1Engine(reboot bool) *fakeEngine {
	return &fakeEngine{
		family:    "windows",
		available: []entry{{Source: sourceWindowsUpdate, Identifier: "KB1", Title: "x", Severity: "critical"}},
		reboot:    reboot,
	}
}

func TestPlanRestartGivesASignedInUserTheDeferralAndEveryoneElseFiveMinutes(t *testing.T) {
	notAfter := testEpoch.Add(time.Hour)
	cases := []struct {
		name     string
		sess     session
		deferral int
		want     time.Duration
		tooLate  bool
	}{
		{"laptop with a user waits the deferral", session{UserPresent: true, Workstation: true}, 8, 8 * time.Hour, false},
		{"a zero deferral still warns for 15 minutes", session{UserPresent: true, Workstation: true}, 0, minUserWarning, false},
		{"a deferral past a week is capped", session{UserPresent: true, Workstation: true}, 1000, maxUserWarning, false},
		{"an empty laptop restarts after the grace", session{Workstation: true}, 24, unattendedGrace, false},
		{"a server with an RDP user still restarts in the window", session{UserPresent: true}, 24, unattendedGrace, false},
	}
	for _, c := range cases {
		p := planRestart(testEpoch, c.sess, c.deferral, &notAfter)
		if p.Grace != c.want || p.TooLate != c.tooLate {
			t.Errorf("%s: grace=%v tooLate=%v, want %v/%v", c.name, p.Grace, p.TooLate, c.want, c.tooLate)
		}
	}
	late := testEpoch.Add(time.Minute)
	if p := planRestart(testEpoch, session{}, 24, &late); !p.TooLate {
		t.Error("a server restart that would land after the window end was planned anyway")
	}
	// A user's laptop follows the user, not the window.
	if p := planRestart(testEpoch, session{UserPresent: true, Workstation: true}, 24, &late); p.TooLate {
		t.Error("a signed-in user's restart was refused for the window end")
	}
}

func TestApplyHonoursTheRebootPolicyUnderResultVersionTwo(t *testing.T) {
	cases := []struct {
		policy    string
		osWants   bool
		scheduled bool
	}{
		{"never", true, false},
		{"if_required", false, false},
		{"if_required", true, true},
		{"forced", false, true},
	}
	for _, c := range cases {
		r := &fakeRestarter{sess: session{UserPresent: true, Workstation: true}}
		res, err := rebootService(kb1Engine(c.osWants), r).apply(context.Background(),
			applyV2(t, c.policy, 4, nil))
		if err != nil {
			t.Fatalf("%s: %v", c.policy, err)
		}
		if got := len(r.scheduled) == 1; got != c.scheduled {
			t.Errorf("reboot=%s os_wants=%v: scheduled=%v, want %v", c.policy, c.osWants, got, c.scheduled)
		}
		if c.scheduled {
			if res.RebootScheduledFor == nil || *res.RebootScheduledFor != "2026-08-06T16:00:00Z" {
				t.Errorf("reboot=%s: reboot_scheduled_for=%v, want four hours on", c.policy, res.RebootScheduledFor)
			}
			if !res.RebootRequired {
				t.Errorf("reboot=%s: a restart was scheduled and reboot_required is false", c.policy)
			}
			if res.UserPresent == nil || !*res.UserPresent {
				t.Errorf("reboot=%s: user_present not reported", c.policy)
			}
		} else if res.RebootScheduledFor != nil {
			t.Errorf("reboot=%s: nothing was scheduled and the result says %s", c.policy, *res.RebootScheduledFor)
		}
	}
}

func TestAnUnattendedRestartPastTheWindowEndIsLeftToTheControlPlane(t *testing.T) {
	end := testEpoch.Add(2 * time.Minute).Format(rfc3339)
	r := &fakeRestarter{sess: session{}}
	res, err := rebootService(kb1Engine(true), r).apply(context.Background(),
		applyV2(t, "if_required", 24, &end))
	if err != nil {
		t.Fatal(err)
	}
	if len(r.scheduled) != 0 || res.RebootScheduledFor != nil {
		t.Fatal("a server restart was scheduled past the end of its maintenance window")
	}
	if !res.RebootRequired {
		t.Fatal("reboot_required must still reach the control plane so it can schedule the restart")
	}
}

func TestAFailedRestartDoesNotFailTheInstalls(t *testing.T) {
	r := &fakeRestarter{err: errors.New("access denied")}
	res, err := rebootService(kb1Engine(true), r).apply(context.Background(),
		applyV2(t, "if_required", 24, nil))
	if err != nil {
		t.Fatal(err)
	}
	if res.Results[0].State != "installed" || res.RebootScheduledFor != nil || !res.RebootRequired {
		t.Fatalf("result = %+v", res)
	}
}

func TestTheRestartMessageIsTheAgentsOwnText(t *testing.T) {
	r := &fakeRestarter{sess: session{UserPresent: true, Workstation: true}}
	_, err := rebootService(kb1Engine(true), r).apply(context.Background(),
		applyV2(t, "if_required", 2, nil))
	if err != nil || len(r.messages) != 1 {
		t.Fatalf("err=%v messages=%v", err, r.messages)
	}
	if !strings.HasPrefix(r.messages[0], "Updates installed by your IT team need a restart.") {
		t.Fatalf("message = %q", r.messages[0])
	}
}

func TestApplyRefusesAMalformedWindowEndOrNegativeDeferral(t *testing.T) {
	bad := "tomorrow"
	for _, raw := range []json.RawMessage{applyV2(t, "if_required", 24, &bad), applyV2(t, "if_required", -1, nil)} {
		eng := kb1Engine(true)
		_, err := rebootService(eng, &fakeRestarter{}).apply(context.Background(), raw)
		var refusal *jobs.Refusal
		if !errors.As(err, &refusal) {
			t.Fatalf("err = %v, want a refusal", err)
		}
		if len(eng.installs) != 0 {
			t.Fatal("a refused job installed something")
		}
	}
}

func TestTheV1ApplyDocumentCarriesNoNewMembers(t *testing.T) {
	r := &fakeRestarter{}
	res, err := rebootService(kb1Engine(true), r).apply(context.Background(),
		applyJSON(t, "if_required", ref(sourceWindowsUpdate, "KB1")))
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(res.forVersion(1))
	if strings.Contains(string(raw), "reboot_scheduled_for") || strings.Contains(string(raw), "user_present") {
		t.Fatalf("v1 document = %s", raw)
	}
}

func TestTheScanReportsBootTimeAndPendingRestartOnlyWhenItCanTell(t *testing.T) {
	eng := kb1Engine(true)
	doc, err := rebootService(eng, &fakeRestarter{}).scan(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if doc.RebootRequired == nil || !*doc.RebootRequired {
		t.Error("reboot_required was not reported")
	}
	if doc.BootedAt == nil || *doc.BootedAt != "2026-08-06T11:00:00Z" {
		t.Errorf("booted_at = %v", doc.BootedAt)
	}
	raw, _ := json.Marshal(doc.forVersion(1))
	if strings.Contains(string(raw), "booted_at") {
		t.Errorf("v1 scan document = %s", raw)
	}

	mac := newMacEngine()
	mac.run = func(context.Context, []string, string, ...string) commandResult {
		return commandResult{Stdout: "No new software available.\n"}
	}
	svc := New(Options{
		NewEngine: func(context.Context) (engine, error) { return mac, nil },
		Clock:     func() time.Time { return testEpoch },
		Logger:    quiet(),
		BootTime:  func(time.Time) (time.Time, bool) { return time.Time{}, false },
	})
	doc, err = svc.scan(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if doc.RebootRequired != nil || doc.BootedAt != nil {
		t.Errorf("macOS cannot tell at scan time; got reboot_required=%v booted_at=%v",
			doc.RebootRequired, doc.BootedAt)
	}
}

func TestPatchRebootSkipsWhenNothingIsPendingAndSchedulesOtherwise(t *testing.T) {
	r := &fakeRestarter{sess: session{UserPresent: true, Workstation: true}}
	res, err := rebootService(kb1Engine(false), r).reboot(context.Background(),
		json.RawMessage(`{"only_if_required": true, "deferral_hours": 1}`))
	if err != nil || res.State != "not_needed" || len(r.scheduled) != 0 {
		t.Fatalf("res=%+v err=%v scheduled=%v", res, err, r.scheduled)
	}

	res, err = rebootService(kb1Engine(false), r).reboot(context.Background(),
		json.RawMessage(`{"only_if_required": false, "deferral_hours": 1}`))
	if err != nil || res.State != "scheduled" || res.ScheduledFor == nil ||
		*res.ScheduledFor != "2026-08-06T13:00:00Z" || !res.UserPresent || !res.Workstation {
		t.Fatalf("res=%+v err=%v", res, err)
	}

	end := testEpoch.Add(time.Minute).Format(rfc3339)
	raw, _ := json.Marshal(map[string]any{"only_if_required": false, "not_after": end})
	res, err = rebootService(kb1Engine(true), &fakeRestarter{}).reboot(context.Background(), raw)
	if err != nil || res.State != "window_too_short" || res.ScheduledFor != nil {
		t.Fatalf("res=%+v err=%v", res, err)
	}
}

func TestPatchRebootRefusesParamsItCannotRead(t *testing.T) {
	for _, raw := range []string{`[]`, `{"not_after": "soon"}`, `{"deferral_hours": -3}`} {
		_, err := rebootService(kb1Engine(true), &fakeRestarter{}).reboot(context.Background(),
			json.RawMessage(raw))
		var refusal *jobs.Refusal
		if !errors.As(err, &refusal) {
			t.Errorf("%s: err = %v, want a refusal", raw, err)
		}
	}
}

func TestGraceMinutesRoundsUp(t *testing.T) {
	if graceMinutes(5*time.Minute) != 5 || graceMinutes(61*time.Second) != 2 || graceMinutes(0) != 1 {
		t.Fatal("graceMinutes must round up and never be zero")
	}
}
