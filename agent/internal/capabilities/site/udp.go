package site

import (
	"context"
	"net"
	"net/netip"
)

// openUDPSender dials -- never binds -- a UDP socket with SO_BROADCAST set, and
// is the only production path to the wire in this package.
//
// # Why net.Dialer and not net.ListenPacket
//
// The obvious way to send a broadcast datagram in Go is `net.ListenPacket("udp4",
// ":0")` followed by WriteTo. It is also the way that puts a `net.ListenPacket`
// in the agent's source, which internal/transport/listen_test.go forbids and
// which would have needed an allowance entry beside netsweep's raw ICMP socket.
//
// An allowance would have been the WRONG ANSWER rather than an inconvenience.
// netsweep's is defensible because it explains a socket that genuinely receives
// (echo replies) and argues why receiving them is not a service. This socket
// receives nothing, so there is nothing to argue -- and a `ListenPacket` here
// would create an unconnected socket that CAN receive, from anyone, for the
// life of the job. `DialContext` produces a connected socket instead: the
// kernel drops any datagram whose source is not the address we connected to,
// no code in this package ever reads from it, and the file needs no exemption
// from the invariant-#2 walker. The dial is also what picks the source address
// and interface, using the kernel's route to the broadcast address rather than
// this process guessing an interface.
//
// # Why the Control hook
//
// Linux returns EACCES for a sendto() to a broadcast destination on a socket
// without SO_BROADCAST, and the connect() to one fails the same way. The option
// therefore has to be set on the raw descriptor BEFORE connect, which is
// exactly what net.Dialer.Control is for. See sockopt_unix.go / sockopt_windows.go.
func openUDPSender(ctx context.Context, dst netip.AddrPort) (Sender, error) {
	d := net.Dialer{Control: enableBroadcast}
	// "udp4" and not "udp": the destination is IPv4 by construction
	// (parseBroadcast refuses anything else), and naming the family keeps the
	// resolver from considering an IPv6 path that cannot carry this packet.
	conn, err := d.DialContext(ctx, "udp4", dst.String())
	if err != nil {
		return nil, err
	}
	return &udpSender{conn: conn}, nil
}

// udpSender writes and closes. It has no Read method, and that is not an
// omission: nothing in this package reads from this socket, so there is no code
// path from a received byte to an action.
type udpSender struct{ conn net.Conn }

func (u *udpSender) Send(payload []byte) error {
	_, err := u.conn.Write(payload)
	return err
}

func (u *udpSender) Close() error { return u.conn.Close() }
