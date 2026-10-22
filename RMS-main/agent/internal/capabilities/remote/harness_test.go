package remote

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/toeshee/it-suite/agent/internal/capabilities/remote/helper"
	"github.com/toeshee/it-suite/agent/internal/capabilities/remote/helper/synthetic"
	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// helperEnv makes this test binary act as the helper when a test launches it.
const helperEnv = "ITMS_REMOTE_TEST_HELPER"

func TestMain(m *testing.M) {
	if mode := os.Getenv(helperEnv); mode != "" {
		os.Exit(runTestHelper(mode))
	}
	// The production launcher runs `<exe> remote-helper`, as cmd/agent does.
	if len(os.Args) > 1 && os.Args[1] == HelperArg {
		os.Exit(realHelper())
	}
	os.Exit(m.Run())
}

// runTestHelper is the helper process in tests: the real session logic over
// the real inherited pipes, against a fake desktop.
func runTestHelper(mode string) int {
	switch mode {
	case "hang": // ignores the service entirely
		time.Sleep(time.Hour)
		return 0
	case "crash": // reads the config and dies without a word
		_, _ = io.ReadFull(os.Stdin, make([]byte, 5))
		return 3
	case "real":
		return realHelper()
	}
	o := synthetic.Options{Answer: helper.Granted}
	if mode == "denied" {
		o.Answer = helper.Denied
	}
	err := helper.Run(context.Background(), os.Stdin, os.Stdout, synthetic.New(o), helper.Options{
		CaptureTimeout: 20 * time.Millisecond, MinFrameInterval: time.Millisecond,
	})
	if err != nil {
		return 1
	}
	return 0
}

// selfLauncher launches this test binary as a helper in the given mode.
func selfLauncher(mode string) ExecLauncher {
	return ExecLauncher{Path: os.Args[0], Args: []string{"-test.run=^$"}, Env: append(os.Environ(), helperEnv+"="+mode)}
}

// pki is a throwaway CA with a gateway and an agent certificate.
type pki struct {
	pool   *x509.CertPool
	server tls.Certificate
	client tls.Certificate
}

func newPKI(t testing.TB) *pki {
	t.Helper()
	caKey, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	caTmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "test ca"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign,
	}
	caDER, err := x509.CreateCertificate(rand.Reader, caTmpl, caTmpl, &caKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	ca, _ := x509.ParseCertificate(caDER)
	issue := func(n int64, server bool) tls.Certificate {
		k, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		tmpl := &x509.Certificate{
			SerialNumber: big.NewInt(n), Subject: pkix.Name{CommonName: "leaf"},
			NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
			KeyUsage: x509.KeyUsageDigitalSignature,
		}
		if server {
			tmpl.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}
			tmpl.IPAddresses = []net.IP{net.ParseIP("127.0.0.1")}
		} else {
			tmpl.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}
		}
		der, err := x509.CreateCertificate(rand.Reader, tmpl, ca, &k.PublicKey, caKey)
		if err != nil {
			t.Fatal(err)
		}
		return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: k}
	}
	pool := x509.NewCertPool()
	pool.AddCert(ca)
	return &pki{pool: pool, server: issue(2, true), client: issue(3, false)}
}

// gateway is a fake relay endpoint that plays the viewer's side.
type gateway struct {
	t      testing.TB
	srv    *httptest.Server
	token  string
	refuse int // close code to refuse AUTH with; 0 admits
	frames chan rsp.Frame
	mu     sync.Mutex
	conn   *websocket.Conn
	ready  chan struct{}
	certs  int
	seq    uint32
}

func newGateway(t testing.TB, p *pki, token string) *gateway {
	t.Helper()
	g := &gateway{t: t, token: token, frames: make(chan rsp.Frame, 4096), ready: make(chan struct{})}
	g.srv = httptest.NewUnstartedServer(http.HandlerFunc(g.handle))
	g.srv.TLS = &tls.Config{
		MinVersion: tls.VersionTLS13, Certificates: []tls.Certificate{p.server},
		ClientAuth: tls.RequireAndVerifyClientCert, ClientCAs: p.pool,
	}
	g.srv.StartTLS()
	t.Cleanup(g.srv.Close)
	return g
}

