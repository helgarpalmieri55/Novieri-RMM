// Command agent is the Toeshee endpoint agent: one static binary, one
// long-lived outbound mTLS WebSocket, one timer wheel, and a registry of
// capabilities that stay dormant until a signed job for them arrives.
//
// At idle this process holds four goroutines and no work: the websocket read
// parked on a blocking read, the websocket writer parked on a channel receive,
// the heartbeat parked in Clock.Sleep, and the timer wheel parked on one timer
// armed at its nearest deadline. Nothing polls. internal/budget asserts the
// wheel's half of that from an ordinary Test, so a regression fails CI --
// `go test -bench` alone exits 0 on one, which is measured in that package's
// comment rather than recalled.
//
// Invariant #2 (outbound only): this process never binds a port. There is no
// http.Server and no net.Listen anywhere in the non-test tree; Task 4's
// listen_test.go asserts that at both the source and the /proc/net level.
//
// Invariant #1 (no unsigned command path): nothing dispatches work here. The
// only path to a capability is through the signed-job runner
// (internal/jobs), whose output type cannot be constructed outside that
// package. This process holds no registry of its own and no fallback branch
// that runs a job the runner refused.
//
// The capability registry (internal/capabilities) is built once at package
// init and read-only thereafter. A job type it does not carry is REFUSED with
// `unknown_type` before anything runs -- a correct, signed-path-respecting
// answer, and the one IPAM's dispatcher reads as "this agent cannot do that".
package main

import (
	"context"
	"crypto/tls"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"time"

	"github.com/toeshee/it-suite/agent/internal/backupstore"
	"github.com/toeshee/it-suite/agent/internal/capabilities"
	"github.com/toeshee/it-suite/agent/internal/capabilities/backup"
	"github.com/toeshee/it-suite/agent/internal/capabilities/health"
	"github.com/toeshee/it-suite/agent/internal/capabilities/jobcancel"
	"github.com/toeshee/it-suite/agent/internal/capabilities/logs"
	"github.com/toeshee/it-suite/agent/internal/capabilities/provision"
	"github.com/toeshee/it-suite/agent/internal/capabilities/remote"
	"github.com/toeshee/it-suite/agent/internal/capabilities/remote/helper"
	"github.com/toeshee/it-suite/agent/internal/capabilities/selfupdate"
	"github.com/toeshee/it-suite/agent/internal/capabilities/uninstall"
	"github.com/toeshee/it-suite/agent/internal/chunkreply"
	"github.com/toeshee/it-suite/agent/internal/config"
	"github.com/toeshee/it-suite/agent/internal/enroll"
	"github.com/toeshee/it-suite/agent/internal/install"
	"github.com/toeshee/it-suite/agent/internal/jobs"
	"github.com/toeshee/it-suite/agent/internal/readreply"
	"github.com/toeshee/it-suite/agent/internal/scheduler"
	"github.com/toeshee/it-suite/agent/internal/service"
	"github.com/toeshee/it-suite/agent/internal/transport"
	"github.com/toeshee/it-suite/agent/internal/update"
	"github.com/toeshee/it-suite/agent/internal/wire"
)

// version is set at build time with
//
//	-ldflags "-X main.version=<v>"
//
// It stays "dev" in ordinary builds so an unstamped binary is obvious.
var version = "dev"

// advertised is what this build tells the control plane it can do, at
// enrollment.
//
// It is read FROM THE REGISTRY, never written by hand: advertising a job type
// this binary cannot run is worse than advertising none, and a hand-kept list
// is a list that drifts. `agents.capabilities` is advisory in any case --
// IPAM's dispatcher dispatches regardless and reads a `refused` job as the
// answer -- so the cost of being wrong here is a job that fails instead of one
// that is never sent.
func advertised() []string { return capabilities.Names() }

// connSender is the write capability OnConnected hands the reply registries:
// transport's *Conn narrowed to Send.
//
// It exists because chunkreply.Attach takes a chunkreply.Sender and
// readreply.Attach takes a readreply.Sender -- structurally identical,
// nominally distinct types (see either package's "why this is a duplicate"
// comment for why they stay that way) -- so the two registries cannot be
// driven through one shared interface directly. Go interface satisfaction is
// structural, so *transport.Conn, and a test fake with nothing but a Send
// method, already satisfy connSender AND both packages' Sender interfaces
// with no conversion.
type connSender interface {
	Send(ctx context.Context, msg any) error
}

