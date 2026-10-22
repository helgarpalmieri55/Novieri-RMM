package install

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/toeshee/it-suite/agent/internal/update"
)

// ReleaseMeta is `GET <console>/i/<code>/windows.sig?arch=<amd64|arm64>&sha256=<hex>`:
// the release the install link offers for that architecture whose bytes are
// the file being installed (fetchReleaseMeta). Served by
// the console under the same 404 rules as the link's other paths, over
// ordinary TLS. Unknown members are ignored (the wire-protocol rule: a newer
// console may add some).
type ReleaseMeta struct {
	Version   string `json:"version"`
	SHA256    string `json:"sha256"`
	Signature string `json:"signature"`
}

// maxMetaBytes bounds the metadata response. It is three short strings.
const maxMetaBytes = 64 << 10

// metaTimeout bounds the one metadata request.
const metaTimeout = 30 * time.Second

// metaClient is the client the elevated installer asks the console with. It
// verifies TLS against the system trust store (a nil base's default
// transport) and FOLLOWS NO REDIRECT: the path answers or it does not. A base
// is accepted so tests can supply their own trust roots.
func metaClient(base *http.Client) *http.Client {
	c := &http.Client{
		Timeout: metaTimeout,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	if base != nil {
		c.Transport = base.Transport
	}
	return c
}

// fetchReleaseMeta asks the console for the release the link serves for
// goarch whose bytes are `digest` -- the SHA-256 of the file being installed,
// lower-case hex (sha256Hex). Naming the digest (final review I1) is what lets
// a file saved before a newer release was promoted, on a share or a USB
// stick, still install: the console answers for THAT release, or 404s. It
// selects and never vouches -- verifyStaged still checks the staged bytes
// against the digest and signature returned.
//
// The request, `/i/<code>/windows.sig?arch=<arch>&sha256=<hex>`, is FROZEN
// from agent 1.6.0 on: every console must keep answering it.
func fetchReleaseMeta(ctx context.Context, client *http.Client, link Link, goarch, digest string) (ReleaseMeta, error) {
	url := link.Console + "/i/" + link.Code + "/windows.sig?arch=" + goarch + "&sha256=" + digest
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return ReleaseMeta{}, err
	}
	resp, err := client.Do(req)
	if err != nil {
		// The URL carries the code; the error names the console only.
		var uerr interface{ Unwrap() error }
		if errors.As(err, &uerr) && uerr.Unwrap() != nil {
			err = uerr.Unwrap()
		}
		return ReleaseMeta{}, fmt.Errorf("could not reach %s: %w", link.Console, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxMetaBytes))
		return ReleaseMeta{}, fmt.Errorf("%s did not confirm this release (HTTP %d); the install link "+
			"may be off, or this computer outside the networks it serves", link.Console, resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxMetaBytes+1))
	if err != nil {
		return ReleaseMeta{}, fmt.Errorf("read the release details from %s: %w", link.Console, err)
	}
	if len(body) > maxMetaBytes {
		return ReleaseMeta{}, fmt.Errorf("%s sent more than a release's details", link.Console)
	}
	var m ReleaseMeta
	if err := json.Unmarshal(body, &m); err != nil {
		return ReleaseMeta{}, fmt.Errorf("%s sent release details that do not parse: %w", link.Console, err)
	}
	if m.Version == "" || m.Signature == "" || !isSHA256Hex(m.SHA256) {
		return ReleaseMeta{}, fmt.Errorf("%s sent incomplete release details", link.Console)
	}
	return m, nil
}

// sha256Hex is the lower-case hex SHA-256 of everything r yields.
func sha256Hex(r io.Reader) (string, error) {
	h := sha256.New()
	if _, err := io.Copy(h, r); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func isSHA256Hex(s string) bool {
	if len(s) != 2*sha256.Size {
		return false
	}
	_, err := hex.DecodeString(s)
	return err == nil
}

// verifyStaged checks the bytes about to become the SYSTEM service: their
// SHA-256 must be the release's, and the release -- version, "windows",
// goarch and that digest -- must carry the offline release signature under
// `pub`, the key compiled into this binary (update.ReleaseKey). It is
// update.CheckReleaseSignature, the same check self-update makes.
//
// A nil pub is a build with no pinned release key (a development build; every
// release build pins one): the signature is then `not_required`, exactly as
// for self-update, and the digest is still checked.
func verifyStaged(pub ed25519.PublicKey, m ReleaseMeta, goarch string, staged io.Reader) error {
	sum, err := sha256Hex(staged)
	if err != nil {
		return fmt.Errorf("read the staged agent: %w", err)
	}
	if !strings.EqualFold(sum, m.SHA256) {
		return fmt.Errorf("the installer's SHA-256 is not the published release's (%s): "+
			"the file is not the one the install link serves", m.Version)
	}
	if _, reason := update.CheckReleaseSignature(pub, m.Version, "windows", goarch, sum, m.Signature); reason != "" {
		return fmt.Errorf("the release signature does not verify (%s)", reason)
	}
	return nil
}
