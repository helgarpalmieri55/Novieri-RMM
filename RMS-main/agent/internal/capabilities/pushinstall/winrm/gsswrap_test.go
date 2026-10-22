package winrm

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"testing"

	"github.com/jcmturner/gokrb5/v8/crypto"
	"github.com/jcmturner/gokrb5/v8/iana/etypeID"
	"github.com/jcmturner/gokrb5/v8/iana/keyusage"
	"github.com/jcmturner/gokrb5/v8/types"
)

func aesKey(t *testing.T, id int32) types.EncryptionKey {
	t.Helper()
	et, err := crypto.GetEtype(id)
	if err != nil {
		t.Fatal(err)
	}
	kb := make([]byte, et.GetKeyByteSize())
	for i := range kb {
		kb[i] = byte(i)
	}
	return types.EncryptionKey{KeyType: id, KeyValue: kb}
}

func aes256Key(t *testing.T) types.EncryptionKey { return aesKey(t, etypeID.AES256_CTS_HMAC_SHA1_96) }

// Round trip: fast self-consistency check (NOT the interop proof — the CI
// pyspnego oracle and TestWinRMWrapKnownAnswer are).
func TestWinRMWrapRoundTrip(t *testing.T) {
	for _, id := range []int32{etypeID.AES256_CTS_HMAC_SHA1_96, etypeID.AES128_CTS_HMAC_SHA1_96} {
		key := aesKey(t, id)
		msg := []byte("<s:Envelope/> the SOAP body")
		hdr, data, err := wrapWinRM(key, true, 7, msg)
		if err != nil {
			t.Fatal(err)
		}
		if bytes.Contains(append(append([]byte{}, hdr...), data...), msg) {
			t.Fatal("the payload appears in the token in clear — not sealed")
		}
		// Our own token is initiator-sealed; open it as the acceptor would.
		got, seq, err := unsealCFX(key, false, true, append(hdr, data...))
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(got, msg) || seq != 7 {
			t.Fatalf("round trip = %q seq %d, want %q seq 7", got, seq, msg)
		}
	}
}

// TestWinRMWrapHeaderLayout pins the RFC 4121 §4.2.6.2 header and the
// MS-WSMV split. The header is deterministic without the confounder, so this
// holds on every run.
func TestWinRMWrapHeaderLayout(t *testing.T) {
	for _, id := range []int32{etypeID.AES256_CTS_HMAC_SHA1_96, etypeID.AES128_CTS_HMAC_SHA1_96} {
		key := aesKey(t, id)
		msg := bytes.Repeat([]byte("x"), 1000)
		hdr, data, err := wrapWinRM(key, true, 0x0102030405, msg)
		if err != nil {
			t.Fatal(err)
		}
		want := []byte{0x05, 0x04, 0x06, 0xFF, 0x00, 0x00, 0x00, 28, 0, 0, 0, 0x01, 0x02, 0x03, 0x04, 0x05}
		if !bytes.Equal(hdr[:16], want) {
			t.Fatalf("etype %d header = %x, want %x (TOK_ID 0504, Sealed|AcceptorSubkey, EC 0, RRC 28, SND)", id, hdr[:16], want)
		}
		// MS-WSMV: the "signature" framed with a 4-byte length is the token
		// header + the RRC-rotated bytes + the confounder: 16+28+16 = 60, and
		// the encrypted data is exactly as long as the plaintext (EC = 0).
		if len(hdr) != 60 {
			t.Fatalf("etype %d signature length %d, want 60", id, len(hdr))
		}
		if len(data) != len(msg) {
			t.Fatalf("etype %d encrypted data length %d, want %d", id, len(data), len(msg))
		}
		hdr2, _, _ := wrapWinRM(key, false, 0, msg)
		if hdr2[2] != 0x02 {
			t.Fatalf("flags without an acceptor subkey = %#x, want 0x02 (Sealed)", hdr2[2])
		}
	}
}

