package health

import (
	"bytes"
	"context"
	"io"
	"strings"
	"sync"
	"testing"
)

// fakeRun records every command and answers from a table keyed by the binary
// name. A name absent from the table is reported NotFound, which is how a
// probe learns a tool is not installed.
//
// calls holds only the binary names, in call order, so existing assertions
// that compare strings.Join(f.calls, ",") against a list of binary names
// keep working. argv holds the FULL invocation (name plus args) for every
// call, in the same order, so a test can assert on the arguments a binary
// was actually invoked with.
type fakeRun struct {
	mu    sync.Mutex
	calls []string
	argv  [][]string
	out   map[string]postureResult
}

func (f *fakeRun) run(_ context.Context, name string, args ...string) postureResult {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, name)
	f.argv = append(f.argv, append([]string{name}, args...))
	if r, ok := f.out[name]; ok {
		return r
	}
	return postureResult{ExitCode: -1, NotFound: true, Err: errNotInstalled}
}

func (f *fakeRun) n() int { f.mu.Lock(); defer f.mu.Unlock(); return len(f.calls) }

// argvFor returns the recorded argv (binary name followed by its arguments)
// of the first call made to the named binary, and whether such a call was
// recorded at all.
func (f *fakeRun) argvFor(name string) ([]string, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for i, n := range f.calls {
		if n == name {
			return f.argv[i], true
		}
	}
	return nil, false
}

func TestAnUnknownCheckTypeIsUnavailableAndRunsNothing(t *testing.T) {
	f := &fakeRun{}
	s := newPostureSampler(f.run, map[string]probeFunc{})
	if got := s.Sample("posture.disk_encryption", ""); got.Available {
		t.Errorf("Sample of an unregistered probe = %+v, want unavailable", got)
	}
	if f.n() != 0 {
		t.Errorf("ran %d commands for an unregistered probe, want 0", f.n())
	}
}

func TestASecondConcurrentSampleRunsZeroCommands(t *testing.T) {
	// The overlap guard. health.check.now is a signed job that samples every
	// posture check; the posture sub-tick is driven by the engine's own timer.
	// Only one of the two is a job, so the dispatcher's per-capability
	// serialisation does not keep them apart.
	release := make(chan struct{})
	entered := make(chan struct{})
	f := &fakeRun{}
	blocking := func(ctx context.Context, run postureRunner, _ string) Sample {
		close(entered)
		<-release
		run(ctx, "slow-probe")
		return boolSample(true)
	}
	s := newPostureSampler(f.run, map[string]probeFunc{"posture.disk_encryption": blocking})

	done := make(chan Sample, 1)
	go func() { done <- s.Sample("posture.disk_encryption", "") }()
	<-entered

	second := s.Sample("posture.disk_encryption", "")
	if second.Available {
		t.Errorf("the second concurrent Sample = %+v, want unavailable", second)
	}
	if n := f.n(); n != 0 {
		t.Errorf("the second concurrent Sample ran %d commands, want 0 -- probes must not stack", n)
	}
	close(release)
	if first := <-done; !first.Available || !first.Bool {
		t.Errorf("the first Sample = %+v, want available/true", first)
	}
}

func TestTheRealRunnerReportsAMissingBinaryAsNotFoundRatherThanFailure(t *testing.T) {
	// A probe distinguishes "the tool is not installed" from "the tool ran and
	// said no" -- firewall-cmd exits non-zero to say "not running", which IS an
	// answer, while an absent binary is not.
	res := execPostureRunner(context.Background(), "definitely-not-a-real-binary-xyz")
	if !res.NotFound {
		t.Errorf("execPostureRunner on a missing binary = %+v, want NotFound", res)
	}
}

