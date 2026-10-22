//go:build e2e

// Package e2e is the agent-update-e2e job (spec-self-update.md §11): a real
// service on the runner's OS, a small mTLS WebSocket gateway and an HTTPS
// artifact server, and the six scenarios. It needs root (Administrator on
// Windows) and E2E_BIN_DIR from build.sh; it is never part of `go test ./...`.
package e2e

import (
	"context"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"math/big"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"

	"github.com/toeshee/it-suite/agent/internal/canonical"
	"github.com/toeshee/it-suite/agent/internal/update"
)

const (
	versionA = "0.0.1-e2e.1"
	versionB = "0.0.1-e2e.2"
	versionC = "0.0.1-e2e.3"
	versionD = "0.0.1-e2e.4"
	deviceID = "0192f000-0000-7000-8000-00000000e2e0"
	window   = 120
)

// ---------------------------------------------------------------- PKI

type pki struct {
	caPEM                []byte
	ca                   *x509.Certificate
	caKey                *ecdsa.PrivateKey
	serverTLS            tls.Certificate
	clientCert, clientKy []byte
	pool                 *x509.CertPool
}

func newPKI(t *testing.T) *pki {
	t.Helper()
	caKey, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	caTmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "its-agent e2e throwaway CA"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(24 * time.Hour),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
	}
	caDER, err := x509.CreateCertificate(rand.Reader, caTmpl, caTmpl, &caKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	ca, _ := x509.ParseCertificate(caDER)
	issue := func(cn string, server bool) ([]byte, []byte) {
		key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
		tmpl := &x509.Certificate{
			SerialNumber: big.NewInt(time.Now().UnixNano()),
			Subject:      pkix.Name{CommonName: cn},
			NotBefore:    time.Now().Add(-time.Hour),
			NotAfter:     time.Now().Add(24 * time.Hour),
			KeyUsage:     x509.KeyUsageDigitalSignature,
		}
		if server {
			tmpl.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}
			tmpl.DNSNames = []string{"localhost"}
			tmpl.IPAddresses = []net.IP{net.ParseIP("127.0.0.1"), net.ParseIP("::1")}
		} else {
			tmpl.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}
		}
		der, err := x509.CreateCertificate(rand.Reader, tmpl, ca, &key.PublicKey, caKey)
		if err != nil {
			t.Fatal(err)
		}
		kder, _ := x509.MarshalPKCS8PrivateKey(key)
		return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}),
			pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: kder})
	}
	p := &pki{caPEM: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER}), ca: ca, caKey: caKey}
	sc, sk := issue("localhost", true)
	p.serverTLS, err = tls.X509KeyPair(sc, sk)
	if err != nil {
		t.Fatal(err)
	}
	p.clientCert, p.clientKy = issue(deviceID, false)
	p.pool = x509.NewCertPool()
	p.pool.AddCert(ca)
	return p
}

// ---------------------------------------------------------------- gateway

type conn struct {
	version string
	caps    string
	opened  time.Time
	closed  time.Time
	ws      *websocket.Conn
}

type result struct {
	State  string         `json:"state"`
	Result map[string]any `json:"result"`
}

type gateway struct {
	t       *testing.T
	url     string
	mu      sync.Mutex
	conns   []*conn
	results map[string]result
	jobKey  ed25519.PrivateKey
}

func startGateway(t *testing.T, p *pki, jobKey ed25519.PrivateKey) *gateway {
	g := &gateway{t: t, results: map[string]result{}, jobKey: jobKey}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{
		Handler: http.HandlerFunc(g.serve),
		TLSConfig: &tls.Config{
			Certificates: []tls.Certificate{p.serverTLS},
			ClientCAs:    p.pool,
			ClientAuth:   tls.RequireAndVerifyClientCert,
			MinVersion:   tls.VersionTLS13,
		},
	}
	go func() { _ = srv.ServeTLS(ln, "", "") }()
	t.Cleanup(func() { _ = srv.Close() })
	g.url = fmt.Sprintf("wss://localhost:%d/agent", ln.Addr().(*net.TCPAddr).Port)
	return g
}

