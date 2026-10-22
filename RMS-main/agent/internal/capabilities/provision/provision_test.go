package provision

import (
	"bytes"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// ---------------------------------------------------------------------------
// Apparatus
// ---------------------------------------------------------------------------

// fakeHost is the OS seam. Every branch of run() is reachable through it on a
// Linux container that is neither Windows nor macOS.
type fakeHost struct {
	name    string
	nameErr error
	domain  DomainStatus
	domErr  error
	calls   int
}

func (h *fakeHost) Hostname() (string, error) { h.calls++; return h.name, h.nameErr }

func (h *fakeHost) DomainStatus() (DomainStatus, error) { h.calls++; return h.domain, h.domErr }

func quiet() *slog.Logger {
	return slog.New(slog.NewTextHandler(discardWriter{}, &slog.HandlerOptions{Level: slog.LevelError}))
}

type discardWriter struct{}

func (discardWriter) Write(p []byte) (int, error) { return len(p), nil }

func healthy(time.Time) (bool, string) { return true, "" }

// paramsJSON builds a ProvisionEnrollParams document. `extra` is merged last so
// a test can plant members the schema does not define.
func paramsJSON(t *testing.T, hostname string, post map[string]any, extra map[string]any) json.RawMessage {
	t.Helper()
	doc := map[string]any{
		"provision_run_id":       "0198c0de-0000-7000-8000-000000000001",
		"os_family":              thisOSFamily(),
		"hostname":               hostname,
		"post_config":            post,
		"expected_agent_version": nil,
	}
	for k, v := range extra {
		doc[k] = v
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		t.Fatalf("marshalling params: %v", err)
	}
	return raw
}

func runWith(t *testing.T, s *Service, raw json.RawMessage) *result {
	t.Helper()
	res, err := s.run(raw)
	if err != nil {
		t.Fatalf("run: %v", err)
	}
	return res
}

// ---------------------------------------------------------------------------
// The baseline itself
// ---------------------------------------------------------------------------

func TestAMachineThatCameUpToBaselineReportsSuccess(t *testing.T) {
	h := &fakeHost{name: "WS-0142"}
	s := New(Options{Host: h, Health: healthy, Version: "1.4.0", Logger: quiet()})

	res := runWith(t, s, paramsJSON(t, "ws-0142", map[string]any{"hostname": "ws-0142"}, nil))

	if res.State != stateSuccess {
		t.Fatalf("state = %q with error %v; a machine whose hostname matches and whose agent is "+
			"healthy is the success case", res.State, deref(res.Error))
	}
	if !res.HostnameSet || !res.AgentHealthy {
		t.Fatalf("hostname_set=%v agent_healthy=%v; both are true here", res.HostnameSet, res.AgentHealthy)
	}
	if res.DomainJoined != nil {
		t.Fatalf("domain_joined = %v; this profile asked for no domain join, and NULL is the only "+
			"answer that does not claim a check that never happened", *res.DomainJoined)
	}
	if res.Error != nil {
		t.Fatalf("error = %q on a successful run", *res.Error)
	}
}

func TestAWrongHostnameIsASucceededJobCarryingStateFailed(t *testing.T) {
	// THE DECISION THIS TEST PINS: `handle_provision_result` is a RESULT
	// HANDLER, and app/jobs/registry.py invokes those only "after a succeeded
	// result". A returned error here would never reach
	// `ingest_provision_result`, so the ProvisionRun would sit on `enrolled`
	// forever -- no terminal state, no error text, and the answer-file cleanup
	// that keys off a terminal state never runs, leaving a secret-bearing
	// artifact in object storage.
	//
	// FALSIFYING OBSERVATION: run() returning a non-nil error for a baseline
	// miss, or a result whose state is not "failed".
	h := &fakeHost{name: "WIN-8FJ29DKS"} // the image's unattended default
	s := New(Options{Host: h, Health: healthy, Logger: quiet()})

	res, err := s.run(paramsJSON(t, "ws-0142", map[string]any{"hostname": "ws-0142"}, nil))
	if err != nil {
		t.Fatalf("run returned %v; a baseline miss must be a SUCCEEDED job with state=failed, "+
			"or the run never leaves `enrolled`", err)
	}
	if res.State != stateFailed {
		t.Fatalf("state = %q, want %q", res.State, stateFailed)
	}
	if res.HostnameSet {
		t.Fatal("hostname_set = true when the machine is called WIN-8FJ29DKS and the profile " +
			"asked for ws-0142")
	}
	if res.Error == nil || !strings.Contains(*res.Error, "WIN-8FJ29DKS") ||
		!strings.Contains(*res.Error, "ws-0142") {
		t.Fatalf("error = %v; the operator cannot fix this without BOTH the name the machine has "+
			"and the name it should have", deref(res.Error))
	}
}

func TestHostnameComparisonIsPerLabelAndCaseInsensitiveOnBothFleetPlatforms(t *testing.T) {
	// Windows uppercases the NetBIOS name whatever the profile wrote; a bound
	// Mac answers with an FQDN. Comparing the full strings would mark every
	// correctly provisioned machine on one of the two fleet platforms as
	// failed, forever, and the run's `error` would name a hostname that IS the
	// hostname.
	//
	// FALSIFYING OBSERVATION: a case-only or suffix-only difference reported as
	// a mismatch, or a genuinely different name reported as a match.
	for _, tc := range []struct {
		want, actual string
		match        bool
	}{
		{"ws-0142", "WS-0142", true},                      // windows, uppercased
		{"ws-0142", "ws-0142.local", true},                // macOS, bonjour
		{"ws-0142", "ws-0142.corp.toeshee.local", true},   // macOS, AD-bound
		{"WS-0142", "ws-0142", true},                      // profile shouted
		{"ws-0142", "ws-0143", false},                     // a DIFFERENT machine
		{"ws-0142", "ws-01420", false},                    // prefix is not a match
		{"ws-0142", "", false},                            // OS said nothing
		{"ws-0142", "ws-0142x.corp.toeshee.local", false}, // first label differs
	} {
		if got := hostnameMatches(tc.want, tc.actual); got != tc.match {
			t.Errorf("hostnameMatches(%q, %q) = %v, want %v", tc.want, tc.actual, got, tc.match)
		}
	}
}

// ---------------------------------------------------------------------------
// Domain membership: three states, and NULL is one of them
// ---------------------------------------------------------------------------

func TestAJoinedMachineReportsDomainJoinedTrueAndAnUnjoinedOneReportsFalse(t *testing.T) {
	post := map[string]any{"hostname": "ws-0142", "domain_join": map[string]any{
		"domain": "corp.toeshee.local", "ou": "OU=Workstations,DC=corp,DC=toeshee,DC=local"}}

	joined := New(Options{
		Host:   &fakeHost{name: "ws-0142", domain: DomainStatus{Supported: true, Joined: true, Domain: "CORP"}},
		Health: healthy, Logger: quiet(),
	})
	res := runWith(t, joined, paramsJSON(t, "ws-0142", post, nil))
	if res.DomainJoined == nil || !*res.DomainJoined {
		t.Fatalf("domain_joined = %v for a machine NetGetJoinInformation reports as joined to "+
			"CORP against a profile asking for corp.toeshee.local. The NetBIOS/DNS split is not "+
			"a mismatch", res.DomainJoined)
	}
	if res.State != stateSuccess {
		t.Fatalf("state = %q (%v)", res.State, deref(res.Error))
	}

	unjoined := New(Options{
		Host:   &fakeHost{name: "ws-0142", domain: DomainStatus{Supported: true, Joined: false, Domain: "WORKGROUP"}},
		Health: healthy, Logger: quiet(),
	})
	res = runWith(t, unjoined, paramsJSON(t, "ws-0142", post, nil))
	if res.DomainJoined == nil || *res.DomainJoined {
		t.Fatalf("domain_joined = %v for a machine in WORKGROUP", res.DomainJoined)
	}
	if res.State != stateFailed {
		t.Fatalf("state = %q; a profile that asked for a domain join and did not get one has not "+
			"reached baseline", res.State)
	}
	if res.Error == nil || !strings.Contains(*res.Error, "WORKGROUP") {
		t.Fatalf("error = %v; it must name the membership actually found", deref(res.Error))
	}
}

func TestAnEndpointThatCannotBeAskedReportsNullAndNeverFalse(t *testing.T) {
	// THE DISTINCTION THIS TEST EXISTS FOR. `ingest_provision_result` writes
	// `domain_joined` straight into the hash-chained audit metadata. `false`
	// there says "this machine was checked and is not in the domain"; `null`
	// says "nobody could check". An auditor reading a SOC 2 evidence trail a
	// year later cannot tell them apart if the agent collapses them.
	//
	// FALSIFYING OBSERVATION: domain_joined non-nil when DomainStatus reports
	// Supported=false or errors.
	post := map[string]any{"domain_join": map[string]any{"domain": "corp.toeshee.local"}}

	for name, host := range map[string]*fakeHost{
		"unsupported": {name: "ws-0142", domain: DomainStatus{Supported: false}},
		"errored":     {name: "ws-0142", domErr: errors.New("permission denied")},
	} {
		s := New(Options{Host: host, Health: healthy, Logger: quiet()})
		res := runWith(t, s, paramsJSON(t, "ws-0142", post, nil))
		if res.DomainJoined != nil {
			t.Errorf("%s: domain_joined = %v; a check that could not run must report NULL, not a "+
				"claim about the machine", name, *res.DomainJoined)
		}
		if res.State != stateFailed {
			t.Errorf("%s: state = %q; a profile asked for a join that could not be confirmed, "+
				"which is not a baseline reached", name, res.State)
		}
	}
}

func TestAProfileWithNoDomainJoinNeverAsksTheOSAboutOne(t *testing.T) {
	// Dormancy in the small: the domain probe is a DLL call on Windows and a
	// directory read on macOS, and a profile that asked for no join must pay
	// neither. `calls` counts BOTH Host methods, so 1 is the hostname read and
	// nothing else.
	//
	// FALSIFYING OBSERVATION: more than one Host call for a params document
	// whose post_config has no `domain_join` member.
	h := &fakeHost{name: "ws-0142"}
	s := New(Options{Host: h, Health: healthy, Logger: quiet()})
	runWith(t, s, paramsJSON(t, "ws-0142", map[string]any{"hostname": "ws-0142"}, nil))
	if h.calls != 1 {
		t.Fatalf("the Host seam was called %d times; a profile with no domain_join must cost "+
			"exactly one call (the hostname)", h.calls)
	}
}

func TestAnEmptyHostnameIsNoRequirementRatherThanAFailedOne(t *testing.T) {
	// `dispatch_provision_enroll` writes `hostname = post_config.get("hostname")
	// or ""`, so a profile that does not rename its machines sends "". Reporting
	// hostname_set=false there would fail every run of every such profile.
	//
	// FALSIFYING OBSERVATION: hostname_set=false, or the Host being asked at
	// all, for an empty requested hostname.
	h := &fakeHost{name: "whatever-the-image-called-it"}
	s := New(Options{Host: h, Health: healthy, Logger: quiet()})
	res := runWith(t, s, paramsJSON(t, "", map[string]any{}, nil))
	if !res.HostnameSet || res.State != stateSuccess {
		t.Fatalf("hostname_set=%v state=%q for a profile that requested no rename",
			res.HostnameSet, res.State)
	}
	if h.calls != 0 {
		t.Fatalf("the Host was asked %d times for a hostname nobody requested", h.calls)
	}
}

// ---------------------------------------------------------------------------
// Health and the pinned version
// ---------------------------------------------------------------------------

func TestAnUnhealthyAgentFailsTheRunAndSaysWhy(t *testing.T) {
	s := New(Options{
		Host:   &fakeHost{name: "ws-0142"},
		Health: func(time.Time) (bool, string) { return false, "the agent's certificate has expired" },
		Logger: quiet(),
	})
	res := runWith(t, s, paramsJSON(t, "ws-0142", map[string]any{}, nil))
	if res.AgentHealthy {
		t.Fatal("agent_healthy = true when the health probe said otherwise")
	}
	if res.State != stateFailed || res.Error == nil ||
		!strings.Contains(*res.Error, "certificate has expired") {
		t.Fatalf("state=%q error=%v", res.State, deref(res.Error))
	}
}

func TestAPinnedAgentVersionIsComparedAndAMismatchNamesBothVersions(t *testing.T) {
	s := New(Options{Host: &fakeHost{name: "ws-0142"}, Health: healthy, Version: "1.4.0", Logger: quiet()})

	same := runWith(t, s, paramsJSON(t, "ws-0142", map[string]any{},
		map[string]any{"expected_agent_version": "1.4.0"}))
	if same.State != stateSuccess {
		t.Fatalf("state = %q for a build that IS the pinned version (%v)", same.State, deref(same.Error))
	}

	other := runWith(t, s, paramsJSON(t, "ws-0142", map[string]any{},
		map[string]any{"expected_agent_version": "1.5.0"}))
	if other.State != stateFailed {
		t.Fatalf("state = %q; a golden image carrying the wrong agent build has not reached "+
			"baseline", other.State)
	}
	if other.Error == nil || !strings.Contains(*other.Error, "1.4.0") ||
		!strings.Contains(*other.Error, "1.5.0") {
		t.Fatalf("error = %v; it must name the version this build IS and the one that was "+
			"expected", deref(other.Error))
	}
}

func TestAnAbsentExpectedVersionIsNotAnEmptyOne(t *testing.T) {
	// `expected_agent_version` defaults to null. If "absent" collapsed into
	// "the empty string", every job from a control plane that omits the member
	// would be a version mismatch against a build whose version is "dev".
	//
	// FALSIFYING OBSERVATION: state=failed for a params document with no
	// expected_agent_version.
	s := New(Options{Host: &fakeHost{name: "ws-0142"}, Health: healthy, Version: "1.4.0", Logger: quiet()})
	res := runWith(t, s, paramsJSON(t, "ws-0142", map[string]any{}, nil))
	if res.State != stateSuccess {
		t.Fatalf("state = %q (%v) for params with expected_agent_version: null",
			res.State, deref(res.Error))
	}
}

func TestAnUnstampedBuildComparesAgainstMainsOwnDefault(t *testing.T) {
	// A Service with no Version must compare against "dev" -- what main.go's
	// `var version = "dev"` makes `--version` print for an unstamped build --
	// and not against "".
	s := New(Options{Host: &fakeHost{name: "ws-0142"}, Health: healthy, Logger: quiet()})
	if got := s.version(); got != "dev" {
		t.Fatalf("an unwired Service reports version %q; main.go's unstamped default is %q",
			got, unstampedVersion)
	}
	res := runWith(t, s, paramsJSON(t, "ws-0142", map[string]any{},
		map[string]any{"expected_agent_version": "dev"}))
	if res.State != stateSuccess {
		t.Fatalf("state = %q (%v)", res.State, deref(res.Error))
	}
}

// ---------------------------------------------------------------------------
// Refusal: the two cases that are NOT a baseline miss
// ---------------------------------------------------------------------------

func TestAJobForAnotherOSIsRefusedAndVerifiesNothing(t *testing.T) {
	// A `provision.enroll` whose os_family is not this machine's means the run
	// bound a device it did not image. Refusing leaves the run visibly stuck on
	// `enrolled`, which is the correct outcome: marking it `failed` would file a
	// binding bug as a provisioning outcome and close it.
	//
	// FALSIFYING OBSERVATION: a *result* returned, or the Host consulted at all.
	h := &fakeHost{name: "ws-0142"}
	s := New(Options{Host: h, Health: healthy, Logger: quiet()})

	raw := paramsJSON(t, "ws-0142", map[string]any{}, map[string]any{"os_family": "os/2"})
	res, err := s.run(raw)
	if res != nil {
		t.Fatalf("a job for another OS produced a result: %+v", res)
	}
	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Fatalf("err = %v (%T); this must be a jobs.Refusal, because `failed` reads to the "+
			"control plane as 'try again' and this will never succeed", err, err)
	}
	if h.calls != 0 {
		t.Fatalf("the Host was consulted %d times before the OS family was checked; a post-config "+
			"check against the wrong OS produces a confident, wrong answer", h.calls)
	}
	if !strings.Contains(refusal.Reason, thisOSFamily()) {
		t.Fatalf("the refusal reason %q does not name what this endpoint actually is", refusal.Reason)
	}
}

