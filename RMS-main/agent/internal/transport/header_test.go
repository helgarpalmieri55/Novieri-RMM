package transport

import (
	"context"
	"crypto/tls"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func TestDialSendsTheConfiguredHeaders(t *testing.T) {
	pki := newTestPKI(t)
	got := make(chan http.Header, 1)
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got <- r.Header.Clone()
		conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
		if err != nil {
			return
		}
		_ = conn.Close(websocket.StatusNormalClosure, "")
	}))
	srv.TLS = &tls.Config{
		Certificates: []tls.Certificate{pki.server},
		ClientCAs:    pki.caPool,
		ClientAuth:   tls.RequireAndVerifyClientCert,
		MinVersion:   tls.VersionTLS13,
	}
	srv.StartTLS()
	defer srv.Close()

	opts := pki.options("wss" + strings.TrimPrefix(srv.URL, "https") + "/agent")
	opts.Header = IdentityHeader("1.7.0", []string{"hello", "agent.update", "app.deploy"})
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	conn, err := Dial(ctx, opts)
	if err != nil {
		t.Fatal(err)
	}
	conn.Close()

	h := <-got
	if h.Get(HeaderAgentVersion) != "1.7.0" {
		t.Fatalf("version header = %q", h.Get(HeaderAgentVersion))
	}
	if h.Get(HeaderAgentCapabilities) != "agent.update,app.deploy,hello" {
		t.Fatalf("capabilities header = %q", h.Get(HeaderAgentCapabilities))
	}
}

func TestIdentityHeaderStaysWithinItsBound(t *testing.T) {
	var many []string
	for i := 0; i < 1000; i++ {
		many = append(many, "capability.number."+strings.Repeat("x", 10)+string(rune('a'+i%26)))
	}
	h := IdentityHeader("1.0.0", many)
	if len(h.Get(HeaderAgentCapabilities)) > MaxCapabilitiesHeader {
		t.Fatalf("capabilities header is %d bytes", len(h.Get(HeaderAgentCapabilities)))
	}
	if h := IdentityHeader("", nil); h.Get(HeaderAgentVersion) != "" || h.Get(HeaderAgentCapabilities) != "" {
		t.Fatal("empty values must not be sent")
	}
}