func TestPostureBoundedBufferWriteReportsTheFullLengthWithNilError(t *testing.T) {
	// io.Writer requires n == len(p) whenever err == nil. A buffer that
	// truncates p and then returns len(p) -- the TRUNCATED length -- breaks
	// that contract for any write that starts with room left but whose length
	// exceeds it.
	//
	// FALSIFYING OBSERVATION: n != len(p) for such a write, or a non-nil
	// error.
	b := &postureBoundedBuffer{limit: 10}
	p := bytes.Repeat([]byte("A"), 100)
	n, err := b.Write(p)
	if err != nil {
		t.Fatalf("Write returned an error: %v, want nil", err)
	}
	if n != len(p) {
		t.Fatalf("Write(p) returned n=%d, want n=len(p)=%d -- io.Writer requires n==len(p) when err==nil",
			n, len(p))
	}
	if got := b.buf.Len(); got != 10 {
		t.Fatalf("buffered %d bytes, want the limit of 10 -- the cap must still hold the HEAD of the "+
			"input and drop the rest", got)
	}
}

func TestLinuxDiskEncryption(t *testing.T) {
	const dev = "/dev/mapper/vg0-root"
	for _, tc := range []struct {
		name string
		out  map[string]postureResult
		want Sample
		// wantLsblkArgv, when non-nil, asserts the exact argv lsblk was
		// invoked with -- used to pin down argument-stripping behaviour that
		// the Sample result alone cannot distinguish.
		wantLsblkArgv []string
	}{
		{
			name: "root sits on a crypt device",
			out: map[string]postureResult{
				"findmnt": {Stdout: dev + "\n"},
				"lsblk":   {Stdout: "vg0-root lvm\nluks-abc crypt\nvda2 part\nvda disk\n"},
			},
			want: boolSample(true),
		},
		{
			name: "root has no crypt anywhere in its ancestry",
			out: map[string]postureResult{
				"findmnt": {Stdout: dev + "\n"},
				"lsblk":   {Stdout: "vg0-root lvm\nvda2 part\nvda disk\n"},
			},
			want: boolSample(false),
		},
		{
			name: "lsblk is not installed -- absence of evidence is not `false`",
			out:  map[string]postureResult{"findmnt": {Stdout: dev + "\n"}},
			want: unavailable(),
		},
		{
			name: "lsblk answered nothing parseable",
			out: map[string]postureResult{
				"findmnt": {Stdout: dev + "\n"},
				"lsblk":   {Stdout: "\n\n"},
			},
			want: unavailable(),
		},
		{
			name: "findmnt is not installed",
			out:  map[string]postureResult{},
			want: unavailable(),
		},
		{
			name: "findmnt reported an empty source",
			out:  map[string]postureResult{"findmnt": {Stdout: "\n"}},
			want: unavailable(),
		},
		{
			name: "a bind-mounted root's [subpath] suffix is stripped before lsblk",
			out: map[string]postureResult{
				"findmnt": {Stdout: "/dev/vda2[/rootfs]\n"},
				"lsblk":   {Stdout: "vda2 part\nluks-x crypt\n"},
			},
			want: boolSample(true),
			// The FALSIFYING OBSERVATION: lsblk must receive the bare device
			// "/dev/vda2" -- no brackets, no subpath. Asserting only the
			// Sample result does not distinguish stripping from not
			// stripping here, because lsblk's fake output happens to answer
			// "crypt" regardless of what argument it was called with.
			wantLsblkArgv: []string{"lsblk", "-nso", "NAME,TYPE", "/dev/vda2"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := &fakeRun{out: tc.out}
			if got := probeDiskEncryptionLinux(context.Background(), f.run, ""); got != tc.want {
				t.Errorf("= %+v, want %+v", got, tc.want)
			}
			if tc.wantLsblkArgv != nil {
				got, ok := f.argvFor("lsblk")
				if !ok {
					t.Fatalf("lsblk was never invoked, want argv %v", tc.wantLsblkArgv)
				}
				if len(got) != len(tc.wantLsblkArgv) {
					t.Fatalf("lsblk argv = %v, want %v", got, tc.wantLsblkArgv)
				}
				for i := range got {
					if got[i] != tc.wantLsblkArgv[i] {
						t.Fatalf("lsblk argv = %v, want %v", got, tc.wantLsblkArgv)
					}
				}
			}
		})
	}
}

