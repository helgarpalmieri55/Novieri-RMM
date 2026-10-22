package winrm

// WS-Management over net/http for ONE push job: Kerberos-only SPNEGO with
// mutual authentication, channel binding on 5986, RFC 4121 sealed messages in
// the MS-WSMV multipart framing on 5985, and the Shell/Command/Receive/Signal/
// Delete exchange that runs the fixed PowerShell.
//
// ONE CONNECTION. The TCP (and on 5986 the TLS) connection is dialled before
// authentication and handed to net/http exactly once. The channel binding is
// computed from THAT connection's certificate, the Kerberos context is bound
// to THAT connection (HTTP.sys ties 5985 session encryption to it), and if it
// drops, the next request fails instead of silently re-dialling an
// unauthenticated one. There is no proxy: the site server talks to the target
// on its own segment (2a clause (a)), never through HTTP(S)_PROXY.

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/xml"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf16"

	"github.com/jcmturner/gokrb5/v8/client"
	"github.com/jcmturner/gokrb5/v8/messages"
	"github.com/jcmturner/gokrb5/v8/spnego"
	"github.com/jcmturner/gokrb5/v8/types"
)

// ErrAuthentication is what every authentication failure wraps, so the
// capability reports `auth_failed` rather than `unreachable`.
var ErrAuthentication = errors.New("winrm: authentication failed")

var (
	// errNonKerberos: the target offered no Kerberos. There is no other
	// scheme in this package to fall back to (design §2.3).
	errNonKerberos = fmt.Errorf("%w: server did not offer Kerberos", ErrAuthentication)
	// errKerberosRejected: the target offers Negotiate/Kerberos but refused
	// our ticket (wrong SPN, clock skew, channel binding, account rights).
	errKerberosRejected = fmt.Errorf("%w: the target rejected the Kerberos authentication", ErrAuthentication)
	// errConnLost: the one authenticated connection is gone.
	errConnLost = errors.New("winrm: the authenticated connection was closed; the Kerberos context is bound to it")
)

const (
	defaultPath        = "/wsman"
	soapContentType    = "application/soap+xml;charset=UTF-8"
	encProtocol        = "application/HTTP-SPNEGO-session-encrypted"
	encBoundary        = "Encrypted Boundary"
	encContentType     = `multipart/encrypted;protocol="` + encProtocol + `";boundary="` + encBoundary + `"`
	maxResponseBytes   = 8 << 20
	dialTimeout        = 15 * time.Second
	responseTimeout    = 120 * time.Second
	receiveOpTimeout   = "PT60S"
	maxEnvelopeSize    = "153600"
	cleanupTimeout     = 15 * time.Second
	ntlmOIDString      = "1.3.6.1.4.1.311.2.2.10"
	negotiateScheme    = "negotiate"
	kerberosSchemeName = "kerberos"
)

// Endpoint is where the WinRM listener is dialled. Host is the TARGET ADDRESS
// from the signed params with the port (never a name DNS chose); HTTPS selects
// 5986-style TLS + channel binding, otherwise 5985 message encryption. Path
// defaults to /wsman.
type Endpoint struct {
	Host  string
	HTTPS bool
	Path  string
}

// Conn is an authenticated WS-Man connection for one push job.
type Conn struct {
	http    *http.Client
	tr      *http.Transport
	url     string
	https   bool
	fqdn    string
	cl      *client.Client
	tkt     messages.Ticket
	tktKey  types.EncryptionKey
	bnd     []byte
	closeMu sync.Mutex
	raw     net.Conn

	// The GSS context (5985): the key every Wrap/Unwrap uses and our send
	// sequence number, started at the authenticator's.
	wrapKey        types.EncryptionKey
	acceptorSubkey bool
	sendSeq        uint64
	// recv checks the acceptor's sequence numbers (review Minor 4); reset
	// with every new context.
	recv replyWindow
}

// oneShot hands net/http the pre-dialled connection exactly once.
type oneShot struct {
	mu sync.Mutex
	c  net.Conn
}

