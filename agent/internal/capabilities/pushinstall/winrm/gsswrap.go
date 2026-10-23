package winrm

// Sealed GSS-API Wrap tokens for WinRM Kerberos message encryption on HTTP/5985
// (design §2.3: "never unencrypted"). RFC 4121 §4.2.6.2, framed for MS-WSMV
// 2.2.9.1.2. gokrb5 ships only a SIGN-ONLY WrapToken, so the sealed variant is
// built here from its exported RFC 3961/3962 primitives.
//
// TOKEN (RFC 4121 §4.2.6.2, §4.2.5 rotation):
//
//	header(16) = TOK_ID 05 04 | Flags | Filler FF | EC(2, BE) | RRC(2, BE) | SND(8, BE)
//	body       = rotateRight( E(confounder | plaintext | header'), RRC )
//
// Flags: 0x02 Sealed, | 0x04 AcceptorSubkey when the acceptor sent a subkey,
// | 0x01 SentByAcceptor on the server's tokens. EC = 0: AES-CTS needs no
// padding. header' is the header with RRC = 0 -- what MIT and Heimdal encrypt,
// and what Heimdal compares against after zeroing RRC on receipt. RRC = 16 + the
// HMAC trailer (12 for the SHA1-96 etypes) = 28, computed not hardcoded, so the
// encrypted header copy and the checksum sit at the front of the body, as
// Windows' SSPI and MIT's IOV wrap put them.
//
// MS-WSMV SPLIT (pyspnego WinRMWrapResult, pywinrm _build_message): what is
// framed with the 4-byte LE length is the "signature" = header(16) + the first
// RRC + confounder bytes of the body (16+28+16 = 60 for AES); the rest is the
// encrypted data, exactly as long as the plaintext. The split is not cosmetic:
// an acceptor using an IOV unwrap (MIT, SSPI) takes the header buffer from that
// length, so a 16-byte "signature" -- what the plan text first specified --
// would be refused by every real receiver.

import (
	"crypto/hmac"
	"crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"

	"github.com/jcmturner/gokrb5/v8/crypto/common"
	"github.com/jcmturner/gokrb5/v8/iana/keyusage"
	"github.com/jcmturner/gokrb5/v8/types"
)

const (
	wrapTokenID        = 0x0504
	flagSentByAcceptor = 0x01
	flagSealed         = 0x02
	flagAcceptorSubkey = 0x04
	tokenHdrLen        = 16
)

func header16(flags byte, ec, rrc uint16, snd uint64) []byte {
	h := make([]byte, tokenHdrLen)
	binary.BigEndian.PutUint16(h[0:2], wrapTokenID)
	h[2] = flags
	h[3] = 0xFF
	binary.BigEndian.PutUint16(h[4:6], ec)
	binary.BigEndian.PutUint16(h[6:8], rrc)
	binary.BigEndian.PutUint64(h[8:16], snd)
	return h
}

// rotateRight rotates b right by n (RRC), matching gss_krb5 wrap.
func rotateRight(b []byte, n int) []byte {
	if len(b) == 0 {
		return []byte{}
	}
	n %= len(b)
	out := make([]byte, len(b))
	copy(out, b[len(b)-n:])
	copy(out[n:], b[:len(b)-n])
	return out
}

func rotateLeft(b []byte, n int) []byte {
	if len(b) == 0 {
		return []byte{}
	}
	n %= len(b)
	out := make([]byte, len(b))
	copy(out, b[n:])
	copy(out[len(b)-n:], b[:n])
	return out
}

// rawEncrypt is RFC 3961 simplified-profile encryption with an explicit
// confounder: E_Ke(conf | msg) | HMAC_Ki(conf | msg)[:h]. It is exactly
// gokrb5's rfc3962.EncryptMessage with the random confounder lifted out, so
// the known-answer test can reproduce an independent implementation's bytes.
func rawEncrypt(key types.EncryptionKey, usage uint32, conf, msg []byte) ([]byte, error) {
	et, err := wrapEtype(key.KeyType)
	if err != nil {
		return nil, err
	}
	if len(conf) != et.GetConfounderByteSize() {
		return nil, fmt.Errorf("winrm: confounder is %d bytes, want %d", len(conf), et.GetConfounderByteSize())
	}
	plain := make([]byte, 0, len(conf)+len(msg))
	plain = append(plain, conf...)
	plain = append(plain, msg...)
	ke, err := et.DeriveKey(key.KeyValue, common.GetUsageKe(usage))
	if err != nil {
		return nil, err
	}
	_, ct, err := et.EncryptData(ke, plain)
	if err != nil {
		return nil, err
	}
	ih, err := common.GetIntegrityHash(plain, key.KeyValue, usage, et)
	if err != nil {
		return nil, err
	}
	return append(ct, ih...), nil
}