func TestLinuxFirewall(t *testing.T) {
	for _, tc := range []struct {
		name     string
		out      map[string]postureResult
		want     Sample
		wantRuns []string // exact commands, in order
	}{
		{
			name:     "ufw active wins and nothing below it runs",
			out:      map[string]postureResult{"ufw": {Stdout: "Status: active\n"}},
			want:     boolSample(true),
			wantRuns: []string{"ufw"},
		},
		{
			name:     "ufw inactive is an answer, not a fallthrough",
			out:      map[string]postureResult{"ufw": {Stdout: "Status: inactive\n"}},
			want:     boolSample(false),
			wantRuns: []string{"ufw"},
		},
		{
			name:     "ufw present but unreadable (not root) is unavailable, not false",
			out:      map[string]postureResult{"ufw": {Stderr: "ERROR: You need to be root to run this script", ExitCode: 1, Err: errNotInstalled}},
			want:     unavailable(),
			wantRuns: []string{"ufw"},
		},
		{
			name: "firewalld running, ufw absent",
			out:  map[string]postureResult{"firewall-cmd": {Stdout: "running\n"}},
			want: boolSample(true), wantRuns: []string{"ufw", "firewall-cmd"},
		},
		{
			name: "firewalld says not running on a NON-ZERO exit -- still an answer",
			out: map[string]postureResult{
				"firewall-cmd": {Stdout: "not running\n", ExitCode: 252, Err: errNotInstalled},
			},
			want: boolSample(false), wantRuns: []string{"ufw", "firewall-cmd"},
		},
		{
			name: "firewalld running with a PolicyKit warning on stderr is still running",
			out: map[string]postureResult{
				"firewall-cmd": {Stdout: "running\n", Stderr: "WARNING: AuthorizationManager.checkAuthorization(): no polkit agent"},
			},
			want: boolSample(true), wantRuns: []string{"ufw", "firewall-cmd"},
		},
		{
			name: "firewalld not running with extra deprecation text on stderr must not be read as running",
			out: map[string]postureResult{
				"firewall-cmd": {Stdout: "not running\n", Stderr: "FutureWarning: --state is deprecated", ExitCode: 252, Err: errNotInstalled},
			},
			want: boolSample(false), wantRuns: []string{"ufw", "firewall-cmd"},
		},
		{
			name: "no front-end: nft input policy drop",
			out: map[string]postureResult{
				"nft": {Stdout: "table inet filter {\n  chain input {\n    type filter hook input priority 0; policy drop;\n  }\n}\n"},
			},
			want: boolSample(true), wantRuns: []string{"ufw", "firewall-cmd", "nft"},
		},
		{
			name: "no front-end: nft ruleset with an accept policy is NOT enabled",
			out: map[string]postureResult{
				"nft": {Stdout: "table inet filter {\n  chain input {\n    type filter hook input priority 0; policy accept;\n    tcp dport 22 accept\n  }\n}\n"},
			},
			want: boolSample(false), wantRuns: []string{"ufw", "firewall-cmd", "nft"},
		},
		{
			name: "no front-end, no nft: iptables DROP policy",
			out: map[string]postureResult{
				"iptables": {Stdout: "-P INPUT DROP\n-A INPUT -i lo -j ACCEPT\n"},
			},
			want: boolSample(true), wantRuns: []string{"ufw", "firewall-cmd", "nft", "iptables"},
		},
		{
			name: "no front-end, no nft: iptables ACCEPT policy with rules is not a firewall",
			out: map[string]postureResult{
				"iptables": {Stdout: "-P INPUT ACCEPT\n-A INPUT -p tcp --dport 22 -j ACCEPT\n"},
			},
			want: boolSample(false), wantRuns: []string{"ufw", "firewall-cmd", "nft", "iptables"},
		},
		{
			name: "nothing installed at all",
			out:  map[string]postureResult{},
			want: unavailable(), wantRuns: []string{"ufw", "firewall-cmd", "nft", "iptables"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := &fakeRun{out: tc.out}
			if got := probeFirewallLinux(context.Background(), f.run, ""); got != tc.want {
				t.Errorf("= %+v, want %+v", got, tc.want)
			}
			if strings.Join(f.calls, ",") != strings.Join(tc.wantRuns, ",") {
				t.Errorf("ran %v, want %v", f.calls, tc.wantRuns)
			}
		})
	}
}

