//go:build e2e

package backupstore_test

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/toeshee/it-suite/agent/internal/backupstore"
	"github.com/toeshee/it-suite/agent/internal/canonical"
	"github.com/toeshee/it-suite/agent/internal/capabilities/backup"
	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// End to end against a real MinIO (spec-server-backup.md §12): the production
// capability and transport, jobs signed with a test key and verified by the
// real runner, writes under real SigV4 POST policies and reads under real
// presigned GETs, a format-2 backup through the real `auto` snapshot provider,
// then a restore and a verify of it. The CI step that starts MinIO sets the
// environment; without it this skips and says so.

const (
	e2eDevice     = "6f1a2c34-5b6d-4e7f-8a9b-0c1d2e3f4a5b"
	e2eStorageRef = "11111111-1111-1111-1111-111111111111/backup/22222222-2222-2222-2222-222222222222"
	e2eRegion     = "us-east-1"
)

type s3env struct {
	endpoint *url.URL
	bucket   string
	ak, sk   string
	client   *http.Client
}

func loadEnv(t *testing.T) *s3env {
	t.Helper()
	ep := os.Getenv("ITMS_E2E_S3_ENDPOINT")
	if ep == "" {
		t.Skip("ITMS_E2E_S3_ENDPOINT is not set; the MinIO end-to-end step in CI sets it")
	}
	if runtime.GOOS != "linux" {
		t.Skip("the MinIO end-to-end run is a Linux CI step")
	}
	u, err := url.Parse(ep)
	if err != nil {
		t.Fatal(err)
	}
	pem, err := os.ReadFile(os.Getenv("ITMS_E2E_S3_CA"))
	if err != nil {
		t.Fatal(err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pem) {
		t.Fatal("ITMS_E2E_S3_CA holds no certificate")
	}
	tlsConf := &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}
	t.Cleanup(backupstore.SetTestTLSConfig(tlsConf))
	return &s3env{
		endpoint: u, bucket: os.Getenv("ITMS_E2E_S3_BUCKET"),
		ak: os.Getenv("ITMS_E2E_S3_ACCESS_KEY"), sk: os.Getenv("ITMS_E2E_S3_SECRET_KEY"),
		client: &http.Client{Transport: &http.Transport{TLSClientConfig: tlsConf}, Timeout: 30 * time.Second},
	}
}

func hmacSHA(key []byte, s string) []byte {
	m := hmac.New(sha256.New, key)
	m.Write([]byte(s))
	return m.Sum(nil)
}

func (e *s3env) signingKey(date string) []byte {
	k := hmacSHA([]byte("AWS4"+e.sk), date)
	k = hmacSHA(k, e2eRegion)
	k = hmacSHA(k, "s3")
	return hmacSHA(k, "aws4_request")
}

// postGrant is what the control plane mints on the dedup exchange: a POST
// policy for the repository prefix, bounded in size, short-lived.
func (e *s3env) postGrant(maxBytes int64) wire.UploadGrant {
	now := time.Now().UTC()
	date, amz := now.Format("20060102"), now.Format("20060102T150405Z")
	cred := e.ak + "/" + date + "/" + e2eRegion + "/s3/aws4_request"
	policy, _ := json.Marshal(map[string]any{
		"expiration": now.Add(15 * time.Minute).Format("2006-01-02T15:04:05.000Z"),
		"conditions": []any{
			map[string]string{"bucket": e.bucket},
			[]any{"starts-with", "$key", e2eStorageRef + "/"},
			[]any{"content-length-range", 0, maxBytes},
			map[string]string{"x-amz-algorithm": "AWS4-HMAC-SHA256"},
			map[string]string{"x-amz-credential": cred},
			map[string]string{"x-amz-date": amz},
		},
	})
	b64 := base64.StdEncoding.EncodeToString(policy)
	return wire.UploadGrant{
		URL: e.endpoint.String() + "/" + e.bucket,
		Fields: map[string]string{
			"policy":           b64,
			"x-amz-algorithm":  "AWS4-HMAC-SHA256",
			"x-amz-credential": cred,
			"x-amz-date":       amz,
			"x-amz-signature":  hex.EncodeToString(hmacSHA(e.signingKey(date), b64)),
		},
		KeyPrefix: e2eStorageRef + "/",
		MaxBytes:  maxBytes,
	}
}