// rawDecrypt inverts rawEncrypt, verifying the HMAC, and returns conf | msg.
func rawDecrypt(key types.EncryptionKey, usage uint32, ct []byte) ([]byte, error) {
	et, err := wrapEtype(key.KeyType)
	if err != nil {
		return nil, err
	}
	h := et.GetHMACBitLength() / 8
	if len(ct) < et.GetConfounderByteSize()+h {
		return nil, errors.New("winrm: ciphertext too short")
	}
	ke, err := et.DeriveKey(key.KeyValue, common.GetUsageKe(usage))
	if err != nil {
		return nil, err
	}
	plain, err := et.DecryptData(ke, ct[:len(ct)-h])
	if err != nil {
		return nil, err
	}
	want, err := common.GetIntegrityHash(plain, key.KeyValue, usage, et)
	if err != nil {
		return nil, err
	}
	if !hmac.Equal(want, ct[len(ct)-h:]) {
		return nil, errors.New("winrm: integrity check failed")
	}
	return plain, nil
}

// sealCFX builds a complete sealed Wrap token (header | rotated body). conf nil
// draws a fresh random confounder (production); a fixed one is for the KAT.
func sealCFX(key types.EncryptionKey, usage uint32, flags byte, seq uint64, conf, plaintext []byte) ([]byte, error) {
	et, err := wrapEtype(key.KeyType)
	if err != nil {
		return nil, err
	}
	if conf == nil {
		conf = make([]byte, et.GetConfounderByteSize())
		if _, err := rand.Read(conf); err != nil {
			return nil, fmt.Errorf("winrm: confounder: %w", err)
		}
	}
	rrc := tokenHdrLen + et.GetHMACBitLength()/8 // 28 for the AES SHA1-96 etypes
	toSeal := make([]byte, 0, len(plaintext)+tokenHdrLen)
	toSeal = append(toSeal, plaintext...)
	toSeal = append(toSeal, header16(flags, 0, 0, seq)...) // header' carries RRC 0
	body, err := rawEncrypt(key, usage, conf, toSeal)
	if err != nil {
		return nil, fmt.Errorf("winrm: seal: %w", err)
	}
	out := header16(flags, 0, uint16(rrc), seq)
	return append(out, rotateRight(body, rrc)...), nil
}

// unsealCFX opens a sealed Wrap token. fromAcceptor says which side must have
// sent it (and so which key usage and SentByAcceptor value are required);
// acceptorSubkey is what the context negotiated, and the token's flag must
// agree. It returns the plaintext and the sender's sequence number.
func unsealCFX(key types.EncryptionKey, fromAcceptor, acceptorSubkey bool, tok []byte) ([]byte, uint64, error) {
	if len(tok) < tokenHdrLen {
		return nil, 0, errors.New("winrm: wrap token shorter than its header")
	}
	if binary.BigEndian.Uint16(tok[0:2]) != wrapTokenID || tok[3] != 0xFF {
		return nil, 0, errors.New("winrm: not an RFC 4121 wrap token")
	}
	fl := tok[2]
	if fl&flagSealed == 0 {
		return nil, 0, errors.New("winrm: wrap token is not sealed; refusing a plaintext reply")
	}
	if (fl&flagSentByAcceptor != 0) != fromAcceptor {
		return nil, 0, errors.New("winrm: wrap token direction is wrong (a reflected token?)")
	}
	if (fl&flagAcceptorSubkey != 0) != acceptorSubkey {
		return nil, 0, errors.New("winrm: wrap token's AcceptorSubkey flag disagrees with the negotiated key")
	}
	ec := int(binary.BigEndian.Uint16(tok[4:6]))
	rrc := int(binary.BigEndian.Uint16(tok[6:8]))
	seq := binary.BigEndian.Uint64(tok[8:16])
	usage := uint32(keyusage.GSSAPI_INITIATOR_SEAL)
	if fromAcceptor {
		usage = keyusage.GSSAPI_ACCEPTOR_SEAL
	}
	et, err := wrapEtype(key.KeyType)
	if err != nil {
		return nil, 0, err
	}
	plain, err := rawDecrypt(key, usage, rotateLeft(tok[tokenHdrLen:], rrc))
	if err != nil {
		return nil, 0, fmt.Errorf("winrm: unseal: %w", err)
	}
	plain = plain[et.GetConfounderByteSize():]
	if len(plain) < tokenHdrLen+ec {
		return nil, 0, errors.New("winrm: unsealed payload too short")
	}
	// The encrypted header copy must equal the outer header, RRC excepted
	// (RFC 4121 §4.2.4; the copy is encrypted before rotation is decided).
	cp := plain[len(plain)-tokenHdrLen:]
	if !hmac.Equal(cp[0:6], tok[0:6]) || !hmac.Equal(cp[8:16], tok[8:16]) {
		return nil, 0, errors.New("winrm: wrap header does not match its encrypted copy")
	}
	return plain[:len(plain)-tokenHdrLen-ec], seq, nil
}