// TestNftInputDrops exercises the small scanner directly. The existing
// TestLinuxFirewall cases above only ever exercise a single table with a
// single chain; these cases pin down the shapes that leak state across
// chains -- the failure this test suite exists to catch.
func TestNftInputDrops(t *testing.T) {
	for _, tc := range []struct {
		name string
		out  string
		want bool
	}{
		{
			name: "single-line input-accept chain followed by a single-line forward-drop chain must not leak",
			out:  "table inet filter {\n  chain input { type filter hook input priority 0; policy accept; }\n  chain forward { type filter hook forward priority 0; policy drop; }\n}\n",
			want: false,
		},
		{
			name: "single-line input-drop chain is true",
			out:  "table inet filter {\n  chain input { type filter hook input priority 0; policy drop; }\n  chain forward { type filter hook forward priority 0; policy accept; }\n}\n",
			want: true,
		},
		{
			name: "policy drop on the same line as hook input",
			out:  "table inet filter {\n  chain input {\n    type filter hook input priority 0; policy drop;\n  }\n}\n",
			want: true,
		},
		{
			name: "policy drop on a later line, well inside the input chain body",
			out:  "table inet filter {\n  chain input {\n    type filter hook input priority 0;\n    tcp dport 22 accept\n    policy drop;\n  }\n}\n",
			want: true,
		},
		{
			name: "closing brace sharing a line with other tokens still ends the input chain",
			out:  "table inet filter {\n  chain input { type filter hook input priority 0; policy accept; } chain forward { type filter hook forward priority 0; policy drop; }\n}\n",
			want: false,
		},
		{
			name: "several tables, each with input/forward/output -- only input's own policy counts",
			out: "table inet filter {\n" +
				"  chain input { type filter hook input priority 0; policy accept; }\n" +
				"  chain forward { type filter hook forward priority 0; policy drop; }\n" +
				"  chain output { type filter hook output priority 0; policy drop; }\n" +
				"}\n" +
				"table ip mangle {\n" +
				"  chain prerouting { type filter hook prerouting priority -150; policy accept; }\n" +
				"}\n",
			want: false,
		},
		{
			name: "several tables, one of which drops on input, appearing after tables that don't",
			out: "table inet filter {\n" +
				"  chain forward { type filter hook forward priority 0; policy accept; }\n" +
				"  chain output { type filter hook output priority 0; policy accept; }\n" +
				"}\n" +
				"table ip filter {\n" +
				"  chain input { type filter hook input priority 0; policy drop; }\n" +
				"}\n",
			want: true,
		},
		{
			// The counterexample to "it cannot manufacture a false verdict":
			// a rule COMMENT sits in the input chain body at the tracked
			// depth, so a substring scan reads it as the chain's policy. The
			// verdict it manufactures is `true` on an ACCEPT-policy machine --
			// the alert-CLEARING direction, which is the one that costs an
			// operator something.
			name: "a rule comment containing the words policy drop is not a policy",
			out:  "table inet filter {\n  chain input {\n    type filter hook input priority 0; policy accept;\n    tcp dport 22 accept comment \"policy drop\"\n  }\n}\n",
			want: false,
		},
		{
			name: "a rule comment containing the words hook input does not start an input chain",
			out:  "table inet filter {\n  chain forward {\n    type filter hook forward priority 0; policy drop;\n    tcp dport 22 accept comment \"hook input\"\n  }\n}\n",
			want: false,
		},
		{
			name: "a quoted brace does not shift the depth the policy is read at",
			out:  "table inet filter {\n  chain input {\n    type filter hook input priority 0; policy drop;\n    tcp dport 22 accept comment \"{{{\"\n  }\n}\n",
			want: true,
		},
		{
			name: "a real policy drop is still read when a comment also mentions one",
			out:  "table inet filter {\n  chain input {\n    type filter hook input priority 0; policy drop;\n    tcp dport 22 accept comment \"policy drop\"\n  }\n}\n",
			want: true,
		},
		{
			name: "no input hook anywhere",
			out:  "table inet filter {\n  chain forward { type filter hook forward priority 0; policy drop; }\n}\n",
			want: false,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, complete := nftInputDrops(tc.out)
			if !complete {
				t.Fatalf("nftInputDrops(%q) reported an incomplete read; every case in this table has "+
					"balanced quotes and must not", tc.out)
			}
			if got != tc.want {
				t.Errorf("nftInputDrops(%q) = %v, want %v", tc.out, got, tc.want)
			}
		})
	}
}

