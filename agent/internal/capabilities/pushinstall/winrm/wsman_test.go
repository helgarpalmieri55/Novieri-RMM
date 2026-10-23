package winrm

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jcmturner/gofork/encoding/asn1"
	"github.com/jcmturner/gokrb5/v8/gssapi"
	"github.com/jcmturner/gokrb5/v8/iana/keyusage"
	"github.com/jcmturner/gokrb5/v8/spnego"
	"github.com/jcmturner/gokrb5/v8/types"
)

func newConnForTest(srv *httptest.Server) *Conn {
	return &Conn{http: srv.Client(), url: srv.URL + "/wsman"}
}

// --- refusal: Kerberos only, no fallback ---------------------------------------

func TestCompleteAuthRefusesNonKerberosWithNoFallback(t *testing.T) {
	for _, scheme := range []string{"NTLM", "Basic realm=\"WSMAN\"", "CredSSP"} {
		var reqs int
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			reqs++
			if !strings.HasPrefix(r.Header.Get("Authorization"), "Negotiate ") {
				t.Errorf("sent Authorization %q; only Negotiate may ever be sent", r.Header.Get("Authorization"))
			}
			w.Header().Set("WWW-Authenticate", scheme)
			w.WriteHeader(http.StatusUnauthorized)
		}))
		c := newConnForTest(srv)
		err := c.completeAuth(context.Background(), []byte("fake-apreq"), types.EncryptionKey{}, types.Authenticator{})
		srv.Close()
		if !errors.Is(err, errNonKerberos) || !errors.Is(err, ErrAuthentication) {
			t.Fatalf("%s: err = %v, want errNonKerberos", scheme, err)
		}
		name := strings.Fields(scheme)[0]
		if !strings.Contains(err.Error(), "this computer only offered "+name+"; pushes use Kerberos only") {
			t.Fatalf("%s: message %q lacks the operator sentence", scheme, err.Error())
		}
		if reqs != 1 {
			t.Fatalf("%s: sent %d requests; a fallback to another scheme was attempted", scheme, reqs)
		}
	}
}

func TestCompleteAuthNegotiateSelectingNTLMIsNonKerberos(t *testing.T) {
	tok, _ := (&spnego.NegTokenResp{NegState: 1, SupportedMech: asn1.ObjectIdentifier{1, 3, 6, 1, 4, 1, 311, 2, 2, 10}}).Marshal()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("WWW-Authenticate", "Negotiate "+base64.StdEncoding.EncodeToString(tok))
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()
	err := newConnForTest(srv).completeAuth(context.Background(), []byte("x"), types.EncryptionKey{}, types.Authenticator{})
	if !errors.Is(err, errNonKerberos) || !strings.Contains(err.Error(), "only offered NTLM; pushes use Kerberos only") {
		t.Fatalf("err = %v", err)
	}
}

func TestCompleteAuthKerberosRejectedIsNamedAsSuch(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("WWW-Authenticate", "Negotiate")
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()
	err := newConnForTest(srv).completeAuth(context.Background(), []byte("x"), types.EncryptionKey{}, types.Authenticator{})
	if !errors.Is(err, errKerberosRejected) || errors.Is(err, errNonKerberos) || !errors.Is(err, ErrAuthentication) {
		t.Fatalf("err = %v, want errKerberosRejected", err)
	}
}

func TestCompleteAuthFailsClosedWithoutAnAPREP(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK) // authenticated us but sent NO WWW-Authenticate AP-REP
	}))
	defer srv.Close()
	err := newConnForTest(srv).completeAuth(context.Background(), []byte("fake-apreq"), types.EncryptionKey{}, types.Authenticator{})
	if err == nil || !strings.Contains(err.Error(), "mutual authentication failed") {
		t.Fatalf("err = %v; a missing AP-REP must fail closed", err)
	}
}

// --- a fake acceptor, for flow coverage (the independent oracle is CI's) -----

type fakeAcceptor struct {
	t          *testing.T
	sessionKey types.EncryptionKey
	subkey     types.EncryptionKey
	wantBnd    []byte
	sealed     bool

	mu         sync.Mutex
	auths      int
	peerSeq    uint64
	mySeq      uint64
	actions    []string
	bodies     [][]byte
	receives   int
	reauthOnce bool // HTTPS: 401 the first SOAP request
	plainReply bool // 5985: answer in clear (must be refused)
	// Review Minor 4: replayReplyAt > 0 answers the Nth sealed request with
	// the FIRST sealed reply again, verbatim (a replay); skipSeqAt > 0 jumps
	// the acceptor's sequence number by one before the Nth reply.
	replayReplyAt int
	skipSeqAt     int
	sealedReplies int
	firstReply    []byte
	sawCmdArgs    string
	sawStdin      string
	unauthRequest bool
}

