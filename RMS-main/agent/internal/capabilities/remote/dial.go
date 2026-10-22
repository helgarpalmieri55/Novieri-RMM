package remote

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"time"

	"github.com/coder/websocket"

	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// errNoPin mirrors the agent channel's rule: no pinned CA, no dial.
var errNoPin = errors.New("no pinned CA bundle")

// dialRelay opens the second outbound mTLS WebSocket (same client
// certificate, same pinned CA as the agent channel), negotiates itms-rsp.1
// and completes the AUTH handshake with the one-shot relay token.
func dialRelay(ctx context.Context, p Params, o Options) (*websocket.Conn, error) {
	u, err := url.Parse(p.RelayURL)
	if err != nil || u.Scheme != "wss" {
		return nil, errors.New("relay_url is not a wss:// URL")
	}
	if o.RootCAs == nil {
		return nil, errNoPin
	}
	cert, err := o.ClientCert()
	if err != nil {
		return nil, fmt.Errorf("client certificate: %w", err)
	}
	client := &http.Client{Transport: &http.Transport{
		TLSClientConfig: &tls.Config{
			MinVersion:   tls.VersionTLS13,
			RootCAs:      o.RootCAs,
			Certificates: []tls.Certificate{cert},
		},
		ForceAttemptHTTP2: false,
		DisableKeepAlives: true,
	}}
	dctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	conn, resp, err := websocket.Dial(dctx, u.String(), &websocket.DialOptions{
		HTTPClient:   client,
		Subprotocols: []string{rsp.Subprotocol},
	})
	if resp != nil && resp.Body != nil {
		_ = resp.Body.Close()
	}
	if err != nil {
		return nil, fmt.Errorf("dial %s: %w", u.Redacted(), err)
	}
	if conn.Subprotocol() != rsp.Subprotocol {
		conn.CloseNow()
		return nil, fmt.Errorf("gateway negotiated subprotocol %q", conn.Subprotocol())
	}
	conn.SetReadLimit(rsp.MaxMessageBytes)

	auth, err := rsp.EncodeJSON(rsp.Auth, 1, rsp.AuthBody{SessionID: p.SessionID, Token: p.RelayToken})
	if err != nil {
		conn.CloseNow()
		return nil, err
	}
	actx, cancelAuth := context.WithTimeout(ctx, o.AuthTimeout)
	defer cancelAuth()
	if err := conn.Write(actx, websocket.MessageBinary, auth); err != nil {
		conn.CloseNow()
		return nil, fmt.Errorf("AUTH: %w", err)
	}
	typ, b, err := conn.Read(actx)
	if err != nil {
		conn.CloseNow()
		if code := websocket.CloseStatus(err); code != -1 {
			return nil, fmt.Errorf("gateway refused the relay token (close %d)", code)
		}
		return nil, fmt.Errorf("AUTH: %w", err)
	}
	f, err := rsp.Decode(b)
	if typ != websocket.MessageBinary || err != nil || f.Type != rsp.AuthOK {
		conn.CloseNow()
		return nil, errors.New("gateway did not answer AUTH_OK")
	}
	return conn, nil
}
