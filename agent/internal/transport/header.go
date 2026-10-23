package transport

import (
	"net/http"
	"sort"
	"strings"
)

// Optional, additive upgrade-request headers (spec-self-update.md §3.3). They
// are self-reported and used for display and eligibility, never for
// authorization. An older gateway ignores them.
const (
	HeaderAgentVersion      = "X-ITS-Agent-Version"
	HeaderAgentCapabilities = "X-ITS-Agent-Capabilities"
	// MaxCapabilitiesHeader is the gateway's bound; a longer list is cut at a
	// name boundary rather than sent and ignored whole.
	MaxCapabilitiesHeader = 4096
)

// IdentityHeader builds the two headers: the version, and the job types this
// binary can run, sorted and comma-separated.
func IdentityHeader(version string, capabilities []string) http.Header {
	h := http.Header{}
	if version != "" {
		h.Set(HeaderAgentVersion, version)
	}
	names := append([]string(nil), capabilities...)
	sort.Strings(names)
	var b strings.Builder
	for _, n := range names {
		if b.Len()+len(n)+1 > MaxCapabilitiesHeader {
			break
		}
		if b.Len() > 0 {
			b.WriteByte(',')
		}
		b.WriteString(n)
	}
	if b.Len() > 0 {
		h.Set(HeaderAgentCapabilities, b.String())
	}
	return h
}
