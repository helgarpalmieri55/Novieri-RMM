package remote

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"os/exec"
	"sync"
)

// ErrNoUserSession means nobody is logged on at the console, so there is no
// session to share (spec §3.4: attended never degrades to unattended).
var ErrNoUserSession = errors.New("remote: no interactive user session")

// Launcher starts the helper where the user can see it.
type Launcher interface {
	Launch(ctx context.Context) (Helper, error)
}

// Helper is a running helper process and the service's ends of its pipes.
type Helper interface {
	Stdin() io.WriteCloser
	Stdout() io.Reader
	// Exited is closed when the process has exited.
	Exited() <-chan struct{}
	Kill() error
	// Close releases the pipes and process handles; call after Exited.
	Close() error
	PID() int
}

// ExecLauncher runs a command with inherited anonymous pipes as its stdin and
// stdout. It is the portable launcher: tests and the backend's end-to-end
// double use it, and it is the shape the Windows launcher reproduces with a
// user token.
type ExecLauncher struct {
	Path string
	Args []string
	Env  []string
}

// Launch implements Launcher.
func (l ExecLauncher) Launch(ctx context.Context) (Helper, error) {
	inR, inW, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	outR, outW, err := os.Pipe()
	if err != nil {
		_ = inR.Close()
		_ = inW.Close()
		return nil, err
	}
	cmd := exec.Command(l.Path, l.Args...)
	cmd.Env = l.Env
	cmd.Stdin, cmd.Stdout = inR, outW
	h := &execHelper{cmd: cmd, in: inW, out: outR, exited: make(chan struct{})}
	cmd.Stderr = &h.stderr
	if err := cmd.Start(); err != nil {
		for _, f := range []*os.File{inR, inW, outR, outW} {
			_ = f.Close()
		}
		return nil, err
	}
	// The child holds its own copies; ours would keep the pipes open forever.
	_ = inR.Close()
	_ = outW.Close()
	go func() {
		_ = cmd.Wait()
		close(h.exited)
	}()
	return h, nil
}

type execHelper struct {
	cmd    *exec.Cmd
	in     *os.File
	out    *os.File
	exited chan struct{}
	stderr capped
	once   sync.Once
}

func (h *execHelper) Stdin() io.WriteCloser   { return h.in }
func (h *execHelper) Stdout() io.Reader       { return h.out }
func (h *execHelper) Exited() <-chan struct{} { return h.exited }
func (h *execHelper) PID() int                { return h.cmd.Process.Pid }
func (h *execHelper) Kill() error             { return h.cmd.Process.Kill() }

func (h *execHelper) Close() error {
	h.once.Do(func() {
		_ = h.in.Close()
		_ = h.out.Close()
	})
	return nil
}

// Stderr is what the helper wrote to stderr, capped.
func (h *execHelper) Stderr() string { return h.stderr.String() }

// capped keeps the first 16 KiB written to it.
type capped struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (c *capped) Write(p []byte) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if room := 16<<10 - c.buf.Len(); room > 0 {
		if len(p) > room {
			c.buf.Write(p[:room])
		} else {
			c.buf.Write(p)
		}
	}
	return len(p), nil
}

func (c *capped) String() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.buf.String()
}