// winrmSplit is the MS-WSMV signature length for key: header + RRC + confounder.
func winrmSplit(key types.EncryptionKey) (int, error) {
	et, err := wrapEtype(key.KeyType)
	if err != nil {
		return 0, err
	}
	return tokenHdrLen + tokenHdrLen + et.GetHMACBitLength()/8 + et.GetConfounderByteSize(), nil
}

// wrapWinRM seals plaintext for the initiator (our client) and splits it for
// MS-WSMV: header is the "signature" the 4-byte LE length frames (60 bytes for
// AES), data is the encrypted payload (len(plaintext) bytes).
func wrapWinRM(key types.EncryptionKey, acceptorSubkey bool, seq uint64, plaintext []byte) ([]byte, []byte, error) {
	flags := byte(flagSealed)
	if acceptorSubkey {
		flags |= flagAcceptorSubkey
	}
	tok, err := sealCFX(key, keyusage.GSSAPI_INITIATOR_SEAL, flags, seq, nil, plaintext)
	if err != nil {
		return nil, nil, err
	}
	n, err := winrmSplit(key)
	if err != nil {
		return nil, nil, err
	}
	return tok[:n], tok[n:], nil
}

// unwrapWinRM opens the acceptor's sealed reply (SentByAcceptor set,
// GSSAPI_ACCEPTOR_SEAL) from its MS-WSMV signature and data parts.
func unwrapWinRM(key types.EncryptionKey, acceptorSubkey bool, header, data []byte) ([]byte, error) {
	plain, _, err := unwrapWinRMSeq(key, acceptorSubkey, header, data)
	return plain, err
}

// unwrapWinRMSeq is unwrapWinRM that also returns the acceptor's sequence
// number, for the connection's replyWindow.
func unwrapWinRMSeq(key types.EncryptionKey, acceptorSubkey bool, header, data []byte) ([]byte, uint64, error) {
	tok := make([]byte, 0, len(header)+len(data))
	tok = append(tok, header...)
	tok = append(tok, data...)
	return unsealCFX(key, true, acceptorSubkey, tok)
}

// replyWindow enforces the acceptor's sequence numbers within one GSS
// context (review Minor 4; RFC 4121 §4.2.2's SND_SEQ). The first sealed reply
// sets the base -- the acceptor chose its initial number, and its key is
// fresh for this context, so nothing from another session opens here -- and
// every later reply must carry exactly the next number: a reply replayed
// verbatim (valid ciphertext, an old number), one out of order, or one after
// a gap is refused rather than read into the conversation. A refusal does not
// move the window.
type replyWindow struct {
	next    uint64
	started bool
}

func (w *replyWindow) accept(seq uint64) error {
	if w.started && seq != w.next {
		return fmt.Errorf("winrm: sealed reply sequence number %d, expected %d (a replayed or reordered reply); refusing", seq, w.next)
	}
	w.started, w.next = true, seq+1
	return nil
}
