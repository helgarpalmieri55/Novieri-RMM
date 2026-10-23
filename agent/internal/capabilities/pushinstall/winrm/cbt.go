package winrm

import (
	"crypto"
	"crypto/md5" // #nosec G501 -- RFC 4121 §4.1.1.2 fixes MD5 for the Bnd; not used for integrity of anything else.
	"crypto/sha256"
	"crypto/x509"
	"encoding/binary"

	// Register the hashes RFC 5929 may select.
	_ "crypto/sha512"
)

// tlsServerEndPointPrefix is RFC 5929 §4's channel-binding type label.
const tlsServerEndPointPrefix = "tls-server-end-point:"

// channelBindingMD5 returns the 16-byte Bnd for the AP-REQ authenticator
// checksum (RFC 4121 §4.1.1), carrying the RFC 5929 tls-server-end-point
// binding that WinRM with CbtHardeningLevel=Strict requires (design §I2/W8).
//
// The gss_channel_bindings_struct hashed is (RFC 4121 §4.1.1.2, all
// little-endian uint32):
//
//	initiator_addrtype (0) | initiator_address length (0)
//	acceptor_addrtype  (0) | acceptor_address length  (0)
//	application_data length | application_data
//
// -- a 20-byte prefix. application_data is "tls-server-end-point:" + H(cert),
// H being the certificate's signature hash, or SHA-256 when that is MD5 or
// SHA-1 (RFC 5929 §4.1). certDER is the leaf of the TLS connection the AP-REQ
// travels on. cbt_test.go pins this against pyspnego's own packing.
func channelBindingMD5(certDER []byte) []byte {
	appData := append([]byte(tlsServerEndPointPrefix), certHash(certDER)...)
	buf := make([]byte, 20+len(appData))
	binary.LittleEndian.PutUint32(buf[16:20], uint32(len(appData)))
	copy(buf[20:], appData)
	sum := md5.Sum(buf) // #nosec G401 -- mandated by RFC 4121 §4.1.1.2.
	return sum[:]
}

// noChannelBinding is the Bnd on 5985: no TLS, so sixteen zero bytes.
func noChannelBinding() []byte { return make([]byte, 16) }

// certHash is RFC 5929 §4.1's hash of the server certificate.
func certHash(certDER []byte) []byte {
	h := crypto.SHA256
	if c, err := x509.ParseCertificate(certDER); err == nil {
		switch c.SignatureAlgorithm {
		case x509.SHA384WithRSA, x509.ECDSAWithSHA384, x509.SHA384WithRSAPSS:
			h = crypto.SHA384
		case x509.SHA512WithRSA, x509.ECDSAWithSHA512, x509.SHA512WithRSAPSS:
			h = crypto.SHA512
		}
	}
	if h == crypto.SHA256 {
		s := sha256.Sum256(certDER)
		return s[:]
	}
	w := h.New()
	_, _ = w.Write(certDER)
	return w.Sum(nil)
}