// replyBinder pairs one reply registry's Attach and Detach so OnConnected and
// OnDisconnect can drive an arbitrary number of registries from one loop
// instead of one call site per registry per event.
type replyBinder struct {
	attach func(connSender)
	detach func(error)
}

// replyBinders returns the exact attach/detach pair `run`'s OnConnected and
// OnDisconnect hooks drive on every connect and disconnect: the chunk-grant
// registry backing backup.run's dedup exchange (docs/modules/backup/spec.md
// §5.2), and the read-grant registry backing backup.restore's fetch (§6.1).
//
// `run` and this package's tests call this SAME function -- not a parallel
// copy of its wiring -- so deleting any one of the four Attach/Detach calls
// it makes is a code change a test can catch, rather than a silent stall an
// operator would only see hours into a large restore's next reconnect.
func replyBinders(replies *chunkreply.Registry, readGrants *readreply.Registry) []replyBinder {
	chunkAttach := func(s connSender) { replies.Attach(s) }
	chunkDetach := func(cause error) { replies.Detach(cause) }
	readAttach := func(s connSender) { readGrants.Attach(s) }
	readDetach := func(cause error) { readGrants.Detach(cause) }
	return []replyBinder{
		{attach: chunkAttach, detach: chunkDetach},
		{attach: readAttach, detach: readDetach},
	}
}

// currentIdentity re-reads the persisted identity, falling back to the one this
// process started with.
//
// A certificate rotation replaces identity.json under a running agent, so a
// long-lived process reporting on `identity` would be reporting on a
// certificate it no longer presents -- which is exactly the kind of stale
// expiry warning that trains an operator to ignore the real one.
func currentIdentity(cfg config.Config, fallback *enroll.Identity) *enroll.Identity {
	if id, err := enroll.Load(cfg); err == nil {
		return id
	}
	return fallback
}

