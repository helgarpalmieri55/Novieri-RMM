package install

import "testing"

// Review I3. The console host comes from a FILE NAME, which anyone can write,
// so before anything is elevated the person at the machine is asked, in
// words, where this computer is about to be connected.
func TestThePromptNamesTheConsoleTheFileNamePointsAt(t *testing.T) {
	for console, want := range map[string]string{
		"https://itms.toeshee.com":      "Install the ITMS agent and connect this computer to itms.toeshee.com?",
		"https://itms.toeshee.com:8443": "Install the ITMS agent and connect this computer to itms.toeshee.com:8443?",
	} {
		if got := confirmText(Link{Console: console}); got != want {
			t.Errorf("confirmText(%q) = %q, want %q", console, got, want)
		}
	}
}

// Final review M6. EnvNoUI skips the host question ONLY in a process that is
// already elevated (the e2e runner). A release binary started unelevated with
// the variable set -- by anyone who can write the user's environment -- is
// still asked. The UAC re-launch is never asked again: the person said Yes.
func TestTheHostQuestionIsSkippedOnlyForAnElevatedNoUIStartOrTheRelaunch(t *testing.T) {
	for _, c := range []struct {
		relaunched, noUI, elevated, ask bool
	}{
		{relaunched: false, noUI: false, elevated: false, ask: true},
		{relaunched: false, noUI: false, elevated: true, ask: true},
		{relaunched: false, noUI: true, elevated: false, ask: true},
		{relaunched: false, noUI: true, elevated: true, ask: false},
		{relaunched: true, noUI: false, elevated: true, ask: false},
		{relaunched: true, noUI: false, elevated: false, ask: false},
		{relaunched: true, noUI: true, elevated: false, ask: false},
	} {
		if got := asksHost(c.relaunched, c.noUI, c.elevated); got != c.ask {
			t.Errorf("asksHost(relaunched=%v, noUI=%v, elevated=%v) = %v, want %v",
				c.relaunched, c.noUI, c.elevated, got, c.ask)
		}
	}
}