func (o *oneShot) dial(context.Context, string, string) (net.Conn, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.c == nil {
		return nil, errConnLost
	}
	c := o.c
	o.c = nil
	return c, nil
}

// tlsConfig trusts the system roots plus the credential's optional CA bundle
// (§I3/W9). InsecureSkipVerify is never set on any path.
func tlsConfig(fqdn, caBundlePEM string) (*tls.Config, error) {
	pool, err := x509.SystemCertPool()
	if err != nil || pool == nil {
		pool = x509.NewCertPool()
	}
	if strings.TrimSpace(caBundlePEM) != "" && !pool.AppendCertsFromPEM([]byte(caBundlePEM)) {
		return nil, errors.New("winrm: the credential's CA certificate is not valid PEM")
	}
	return &tls.Config{
		RootCAs:    pool,
		ServerName: fqdn,
		MinVersion: tls.VersionTLS12,
		NextProtos: []string{"http/1.1"},
	}, nil
}

// validFQDN: Kerberos needs a real name for HTTP/<fqdn> (W10).
func validFQDN(fqdn string) error {
	f := strings.TrimSuffix(fqdn, ".")
	if f == "" || net.ParseIP(f) != nil || !strings.Contains(f, ".") {
		return fmt.Errorf("winrm: %q is not a DNS name; Kerberos needs HTTP/<fqdn>", fqdn)
	}
	return nil
}

// Dial connects to ep, authenticates with Kerberos (mutual, with channel
// binding on HTTPS) and returns the connection. It fails closed on anything
// but a verified Kerberos AP-REP.
func Dial(ctx context.Context, sess *Session, fqdn string, ep Endpoint, caBundlePEM string) (*Conn, error) {
	if sess == nil {
		return nil, errors.New("winrm: no Kerberos session")
	}
	if err := validFQDN(fqdn); err != nil {
		return nil, err
	}
	tkt, key, err := sess.ServiceTicket(fqdn)
	if err != nil {
		return nil, fmt.Errorf("%w: service ticket for HTTP/%s: %v", ErrAuthentication, strings.ToLower(fqdn), err)
	}
	return dialWithTicket(ctx, sess.cl, tkt, key, fqdn, ep, caBundlePEM)
}

func dialWithTicket(ctx context.Context, cl *client.Client, tkt messages.Ticket, key types.EncryptionKey,
	fqdn string, ep Endpoint, caBundlePEM string) (*Conn, error) {
	if err := validFQDN(fqdn); err != nil {
		return nil, err
	}
	_, port, err := net.SplitHostPort(ep.Host)
	if err != nil {
		return nil, fmt.Errorf("winrm: endpoint %q: %w", ep.Host, err)
	}
	path := ep.Path
	if path == "" {
		path = defaultPath
	}
	d := net.Dialer{Timeout: dialTimeout}
	raw, err := d.DialContext(ctx, "tcp", ep.Host)
	if err != nil {
		return nil, fmt.Errorf("winrm: dial %s: %w", ep.Host, err)
	}
	conn := raw
	bnd := noChannelBinding()
	scheme := "http"
	if ep.HTTPS {
		scheme = "https"
		cfg, err := tlsConfig(fqdn, caBundlePEM)
		if err != nil {
			_ = raw.Close()
			return nil, err
		}
		tc := tls.Client(raw, cfg)
		if err := tc.HandshakeContext(ctx); err != nil {
			_ = raw.Close()
			return nil, fmt.Errorf("winrm: TLS to %s (%s): %w", fqdn, ep.Host, err)
		}
		st := tc.ConnectionState()
		if len(st.PeerCertificates) == 0 {
			_ = tc.Close()
			return nil, errors.New("winrm: TLS peer sent no certificate")
		}
		bnd = channelBindingMD5(st.PeerCertificates[0].Raw)
		conn = tc
	}
	once := &oneShot{c: conn}
	tr := &http.Transport{
		Proxy:                 nil, // never a proxy: same segment, 2a clause (a)
		MaxConnsPerHost:       1,
		MaxIdleConnsPerHost:   1,
		DisableCompression:    true,
		ForceAttemptHTTP2:     false,
		TLSNextProto:          map[string]func(string, *tls.Conn) http.RoundTripper{},
		ResponseHeaderTimeout: responseTimeout,
	}
	if ep.HTTPS {
		tr.DialTLSContext = once.dial
	} else {
		tr.DialContext = once.dial
	}
	c := &Conn{
		http:   &http.Client{Transport: tr, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }},
		tr:     tr,
		url:    scheme + "://" + net.JoinHostPort(strings.ToLower(strings.TrimSuffix(fqdn, ".")), port) + path,
		https:  ep.HTTPS,
		fqdn:   fqdn,
		cl:     cl,
		tkt:    tkt,
		tktKey: key,
		bnd:    bnd,
		raw:    conn,
	}
	if err := c.authenticate(ctx); err != nil {
		_ = c.Close()
		return nil, err
	}
	return c, nil
}