func (g *gateway) serve(w http.ResponseWriter, r *http.Request) {
	ws, err := websocket.Accept(w, r, nil)
	if err != nil {
		return
	}
	ws.SetReadLimit(1 << 20)
	c := &conn{
		version: r.Header.Get("X-ITS-Agent-Version"),
		caps:    r.Header.Get("X-ITS-Agent-Capabilities"),
		opened:  time.Now(),
		ws:      ws,
	}
	g.mu.Lock()
	g.conns = append(g.conns, c)
	g.mu.Unlock()
	g.t.Logf("gateway: agent %s connected", c.version)
	defer func() {
		g.mu.Lock()
		c.closed = time.Now()
		g.mu.Unlock()
		g.t.Logf("gateway: agent %s disconnected", c.version)
	}()
	for {
		_, raw, err := ws.Read(context.Background())
		if err != nil {
			return
		}
		var msg struct {
			Kind   string         `json:"kind"`
			JobID  string         `json:"job_id"`
			State  string         `json:"state"`
			Result map[string]any `json:"result"`
		}
		if json.Unmarshal(raw, &msg) != nil || msg.Kind != "job_result" {
			continue
		}
		g.t.Logf("gateway: result for %s: %s %v", msg.JobID, msg.State, msg.Result)
		g.mu.Lock()
		g.results[msg.JobID] = result{State: msg.State, Result: msg.Result}
		g.mu.Unlock()
	}
}

func (g *gateway) live() *conn {
	g.mu.Lock()
	defer g.mu.Unlock()
	for i := len(g.conns) - 1; i >= 0; i-- {
		if g.conns[i].closed.IsZero() {
			return g.conns[i]
		}
	}
	return nil
}

func (g *gateway) connCount() int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return len(g.conns)
}

// sign builds a JobEnvelope the agent's runner will verify.
func (g *gateway) sign(jobType string, params map[string]any) (string, []byte) {
	id := fmt.Sprintf("%08x-0000-7000-8000-%012x", time.Now().Unix()&0xffffffff, time.Now().UnixNano()&0xffffffffffff)
	now := time.Now().UTC()
	job := map[string]any{
		"id":         id,
		"type":       jobType,
		"device":     deviceID,
		"params":     params,
		"issued_at":  now.Add(-5 * time.Second).Format("2006-01-02T15:04:05Z"),
		"expires_at": now.Add(time.Hour).Format("2006-01-02T15:04:05Z"),
		"nonce":      fmt.Sprintf("e2e-%d", now.UnixNano()),
	}
	raw, _ := json.Marshal(job)
	msg, err := canonical.BytesFromRaw(raw)
	if err != nil {
		g.t.Fatal(err)
	}
	sig := base64.StdEncoding.EncodeToString(ed25519.Sign(g.jobKey, msg))
	return id, []byte(fmt.Sprintf(`{"v":1,"kind":"job","job":%s,"sig":%q}`, raw, sig))
}

func (g *gateway) send(jobType string, params map[string]any) string {
	g.t.Helper()
	id, frame := g.sign(jobType, params)
	c := g.waitLive(2 * time.Minute)
	if err := c.ws.Write(context.Background(), websocket.MessageText, frame); err != nil {
		g.t.Fatalf("send %s: %v", jobType, err)
	}
	g.t.Logf("gateway: sent %s %s", jobType, id)
	return id
}

func (g *gateway) waitLive(d time.Duration) *conn {
	g.t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if c := g.live(); c != nil {
			return c
		}
		time.Sleep(500 * time.Millisecond)
	}
	g.t.Fatalf("no agent connection within %s", d)
	return nil
}

// waitVersion waits for a live connection from the given version.
func (g *gateway) waitVersion(version string, d time.Duration) *conn {
	g.t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if c := g.live(); c != nil && c.version == version {
			return c
		}
		time.Sleep(time.Second)
	}
	g.t.Fatalf("version %s never connected within %s", version, d)
	return nil
}

func (g *gateway) waitResult(id string, d time.Duration) result {
	g.t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		g.mu.Lock()
		r, ok := g.results[id]
		g.mu.Unlock()
		if ok {
			return r
		}
		time.Sleep(time.Second)
	}
	g.t.Fatalf("no result for %s within %s", id, d)
	return result{}
}

// ---------------------------------------------------------------- artifacts

type artifacts struct {
	url    string
	sha    map[string]string
	size   map[string]int64
	binDir string
}

func startArtifacts(t *testing.T, p *pki, binDir string) *artifacts {
	a := &artifacts{sha: map[string]string{}, size: map[string]int64{}, binDir: binDir}
	files := map[string][]byte{}
	for _, name := range []string{"A", "B", "C", "D"} {
		b, err := os.ReadFile(filepath.Join(binDir, name+exe()))
		if err != nil {
			t.Fatal(err)
		}
		files[name] = b
		s := sha256.Sum256(b)
		a.sha[name], a.size[name] = hex.EncodeToString(s[:]), int64(len(b))
	}
	tampered := append([]byte(nil), files["B"]...)
	tampered[len(tampered)/2] ^= 0xff
	files["tampered"] = tampered

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			b, ok := files[strings.TrimPrefix(r.URL.Path, "/")]
			if !ok {
				http.NotFound(w, r)
				return
			}
			_, _ = w.Write(b)
		}),
		TLSConfig: &tls.Config{Certificates: []tls.Certificate{p.serverTLS}, MinVersion: tls.VersionTLS12},
	}
	go func() { _ = srv.ServeTLS(ln, "", "") }()
	t.Cleanup(func() { _ = srv.Close() })
	a.url = fmt.Sprintf("https://localhost:%d", ln.Addr().(*net.TCPAddr).Port)
	return a
}

