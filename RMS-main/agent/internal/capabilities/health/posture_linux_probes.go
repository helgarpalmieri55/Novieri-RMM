package health

// This file has NO build tag. It shells out only through the injected
// postureRunner, so every decision it makes -- device resolution, the crypt
// test, the firewall ladder, SMART aggregation, and every `unavailable` branch
// -- is unit-tested on any machine, including this project's CI, which has no
// cryptsetup, no ufw, no firewalld and no smartctl. Only the dispatch that
// SELECTS these probes (posture_linux.go) needs `//go:build linux`.
//
// The name matters: Go applies an IMPLICIT build constraint to any file whose
// base name ends in a GOOS, so a `_linux.go` suffix here would silently
// exclude this logic from every other platform's build and test run --
// exactly the untagged-everywhere property this file depends on. `_probes`
// is not a GOOS, so no implicit constraint applies.

import (
	"context"
	"strings"
)

// probeDiskEncryptionLinux answers for the SYSTEM/BOOT volume only (design
// §2.3): the device root is mounted from, and whether a dm-crypt layer appears
// anywhere in that device's ancestry.
//
// `lsblk -s` walks PARENTS, which is what makes this correct for root on
// LVM-on-LUKS as well as for a plain LUKS partition.
//
// A machine with an encrypted root and an unencrypted /data reads as healthy.
// That is a stated limit, not an oversight: the check takes no selector, and
// per-volume coverage needs its own check type, which §0.1 makes a schema
// change.
func probeDiskEncryptionLinux(ctx context.Context, run postureRunner, _ string) Sample {
	res := run(ctx, "findmnt", "-n", "-o", "SOURCE", "--target", "/")
	if res.NotFound || !res.ok() {
		return unavailable()
	}
	dev := strings.TrimSpace(res.Stdout)
	if i := strings.IndexByte(dev, '\n'); i >= 0 {
		dev = dev[:i]
	}
	// findmnt renders a bind mount's source as `/dev/vda2[/subpath]`. lsblk
	// does not accept that form, so the suffix is stripped: the QUESTION is
	// about the device, and the subpath does not change which device it is.
	if i := strings.IndexByte(dev, '['); i >= 0 {
		dev = dev[:i]
	}
	if dev == "" {
		return unavailable()
	}

	anc := run(ctx, "lsblk", "-nso", "NAME,TYPE", dev)
	if anc.NotFound || !anc.ok() {
		return unavailable()
	}
	parsed := false
	for _, line := range strings.Split(anc.Stdout, "\n") {
		fields := strings.Fields(line)
		if len(fields) < 2 {
			continue
		}
		parsed = true
		if fields[len(fields)-1] == "crypt" {
			return boolSample(true)
		}
	}
	if !parsed {
		// lsblk ran and said nothing this code understands. Reporting `false`
		// here would be inferring "not encrypted" from "cannot tell", which is
		// the one direction §5 forbids in both directions.
		return unavailable()
	}
	return boolSample(false)
}

// probeFirewallLinux asks the front-end that OWNS the policy, and only falls
// back to the kernel ruleset when no front-end is installed (design §2.2).
//
// The order matters and the stopping rule matters more: a front-end that is
// installed ANSWERS, including when its answer is "inactive". Falling through
// to the raw ruleset after ufw said "inactive" would let a leftover iptables
// chain report a disabled firewall as enabled.
//
// `firewall-cmd --state` exits NON-ZERO (252) to say "not running". That is an
// answer, which is why every step here tests NotFound rather than ok().
func probeFirewallLinux(ctx context.Context, run postureRunner, _ string) Sample {
	if res := run(ctx, "ufw", "status"); !res.NotFound {
		out := strings.ToLower(res.Stdout)
		switch {
		case strings.Contains(out, "status: active"):
			return boolSample(true)
		case strings.Contains(out, "status: inactive"):
			return boolSample(false)
		}
		// Installed but unreadable -- most often "you need to be root".
		return unavailable()
	}
	if res := run(ctx, "firewall-cmd", "--state"); !res.NotFound {
		// `firewall-cmd --state` prints exactly "running" or "not running", but
		// either stream can carry EXTRA text around that -- a PolicyKit/D-Bus
		// warning, a deprecation notice -- so an exact-equality match on the
		// glued streams silently downgrades a real answer to unavailable. A
		// substring test survives the extra text; a literal "\n" joins the
		// streams so a stdout without its own trailing newline can't run into
		// the stderr text and accidentally form a different substring.
		//
		// "not running" CONTAINS "running", so it MUST be tested first: testing
		// "running" first would report a stopped firewalld as running, which is
		// the dangerous direction.
		combined := res.Stdout + "\n" + res.Stderr
		switch {
		case strings.Contains(combined, "not running"):
			return boolSample(false)
		case strings.Contains(combined, "running"):
			return boolSample(true)
		}
		return unavailable()
	}
	if res := run(ctx, "nft", "list", "ruleset"); !res.NotFound {
		if !res.ok() {
			return unavailable()
		}
		drops, complete := nftInputDrops(res.Stdout)
		if !complete {
			// A quoted comment was never closed, so stripNftStrings discarded
			// everything after it -- possibly a genuine "policy drop" further
			// down the ruleset. `drops` was computed from a truncated read and
			// is not a positive parsed statement; report unavailable rather
			// than mint a verdict nothing actually observed.
			return unavailable()
		}
		return boolSample(drops)
	}
	if res := run(ctx, "iptables", "-S", "INPUT"); !res.NotFound {
		if !res.ok() {
			return unavailable()
		}
		for _, line := range strings.Split(res.Stdout, "\n") {
			// REJECT is not a case here: `iptables -P` only accepts ACCEPT or
			// DROP as a chain's default policy -- the kernel itself refuses
			// REJECT for a policy target -- so `-P INPUT REJECT` can never
			// appear in real `iptables -S INPUT` output. It is omitted rather
			// than kept as dead code.
			if strings.TrimSpace(line) == "-P INPUT DROP" {
				return boolSample(true)
			}
		}
		// An ACCEPT policy with rules in it is NOT "enabled": those rules may
		// all be permissive, and "iptables is installed" is not a firewall.
		return boolSample(false)
	}
	return unavailable()
}