func (g *gateway) url() string { return "wss://" + g.srv.Listener.Addr().String() + "/relay" }

func (g *gateway) handle(w http.ResponseWriter, r *http.Request) {
	g.mu.Lock()
	g.certs = len(r.TLS.PeerCertificates)
	g.mu.Unlock()
	c, err := websocket.Accept(w, r, &websocket.AcceptOptions{Subprotocols: []string{rsp.Subprotocol}})
	if err != nil {
		return
	}
	c.SetReadLimit(rsp.MaxMessageBytes)
	ctx := context.Background()
	_, b, err := c.Read(ctx)
	if err != nil {
		return
	}
	f, _ := rsp.Decode(b)
	var auth rsp.AuthBody
	if f.Type != rsp.Auth || rsp.DecodeJSON(f, &auth) != nil || auth.Token != g.token || g.refuse != 0 {
		code := g.refuse
		if code == 0 {
			code = rsp.CloseRefused
		}
		_ = c.Close(websocket.StatusCode(code), "refused")
		return
	}
	ok, _ := rsp.EncodeJSON(rsp.AuthOK, 1, map[string]string{"session_id": auth.SessionID})
	_ = c.Write(ctx, websocket.MessageBinary, ok)
	g.mu.Lock()
	g.conn = c
	g.mu.Unlock()
	close(g.ready)
	defer close(g.frames)
	for {
		typ, b, err := c.Read(ctx)
		if err != nil {
			return
		}
		f, err := rsp.Decode(b)
		if typ != websocket.MessageBinary || err != nil {
			g.t.Errorf("agent sent a non-rsp message")
			continue
		}
		if !rsp.AgentToViewer(f.Type) {
			g.t.Errorf("agent leg carried %#x", f.Type)
		}
		f.Body = append([]byte(nil), f.Body...)
		g.frames <- f
	}
}

func (g *gateway) send(b []byte) {
	<-g.ready
	g.mu.Lock()
	c := g.conn
	g.mu.Unlock()
	_ = c.Write(context.Background(), websocket.MessageBinary, b)
}

func (g *gateway) next() uint32 { g.seq++; return g.seq }

func (g *gateway) close() {
	<-g.ready
	g.mu.Lock()
	c := g.conn
	g.mu.Unlock()
	_ = c.Close(websocket.StatusNormalClosure, "viewer left")
}

// recv returns the next agent frame, or fails the test.
func (g *gateway) recv() rsp.Frame {
	g.t.Helper()
	select {
	case f, ok := <-g.frames:
		if !ok {
			g.t.Fatal("the agent leg closed")
		}
		return f
	case <-time.After(10 * time.Second):
		g.t.Fatal("no frame on the agent leg")
	}
	return rsp.Frame{}
}

func (g *gateway) state() rsp.StateBody {
	g.t.Helper()
	for {
		if f := g.recv(); f.Type == rsp.State {
			var s rsp.StateBody
			_ = json.Unmarshal(f.Body, &s)
			return s
		}
	}
}

func (g *gateway) until(t rsp.Type) rsp.Frame {
	g.t.Helper()
	for {
		if f := g.recv(); f.Type == t {
			return f
		}
	}
}

func params(g *gateway, token string) Params {
	yes := true
	return Params{
		SessionID: "01928c1e-0000-7000-8000-000000000001", RelayURL: g.url(), RelayToken: token,
		ConnectBy: time.Now().Add(time.Minute).UTC().Format(time.RFC3339), ConsentMode: "attended",
		Consent:      Consent{Technician: "Ann", Org: "Toeshee", Reason: "INC-1", TimeoutS: 30},
		MaxDurationS: 3600, AllowInput: &yes, Record: &yes, Desktop: "user",
	}
}

func options(p *pki, l Launcher) Options {
	return Options{
		ClientCert: func() (tls.Certificate, error) { return p.client, nil },
		RootCAs:    p.pool, Launcher: l, HelperGrace: 2 * time.Second,
	}.withDefaults()
}
