package winrm

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/binary"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/jcmturner/gofork/encoding/asn1"
	"github.com/jcmturner/gokrb5/v8/asn1tools"
	"github.com/jcmturner/gokrb5/v8/client"
	"github.com/jcmturner/gokrb5/v8/config"
	"github.com/jcmturner/gokrb5/v8/crypto"
	"github.com/jcmturner/gokrb5/v8/gssapi"
	"github.com/jcmturner/gokrb5/v8/iana/asnAppTag"
	"github.com/jcmturner/gokrb5/v8/iana/chksumtype"
	"github.com/jcmturner/gokrb5/v8/iana/etypeID"
	"github.com/jcmturner/gokrb5/v8/iana/flags"
	"github.com/jcmturner/gokrb5/v8/iana/keyusage"
	"github.com/jcmturner/gokrb5/v8/iana/msgtype"
	"github.com/jcmturner/gokrb5/v8/iana/nametype"
	"github.com/jcmturner/gokrb5/v8/messages"
	"github.com/jcmturner/gokrb5/v8/spnego"
	"github.com/jcmturner/gokrb5/v8/types"
)

func TestRealmFor(t *testing.T) {
	cases := []struct {
		user, fqdn, want string
	}{
		{"CORP\\svc-push", "pc1.corp.example.com", "CORP.EXAMPLE.COM"},
		{"svc-push@CORP.EXAMPLE.COM", "pc1.corp.example.com", "CORP.EXAMPLE.COM"},
		{"svc@corp.example.com", "pc1.corp.example.com", "CORP.EXAMPLE.COM"},
		// The probe derives the realm with NO credential at all.
		{"", "pc1.corp.example.com", "CORP.EXAMPLE.COM"},
	}
	for _, c := range cases {
		if got := RealmFor(c.user, c.fqdn); got != c.want {
			t.Errorf("RealmFor(%q,%q) = %q, want %q", c.user, c.fqdn, got, c.want)
		}
	}
}