// TestWinRMWrapIsGokrb5CiphertextRotated proves the body is RFC 3961
// encryption of {plaintext | header-with-RRC-0} (what MIT and Heimdal
// encrypt), right-rotated by RRC -- decrypting it with gokrb5's own
// DecryptMessage, not with our unwrap.
func TestWinRMWrapIsGokrb5CiphertextRotated(t *testing.T) {
	key := aes256Key(t)
	msg := []byte("rotate me")
	hdr, data, err := wrapWinRM(key, false, 42, msg)
	if err != nil {
		t.Fatal(err)
	}
	full := append(hdr, data...)
	body := rotateLeft(full[16:], 28)
	plain, err := crypto.DecryptMessage(body, key, keyusage.GSSAPI_INITIATOR_SEAL)
	if err != nil {
		t.Fatalf("the un-rotated body is not a GSSAPI_INITIATOR_SEAL ciphertext: %v", err)
	}
	if !bytes.Equal(plain[:len(msg)], msg) {
		t.Fatal("plaintext is not first")
	}
	copyHdr := plain[len(msg):]
	want := append([]byte{}, full[:16]...)
	want[6], want[7] = 0, 0 // the encrypted copy carries RRC 0
	if !bytes.Equal(copyHdr, want) {
		t.Fatalf("encrypted header copy = %x, want %x", copyHdr, want)
	}
}

func TestRotate(t *testing.T) {
	b := []byte{1, 2, 3, 4, 5}
	if got := rotateRight(b, 2); !bytes.Equal(got, []byte{4, 5, 1, 2, 3}) {
		t.Fatalf("rotateRight = %v", got)
	}
	if got := rotateLeft(rotateRight(b, 7), 7); !bytes.Equal(got, b) {
		t.Fatalf("rotateLeft(rotateRight) = %v", got)
	}
	if got := rotateRight(nil, 3); len(got) != 0 {
		t.Fatal("rotate of empty")
	}
}

// acceptorWrap simulates the SERVER's sealed reply with our primitive (the
// independent oracle is CI's pyspnego; this is the unit-speed version).
func acceptorWrap(t *testing.T, key types.EncryptionKey, seq uint64, msg []byte) ([]byte, []byte) {
	t.Helper()
	full, err := sealCFX(key, keyusage.GSSAPI_ACCEPTOR_SEAL, flagSentByAcceptor|flagSealed|flagAcceptorSubkey, seq, nil, msg)
	if err != nil {
		t.Fatal(err)
	}
	n := winrmHeaderLen(t, key)
	return full[:n], full[n:]
}

func winrmHeaderLen(t *testing.T, key types.EncryptionKey) int {
	t.Helper()
	et, err := wrapEtype(key.KeyType)
	if err != nil {
		t.Fatal(err)
	}
	return tokenHdrLen + tokenHdrLen + et.GetHMACBitLength()/8 + et.GetConfounderByteSize()
}

func TestUnwrapWinRMOpensTheAcceptorsReply(t *testing.T) {
	key := aes256Key(t)
	msg := []byte("<s:Envelope>reply</s:Envelope>")
	h, d := acceptorWrap(t, key, 99, msg)
	got, err := unwrapWinRM(key, true, h, d)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, msg) {
		t.Fatalf("got %q", got)
	}
}

