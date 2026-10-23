package pushinstall

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

// The winrm branch of the capability, with the transport faked (the Kerberos
// client itself is tested in ./winrm and against the independent acceptor in
// the agent-winrm CI job).

type fakeWinRMDialer struct {
	readiness WinRMReadiness
	stdout    string
	code      int
	runErr    error

	probeCalled bool
	probeFQDN   string
	probeAddr   string
	runCalled   bool
	runFQDN     string
	runAddr     string
	runUser     string
	runKDC      string
	runPassword []byte // copy AS SEEN
	passwordRef []byte // the capability's own buffer, to prove it is zeroed
	runScript   string
	runStdin    []byte
}

// Probe's signature has no credential parameter: that is the assertion (§C1).
func (d *fakeWinRMDialer) Probe(_ context.Context, fqdn, addr string) (WinRMReadiness, error) {
	d.probeCalled, d.probeFQDN, d.probeAddr = true, fqdn, addr
	return d.readiness, nil
}

func (d *fakeWinRMDialer) Run(_ context.Context, fqdn, addr string, cred WinRMCredential, script string, stdin []byte) (string, int, error) {
	d.runCalled, d.runFQDN, d.runAddr = true, fqdn, addr
	d.runUser, d.runKDC = cred.Username, cred.KDC
	d.runPassword = append([]byte(nil), cred.Password...)
	d.passwordRef = cred.Password
	d.runScript = script
	d.runStdin = append([]byte(nil), stdin...)
	return d.stdout, d.code, d.runErr
}

const okStdout = "ITS-PUSH-ARCH=amd64\r\nITS-PUSH-INSTALLED\r\n"

func winrmInstallParamsJSON(t *testing.T, mutate func(m map[string]any)) json.RawMessage {
	t.Helper()
	secret, _ := json.Marshal(map[string]string{"secret": "hunter2", "kdc": "dc1.corp.example.com", "ca_bundle": ""})
	return installParamsJSON(t, func(m map[string]any) {
		m["transport"] = "winrm"
		m["dns_name"] = "pc1.corp.example.com"
		m["os"] = "windows"
		m["host_key_fingerprint"] = "kerberos"
		// External object storage (W2 superseded): the presigned host is the
		// storage provider's, and the console is the separate, signed enroll_url.
		m["release"].(map[string]any)["url"] = "https://s3.eu-west-1.amazonaws.com/it-suite/its-agent/1.6.0?sig=abc"
		m["enroll_url"] = "https://console.example"
		m["credential"] = map[string]any{"username": "svc-push@corp.example.com", "secret": string(secret)}
		if mutate != nil {
			mutate(m)
		}
	})
}

func winrmService(w *fakeWinRMDialer, ssh *fakeDialer, dl *fakeDownloader) *Service {
	return New(Options{Dialer: ssh, WinRMDialer: w, Downloader: dl})
}