// TestNftInputDropsUnterminatedQuoteIsIncomplete pins down the completion
// signal directly: an unterminated quote must be reported as an incomplete
// read (regardless of what drops happens to come back as), and a properly
// closed escaped quote must not be mistaken for one.
func TestNftInputDropsUnterminatedQuoteIsIncomplete(t *testing.T) {
	for _, tc := range []struct {
		name         string
		out          string
		wantComplete bool
	}{
		{
			name: "unterminated quote swallows a genuine policy drop further down",
			out: "table inet filter {\n" +
				"  chain forward {\n" +
				"    type filter hook forward priority 0; policy accept;\n" +
				"    tcp dport 80 accept comment \"oops\n" +
				"  }\n" +
				"  chain input {\n" +
				"    type filter hook input priority 0; policy drop;\n" +
				"  }\n" +
				"}\n",
			wantComplete: false,
		},
		{
			// The reproduction's counterexample: a backslash-escaped
			// backslash immediately before the real closing quote must not
			// be mistaken for an unterminated string.
			name: "an escaped backslash right before the closing quote still closes it",
			out: "table inet filter {\n  chain input {\n    type filter hook input priority 0; policy drop;\n" +
				"    tcp dport 22 accept comment \"C:\\\\\"\n  }\n}\n",
			wantComplete: true,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, complete := nftInputDrops(tc.out)
			if complete != tc.wantComplete {
				t.Errorf("nftInputDrops(%q) complete = %v, want %v", tc.out, complete, tc.wantComplete)
			}
		})
	}
}

// TestStripNftStrings exercises the stripper directly, including the
// completion signal that probeFirewallLinux now depends on to avoid minting a
// verdict from a truncated read.
func TestStripNftStrings(t *testing.T) {
	for _, tc := range []struct {
		name         string
		in           string
		wantOut      string
		wantComplete bool
	}{
		{
			name:         "no quotes at all",
			in:           "policy drop;",
			wantOut:      "policy drop;",
			wantComplete: true,
		},
		{
			name:         "a balanced quote is removed",
			in:           `a "quoted text" b`,
			wantOut:      "a  b",
			wantComplete: true,
		},
		{
			name:         "an escaped backslash immediately before the closing quote still closes it",
			in:           `comment "C:\\"`,
			wantOut:      `comment `,
			wantComplete: true,
		},
		{
			name:         "an unterminated quote consumes the rest of the input and is reported incomplete",
			in:           `a "unterminated and the rest is gone`,
			wantOut:      "a ",
			wantComplete: false,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			out, complete := stripNftStrings(tc.in)
			if out != tc.wantOut {
				t.Errorf("stripNftStrings(%q) out = %q, want %q", tc.in, out, tc.wantOut)
			}
			if complete != tc.wantComplete {
				t.Errorf("stripNftStrings(%q) complete = %v, want %v", tc.in, complete, tc.wantComplete)
			}
		})
	}
}