// authenticate builds a fresh AP-REQ (new authenticator, same ticket and
// channel binding) and completes mutual auth with an empty POST, as pywinrm
// does before its first encrypted message.
func (c *Conn) authenticate(ctx context.Context) error {
	apreq, auth, err := BuildAPREQ(c.cl, c.tkt, c.tktKey, c.bnd)
	if err != nil {
		return err
	}
	return c.completeAuth(ctx, apreq, c.tktKey, auth)
}

// completeAuth sends the AP-REQ and finishes mutual auth. KERBEROS ONLY: if
// the 401 offers only NTLM/Basic/CredSSP (or Negotiate that selects NTLM), it
// returns errNonKerberos and NEVER tries another scheme -- there is none in
// this package (design §2.3). A 200/500 must carry a verifiable AP-REP.
func (c *Conn) completeAuth(ctx context.Context, apreq []byte, sessionKey types.EncryptionKey, auth types.Authenticator) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, http.NoBody)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", soapContentType)
	req.Header.Set("Authorization", "Negotiate "+base64.StdEncoding.EncodeToString(apreq))
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("winrm: authentication request: %w", err)
	}
	drain(resp)
	if resp.StatusCode == http.StatusUnauthorized {
		return refusal(resp.Header.Values("WWW-Authenticate"))
	}
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusInternalServerError {
		return fmt.Errorf("winrm: unexpected status %d during authentication", resp.StatusCode)
	}
	reply := negotiateToken(resp.Header.Values("WWW-Authenticate"))
	if reply == nil {
		return fmt.Errorf("%w: server returned no AP-REP; mutual authentication failed (fail closed)", ErrAuthentication)
	}
	key, acceptor, err := ParseAPREP(reply, sessionKey, auth)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrAuthentication, err)
	}
	c.wrapKey, c.acceptorSubkey = key, acceptor
	c.sendSeq = uint64(auth.SeqNumber)
	c.recv = replyWindow{}
	return nil
}

func drain(resp *http.Response) {
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxResponseBytes))
	_ = resp.Body.Close()
}

// authChallenge is one parsed WWW-Authenticate value.
type authChallenge struct {
	scheme string // lower-case
	name   string // as sent, for the operator message
	token  []byte
}

func challenges(values []string) []authChallenge {
	var out []authChallenge
	for _, v := range values {
		for _, part := range strings.Split(v, ",") {
			part = strings.TrimSpace(part)
			if part == "" || strings.Contains(strings.SplitN(part, " ", 2)[0], "=") {
				continue // an auth-param of the previous challenge, not a scheme
			}
			name, rest, _ := strings.Cut(part, " ")
			ch := authChallenge{scheme: strings.ToLower(name), name: name}
			if tok, err := base64.StdEncoding.DecodeString(strings.TrimSpace(rest)); err == nil && len(tok) > 0 {
				ch.token = tok
			}
			out = append(out, ch)
		}
	}
	return out
}

// negotiateToken is the token of the first Negotiate/Kerberos challenge, or nil.
func negotiateToken(values []string) []byte {
	for _, ch := range challenges(values) {
		if (ch.scheme == negotiateScheme || ch.scheme == kerberosSchemeName) && ch.token != nil {
			return ch.token
		}
	}
	return nil
}

