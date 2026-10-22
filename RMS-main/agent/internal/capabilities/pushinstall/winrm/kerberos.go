// Package winrm is the site-server agent's Kerberos-only WinRM client for a
// Windows push (design §2). It is pure Go: the TGT is minted from the push
// credential's password with gokrb5, so no site server needs a system krb5, an
// /etc/krb5.conf or a keytab, and a Linux site server can push to Windows.
//
// KERBEROS ONLY, STRUCTURALLY. The client emits only a SPNEGO Negotiate token
// carrying a Kerberos AP-REQ, offers only the Kerberos mechanism, and accepts
// only a Kerberos AP-REP back. There is NO NTLM, Basic or CredSSP code in this
// package -- refusal is the absence of a code path, not a flag (design §2.3).
// refusal_test.go asserts the package imports no NTLM library and wsman_test.go
// that a non-Kerberos challenge is refused with no second attempt.
//
// DORMANT. Nothing here runs, dials or allocates until a verified push job
// calls it; a Session lives for one job and is destroyed at its end.
package winrm

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"strconv"
	"strings"
	"time"

	"github.com/jcmturner/gofork/encoding/asn1"
	"github.com/jcmturner/gokrb5/v8/asn1tools"
	"github.com/jcmturner/gokrb5/v8/client"
	"github.com/jcmturner/gokrb5/v8/config"
	"github.com/jcmturner/gokrb5/v8/crypto"
	"github.com/jcmturner/gokrb5/v8/crypto/etype"
	"github.com/jcmturner/gokrb5/v8/gssapi"
	"github.com/jcmturner/gokrb5/v8/iana/chksumtype"
	"github.com/jcmturner/gokrb5/v8/iana/etypeID"
	"github.com/jcmturner/gokrb5/v8/iana/flags"
	"github.com/jcmturner/gokrb5/v8/iana/keyusage"
	"github.com/jcmturner/gokrb5/v8/messages"
	"github.com/jcmturner/gokrb5/v8/spnego"
	"github.com/jcmturner/gokrb5/v8/types"
)

// Creds is the decrypted Windows domain account. KDC and CABundle are optional.
type Creds struct {
	Username string
	Password string
	KDC      string // "" => SRV discovery; else host or host:port
	CABundle string // optional PEM added to RootCAs for 5986 (used by wsman.go)
}

// Session holds a logged-in gokrb5 client for ONE push job (install path only).
// It never outlives the job (idle budget): Close destroys it.
type Session struct {
	cl    *client.Client
	realm string
}

// kdcDialTimeout bounds the probe's plain TCP dial to a KDC.
const kdcDialTimeout = 5 * time.Second

// RealmFor derives the Kerberos realm, credential-free. A UPN (`user@domain`)
// carries its own; a `DOMAIN\user` (or no username at all, as in the probe)
// uses the AD DNS domain -- everything after the first label of the FQDN.
// Upper-cased (AD realms are upper-case).
func RealmFor(username, fqdn string) string {
	if i := strings.IndexByte(username, '@'); i >= 0 {
		return strings.ToUpper(username[i+1:])
	}
	fqdn = strings.TrimSuffix(fqdn, ".")
	if _, rest, ok := strings.Cut(fqdn, "."); ok {
		return strings.ToUpper(rest)
	}
	return strings.ToUpper(fqdn)
}