// TestFirewallLinuxUnterminatedNftQuoteMustNotMintAVerdict is the finding-1
// reproduction: an unterminated quote in an nft comment makes stripNftStrings
// discard the rest of the ruleset, including a genuine `policy drop` on the
// input chain further down. That reads as an accept-policy machine and
// probeFirewallLinux must NOT answer a definite `false` for it -- this
// machine IS firewalled, and a `false` here is a breach alert nothing
// actually observed. It must answer `unavailable`.
func TestFirewallLinuxUnterminatedNftQuoteMustNotMintAVerdict(t *testing.T) {
	out := "table inet filter {\n" +
		"  chain forward {\n" +
		"    type filter hook forward priority 0; policy accept;\n" +
		"    tcp dport 80 accept comment \"oops\n" +
		"  }\n" +
		"  chain input {\n" +
		"    type filter hook input priority 0; policy drop;\n" +
		"  }\n" +
		"}\n"
	f := &fakeRun{out: map[string]postureResult{"nft": {Stdout: out}}}
	got := probeFirewallLinux(context.Background(), f.run, "")
	if got != unavailable() {
		t.Errorf("probeFirewallLinux with an unterminated nft comment quote = %+v, want unavailable "+
			"(the ruleset was truncated by the stripper, not genuinely read as an accept policy)", got)
	}
}

func TestLinuxSMART(t *testing.T) {
	pass := postureResult{Stdout: "SMART overall-health self-assessment test result: PASSED\n"}
	fail := postureResult{Stdout: "SMART overall-health self-assessment test result: FAILED!\n", ExitCode: 8, Err: errNotInstalled}

	t.Run("a named device that passes", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{"smartctl": pass}}
		if got := probeSMARTLinux(context.Background(), f.run, "/dev/sda"); got != boolSample(true) {
			t.Errorf("= %+v, want true", got)
		}
	})
	t.Run("a named device that is failing", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{"smartctl": fail}}
		if got := probeSMARTLinux(context.Background(), f.run, "/dev/sda"); got != boolSample(false) {
			t.Errorf("= %+v, want false", got)
		}
	})
	t.Run("smartctl absent is unavailable, never true", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{}}
		if got := probeSMARTLinux(context.Background(), f.run, "/dev/sda"); got.Available {
			t.Errorf("= %+v, want unavailable", got)
		}
	})
	t.Run("star: no device could be read at all is unavailable, NOT true", func(t *testing.T) {
		// The tempting "true if no failures were seen" would report a healthy
		// fleet on a machine where smartctl cannot read a single disk.
		f := &fakeRun{out: map[string]postureResult{
			"lsblk":    {Stdout: "sda disk\nsdb disk\n"},
			"smartctl": {Stdout: "Smartctl open device: /dev/sda failed: Permission denied\n", ExitCode: 2, Err: errNotInstalled},
		}}
		if got := probeSMARTLinux(context.Background(), f.run, "*"); got.Available {
			t.Errorf("= %+v, want unavailable", got)
		}
	})
	t.Run("star: any failing device makes the whole answer false", func(t *testing.T) {
		calls := 0
		run := func(ctx context.Context, name string, args ...string) postureResult {
			if name == "lsblk" {
				return postureResult{Stdout: "sda disk\nsdb disk\n"}
			}
			calls++
			if calls == 2 {
				return fail
			}
			return pass
		}
		if got := probeSMARTLinux(context.Background(), run, "*"); got != boolSample(false) {
			t.Errorf("= %+v, want false", got)
		}
	})
	t.Run("star: every readable device passing is true", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{
			"lsblk":    {Stdout: "sda disk\nsdb disk\n"},
			"smartctl": pass,
		}}
		if got := probeSMARTLinux(context.Background(), f.run, "*"); got != boolSample(true) {
			t.Errorf("= %+v, want true", got)
		}
	})
}

