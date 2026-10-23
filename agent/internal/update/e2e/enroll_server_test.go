//go:build e2e && (windows || linux)

package e2e

import (
	"crypto/ed25519"
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
	"io"
	"math/big"
	"net"
	"net/http"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/update"
)

// ---------------------------------------------------------------- enrollment

// enrollServer is `POST /enroll/v1` (app/api/v1/enrollment.py), cut down to
// what an agent needs: it signs the CSR with the throwaway CA for deviceID and
// hands back the gateway and the job-signing key. It refuses a body carrying a
// `token` with the real server's bare 403, so a self-installer that sent its
// code as a token fails here the way it would in the field.
//
// It also serves the link's release details,
// `GET /i/<code>/windows.sig?arch=<arch>&sha256=<hex>`
// (review I1), for whatever offer() registered -- signed with build.sh's
// throwaway release key, which binary A pins -- and 404 for anything else,
// as the console does.
//
// A token enrollment (the SSH push, TestPushInstallLinux) is accepted only
// for the one token acceptToken registered: a body carrying any other token --
// or any token at all, when none was registered -- gets the bare 403.
type enrollServer struct {
	url     string
	mu      sync.Mutex
	bodies  []map[string]any
	offered map[string][]byte // "<code>/<arch>/<sha256>" -> JSON body
	token   string
}

// acceptToken makes the server accept an enrollment that presents token.
func (e *enrollServer) acceptToken(token string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.token = token
}

// offer makes the server answer the release-details request for code and the
// runner's architecture with binary's digest, signed for version.
func (e *enrollServer) offer(t *testing.T, code, version string, binary []byte, relKey ed25519.PrivateKey) {
	t.Helper()
	sum := sha256.Sum256(binary)
	shaHex := hex.EncodeToString(sum[:])
	sig := ed25519.Sign(relKey, update.ReleaseMessage(version, runtime.GOOS, runtime.GOARCH, shaHex))
	body, err := json.Marshal(map[string]string{
		"version": version, "sha256": shaHex, "signature": base64.StdEncoding.EncodeToString(sig),
	})
	if err != nil {
		t.Fatal(err)
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.offered == nil {
		e.offered = map[string][]byte{}
	}
	e.offered[code+"/"+runtime.GOARCH+"/"+shaHex] = body
}

func startEnroll(t *testing.T, p *pki, gatewayURL string, jobPub ed25519.PublicKey) *enrollServer {
	t.Helper()
	e := &enrollServer{}
	pubDER, _ := x509.MarshalPKIXPublicKey(jobPub)
	jobPEM := string(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: pubDER}))
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && strings.HasPrefix(r.URL.Path, "/i/") && strings.HasSuffix(r.URL.Path, "/windows.sig") {
			code := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/i/"), "/windows.sig")
			e.mu.Lock()
			body, ok := e.offered[code+"/"+r.URL.Query().Get("arch")+"/"+r.URL.Query().Get("sha256")]
			e.mu.Unlock()
			if !ok {
				http.NotFound(w, r)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write(body)
			return
		}
		if r.Method != http.MethodPost || r.URL.Path != "/enroll/v1" {
			http.NotFound(w, r)
			return
		}
		var body map[string]any
		if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&body); err != nil {
			http.Error(w, "bad json", http.StatusUnprocessableEntity)
			return
		}
		e.mu.Lock()
		e.bodies = append(e.bodies, body)
		accepted := e.token
		e.mu.Unlock()
		if presented, hasToken := body["token"]; hasToken && (accepted == "" || presented != accepted) {
			http.Error(w, "enrollment rejected", http.StatusForbidden)
			return
		}
		csrPEM, _ := body["csr_pem"].(string)
		block, _ := pem.Decode([]byte(csrPEM))
		if block == nil {
			http.Error(w, "no csr", http.StatusUnprocessableEntity)
			return
		}
		csr, err := x509.ParseCertificateRequest(block.Bytes)
		if err != nil || csr.CheckSignature() != nil {
			http.Error(w, "bad csr", http.StatusUnprocessableEntity)
			return
		}
		tmpl := &x509.Certificate{
			SerialNumber: big.NewInt(time.Now().UnixNano()),
			Subject:      pkix.Name{CommonName: deviceID},
			NotBefore:    time.Now().Add(-time.Hour),
			NotAfter:     time.Now().Add(24 * time.Hour),
			KeyUsage:     x509.KeyUsageDigitalSignature,
			ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
		}
		der, err := x509.CreateCertificate(rand.Reader, tmpl, p.ca, csr.PublicKey, p.caKey)
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]string{
			"device_id":                  deviceID,
			"cert_pem":                   string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})),
			"chain_pem":                  "",
			"ca_bundle_pem":              string(p.caPEM),
			"gateway_url":                gatewayURL,
			"job_signing_public_key_pem": jobPEM,
		})
	})
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{
		Handler:   handler,
		TLSConfig: &tls.Config{Certificates: []tls.Certificate{p.serverTLS}, MinVersion: tls.VersionTLS12},
	}
	go func() { _ = srv.ServeTLS(ln, "", "") }()
	t.Cleanup(func() { _ = srv.Close() })
	e.url = fmt.Sprintf("https://localhost:%d", ln.Addr().(*net.TCPAddr).Port)
	return e
}

func (e *enrollServer) requests() []map[string]any {
	e.mu.Lock()
	defer e.mu.Unlock()
	return append([]map[string]any(nil), e.bodies...)
}