// firstScheme names the first scheme offered, for the operator sentence.
func firstScheme(values []string) string {
	if cs := challenges(values); len(cs) > 0 {
		return cs[0].name
	}
	return "no authentication scheme"
}

// refusal classifies a 401 after our Kerberos AP-REQ.
func refusal(values []string) error {
	cs := challenges(values)
	for _, ch := range cs {
		if ch.scheme != negotiateScheme && ch.scheme != kerberosSchemeName {
			continue
		}
		if ch.token != nil && selectsNTLM(ch.token) {
			return fmt.Errorf("%w: this computer only offered NTLM; pushes use Kerberos only", errNonKerberos)
		}
		return fmt.Errorf("%w (HTTP 401): check the account, the target's HTTP/<fqdn> SPN, clock skew and the WinRM channel-binding setting", errKerberosRejected)
	}
	return fmt.Errorf("%w: this computer only offered %s; pushes use Kerberos only", errNonKerberos, firstScheme(values))
}

// selectsNTLM reports a Negotiate reply that chose NTLM.
func selectsNTLM(tok []byte) bool {
	if len(tok) == 0 || tok[0] != 0xa1 {
		return bytes.HasPrefix(tok, []byte("NTLMSSP\x00"))
	}
	_, nt, err := spnego.UnmarshalNegToken(tok)
	if err != nil {
		return false
	}
	resp, ok := nt.(spnego.NegTokenResp)
	return ok && resp.SupportedMech.String() == ntlmOIDString
}

// Close releases the connection. The Conn is job-scoped (idle budget).
func (c *Conn) Close() error {
	c.closeMu.Lock()
	defer c.closeMu.Unlock()
	if c.tr != nil {
		c.tr.CloseIdleConnections()
	}
	if c.raw != nil {
		err := c.raw.Close()
		c.raw = nil
		if err != nil && !errors.Is(err, net.ErrClosed) {
			return err
		}
	}
	return nil
}

// --- the encrypted (5985) framing ---------------------------------------------

// encryptedBody is the MS-WSMV 2.2.9.1.2.1 body, byte for byte as pywinrm's
// _build_message: CRLF line ends, TAB-indented part headers, the payload
// [4-byte LE signature length][signature][encrypted data] with no CRLF after
// it, then the closing boundary.
func encryptedBody(plainLen int, sig, data []byte) []byte {
	var b bytes.Buffer
	b.WriteString("--" + encBoundary + "\r\n")
	b.WriteString("\tContent-Type: " + encProtocol + "\r\n")
	b.WriteString("\tOriginalContent: type=application/soap+xml;charset=UTF-8;Length=" + strconv.Itoa(plainLen) + "\r\n")
	b.WriteString("--" + encBoundary + "\r\n")
	b.WriteString("\tContent-Type: application/octet-stream\r\n")
	var l [4]byte
	binary.LittleEndian.PutUint32(l[:], uint32(len(sig)))
	b.Write(l[:])
	b.Write(sig)
	b.Write(data)
	b.WriteString("--" + encBoundary + "--\r\n")
	return b.Bytes()
}