func TestParamsThatAreNotAProvisionEnrollParamsAreRefused(t *testing.T) {
	s := New(Options{Host: &fakeHost{}, Health: healthy, Logger: quiet()})
	for name, raw := range map[string]string{
		"not an object":  `["provision_run_id"]`,
		"not json":       `{`,
		"no os_family":   `{"provision_run_id":"x","hostname":"ws-0142","post_config":{}}`,
		"null os_family": `{"provision_run_id":"x","os_family":null,"hostname":"h","post_config":{}}`,
	} {
		_, err := s.run(json.RawMessage(raw))
		var refusal *jobs.Refusal
		if !errors.As(err, &refusal) {
			t.Errorf("%s: err = %v (%T), want a jobs.Refusal", name, err, err)
		}
	}
}

func TestARefusalReasonNeverCarriesAnUnboundedJobSuppliedString(t *testing.T) {
	// The reason travels into the server's hash-chained audit log through
	// JobEngine.refuse. The server sanitises too, and `os_family` is already
	// constrained by the exported schema -- but "the other side checks it" is
	// how a megabyte of attacker-chosen text ends up in an audit record.
	//
	// FALSIFYING OBSERVATION: the refusal reason containing the 4096th
	// character of the planted family, or any control character from it.
	planted := strings.Repeat("A", 4000) + "\n\x00SENTINEL"
	s := New(Options{Host: &fakeHost{}, Health: healthy, Logger: quiet()})
	_, err := s.run(paramsJSON(t, "ws-0142", map[string]any{},
		map[string]any{"os_family": planted}))
	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Fatalf("err = %v, want a refusal", err)
	}
	if strings.Contains(refusal.Reason, "SENTINEL") {
		t.Fatalf("the refusal echoed 4008 characters of job-supplied text: %q", refusal.Reason)
	}
	if strings.ContainsAny(refusal.Reason, "\n\x00") {
		t.Fatalf("the refusal reason carries a control character from the job: %q", refusal.Reason)
	}
}