// probeSMARTLinux reads one device's SMART self-assessment, or aggregates over
// every fixed disk when the selector is `*`.
//
// The aggregation rule is stated because the obvious one is wrong:
//
//   - `false` if ANY readable device reports failing;
//   - `true` only if AT LEAST ONE device was read and none reported failing;
//   - `unavailable` if NO device could be read.
//
// "true if no failures were seen" would report a healthy fleet on a machine
// where smartctl cannot read a single disk -- absence of evidence becoming a
// definite answer, which §5 forbids.
//
// smartctl's exit code is a BITFIELD (bit 3 = failing), so the health verdict
// is read from the output text rather than from the exit status.
func probeSMARTLinux(ctx context.Context, run postureRunner, selector string) Sample {
	// selectorIsWildcard, not `selector != "*"`: it TRIMS, and probeSMARTWindows
	// already used it. Raw comparison made `" * "` a device path on Linux and an
	// aggregate on Windows -- the same signed policy meaning two things
	// depending on the endpoint's OS. Harmless in outcome (a device path that
	// does not exist is `unavailable`), but a check type means one thing.
	if s := strings.TrimSpace(selector); s != "" && !selectorIsWildcard(selector) {
		return smartOneLinux(ctx, run, s)
	}
	devs := fixedDisksLinux(ctx, run)
	if len(devs) == 0 {
		return unavailable()
	}
	read := 0
	for _, d := range devs {
		s := smartOneLinux(ctx, run, d)
		if !s.Available {
			continue
		}
		read++
		if !s.Bool {
			return boolSample(false)
		}
	}
	if read == 0 {
		return unavailable()
	}
	return boolSample(true)
}

func smartOneLinux(ctx context.Context, run postureRunner, dev string) Sample {
	res := run(ctx, "smartctl", "-H", dev)
	if res.NotFound {
		return unavailable()
	}
	out := res.Stdout + res.Stderr
	switch {
	case strings.Contains(out, "FAILED!"), strings.Contains(out, "FAILING_NOW"):
		return boolSample(false)
	case strings.Contains(out, "PASSED"), strings.Contains(out, "SMART Health Status: OK"):
		return boolSample(true)
	}
	return unavailable()
}

// fixedDisksLinux lists whole disks: `lsblk` TYPE `disk` only, which excludes
// partitions, LVM and crypt mappings, and then loop, ram and zram devices by
// name. (zram was in the code and not in this sentence until 2026-08-13. It is
// listed because a compressed-RAM block device has no SMART data and asking
// smartctl about one spends a 5s deadline to learn nothing.)
func fixedDisksLinux(ctx context.Context, run postureRunner) []string {
	res := run(ctx, "lsblk", "-ndo", "NAME,TYPE")
	if res.NotFound || !res.ok() {
		return nil
	}
	var out []string
	for _, line := range strings.Split(res.Stdout, "\n") {
		f := strings.Fields(line)
		if len(f) != 2 || f[1] != "disk" {
			continue
		}
		if strings.HasPrefix(f[0], "loop") || strings.HasPrefix(f[0], "ram") || strings.HasPrefix(f[0], "zram") {
			continue
		}
		out = append(out, "/dev/"+f[0])
	}
	return out
}