// parseEncryptedBody inverts encryptedBody for the server's reply.
func parseEncryptedBody(body []byte) (plainLen int, sig, data []byte, err error) {
	bad := func(why string) (int, []byte, []byte, error) {
		return 0, nil, nil, fmt.Errorf("winrm: encrypted reply malformed: %s", why)
	}
	open := []byte("--" + encBoundary + "\r\n")
	if !bytes.HasPrefix(body, open) {
		return bad("no opening boundary")
	}
	rest := body[len(open):]
	i := bytes.Index(rest, open)
	if i < 0 {
		return bad("no second part")
	}
	headers := string(rest[:i])
	if !strings.Contains(strings.ToLower(headers), strings.ToLower(encProtocol)) {
		return bad("first part is not " + encProtocol)
	}
	li := strings.Index(strings.ToLower(headers), "length=")
	if li < 0 {
		return bad("no OriginalContent Length")
	}
	digits := headers[li+len("length="):]
	end := strings.IndexFunc(digits, func(r rune) bool { return r < '0' || r > '9' })
	if end == 0 {
		return bad("empty Length")
	}
	if end > 0 {
		digits = digits[:end]
	}
	plainLen, perr := strconv.Atoi(digits)
	if perr != nil || plainLen < 0 {
		return bad("bad Length")
	}
	part := rest[i+len(open):]
	nl := bytes.Index(part, []byte("\r\n"))
	if nl < 0 || !strings.Contains(strings.ToLower(string(part[:nl])), "application/octet-stream") {
		return bad("second part is not application/octet-stream")
	}
	payload := part[nl+2:]
	closing := []byte("--" + encBoundary + "--")
	j := bytes.LastIndex(payload, closing)
	if j < 0 {
		return bad("no closing boundary")
	}
	payload = payload[:j]
	if len(payload) < 4 {
		return bad("payload too short")
	}
	n := int(binary.LittleEndian.Uint32(payload[:4]))
	if n < tokenHdrLen || 4+n > len(payload) {
		return bad("signature length out of range")
	}
	return plainLen, payload[4 : 4+n], payload[4+n:], nil
}

// --- the request/response loop ------------------------------------------------

// post sends one SOAP envelope and returns the SOAP reply and HTTP status.
func (c *Conn) post(ctx context.Context, soap []byte) ([]byte, int, error) {
	if c.https {
		return c.postHTTPS(ctx, soap, true)
	}
	return c.postSealed(ctx, soap)
}

func (c *Conn) postHTTPS(ctx context.Context, soap []byte, mayReauth bool) ([]byte, int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(soap))
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Content-Type", soapContentType)
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("winrm: request: %w", err)
	}
	if resp.StatusCode == http.StatusUnauthorized && mayReauth {
		// HTTP.sys may authenticate per request. Re-run the FULL mutual
		// Kerberos exchange (fresh authenticator, same channel binding) on the
		// same connection, then resend once. Still Kerberos only.
		values := resp.Header.Values("WWW-Authenticate")
		drain(resp)
		if !offersKerberos(values) {
			return nil, 0, refusal(values)
		}
		if err := c.authenticate(ctx); err != nil {
			return nil, 0, err
		}
		return c.postHTTPS(ctx, soap, false)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode == http.StatusUnauthorized {
		return nil, 0, refusal(resp.Header.Values("WWW-Authenticate"))
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes))
	if err != nil {
		return nil, 0, fmt.Errorf("winrm: reading reply: %w", err)
	}
	return body, resp.StatusCode, nil
}

func offersKerberos(values []string) bool {
	for _, ch := range challenges(values) {
		if ch.scheme == negotiateScheme || ch.scheme == kerberosSchemeName {
			return true
		}
	}
	return false
}

func (c *Conn) postSealed(ctx context.Context, soap []byte) ([]byte, int, error) {
	sig, data, err := wrapWinRM(c.wrapKey, c.acceptorSubkey, c.sendSeq, soap)
	if err != nil {
		return nil, 0, err
	}
	c.sendSeq++
	body := encryptedBody(len(soap), sig, data)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(body))
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Content-Type", encContentType)
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("winrm: request: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode == http.StatusUnauthorized {
		return nil, 0, fmt.Errorf("%w: the encrypted session was refused (HTTP 401)", ErrAuthentication)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes))
	if err != nil {
		return nil, 0, fmt.Errorf("winrm: reading reply: %w", err)
	}
	ct := strings.ToLower(resp.Header.Get("Content-Type"))
	if !strings.HasPrefix(ct, "multipart/encrypted") || !strings.Contains(ct, strings.ToLower(encProtocol)) {
		// Never accept a plaintext reply on the encrypted channel.
		return nil, 0, fmt.Errorf("winrm: HTTP %d reply is not Kerberos-encrypted (%q); refusing", resp.StatusCode, resp.Header.Get("Content-Type"))
	}
	n, rsig, rdata, err := parseEncryptedBody(raw)
	if err != nil {
		return nil, 0, err
	}
	plain, seq, err := unwrapWinRMSeq(c.wrapKey, c.acceptorSubkey, rsig, rdata)
	if err != nil {
		return nil, 0, err
	}
	if err := c.recv.accept(seq); err != nil {
		return nil, 0, err
	}
	if len(plain) != n {
		return nil, 0, fmt.Errorf("winrm: decrypted reply is %d bytes, header says %d", len(plain), n)
	}
	return plain, resp.StatusCode, nil
}