func TestWinRMInstallRunsTheFixedScriptAndNeverTheSSHDialer(t *testing.T) {
	w := &fakeWinRMDialer{stdout: okStdout}
	ssh := &fakeDialer{}
	s := winrmService(w, ssh, &fakeDownloader{data: artefactBytes()})
	out, err := s.install(context.Background(), winrmInstallParamsJSON(t, nil))
	if err != nil {
		t.Fatal(err)
	}
	r := out.(installResult)
	if r.Outcome != outcomeInstalled || r.OSDetected != "windows" || r.ArchDetected != "amd64" {
		t.Fatalf("result = %+v", r)
	}
	if ssh.openCalled || ssh.probeCalled {
		t.Fatal("the ssh dialer was used for a winrm job")
	}
	// Agent falsifier 4: the one address dialled is the signed one; the DNS
	// name is the Kerberos service name only.
	if w.runAddr != "10.0.4.17" || w.runFQDN != "pc1.corp.example.com" {
		t.Fatalf("dialled %s as %s", w.runAddr, w.runFQDN)
	}
	if w.runUser != "svc-push@corp.example.com" || string(w.runPassword) != "hunter2" || w.runKDC != "dc1.corp.example.com" {
		t.Fatalf("credential as seen = %q/%q/%q", w.runUser, w.runPassword, w.runKDC)
	}
	// Falsifier 3: the capability's password buffer is zeroed on return.
	if !bytes.Equal(w.passwordRef, make([]byte, len(w.passwordRef))) {
		t.Fatal("the password buffer was not zeroed")
	}
	// The script is the shared layout; the token is on stdin, not in it.
	for _, want := range []string{`Join-Path $env:ProgramFiles 'ITSuite\its-agent.exe'`, `Join-Path $env:ProgramData 'ITSuite\push'`, `$enrollUrl = 'https://console.example'`} {
		if !strings.Contains(w.runScript, want) {
			t.Errorf("script lacks %s", want)
		}
	}
	// The enroll URL is the signed enroll_url, NEVER the presigned release
	// URL's origin: on external storage that origin is the storage provider,
	// which would be sent the single-use enrollment token (W2 superseded).
	if strings.Contains(w.runScript, "$enrollUrl = 'https://s3.") {
		t.Fatal("the enroll URL was derived from the release URL's origin")
	}
	if strings.Contains(w.runScript, "SECRET-TOKEN") || strings.Contains(w.runScript, "hunter2") {
		t.Fatal("secret material in the script (its command line)")
	}
	if string(w.runStdin) != "SECRET-TOKEN\n" {
		t.Fatalf("stdin = %q, want the token", w.runStdin)
	}
}

func TestWinRMInstallVerifiesTheDigestBeforeUsingTheCredential(t *testing.T) {
	w := &fakeWinRMDialer{stdout: okStdout}
	s := winrmService(w, &fakeDialer{}, &fakeDownloader{data: []byte("tampered")})
	out, err := s.install(context.Background(), winrmInstallParamsJSON(t, nil))
	if err != nil {
		t.Fatal(err)
	}
	if r := out.(installResult); r.Outcome != outcomeFailed || !strings.Contains(r.Detail, "does not match") {
		t.Fatalf("result = %+v", r)
	}
	if w.runCalled {
		t.Fatal("the credential was presented although the artefact digest did not match")
	}
}

func TestWinRMInstallRefusesBeforeAnyDialOrDownload(t *testing.T) {
	cases := map[string]func(m map[string]any){
		"no dns_name":           func(m map[string]any) { delete(m, "dns_name") },
		"empty dns_name":        func(m map[string]any) { m["dns_name"] = "" },
		"IP literal dns_name":   func(m map[string]any) { m["dns_name"] = "10.0.4.17" },
		"dotless dns_name":      func(m map[string]any) { m["dns_name"] = "pc1" },
		"winrm to linux":        func(m map[string]any) { m["os"] = "linux" },
		"secret not JSON":       func(m map[string]any) { m["credential"] = map[string]any{"username": "u", "secret": "hunter2"} },
		"JSON without password": func(m map[string]any) { m["credential"] = map[string]any{"username": "u", "secret": `{"kdc":"x"}`} },
		"no username":           func(m map[string]any) { m["credential"].(map[string]any)["username"] = "" },
		// Review I1: the realm is the account's, and the target must be in it.
		"a NetBIOS account": func(m map[string]any) {
			m["credential"].(map[string]any)["username"] = `CORP\svc-push`
		},
		"a target outside the realm": func(m map[string]any) { m["dns_name"] = "pc1.evil.example.net" },
		// W2 superseded: no configured enroll URL means no install -- there is
		// no fallback to the release URL's origin.
		"no enroll_url":          func(m map[string]any) { delete(m, "enroll_url") },
		"null enroll_url":        func(m map[string]any) { m["enroll_url"] = nil },
		"empty enroll_url":       func(m map[string]any) { m["enroll_url"] = "" },
		"enroll_url with a path": func(m map[string]any) { m["enroll_url"] = "https://console.example/enroll/v1" },
		"plain-http enroll_url":  func(m map[string]any) { m["enroll_url"] = "http://console.example" },
	}
	for name, mutate := range cases {
		w := &fakeWinRMDialer{stdout: okStdout}
		dl := &fakeDownloader{data: artefactBytes()}
		s := winrmService(w, &fakeDialer{}, dl)
		_, err := s.install(context.Background(), winrmInstallParamsJSON(t, mutate))
		if err == nil {
			t.Errorf("%s: accepted", name)
		} else if strings.Contains(err.Error(), "hunter2") {
			t.Errorf("%s: the error echoes the password", name)
		}
		if w.runCalled || dl.called {
			t.Errorf("%s: dialled or downloaded before refusing", name)
		}
	}
}

