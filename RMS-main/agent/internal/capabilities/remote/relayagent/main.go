// Command relayagent is a test double of an agent that can share its screen,
// used by backend/tests/test_remote_screen_relay.py. It is never packaged.
//
// Unlike internal/rsp/fakeagent it runs the REAL network side: it connects to
// the gateway's agent channel over mTLS, verifies the signed
// remote.screen.open job with the real runner, and hands it to the real
// capability, which dials /relay, authenticates and launches a helper. Only
// the desktop is fake: the helper is this binary re-run with -helper, playing
// the real session logic against the synthetic desktop (consent granted,
// pointer echoed as the cursor, clicks drawn on the screen).
//
// Each observation is printed to stdout as one JSON line.
package main

import (
	"context"
	"crypto/ed25519"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"os"
	"sync"
	"time"

	"github.com/toeshee/it-suite/agent/internal/capabilities"
	"github.com/toeshee/it-suite/agent/internal/capabilities/remote"
	"github.com/toeshee/it-suite/agent/internal/capabilities/remote/helper"
	"github.com/toeshee/it-suite/agent/internal/capabilities/remote/helper/synthetic"
	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/transport"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

func main() {
	var o struct {
		gateway, cert, key, ca, device, signingKey string
		helper                                     bool
		timeout                                    time.Duration
	}
	flag.StringVar(&o.gateway, "gateway", "", "wss URL of the gateway's agent channel")
	flag.StringVar(&o.cert, "cert", "", "client certificate (PEM)")
	flag.StringVar(&o.key, "key", "", "client key (PEM)")
	flag.StringVar(&o.ca, "ca", "", "CA bundle that signed the gateway certificate (PEM)")
	flag.StringVar(&o.device, "device", "", "this agent's device id")
	flag.StringVar(&o.signingKey, "signing-key", "", "the job-signing public key (PEM file)")
	flag.BoolVar(&o.helper, "helper", false, "run as the helper (the capability launches this)")
	flag.DurationVar(&o.timeout, "timeout", 60*time.Second, "overall deadline")
	flag.Parse()

	if o.helper {
		desk := synthetic.New(synthetic.Options{Answer: helper.Granted, AnswerAfter: 100 * time.Millisecond})
		if err := helper.Run(context.Background(), os.Stdin, os.Stdout, desk, helper.Options{}); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), o.timeout)
	defer cancel()
	cert, err := tls.LoadX509KeyPair(o.cert, o.key)
	if err != nil {
		fail(err)
	}
	caPEM, err := os.ReadFile(o.ca)
	if err != nil {
		fail(err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		fail(errors.New("no CA certificates in bundle"))
	}
	keyPEM, err := os.ReadFile(o.signingKey)
	if err != nil {
		fail(err)
	}
	signing, err := parseKey(keyPEM)
	if err != nil {
		fail(err)
	}

	conn, err := transport.Dial(ctx, transport.Options{GatewayURL: o.gateway, ClientCert: cert, RootCAs: pool})
	if err != nil {
		fail(err)
	}
	defer conn.Close()
	emit(map[string]any{"event": "connected"})

	self, _ := os.Executable()
	svc := remote.New(remote.Options{
		ClientCert: func() (tls.Certificate, error) { return cert, nil },
		RootCAs:    pool,
		Launcher:   remote.ExecLauncher{Path: self, Args: []string{"-helper"}},
	})
	reg := capabilities.New()
	if err := reg.Register(remote.JobType, svc.Open); err != nil {
		fail(err)
	}
	done := make(chan struct{})
	sender := &watch{conn: conn, done: done}
	runner, err := jobs.New(jobs.Options{
		DeviceID: o.device, SigningKey: signing, Nonces: &nonces{seen: map[string]bool{}},
		Registry: reg, Sender: sender, BaseContext: ctx,
	})
	if err != nil {
		fail(err)
	}
	go func() {
		for {
			frame, err := conn.Recv(ctx)
			if err != nil {
				return
			}
			runner.Handle(ctx, frame)
		}
	}()
	select {
	case <-done:
		time.Sleep(200 * time.Millisecond) // let the writer flush the result
	case <-ctx.Done():
		fail(errors.New("no job result before the deadline"))
	}
}

// watch forwards the runner's messages and reports the job's result.
type watch struct {
	conn *transport.Conn
	done chan struct{}
	once sync.Once
}

func (w *watch) Send(ctx context.Context, msg any) error {
	err := w.conn.Send(ctx, msg)
	if r, ok := msg.(wire.JobResult); ok {
		w.report(r)
	} else if r, ok := msg.(*wire.JobResult); ok {
		w.report(*r)
	}
	return err
}

// report ends the run on the screen-share job's result only. The gateway also
// sends other jobs on connect (a first inventory collect), which this
// double's registry refuses; those results must not end the session.
func (w *watch) report(r wire.JobResult) {
	if _, ok := r.Result["outcome"]; !ok {
		return
	}
	emit(map[string]any{"event": "result", "state": r.State, "result": r.Result})
	w.once.Do(func() { close(w.done) })
}

type nonces struct {
	mu   sync.Mutex
	seen map[string]bool
}

func (n *nonces) SeenOrRecord(nonce string, _ time.Time) bool {
	n.mu.Lock()
	defer n.mu.Unlock()
	if n.seen[nonce] {
		return true
	}
	n.seen[nonce] = true
	return false
}

func parseKey(b []byte) (ed25519.PublicKey, error) {
	block, _ := pem.Decode(b)
	if block == nil {
		return nil, errors.New("signing key is not PEM")
	}
	k, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return nil, err
	}
	pk, ok := k.(ed25519.PublicKey)
	if !ok {
		return nil, errors.New("signing key is not Ed25519")
	}
	return pk, nil
}

func emit(v map[string]any) {
	b, _ := json.Marshal(v)
	fmt.Println(string(b))
}

func fail(err error) {
	emit(map[string]any{"event": "error", "error": err.Error()})
	os.Exit(1)
}