// --- WS-Man SOAP ----------------------------------------------------------

const (
	nsSOAP        = "http://www.w3.org/2003/05/soap-envelope"
	resourceCmd   = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/cmd"
	actionCreate  = "http://schemas.xmlsoap.org/ws/2004/09/transfer/Create"
	actionDelete  = "http://schemas.xmlsoap.org/ws/2004/09/transfer/Delete"
	actionCommand = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/Command"
	actionReceive = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/Receive"
	actionSignal  = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/Signal"
	actionSend    = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/Send"
	signalTerm    = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/signal/terminate"
	stateDone     = "http://schemas.microsoft.com/wbem/wsman/1/windows/shell/CommandState/Done"
	// wsmanTimedOut is the WS-Man fault a Receive returns when no output
	// arrived within OperationTimeout; the command is still running.
	wsmanTimedOut = "2150858793"
)

func messageID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	h := hex.EncodeToString(b[:])
	return "uuid:" + strings.ToUpper(h[0:8]+"-"+h[8:12]+"-"+h[12:16]+"-"+h[16:20]+"-"+h[20:32])
}

func xmlEscape(s string) string {
	var b bytes.Buffer
	_ = xml.EscapeText(&b, []byte(s))
	return b.String()
}

// envelope builds a WS-Man request. Every dynamic value is XML-escaped.
func (c *Conn) envelope(action, shellID, options, body string) []byte {
	var b strings.Builder
	b.WriteString(`<s:Envelope xmlns:s="` + nsSOAP + `" xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" ` +
		`xmlns:w="http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd" xmlns:p="http://schemas.microsoft.com/wbem/wsman/1/wsman.xsd" ` +
		`xmlns:rsp="http://schemas.microsoft.com/wbem/wsman/1/windows/shell"><s:Header>`)
	b.WriteString(`<a:To>` + xmlEscape(c.url) + `</a:To>`)
	b.WriteString(`<a:ReplyTo><a:Address s:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:Address></a:ReplyTo>`)
	b.WriteString(`<w:MaxEnvelopeSize s:mustUnderstand="true">` + maxEnvelopeSize + `</w:MaxEnvelopeSize>`)
	b.WriteString(`<a:MessageID>` + messageID() + `</a:MessageID>`)
	b.WriteString(`<w:Locale xml:lang="en-US" s:mustUnderstand="false"/><p:DataLocale xml:lang="en-US" s:mustUnderstand="false"/>`)
	b.WriteString(`<w:OperationTimeout>` + receiveOpTimeout + `</w:OperationTimeout>`)
	b.WriteString(`<w:ResourceURI s:mustUnderstand="true">` + resourceCmd + `</w:ResourceURI>`)
	b.WriteString(`<a:Action s:mustUnderstand="true">` + action + `</a:Action>`)
	if shellID != "" {
		b.WriteString(`<w:SelectorSet><w:Selector Name="ShellId">` + xmlEscape(shellID) + `</w:Selector></w:SelectorSet>`)
	}
	b.WriteString(options)
	b.WriteString(`</s:Header><s:Body>` + body + `</s:Body></s:Envelope>`)
	return []byte(b.String())
}

// soapFault is a parsed WS-Man fault.
type soapFault struct {
	code   string // WSManFault Code, if any
	reason string
}

func (f *soapFault) Error() string {
	if f.code != "" {
		return fmt.Sprintf("winrm: WS-Man fault %s: %s", f.code, f.reason)
	}
	return "winrm: SOAP fault: " + f.reason
}