func uriEncode(s string, slash bool) string {
	var b strings.Builder
	for _, c := range []byte(s) {
		switch {
		case c >= 'A' && c <= 'Z', c >= 'a' && c <= 'z', c >= '0' && c <= '9', c == '-', c == '_', c == '.', c == '~':
			b.WriteByte(c)
		case c == '/' && !slash:
			b.WriteByte(c)
		default:
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

// presign is one read grant: a SigV4 query-signed GET for one object.
func (e *s3env) presign(key string) string {
	now := time.Now().UTC()
	date, amz := now.Format("20060102"), now.Format("20060102T150405Z")
	scope := date + "/" + e2eRegion + "/s3/aws4_request"
	q := []string{
		"X-Amz-Algorithm=AWS4-HMAC-SHA256",
		"X-Amz-Credential=" + uriEncode(e.ak+"/"+scope, true),
		"X-Amz-Date=" + amz,
		"X-Amz-Expires=900",
		"X-Amz-SignedHeaders=host",
	}
	sort.Strings(q)
	query := strings.Join(q, "&")
	path := "/" + e.bucket + "/" + uriEncode(key, false)
	creq := "GET\n" + path + "\n" + query + "\nhost:" + e.endpoint.Host + "\n\nhost\nUNSIGNED-PAYLOAD"
	sum := sha256.Sum256([]byte(creq))
	sts := "AWS4-HMAC-SHA256\n" + amz + "\n" + scope + "\n" + hex.EncodeToString(sum[:])
	sig := hex.EncodeToString(hmacSHA(e.signingKey(date), sts))
	return e.endpoint.String() + path + "?" + query + "&X-Amz-Signature=" + sig
}

func (e *s3env) makeBucket(t *testing.T) {
	t.Helper()
	now := time.Now().UTC()
	date, amz := now.Format("20060102"), now.Format("20060102T150405Z")
	scope := date + "/" + e2eRegion + "/s3/aws4_request"
	empty := sha256.Sum256(nil)
	ph := hex.EncodeToString(empty[:])
	creq := "PUT\n/" + e.bucket + "\n\nhost:" + e.endpoint.Host + "\nx-amz-content-sha256:" + ph +
		"\nx-amz-date:" + amz + "\n\nhost;x-amz-content-sha256;x-amz-date\n" + ph
	sum := sha256.Sum256([]byte(creq))
	sts := "AWS4-HMAC-SHA256\n" + amz + "\n" + scope + "\n" + hex.EncodeToString(sum[:])
	sig := hex.EncodeToString(hmacSHA(e.signingKey(date), sts))
	req, _ := http.NewRequest(http.MethodPut, e.endpoint.String()+"/"+e.bucket, nil)
	req.Header.Set("x-amz-content-sha256", ph)
	req.Header.Set("x-amz-date", amz)
	req.Header.Set("Authorization", "AWS4-HMAC-SHA256 Credential="+e.ak+"/"+scope+
		", SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature="+sig)
	resp, err := e.client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != 200 && !bytes.Contains(body, []byte("BucketAlreadyOwnedByYou")) {
		t.Fatalf("creating the bucket: %d %s", resp.StatusCode, body)
	}
}

// controlPlane is the two exchanges the gateway answers: dedup (with its
// write grant) and read (with its GET grants), from a chunk index it keeps.
type controlPlane struct {
	env   *s3env
	mu    sync.Mutex
	index map[string]bool
}

func (c *controlPlane) query(_ context.Context, q wire.ChunkQuery) (wire.ChunkGrant, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(q.Check) > 1024 || len(q.Stored) > 1024 {
		return wire.ChunkGrant{}, fmt.Errorf("batch over the bound")
	}
	for _, id := range q.Stored {
		c.index[id] = true
	}
	fresh := []string{}
	for _, id := range q.Check {
		if !c.index[id] {
			fresh = append(fresh, id)
		}
	}
	return wire.ChunkGrant{JobID: q.JobID, QueryID: q.QueryID, New: fresh, Recorded: int64(len(q.Stored)),
		Upload: c.env.postGrant(8 << 20), ExpiresAt: time.Now().Add(15 * time.Minute).UTC().Format(time.RFC3339)}, nil
}

func (c *controlPlane) Query(_ context.Context, q wire.ReadQuery) (wire.ReadGrant, error) {
	g := wire.ReadGrant{JobID: q.JobID, QueryID: q.QueryID}
	for _, s := range q.Suffixes {
		g.Grants = append(g.Grants, wire.ReadGrantItem{Suffix: s, URL: c.env.presign(e2eStorageRef + "/" + s)})
	}
	return g, nil
}

type registry map[string]jobs.Handler

func (r registry) Lookup(t string) (jobs.Handler, bool) { h, ok := r[t]; return h, ok }

type nonces struct{}

func (nonces) SeenOrRecord(string, time.Time) bool { return false }

type sender struct {
	mu   sync.Mutex
	msgs []any
}

func (s *sender) Send(_ context.Context, m any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.msgs = append(s.msgs, m)
	return nil
}

func (s *sender) result(t *testing.T, id string) wire.JobResult {
	t.Helper()
	deadline := time.Now().Add(10 * time.Minute)
	for time.Now().Before(deadline) {
		s.mu.Lock()
		for _, m := range s.msgs {
			if r, ok := m.(wire.JobResult); ok && r.JobID == id {
				s.mu.Unlock()
				return r
			}
		}
		s.mu.Unlock()
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("no result for job %s", id)
	return wire.JobResult{}
}

type harness struct {
	t      *testing.T
	priv   ed25519.PrivateKey
	runner *jobs.Runner
	out    *sender
	n      int
}

func newHarness(t *testing.T, svc *backup.Service) *harness {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	out := &sender{}
	r, err := jobs.New(jobs.Options{
		BaseContext: t.Context(), DeviceID: e2eDevice, SigningKey: pub, Nonces: nonces{},
		Registry: registry{backup.JobTypeRun: svc.Run, backup.JobTypeRestore: svc.Restore},
		Sender:   out, Now: time.Now, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatal(err)
	}
	return &harness{t: t, priv: priv, runner: r, out: out}
}

// dispatch signs and delivers one job exactly as the gateway frames it.
func (h *harness) dispatch(jobType string, params any) wire.JobResult {
	h.t.Helper()
	h.n++
	id := fmt.Sprintf("44444444-4444-4444-4444-%012d", h.n)
	raw, _ := json.Marshal(params)
	now := time.Now().UTC()
	job := fmt.Sprintf(`{"id":%q,"type":%q,"device":%q,"params":%s,"issued_at":%q,"expires_at":%q,"nonce":%q}`,
		id, jobType, e2eDevice, raw, now.Add(-time.Second).Format("2006-01-02T15:04:05Z"),
		now.Add(30*time.Minute).Format("2006-01-02T15:04:05Z"), id)
	msg, err := canonical.BytesFromRaw(json.RawMessage(job))
	if err != nil {
		h.t.Fatal(err)
	}
	sig := base64.StdEncoding.EncodeToString(ed25519.Sign(h.priv, msg))
	h.runner.Handle(context.Background(), []byte(fmt.Sprintf(`{"v":1,"kind":"job","job":%s,"sig":%q}`, job, sig)))
	return h.out.result(h.t, id)
}

func writeTree(t *testing.T) string {
	t.Helper()
	src := filepath.Join(t.TempDir(), "srv")
	big := make([]byte, 24<<20)
	if _, err := rand.Read(big); err != nil {
		t.Fatal(err)
	}
	files := map[string][]byte{"data/big.bin": big, "data/empty": nil}
	for i := 0; i < 200; i++ {
		files[fmt.Sprintf("docs/%02d/note-%03d.txt", i%10, i)] = []byte(strings.Repeat(fmt.Sprintf("note %d\n", i), i+1))
	}
	for rel, b := range files {
		p := filepath.Join(src, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, b, 0o640); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink("data/big.bin", filepath.Join(src, "link")); err != nil {
		t.Fatal(err)
	}
	return src
}

func runParams(src string) map[string]any {
	key := make([]byte, 32)
	for i := range key {
		key[i] = byte(i * 13)
	}
	return map[string]any{
		"repository_id": "22222222-2222-2222-2222-222222222222", "storage_ref": e2eStorageRef,
		"repo_key": base64.StdEncoding.EncodeToString(key), "snapshot": "auto", "format": 2,
		"sources": map[string]any{"paths": []string{src}}, "dedup_params": map[string]any{},
		"workload": map[string]any{"id": "w-e2e", "kind": "files", "config": map[string]any{}},
	}
}

func TestE2EFormat2BackupRestoreAndVerifyAgainstMinIO(t *testing.T) {
	env := loadEnv(t)
	env.makeBucket(t)
	cp := &controlPlane{env: env, index: map[string]bool{}}
	svc := backup.New(backup.Options{
		OpenStore:     backupstore.Opener(cp),
		QueryChunks:   cp.query,
		OpenSnapshot:  backup.PlatformSnapshot,
		ScratchParent: t.TempDir(),
		Logger:        slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	h := newHarness(t, svc)
	src := writeTree(t)
	params := runParams(src)

	start := time.Now()
	res := h.dispatch(backup.JobTypeRun, params)
	took := time.Since(start)
	if res.State != "succeeded" || res.Result["state"] != "success" {
		t.Fatalf("backup.run: %s %v", res.State, res.Result)
	}
	protected, _ := res.Result["bytes_protected"].(float64)
	stored, _ := res.Result["bytes_stored"].(float64)
	t.Logf("first run: %.1f MiB protected, %.1f MiB stored in %s: %.1f MiB/s upload to MinIO",
		protected/(1<<20), stored/(1<<20), took.Round(time.Millisecond), stored/(1<<20)/took.Seconds())
	cons, _ := res.Result["consistency"].(map[string]any)
	if cons == nil || res.Result["workload_id"] != "w-e2e" {
		t.Fatalf("format-2 members missing: %v", res.Result)
	}
	t.Logf("consistency: %v, warnings %v", cons, res.Result["warnings"])

	// A second run over the same tree uploads no file data: the index knew it.
	again := h.dispatch(backup.JobTypeRun, params)
	stats, _ := again.Result["dedup_stats"].(map[string]any)
	if again.Result["state"] != "success" || stats["chunks_uploaded"] != float64(0) {
		t.Fatalf("second run: %v", again.Result)
	}

	ref := "s3://" + env.bucket + "/" + e2eStorageRef + "/" + res.Result["manifest_ref"].(string)
	dest := filepath.Join(t.TempDir(), "restored")
	rr := h.dispatch(backup.JobTypeRestore, map[string]any{
		"repository_id": params["repository_id"], "storage_ref": e2eStorageRef, "manifest_ref": ref,
		"selection": []string{src}, "target": map[string]any{"path": dest, "conflict": "overwrite"},
		"mode": "restore", "repo_key": params["repo_key"], "format": 2,
	})
	if rr.Result["state"] != "success" || rr.Result["mismatch"] != nil {
		t.Fatalf("backup.restore: %v", rr.Result)
	}
	out := filepath.Join(dest, filepath.FromSlash(strings.TrimPrefix(filepath.ToSlash(src), "/")))
	n := 0
	err := filepath.WalkDir(src, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || d.Type()&fs.ModeSymlink != 0 {
			return err
		}
		rel, _ := filepath.Rel(src, p)
		want, _ := os.ReadFile(p)
		got, err := os.ReadFile(filepath.Join(out, rel))
		if err != nil || !bytes.Equal(got, want) {
			t.Errorf("%s did not come back byte for byte (%v)", rel, err)
		}
		n++
		return nil
	})
	if err != nil || n < 200 {
		t.Fatalf("compared %d files: %v", n, err)
	}

	vr := h.dispatch(backup.JobTypeRestore, map[string]any{
		"repository_id": params["repository_id"], "storage_ref": e2eStorageRef, "manifest_ref": ref,
		"selection": []string{}, "target": map[string]any{}, "mode": "verify",
		"repo_key": params["repo_key"], "format": 2,
	})
	if vr.Result["verified"] != true {
		t.Fatalf("verify: %v", vr.Result)
	}
	t.Logf("restored and verified %d files from MinIO", n)
}
