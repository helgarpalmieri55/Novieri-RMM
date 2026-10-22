package fetch

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestHTTPSRefusesEverythingElse(t *testing.T) {
	for _, u := range []string{"http://example.test/a", "s3://bucket/key", "file:///etc/passwd", "/bin/sh"} {
		if _, err := HTTPS(context.Background(), u); !errors.Is(err, ErrNotHTTPS) {
			t.Errorf("%s: err = %v", u, err)
		}
	}
}

func TestHTTPSErrorsDoNotEchoTheURL(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	}))
	defer srv.Close()
	secret := "X-Amz-Signature=deadbeef"
	_, err := HTTPS(context.Background(), srv.URL+"/obj?"+secret)
	if err == nil || strings.Contains(err.Error(), secret) || strings.Contains(err.Error(), srv.URL) {
		t.Fatalf("err = %v", err)
	}
}

func TestHTTPSStreamsTheBody(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("payload"))
	}))
	defer srv.Close()
	old := http.DefaultTransport
	http.DefaultTransport = srv.Client().Transport
	defer func() { http.DefaultTransport = old }()

	body, err := HTTPS(context.Background(), srv.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer body.Close()
	b, _ := io.ReadAll(body)
	if string(b) != "payload" {
		t.Fatalf("body = %q", b)
	}
}