// ---------------------------------------------------------------------------
// The wrapper, and the counter dormancy is observed through
// ---------------------------------------------------------------------------

func TestTheCapabilityTakesAVerifiedJobAndNothingElse(t *testing.T) {
	// A compile-time fact written as a test so its failure names the reason:
	// jobs.VerifiedJob is an interface with an unexported method and no
	// exported implementation, so Enroll is unreachable except through the
	// signed-job runner. There is no second entry point into this capability,
	// and -- see no_second_enrollment_test.go -- no entry point out of it into
	// enrollment.
	var _ jobs.Handler = Process.Enroll
}

func TestOnlyCompletedVerificationsAreCounted(t *testing.T) {
	// Runs() is what a dormancy assertion outside this package observes. A
	// refused job verified nothing and must not move it.
	s := New(Options{Host: &fakeHost{name: "ws-0142"}, Health: healthy, Logger: quiet()})
	if s.Runs() != 0 {
		t.Fatalf("a fresh Service reports %d runs", s.Runs())
	}
	runWith(t, s, paramsJSON(t, "ws-0142", map[string]any{}, nil))
	if s.Runs() != 1 {
		t.Fatalf("Runs() = %d after one verification", s.Runs())
	}
	_, _ = s.run(paramsJSON(t, "ws-0142", map[string]any{}, map[string]any{"os_family": "os/2"}))
	if s.Runs() != 1 {
		t.Fatalf("Runs() = %d; a refused job verified nothing", s.Runs())
	}
}

