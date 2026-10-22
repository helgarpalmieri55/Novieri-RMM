// Package fetch is the agent's one artifact download: an https:// GET whose
// client exists only for the length of the job. Shared by app.deploy and
// agent.update. Callers verify the bytes; this package only streams them.
package fetch

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// Timeout bounds one download independently of the job's deadline.
const Timeout = 30 * time.Minute

// ErrNotHTTPS is returned for any URL that is not https://.
var ErrNotHTTPS = errors.New("artifact URL is not https://")

// HTTPS opens a GET stream. Errors never echo the URL: it is job-supplied
// and may carry a presigned query string.
func HTTPS(ctx context.Context, url string) (io.ReadCloser, error) {
	if !strings.HasPrefix(url, "https://") {
		return nil, ErrNotHTTPS
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, errors.New("artifact URL is not usable")
	}
	// Built per download: a package-level client would keep an idle pool
	// resident on an agent that is doing nothing.
	client := &http.Client{Timeout: Timeout}
	resp, err := client.Do(req)
	if err != nil {
		return nil, errors.New("the artifact could not be downloaded")
	}
	if resp.StatusCode != http.StatusOK {
		resp.Body.Close()
		return nil, fmt.Errorf("the artifact download returned HTTP %d", resp.StatusCode)
	}
	return resp.Body, nil
}
