package pushinstall

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"time"

	"golang.org/x/crypto/ssh"

	"github.com/toeshee/it-suite/agent/internal/capabilities/pushinstall/winrm"
)

// The production Dialer and Downloader. They are resolved at JOB TIME by
// Service.dialer()/downloader(), so the idle agent holds neither a socket nor an
// http client (falsifier 2). The capability's decision logic lives in
// pushinstall.go and is tested with fakes; this file is the thin real glue.

const (
	sshPort        = "22"
	dialTimeout    = 15 * time.Second
	commandTimeout = 60 * time.Second
)

// defaultDialer dials SSH with golang.org/x/crypto/ssh (BSD-licensed, which the
// open-core posture wants, spec §7.4).
type defaultDialer struct{}

// Probe opens a bare SSH handshake, captures the host key in the callback, and
// closes WITHOUT AUTHENTICATING. The key is presented during key exchange,
// before auth, so this learns an unknown key while presenting no secret (§4.4).
func (defaultDialer) Probe(ctx context.Context, addr string) (ProbeInfo, error) {
	hostport := net.JoinHostPort(addr, sshPort)
	var info ProbeInfo
	cfg := &ssh.ClientConfig{
		// Accept and RECORD any key: a probe presents no credential, so learning
		// an unknown key is harmless. This is the ONLY place any key is accepted.
		HostKeyCallback: func(_ string, _ net.Addr, key ssh.PublicKey) error {
			info.Fingerprint = ssh.FingerprintSHA256(key)
			info.KeyType = key.Type()
			return nil
		},
		BannerCallback: func(message string) error {
			info.Banner = strings.TrimSpace(message)
			return nil
		},
		Timeout: dialTimeout,
		// No auth methods: the handshake reaches key exchange (which fills the
		// callback) and then fails auth. The failure is expected; the key is the
		// prize.
	}
	var d net.Dialer
	tcp, err := d.DialContext(ctx, "tcp", hostport)
	if err != nil {
		return ProbeInfo{}, fmt.Errorf("tcp dial: %w", err)
	}
	_ = tcp.SetDeadline(time.Now().Add(dialTimeout))
	conn, chans, reqs, err := ssh.NewClientConn(tcp, hostport, cfg)
	if conn != nil {
		_ = ssh.NewClient(conn, chans, reqs).Close()
	} else {
		_ = tcp.Close()
	}
	if info.Fingerprint == "" {
		return ProbeInfo{}, fmt.Errorf("no host key observed: %w", err)
	}
	return info, nil
}

// Open verifies the host key against expectFingerprint AND authenticates in the
// SAME handshake, so the credential is presented only on a connection whose key
// was checked. A key mismatch returns ErrHostKey before the password is tried;
// a bad password returns ErrAuth.
func (defaultDialer) Open(
	ctx context.Context, addr, expectFingerprint, username string, secret []byte,
) (Session, error) {
	hostport := net.JoinHostPort(addr, sshPort)
	var mismatch error
	cfg := &ssh.ClientConfig{
		User: username,
		Auth: []ssh.AuthMethod{ssh.Password(string(secret))},
		HostKeyCallback: func(_ string, _ net.Addr, key ssh.PublicKey) error {
			got := ssh.FingerprintSHA256(key)
			if !fingerprintsEqual(got, expectFingerprint) {
				mismatch = fmt.Errorf(
					"%w: target presents %s, pinned %s", ErrHostKey, got, expectFingerprint)
				return mismatch
			}
			return nil
		},
		Timeout: dialTimeout,
	}
	var d net.Dialer
	tcp, err := d.DialContext(ctx, "tcp", hostport)
	if err != nil {
		return nil, fmt.Errorf("tcp dial: %w", err)
	}
	_ = tcp.SetDeadline(time.Now().Add(dialTimeout))
	conn, chans, reqs, err := ssh.NewClientConn(tcp, hostport, cfg)
	if err != nil {
		_ = tcp.Close()
		if mismatch != nil {
			return nil, mismatch // ErrHostKey — never reached auth
		}
		return nil, fmt.Errorf("%w: %v", ErrAuth, err)
	}
	return &sshSession{client: ssh.NewClient(conn, chans, reqs)}, nil
}

// sshSession is an authenticated connection.
type sshSession struct{ client *ssh.Client }

func (s *sshSession) Run(ctx context.Context, cmd string) (string, error) {
	session, err := s.client.NewSession()
	if err != nil {
		return "", err
	}
	defer func() { _ = session.Close() }()
	var out bytes.Buffer
	session.Stdout = &out
	done := make(chan error, 1)
	go func() { done <- session.Run(cmd) }()
	select {
	case <-ctx.Done():
		return "", ctx.Err()
	case err := <-done:
		return out.String(), err
	case <-time.After(commandTimeout):
		return "", errors.New("command timed out")
	}
}

