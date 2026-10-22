//go:build winrm_e2e

package winrm

import (
	"context"
	"errors"
	"os"
	"strings"
	"testing"
)

// THE KERBEROS PUSH PATH, END TO END, AGAINST CODE THAT IS NOT OURS (plan
// Task 9, §I5): a Samba AD KDC (TGT from the password, HTTP/<fqdn> service
// ticket, AES) and deploy/ci/winrm-acceptor -- MIT Kerberos + pyspnego, which
// decrypts our sealed messages, seals its own, and checks our channel binding.
// Run by the agent-winrm CI job; every variable is REQUIRED (M5): a missing
// one is a failure, never a skip.

func e2eEnv(t *testing.T, name string) string {
	t.Helper()
	v := os.Getenv(name)
	if v == "" {
		t.Fatalf("%s is not set; the agent-winrm job must set it (a missing variable is a failure, not a skip)", name)
	}
	return v
}

// endpointFromEnv splits "host:port[/path]".
func endpointFromEnv(t *testing.T, name string, https bool) Endpoint {
	hp, path, _ := strings.Cut(e2eEnv(t, name), "/")
	ep := Endpoint{Host: hp, HTTPS: https}
	if path != "" {
		ep.Path = "/" + path
	}
	return ep
}

func TestE2E_KerberosPushPath(t *testing.T) {
	ctx := context.Background()
	fqdn := e2eEnv(t, "ITS_WINRM_E2E_FQDN")
	caPEM := e2eEnv(t, "ITS_WINRM_E2E_CA") // the acceptor's self-signed cert (5986 trust)
	sess, err := Login(Creds{
		Username: e2eEnv(t, "ITS_WINRM_E2E_USER"),
		Password: e2eEnv(t, "ITS_WINRM_E2E_PASS"),
		KDC:      e2eEnv(t, "ITS_WINRM_E2E_KDC"),
		CABundle: caPEM,
	}, fqdn)
	if err != nil {
		t.Fatalf("login: %v", err)
	}
	defer sess.Close()

	for _, ep := range []Endpoint{
		endpointFromEnv(t, "ITS_WINRM_E2E_HTTPS", true),
		endpointFromEnv(t, "ITS_WINRM_E2E_HTTP", false),
	} {
		conn, err := Dial(ctx, sess, fqdn, ep, caPEM)
		if err != nil {
			t.Fatalf("dial %+v: %v", ep, err)
		}
		out, code, err := conn.RunPowerShell(ctx, "Write-Output WINRM-OK", nil)
		if err != nil || code != 0 || (out != "WINRM-OK\r\n" && out != "WINRM-OK\n") {
			_ = conn.Close()
			t.Fatalf("run %+v: out=%q code=%d err=%v", ep, out, code, err)
		}
		// stdin (how the enrolment token travels) round-trips, sealed on 5985.
		out, code, err = conn.RunPowerShell(ctx, "[Console]::In.ReadLine()", []byte("STDIN-LINE-OK\n"))
		_ = conn.Close()
		if err != nil || code != 0 || out != "STDIN-LINE-OK\r\n" {
			t.Fatalf("stdin %+v: out=%q code=%d err=%v", ep, out, code, err)
		}
		t.Logf("%+v: Kerberos mutual auth + run + stdin OK", ep)
	}

	// The channel binding is ENFORCED, so the 5986 success above is not
	// vacuous: an acceptor bound to a different certificate must refuse us.
	_, err = Dial(ctx, sess, fqdn, endpointFromEnv(t, "ITS_WINRM_E2E_HTTPS_BADCB", true), caPEM)
	if !errors.Is(err, errKerberosRejected) {
		t.Fatalf("a mismatched channel binding was not rejected: %v", err)
	}

	// No trust anchor for the certificate: the TLS handshake fails; never
	// InsecureSkipVerify.
	if _, err := Dial(ctx, sess, fqdn, endpointFromEnv(t, "ITS_WINRM_E2E_HTTPS", true), ""); err == nil {
		t.Fatal("5986 connected without the CA bundle")
	}

	// NTLM / Basic / CredSSP-only endpoints: refused, no fallback.
	for _, v := range []string{"ITS_WINRM_E2E_NTLM", "ITS_WINRM_E2E_BASIC", "ITS_WINRM_E2E_CREDSSP"} {
		_, err = Dial(ctx, sess, fqdn, endpointFromEnv(t, v, false), caPEM)
		if !errors.Is(err, errNonKerberos) || !strings.Contains(err.Error(), "pushes use Kerberos only") {
			t.Fatalf("%s endpoint not refused: %v", v, err)
		}
	}

	// The probe's check, credential-free, against the real KDC.
	if !KDCReachable(context.Background(), RealmFor("", fqdn), e2eEnv(t, "ITS_WINRM_E2E_KDC")) {
		t.Fatal("KDCReachable = false against the running KDC")
	}

	// A wrong password never gets a TGT.
	if _, err := Login(Creds{Username: e2eEnv(t, "ITS_WINRM_E2E_USER"), Password: "wrong-password", KDC: e2eEnv(t, "ITS_WINRM_E2E_KDC")}, fqdn); err == nil {
		t.Fatal("a wrong password minted a TGT")
	}
}