// scan walks the reply; visit gets each start element and the text directly
// inside it.
func scan(doc []byte, visit func(se xml.StartElement, text string)) error {
	d := xml.NewDecoder(bytes.NewReader(doc))
	var stack []xml.StartElement
	var texts []strings.Builder
	for {
		tok, err := d.Token()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return fmt.Errorf("winrm: reply is not XML: %w", err)
		}
		switch t := tok.(type) {
		case xml.StartElement:
			stack = append(stack, t.Copy())
			texts = append(texts, strings.Builder{})
		case xml.CharData:
			if len(texts) > 0 {
				texts[len(texts)-1].Write(t)
			}
		case xml.EndElement:
			if len(stack) == 0 {
				return errors.New("winrm: unbalanced XML")
			}
			visit(stack[len(stack)-1], strings.TrimSpace(texts[len(texts)-1].String()))
			stack, texts = stack[:len(stack)-1], texts[:len(texts)-1]
		}
	}
}

func attr(se xml.StartElement, name string) string {
	for _, a := range se.Attr {
		if a.Name.Local == name {
			return a.Value
		}
	}
	return ""
}

// fault returns the reply's SOAP fault, or nil. Children are visited before
// their parent, so the reason text is collected before Fault is seen.
func fault(doc []byte) *soapFault {
	var isFault bool
	f := &soapFault{}
	_ = scan(doc, func(se xml.StartElement, text string) {
		switch se.Name.Local {
		case "Fault":
			isFault = true
		case "WSManFault":
			f.code = attr(se, "Code")
		case "Text", "Message":
			if f.reason == "" && text != "" {
				f.reason = text
			}
		}
	})
	if !isFault {
		return nil
	}
	return f
}

// call posts an envelope and returns the reply, turning a fault into an error.
func (c *Conn) call(ctx context.Context, env []byte) ([]byte, error) {
	reply, status, err := c.post(ctx, env)
	if err != nil {
		return nil, err
	}
	if f := fault(reply); f != nil {
		if f.reason == "" {
			f.reason = fmt.Sprintf("HTTP %d", status)
		}
		return nil, f
	}
	if status != http.StatusOK {
		return nil, fmt.Errorf("winrm: HTTP %d with no SOAP fault", status)
	}
	return reply, nil
}

func textOf(doc []byte, local string) string {
	var out string
	_ = scan(doc, func(se xml.StartElement, text string) {
		if out == "" && se.Name.Local == local {
			out = text
		}
	})
	return out
}

// encodedCommand is PowerShell's -EncodedCommand: base64 of UTF-16LE.
func encodedCommand(script string) string {
	u := utf16.Encode([]rune(script))
	b := make([]byte, 2*len(u))
	for i, r := range u {
		binary.LittleEndian.PutUint16(b[2*i:], r)
	}
	return base64.StdEncoding.EncodeToString(b)
}

