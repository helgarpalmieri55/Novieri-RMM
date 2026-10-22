//go:build !linux

package netsweep

// There is no neighbour-table reader on Windows or macOS in v1.
//
// §5.4's `method` field records what was ACTUALLY done, and the plan's rule
// holds: on a platform where the ARP half cannot be performed, `icmp+arp`
// degrades to ICMP-only and the missing half is reported in `errors[]` rather
// than failing the job. A sweep that found hosts is worth more than a sweep
// that refused because it could not also learn their MACs, and §4.1's
// reconciliation still works from ICMP evidence — it is MAC-derived identity
// that is weakened, and the document says so.
//
// Returning nil here is what produces that `errors[]` entry (see collect): the
// absence is REPORTED, never silently substituted with an empty table, because
// an empty table and "this platform has no table" are different facts and only
// one of them means "no host on this segment has a MAC".
func defaultNeighbours() Neighbours { return nil }
