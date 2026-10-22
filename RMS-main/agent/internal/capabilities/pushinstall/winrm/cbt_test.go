package winrm

import (
	"bytes"
	"encoding/hex"
	"encoding/pem"
	"testing"
)

// Fixed certificates and the Bnd an INDEPENDENT implementation computes for
// them: pyspnego 0.12.2's spnego.channel_bindings.GssChannelBindings, packed
// and MD5'd --
//
//	cb = GssChannelBindings(application_data=b"tls-server-end-point:" + H(der))
//	hashlib.md5(cb.pack()).hexdigest()
//
// with H = SHA-256 for the ecdsa-with-SHA256 certificate and SHA-384 for the
// ecdsa-with-SHA384 one (RFC 5929 §4.1: the certificate's own signature hash,
// SHA-256 when that is MD5 or SHA-1). pack() is 73 and 89 bytes: a 20-byte
// prefix of five little-endian uint32 (initiator addrtype, initiator length,
// acceptor addrtype, acceptor length, application-data length) then the data.
// These vectors are what the pyspnego acceptor in CI verifies our AP-REQ
// against on 5986, pinned here so a packing regression fails without it.
const (
	certP256SHA256 = `-----BEGIN CERTIFICATE-----
MIIBkjCCATmgAwIBAgIUOV/Jfoe4JXieY4o7ci6NBMJxwzYwCgYIKoZIzj0EAwIw
HzEdMBsGA1UEAwwUcGMxLmNvcnAuZXhhbXBsZS5jb20wHhcNMjYwOTI5MDI1ODAz
WhcNMzYwOTI2MDI1ODAzWjAfMR0wGwYDVQQDDBRwYzEuY29ycC5leGFtcGxlLmNv
bTBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABEAGFF0kDaiVIhSiqMuIrzJuNQeg
o9H7u+zM/HfvEOSh3f7AoTRRv7LLhcbDRy1FBH90t1sqKssIieL5HQDZNvCjUzBR
MB0GA1UdDgQWBBR0y+FJoQ4N3y2pUpzIGPxtpk7BxDAfBgNVHSMEGDAWgBR0y+FJ
oQ4N3y2pUpzIGPxtpk7BxDAPBgNVHRMBAf8EBTADAQH/MAoGCCqGSM49BAMCA0cA
MEQCIGdivitzsVCxZq8COU3fPE/0/N6tWn45wtS2tKixh97VAiA9qOmy9nDwDeet
Lepe9EVc8ffU4aEt77KdI45lAcezpg==
-----END CERTIFICATE-----`
	bndP256SHA256 = "6ecbb596a1f156d1b542ec7b7045bc30"

	certP384SHA384 = `-----BEGIN CERTIFICATE-----
MIIB0DCCAVagAwIBAgIUDC/xEGF6ZhnyekgV8YdxPESN4p4wCgYIKoZIzj0EAwMw
HzEdMBsGA1UEAwwUcGMxLmNvcnAuZXhhbXBsZS5jb20wHhcNMjYwOTI5MDI1ODAz
WhcNMzYwOTI2MDI1ODAzWjAfMR0wGwYDVQQDDBRwYzEuY29ycC5leGFtcGxlLmNv
bTB2MBAGByqGSM49AgEGBSuBBAAiA2IABAqE5ieT4kABH7PRsRaepmBWwL/ip3h9
Ck/vz2EJciRwsH9mJR5rLXY2Y1TXq7/yxy5Ekw21jIVv7UM7JUdWnLD8M1QCWkoM
uzO0q6ysNr+tdL3zEQeJysQGrHsirbcu96NTMFEwHQYDVR0OBBYEFAMQRJscbfhV
KyvPeQwH5WcU9oZaMB8GA1UdIwQYMBaAFAMQRJscbfhVKyvPeQwH5WcU9oZaMA8G
A1UdEwEB/wQFMAMBAf8wCgYIKoZIzj0EAwMDaAAwZQIxAPppFhXvKtcb608bNZiT
zFkcvu8vHzDgaJLpHwW664pDawmvB556tnf6YZMacmL8EAIwOroQRH2DDn0O7NWb
eJ0qGd7AWxFF57f1/XjLzg+7Z2CSuZ0CJhvAWCU3vDRafq3/
-----END CERTIFICATE-----`
	bndP384SHA384 = "50efdd8d013a50b1c32e50986dab73d6"
)

func der(t *testing.T, p string) []byte {
	t.Helper()
	b, _ := pem.Decode([]byte(p))
	if b == nil {
		t.Fatal("bad PEM")
	}
	return b.Bytes
}

func TestChannelBindingMatchesPyspnegoVectors(t *testing.T) {
	for _, c := range []struct{ name, cert, want string }{
		{"ecdsa-with-SHA256", certP256SHA256, bndP256SHA256},
		{"ecdsa-with-SHA384 (hash agility, RFC 5929 §4.1)", certP384SHA384, bndP384SHA384},
	} {
		got := channelBindingMD5(der(t, c.cert))
		if hex.EncodeToString(got) != c.want {
			t.Errorf("%s: Bnd = %x, want %s (pyspnego GssChannelBindings)", c.name, got, c.want)
		}
	}
}

func TestChannelBindingIs16BytesAndTracksTheCertificate(t *testing.T) {
	a := channelBindingMD5(der(t, certP256SHA256))
	b := channelBindingMD5(der(t, certP384SHA384))
	if len(a) != 16 || len(b) != 16 {
		t.Fatal("Bnd must be 16 bytes")
	}
	if bytes.Equal(a, b) {
		t.Fatal("two certificates produced one binding")
	}
	if bytes.Equal(a, make([]byte, 16)) {
		t.Fatal("a TLS binding must not be the no-binding zero value")
	}
	// Unparseable DER still binds (SHA-256 of the bytes), never to zeroes.
	if bytes.Equal(channelBindingMD5([]byte("not a certificate")), make([]byte, 16)) {
		t.Fatal("garbage bound to zeroes")
	}
}

func TestNoBindingIsSixteenZeroBytes(t *testing.T) {
	if !bytes.Equal(noChannelBinding(), make([]byte, 16)) {
		t.Fatal("5985 has no TLS: Bnd must be 16 zero bytes")
	}
}
