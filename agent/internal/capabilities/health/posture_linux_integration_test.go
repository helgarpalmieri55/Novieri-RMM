package health

// This file has NO build tag; each test skips at runtime when the tool it
// needs is absent. A skip that reads as a pass is how a gap survives, so every
// skip here names what did not run.

import (
	"context"
	"os/exec"
	"strings"
	"testing"
)

// TestARealDiskEncryptionProbeAgreesWithLsblk runs the probe against the REAL
// tools on this machine and checks its answer against an independently derived
// one, rather than merely checking that it returned something.
func TestARealDiskEncryptionProbeAgreesWithLsblk(t *testing.T) {
	for _, tool := range []string{"findmnt", "lsblk"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Skipf("SKIPPING the only real-tool disk-encryption proof: %q is not installed. "+
				"Nothing in this run exercised a real %s.", tool, tool)
		}
	}
	got := probeDiskEncryptionLinux(context.Background(), execPostureRunner, "")
	if !got.Available {
		t.Fatalf("the probe could not answer on a machine with both tools present: %+v", got)
	}

	// Independent derivation: ask the same two tools by hand.
	src, err := exec.Command("findmnt", "-n", "-o", "SOURCE", "--target", "/").Output()
	if err != nil {
		t.Fatalf("findmnt: %v", err)
	}
	dev := strings.TrimSpace(string(src))
	if i := strings.IndexByte(dev, '['); i >= 0 {
		dev = dev[:i]
	}
	anc, err := exec.Command("lsblk", "-nso", "NAME,TYPE", dev).Output()
	if err != nil {
		t.Fatalf("lsblk: %v", err)
	}
	want := strings.Contains(string(anc), " crypt")
	if got.Bool != want {
		t.Errorf("probe said encrypted=%v; lsblk's ancestry for %s says %v\n%s",
			got.Bool, dev, want, anc)
	}
	t.Logf("real disk-encryption probe on this machine: encrypted=%v (device %s)", got.Bool, dev)
}

// TestARealFirewallProbeReachesADefiniteAnswerOrSaysWhyNot exercises the
// ladder against whatever this machine actually has.
func TestARealFirewallProbeReachesADefiniteAnswerOrSaysWhyNot(t *testing.T) {
	have := []string{}
	for _, tool := range []string{"ufw", "firewall-cmd", "nft", "iptables"} {
		if _, err := exec.LookPath(tool); err == nil {
			have = append(have, tool)
		}
	}
	if len(have) == 0 {
		t.Skip("SKIPPING the only real-tool firewall proof: none of ufw, firewall-cmd, nft or iptables " +
			"is installed. Nothing in this run exercised a real firewall front-end or ruleset.")
	}
	got := probeFirewallLinux(context.Background(), execPostureRunner, "")
	t.Logf("real firewall probe on this machine (tools present: %v): available=%v value=%v",
		have, got.Available, got.Bool)
	if !got.Available && len(have) > 0 {
		// Not a failure: `nft list ruleset` needs privilege, and unavailable is
		// the correct answer for "installed but unreadable". Logged so a run
		// that answers nothing is visible rather than silent.
		t.Logf("the probe reported unavailable despite %v being present -- expected without privilege", have)
	}
}