func TestWinRMInstallClassifiesOutcomes(t *testing.T) {
	cases := []struct {
		name    string
		d       fakeWinRMDialer
		outcome string
		arch    string
	}{
		{"arch mismatch", fakeWinRMDialer{stdout: "ITS-PUSH-ARCH=arm64\r\n", code: scriptExitArchMismatch}, outcomeOSMismatch, "arm64"},
		{"target digest mismatch", fakeWinRMDialer{stdout: "ITS-PUSH-ARCH=amd64\r\n", code: scriptExitDigestMismatch}, outcomeFailed, "amd64"},
		{"no token on stdin", fakeWinRMDialer{stdout: "ITS-PUSH-ARCH=amd64\r\n", code: scriptExitNoToken}, outcomeFailed, "amd64"},
		{"a reparse point in the layout", fakeWinRMDialer{stdout: "ITS-PUSH-ARCH=amd64\r\n", code: scriptExitUnsafeDirectory}, outcomeFailed, "amd64"},
		{"a root someone else owns", fakeWinRMDialer{stdout: "ITS-PUSH-ARCH=amd64\r\n", code: scriptExitForeignRoot}, outcomeFailed, "amd64"},
		{"the placed binary changed", fakeWinRMDialer{stdout: "ITS-PUSH-ARCH=amd64\r\n", code: scriptExitInstalledMismatch}, outcomeFailed, "amd64"},
		{"no ProgramFiles/ProgramData", fakeWinRMDialer{stdout: "ITS-PUSH-ARCH=amd64\r\n", code: scriptExitNoLayout}, outcomeFailed, "amd64"},
		{"script threw", fakeWinRMDialer{stdout: "ITS-PUSH-ARCH=amd64\r\n", code: 1}, outcomeFailed, "amd64"},
		{"exit 0 without the marker", fakeWinRMDialer{stdout: "", code: 0}, outcomeFailed, ""},
		{"kerberos failure", fakeWinRMDialer{runErr: errors.Join(ErrAuth, errors.New("KDC_ERR_PREAUTH_FAILED"))}, outcomeAuthFailed, ""},
		{"nothing answered", fakeWinRMDialer{runErr: errors.New("connection refused")}, outcomeUnreach, ""},
	}
	for _, c := range cases {
		d := c.d
		s := winrmService(&d, &fakeDialer{}, &fakeDownloader{data: artefactBytes()})
		out, err := s.install(context.Background(), winrmInstallParamsJSON(t, nil))
		if err != nil {
			t.Fatalf("%s: %v", c.name, err)
		}
		r := out.(installResult)
		if r.Outcome != c.outcome || r.ArchDetected != c.arch {
			t.Errorf("%s: result = %+v, want %s/%s", c.name, r, c.outcome, c.arch)
		}
		if strings.Contains(r.Detail, "SECRET-TOKEN") || strings.Contains(r.Detail, "hunter2") {
			t.Errorf("%s: secret material in the result detail", c.name)
		}
	}
}

