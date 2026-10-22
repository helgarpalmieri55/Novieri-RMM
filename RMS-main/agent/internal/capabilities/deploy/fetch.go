package deploy

import (
	"context"
	"fmt"
	"io"
	"strings"

	"github.com/toeshee/it-suite/agent/internal/fetch"
	"github.com/toeshee/it-suite/agent/internal/jobs"
)

// Fetcher retrieves an installer artifact by its `artifact_ref`.
//
// It returns a stream rather than bytes on purpose: install.go hashes as it
// writes, so a 2 GiB installer costs a 2 GiB file and a 32 KiB buffer rather
// than 2 GiB of the agent's memory budget. The stream is the ONLY thing this
// seam provides — the Fetcher does not decide whether the bytes may run, and
// there is no code path from a Fetcher's output to a process that does not go
// through `stage`'s hash check.
type Fetcher func(ctx context.Context, ref string) (io.ReadCloser, error)

// maxArtifactBytes bounds a download. Installers are large; a stream that
// never ends is unbounded disk consumption on an endpoint, which is a denial
// of service against the device rather than against the agent.
//
// 2 GiB is well above any real MSI or .pkg and well below "fills the disk".
const maxArtifactBytes = 2 << 30

// defaultFetcher is the production Fetcher, and it is where a REAL GAP IN THE
// WIRE CONTRACT is made loud instead of quiet.
//
// `artifact_ref` is produced by ObjectStore.put_package_artifact
// (backend/app/core/storage.py) and is an `s3://<bucket>/<key>` URI. There is
// NO agent-facing route that resolves one: the agent's HTTP surface is
// enrollment plus the WebSocket gateway, and app/api/v1 has no
// package-artifact endpoint at all (searched: the only `artifact` in
// app/api/v1 and app/gateway is `artifact_ref` on a job-log message). An agent
// cannot fetch an s3:// URI without object-store credentials, and it must
// never have them — invariant #3 puts stored credentials in the vault,
// decrypted at point of use, not distributed to every endpoint in the fleet.
//
// So this refuses, with the reason naming what is missing. REFUSAL and not
// failure, deliberately: retrying does not grow a route, and a device that
// cannot resolve an artifact_ref today cannot resolve it in five minutes. The
// alternative — failing quietly and forever — is how a contract gap becomes a
// permanent mystery in a dashboard.
//
// `https://` IS supported, so the fix on the server side is a presigned URL or
// an authenticated agent-facing download route, and this agent already handles
// the result.
func defaultFetcher(ctx context.Context, ref string) (io.ReadCloser, error) {
	switch {
	case strings.HasPrefix(ref, "https://"):
		return httpFetch(ctx, ref)
	case strings.HasPrefix(ref, "s3://"):
		return nil, jobs.Refuse("app.deploy: artifact_ref is an s3:// object-store URI, which " +
			"this agent cannot resolve. It holds no object-store credentials and must not " +
			"(invariant #3), and the control plane exposes no agent-facing route that serves a " +
			"package artifact. A presigned https:// URL in artifact_ref would work unchanged. " +
			"Nothing was downloaded and nothing was executed")
	}
	// Not `file://`, not `http://`, not a bare path. A plaintext transport for
	// a binary that is about to run as SYSTEM is not made safe by the hash
	// check — the hash proves the bytes are the ones the server named, and a
	// network attacker who cannot change the bytes can still read them and can
	// still stall the transfer. A LOCAL PATH is worse: it would turn
	// artifact_ref into "execute this file already on the box", which is a
	// different capability wearing a deployment's clothes.
	return nil, jobs.Refuse("app.deploy: artifact_ref is not an https:// URL. This agent " +
		"downloads artifacts over TLS and from nowhere else; nothing was executed")
}

// httpFetch is the one network call this package makes, through the shared
// internal/fetch. A transport failure is FAILED, not refused: the device is
// capable, the network was not.
func httpFetch(ctx context.Context, ref string) (io.ReadCloser, error) {
	body, err := fetch.HTTPS(ctx, ref)
	if err != nil {
		return nil, fmt.Errorf("app.deploy: %w", err)
	}
	return body, nil
}
