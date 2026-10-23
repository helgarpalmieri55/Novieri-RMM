// Package ipc is the framing between the agent service and its screen-share
// helper, spoken over the helper's inherited stdin/stdout pipes
// (spec-screen-share.md §2.3, §2.4).
//
// Both ends are the same binary, so this is not a wire contract; it is still
// bounded and validated, because the helper runs as the logged-on user and
// the service must not trust what arrives on the pipe more than it trusts the
// relay.
//
// A message is: length (u32, big-endian, counts kind + body) | kind (u8) | body.
package ipc

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sync"

	"github.com/toeshee/it-suite/agent/internal/rsp"
)

// Kind is a message kind.
type Kind byte

const (
	// Config is the first message the service sends: the session's settings.
	Config Kind = 1
	// Frame carries one rsp/1 frame, in either direction.
	Frame Kind = 2
	// Stop asks the helper to end the session and exit.
	Stop Kind = 3
)

// MaxBody is the largest body: one rsp/1 message.
const MaxBody = rsp.MaxMessageBytes

// ErrMessage is returned for a message that breaks the framing.
var ErrMessage = errors.New("ipc: malformed message")

// SessionConfig is the Config body. It carries what the helper shows and
// enforces; nothing in it is a secret (the relay token never leaves the
// service).
type SessionConfig struct {
	SessionID   string `json:"session_id"`
	Attended    bool   `json:"attended"`
	Technician  string `json:"technician"`
	Org         string `json:"org"`
	Reason      string `json:"reason"`
	TimeoutS    int    `json:"timeout_s"`
	AllowInput  bool   `json:"allow_input"`
	Record      bool   `json:"record"`
	MaxDuration int    `json:"max_duration_s"`
}

// StopBody is the Stop body.
type StopBody struct {
	Reason string `json:"reason"`
}

// Writer frames messages onto w. Safe for concurrent use.
type Writer struct {
	mu  sync.Mutex
	w   io.Writer
	hdr [5]byte
}

// NewWriter wraps w.
func NewWriter(w io.Writer) *Writer { return &Writer{w: w} }

// Write sends one message.
func (w *Writer) Write(k Kind, body []byte) error {
	if len(body) > MaxBody {
		return fmt.Errorf("%w: %d-byte body", ErrMessage, len(body))
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	binary.BigEndian.PutUint32(w.hdr[:4], uint32(1+len(body)))
	w.hdr[4] = byte(k)
	if _, err := w.w.Write(w.hdr[:]); err != nil {
		return err
	}
	if len(body) == 0 {
		return nil
	}
	_, err := w.w.Write(body)
	return err
}

// WriteJSON sends a message whose body is v as JSON.
func (w *Writer) WriteJSON(k Kind, v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return w.Write(k, b)
}

// Reader reads framed messages. Not safe for concurrent use.
type Reader struct {
	r   io.Reader
	hdr [5]byte
}

// NewReader wraps r.
func NewReader(r io.Reader) *Reader { return &Reader{r: r} }

// Read returns the next message. The body is freshly allocated. io.EOF means
// the other end closed cleanly between messages.
func (r *Reader) Read() (Kind, []byte, error) {
	if _, err := io.ReadFull(r.r, r.hdr[:4]); err != nil {
		if errors.Is(err, io.ErrUnexpectedEOF) {
			return 0, nil, fmt.Errorf("%w: truncated header", ErrMessage)
		}
		return 0, nil, err
	}
	n := binary.BigEndian.Uint32(r.hdr[:4])
	if n < 1 || n > MaxBody+1 {
		return 0, nil, fmt.Errorf("%w: length %d", ErrMessage, n)
	}
	if _, err := io.ReadFull(r.r, r.hdr[4:5]); err != nil {
		return 0, nil, fmt.Errorf("%w: truncated kind", ErrMessage)
	}
	body := make([]byte, n-1)
	if _, err := io.ReadFull(r.r, body); err != nil {
		return 0, nil, fmt.Errorf("%w: truncated body", ErrMessage)
	}
	return Kind(r.hdr[4]), body, nil
}
