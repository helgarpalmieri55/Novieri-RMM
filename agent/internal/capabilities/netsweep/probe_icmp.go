package netsweep

import (
	"context"
	"errors"
	"math/rand/v2"
	"net"
	"net/netip"
	"sync"
	"time"
)

// The raw ICMP echo prober.
//
// # This is the one allowance in internal/transport/listen_test.go
//
// `net.ListenPacket("ip4:icmp", ...)` appears here and nowhere else in the
// agent. Invariant #2 says the agent has no listening port, and this is not
// one:
//
//   - it is opened inside a VERIFIED job (Service.run, after the scope check)
//     and closed in a defer when that job ends. There is no socket between
//     jobs;
//   - a raw ICMP socket has no port. Nothing can connect to it, and no protocol
//     an outsider can start runs on it;
//   - what it reads are echo REPLIES to echo requests this process sent moments
//     earlier, filtered by an identifier this process chose at random. An
//     unsolicited packet is discarded by that filter, and there is no code path
//     from a received byte to an action of any kind — the only effect a reply
//     can have is to add its SOURCE ADDRESS to a set, and that set is then
//     intersected with the signed scope before anything is reported.
//
// The allowance in listen_test.go names this file and this call. It was also
// narrowed as part of adding it: it used to be "skip this file", which would
// have let a future `net.Listen` in here unchallenged.
//
// # No x/net
//
// docs/modules/agent/plan.md's tech stack allows `golang.org/x/net` (`icmp`,
// `ipv4`, `ipv6`) for this task. It is not used, and the reason is a measured
// one: the current `golang.org/x/net` requires a go1.25 toolchain, and
// `go get golang.org/x/net/icmp` in this container rewrote go.mod's `go 1.24`
// to `go 1.25.0`. The agent's CI pins Go 1.24. An echo request is eight bytes
// of header and a one's-complement checksum; taking a dependency that moves the
// language version to avoid writing them is the wrong trade for a binary whose
// selling point is that it has almost none.
//
// IPv4 only, matching what can legitimately arrive: `create_sweep` refuses an
// IPv6 subnet before signing (§0.5), and `enumerate` records and skips one.

// icmpEmitter sends echo requests on a raw socket and records what answers.
type icmpEmitter struct {
	conn net.PacketConn
	id   uint16

	mu   sync.Mutex
	seen map[netip.Addr]bool

	seq  uint16
	done chan struct{}
	wg   sync.WaitGroup
}

// openICMPEmitter is the production Emitter.
func openICMPEmitter(_ context.Context) (Emitter, error) {
	conn, err := net.ListenPacket("ip4:icmp", "0.0.0.0")
	if err != nil {
		return nil, err
	}
	e := &icmpEmitter{
		conn: conn,
		// A random identifier per job. Two sweeps on one host, or another
		// process pinging, must not have their replies attributed to this job.
		id:   uint16(rand.Uint32()), //nolint:gosec // not a security value
		seen: map[netip.Addr]bool{},
		done: make(chan struct{}),
	}
	e.wg.Add(1)
	go e.read()
	return e, nil
}

// Send emits one echo request. It does not wait for a reply.
func (e *icmpEmitter) Send(addr netip.Addr) error {
	if !addr.Is4() {
		return errors.New("netsweep: only IPv4 echo requests are sent")
	}
	e.mu.Lock()
	e.seq++
	seq := e.seq
	e.mu.Unlock()

	msg := echoRequest(e.id, seq)
	_, err := e.conn.WriteTo(msg, &net.IPAddr{IP: addr.AsSlice()})
	return err
}

// Responded is a snapshot of the addresses that have answered.
func (e *icmpEmitter) Responded() map[netip.Addr]bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	out := make(map[netip.Addr]bool, len(e.seen))
	for k, v := range e.seen {
		out[k] = v
	}
	return out
}

// Close stops the reader and releases the socket.
//
// §5.5: "the socket is opened at job start and closed at job end". The
// goroutine count and the open file descriptors return to their pre-job values
// here, and netsweep_test.go asserts both by reading /proc/self/fd.
func (e *icmpEmitter) Close() error {
	select {
	case <-e.done:
	default:
		close(e.done)
	}
	err := e.conn.Close() // unblocks the reader
	e.wg.Wait()
	return err
}

// read parks on the socket until Close. It is ONE goroutine, it exists only for
// the life of the job, and it does nothing with a packet except record its
// source.
func (e *icmpEmitter) read() {
	defer e.wg.Done()
	buf := make([]byte, 1500)
	for {
		select {
		case <-e.done:
			return
		default:
		}
		// A deadline rather than a blocking read, so a Close that races the
		// read still terminates promptly on every platform.
		_ = e.conn.SetReadDeadline(time.Now().Add(200 * time.Millisecond))
		n, src, err := e.conn.ReadFrom(buf)
		if err != nil {
			var ne net.Error
			if errors.As(err, &ne) && ne.Timeout() {
				continue
			}
			return // the socket is closed
		}
		id, ok := echoReplyID(buf[:n])
		if !ok || id != e.id {
			// Someone else's ping, our own outgoing request looped back, or an
			// unsolicited packet. Discarded here; there is no other path.
			continue
		}
		ip, ok := src.(*net.IPAddr)
		if !ok {
			continue
		}
		addr, ok := netip.AddrFromSlice(ip.IP)
		if !ok {
			continue
		}
		addr = addr.Unmap()
		e.mu.Lock()
		e.seen[addr] = true
		e.mu.Unlock()
	}
}

// echoRequest builds an ICMPv4 echo request (RFC 792): type 8, code 0, the
// one's-complement checksum, an identifier and a sequence number, and an empty
// payload. Sixteen bytes.
func echoRequest(id, seq uint16) []byte {
	msg := make([]byte, 16)
	msg[0] = 8 // echo request
	msg[4], msg[5] = byte(id>>8), byte(id)
	msg[6], msg[7] = byte(seq>>8), byte(seq)
	ck := checksum(msg)
	msg[2], msg[3] = byte(ck>>8), byte(ck)
	return msg
}

// echoReplyID reads the identifier out of an ICMPv4 echo REPLY.
//
// Go's `ip4:icmp` PacketConn hands back the payload with the IP header already
// stripped, so byte 0 is the ICMP type. Verified by running it rather than read
// from a manual page: a probe to 127.0.0.1 delivers both the outgoing type-8
// request (loopback echoes it) and the type-0 reply, each 16 bytes starting at
// the ICMP header. That is why the type check below is not optional — without
// it a sweep on loopback would count its own requests as answers.
func echoReplyID(b []byte) (uint16, bool) {
	if len(b) < 8 || b[0] != 0 || b[1] != 0 {
		return 0, false
	}
	return uint16(b[4])<<8 | uint16(b[5]), true
}

// checksum is the internet checksum (RFC 1071).
func checksum(b []byte) uint16 {
	var sum uint32
	for i := 0; i+1 < len(b); i += 2 {
		sum += uint32(b[i])<<8 | uint32(b[i+1])
	}
	if len(b)%2 == 1 {
		sum += uint32(b[len(b)-1]) << 8
	}
	for sum>>16 != 0 {
		sum = (sum & 0xffff) + (sum >> 16)
	}
	return ^uint16(sum)
}