func (s *sshSession) Put(ctx context.Context, path string, data []byte, mode uint32) error {
	// Minimal SCP-over-exec sink. The path and mode are compiled-in constants
	// (never job params); the token is written this way so it never appears in
	// argv (falsifier 14).
	session, err := s.client.NewSession()
	if err != nil {
		return err
	}
	defer func() { _ = session.Close() }()
	w, err := session.StdinPipe()
	if err != nil {
		return err
	}
	go func() {
		defer func() { _ = w.Close() }()
		fmt.Fprintf(w, "C%04o %d %s\n", mode, len(data), lastPathElement(path))
		_, _ = w.Write(data)
		fmt.Fprint(w, "\x00")
	}()
	return session.Run("scp -t " + path)
}

func (s *sshSession) Close() error { return s.client.Close() }

func lastPathElement(p string) string {
	if i := strings.LastIndex(p, "/"); i >= 0 {
		return p[i+1:]
	}
	return p
}

// fingerprintsEqual compares two SHA256 fingerprints, tolerating the optional
// "SHA256:" prefix on either side.
func fingerprintsEqual(a, b string) bool {
	na := strings.TrimPrefix(strings.TrimSpace(a), "SHA256:")
	nb := strings.TrimPrefix(strings.TrimSpace(b), "SHA256:")
	return na != "" && na == nb
}

// defaultDownloader fetches the presigned artefact over HTTPS. Outbound from the
// site server to the object store — allowed (2a clause (a)); NOT a connection to
// the target.
type defaultDownloader struct{}

func (defaultDownloader) Download(ctx context.Context, url string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("artefact fetch returned %d", resp.StatusCode)
	}
	return io.ReadAll(resp.Body)
}

// --- winrm ---------------------------------------------------------------------

const (
	winrmHTTPSPort    = "5986"
	winrmHTTPPort     = "5985"
	winrmProbeTimeout = 5 * time.Second
)

// defaultWinRMDialer is the production WinRMDialer: the pure-Go Kerberos
// client in ./winrm, constructed inside the job and released in a defer, like
// the ssh dialer (falsifier 2). It dials ONLY the signed address.
type defaultWinRMDialer struct{}

// Probe is CREDENTIAL-FREE (§C1/W3): two TCP dials to the signed address and a
// plain TCP dial to the realm's KDC (SRV-discovered from the DNS name). No
// Login, no AS exchange, no password -- the probe params carry none.
func (defaultWinRMDialer) Probe(ctx context.Context, fqdn, addr string) (WinRMReadiness, error) {
	return WinRMReadiness{
		HTTPS:        tcpAnswers(ctx, net.JoinHostPort(addr, winrmHTTPSPort)),
		HTTP:         tcpAnswers(ctx, net.JoinHostPort(addr, winrmHTTPPort)),
		KDCReachable: winrm.KDCReachable(ctx, winrm.RealmFor("", fqdn), ""),
	}, nil
}

func tcpAnswers(ctx context.Context, hostport string) bool {
	d := net.Dialer{Timeout: winrmProbeTimeout}
	c, err := d.DialContext(ctx, "tcp", hostport)
	if err != nil {
		return false
	}
	_ = c.Close()
	return true
}

// Run mints the TGT from the password (pure Go, the password goes only to the
// KDC), then authenticates to the target with a HTTP/<fqdn> service ticket --
// 5986 (TLS + channel binding) first, 5985 (Kerberos-sealed) if 5986 does not
// connect. An authentication failure on either is final: there is no other
// scheme to try, and none is compiled in.
func (defaultWinRMDialer) Run(
	ctx context.Context, fqdn, addr string, cred WinRMCredential, script string, stdin []byte,
) (string, int, error) {
	sess, err := winrm.Login(winrm.Creds{
		Username: cred.Username,
		Password: string(cred.Password), // gokrb5 takes a string; this copy cannot be zeroed
		KDC:      cred.KDC,
		CABundle: cred.CABundle,
	}, fqdn)
	if err != nil {
		return "", 0, fmt.Errorf("%w: %v", ErrAuth, err)
	}
	defer sess.Close()

	var conn *winrm.Conn
	var errs []error
	for _, ep := range []winrm.Endpoint{
		{Host: net.JoinHostPort(addr, winrmHTTPSPort), HTTPS: true},
		{Host: net.JoinHostPort(addr, winrmHTTPPort), HTTPS: false},
	} {
		c, derr := winrm.Dial(ctx, sess, fqdn, ep, cred.CABundle)
		if derr == nil {
			conn = c
			break
		}
		if errors.Is(derr, winrm.ErrAuthentication) {
			return "", 0, fmt.Errorf("%w: %v", ErrAuth, derr)
		}
		errs = append(errs, derr)
	}
	if conn == nil {
		return "", 0, fmt.Errorf("winrm: neither 5986 nor 5985 could be used: %w", errors.Join(errs...))
	}
	defer func() { _ = conn.Close() }()
	out, code, err := conn.RunPowerShell(ctx, script, stdin)
	if err != nil {
		if errors.Is(err, winrm.ErrAuthentication) {
			return "", 0, fmt.Errorf("%w: %v", ErrAuth, err)
		}
		return "", 0, err
	}
	return out, code, nil
}