// nftInputDrops reports whether any base chain hooked to `input` has a default
// policy of drop, and whether the read that produced that verdict was
// complete. Only the DEFAULT policy counts, for the same reason the iptables
// branch above only reads `-P`.
//
// This is a small brace-DEPTH scanner, not an nft parser. It splits the whole
// ruleset text on every `{` and `}` -- wherever they fall, regardless of line
// breaks -- and tracks nesting depth across the entire stream. The depth at
// which "hook input" first appears is remembered as "the input chain's
// depth"; a "policy drop" is only believed while the scanner is still at
// exactly that depth, and the depth stops counting the moment a `}` closes
// back out of it. Because depth, not "am I on the line after hook input", is
// what is tracked, it does not matter whether a chain body is spread over
// many lines or crammed onto one, or whether a `}` shares its line with the
// next chain's opening tokens -- see the test cases in TestNftInputDrops for
// the shapes this was built against, including multiple tables each with
// their own input/forward/output chains.
//
// QUOTED TEXT IS REMOVED BEFORE ANY OF THAT HAPPENS, and it is not a nicety.
// nft rules carry free-text comments -- `tcp dport 22 accept comment "policy
// drop"` -- which sit in the input chain's body at exactly the tracked depth.
// A substring scan reads such a comment as the chain's policy and returns a
// definite `true` on an ACCEPT-policy machine: a firewall reported enabled
// when it is not, in the alert-CLEARING direction, from a string an operator
// typed. Stripping quoted spans first is what makes the scanner read
// STATEMENTS rather than text, and it also removes any `{`/`}` a comment
// contains, which would otherwise shift every depth after it.
//
// (Requiring "hook input" in the same segment as "policy drop" does NOT fix
// this, which is why it was not the fix chosen: a chain body IS one segment,
// so the comment above and the real declaration share it.)
//
// What it does NOT understand: nft also uses `{ }` for an inline anonymous
// set inside a rule, e.g. `ip saddr { 10.0.0.0/8, 192.168.0.0/16 } drop`.
// Such a set nests one depth deeper than the chain body it appears in, and
// this scanner has no notion of "set" vs. "chain" -- it just counts braces.
// That is harmless in practice: neither "hook" nor "policy" tokens ever
// appear inside a set literal, and real nft output never wraps a `policy`
// statement across a set literal. It is a real gap if nft ever emits
// something this scanner was not shaped against; when in doubt, this errs
// toward requiring "policy drop" was seen at the tracked depth rather than
// inferring it.
//
// complete is false when stripNftStrings ran off the end of an unterminated
// quote: everything after that point -- possibly a genuine "policy drop" --
// was discarded rather than read, so drops in that case is NOT a positive
// parsed statement and callers must not treat it as one (see
// probeFirewallLinux, which answers `unavailable` rather than a verdict when
// complete is false).
func nftInputDrops(out string) (drops, complete bool) {
	stripped, complete := stripNftStrings(out)
	if !complete {
		return false, false
	}
	depth := 0
	inputChainDepth := 0 // 0 == "not currently inside the input chain's body"
	for i := 0; i < len(stripped); {
		switch stripped[i] {
		case '{':
			depth++
			i++
		case '}':
			if inputChainDepth != 0 && depth == inputChainDepth {
				inputChainDepth = 0
			}
			if depth > 0 {
				depth--
			}
			i++
		default:
			j := i
			for j < len(stripped) && stripped[j] != '{' && stripped[j] != '}' {
				j++
			}
			seg := stripped[i:j]
			if inputChainDepth == 0 && strings.Contains(seg, "hook input") {
				inputChainDepth = depth
			}
			if inputChainDepth != 0 && depth == inputChainDepth && strings.Contains(seg, "policy drop") {
				return true, true
			}
			i = j
		}
	}
	return false, true
}

// stripNftStrings removes double-quoted spans, keeping everything else byte
// for byte, and reports whether every quote it opened was closed. See
// nftInputDrops for why the removal matters: a quoted span is OPERATOR TEXT
// and must not be able to spell a statement or a brace.
//
// An unterminated quote consumes the rest of the input -- everything after it
// is discarded rather than half-read. That is NOT safe to treat as "the
// ruleset said nothing more": the discarded tail can contain a genuine
// `policy drop`, and reading its absence as an accept policy mints a `false`
// on a machine that IS firewalled (finding 1, cycle 4 wave 2). So this
// reports completed=false in that case, and the caller (probeFirewallLinux,
// through nftInputDrops) must answer `unavailable` rather than a verdict
// computed from a truncated read.
func stripNftStrings(s string) (out string, completed bool) {
	if !strings.Contains(s, `"`) {
		return s, true // the overwhelmingly common case: no allocation at all
	}
	var b strings.Builder
	b.Grow(len(s))
	completed = true
	for i := 0; i < len(s); i++ {
		if s[i] != '"' {
			b.WriteByte(s[i])
			continue
		}
		// Skip to the closing quote. A backslash escapes the next byte, which
		// is how nft renders a quote inside a comment.
		closed := false
		for i++; i < len(s); i++ {
			if s[i] == '\\' {
				i++
				continue
			}
			if s[i] == '"' {
				closed = true
				break
			}
		}
		if !closed {
			completed = false
		}
	}
	return b.String(), completed
}
