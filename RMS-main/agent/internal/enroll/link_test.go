package enroll

import (
	"context"
	"testing"
)

// The install link (spec 2026-09-28 §1.4) reuses the installer's token file:
// the self-installer and install.sh write the link code where a token would
// go, and the agent tells the two apart by SHAPE. A token is
// `<uuid>.<secret>` and always has a dot; a link code is 32 URL-safe
// characters and never does.

const linkCode = "AbCdEfGhIjKlMnOpQrStUvWxYz012_-9"

func TestIsLinkCode(t *testing.T) {
	for s, want := range map[string]bool{
		linkCode: true,
		"0192f000-0000-7000-8000-00000000e2e0.s3c": false, // a token
		linkCode[:31]:                      false,
		linkCode + "A":                     false,
		"AbCdEfGhIjKlMnOpQrStUvWxYz012_-.": false,
		"":                                 false,
	} {
		if got := IsLinkCode(s); got != want {
			t.Errorf("IsLinkCode(%q) = %v, want %v", s, got, want)
		}
	}
}

func TestALinkCodeInTheTokenFileIsSentAsLinkCodeAndNeverAsAToken(t *testing.T) {
	cp := newFakeCP(t)
	cfg := cfgWithTokenFile(t, cp, linkCode)
	if _, err := LoadOrEnroll(context.Background(), cfg, Options{Capabilities: []string{"agent.uninstall"}}); err != nil {
		t.Fatalf("LoadOrEnroll: %v", err)
	}
	if cp.lastBody["link_code"] != linkCode {
		t.Fatalf("link_code = %v, want the code", cp.lastBody["link_code"])
	}
	if _, present := cp.lastBody["token"]; present {
		t.Fatalf("the body carries a token member too (%v); the server refuses a body with both", cp.lastBody)
	}
	caps, _ := cp.lastBody["capabilities"].(map[string]any)
	if caps["agent.uninstall"] != true {
		t.Fatalf("capabilities = %v; the review needs to know this agent can uninstall itself", caps)
	}
}

func TestATokenIsStillSentAsATokenAndCarriesNoLinkCode(t *testing.T) {
	cp := newFakeCP(t)
	cfg := cfgWithTokenFile(t, cp, "0192f000-0000-7000-8000-00000000e2e0.s3cr3t")
	if _, err := LoadOrEnroll(context.Background(), cfg, Options{}); err != nil {
		t.Fatalf("LoadOrEnroll: %v", err)
	}
	if cp.lastBody["token"] != "0192f000-0000-7000-8000-00000000e2e0.s3cr3t" {
		t.Fatalf("token = %v", cp.lastBody["token"])
	}
	if _, present := cp.lastBody["link_code"]; present {
		t.Fatal("a token enrollment carries a link_code member; every control plane before this " +
			"one ignores it, but it must not be sent at all")
	}
}
