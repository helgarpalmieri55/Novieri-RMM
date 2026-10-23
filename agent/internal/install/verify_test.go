package install

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/update"
)

// Review I1. The elevated installer copies a file a non-administrator could
// replace -- it sits in Downloads, on a share or on a USB stick -- so the bytes
// that become the SYSTEM service are checked AFTER they are staged: their
// SHA-256 against the published release the console names, and that release's
// offline signature against the key compiled into this binary. Anything that
// does not match is refused before the service is touched.

type release struct {
	pub   ed25519.PublicKey
	bytes []byte
	meta  ReleaseMeta
}

func signedRelease(t *testing.T, version, goarch string, body []byte) release {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(body)
	shaHex := hex.EncodeToString(sum[:])
	sig := ed25519.Sign(priv, update.ReleaseMessage(version, "windows", goarch, shaHex))
	return release{pub: pub, bytes: body, meta: ReleaseMeta{
		Version: version, SHA256: shaHex, Signature: base64.StdEncoding.EncodeToString(sig),
	}}
}

func TestStagedBytesThatAreTheSignedReleaseAreAccepted(t *testing.T) {
	r := signedRelease(t, "1.6.0", "amd64", []byte("MZ...the published its-agent.exe"))
	if err := verifyStaged(r.pub, r.meta, "amd64", bytes.NewReader(r.bytes)); err != nil {
		t.Fatalf("verifyStaged: %v", err)
	}
	upper := r.meta
	upper.SHA256 = strings.ToUpper(upper.SHA256)
	if err := verifyStaged(r.pub, upper, "amd64", bytes.NewReader(r.bytes)); err != nil {
		t.Fatalf("an upper-case digest from the console was refused: %v", err)
	}
}

func TestStagedBytesThatDifferFromTheReleaseAreRefused(t *testing.T) {
	r := signedRelease(t, "1.6.0", "amd64", []byte("MZ...the published its-agent.exe"))
	swapped := []byte("MZ...something a local user put in Downloads")
	err := verifyStaged(r.pub, r.meta, "amd64", bytes.NewReader(swapped))
	if err == nil || !strings.Contains(err.Error(), "SHA-256") {
		t.Fatalf("err = %v; a staged file whose digest is not the release's must be refused", err)
	}
}

func TestADigestTheReleaseKeyDidNotSignIsRefused(t *testing.T) {
	r := signedRelease(t, "1.6.0", "amd64", []byte("genuine"))
	// A console (or anyone between it and here) that names the swapped file's
	// digest cannot also produce the offline signature over it.
	swapped := []byte("swapped")
	sum := sha256.Sum256(swapped)
	forged := r.meta
	forged.SHA256 = hex.EncodeToString(sum[:])
	if err := verifyStaged(r.pub, forged, "amd64", bytes.NewReader(swapped)); err == nil ||
		!strings.Contains(err.Error(), update.ReasonSignatureInvalid) {
		t.Fatalf("err = %v, want %s", err, update.ReasonSignatureInvalid)
	}

	for name, m := range map[string]ReleaseMeta{
		"no signature":            {Version: r.meta.Version, SHA256: r.meta.SHA256},
		"signature not base64":    {Version: r.meta.Version, SHA256: r.meta.SHA256, Signature: "!!"},
		"relabelled version":      {Version: "1.7.0", SHA256: r.meta.SHA256, Signature: r.meta.Signature},
		"another key's signature": signedRelease(t, "1.6.0", "amd64", []byte("genuine")).meta,
	} {
		if err := verifyStaged(r.pub, m, "amd64", bytes.NewReader(r.bytes)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	// Signed for arm64, staged on amd64: the platform is inside the signature.
	arm := signedRelease(t, "1.6.0", "arm64", []byte("genuine"))
	if err := verifyStaged(arm.pub, arm.meta, "amd64", bytes.NewReader(arm.bytes)); err == nil {
		t.Error("an arm64 release was accepted for an amd64 install")
	}
}

func TestTheReleaseMetadataIsFetchedFromTheLinkAndValidated(t *testing.T) {
	r := signedRelease(t, "1.6.0", "arm64", []byte("genuine"))
	var gotPath, gotQuery string
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		gotPath, gotQuery = req.URL.Path, req.URL.RawQuery
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"version":"1.6.0","sha256":"` + r.meta.SHA256 + `","signature":"` +
			r.meta.Signature + `","published_at":"additive members are ignored"}`))
	}))
	defer srv.Close()
	link := Link{Console: srv.URL, Code: "e2eLINKcode_0123456789-ABCDEFGHI"}
	got, err := fetchReleaseMeta(context.Background(), metaClient(srv.Client()), link, "arm64", r.meta.SHA256)
	if err != nil {
		t.Fatalf("fetchReleaseMeta: %v", err)
	}
	if got != r.meta {
		t.Fatalf("meta = %+v, want %+v", got, r.meta)
	}
	// Final review I1: the request names the STAGED bytes' digest, so a file
	// saved before a newer release was promoted is still answered for. This
	// request format is frozen from 1.6.0 on.
	if gotPath != "/i/e2eLINKcode_0123456789-ABCDEFGHI/windows.sig" ||
		gotQuery != "arch=arm64&sha256="+r.meta.SHA256 {
		t.Fatalf("requested %s?%s", gotPath, gotQuery)
	}
}

func TestTheStagedDigestIsLowerCaseHex(t *testing.T) {
	body := []byte("MZ...the published its-agent.exe")
	sum := sha256.Sum256(body)
	got, err := sha256Hex(bytes.NewReader(body))
	if err != nil || got != hex.EncodeToString(sum[:]) {
		t.Fatalf("sha256Hex = %q, %v", got, err)
	}
}

func TestReleaseMetadataThatIsNotAnAnswerIsRefused(t *testing.T) {
	link := func(url string) Link { return Link{Console: url, Code: "e2eLINKcode_0123456789-ABCDEFGHI"} }
	for name, handler := range map[string]http.HandlerFunc{
		"404 (link off, wrong network)": func(w http.ResponseWriter, _ *http.Request) { http.NotFound(w, nil) },
		"a redirect": func(w http.ResponseWriter, r *http.Request) {
			http.Redirect(w, r, "https://elsewhere.invalid/x", http.StatusFound)
		},
		"not JSON": func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write([]byte("<html>")) },
		"no version": func(w http.ResponseWriter, _ *http.Request) {
			_, _ = w.Write([]byte(`{"sha256":"` + strings.Repeat("a", 64) + `","signature":"x"}`))
		},
		"a short digest": func(w http.ResponseWriter, _ *http.Request) {
			_, _ = w.Write([]byte(`{"version":"1.6.0","sha256":"abc","signature":"x"}`))
		},
		"a digest not hex": func(w http.ResponseWriter, _ *http.Request) {
			_, _ = w.Write([]byte(`{"version":"1.6.0","sha256":"` + strings.Repeat("g", 64) + `","signature":"x"}`))
		},
		"an oversized body": func(w http.ResponseWriter, _ *http.Request) {
			_, _ = w.Write(bytes.Repeat([]byte(" "), maxMetaBytes+1))
		},
	} {
		srv := httptest.NewTLSServer(handler)
		if _, err := fetchReleaseMeta(context.Background(), metaClient(srv.Client()), link(srv.URL), "amd64", strings.Repeat("a", 64)); err == nil {
			t.Errorf("%s: accepted", name)
		}
		srv.Close()
	}
}