func TestWinRMProbeIsCredentialFreeAndReportsReadiness(t *testing.T) {
	w := &fakeWinRMDialer{readiness: WinRMReadiness{HTTPS: true, HTTP: false, KDCReachable: true}}
	ssh := &fakeDialer{}
	s := winrmService(w, ssh, &fakeDownloader{})
	raw := []byte(`{"target_address":"10.0.4.17","transport":"winrm","dns_name":"pc1.corp.example.com"}`)
	out, err := s.probe(context.Background(), raw)
	if err != nil {
		t.Fatal(err)
	}
	if !w.probeCalled || w.probeAddr != "10.0.4.17" || w.probeFQDN != "pc1.corp.example.com" || ssh.probeCalled {
		t.Fatalf("probe routing wrong: %+v", w)
	}
	b, _ := json.Marshal(out)
	var got map[string]any
	_ = json.Unmarshal(b, &got)
	if got["transport"] != "winrm" || got["winrm_https"] != true || got["kdc_reachable"] != true {
		t.Fatalf("result = %s", b)
	}
	if _, err := s.probe(context.Background(), []byte(`{"target_address":"10.0.4.17","transport":"winrm"}`)); err == nil {
		t.Fatal("a winrm probe with no DNS name was accepted")
	}
}

// THE WIRE IS A CONTRACT: an ssh probe result must be what an older control
// plane has always received -- no winrm keys appear on the ssh path.
func TestSSHProbeResultCarriesNoWinRMKeys(t *testing.T) {
	d := &fakeDialer{probeInfo: ProbeInfo{Fingerprint: goodFP, KeyType: "ssh-ed25519", Banner: "OpenSSH_9"}}
	s := New(Options{Dialer: d})
	for _, raw := range []string{
		`{"target_address":"10.0.4.17","transport":"ssh"}`,
		`{"target_address":"10.0.4.17"}`, // transport defaults to ssh (schema default)
	} {
		out, err := s.probe(context.Background(), []byte(raw))
		if err != nil {
			t.Fatal(err)
		}
		b, _ := json.Marshal(out)
		for _, k := range []string{"winrm_https", "winrm_http", "kdc_reachable"} {
			if bytes.Contains(b, []byte(k)) {
				t.Fatalf("ssh probe result carries %s: %s", k, b)
			}
		}
	}
}

func TestUnknownTransportIsRefused(t *testing.T) {
	s := New(Options{Dialer: &fakeDialer{}, WinRMDialer: &fakeWinRMDialer{}})
	if _, err := s.probe(context.Background(), []byte(`{"target_address":"10.0.4.17","transport":"smb"}`)); err == nil {
		t.Fatal("probe accepted transport smb")
	}
	if _, err := s.install(context.Background(), installParamsJSON(t, func(m map[string]any) { m["transport"] = "smb" })); err == nil {
		t.Fatal("install accepted transport smb")
	}
}

func TestWinRMInstallNamesWhatTheScriptRefused(t *testing.T) {
	for code, want := range map[int]string{
		scriptExitUnsafeDirectory:   "reparse point",
		scriptExitInstalledMismatch: "does not match the signed SHA-256 after it was placed",
		scriptExitNoLayout:          "%ProgramFiles%",
		scriptExitForeignRoot:       "owned by an account other than Administrators or SYSTEM",
	} {
		d := &fakeWinRMDialer{stdout: "ITS-PUSH-ARCH=amd64\r\n", code: code}
		s := winrmService(d, &fakeDialer{}, &fakeDownloader{data: artefactBytes()})
		out, err := s.install(context.Background(), winrmInstallParamsJSON(t, nil))
		if err != nil {
			t.Fatal(err)
		}
		if r := out.(installResult); !strings.Contains(r.Detail, want) {
			t.Errorf("exit %d: detail %q lacks %q", code, r.Detail, want)
		}
	}
}