func TestUnwrapWinRMFailsClosed(t *testing.T) {
	key := aes256Key(t)
	msg := []byte("<s:Envelope>reply</s:Envelope>")
	h, d := acceptorWrap(t, key, 99, msg)

	tamper := func(b []byte, i int) []byte { c := append([]byte{}, b...); c[i] ^= 0x01; return c }
	ours, oursD, _ := wrapWinRM(key, true, 1, msg)

	cases := map[string][2][]byte{
		"flipped ciphertext bit":                 {h, tamper(d, 3)},
		"flipped rotated-trailer bit":            {tamper(h, 20), d},
		"SND altered (header != its copy)":       {tamper(h, 15), d},
		"our own initiator token reflected back": {ours, oursD},
		"truncated":                              {h[:10], nil},
		"wrong token id":                         {tamper(h, 0), d},
	}
	noSub := append([]byte{}, h...)
	noSub[2] &^= flagAcceptorSubkey
	cases["AcceptorSubkey flag disagrees with the negotiated key"] = [2][]byte{noSub, d}
	unsealed := append([]byte{}, h...)
	unsealed[2] &^= flagSealed
	cases["not sealed (integrity-only token on an encrypted channel)"] = [2][]byte{unsealed, d}

	for name, c := range cases {
		if _, err := unwrapWinRM(key, true, c[0], c[1]); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	other := aes256Key(t)
	other.KeyValue[0] ^= 0xFF
	if _, err := unwrapWinRM(other, true, h, d); err == nil {
		t.Error("opened under the wrong key")
	}
}

func TestWrapRefusesNonAESKeys(t *testing.T) {
	rc4 := types.EncryptionKey{KeyType: etypeID.RC4_HMAC, KeyValue: make([]byte, 16)}
	if _, _, err := wrapWinRM(rc4, false, 0, []byte("x")); err == nil {
		t.Fatal("RC4 sealed")
	}
}

// TestFixedConfounderIsDeterministic: the confounder is the only randomness
// in a sealed token, so fixing it (the KAT's lever) makes the bytes
// reproducible, and production (nil) draws a fresh one every time. There is
// no package-level test seam: the confounder is a parameter of sealCFX.
func TestFixedConfounderIsDeterministic(t *testing.T) {
	key := aes256Key(t)
	conf := bytes.Repeat([]byte{0x42}, 16)
	a, err := sealCFX(key, keyusage.GSSAPI_INITIATOR_SEAL, flagSealed, 0, conf, []byte("known answer"))
	if err != nil {
		t.Fatal(err)
	}
	b, _ := sealCFX(key, keyusage.GSSAPI_INITIATOR_SEAL, flagSealed, 0, conf, []byte("known answer"))
	if !bytes.Equal(a, b) {
		t.Fatal("a fixed confounder must make the token reproducible")
	}
	h1, _, _ := wrapWinRM(key, false, 0, []byte("known answer"))
	h2, _, _ := wrapWinRM(key, false, 0, []byte("known answer"))
	if bytes.Equal(h1, h2) {
		t.Fatal("production confounder is not random")
	}
	if _, err := sealCFX(key, keyusage.GSSAPI_INITIATOR_SEAL, flagSealed, 0, conf[:15], []byte("x")); err == nil {
		t.Fatal("a short confounder was accepted")
	}
}

// KNOWN-ANSWER VECTOR (§I5), captured from the INDEPENDENT acceptor: MIT
// Kerberos 1.20 (Debian 12) via pyspnego 0.12.2's wrap_winrm, in the
// agent-winrm job's stack (deploy/ci/samba-ad + deploy/ci/winrm-acceptor,
// which writes kat.json), run locally on 2026-09-29 against this agent's
// client over 5985. The key is that run's negotiated acceptor subkey from a
// throwaway CI domain; it protects nothing.
//
// acceptor* is MIT SEALING a reply (SentByAcceptor|Sealed|AcceptorSubkey):
// the test (1) opens it with OUR unwrap, then (2) recovers MIT's confounder
// and re-seals the same plaintext with OUR sealCFX, which must reproduce MIT's
// signature and data BYTE FOR BYTE -- so a framing change that still
// round-trips against itself but diverges from MIT fails here, with no Samba
// stack. initiator* is OUR sealed request exactly as MIT received and
// decrypted it (to the recorded plaintext); our own unseal must agree.
var winrmKAT = struct {
	keyType                              int32
	keyHex                               string
	acceptorHeaderHex, acceptorDataHex   string
	acceptorPlaintext                    string
	initiatorHeaderHex, initiatorDataHex string
	initiatorPlaintext                   string
}{
	keyType: 18,
	keyHex:  "63de6d3a082989be4f7e6def59679e8a35afc63124661fd5836fad0116406133",
	acceptorHeaderHex: "050407ff0000001c0000000033e034e7a5d6dc9cdaa624981c8bf507e72c85ec8455800ca92b69e0fda43c4d4b8b1e45" +
		"9039ead9cc6161b1ec2d0f0c",
	acceptorDataHex: "b5ee5a440aac10f02b1d02a2b07368d23759cd673462cba1d39ae55093188677384a1e3e6f3b1a1378b1a9aa049d8140" +
		"2e1bb93b97dec8d1738774cd9949874679ff17672fad1147aacb62fa21dd7564c8f3417fb5e85962b0c07a4e8512000b" +
		"c753a3d2bec36ace00506832e62956f9310f15ff7fcd0c0ed0806be893c1b71298a397dfe33b2edf89f4518e5be43857" +
		"9833f93e53a521ff182182cdbd930533397f0c050ad09445b001e46a4477aa2a5e6ca543aa669ecfdae18e85f315a63d" +
		"429eae4a9b93e06e531098977546a539f05e3f17e859025108562d38fcfdf32b",
	acceptorPlaintext: "<s:Envelope xmlns:s=\"http://www.w3.org/2003/05/soap-envelope\" xmlns:rsp=\"http://schemas.microsoft.com/wbem/wsman/1/windows/shell\"><s:Body><rsp:Shell><rsp:ShellId>ACCEPTOR-SHELL</rsp:ShellId></rsp:Shell></s:Body></s:Envelope>",
	initiatorHeaderHex: "050406ff0000001c00000000383aec4e4d5ed8aa681762462d01e9c2cb7b1355977de9a5864ff37269b9bab2667f7ad5" +
		"85b98bc98d7146cb5d39d46b",
	initiatorDataHex: "eb9454111778886b4ebc98ea64c5812777b6142222decf68215481571bbfa0459cb1d634b90e80c5d5e535bebb42ed40" +
		"08fea1206d5d8aef1612a7e0baa0a5c07fce7d289eeaf369d6d90dc2a1cdd0979361c62fd14575e9dc9cb83337426345" +
		"dff9fd25cbefe6dfc7fc98f3fcedad3c4e63e1dc9029061fd11a9cf4aa09eed064d3eb5fb6cce2ae21cf2fa725a48bc6" +
		"d1ea35d29b0c38a4f7c7d2610aecce40fc6b74d7dcdd5fee2263f320860506ace5a7bd2486d381f425b70341b995657e" +
		"a6c7b4d4b1881f1dff1fce9192377a0b1f05f66527c997974b7b39848fd702a44780fbded9560efc64694f22ae6e1818" +
		"70ec9dc83ef527ba7f3e6136a9b58a886916e31c8e289698b9b1a4c981548fe0a3a80deb7a66b5203e689e3d15e16261" +
		"f13b8fdfafd3e0e9af50343162bf724797a33b177b98474b59b91bae04a8c82fdc017d1c95070bc7c3cc918203ece9ab" +
		"27acaa3e3684ed84a7feb5fe55a3786f04b170adea6007b9d0758966b6a400dd1b880e1798107dbc1dc541d8a44a7db9" +
		"8f6958f32f4769a6db54ebc0a0122049d4ba07d646b5e9724683572fb46b9af1980416bafc54878aa5e1272b38f1d7a2" +
		"26e4d8a77664dd453107feaaece2400ec76b506bf8dc543e307a19b1f7a9f7792a8e9c781a0bd3c060d1aef169c35d29" +
		"86978c99736148cd4599b566d3d5899a3c969e6995ad9f197b56d78edbca9be4968a3cc41528892bb82a6e06d662692a" +
		"de20f5cc94e3efeb27b139ad0400e0b96515cef0ba43c547847576084d6c2296bca4ba7abae64ef72268441a1c48320b" +
		"39d00f1b8225e3bb4b573934bc4c71c8481edb7bf13c2f502118127294c1d3e907f2666c842f650f2b848e66ebc4a3d2" +
		"17b89dbc4cf9a24a9dc6bbd17f7290eda97db8df5e920fb0a5c6455cc4d0f606df2e47b960ca5dbf1b1a491a74866294" +
		"f66f52dd0e2ed78cdf0b36519951352827f86baa73714f51fa61eadb091614b464a0231e8db4aa8561c400077bc0762c" +
		"85f6cd245c6481a371bf85aa11ee491d4875dbe0e15c45edf71ce68cc078ac5d218b9b836017be546d0c0b25aa9fa53f" +
		"7036b4969defd02752399ae93964631ba295df4f81af90462036f2a9fd55e849f79852b4dc4971f36decec9e34175a24" +
		"281561f685f7aa92ce8e9c6145756fd47bfad85a801f14406f236485d00da947e2d1fb5660bed0ae6a2dbba26323eb84" +
		"9a678a975bf3e2768593953ee2c23e0248f06c46a7b35b5138711d78b6f2ee02aff1ef2ea5d46a436e13b4831b04f3f8" +
		"de1eb4645638e707dafd89e1d4e6ffb4741a4b68d8187ce1e17fdb72c5d70c9beeb4ebe29884f3983275fcc35aa2e263" +
		"4ad7823ea345b8466962c86921ba37f271a7341575978f208975a999883090df2965804f627b07f21b1911633a661d8c" +
		"cc68469a81016071710ef919cdb989e5cbf00fa31fa137bb5089135f7f43f9817794994f1fa36a00a4fa82aa798fdd5d" +
		"365b7c1c968868c3ba1fd8137968b483d9c497f439102e4c6aed5b3c6d64d6a446dab5192720cf350e0e371c663317ff" +
		"7a3f7a856b91a108b54f3f0d524e37662f73c37eea0505cb1a979a6688e4f4300afedc365c9eb153dfb294d2df5b97d0" +
		"1653e1f6ed24b9b66dd33e6fe296c34d8c9056e5bccc9a526fa78f605eb3c884827610a1c4904512d772b0a68277cdf5" +
		"37f50bdcc142a70ac5e6cc7c712fb5b80d5c46c1b80584724e69fad2fd6ad0e240e1c5d326a6444ac1dfdee611386d48" +
		"81a8b9ad72bd47901e8753f083c19e345da5bcf7eb657c1f1be1e55e9054b62105bb039c4101030b",
	initiatorPlaintext: "<s:Envelope xmlns:s=\"http://www.w3.org/2003/05/soap-envelope\" xmlns:a=\"http://schemas.xmlsoap.org/ws/2004/08/addressing\" xmlns:w=\"http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd\" xmlns:p=\"http://schemas.microsoft.com/wbem/wsman/1/wsman.xsd\" xmlns:rsp=\"http://schemas.microsoft.com/wbem/wsman/1/windows/shell\"><s:Header><a:To>http://pc1.corp.example.com:5985/wsman</a:To><a:ReplyTo><a:Address s:mustUnderstand=\"true\">http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:Address></a:ReplyTo><w:MaxEnvelopeSize s:mustUnderstand=\"true\">153600</w:MaxEnvelopeSize><a:MessageID>uuid:3F984512-AC68-4352-A0B7-21D1C264D93E</a:MessageID><w:Locale xml:lang=\"en-US\" s:mustUnderstand=\"false\"/><p:DataLocale xml:lang=\"en-US\" s:mustUnderstand=\"false\"/><w:OperationTimeout>PT60S</w:OperationTimeout><w:ResourceURI s:mustUnderstand=\"true\">http://schemas.microsoft.com/wbem/wsman/1/windows/shell/cmd</w:ResourceURI><a:Action s:mustUnderstand=\"true\">http://schemas.xmlsoap.org/ws/2004/09/transfer/Create</a:Action><w:OptionSet><w:Option Name=\"WINRS_NOPROFILE\">TRUE</w:Option><w:Option Name=\"WINRS_CODEPAGE\">65001</w:Option></w:OptionSet></s:Header><s:Body><rsp:Shell><rsp:InputStreams>stdin</rsp:InputStreams><rsp:OutputStreams>stdout stderr</rsp:OutputStreams></rsp:Shell></s:Body></s:Envelope>",
}

func TestWinRMWrapKnownAnswer(t *testing.T) {
	kb, _ := hex.DecodeString(winrmKAT.keyHex)
	key := types.EncryptionKey{KeyType: winrmKAT.keyType, KeyValue: kb}
	hdr, _ := hex.DecodeString(winrmKAT.acceptorHeaderHex)
	data, _ := hex.DecodeString(winrmKAT.acceptorDataHex)
	if len(hdr) != 60 {
		t.Fatalf("MIT's signature is %d bytes; our split says 60", len(hdr))
	}

	// (1) Our unwrap opens MIT's sealed reply.
	got, err := unwrapWinRM(key, hdr[2]&flagAcceptorSubkey != 0, hdr, data)
	if err != nil {
		t.Fatalf("our unwrap cannot open MIT's token: %v", err)
	}
	if string(got) != winrmKAT.acceptorPlaintext {
		t.Fatalf("our unwrap of MIT's token = %q, want %q", got, winrmKAT.acceptorPlaintext)
	}

	// (2) Our seal reproduces MIT's bytes exactly, given MIT's confounder.
	full := append(append([]byte{}, hdr...), data...)
	rrc := int(binary.BigEndian.Uint16(full[6:8]))
	raw, err := rawDecrypt(key, keyusage.GSSAPI_ACCEPTOR_SEAL, rotateLeft(full[tokenHdrLen:], rrc))
	if err != nil {
		t.Fatal(err)
	}
	conf := raw[:16]
	again, err := sealCFX(key, keyusage.GSSAPI_ACCEPTOR_SEAL, full[2], binary.BigEndian.Uint64(full[8:16]), conf, got)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(again, full) {
		t.Fatalf("our seal diverges from MIT's:\n ours %x\n MIT  %x", again, full)
	}
	n, _ := winrmSplit(key)
	if n != len(hdr) {
		t.Fatalf("our MS-WSMV split is %d, MIT's is %d", n, len(hdr))
	}

	// (3) The request MIT decrypted from us opens, under our code, to the same
	// plaintext MIT recorded.
	ih, _ := hex.DecodeString(winrmKAT.initiatorHeaderHex)
	id, _ := hex.DecodeString(winrmKAT.initiatorDataHex)
	plain, _, err := unsealCFX(key, false, true, append(ih, id...))
	if err != nil {
		t.Fatal(err)
	}
	if string(plain) != winrmKAT.initiatorPlaintext {
		t.Fatal("our unseal of our own request disagrees with what MIT decrypted")
	}
}