func (f *fakeAcceptor) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	body, _ := io.ReadAll(r.Body)
	f.bodies = append(f.bodies, body)
	if a := r.Header.Get("Authorization"); a != "" {
		f.acceptAuth(w, a)
		return
	}
	if f.auths == 0 {
		f.unauthRequest = true
		w.Header().Set("WWW-Authenticate", "Negotiate")
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	if f.reauthOnce {
		f.reauthOnce = false
		w.Header().Set("WWW-Authenticate", "Negotiate")
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	soap := body
	if f.sealed {
		if !strings.HasPrefix(r.Header.Get("Content-Type"), "multipart/encrypted;protocol=\"application/HTTP-SPNEGO-session-encrypted\"") {
			f.t.Errorf("request Content-Type %q", r.Header.Get("Content-Type"))
		}
		n, sig, data, err := parseEncryptedBody(body)
		if err != nil {
			f.t.Errorf("request framing: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		plain, seq, err := unsealCFX(f.subkey, false, true, append(append([]byte{}, sig...), data...))
		if err != nil {
			f.t.Errorf("request does not unseal: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if seq != f.peerSeq {
			f.t.Errorf("request SND %d, want %d", seq, f.peerSeq)
		}
		f.peerSeq++
		if len(plain) != n {
			f.t.Errorf("OriginalContent Length %d != %d", n, len(plain))
		}
		soap = plain
	}
	reply := f.handleSOAP(soap)
	if f.sealed && !f.plainReply {
		f.sealedReplies++
		if f.replayReplyAt > 0 && f.sealedReplies == f.replayReplyAt && f.firstReply != nil {
			w.Header().Set("Content-Type", encContentType)
			_, _ = w.Write(f.firstReply)
			return
		}
		if f.skipSeqAt > 0 && f.sealedReplies == f.skipSeqAt {
			f.mySeq++
		}
		tok, err := sealCFX(f.subkey, keyusage.GSSAPI_ACCEPTOR_SEAL, flagSentByAcceptor|flagSealed|flagAcceptorSubkey, f.mySeq, nil, reply)
		if err != nil {
			f.t.Fatal(err)
		}
		f.mySeq++
		split, _ := winrmSplit(f.subkey)
		body := encryptedBody(len(reply), tok[:split], tok[split:])
		if f.firstReply == nil {
			f.firstReply = body
		}
		w.Header().Set("Content-Type", encContentType)
		_, _ = w.Write(body)
		return
	}
	w.Header().Set("Content-Type", soapContentType)
	_, _ = w.Write(reply)
}

func (f *fakeAcceptor) acceptAuth(w http.ResponseWriter, authz string) {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimPrefix(authz, "Negotiate "))
	if err != nil {
		f.t.Errorf("Authorization is not base64: %v", err)
		w.WriteHeader(http.StatusBadRequest)
		return
	}
	_, apreq, _ := parseInit(f.t, raw)
	if err := apreq.DecryptAuthenticator(f.sessionKey); err != nil {
		f.t.Errorf("authenticator: %v", err)
		w.WriteHeader(http.StatusBadRequest)
		return
	}
	a := apreq.Authenticator
	want := f.wantBnd
	if want == nil {
		want = make([]byte, 16)
	}
	if !bytes.Equal(a.Cksum.Checksum[4:20], want) {
		// What a CbtHardeningLevel=Strict listener does with a wrong binding.
		w.Header().Set("WWW-Authenticate", "Negotiate")
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	f.auths++
	f.peerSeq = uint64(a.SeqNumber)
	f.mySeq = 777
	rep := fakeAPREP(f.t, apRepOpts{subkey: &f.subkey, ctime: a.CTime, cusec: a.Cusec, encKey: f.sessionKey, mech: gssapi.OIDKRB5.OID()})
	w.Header().Set("WWW-Authenticate", "Negotiate "+base64.StdEncoding.EncodeToString(rep))
	w.WriteHeader(http.StatusOK)
}

func (f *fakeAcceptor) handleSOAP(soap []byte) []byte {
	action := textOf(soap, "Action")
	f.actions = append(f.actions, action[strings.LastIndex(action, "/")+1:])
	env := func(body string) []byte {
		return []byte(`<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:rsp="http://schemas.microsoft.com/wbem/wsman/1/windows/shell"><s:Body>` + body + `</s:Body></s:Envelope>`)
	}
	switch action {
	case actionCreate:
		return env(`<rsp:Shell><rsp:ShellId>SHELL-1</rsp:ShellId></rsp:Shell>`)
	case actionSend:
		b, _ := base64.StdEncoding.DecodeString(textOf(soap, "Stream"))
		f.sawStdin = string(b)
		return env(`<rsp:SendResponse/>`)
	case actionCommand:
		f.sawCmdArgs = textOf(soap, "Arguments")
		return env(`<rsp:CommandResponse><rsp:CommandId>CMD-1</rsp:CommandId></rsp:CommandResponse>`)
	case actionReceive:
		f.receives++
		if f.receives == 1 {
			return []byte(`<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body><s:Fault><s:Reason><s:Text>timed out</s:Text></s:Reason>` +
				`<s:Detail><f:WSManFault xmlns:f="http://schemas.microsoft.com/wbem/wsman/1/wsmanfault" Code="2150858793"/></s:Detail></s:Fault></s:Body></s:Envelope>`)
		}
		out := base64.StdEncoding.EncodeToString([]byte("WINRM-OK\r\n"))
		return env(`<rsp:ReceiveResponse><rsp:Stream Name="stdout" CommandId="CMD-1">` + out + `</rsp:Stream>` +
			`<rsp:Stream Name="stderr" CommandId="CMD-1">` + base64.StdEncoding.EncodeToString([]byte("line with ENROLLTOKEN")) + `</rsp:Stream>` +
			`<rsp:CommandState CommandId="CMD-1" State="` + stateDone + `"><rsp:ExitCode>0</rsp:ExitCode></rsp:CommandState></rsp:ReceiveResponse>`)
	default:
		return env(``)
	}
}

func newFakeAcceptor(t *testing.T, sealed bool) (*fakeAcceptor, func(ctx context.Context, srv *httptest.Server, fqdn string, https bool, ca string) (*Conn, error)) {
	f := &fakeAcceptor{t: t, sessionKey: testKey(t, 7), subkey: testKey(t, 9), sealed: sealed}
	cl, tkt := testTicketAndClient(t)
	dial := func(ctx context.Context, srv *httptest.Server, fqdn string, https bool, ca string) (*Conn, error) {
		return dialWithTicket(ctx, cl, tkt, f.sessionKey, fqdn, Endpoint{Host: srv.Listener.Addr().String(), HTTPS: https}, ca)
	}
	return f, dial
}

func TestSealedSessionRunsPowerShellAndNeverSendsPlaintext(t *testing.T) {
	// A proxy in the environment must not be used: same-segment only.
	t.Setenv("HTTP_PROXY", "http://127.0.0.1:1")
	t.Setenv("HTTPS_PROXY", "http://127.0.0.1:1")
	f, dial := newFakeAcceptor(t, true)
	srv := httptest.NewServer(f)
	defer srv.Close()
	c, err := dial(context.Background(), srv, "pc1.corp.example.com", false, "")
	if err != nil {
		t.Fatal(err)
	}
	out, code, err := c.RunPowerShell(context.Background(), "Write-Output WINRM-OK", []byte("TOKEN-VIA-STDIN\n"))
	_ = c.Close()
	if err != nil || code != 0 || out != "WINRM-OK\r\n" {
		t.Fatalf("out=%q code=%d err=%v", out, code, err)
	}
	if strings.Contains(out, "ENROLLTOKEN") {
		t.Fatal("stderr leaked into the returned output")
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.unauthRequest {
		t.Fatal("a request reached the target before authentication")
	}
	want := []string{"Create", "Command", "Send", "Receive", "Receive", "Signal", "Delete"}
	if strings.Join(f.actions, ",") != strings.Join(want, ",") {
		t.Fatalf("actions = %v, want %v", f.actions, want)
	}
	if f.sawStdin != "TOKEN-VIA-STDIN\n" {
		t.Fatalf("stdin = %q", f.sawStdin)
	}
	for i, b := range f.bodies {
		if bytes.Contains(b, []byte("TOKEN-VIA-STDIN")) {
			t.Fatalf("request %d carried stdin in clear", i)
		}
	}
	for i, b := range f.bodies {
		if bytes.Contains(b, []byte("Envelope")) || bytes.Contains(b, []byte("EncodedCommand")) {
			t.Fatalf("request %d carried SOAP in clear on 5985", i)
		}
	}
	if !strings.Contains(f.sawCmdArgs, "-NoProfile -NonInteractive -EncodedCommand VwByAGkAdABlAC0ATwB1AHQAcAB1AHQAIABXAEkATgBSAE0ALQBPAEsA") {
		t.Fatalf("command arguments = %q", f.sawCmdArgs)
	}
}

func TestSealedChannelRefusesAPlaintextReply(t *testing.T) {
	f, dial := newFakeAcceptor(t, true)
	f.plainReply = true
	srv := httptest.NewServer(f)
	defer srv.Close()
	c, err := dial(context.Background(), srv, "pc1.corp.example.com", false, "")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Close() }()
	if _, _, err := c.RunPowerShell(context.Background(), "x", nil); err == nil || !strings.Contains(err.Error(), "not Kerberos-encrypted") {
		t.Fatalf("err = %v; a plaintext reply on 5985 must be refused", err)
	}
}

func certPEM(srv *httptest.Server) string {
	return string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw}))
}

func TestHTTPSBindsTheChannelTrustsTheCABundleAndReauthenticates(t *testing.T) {
	f, dial := newFakeAcceptor(t, false)
	srv := httptest.NewTLSServer(f)
	defer srv.Close()
	f.wantBnd = channelBindingMD5(srv.Certificate().Raw)
	f.reauthOnce = true
	// httptest's certificate names example.com; the CA bundle is the only trust.
	c, err := dial(context.Background(), srv, "example.com", true, certPEM(srv))
	if err != nil {
		t.Fatal(err)
	}
	out, code, err := c.RunPowerShell(context.Background(), "Write-Output WINRM-OK", []byte("TOKEN-VIA-STDIN\n"))
	_ = c.Close()
	if err != nil || code != 0 || out != "WINRM-OK\r\n" {
		t.Fatalf("out=%q code=%d err=%v", out, code, err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.auths != 2 {
		t.Fatalf("auths = %d, want 2 (initial + one full re-authentication after a 401)", f.auths)
	}
}

func TestHTTPSWithAWrongChannelBindingIsRejected(t *testing.T) {
	f, dial := newFakeAcceptor(t, false)
	srv := httptest.NewTLSServer(f)
	defer srv.Close()
	f.wantBnd = bytes.Repeat([]byte{1}, 16) // not this certificate's binding
	_, err := dial(context.Background(), srv, "example.com", true, certPEM(srv))
	if !errors.Is(err, errKerberosRejected) {
		t.Fatalf("err = %v, want errKerberosRejected", err)
	}
}

func TestHTTPSWithoutTrustNeverConnects(t *testing.T) {
	f, dial := newFakeAcceptor(t, false)
	srv := httptest.NewTLSServer(f)
	defer srv.Close()
	_, err := dial(context.Background(), srv, "example.com", true, "")
	if err == nil || !strings.Contains(err.Error(), "certificate") {
		t.Fatalf("err = %v; an untrusted certificate must fail the handshake", err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.bodies) != 0 {
		t.Fatal("a request was sent over an unverified TLS connection")
	}
	if _, err := tlsConfig("example.com", "not pem"); err == nil {
		t.Fatal("a bad CA bundle was silently ignored")
	}
}

func TestALostConnectionIsNeverRedialled(t *testing.T) {
	f, dial := newFakeAcceptor(t, true)
	srv := httptest.NewServer(f)
	defer srv.Close()
	c, err := dial(context.Background(), srv, "pc1.corp.example.com", false, "")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = c.Close() }()
	srv.CloseClientConnections()
	time.Sleep(20 * time.Millisecond)
	_, _, err = c.RunPowerShell(context.Background(), "x", nil)
	if err == nil {
		t.Fatal("a request succeeded on a connection that was closed")
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.unauthRequest || len(f.actions) != 0 {
		t.Fatal("an unauthenticated connection was dialled and used")
	}
}

func TestDialRefusesAnIPOrDotlessName(t *testing.T) {
	for _, n := range []string{"10.0.0.9", "pc1", "", "::1"} {
		if _, err := Dial(context.Background(), &Session{}, n, Endpoint{Host: "127.0.0.1:5985"}, ""); err == nil {
			t.Errorf("Dial(%q) accepted", n)
		}
	}
}

func TestEncryptedBodyFramingIsExact(t *testing.T) {
	got := encryptedBody(5, []byte("SIGNATURE-16BYTE"), []byte("DATA"))
	want := "--Encrypted Boundary\r\n" +
		"\tContent-Type: application/HTTP-SPNEGO-session-encrypted\r\n" +
		"\tOriginalContent: type=application/soap+xml;charset=UTF-8;Length=5\r\n" +
		"--Encrypted Boundary\r\n" +
		"\tContent-Type: application/octet-stream\r\n" +
		"\x10\x00\x00\x00SIGNATURE-16BYTEDATA" +
		"--Encrypted Boundary--\r\n"
	if string(got) != want {
		t.Fatalf("framing:\n got %q\nwant %q", got, want)
	}
	n, sig, data, err := parseEncryptedBody(got)
	if err != nil || n != 5 || string(sig) != "SIGNATURE-16BYTE" || string(data) != "DATA" {
		t.Fatalf("parse = %d %q %q %v", n, sig, data, err)
	}
	if encContentType != `multipart/encrypted;protocol="application/HTTP-SPNEGO-session-encrypted";boundary="Encrypted Boundary"` {
		t.Fatalf("Content-Type = %s", encContentType)
	}
	for _, bad := range [][]byte{nil, []byte("--Encrypted Boundary\r\n"), got[:len(got)-30]} {
		if _, _, _, err := parseEncryptedBody(bad); err == nil {
			t.Errorf("malformed body %q parsed", bad)
		}
	}
}

func TestEncodedCommandIsBase64UTF16LE(t *testing.T) {
	if got := encodedCommand("Write-Output WINRM-OK"); got != "VwByAGkAdABlAC0ATwB1AHQAcAB1AHQAIABXAEkATgBSAE0ALQBPAEsA" {
		t.Fatalf("encodedCommand = %s", got)
	}
}

func TestEnvelopeEscapesEveryDynamicValue(t *testing.T) {
	c := &Conn{url: "http://pc1.corp.example.com:5985/wsman"}
	env := c.envelope(actionCommand, `S"<1>&`, "", "")
	if bytes.Contains(env, []byte(`S"<1>&`)) {
		t.Fatal("shell id was not escaped")
	}
	if textOf(env, "Selector") != `S"<1>&` {
		t.Fatalf("escaped selector does not round trip: %q", textOf(env, "Selector"))
	}
}

// REVIEW Minor 4. Every sealed reply in one session carries the acceptor's
// sequence number, and it must be the next one expected: a reply replayed
// verbatim (valid ciphertext, valid header, old number) or one that skips a
// number is refused, not decrypted into the conversation.
func TestSealedChannelRefusesAReplayedOrOutOfSequenceReply(t *testing.T) {
	for name, set := range map[string]func(f *fakeAcceptor){
		"a replay of the first reply": func(f *fakeAcceptor) { f.replayReplyAt = 3 },
		"a skipped sequence number":   func(f *fakeAcceptor) { f.skipSeqAt = 2 },
	} {
		f, dial := newFakeAcceptor(t, true)
		set(f)
		srv := httptest.NewServer(f)
		c, err := dial(context.Background(), srv, "pc1.corp.example.com", false, "")
		if err != nil {
			srv.Close()
			t.Fatal(err)
		}
		_, _, err = c.RunPowerShell(context.Background(), "Write-Output WINRM-OK", []byte("x\n"))
		_ = c.Close()
		srv.Close()
		if err == nil || !strings.Contains(err.Error(), "sequence") {
			t.Errorf("%s: err = %v, want a sequence refusal", name, err)
		}
	}
}

func TestReplyWindowAcceptsOnlyTheNextNumber(t *testing.T) {
	var w replyWindow
	if err := w.accept(777); err != nil {
		t.Fatalf("the first reply sets the base: %v", err)
	}
	if err := w.accept(778); err != nil {
		t.Fatalf("the next number: %v", err)
	}
	for _, bad := range []uint64{778, 777, 780, 0} {
		if err := w.accept(bad); err == nil {
			t.Errorf("accepted %d after 778", bad)
		}
	}
	if err := w.accept(779); err != nil {
		t.Fatalf("a refusal must not move the window: %v", err)
	}
}
