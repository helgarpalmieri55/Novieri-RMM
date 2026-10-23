package install

// The shipped service definitions, copied verbatim so the install mode can
// write them without the service linking `embed` (imports_test.go).
// pushunix_test.go (TestTheServiceDefinitionsAreTheShippedFiles) holds each
// byte-for-byte equal to its file; edit the file under agent/packaging and
// copy it here, never the other way round. A backtick in the source is spelled
// "` + "`" + `" because a Go raw string cannot hold one.

const (
	// linuxUnit is agent/packaging/linux/its-agent.service.
	linuxUnit = `[Unit]
# The Toeshee endpoint agent.
#
# Invariant #2, stated here because a unit file is where someone would try to
# break it: this service opens NO listening port. There is no matching .socket
# unit, no socket activation, and nothing for ` + "`" + `After=network.target` + "`" + ` to wait to
# accept on. It dials outbound and only outbound.
Description=ITMS Agent
Documentation=file:///usr/share/doc/its-agent/README.md
# network-online rather than network: the first thing the agent does on a
# never-enrolled host is an HTTPS POST to the control plane. Failing that resolve
# is survivable (Restart=always retries) but noisy on every boot.
Wants=network-online.target
After=network-online.target

# The crash-loop limiter. These two live in [Unit], NOT [Service] -- systemd
# moved them in v230, and a systemd 255 (Ubuntu 24.04) parses
# ` + "`" + `StartLimitIntervalSec` + "`" + ` in [Service] as an unknown key, ignores it, and says
# so only if someone runs ` + "`" + `systemd-analyze verify` + "`" + `. Which is how this was found.
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
# NOT Type=notify. That needs sd_notify -- a fourth dependency, or a hand-rolled
# unix socket protocol -- for a readiness signal that nothing in this platform
# waits on. Type=simple with Restart=always is the honest shape.
ExecStart=/usr/local/bin/its-agent
# No arguments, ever. Everything on this line is world-readable through
# /proc/<pid>/cmdline for the life of the process, which is why the enrollment
# token arrives as a 0600 file in the state directory instead
# (config.EnrollTokenPath, deleted by the agent once it has bought a
# certificate).

Restart=always
# The agent has its own reconnect backoff for a gateway that is down; this is
# for a process that DIED. 5 s, with the burst limiter below so a crash loop
# from a bad build cannot become a hot loop.
RestartSec=5s
# systemd sends SIGTERM; internal/service turns it into a cancelled context and
# the agent flushes its outbound buffer. 30 s, then SIGKILL.
TimeoutStopSec=30s

# --- Configuration -------------------------------------------------------
# Two variables, neither of them a secret. The token is deliberately NOT here:
# ` + "`" + `systemctl show its-agent` + "`" + ` prints Environment= to any user who can read the
# unit, and a spent token would sit in /etc forever.
Environment=ITS_AGENT_DATA_DIR=/var/lib/its-agent
# Written 0600 by install.sh; carries ITS_AGENT_ENROLL_URL. The leading ` + "`" + `-` + "`" + `
# means an absent file is not a startup failure -- an enrolled agent needs
# neither the URL nor the token.
EnvironmentFile=-/etc/its-agent/agent.env

# --- Privilege -----------------------------------------------------------
# ROOT, and the reason is written down rather than assumed.
# docs/05-agent-spec.md "Process & service model" fixes this: the agent runs as
# root because the management capabilities it exists to carry need it (patch
# installation, service control, LVM snapshots for backup, provisioning).
# TODAY's compiled capability set is narrower than that -- see
# agent/packaging/README.md "Why root", which states exactly which of them
# actually need it and which would run unprivileged. When a capability lands
# that needs more than the bounding set below, this file changes with it.
User=root
Group=root

# NoNewPrivileges=yes, contrary to docs/modules/agent/plan.md Task 14 Step 1,
# which suggested ` + "`" + `false` + "`" + ` to protect the raw socket. That is a misreading: NNP
# blocks privilege GAIN through setuid/file capabilities on execve. It does not
# touch capabilities the manager granted at exec, so CAP_NET_RAW survives it.
# What it does buy is that a compromised agent cannot escalate through a setuid
# binary -- on a process that is root over the whole fleet, worth having.
NoNewPrivileges=yes

# The capability set, narrowed from root's full set to the ONE capability the
# compiled capabilities actually use:
#
#   CAP_NET_RAW   net.sweep's ICMP echo socket
#                 (internal/capabilities/netsweep/probe_icmp.go). The only
#                 privileged syscall in the build.
#
# Everything else the agent touches is reachable as uid 0 without a capability:
# /sys/class/dmi/id/* is 0400 ROOT-OWNED (owner read, no CAP_DAC_READ_SEARCH),
# /proc/<pid>/comm is 0444 (no CAP_SYS_PTRACE), and the state directory is
# root's own.
#
# MEASURED, not reasoned: the inventory, health and netsweep test binaries all
# pass under ` + "`" + `setpriv --bounding-set=-all,+net_raw` + "`" + ` (CapBnd 0x2000 -- exactly
# this line), a raw ICMP socket opens there, and it fails EPERM the moment
# cap_net_raw leaves the bounding set. The BOUNDING set is the control that
# matters for a root service: at execve a uid-0 process takes its permitted set
# from the bounding set, so narrowing Ambient alone would narrow nothing.
#
# This set is deliberately too small for the capabilities that are NOT built
# yet. patch, backup, remote-control and provisioning will each need more, and
# each should widen this line with its own justification rather than the line
# starting wide enough to hide them.
CapabilityBoundingSet=CAP_NET_RAW
AmbientCapabilities=CAP_NET_RAW

# --- Filesystem ----------------------------------------------------------
# StateDirectory creates and chowns /var/lib/its-agent at start, and 0700 is the
# same mode internal/config enforces from the other side: the device private key
# lives in there.
StateDirectory=its-agent
StateDirectoryMode=0700

# The whole hierarchy read-only except /dev, /proc, /sys...
ProtectSystem=strict
# ...and the one directory the agent legitimately writes: identity, key, pins,
# replay store, outbound buffer. Nothing else.
ReadWritePaths=/var/lib/its-agent
ProtectHome=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
RestrictNamespaces=yes
# AF_INET/AF_INET6 for the gateway and for ICMP; AF_UNIX for the resolver and
# journald; AF_NETLINK for the interface enumeration inventory does.
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK

# NOT SET, on purpose, each for a stated reason:
#   ProtectProc / ProcSubset  -- health's process checks read /proc/<pid>/comm
#                                across users; hidepid would blind them.
#   PrivateDevices            -- backup (not yet built) needs block devices;
#                                left off so the unit does not have to change
#                                under a capability that is coming.
#   MemoryDenyWriteExecute    -- the Go runtime is fine with it, but self-update
#                                (OQ-3) is not designed yet and this is the kind
#                                of flag that turns into a mystery at 2 a.m.
#   SystemCallFilter          -- a seccomp allowlist for a binary that shells
#                                out to per-OS inventory helpers is a
#                                maintenance burden with no owner. Named here
#                                so its absence is a decision, not an oversight.

# Journal, tagged. The agent logs to stderr and never logs a credential
# (asserted in internal/enroll's tests).
StandardOutput=journal
StandardError=journal
SyslogIdentifier=its-agent

[Install]
WantedBy=multi-user.target
`

	// darwinPlistTemplate is agent/packaging/darwin/com.toeshee.itsagent.plist.
	// darwinPlist adds ITS_AGENT_ENROLL_URL to its EnvironmentVariables, as
	// darwin/install.sh does with PlistBuddy.
	darwinPlistTemplate = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!--
  The Toeshee endpoint agent as a launchd SYSTEM daemon
  (/Library/LaunchDaemons, loaded into the ` + "`" + `system` + "`" + ` domain, running as root).

  Invariant #2: there is no Sockets key and no socket activation. launchd never
  binds anything on this agent's behalf, because the agent has no listening
  port. It dials outbound and only outbound.

  The enrollment token is NOT here. EnvironmentVariables in a plist under
  /Library/LaunchDaemons is world-readable and is echoed by ` + "`" + `launchctl print
  system/com.toeshee.itsagent` + "`" + `. The token goes to /var/lib/its-agent/enroll-token
  at mode 0600 (config.EnrollTokenPath), and the agent deletes it once it has
  exchanged it for a certificate.

  ProgramArguments carries the binary and nothing else, for the same reason:
  argv is visible in ` + "`" + `ps` + "`" + ` to every user on the machine.
-->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.toeshee.itsagent</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/libexec/its-agent</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <!--
      KeepAlive unconditionally: this is the supervision half of the agent's
      restart story, the counterpart of Restart=always in the systemd unit. The
      agent exits non-zero on a terminal verdict (a 4401/4403 from the gateway,
      an unusable identity), and an operator seeing a device that is off is the
      intended outcome of those.

      ThrottleInterval BOUNDS THE RATE OF A RESTART LOOP; IT DOES NOT END ONE.
      launchd will respawn an agent that can never start, once every
      ThrottleInterval seconds, indefinitely. The thing that actually prevents
      the loop is upstream: install.sh does not bootstrap this job at all when
      the Mac has neither an identity nor an enrollment token, because that is
      the state in which the agent exits 2 on every start. Same rule as
      linux/debian/postinst and windows/install.ps1.
    -->
    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <!--
      launchd sends SIGTERM on ` + "`" + `launchctl bootout` + "`" + ` and at shutdown;
      internal/service turns it into a cancelled context so the outbound buffer
      is flushed. 30 s, matching TimeoutStopSec in the systemd unit, then SIGKILL.
    -->
    <key>ExitTimeOut</key>
    <integer>30</integer>

    <key>EnvironmentVariables</key>
    <dict>
        <key>ITS_AGENT_DATA_DIR</key>
        <string>/var/lib/its-agent</string>
        <!--
          ITS_AGENT_ENROLL_URL is written here by install.sh. It is not a
          secret. The token is not, and must never be.
        -->
    </dict>

    <!--
      The agent logs to stderr. /var/log/its-agent is created 0750 root:wheel by
      install.sh; no credential is ever logged (asserted in internal/enroll's
      tests). Rotation is newsyslog's job (see packaging/README.md).
    -->
    <key>StandardErrorPath</key>
    <string>/var/log/its-agent/agent.log</string>
    <key>StandardOutPath</key>
    <string>/var/log/its-agent/agent.log</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
`
)