func TestTheResultMarshalsWithEveryKeyIncludingTheNullOnes(t *testing.T) {
	// ProvisionEnrollResult is extra="forbid" with null defaults on
	// `domain_joined` and `error`. An omitempty on either would make a dropped
	// key and an explicit null two different documents for the same fact; the
	// server accepts both, so the drift would be silent until someone read the
	// audit metadata.
	//
	// FALSIFYING OBSERVATION: a marshalled result missing any of the five keys.
	s := New(Options{Host: &fakeHost{name: "ws-0142"}, Health: healthy, Logger: quiet()})
	res := runWith(t, s, paramsJSON(t, "ws-0142", map[string]any{}, nil))
	raw, err := json.Marshal(res)
	if err != nil {
		t.Fatalf("marshalling the result: %v", err)
	}
	var got map[string]json.RawMessage
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"state", "hostname_set", "domain_joined", "agent_healthy", "error"} {
		if _, ok := got[key]; !ok {
			t.Errorf("the marshalled result has no %q key: %s", key, raw)
		}
	}
	if string(got["domain_joined"]) != "null" || string(got["error"]) != "null" {
		t.Errorf("domain_joined=%s error=%s; both are null on this run",
			got["domain_joined"], got["error"])
	}
}

// ---------------------------------------------------------------------------
// Invariant #3: nothing from post_config reaches a log line
// ---------------------------------------------------------------------------