func TestBareUser(t *testing.T) {
	for in, want := range map[string]string{
		`CORP\svc-push`:             "svc-push",
		"svc-push@CORP.EXAMPLE.COM": "svc-push",
		"svc-push":                  "svc-push",
	} {
		if got := bareUser(in); got != want {
			t.Errorf("bareUser(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestLoginRefusesEmptyPassword(t *testing.T) {
	if _, err := Login(Creds{Username: "svc@corp.example.com", Password: ""}, "pc1.corp.example.com"); err == nil {
		t.Fatal("expected refusal of an empty password")
	}
}

// REVIEW I1. The realm -- and so the KDC that receives the account's
// pre-authentication -- is the ACCOUNT's, never the target's DNS name: a UPN
// names it; a DOMAIN\user names no realm and is refused; and the target must be
// inside it. Every case here is refused before any network use.
func TestRealmOfAccount(t *testing.T) {
	for in, want := range map[string]string{
		"svc-push@corp.example.com": "CORP.EXAMPLE.COM",
		"svc@Corp.Example.COM":      "CORP.EXAMPLE.COM",
	} {
		if got, err := RealmOfAccount(in); err != nil || got != want {
			t.Errorf("RealmOfAccount(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
	for _, bad := range []string{`CORP\svc-push`, "svc-push", "svc@corp", "@corp.example.com", "a@b@corp.example.com", "svc@corp..example.com", ""} {
		if _, err := RealmOfAccount(bad); err == nil {
			t.Errorf("RealmOfAccount(%q) accepted", bad)
		}
	}
}

func TestInRealmIsASuffixOnALabelBoundary(t *testing.T) {
	for _, c := range []struct {
		fqdn, realm string
		want        bool
	}{
		{"pc1.corp.example.com", "CORP.EXAMPLE.COM", true},
		{"PC1.Branch.Corp.Example.com.", "CORP.EXAMPLE.COM", true},
		{"pc1.notcorp.example.com", "CORP.EXAMPLE.COM", false},
		{"corp.example.com", "CORP.EXAMPLE.COM", false},
		{"pc1.evil.example.net", "CORP.EXAMPLE.COM", false},
		{"pc1.corp.example.com", "", false},
	} {
		if got := InRealm(c.fqdn, c.realm); got != c.want {
			t.Errorf("InRealm(%q, %q) = %v, want %v", c.fqdn, c.realm, got, c.want)
		}
	}
}

func TestLoginTakesTheRealmFromTheAccountAndRefusesATargetOutsideIt(t *testing.T) {
	for name, c := range map[string]struct{ user, fqdn string }{
		"a NetBIOS account":         {`CORP\svc-push`, "pc1.corp.example.com"},
		"a target in another realm": {"svc@corp.example.com", "pc1.evil.example.net"},
		"not on a label boundary":   {"svc@corp.example.com", "pc1.notcorp.example.com"},
	} {
		// No KDC is named and none is reachable: a refusal here happens before
		// any DNS SRV lookup or AS exchange, or the test would hang or differ.
		if _, err := Login(Creds{Username: c.user, Password: "pw", KDC: "127.0.0.1:1"}, c.fqdn); err == nil ||
			!strings.Contains(err.Error(), "realm") {
			t.Errorf("%s: Login = %v, want a realm refusal", name, err)
		}
	}
}

func TestKDCReachableIsCredentialFree(t *testing.T) {
	// No KDC at this address, so it returns false — the point is it neither
	// panics nor needs a password: the probe calls it with no credential.
	if KDCReachable(context.Background(), "CORP.EXAMPLE.COM", "127.0.0.1:1") {
		t.Fatal("no KDC on port 1")
	}
}

func TestKDCReachableTrueWhenSomethingAcceptsTCP(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = ln.Close() }()
	go func() {
		c, err := ln.Accept()
		if err == nil {
			_ = c.Close()
		}
	}()
	if !KDCReachable(context.Background(), "CORP.EXAMPLE.COM", ln.Addr().String()) {
		t.Fatal("a TCP listener at the explicit KDC address was not reachable")
	}
}

// Codex on #90 (transport.go:218). The KDC check ignored the probe's
// context: SRV discovery and every dial ran on their own clocks, so a
// cancelled or expired probe kept dialling stale DC records, 5 s each. With
// the context already done it must not dial at all -- even a KDC that is
// listening is not reported reachable.
func TestKDCReachableHonoursTheProbeContext(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = ln.Close() }()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if KDCReachable(ctx, "CORP.EXAMPLE.COM", ln.Addr().String()) {
		t.Fatal("a cancelled probe still dialled the KDC")
	}
}

func TestKrb5ConfigIsInMemoryAESOnlyAndTCP(t *testing.T) {
	c := krb5Config("CORP.EXAMPLE.COM", "dc1.corp.example.com")
	if c.LibDefaults.DNSLookupKDC {
		t.Error("an explicit KDC must take precedence over SRV discovery")
	}
	if len(c.Realms) != 1 || c.Realms[0].KDC[0] != "dc1.corp.example.com:88" {
		t.Errorf("realms = %+v", c.Realms)
	}
	for _, ids := range [][]int32{c.LibDefaults.DefaultTGSEnctypeIDs, c.LibDefaults.DefaultTktEnctypeIDs, c.LibDefaults.PermittedEnctypeIDs} {
		for _, id := range ids {
			if id != etypeID.AES256_CTS_HMAC_SHA1_96 && id != etypeID.AES128_CTS_HMAC_SHA1_96 {
				t.Errorf("non-AES enctype %d permitted", id)
			}
		}
	}
	if c.LibDefaults.UDPPreferenceLimit != 1 {
		t.Error("KDC traffic must be TCP (the probe checks TCP/88)")
	}
	if !krb5Config("CORP.EXAMPLE.COM", "").LibDefaults.DNSLookupKDC {
		t.Error("with no explicit KDC, SRV discovery must be on")
	}
}

// TestGSSChecksumLayout pins RFC 4121 §4.1.1: Lgth=16 LE, the 16-byte Bnd,
// Flags LE = Mutual|Integ|Conf (0x32), and NO Deleg field (24 bytes, not 28).
func TestGSSChecksumLayout(t *testing.T) {
	bnd := bytes.Repeat([]byte{0xAB}, 16)
	c := gssChecksum(bnd)
	if len(c) != 24 {
		t.Fatalf("checksum length %d, want 24 (delegation off)", len(c))
	}
	if binary.LittleEndian.Uint32(c[0:4]) != 16 {
		t.Error("Lgth != 16")
	}
	if !bytes.Equal(c[4:20], bnd) {
		t.Error("Bnd not copied verbatim")
	}
	if f := binary.LittleEndian.Uint32(c[20:24]); f != 0x32 {
		t.Errorf("flags = %#x, want 0x32 (mutual|integ|conf, deleg off)", f)
	}
}

// --- AP-REQ / AP-REP, with no KDC: a synthetic ticket and session key -------

func testKey(t *testing.T, fill byte) types.EncryptionKey {
	t.Helper()
	return types.EncryptionKey{KeyType: etypeID.AES256_CTS_HMAC_SHA1_96, KeyValue: bytes.Repeat([]byte{fill}, 32)}
}

func testTicketAndClient(t *testing.T) (*client.Client, messages.Ticket) {
	t.Helper()
	cl := client.NewWithPassword("svc-push", "CORP.EXAMPLE.COM", "unused", config.New())
	cipher := make([]byte, 64)
	_, _ = rand.Read(cipher)
	tkt := messages.Ticket{
		TktVNO: 5,
		Realm:  "CORP.EXAMPLE.COM",
		SName:  types.NewPrincipalName(nametype.KRB_NT_SRV_INST, "HTTP/pc1.corp.example.com"),
		EncPart: types.EncryptedData{
			EType: etypeID.AES256_CTS_HMAC_SHA1_96, KVNO: 2, Cipher: cipher,
		},
	}
	return cl, tkt
}

// parseInit is the acceptor's view of our token: GSS InitialContextToken
// (SPNEGO OID) -> NegTokenInit -> KRB5 mech token -> AP-REQ.
func parseInit(t *testing.T, tok []byte) (spnego.NegTokenInit, messages.APReq, []byte) {
	t.Helper()
	var st spnego.SPNEGOToken
	if err := st.Unmarshal(tok); err != nil {
		t.Fatalf("our token is not a SPNEGO InitialContextToken: %v", err)
	}
	if !st.Init {
		t.Fatal("not a NegTokenInit")
	}
	mech := st.NegTokenInit.MechTokenBytes
	var kt spnego.KRB5Token
	if err := kt.Unmarshal(mech); err != nil {
		t.Fatalf("mech token: %v", err)
	}
	if !kt.IsAPReq() {
		t.Fatal("mech token is not an AP-REQ")
	}
	return st.NegTokenInit, kt.APReq, mech
}

func TestBuildAPREQCarriesTheCBTChecksumSubkeyAndMutualRequired(t *testing.T) {
	cl, tkt := testTicketAndClient(t)
	key := testKey(t, 7)
	bnd := bytes.Repeat([]byte{0x5A}, 16)
	tok, auth, err := BuildAPREQ(cl, tkt, key, bnd)
	if err != nil {
		t.Fatal(err)
	}
	init, apreq, mech := parseInit(t, tok)
	if len(init.MechTypes) != 1 || !init.MechTypes[0].Equal(gssapi.OIDKRB5.OID()) {
		t.Fatalf("mech list %v: Kerberos must be the ONLY mechanism offered", init.MechTypes)
	}
	// RFC 1964/4121 §4.1: [APPLICATION 0] { KRB5 OID, TOK_ID 01 00, AP-REQ }.
	oidDER, _ := asn1.Marshal(gssapi.OIDKRB5.OID())
	if i := bytes.Index(mech, oidDER); i < 0 || !bytes.Equal(mech[i+len(oidDER):i+len(oidDER)+2], []byte{0x01, 0x00}) {
		t.Fatal("TOK_ID 01 00 does not follow the KRB5 OID")
	}
	if !types.IsFlagSet(&apreq.APOptions, flags.APOptionMutualRequired) {
		t.Fatal("AP-REQ does not require mutual authentication")
	}
	if err := apreq.DecryptAuthenticator(key); err != nil {
		t.Fatalf("authenticator does not decrypt under the session key: %v", err)
	}
	a := apreq.Authenticator
	if a.Cksum.CksumType != chksumtype.GSSAPI || !bytes.Equal(a.Cksum.Checksum, gssChecksum(bnd)) {
		t.Fatalf("authenticator checksum = %x, want the GSS checksum carrying Bnd", a.Cksum.Checksum)
	}
	if len(a.SubKey.KeyValue) != 32 || a.SubKey.KeyType != key.KeyType {
		t.Fatal("no initiator subkey of the session key's type")
	}
	if a.SeqNumber != auth.SeqNumber || !bytes.Equal(a.SubKey.KeyValue, auth.SubKey.KeyValue) {
		t.Fatal("returned authenticator does not match the one sent")
	}
}

func TestBuildAPREQRefusesABndThatIsNot16Bytes(t *testing.T) {
	cl, tkt := testTicketAndClient(t)
	if _, _, err := BuildAPREQ(cl, tkt, testKey(t, 7), make([]byte, 15)); err == nil {
		t.Fatal("a 15-byte Bnd was accepted")
	}
}

// fakeAPREP builds what an acceptor sends back: NegTokenResp{accept-completed,
// KRB5, [APPLICATION 0]{KRB5 OID, 02 00, AP-REP}}.
type apRepOpts struct {
	subkey   *types.EncryptionKey
	ctime    time.Time
	cusec    int
	negState int
	encKey   types.EncryptionKey
	mech     asn1.ObjectIdentifier
	mic      func(key types.EncryptionKey) []byte
}

func fakeAPREP(t *testing.T, o apRepOpts) []byte {
	t.Helper()
	part := messages.EncAPRepPart{CTime: o.ctime, Cusec: o.cusec, SequenceNumber: 12345}
	if o.subkey != nil {
		part.Subkey = *o.subkey
	}
	pb, err := asn1.Marshal(part)
	if err != nil {
		t.Fatal(err)
	}
	pb = asn1tools.AddASNAppTag(pb, asnAppTag.EncAPRepPart)
	ed, err := crypto.GetEncryptedData(pb, o.encKey, keyusage.AP_REP_ENCPART, 0)
	if err != nil {
		t.Fatal(err)
	}
	rep := messages.APRep{PVNO: 5, MsgType: msgtype.KRB_AP_REP, EncPart: ed}
	rb, err := asn1.Marshal(rep)
	if err != nil {
		t.Fatal(err)
	}
	rb = asn1tools.AddASNAppTag(rb, asnAppTag.APREP)
	oidDER, _ := asn1.Marshal(gssapi.OIDKRB5.OID())
	mech := asn1tools.AddASNAppTag(append(append(oidDER, 0x02, 0x00), rb...), 0)
	resp := spnego.NegTokenResp{NegState: asn1.Enumerated(o.negState), SupportedMech: o.mech, ResponseToken: mech}
	if o.mic != nil {
		key := o.encKey
		if o.subkey != nil {
			key = *o.subkey
		}
		resp.MechListMIC = o.mic(key)
	}
	b, err := resp.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestParseAPREPReturnsTheAcceptorSubkey(t *testing.T) {
	cl, tkt := testTicketAndClient(t)
	key := testKey(t, 7)
	_, auth, err := BuildAPREQ(cl, tkt, key, make([]byte, 16))
	if err != nil {
		t.Fatal(err)
	}
	sub := testKey(t, 9)
	reply := fakeAPREP(t, apRepOpts{subkey: &sub, ctime: auth.CTime, cusec: auth.Cusec, encKey: key, mech: gssapi.OIDKRB5.OID()})
	got, acceptor, err := ParseAPREP(reply, key, auth)
	if err != nil {
		t.Fatal(err)
	}
	if !acceptor || !bytes.Equal(got.KeyValue, sub.KeyValue) {
		t.Fatalf("wrap key = %x (acceptor=%v), want the acceptor subkey", got.KeyValue, acceptor)
	}
}

func TestParseAPREPWithoutAcceptorSubkeyUsesTheInitiatorSubkey(t *testing.T) {
	// RFC 4121 §2: the acceptor subkey if present, else the initiator subkey
	// if present, else the ticket session key. We always send an initiator
	// subkey, so the session key is never the wrap key.
	cl, tkt := testTicketAndClient(t)
	key := testKey(t, 7)
	_, auth, err := BuildAPREQ(cl, tkt, key, make([]byte, 16))
	if err != nil {
		t.Fatal(err)
	}
	reply := fakeAPREP(t, apRepOpts{ctime: auth.CTime, cusec: auth.Cusec, encKey: key, mech: gssapi.OIDKRB5.OID()})
	got, acceptor, err := ParseAPREP(reply, key, auth)
	if err != nil {
		t.Fatal(err)
	}
	if acceptor || !bytes.Equal(got.KeyValue, auth.SubKey.KeyValue) {
		t.Fatal("without an acceptor subkey the initiator subkey must key the wrap")
	}
}

func TestParseAPREPVerifiesAMechListMIC(t *testing.T) {
	cl, tkt := testTicketAndClient(t)
	key := testKey(t, 7)
	_, auth, err := BuildAPREQ(cl, tkt, key, make([]byte, 16))
	if err != nil {
		t.Fatal(err)
	}
	sub := testKey(t, 9)
	mechList, _ := asn1.Marshal([]asn1.ObjectIdentifier{gssapi.OIDKRB5.OID()})
	goodMIC := func(k types.EncryptionKey) []byte {
		m := gssapi.MICToken{Flags: gssapi.MICTokenFlagSentByAcceptor | gssapi.MICTokenFlagAcceptorSubkey, SndSeqNum: 1, Payload: mechList}
		if err := m.SetChecksum(k, keyusage.GSSAPI_ACCEPTOR_SIGN); err != nil {
			t.Fatal(err)
		}
		b, _ := m.Marshal()
		return b
	}
	reply := fakeAPREP(t, apRepOpts{subkey: &sub, ctime: auth.CTime, cusec: auth.Cusec, encKey: key, mech: gssapi.OIDKRB5.OID(), mic: goodMIC})
	if _, _, err := ParseAPREP(reply, key, auth); err != nil {
		t.Fatalf("a valid mechListMIC was rejected: %v", err)
	}
	badMIC := func(k types.EncryptionKey) []byte {
		b := goodMIC(k)
		b[len(b)-1] ^= 1
		return b
	}
	reply = fakeAPREP(t, apRepOpts{subkey: &sub, ctime: auth.CTime, cusec: auth.Cusec, encKey: key, mech: gssapi.OIDKRB5.OID(), mic: badMIC})
	if _, _, err := ParseAPREP(reply, key, auth); err == nil {
		t.Fatal("a tampered mechListMIC was accepted")
	}
}

func TestParseAPREPFailsClosed(t *testing.T) {
	cl, tkt := testTicketAndClient(t)
	key := testKey(t, 7)
	_, auth, err := BuildAPREQ(cl, tkt, key, make([]byte, 16))
	if err != nil {
		t.Fatal(err)
	}
	sub := testKey(t, 9)
	ok := apRepOpts{subkey: &sub, ctime: auth.CTime, cusec: auth.Cusec, encKey: key, mech: gssapi.OIDKRB5.OID()}

	cases := map[string][]byte{}
	wrongKey := ok
	wrongKey.encKey = testKey(t, 8)
	cases["AP-REP under a key other than the session key"] = fakeAPREP(t, wrongKey)
	replay := ok
	replay.ctime = auth.CTime.Add(-time.Hour)
	cases["AP-REP echoing a different ctime (not a reply to OUR authenticator)"] = fakeAPREP(t, replay)
	cusec := ok
	cusec.cusec = auth.Cusec + 1
	cases["AP-REP echoing a different cusec"] = fakeAPREP(t, cusec)
	reject := ok
	reject.negState = 2
	cases["negState reject"] = fakeAPREP(t, reject)
	ntlm := ok
	ntlm.mech = asn1.ObjectIdentifier{1, 3, 6, 1, 4, 1, 311, 2, 2, 10}
	cases["supportedMech NTLM"] = fakeAPREP(t, ntlm)
	empty, _ := (&spnego.NegTokenResp{NegState: 0, SupportedMech: gssapi.OIDKRB5.OID()}).Marshal()
	cases["no response token"] = empty
	cases["garbage"] = []byte("NTLMSSP\x00")
	cases["nothing"] = nil
	good := fakeAPREP(t, ok)
	tampered := append([]byte{}, good...)
	tampered[len(tampered)-3] ^= 0xFF
	cases["tampered ciphertext"] = tampered

	for name, reply := range cases {
		if _, _, err := ParseAPREP(reply, key, auth); err == nil {
			t.Errorf("%s: accepted; mutual authentication must fail closed", name)
		} else if !strings.Contains(err.Error(), "winrm:") {
			t.Errorf("%s: error %q is not attributed", name, err)
		}
	}
	if _, _, err := ParseAPREP(good, key, auth); err != nil {
		t.Fatalf("the untampered control case failed, so the cases above prove nothing: %v", err)
	}
}
