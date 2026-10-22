// Command release-sign signs agent release binaries with the offline Ed25519
// release-signing key (spec-self-update.md, operator decision 2026-09-23).
//
// The key is read from the environment variable AGENT_RELEASE_SIGNING_KEY
// (a PKCS#8 PEM Ed25519 private key; the release workflow fills it from the
// GitHub secret of the same name). It is never written to disk or printed.
//
//	release-sign genkey > key.pem                     # a new key, offline, once
//	release-sign pubkey                               # base64 public key, for -ldflags
//	release-sign sign -version 1.4.0 dist/its-agent-linux-amd64 ...
//
// `sign` writes <file>.sig (base64) beside each binary. The platform is taken
// from the file name, its-agent-<os>-<arch>[.exe], as build.sh names them.
package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/toeshee/it-suite/agent/internal/update"
)

// KeyEnv names the environment variable, and the GitHub secret, holding the key.
const KeyEnv = "AGENT_RELEASE_SIGNING_KEY"

func main() {
	if err := run(os.Args[1:], os.Getenv(KeyEnv), os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "release-sign:", err)
		os.Exit(1)
	}
}

func run(args []string, keyPEM string, out io.Writer) error {
	if len(args) == 0 {
		return errors.New("usage: release-sign genkey | pubkey | sign -version V FILE [FILE]")
	}
	if args[0] == "genkey" {
		// For the operator, offline, once; and for throwaway CI keys.
		_, priv, err := ed25519.GenerateKey(rand.Reader)
		if err != nil {
			return err
		}
		der, err := x509.MarshalPKCS8PrivateKey(priv)
		if err != nil {
			return err
		}
		_, err = out.Write(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}))
		return err
	}
	priv, err := parseKey(keyPEM)
	if err != nil {
		return err
	}
	switch args[0] {
	case "pubkey":
		_, err := fmt.Fprintln(out, base64.StdEncoding.EncodeToString(priv.Public().(ed25519.PublicKey)))
		return err
	case "sign":
		fs := flag.NewFlagSet("sign", flag.ContinueOnError)
		version := fs.String("version", "", "the release version stamped into the binaries")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		v, err := update.ParseVersion(*version)
		if err != nil {
			return fmt.Errorf("-version %q is not strict semver", *version)
		}
		if fs.NArg() == 0 {
			return errors.New("no files to sign")
		}
		for _, path := range fs.Args() {
			if err := signFile(priv, v.String(), path, out); err != nil {
				return err
			}
		}
		return nil
	}
	return fmt.Errorf("unknown command %q", args[0])
}

func parseKey(keyPEM string) (ed25519.PrivateKey, error) {
	if strings.TrimSpace(keyPEM) == "" {
		return nil, fmt.Errorf("%s is empty", KeyEnv)
	}
	block, _ := pem.Decode([]byte(keyPEM))
	if block == nil {
		return nil, fmt.Errorf("%s is not PEM", KeyEnv)
	}
	k, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("%s is not a PKCS#8 private key", KeyEnv)
	}
	priv, ok := k.(ed25519.PrivateKey)
	if !ok {
		return nil, fmt.Errorf("%s is not an Ed25519 key", KeyEnv)
	}
	return priv, nil
}

func platformOf(path string) (goos, goarch string, err error) {
	name := strings.TrimSuffix(filepath.Base(path), ".exe")
	parts := strings.Split(name, "-")
	if len(parts) != 4 || parts[0] != "its" || parts[1] != "agent" {
		return "", "", fmt.Errorf("%s is not named its-agent-<os>-<arch>", path)
	}
	goos, goarch = parts[2], parts[3]
	switch goos {
	case "linux", "windows", "darwin":
	default:
		return "", "", fmt.Errorf("%s: unknown os %q", path, goos)
	}
	switch goarch {
	case "amd64", "arm64":
	default:
		return "", "", fmt.Errorf("%s: unknown arch %q", path, goarch)
	}
	return goos, goarch, nil
}

func signFile(priv ed25519.PrivateKey, version, path string, out io.Writer) error {
	goos, goarch, err := platformOf(path)
	if err != nil {
		return err
	}
	sha, err := update.FileSHA256(path)
	if err != nil {
		return err
	}
	sig := base64.StdEncoding.EncodeToString(ed25519.Sign(priv, update.ReleaseMessage(version, goos, goarch, sha)))
	if err := os.WriteFile(path+".sig", []byte(sig+"\n"), 0o644); err != nil {
		return err
	}
	_, err = fmt.Fprintf(out, "%s  %s  %s/%s  %s\n", sha, filepath.Base(path), goos, goarch, sig)
	return err
}
