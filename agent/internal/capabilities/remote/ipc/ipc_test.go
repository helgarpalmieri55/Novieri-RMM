package ipc

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"sync"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/rsp"
)

func TestMessagesRoundTrip(t *testing.T) {
	var buf bytes.Buffer
	w := NewWriter(&buf)
	cfg := SessionConfig{SessionID: "s", Attended: true, Technician: "Ann", TimeoutS: 30, AllowInput: true}
	if err := w.WriteJSON(Config, cfg); err != nil {
		t.Fatal(err)
	}
	frame := rsp.EncodeAck(1, 5)
	if err := w.Write(Frame, frame); err != nil {
		t.Fatal(err)
	}
	if err := w.Write(Stop, nil); err != nil {
		t.Fatal(err)
	}
	r := NewReader(&buf)
	k, body, err := r.Read()
	if err != nil || k != Config || !bytes.Contains(body, []byte(`"technician":"Ann"`)) {
		t.Fatalf("config = %d %s, %v", k, body, err)
	}
	k, body, err = r.Read()
	if err != nil || k != Frame || !bytes.Equal(body, frame) {
		t.Fatalf("frame = %d %x, %v", k, body, err)
	}
	k, body, err = r.Read()
	if err != nil || k != Stop || len(body) != 0 {
		t.Fatalf("stop = %d %x, %v", k, body, err)
	}
	if _, _, err := r.Read(); err != io.EOF {
		t.Fatalf("after the last message: %v, want io.EOF", err)
	}
}

func TestOversizedAndTruncatedMessagesAreRefused(t *testing.T) {
	if err := NewWriter(io.Discard).Write(Frame, make([]byte, MaxBody+1)); !errors.Is(err, ErrMessage) {
		t.Errorf("oversized write: %v", err)
	}
	huge := make([]byte, 5)
	binary.BigEndian.PutUint32(huge, MaxBody+2)
	zero := make([]byte, 5)
	for name, raw := range map[string][]byte{
		"length over the cap": huge,
		"zero length":         zero,
		"truncated header":    {0, 0},
		"truncated body":      {0, 0, 0, 9, byte(Frame), 1, 2},
	} {
		if _, _, err := NewReader(bytes.NewReader(raw)).Read(); !errors.Is(err, ErrMessage) {
			t.Errorf("%s: err = %v", name, err)
		}
	}
}

func TestConcurrentWritersNeverInterleave(t *testing.T) {
	var buf bytes.Buffer
	w := NewWriter(&buf)
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			for j := 0; j < 50; j++ {
				_ = w.Write(Frame, bytes.Repeat([]byte{byte(i)}, 100+i))
			}
		}(i)
	}
	wg.Wait()
	r := NewReader(&buf)
	for n := 0; n < 400; n++ {
		_, body, err := r.Read()
		if err != nil {
			t.Fatal(err)
		}
		i := int(body[0])
		if len(body) != 100+i || !bytes.Equal(body, bytes.Repeat([]byte{byte(i)}, 100+i)) {
			t.Fatalf("message %d interleaved", n)
		}
	}
}