// RealmOfAccount is the realm a push ACCOUNT names (review I1): the domain of
// its user principal name, upper-cased. A down-level `DOMAIN\user` names a
// NetBIOS domain, not a realm, and is refused -- deriving the realm from the
// TARGET's DNS name instead let whoever wrote that name choose which KDC
// received the account's pre-authentication. The control plane refuses the
// same accounts when they are saved.
func RealmOfAccount(username string) (string, error) {
	user, domain, ok := strings.Cut(strings.TrimSpace(username), "@")
	if !ok || user == "" || strings.ContainsAny(user, `\`) || strings.Contains(domain, "@") {
		return "", errors.New("winrm: the account must be a user principal name (user@domain); " +
			"its domain is the Kerberos realm, and DOMAIN\\user names none")
	}
	labels := strings.Split(strings.TrimSuffix(domain, "."), ".")
	if len(labels) < 2 {
		return "", errors.New("winrm: the account's domain has no dot, so it names no realm")
	}
	for _, l := range labels {
		if l == "" || len(l) > 63 || strings.Trim(l, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-") != "" ||
			l[0] == '-' || l[len(l)-1] == '-' {
			return "", errors.New("winrm: the account's domain is not a DNS domain")
		}
	}
	return strings.ToUpper(strings.TrimSuffix(domain, ".")), nil
}

// InRealm reports whether fqdn is a host inside realm's DNS domain: a
// case-insensitive suffix match on a label boundary.
func InRealm(fqdn, realm string) bool {
	domain := strings.ToLower(strings.TrimSuffix(strings.TrimSpace(realm), "."))
	name := strings.ToLower(strings.TrimSuffix(strings.TrimSpace(fqdn), "."))
	return domain != "" && strings.HasSuffix(name, "."+domain)
}

// bareUser strips the NetBIOS domain or the UPN suffix: the realm is carried
// separately, as the Kerberos principal requires.
func bareUser(username string) string {
	if i := strings.LastIndexByte(username, '\\'); i >= 0 {
		return username[i+1:]
	}
	if i := strings.IndexByte(username, '@'); i >= 0 {
		return username[:i]
	}
	return username
}

// withKDCPort appends :88 to a host with no port (IPv6 literals included).
func withKDCPort(k string) string {
	if _, _, err := net.SplitHostPort(k); err == nil {
		return k
	}
	return net.JoinHostPort(strings.Trim(k, "[]"), "88")
}

// kdcTargets returns the KDC host:port list, credential-free: the explicit KDC
// if given, else DNS SRV `_kerberos._tcp.<realm>`, looked up under ctx.
func kdcTargets(ctx context.Context, realm, explicitKDC string) []string {
	if k := strings.TrimSpace(explicitKDC); k != "" {
		return []string{withKDCPort(k)}
	}
	_, addrs, err := net.DefaultResolver.LookupSRV(ctx, "kerberos", "tcp", strings.ToLower(realm))
	if err != nil {
		return nil
	}
	out := make([]string, 0, len(addrs))
	for _, a := range addrs {
		out = append(out, net.JoinHostPort(strings.TrimSuffix(a.Target, "."), strconv.Itoa(int(a.Port))))
	}
	return out
}

// KDCReachable is the PROBE's check (design §2.4, §C1): NO CREDENTIAL, NO AS
// EXCHANGE. It discovers the KDC (SRV or explicit) and does a plain TCP dial to
// it. A KDC that accepts a TCP connection is "reachable"; whether the
// credential is valid is the install's business, not the probe's.
//
// Discovery and every dial run under the probe's ctx (Codex on #90): a
// cancelled or expired probe stops here instead of working through a domain's
// stale DC records at kdcDialTimeout each.
func KDCReachable(ctx context.Context, realm, explicitKDC string) bool {
	d := net.Dialer{Timeout: kdcDialTimeout}
	for _, hp := range kdcTargets(ctx, realm, explicitKDC) {
		if ctx.Err() != nil {
			return false
		}
		conn, err := d.DialContext(ctx, "tcp", hp)
		if err == nil {
			_ = conn.Close()
			return true
		}
	}
	return false
}

// aesOnly is the enctype set: AES-CTS-HMAC-SHA1-96 (RFC 3962), what AD issues
// by default and what gsswrap.go implements. RC4 and DES are never requested.
var aesOnly = []int32{etypeID.AES256_CTS_HMAC_SHA1_96, etypeID.AES128_CTS_HMAC_SHA1_96}

// wrapEtype returns the etype for a key type, refusing anything but the two
// RFC 3962 AES enctypes: the only ones gsswrap.go's RFC 4121 framing (and its
// RRC arithmetic) is written for. A ticket or subkey of any other type fails
// the push closed rather than being sealed with a layout nobody tested.
func wrapEtype(id int32) (etype.EType, error) {
	for _, ok := range aesOnly {
		if id == ok {
			return crypto.GetEtype(id)
		}
	}
	return nil, fmt.Errorf("winrm: key type %d is not AES-CTS-HMAC-SHA1-96; refusing", id)
}

// krb5Config builds an in-memory krb5 config. No file is read.
func krb5Config(realm, explicitKDC string) *config.Config {
	c := config.New()
	c.LibDefaults.DefaultRealm = realm
	c.LibDefaults.DNSLookupKDC = strings.TrimSpace(explicitKDC) == ""
	names := []string{"aes256-cts-hmac-sha1-96", "aes128-cts-hmac-sha1-96"}
	c.LibDefaults.DefaultTGSEnctypes = names
	c.LibDefaults.DefaultTktEnctypes = names
	c.LibDefaults.PermittedEnctypes = names
	c.LibDefaults.DefaultTGSEnctypeIDs = append([]int32(nil), aesOnly...)
	c.LibDefaults.DefaultTktEnctypeIDs = append([]int32(nil), aesOnly...)
	c.LibDefaults.PermittedEnctypeIDs = append([]int32(nil), aesOnly...)
	// TCP only: AD replies routinely exceed a UDP datagram, and the probe's
	// reachability answer is a TCP one -- they must be the same path.
	c.LibDefaults.UDPPreferenceLimit = 1
	if k := strings.TrimSpace(explicitKDC); k != "" {
		c.Realms = append(c.Realms, config.Realm{Realm: realm, KDC: []string{withKDCPort(k)}})
	}
	return c
}

// Login mints a TGT from the password (install path only; the probe never
// calls this). Pure Go. The realm is the ACCOUNT's (RealmOfAccount), the KDC
// is the credential's explicit one or SRV-discovered for THAT realm, and the
// target must be inside it -- all refused before any lookup or exchange.
func Login(creds Creds, fqdn string) (*Session, error) {
	if strings.TrimSpace(creds.Password) == "" {
		return nil, errors.New("winrm: empty password")
	}
	if bareUser(creds.Username) == "" {
		return nil, errors.New("winrm: empty username")
	}
	realm, err := RealmOfAccount(creds.Username)
	if err != nil {
		return nil, err
	}
	if !InRealm(fqdn, realm) {
		return nil, fmt.Errorf("winrm: %s is not in the account's realm %s; its password is never "+
			"presented for a machine outside its own domain", fqdn, realm)
	}
	cfg := krb5Config(realm, creds.KDC)
	cl := client.NewWithPassword(bareUser(creds.Username), realm, creds.Password, cfg,
		client.DisablePAFXFAST(true)) // match Windows: no FAST armor.
	if err := cl.Login(); err != nil {
		cl.Destroy()
		return nil, fmt.Errorf("winrm: kerberos login (realm %s): %w", realm, err)
	}
	return &Session{cl: cl, realm: realm}, nil
}

// Realm is the realm the session logged in to.
func (s *Session) Realm() string { return s.realm }

// ServiceTicket gets the HTTP/<fqdn> ticket and its session key.
func (s *Session) ServiceTicket(fqdn string) (messages.Ticket, types.EncryptionKey, error) {
	return s.cl.GetServiceTicket("HTTP/" + strings.ToLower(strings.TrimSuffix(fqdn, ".")))
}

// Close destroys the client's tickets and keys. The session is job-scoped.
func (s *Session) Close() {
	if s != nil && s.cl != nil {
		s.cl.Destroy()
	}
}

// gssFlags are the RFC 4121 §4.1.1 checksum flags we request: mutual auth,
// integrity and confidentiality. DELEGATION IS OFF (no 28-byte Deleg field).
const (
	gssFlagMutual = 0x02
	gssFlagConf   = 0x10
	gssFlagInteg  = 0x20
)

// gssChecksum builds the RFC 4121 §4.1.1 authenticator checksum:
//
//	Lgth (4, LE) = 16
//	Bnd  (16)    = MD5 of the channel-bindings structure (16 zero bytes on 5985)
//	Flags(4, LE) = Mutual|Integ|Conf, Deleg OFF
//
// This is what gokrb5's default authenticator cannot set, which is why we
// build the AP-REQ ourselves (§I2/W8).
func gssChecksum(bnd []byte) []byte {
	b := make([]byte, 24)
	binary.LittleEndian.PutUint32(b[0:4], 16)
	copy(b[4:20], bnd)
	binary.LittleEndian.PutUint32(b[20:24], gssFlagMutual|gssFlagInteg|gssFlagConf)
	return b
}

// tokIDAPReq is the GSS-API KRB5 AP-REQ mech-token id (RFC 4121 §4.1,
// big-endian on the wire: 01 00). The AP-REP (02 00) is parsed by gokrb5.
var tokIDAPReq = []byte{0x01, 0x00}

// krb5MechOIDs are the two OIDs a Kerberos acceptor may name: the IETF one we
// offer, and the legacy Microsoft one Windows sometimes answers with. Nothing
// else is accepted as the negotiated mechanism.
func isKerberosMech(oid asn1.ObjectIdentifier) bool {
	return oid.Equal(gssapi.OIDKRB5.OID()) || oid.Equal(gssapi.OIDMSLegacyKRB5.OID())
}

// mechTypeList is the DER of the MechTypeList we send -- Kerberos alone. A
// mechListMIC from the acceptor is computed over exactly these bytes.
func mechTypeList() []asn1.ObjectIdentifier {
	return []asn1.ObjectIdentifier{gssapi.OIDKRB5.OID()}
}

// BuildAPREQ builds the SPNEGO NegTokenInit carrying an AP-REQ whose
// authenticator checksum is the GSS checksum with channel bindings (§I2). It
// requests mutual auth so the server returns an AP-REP we can verify, and
// carries an initiator subkey and sequence number. It returns the
// authenticator, which ParseAPREP needs to verify the reply is to THIS request
// and which carries the initial send sequence number for Wrap.
//
// bnd must be exactly 16 bytes: channelBindingMD5 on 5986, zeroes on 5985.
func BuildAPREQ(cl *client.Client, tkt messages.Ticket, sessionKey types.EncryptionKey, bnd []byte) ([]byte, types.Authenticator, error) {
	if len(bnd) != 16 {
		return nil, types.Authenticator{}, fmt.Errorf("winrm: channel-binding hash is %d bytes, want 16", len(bnd))
	}
	if _, err := wrapEtype(sessionKey.KeyType); err != nil {
		return nil, types.Authenticator{}, err
	}
	auth, err := types.NewAuthenticator(cl.Credentials.Domain(), cl.Credentials.CName())
	if err != nil {
		return nil, types.Authenticator{}, fmt.Errorf("winrm: authenticator: %w", err)
	}
	if err := auth.GenerateSeqNumberAndSubKey(sessionKey.KeyType, len(sessionKey.KeyValue)); err != nil {
		return nil, types.Authenticator{}, fmt.Errorf("winrm: subkey: %w", err)
	}
	auth.Cksum = types.Checksum{CksumType: chksumtype.GSSAPI, Checksum: gssChecksum(bnd)}
	apReq, err := messages.NewAPReq(tkt, sessionKey, auth)
	if err != nil {
		return nil, types.Authenticator{}, fmt.Errorf("winrm: AP-REQ: %w", err)
	}
	types.SetFlag(&apReq.APOptions, flags.APOptionMutualRequired)
	der, err := apReq.Marshal()
	if err != nil {
		return nil, types.Authenticator{}, fmt.Errorf("winrm: AP-REQ marshal: %w", err)
	}
	// RFC 4121 §4.1 InitialContextToken: [APPLICATION 0] { thisMech OID,
	// TOK_ID 01 00, AP-REQ }. Built by hand because gokrb5's KRB5Token keeps
	// its token id unexported.
	oidDER, err := asn1.Marshal(gssapi.OIDKRB5.OID())
	if err != nil {
		return nil, types.Authenticator{}, err
	}
	inner := make([]byte, 0, len(oidDER)+2+len(der))
	inner = append(inner, oidDER...)
	inner = append(inner, tokIDAPReq...)
	inner = append(inner, der...)
	mech := asn1tools.AddASNAppTag(inner, 0)

	tok := spnego.SPNEGOToken{
		Init: true,
		NegTokenInit: spnego.NegTokenInit{
			MechTypes:      mechTypeList(),
			MechTokenBytes: mech,
		},
	}
	out, err := tok.Marshal()
	if err != nil {
		return nil, types.Authenticator{}, fmt.Errorf("winrm: SPNEGO marshal: %w", err)
	}
	return out, auth, nil
}

// negState values (RFC 4178 §4.2.2).
const negStateAcceptCompleted = 0

// ParseAPREP completes mutual auth (§C2/W7). It decodes the server's SPNEGO
// reply (a NegTokenResp that must be accept-completed with Kerberos as the
// mechanism), extracts the AP-REP, decrypts its EncPart with the ticket
// session key (usage AP_REP_ENCPART=12), checks it echoes OUR authenticator's
// ctime/cusec (RFC 4120 §3.2.5 -- otherwise it is not a reply to this
// request), and verifies a mechListMIC if the acceptor sent one.
//
// It returns the key every Wrap/Unwrap uses (RFC 4121 §2): the acceptor subkey
// when the AP-REP carries one (acceptorSubkey=true), else the initiator subkey
// we sent, else the ticket session key.
//
// It FAILS CLOSED: an absent, rejected, non-Kerberos, undecryptable or
// mismatching reply is an error, never a silent downgrade.
func ParseAPREP(reply []byte, sessionKey types.EncryptionKey, auth types.Authenticator) (types.EncryptionKey, bool, error) {
	fail := func(format string, a ...any) (types.EncryptionKey, bool, error) {
		return types.EncryptionKey{}, false, fmt.Errorf("winrm: "+format+"; mutual authentication failed", a...)
	}
	if len(reply) == 0 {
		return fail("server sent no AP-REP")
	}
	var mechTok, mic []byte
	if reply[0] == 0xa1 { // NegTokenResp
		_, nt, err := spnego.UnmarshalNegToken(reply)
		if err != nil {
			return fail("server reply is not a SPNEGO token: %v", err)
		}
		resp, ok := nt.(spnego.NegTokenResp)
		if !ok {
			return fail("server reply is not a NegTokenResp")
		}
		if int(resp.NegState) != negStateAcceptCompleted {
			return fail("server negotiation state is %d, not accept-completed", resp.NegState)
		}
		if len(resp.SupportedMech) > 0 && !isKerberosMech(resp.SupportedMech) {
			return fail("server selected mechanism %s, not Kerberos", resp.SupportedMech.String())
		}
		mechTok, mic = resp.ResponseToken, resp.MechListMIC
	} else {
		// A bare Kerberos mech token (no SPNEGO wrapper) is still Kerberos.
		mechTok = reply
	}
	if len(mechTok) == 0 {
		return fail("server sent no AP-REP")
	}
	var kt spnego.KRB5Token
	if err := kt.Unmarshal(mechTok); err != nil {
		return fail("AP-REP decode: %v", err)
	}
	if kt.IsKRBError() {
		return fail("server returned a Kerberos error: %s", kt.KRBError.Error())
	}
	if !kt.IsAPRep() {
		return fail("server reply is not an AP-REP")
	}
	plain, err := crypto.DecryptEncPart(kt.APRep.EncPart, sessionKey, keyusage.AP_REP_ENCPART)
	if err != nil {
		return fail("AP-REP does not decrypt under the ticket session key: %v", err)
	}
	var part messages.EncAPRepPart
	if err := part.Unmarshal(plain); err != nil {
		return fail("AP-REP EncPart: %v", err)
	}
	if part.CTime.Unix() != auth.CTime.Unix() || part.Cusec != auth.Cusec {
		return fail("AP-REP does not echo our authenticator's timestamp")
	}

	key, acceptor := sessionKey, false
	switch {
	case len(part.Subkey.KeyValue) > 0:
		key, acceptor = part.Subkey, true
	case len(auth.SubKey.KeyValue) > 0:
		key = auth.SubKey
	}
	if _, err := wrapEtype(key.KeyType); err != nil {
		return fail("negotiated key: %v", err)
	}
	if len(mic) > 0 {
		payload, err := asn1.Marshal(mechTypeList())
		if err != nil {
			return fail("mechListMIC payload: %v", err)
		}
		var m gssapi.MICToken
		if err := m.Unmarshal(mic, true); err != nil {
			return fail("mechListMIC: %v", err)
		}
		m.Payload = payload
		if ok, err := m.Verify(key, keyusage.GSSAPI_ACCEPTOR_SIGN); !ok {
			return fail("mechListMIC does not verify: %v", err)
		}
	}
	return key, acceptor, nil
}
