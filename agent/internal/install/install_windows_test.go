//go:build windows

package install

import (
	"testing"

	"golang.org/x/sys/windows"
)

// These run in the agent-windows CI job (go test ./... on windows-latest).
// They replace the two Win32 calls that would show UI or start a process, so
// they exercise the decisions without a UAC prompt or a message box.

type shellCall struct {
	verb, file, params string
	cwd                *uint16
	show               int32
}

func fakeShell(t *testing.T) *[]shellCall {
	t.Helper()
	var calls []shellCall
	prev := shellExecute
	shellExecute = func(_ windows.Handle, verb, file, args, cwd *uint16, show int32) error {
		calls = append(calls, shellCall{
			verb: windows.UTF16PtrToString(verb), file: windows.UTF16PtrToString(file),
			params: windows.UTF16PtrToString(args), cwd: cwd, show: show,
		})
		return nil
	}
	t.Cleanup(func() { shellExecute = prev })
	return &calls
}

type boxCall struct {
	text  string
	style uint32
}

func fakeBox(t *testing.T, answer int32) *[]boxCall {
	t.Helper()
	var calls []boxCall
	prev := messageBox
	messageBox = func(_ windows.HWND, text, _ *uint16, style uint32) (int32, error) {
		calls = append(calls, boxCall{windows.UTF16PtrToString(text), style})
		return answer, nil
	}
	t.Cleanup(func() { messageBox = prev })
	return &calls
}

var testLink = Link{Console: "https://itms.toeshee.com", Code: "e2eLINKcode_0123456789-ABCDEFGHI"}

const testExe = `C:\Users\ana\Downloads\ITMS-Agent-Setup_itms.toeshee.com_e2eLINKcode_0123456789-ABCDEFGHI.exe`

func TestTheUACRelaunchIsRunasOnThisFileWithOnlyTheElevatedArgument(t *testing.T) {
	calls := fakeShell(t)
	if err := relaunchElevated(testExe); err != nil {
		t.Fatal(err)
	}
	if len(*calls) != 1 {
		t.Fatalf("ShellExecute calls = %v", *calls)
	}
	c := (*calls)[0]
	if c.verb != "runas" || c.file != testExe || c.params != "--elevated-install" || c.cwd != nil {
		t.Fatalf("ShellExecute(%q, %q, %q, cwd=%v); want runas, the file itself, and exactly "+
			"--elevated-install -- the code never crosses the UAC boundary", c.verb, c.file, c.params, c.cwd)
	}
}

func TestANoToTheHostQuestionElevatesNothing(t *testing.T) {
	shell := fakeShell(t)
	boxes := fakeBox(t, idNo)
	code, done := prelude(nil, testExe, testLink, true, false)
	if !done || code != 1 {
		t.Fatalf("prelude = %d, %v; want exit 1", code, done)
	}
	if len(*shell) != 0 {
		t.Fatal("the installer asked for elevation after the person said No")
	}
	if len(*boxes) == 0 || (*boxes)[0].text != confirmText(testLink) ||
		(*boxes)[0].style&(windows.MB_YESNO|windows.MB_ICONQUESTION) != windows.MB_YESNO|windows.MB_ICONQUESTION {
		t.Fatalf("message boxes = %+v; want the Yes/No host question first", *boxes)
	}
}

func TestAYesRelaunchesElevated(t *testing.T) {
	shell := fakeShell(t)
	fakeBox(t, idYes)
	code, done := prelude(nil, testExe, testLink, true, false)
	if !done || code != 0 || len(*shell) != 1 {
		t.Fatalf("prelude = %d, %v, relaunches %d; want the elevated copy started and this one done", code, done, len(*shell))
	}
}

func TestTheElevatedCopyIsNotAskedAgainAndProceeds(t *testing.T) {
	shell := fakeShell(t)
	boxes := fakeBox(t, idNo)
	code, done := prelude([]string{ElevatedArg}, testExe, testLink, true, true)
	if done || code != 0 || len(*shell) != 0 || len(*boxes) != 0 {
		t.Fatalf("prelude = %d, %v (relaunches %d, boxes %v); the elevated copy installs without asking twice",
			code, done, len(*shell), *boxes)
	}
}

func TestAnAlreadyElevatedDoubleClickIsStillAsked(t *testing.T) {
	fakeShell(t)
	boxes := fakeBox(t, idNo)
	if code, done := prelude(nil, testExe, testLink, true, true); !done || code != 1 || len(*boxes) == 0 {
		t.Fatalf("prelude = %d, %v; an administrator with UAC off is asked the same question", code, done)
	}
}

func TestWithNoUINothingIsAsked(t *testing.T) {
	fakeShell(t)
	boxes := fakeBox(t, idNo)
	if _, done := prelude(nil, testExe, testLink, false, true); done || len(*boxes) != 0 {
		t.Fatalf("ITS_AGENT_INSTALLER_NO_UI=1 still asked (%v)", *boxes)
	}
}

// Final review M6. EnvNoUI exists for the e2e runner, which is already
// elevated. In a release binary started unelevated -- a double-click, with
// the variable planted in the user's environment -- it must NOT skip the host
// question: that question is what stands between an unauthenticated file name
// and the UAC prompt.
func TestNoUIStillAsksTheHostQuestionWhenNotElevated(t *testing.T) {
	shell := fakeShell(t)
	boxes := fakeBox(t, idNo)
	code, done := prelude(nil, testExe, testLink, false, false)
	if !done || code != 1 || len(*shell) != 0 {
		t.Fatalf("prelude = %d, %v, relaunches %d; an unelevated NO_UI start must still be asked, and a No elevates nothing",
			code, done, len(*shell))
	}
	if len(*boxes) != 1 || (*boxes)[0].text != confirmText(testLink) {
		t.Fatalf("message boxes = %+v; want exactly the host question", *boxes)
	}
}

func TestNoUIWithAYesUnelevatedRelaunches(t *testing.T) {
	shell := fakeShell(t)
	boxes := fakeBox(t, idYes)
	code, done := prelude(nil, testExe, testLink, false, false)
	if !done || code != 0 || len(*shell) != 1 || len(*boxes) != 1 {
		t.Fatalf("prelude = %d, %v, relaunches %d, boxes %v; want asked once, then the elevated copy started",
			code, done, len(*shell), *boxes)
	}
}

func TestARelaunchThatCameBackUnelevatedStops(t *testing.T) {
	shell := fakeShell(t)
	fakeBox(t, idOK)
	if code, done := prelude([]string{ElevatedArg}, testExe, testLink, true, false); !done || code != 1 || len(*shell) != 0 {
		t.Fatalf("prelude = %d, %v; an unelevated --elevated-install must stop, not loop", code, done)
	}
}
