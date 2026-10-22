package enroll

import (
	"context"
	"errors"
	"net/http"
	"os"
	"testing"
	"time"
)

// A LINK ENROLLMENT IS RETRIED IN-PROCESS on the answers that mean "not now":
// 429 (the link's hourly limit, which the server sends with Retry-After), 5xx
// and a control plane that cannot be reached. Exiting instead hands the retry
// to the service manager, and a unit that fails fast enough trips systemd's
// start limit and stays down -- a machine that joined through the link would
// then never enroll. Everything else (404: the link is off or the source is
// outside its networks; any other 4xx) is final, and the file is kept.

func recordWaits(opts *Options) *[]time.Duration {
	var waits []time.Duration
	opts.wait = func(_ context.Context, d time.Duration) error {
		waits = append(waits, d)
		return nil
	}
	return &waits
}

func TestALinkEnrollmentAnswered429IsRetriedAfterRetryAfterAndThenEnrolls(t *testing.T) {
	cp := newFakeCP(t)
	cp.statuses = []int{http.StatusTooManyRequests, http.StatusTooManyRequests}
	cp.retryAfter = "120"
	cfg := cfgWithTokenFile(t, cp, linkCode)
	opts := Options{}
	waits := recordWaits(&opts)
	if _, err := LoadOrEnroll(context.Background(), cfg, opts); err != nil {
		t.Fatalf("LoadOrEnroll: %v", err)
	}
	if got := cp.requests.Load(); got != 3 {
		t.Fatalf("requests = %d, want 3 (429, 429, 201)", got)
	}
	if len(*waits) != 2 || (*waits)[0] != 120*time.Second || (*waits)[1] != 120*time.Second {
		t.Fatalf("waits = %v, want [2m0s 2m0s]: Retry-After is the server's answer to when", *waits)
	}
	if _, err := os.Stat(cfg.EnrollTokenPath()); !os.IsNotExist(err) {
		t.Fatalf("the spent link code survived a successful enrollment: %v", err)
	}
}

func TestALinkEnrollmentRetries5xxOnACappedJitteredTimer(t *testing.T) {
	cp := newFakeCP(t)
	cp.statuses = []int{500, 502, 503, 500, 500, 500, 500, 500, 500, 500}
	cfg := cfgWithTokenFile(t, cp, linkCode)
	opts := Options{}
	waits := recordWaits(&opts)
	if _, err := LoadOrEnroll(context.Background(), cfg, opts); err != nil {
		t.Fatalf("LoadOrEnroll: %v", err)
	}
	if len(*waits) != 10 {
		t.Fatalf("waits = %v, want 10", *waits)
	}
	for i, d := range *waits {
		if d <= 0 || d > linkRetryCap {
			t.Fatalf("wait %d = %v, outside (0, %v]", i, d, linkRetryCap)
		}
	}
	if (*waits)[9] < linkRetryCap/2 {
		t.Fatalf("the tenth wait is %v: the backoff never reached its cap", (*waits)[9])
	}
}

func TestALinkEnrollmentRetriesAnUnreachableControlPlaneUntilCancelled(t *testing.T) {
	cp := newFakeCP(t)
	cfg := cfgWithTokenFile(t, cp, linkCode)
	cfg.EnrollURL = "http://127.0.0.1:1" // nothing listens on port 1
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	opts := Options{}
	tries := 0
	opts.wait = func(context.Context, time.Duration) error {
		if tries++; tries == 3 {
			cancel()
			return ctx.Err()
		}
		return nil
	}
	if _, err := LoadOrEnroll(ctx, cfg, opts); !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want the cancellation that stopped the retries", err)
	}
	if tries != 3 {
		t.Fatalf("a dial error was waited on %d times before cancellation, want 3", tries)
	}
	if _, err := os.Stat(cfg.EnrollTokenPath()); err != nil {
		t.Fatalf("the link code was not kept: %v", err)
	}
}

func TestALinkEnrollmentRefusedWith404OrAnother4xxIsFinalAndKeepsTheFile(t *testing.T) {
	for _, status := range []int{http.StatusNotFound, http.StatusForbidden, http.StatusUnprocessableEntity} {
		cp := newFakeCP(t)
		cp.status = status
		cfg := cfgWithTokenFile(t, cp, linkCode)
		opts := Options{}
		waits := recordWaits(&opts)
		_, err := LoadOrEnroll(context.Background(), cfg, opts)
		if !errors.Is(err, ErrRejected) {
			t.Fatalf("%d: err = %v, want a rejection", status, err)
		}
		if len(*waits) != 0 || cp.requests.Load() != 1 {
			t.Fatalf("%d: retried (%d requests, waits %v)", status, cp.requests.Load(), *waits)
		}
		if _, err := os.Stat(cfg.EnrollTokenPath()); err != nil {
			t.Fatalf("%d: the token file was not kept: %v", status, err)
		}
	}
}

func TestATokenEnrollmentIsNeverRetriedInProcess(t *testing.T) {
	// Unchanged for the token path: one attempt, and the exit code tells the
	// service manager.
	for _, status := range []int{http.StatusTooManyRequests, http.StatusServiceUnavailable} {
		cp := newFakeCP(t)
		cp.status = status
		cfg := cfgWithTokenFile(t, cp, "0192f000-0000-7000-8000-00000000e2e0.s3cr3t")
		opts := Options{}
		waits := recordWaits(&opts)
		if _, err := LoadOrEnroll(context.Background(), cfg, opts); err == nil {
			t.Fatalf("%d enrolled", status)
		}
		if len(*waits) != 0 || cp.requests.Load() != 1 {
			t.Fatalf("%d: a token enrollment was retried: %d requests", status, cp.requests.Load())
		}
	}
}

func TestRetryAfterIsReadAsSecondsOrAnHTTPDateAndBounded(t *testing.T) {
	now := time.Date(2026, 9, 28, 12, 0, 0, 0, time.UTC)
	for header, want := range map[string]time.Duration{
		"":                              0,
		"garbage":                       0,
		"-5":                            0,
		"0":                             0,
		"30":                            30 * time.Second,
		"999999":                        linkRetryAfterMax,
		"Mon, 28 Sep 2026 12:01:00 GMT": time.Minute,
		"Mon, 28 Sep 2026 11:00:00 GMT": 0,
	} {
		if got := retryAfter(header, now); got != want {
			t.Errorf("retryAfter(%q) = %v, want %v", header, got, want)
		}
	}
}

func TestTheBackoffIsJitteredAndCapped(t *testing.T) {
	for attempt := 0; attempt < 40; attempt++ {
		d := linkBackoff(attempt)
		ceil := linkRetryBase << min(attempt, 20)
		if ceil > linkRetryCap || ceil <= 0 {
			ceil = linkRetryCap
		}
		if d < ceil/2 || d > ceil {
			t.Fatalf("linkBackoff(%d) = %v, want within [%v, %v]", attempt, d, ceil/2, ceil)
		}
	}
}
