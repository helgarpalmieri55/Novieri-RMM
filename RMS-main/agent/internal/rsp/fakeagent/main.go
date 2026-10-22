// Command fakeagent is a test double of the agent's relay leg, used by the
// server's end-to-end test (backend/tests/test_remote_screen_go_agent.py).
//
// It dials the gateway's /relay path over mutual TLS with a real enrolled
// certificate, authenticates with the relay token from a signed
// remote.screen.open job, and plays a scripted session in rsp/1. It captures
// nothing and injects nothing: pointer input is echoed back as a cursor
// position. It is never packaged; packaging builds cmd/agent only.
//
// Each observation is printed to stdout as one JSON line.
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"time"

	"github.com/coder/websocket"

	"github.com/toeshee/it-suite/agent/internal/rsp"
)

type options struct {
	url, cert, key, ca string
	session, token     string
	script             string
	timeout            time.Duration
}

func main() {
	var o options
	flag.StringVar(&o.url, "url", "", "wss URL of the gateway's /relay path")
	flag.StringVar(&o.cert, "cert", "", "client certificate (PEM)")
	flag.StringVar(&o.key, "key", "", "client key (PEM)")
	flag.StringVar(&o.ca, "ca", "", "CA bundle that signed the gateway certificate (PEM)")
	flag.StringVar(&o.session, "session", "", "session id from the signed params")
	flag.StringVar(&o.token, "token", "", "relay token from the signed params")
	flag.StringVar(&o.script, "script", "grant", "grant | deny")
	flag.DurationVar(&o.timeout, "timeout", 30*time.Second, "overall deadline")
	flag.Parse()

	ctx, cancel := context.WithTimeout(context.Background(), o.timeout)
	defer cancel()
	if err := run(ctx, o); err != nil {
		emit(map[string]any{"event": "error", "error": err.Error()})
		os.Exit(1)
	}
}

func emit(v map[string]any) {
	b, _ := json.Marshal(v)
	fmt.Println(string(b))
}

func dial(ctx context.Context, o options) (*websocket.Conn, error) {
	cert, err := tls.LoadX509KeyPair(o.cert, o.key)
	if err != nil {
		return nil, err
	}
	caPEM, err := os.ReadFile(o.ca)
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		return nil, errors.New("no CA certificates in bundle")
	}
	client := &http.Client{Transport: &http.Transport{TLSClientConfig: &tls.Config{
		MinVersion:   tls.VersionTLS13,
		RootCAs:      pool,
		Certificates: []tls.Certificate{cert},
	}}}
	c, _, err := websocket.Dial(ctx, o.url, &websocket.DialOptions{
		HTTPClient:   client,
		Subprotocols: []string{rsp.Subprotocol},
	})
	if err != nil {
		return nil, err
	}
	if c.Subprotocol() != rsp.Subprotocol {
		c.Close(websocket.StatusProtocolError, "no subprotocol")
		return nil, fmt.Errorf("gateway negotiated %q", c.Subprotocol())
	}
	c.SetReadLimit(rsp.MaxMessageBytes)
	return c, nil
}

type session struct {
	c   *websocket.Conn
	seq uint32
}

func (s *session) send(ctx context.Context, frame []byte) error {
	return s.c.Write(ctx, websocket.MessageBinary, frame)
}

func (s *session) next() uint32 { s.seq++; return s.seq }

func (s *session) sendJSON(ctx context.Context, t rsp.Type, v any) error {
	b, err := rsp.EncodeJSON(t, s.next(), v)
	if err != nil {
		return err
	}
	return s.send(ctx, b)
}

func (s *session) read(ctx context.Context) (rsp.Frame, error) {
	typ, b, err := s.c.Read(ctx)
	if err != nil {
		return rsp.Frame{}, err
	}
	if typ != websocket.MessageBinary {
		return rsp.Frame{}, errors.New("text message on the relay")
	}
	return rsp.Decode(b)
}

func run(ctx context.Context, o options) error {
	c, err := dial(ctx, o)
	if err != nil {
		return err
	}
	defer c.CloseNow()
	s := &session{c: c}

	if err := s.sendJSON(ctx, rsp.Auth, rsp.AuthBody{SessionID: o.session, Token: o.token}); err != nil {
		return err
	}
	f, err := s.read(ctx)
	if err != nil {
		emit(map[string]any{"event": "refused", "code": int(websocket.CloseStatus(err))})
		return nil
	}
	if f.Type != rsp.AuthOK {
		return fmt.Errorf("expected AUTH_OK, got %#x", f.Type)
	}
	emit(map[string]any{"event": "auth_ok"})

	if err := s.sendJSON(ctx, rsp.State, rsp.StateBody{State: "consent_pending", TimeoutS: 30}); err != nil {
		return err
	}
	if o.script == "deny" {
		if err := s.sendJSON(ctx, rsp.State, rsp.StateBody{State: "consent_denied"}); err != nil {
			return err
		}
		_, err := s.read(ctx)
		emit(map[string]any{"event": "closed", "code": int(websocket.CloseStatus(err))})
		return nil
	}
	if err := s.sendJSON(ctx, rsp.State, rsp.StateBody{State: "consent_granted"}); err != nil {
		return err
	}
	hello := map[string]any{
		"version":      1,
		"capabilities": []string{"tile", "cursor"},
		"monitors": []map[string]any{{
			"id": 0, "x": 0, "y": 0, "width": 1920, "height": 1080, "dpi": 96, "primary": true,
		}},
		"codecs": []string{"fill"},
	}
	if err := s.sendJSON(ctx, rsp.Hello, hello); err != nil {
		return err
	}
	if err := s.send(ctx, rsp.Encode(rsp.FrameBegin, s.next(), nil)); err != nil {
		return err
	}
	tile := rsp.EncodeTile(s.next(), rsp.Rect{X: 0, Y: 0, W: 64, H: 64}, rsp.CodecFill, []byte{0x20, 0x40, 0x80})
	if err := s.send(ctx, tile); err != nil {
		return err
	}
	frameEnd := s.next()
	if err := s.send(ctx, rsp.Encode(rsp.FrameEnd, frameEnd, nil)); err != nil {
		return err
	}
	emit(map[string]any{"event": "frame_sent", "frame_end_seq": frameEnd})

	for {
		f, err := s.read(ctx)
		if err != nil {
			return fmt.Errorf("relay closed before the session ended: %w", err)
		}
		switch f.Type {
		case rsp.Ack:
			n, err := rsp.DecodeAck(f)
			if err != nil {
				return err
			}
			emit(map[string]any{"event": "ack", "frame_seq": n})
		case rsp.Pointer:
			p, err := rsp.DecodePointer(f)
			if err != nil {
				return err
			}
			emit(map[string]any{"event": "pointer", "x": p.X, "y": p.Y, "buttons": p.Buttons})
			if err := s.send(ctx, rsp.EncodeCursorPos(s.next(), p.X, p.Y, true)); err != nil {
				return err
			}
		case rsp.Text:
			text, err := rsp.DecodeText(f)
			if err != nil {
				return err
			}
			emit(map[string]any{"event": "text", "text": text})
			if text == "bye" {
				if err := s.sendJSON(ctx, rsp.State, rsp.StateBody{State: "ended", Reason: "ended_by_user"}); err != nil {
					return err
				}
				c.Close(websocket.StatusNormalClosure, "ended")
				emit(map[string]any{"event": "ended"})
				return nil
			}
		default:
			emit(map[string]any{"event": "frame", "type": int(f.Type)})
		}
	}
}