// installerRequested is the ONE question the service path asks about the
// Windows self-installer (install-link spec §1.3), and it is answered HERE,
// from two constants, without calling into internal/install -- so the service,
// which is `its-agent.exe` started with no arguments, never initialises or
// runs any of it (idle budget).
//
// Installer mode is: no arguments (a double-click), or exactly ElevatedArg
// (the UAC re-launch), AND a file name that starts with install.FilePrefix.
// Anything else on the command line -- the code included -- is not installer
// mode: the code is never an argument.
func installerRequested(args []string, exe string) bool {
	switch {
	case len(args) == 0:
	case len(args) == 1 && args[0] == install.ElevatedArg:
	default:
		return false
	}
	base := exe
	if i := strings.LastIndexAny(base, `/\`); i >= 0 {
		base = base[i+1:]
	}
	return strings.HasPrefix(base, install.FilePrefix)
}

// pushInstallRequested is the one question the service path asks about the
// Linux and macOS install mode the SSH push runs against the binary it placed
// (`its-agent install --token-file <0600 file> --enroll-url <https origin>`).
// Like installerRequested it compares a constant and calls nothing in
// internal/install: the service is started with no arguments.
func pushInstallRequested(args []string) bool {
	return len(args) > 0 && args[0] == install.PushArg
}

// msiInstallRequested and msiUninstallRequested are the questions the service
// path asks about the Group Policy MSI's two custom actions (GPO MSI spec
// 2026-09-30 §2.3): exactly `--msi-install`, or exactly `--msi-uninstall`.
// Constants compared, nothing in internal/install or the uninstall
// capability called: the service is started with no arguments.
func msiInstallRequested(args []string) bool {
	return len(args) == 1 && args[0] == install.MsiInstallArg
}

func msiUninstallRequested(args []string) bool {
	return len(args) == 1 && args[0] == uninstall.MsiUninstallArg
}

// main parses flags and hands the process to the service seam.
//
// The seam is not decoration. On Linux and macOS service.Run is
// signal.NotifyContext and nothing more; on Windows it is the Service Control
// Manager dispatcher, without which `sc.exe create` over this binary installs a
// service that fails to start with error 1053 every time. Both give `run` a
// context that is cancelled by whatever "stop" means on that platform.
func main() {
	// The screen-share helper is this binary, started by the service inside
	// the logged-on user's session with two inherited pipes. It never runs the
	// service seam, enrolls or connects to anything.
	if len(os.Args) > 1 && os.Args[1] == remote.HelperArg {
		os.Exit(helper.Main())
	}
	// The uninstall helper is this binary, started detached by `agent.uninstall`
	// (a transient unit, a launchd job, or a temp copy on Windows). It never
	// runs the service seam, enrolls or connects to anything.
	if len(os.Args) > 1 && os.Args[1] == uninstall.HelperArg {
		os.Exit(uninstall.HelperMain())
	}
	// The Group Policy MSI's two custom actions, run as SYSTEM by Windows
	// Installer against the installed binary. Neither runs the service seam.
	if msiUninstallRequested(os.Args[1:]) {
		os.Exit(uninstall.MsiMain())
	}
	if msiInstallRequested(os.Args[1:]) {
		exe, err := os.Executable()
		if err != nil {
			fmt.Fprintln(os.Stderr, "its-agent --msi-install: cannot locate this binary:", err)
			os.Exit(1)
		}
		os.Exit(install.RunMsi(exe))
	}
	// The Windows self-installer: the file the install link served, opened
	// from Downloads (or copied from a share). A name with the prefix but no
	// valid host and code starts normally -- it is not an installer.
	if exe, err := os.Executable(); err == nil && installerRequested(os.Args[1:], exe) {
		if link, ok := install.FromFileName(exe); ok {
			os.Exit(install.Run(os.Args[1:], exe, link))
		}
	}
	// The Linux and macOS install mode: the SSH push placed this binary and a
	// 0600 token file and runs one fixed command against it (invariant
	// 2a(b)). It installs the service the way packaging/*/install.sh does and
	// never runs the service seam itself.
	if pushInstallRequested(os.Args[1:]) {
		exe, err := os.Executable()
		if err != nil {
			fmt.Fprintln(os.Stderr, "its-agent install: cannot locate this binary:", err)
			os.Exit(1)
		}
		os.Exit(install.RunPush(os.Args[2:], exe))
	}

	showVersion := flag.Bool("version", false, "print the agent version and exit")
	selfTestOnly := flag.Bool("update-selftest", false,
		"load config, identity and registry, print a version report, and exit (self-update)")
	helperPhase := flag.String("update-helper", "", "run the self-update helper: apply or rollback")
	statePath := flag.String("state", "", "the self-update state file, with --update-helper")
	flag.Parse()

	switch {
	case *showVersion:
		fmt.Println(version)
		return
	case *selfTestOnly:
		if err := selfTest(os.Stdout); err != nil {
			fmt.Fprintln(os.Stderr, "update self-test failed:", err)
			os.Exit(1)
		}
		return
	case *helperPhase != "":
		os.Exit(runHelper(*helperPhase, *statePath))
	}

	os.Exit(service.Run(run))
}

// run is the agent proper. Its return value is the process exit code, and the
// codes are load-bearing: the systemd unit and the Windows service recovery
// action both restart on a non-zero exit, and 2/3/4/5/6 tell an operator which
// stage refused to start.
func run(ctx context.Context) int {
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))

	cfg, err := config.Load()
	if err != nil {
		log.Error("configuration", "err", err)
		return 2
	}

	log.Info("its-agent starting",
		"version", version,
		"data_dir", cfg.DataDir,
		"enrolled", cfg.Enrolled(),
	)

	// A self-update in flight (spec-self-update.md §5.3). With none pending
	// this is one os.Stat and returns nil; a nil session is a no-op below.
	upd := update.Resume(update.ResumeOptions{
		Version:  version,
		DataDir:  cfg.DataDir,
		Services: update.SystemServices{},
		Exit:     os.Exit,
		Logger:   log,
	})

	identity, err := enroll.LoadOrEnroll(ctx, cfg, enroll.Options{
		Logger:       log,
		Capabilities: advertised(),
	})
	if err != nil {
		log.Error("enrollment", "err", err)
		return 3
	}

	// Certificates are 365 days and are ROTATED AUTOMATICALLY, starting at
	// two-thirds of life (docs/modules/agent/cert-renewal.md). This check is
	// what makes a rotation that is not happening visible on the endpoint: it
	// is silent while the certificate is healthy, WARNs inside the last third,
	// and ERRORs past NotAfter. No timer of its own, no allocation.
	enroll.LogExpiry(log, time.Now(), identity)

	// The outbound side: everything the runner says goes through a bounded,
	// persisted buffer, so an answer produced while the uplink is down is still
	// owed and still delivered, in order, on the next connect.
	outbox := jobs.NewOutbox(jobs.OpenBufferForConfig(cfg, log), log)

	// A rollback or abort the previous process could not report is enqueued
	// now and delivered on the first connect.
	upd.Attach(ctx, outbox)

	// The reply half of the backup dedup exchange. At rest this is an empty map
	// and a mutex: no goroutine, no timer, no socket.
	replies := chunkreply.New(log)

	// The reply half of the backup READ exchange (docs/modules/backup/spec.md
	// §6.1). Same shape, same idle cost, twin lifecycle: attached and detached
	// on the same connection events as `replies` below, so a restore's fetch
	// fails immediately on a lost connection rather than burning its deadline.
	readGrants := readreply.New(log)

	nonces := jobs.OpenNonceStoreForConfig(cfg, log)

	runner, err := jobs.New(jobs.Options{
		DeviceID:   identity.DeviceID,
		SigningKey: identity.JobSigningKey,
		Nonces:     nonces,
		Registry:   capabilities.Default(),
		Sender:     outbox,
		Logger:     log,
		// Dispatcher is left unset deliberately: jobs.New builds the default
		// (queue depth 8, heavy budget 2, heavy keys backup/patch/app/script --
		// jobs.DefaultHeavyKeys) so this binary runs capabilities off Handle's
		// caller -- the transport's one read goroutine -- rather than blocking
		// it for a job's duration.
		//
		// THIS ctx is the seam that decides how long a running job lives. It is
		// the PROCESS context: service.Run cancels it when the service manager
		// says stop, and nothing else. Passing the transport's per-connection
		// context instead -- which is what Handle's caller holds, and what the
		// dispatcher used before this field existed -- would end every running
		// capability at the next reconnect. See jobs.DispatcherOptions.BaseContext.
		BaseContext: ctx,
	})
	if err != nil {
		// A runner that cannot verify must not exist. Refusing to start is the
		// correct failure: an agent that connects and cannot check a signature
		// is worse than an agent that is visibly down.
		log.Error("signed-job runner", "err", err)
		return 5
	}

	// The wheel: ONE goroutine parked on ONE timer, for every piece of
	// recurring work this process will ever have. Capabilities register entries
	// on it rather than starting tickers of their own -- that is the mechanism
	// docs/modules/endpoint-health-alerting/spec.md §0.5 assumes and the one
	// internal/budget asserts costs zero allocations per wakeup.
	wheel := scheduler.New(scheduler.Real{}, log)
	if err := wheel.Add("daily-housekeeping", 24*time.Hour, func(context.Context) {
		// The replay store is bounded, and freshness already refuses anything
		// past its expires_at -- so an expired nonce cannot be usefully
		// replayed and can go. Once a day, on a timer that already exists.
		if n := nonces.Prune(time.Now()); n > 0 {
			log.Info("pruned expired nonces", "count", n, "remaining", nonces.Len())
		}
		// Also checked at every connect; this covers a process that stays
		// connected for weeks and therefore never re-runs OnConnect. The
		// identity is re-read because a rotation may have replaced it under us.
		enroll.LogExpiry(log, time.Now(), currentIdentity(cfg, identity))
	}); err != nil {
		log.Error("scheduler", "err", err)
		return 6
	}
	// The health edge tick (docs/modules/endpoint-health-alerting/spec.md
	// §0.4). Binding wires the engine to the wheel and the outbox; it installs
	// NO policy and adds NO wheel entry. The cadence and the thresholds arrive
	// as a signed `health.policy.apply` job or they do not exist -- §0.9's
	// argument against a locally scheduled anything, applied here: a locally
	// configured tick would be a standing instruction nobody signed.
	health.Process.Bind(health.Options{
		Scheduler: wheel,
		Sender:    outbox,
		Tick:      health.NewHostSampler(),
		// A SEPARATE object from the fast-tick sampler, so §0.5 falsifier 3
		// ("posture probes running on the fast tick") has a call counter to
		// observe rather than a comment to trust.
		Posture: health.NewPostureSampler(),
		// BorrowEventLog is deliberately nil: the `eventlog.*` check types ride
		// the security-logs capability's EXISTING OS subscription (§0.5 item 5)
		// rather than health opening one of its own, so those checks report
		// `unavailable` (§0.7).
		//
		// UNTIL THIS BRANCH that was because the capability did not exist. It
		// does now -- `logs.ship` is registered below -- so the sentence has
		// changed from "impossible" to "not wired". Sharing the handle is a
		// real design question (`logs.ship` opens its query per job and closes
		// it, so there is no standing subscription to borrow yet), and wiring
		// it blind would be inventing a lifetime neither capability agreed to.
		// Health continues to report `unavailable`, honestly, until someone
		// designs the seam.
		Logger: log,
	})

	// `job.cancel` (2026-08-22-job-lifecycle-design.md §3.8). Binding gives the
	// capability the runner's interrupt handle and nothing else: no goroutine,
	// no timer, no allocation beyond one stored pointer. It must happen AFTER
	// `jobs.New` above, because the capability registry has to exist before the
	// runner and this capability needs the runner -- the same ordering
	// `backup.Process.Bind` and `health.Process.Bind` resolve the same way.
	//
	// Without this call the capability is registered but refuses every cancel
	// naming that it is unbound, which is a true answer the control plane can
	// act on. `unknown_type` -- the answer a build with no capability at all
	// gives -- would instead tell the operator this endpoint can never be
	// interrupted, and that would be false.
	jobcancel.Process.Bind(runner)

	// `agent.update`: the runner is its busy check. Everything else resolves at
	// job time.
	selfupdate.Process.Bind(selfupdate.Options{Version: version, Runner: runner, Logger: log})

	// `agent.uninstall`: binding stores the launcher, this binary's path and
	// the data directory. Nothing starts until a verified job arrives.
	uninstall.Process.Bind(uninstall.Options{
		Launch:  uninstall.LaunchHelper,
		Binary:  os.Executable,
		DataDir: cfg.DataDir,
		Logger:  log,
	})

	// `logs.ship` (docs/modules/security-logs/spec.md §10). Binding gives the
	// capability the outbox and nothing else: it opens NO handle, takes NO
	// wheel entry and starts NO goroutine here, and resolves its OS source and
	// its cursor file at JOB TIME. Without this call the capability is
	// registered but answers `logs.ErrUnbound` and reads nothing -- the same
	// shape health uses above, for the same reason: a binary whose advertised
	// capabilities depend on start-up ordering is worse than one that answers
	// honestly that it is not configured.
	logs.Process.Bind(logs.Options{Sender: outbox, Logger: log})

	// `provision.enroll` compares the params' `expected_agent_version` against
	// THIS BUILD's version. Passing it explicitly rather than letting the
	// package default to "dev": the default exists so an unstamped developer
	// build behaves, not so a released binary silently claims to be one.
	provision.Process.Bind(provision.Options{Version: version, Logger: log})

	// `backup.run` (docs/modules/backup/spec.md §5.2). Binding gives the
	// capability its storage transports and, now, a snapshot provider: the
	// dedup exchange, an object store that spends the write grant that
	// exchange returns, and `backup.PlatformSnapshot` -- whichever
	// build-tagged provider (`snapshot_linux.go`/`_darwin.go`/`_windows.go`/
	// `_other.go`) this GOOS selects. All three resolve at JOB TIME and none
	// is touched while the agent is idle.
	//
	// This does NOT make `backup.run` complete, and has not been observed to
	// on any platform. On Linux, a root on an LVM logical volume gets a real
	// snapshot (logic unit-tested here; the one real-LVM proof
	// (`snapshot_lvm_integration_test.go`) skips in every environment this
	// project has); a root that is not on LVM -- plain ext4-on-a-partition,
	// common on cloud images -- still refuses. Windows (VSS) and macOS (APFS)
	// providers exist, are compile-checked for their `GOOS`, and have never
	// executed; VSS in particular ships nine documented unconfirmed items,
	// one of which produces no error at all on failure. See
	// `docs/modules/backup/agent-engine.md` §4 and §11.
	//
	// Leaving `Options.OpenSnapshot` nil (the zero value) would still make
	// `defaultOpenSnapshot` refuse every `vss`/`lvm`/`apfs` request, by
	// design: a nil field must keep refusing, so any build that forgets this
	// line refuses rather than silently snapshotting. See registry.go and
	// HANDOFF.md §6.
	//
	// `backup.restore` (spec §6.1). OpenStore is now bound to a real grant
	// source -- backupstore.Opener(readGrants) -- so Get/GetRef can fetch
	// through the presigned-GET window instead of refusing outright.
	backup.Process.Bind(backup.Options{
		QueryChunks:  replies.Query,
		OpenStore:    backupstore.Opener(readGrants),
		OpenSnapshot: backup.PlatformSnapshot,
		Logger:       log,
	})

	// `remote.screen.open` (docs/modules/remote-control/spec-screen-share.md
	// §2.3): the relay is a second outbound mTLS WebSocket with the same
	// certificate (re-read per dial, like the agent channel) and the same
	// pinned CA. Binding stores these and starts nothing; registration is
	// Windows-only, and elsewhere DefaultLauncher is nil and the capability
	// refuses.
	remote.Process.Bind(remote.Options{
		ClientCert: func() (tls.Certificate, error) {
			id, err := enroll.Load(cfg)
			if err != nil {
				return tls.Certificate{}, err
			}
			return id.Cert, nil
		},
		RootCAs:  identity.CACerts,
		Launcher: remote.DefaultLauncher(),
		Logger:   log,
	})

	// The same two bindings OnConnected/OnDisconnect drive below, built by the
	// function cmd/agent's test also calls -- see replyBinders.
	binders := replyBinders(replies, readGrants)

	go func() { _ = wheel.Run(ctx) }()

	// One inbound entry point, three destinations. A chunk grant or a read
	// grant answers a question a running job asked and carries no instruction,
	// so each goes to its own registry; everything else is a job and goes to
	// the runner, which verifies its signature. Decoding here rather than in
	// the runner keeps `runner.Handle`'s contract "this is a job" intact.
	inbound := func(c context.Context, frame []byte) {
		if msg, err := wire.Decode(frame); err == nil {
			if g, ok := msg.(*wire.ChunkGrant); ok {
				replies.Deliver(g)
				return
			}
			if g, ok := msg.(*wire.ReadGrant); ok {
				readGrants.Deliver(g)
				return
			}
		}
		// Anything else -- including a frame that failed to decode -- goes to
		// the runner, which owns the logging and the refusal for it. Decoding
		// twice is deliberate: this function must not become the place that
		// decides what a job is.
		runner.Handle(c, frame)
	}

	if e2eNoDial {
		log.Warn("built with e2e_noconfirm: not dialling the gateway")
		<-ctx.Done()
		return 0
	}

	err = transport.Run(ctx, transport.Options{
		GatewayURL: identity.GatewayURL,
		Header:     transport.IdentityHeader(version, advertised()),
		// Re-read per dial rather than captured once: `agent.cert.install`
		// swaps the identity on disk while a connection is up, and the live
		// connection is deliberately not re-keyed. This is what makes the new
		// certificate take effect on the next reconnect with no restart.
		ClientCertFn: func() (tls.Certificate, error) {
			id, err := enroll.Load(cfg)
			if err != nil {
				return tls.Certificate{}, err
			}
			return id.Cert, nil
		},
		ClientCert:     identity.Cert,
		RootCAs:        identity.CACerts,
		HeartbeatEvery: transport.DefaultHeartbeat,
		Logger:         log,
		OnConnect: func(context.Context) {
			enroll.LogExpiry(log, time.Now(), currentIdentity(cfg, identity))
		},
		// The one recoverable 4401: a rotation this endpoint applied and the
		// control plane never recorded, because the acknowledgement was lost.
		// Reverting consumes the previous certificate, so this can happen at
		// most once per rotation and a genuinely expired fleet still stops
		// dialling. See docs/modules/agent/cert-renewal.md §4.4.
		OnUnauthorized: func(context.Context) bool {
			reverted, rerr := enroll.RevertToPrevious(cfg)
			if rerr != nil {
				log.Error("could not revert to the previous certificate", "err", rerr)
				return false
			}
			if reverted {
				log.Warn("the gateway refused the newly installed certificate; " +
					"reverted to the previous one and will retry the rotation")
			}
			return reverted
		},
		OnConnected: func(c context.Context, conn *transport.Conn) error {
			// Drain what the last outage accumulated BEFORE the read loop
			// starts, so a job's answer never overtakes an older one.
			if err := outbox.Attach(c, conn); err != nil {
				return err
			}
			for _, b := range binders {
				b.attach(conn)
			}
			// A trial confirms once this connection has held for 60 s.
			upd.Connected()
			return nil
		},
		// A dedup grant or a read grant comes back on the connection its query
		// went out on, so a pending flush or fetch must fail here rather than
		// burn its full deadline waiting for a reply that cannot arrive.
		OnDisconnect: func(_ context.Context, cause error) {
			upd.Disconnected()
			for _, b := range binders {
				b.detach(cause)
			}
		},
	}, inbound)
	if err != nil {
		// A terminal verdict: 4401/4403, or a gateway URL that is not wss://.
		// Exiting rather than looping is the point -- the service manager
		// restarts the process, and the operator sees a device that is off
		// rather than a device hammering the gateway forever.
		log.Error("gateway", "err", err)
		return 4
	}
	log.Info("its-agent stopped")
	return 0
}