// RunPowerShell runs script -- the FIXED, compiled-in install script, never
// anything from the job params -- via `powershell -EncodedCommand` in a WinRS
// shell, writes stdin (if any) to its standard input and closes it, and returns
// its stdout and exit code.
//
// stdin exists so that a secret never travels on the command line: the
// enrolment token goes here, not into the script (the script IS the command
// line, visible to process listings, event 4688 and script-block logging).
// stderr is deliberately NOT returned: PowerShell echoes failing script lines
// there, and a job result is not the place for them.
func (c *Conn) RunPowerShell(ctx context.Context, script string, stdin []byte) (string, int, error) {
	create := c.envelope(actionCreate, "",
		`<w:OptionSet><w:Option Name="WINRS_NOPROFILE">TRUE</w:Option><w:Option Name="WINRS_CODEPAGE">65001</w:Option></w:OptionSet>`,
		`<rsp:Shell><rsp:InputStreams>stdin</rsp:InputStreams><rsp:OutputStreams>stdout stderr</rsp:OutputStreams></rsp:Shell>`)
	reply, err := c.call(ctx, create)
	if err != nil {
		return "", 0, fmt.Errorf("winrm: create shell: %w", err)
	}
	shellID := textOf(reply, "ShellId")
	if shellID == "" {
		_ = scan(reply, func(se xml.StartElement, text string) {
			if se.Name.Local == "Selector" && attr(se, "Name") == "ShellId" && shellID == "" {
				shellID = text
			}
		})
	}
	if shellID == "" {
		return "", 0, errors.New("winrm: create shell returned no ShellId")
	}
	defer func() {
		cctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), cleanupTimeout)
		defer cancel()
		_, _ = c.call(cctx, c.envelope(actionDelete, shellID, "", ""))
	}()

	cmd := c.envelope(actionCommand, shellID,
		`<w:OptionSet><w:Option Name="WINRS_CONSOLEMODE_STDIN">TRUE</w:Option><w:Option Name="WINRS_SKIP_CMD_SHELL">TRUE</w:Option></w:OptionSet>`,
		`<rsp:CommandLine><rsp:Command>powershell.exe</rsp:Command><rsp:Arguments>-NoProfile -NonInteractive -EncodedCommand `+
			encodedCommand(script)+`</rsp:Arguments></rsp:CommandLine>`)
	reply, err = c.call(ctx, cmd)
	if err != nil {
		return "", 0, fmt.Errorf("winrm: run command: %w", err)
	}
	cmdID := textOf(reply, "CommandId")
	if cmdID == "" {
		return "", 0, errors.New("winrm: command returned no CommandId")
	}
	defer func() {
		cctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), cleanupTimeout)
		defer cancel()
		_, _ = c.call(cctx, c.envelope(actionSignal, shellID, "",
			`<rsp:Signal CommandId="`+xmlEscape(cmdID)+`"><rsp:Code>`+signalTerm+`</rsp:Code></rsp:Signal>`))
	}()

	if len(stdin) > 0 {
		send := c.envelope(actionSend, shellID, "",
			`<rsp:Send><rsp:Stream Name="stdin" CommandId="`+xmlEscape(cmdID)+`" End="true">`+
				base64.StdEncoding.EncodeToString(stdin)+`</rsp:Stream></rsp:Send>`)
		if _, err := c.call(ctx, send); err != nil {
			return "", 0, fmt.Errorf("winrm: send stdin: %w", err)
		}
	}

	var stdout bytes.Buffer
	for {
		if err := ctx.Err(); err != nil {
			return "", 0, err
		}
		recv := c.envelope(actionReceive, shellID,
			`<w:OptionSet><w:Option Name="WSMAN_CMDSHELL_OPTION_KEEPALIVE">TRUE</w:Option></w:OptionSet>`,
			`<rsp:Receive><rsp:DesiredStream CommandId="`+xmlEscape(cmdID)+`">stdout stderr</rsp:DesiredStream></rsp:Receive>`)
		reply, err := c.call(ctx, recv)
		var f *soapFault
		if errors.As(err, &f) && f.code == wsmanTimedOut {
			continue // still running
		}
		if err != nil {
			return "", 0, fmt.Errorf("winrm: receive: %w", err)
		}
		done, code, derr := decodeReceive(reply, &stdout)
		if derr != nil {
			return "", 0, derr
		}
		if done {
			return stdout.String(), code, nil
		}
	}
}

// decodeReceive appends stdout chunks and reports completion and exit code.
func decodeReceive(reply []byte, stdout *bytes.Buffer) (bool, int, error) {
	var done bool
	code := 0
	var derr error
	err := scan(reply, func(se xml.StartElement, text string) {
		switch se.Name.Local {
		case "Stream":
			if attr(se, "Name") != "stdout" || text == "" {
				return
			}
			b, err := base64.StdEncoding.DecodeString(text)
			if err != nil {
				derr = fmt.Errorf("winrm: stdout chunk is not base64: %w", err)
				return
			}
			stdout.Write(b)
		case "CommandState":
			if attr(se, "State") == stateDone {
				done = true
			}
		case "ExitCode":
			if n, err := strconv.Atoi(text); err == nil {
				code = n
			}
		}
	})
	if err != nil {
		return false, 0, err
	}
	return done, code, derr
}
