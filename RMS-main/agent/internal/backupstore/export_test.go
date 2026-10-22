// Compiled only under `go test`, so nothing it exposes exists in a
// production build or is reachable by any importer.
package backupstore

import "crypto/tls"

// SetTestTLSConfig points the upload client at a test CA and returns a
// function restoring the previous value.
func SetTestTLSConfig(c *tls.Config) func() {
	prev := testTLSConfig
	testTLSConfig = c
	return func() { testTLSConfig = prev }
}