func exe() string {
	if runtime.GOOS == "windows" {
		return ".exe"
	}
	return ""
}

// ---------------------------------------------------------------- identity

func seedDataDir(t *testing.T, dir string, p *pki, gatewayURL string, jobPub ed25519.PublicKey) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	pubDER, _ := x509.MarshalPKIXPublicKey(jobPub)
	write := func(name string, v any, mode os.FileMode) {
		var b []byte
		switch x := v.(type) {
		case []byte:
			b = x
		default:
			b, _ = json.Marshal(x)
		}
		if err := os.WriteFile(filepath.Join(dir, name), b, mode); err != nil {
			t.Fatal(err)
		}
	}
	write("agent.key", p.clientKy, 0o600)
	write("pins.json", map[string]string{
		"ca_bundle_pem":              string(p.caPEM),
		"job_signing_public_key_pem": string(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: pubDER})),
	}, 0o600)
	write("identity.json", map[string]string{
		"device_id":   deviceID,
		"gateway_url": gatewayURL,
		"cert_pem":    string(p.clientCert),
		"chain_pem":   "",
	}, 0o600)
}

// ---------------------------------------------------------------- the rig

type rig struct {
	t        *testing.T
	p        platform
	gw       *gateway
	art      *artifacts
	relKey   ed25519.PrivateKey
	sampler  *sampler
	layout   update.Layout
	binDir   string
	repoRoot string
}

func (r *rig) params(name, version string, mut func(map[string]any)) map[string]any {
	sha := r.art.sha[name]
	if name == "tampered" {
		sha = r.art.sha["B"]
	}
	size := r.art.size[name]
	if name == "tampered" {
		size = r.art.size["B"]
	}
	sig := ed25519.Sign(r.relKey, update.ReleaseMessage(version, runtime.GOOS, runtime.GOARCH, sha))
	p := map[string]any{
		"target_version":    version,
		"os":                runtime.GOOS,
		"arch":              runtime.GOARCH,
		"artifact_url":      r.art.url + "/" + name,
		"artifact_sha256":   sha,
		"artifact_size":     size,
		"rollback":          false,
		"health_window_s":   window,
		"rollout_id":        "e2e",
		"release_signature": base64.StdEncoding.EncodeToString(sig),
	}
	if mut != nil {
		mut(p)
	}
	return p
}

func (r *rig) targetSHA() string {
	r.t.Helper()
	s, err := update.FileSHA256(r.p.target())
	if err != nil {
		r.t.Fatal(err)
	}
	return s
}

func (r *rig) waitHelperGone(d time.Duration) {
	r.t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		gone, detail := r.p.helperGone()
		if gone && !update.HelperAlive(r.layout.HelperLock()) {
			return
		}
		r.t.Logf("helper still present: %s", detail)
		time.Sleep(2 * time.Second)
	}
	r.t.Fatalf("an update helper remained %s after the update finished", d)
}

func (r *rig) expectDeferred(res result, state, outcome, reason string) {
	r.t.Helper()
	if res.State != state || res.Result["outcome"] != outcome {
		r.t.Fatalf("result = %+v, want %s/%s", res, state, outcome)
	}
	if reason != "" && res.Result["reason"] != reason {
		r.t.Fatalf("reason = %v, want %s", res.Result["reason"], reason)
	}
}

// ---------------------------------------------------------------- sampler

// sampler records listening sockets owned by any its-agent process for the
// whole run (scenario 7).
type sampler struct {
	mu         sync.Mutex
	samples    int
	violations []string
	stop       chan struct{}
	done       chan struct{}
}

func startSampler(t *testing.T, p platform, every time.Duration) *sampler {
	s := &sampler{stop: make(chan struct{}), done: make(chan struct{})}
	go func() {
		defer close(s.done)
		tick := time.NewTicker(every)
		defer tick.Stop()
		for {
			found, err := p.listeners()
			s.mu.Lock()
			if err != nil {
				s.violations = append(s.violations, "sampler error: "+err.Error())
			} else {
				s.samples++
				s.violations = append(s.violations, found...)
			}
			s.mu.Unlock()
			select {
			case <-s.stop:
				return
			case <-tick.C:
			}
		}
	}()
	return s
}

func (s *sampler) finish() (int, []string) {
	close(s.stop)
	<-s.done
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.samples, s.violations
}