func TestNoPartOfPostConfigReachesALogLineEvenWhenItCarriesCredentials(t *testing.T) {
	// `_non_secret_post_config` strips `domain_join.password` and
	// `local_admin.password` server-side before the job is signed, so in the
	// normal path there is nothing here to leak. That is a property of the
	// SERVER. This test plants the credentials anyway, because an agent that
	// logged whatever arrived would leak them the day that stripping regressed
	// -- and provisioning credentials are domain-join credentials.
	//
	// FALSIFYING OBSERVATION: the sentinel appearing anywhere in the captured
	// log at any level, including Debug.
	const sentinel = "hunter2-DOMAIN-JOIN-SECRET"
	var buf bytes.Buffer
	log := slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug}))

	s := New(Options{Host: &fakeHost{name: "wrong-name"}, Health: healthy, Logger: log})
	post := map[string]any{
		"hostname":     "ws-0142",
		"domain_join":  map[string]any{"domain": "corp.toeshee.local", "password": sentinel},
		"local_admin":  map[string]any{"username": "toeshee-admin", "password": sentinel},
		"unknown_v2":   map[string]any{"secret_token": sentinel},
		"answer_bytes": sentinel,
	}
	res := runWith(t, s, paramsJSON(t, "ws-0142", post, nil))

	if got := buf.String(); strings.Contains(got, sentinel) {
		t.Fatalf("a provisioning credential reached the agent log:\n%s", got)
	}
	if res.Error != nil && strings.Contains(*res.Error, sentinel) {
		t.Fatalf("a provisioning credential reached the result error, which is written into the "+
			"hash-chained audit metadata: %q", *res.Error)
	}
	if buf.Len() == 0 {
		t.Fatal("nothing was logged at all; this test would pass on a capability that never runs")
	}
}