func TestDarwinPostureProbes(t *testing.T) {
	t.Run("FileVault on", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{"fdesetup": {Stdout: "FileVault is On.\n"}}}
		if got := probeDiskEncryptionDarwin(context.Background(), f.run, ""); got != boolSample(true) {
			t.Errorf("= %+v, want true", got)
		}
	})
	t.Run("FileVault off", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{"fdesetup": {Stdout: "FileVault is Off.\n"}}}
		if got := probeDiskEncryptionDarwin(context.Background(), f.run, ""); got != boolSample(false) {
			t.Errorf("= %+v, want false", got)
		}
	})
	t.Run("fdesetup said something else entirely", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{"fdesetup": {Stdout: "Deferred enablement appears to be active\n"}}}
		if got := probeDiskEncryptionDarwin(context.Background(), f.run, ""); got.Available {
			t.Errorf("= %+v, want unavailable", got)
		}
	})
	t.Run("fdesetup absent", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{}}
		if got := probeDiskEncryptionDarwin(context.Background(), f.run, ""); got.Available {
			t.Errorf("= %+v, want unavailable", got)
		}
	})
	t.Run("firewall enabled for specific services (state 1)", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{
			"/usr/libexec/ApplicationFirewall/socketfilterfw": {Stdout: "Firewall is enabled. (State = 1)\n"},
		}}
		if got := probeFirewallDarwin(context.Background(), f.run, ""); got != boolSample(true) {
			t.Errorf("= %+v, want true", got)
		}
	})
	t.Run("firewall blocking all (state 2) is still enabled", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{
			"/usr/libexec/ApplicationFirewall/socketfilterfw": {Stdout: "Firewall is enabled. (State = 2)\n"},
		}}
		if got := probeFirewallDarwin(context.Background(), f.run, ""); got != boolSample(true) {
			t.Errorf("= %+v, want true", got)
		}
	})
	t.Run("firewall disabled (state 0)", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{
			"/usr/libexec/ApplicationFirewall/socketfilterfw": {Stdout: "Firewall is disabled. (State = 0)\n"},
		}}
		if got := probeFirewallDarwin(context.Background(), f.run, ""); got != boolSample(false) {
			t.Errorf("= %+v, want false", got)
		}
	})
	t.Run("socketfilterfw output not recognised", func(t *testing.T) {
		f := &fakeRun{out: map[string]postureResult{
			"/usr/libexec/ApplicationFirewall/socketfilterfw": {Stdout: "???\n"},
		}}
		if got := probeFirewallDarwin(context.Background(), f.run, ""); got.Available {
			t.Errorf("= %+v, want unavailable", got)
		}
	})
}

func TestPostureBoundedBufferSurvivesIoCopyPastTheLimit(t *testing.T) {
	// The real failure mode: exec.Cmd copies a non-*os.File Stdout/Stderr
	// through an internal io.Copy goroutine in 32 KiB chunks. If Write ever
	// reports n < len(p) with a nil error, io.Copy turns that into a "short
	// write" error and cmd.Run() reports the process as failed even though it
	// ran and exited cleanly.
	//
	// FALSIFYING OBSERVATION: a non-nil error from io.Copy, or content beyond
	// the limit retained.
	b := &postureBoundedBuffer{limit: 10}
	src := bytes.NewReader(bytes.Repeat([]byte("B"), 100))
	copied, err := io.Copy(b, src)
	if err != nil {
		t.Fatalf("io.Copy returned an error: %v, want nil -- this is the short-write regression", err)
	}
	if copied != 100 {
		t.Fatalf("io.Copy reported %d bytes copied, want 100", copied)
	}
	if got := b.buf.Len(); got != 10 {
		t.Fatalf("buffered %d bytes, want the limit of 10", got)
	}
}