func TestUnknownPostConfigMembersAreNeverUnmarshalledAtAll(t *testing.T) {
	// The structural half of the test above: `post_config` is
	// map[string]json.RawMessage, so a member this package does not read is
	// never decoded into anything. A future change to map[string]any would make
	// every member a live Go value one fmt.Sprintf away from a log line, and
	// this test is what fails when that happens.
	//
	// FALSIFYING OBSERVATION: `post_config` decoding into a type whose element
	// is not json.RawMessage.
	var p params
	if err := json.Unmarshal(
		paramsJSON(t, "h", map[string]any{"a": map[string]any{"b": 1}}, nil), &p); err != nil {
		t.Fatal(err)
	}
	if _, ok := any(p.PostConfig).(map[string]json.RawMessage); !ok {
		t.Fatalf("params.PostConfig is %T; it must stay map[string]json.RawMessage so members "+
			"this package does not read are never decoded", p.PostConfig)
	}
	if string(p.PostConfig["a"]) != `{"b":1}` {
		t.Fatalf("post_config member is %s; it should be the untouched bytes", p.PostConfig["a"])
	}
}

func TestRequestedDomainReadsExactlyOneMemberOfPostConfig(t *testing.T) {
	for name, tc := range map[string]struct {
		post      map[string]json.RawMessage
		want      string
		requested bool
	}{
		"absent":     {map[string]json.RawMessage{"hostname": json.RawMessage(`"h"`)}, "", false},
		"present":    {map[string]json.RawMessage{"domain_join": json.RawMessage(`{"domain":"corp.toeshee.local"}`)}, "corp.toeshee.local", true},
		"no domain":  {map[string]json.RawMessage{"domain_join": json.RawMessage(`{"ou":"OU=X"}`)}, "", true},
		"not object": {map[string]json.RawMessage{"domain_join": json.RawMessage(`true`)}, "", true},
		"null":       {map[string]json.RawMessage{"domain_join": json.RawMessage(`null`)}, "", true},
	} {
		got, requested := requestedDomain(tc.post)
		if got != tc.want || requested != tc.requested {
			t.Errorf("%s: requestedDomain = (%q, %v), want (%q, %v)",
				name, got, requested, tc.want, tc.requested)
		}
	}
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func deref(s *string) string {
	if s == nil {
		return "<nil>"
	}
	return *s
}
