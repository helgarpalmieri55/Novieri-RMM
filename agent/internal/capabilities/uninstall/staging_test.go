package uninstall

import (
	"reflect"
	"testing"
)

// Review M1. On Windows the helper runs from a COPY, because a running image
// cannot be deleted. The copy is staged under %ProgramData%\ITSuite in a
// directory of its own, created SYSTEM+Administrators-only and owned by
// Administrators -- not in the service's temp directory -- and both the copy
// and that directory are handed to MoveFileEx(DELAY_UNTIL_REBOOT), file first
// so the directory is empty when its turn comes.

func TestTheHelperIsStagedInItsOwnDirectoryUnderProgramData(t *testing.T) {
	dir, exe := helperStaging(`C:\ProgramData`, 1727560000123456789)
	if dir != `C:\ProgramData\ITSuite\uninstall-1727560000123456789` {
		t.Fatalf("dir = %q", dir)
	}
	if exe != dir+`\its-agent-uninstall.exe` {
		t.Fatalf("exe = %q", exe)
	}
	if dir, _ := helperStaging("", 7); dir != `C:\ProgramData\ITSuite\uninstall-7` {
		t.Fatalf("an unset %%ProgramData%% must fall back to C:\\ProgramData, got %q", dir)
	}
	if dir, _ := helperStaging(`D:\Data\`, 7); dir != `D:\Data\ITSuite\uninstall-7` {
		t.Fatalf("dir = %q", dir)
	}
}

func TestTheStagedCopyAndItsDirectoryAreBothDeletedAtBootFileFirst(t *testing.T) {
	_, exe := helperStaging(`C:\ProgramData`, 42)
	want := []string{exe, `C:\ProgramData\ITSuite\uninstall-42`}
	if got := bootDeletions(exe); !reflect.DeepEqual(got, want) {
		t.Fatalf("bootDeletions = %v, want %v", got, want)
	}
	// A helper image anywhere else is deleted alone: its directory is not ours.
	if got := bootDeletions(`C:\Windows\Temp\its-agent-uninstall-1.exe`); !reflect.DeepEqual(got,
		[]string{`C:\Windows\Temp\its-agent-uninstall-1.exe`}) {
		t.Fatalf("bootDeletions = %v", got)
	}
	if got := bootDeletions(`C:\Users\x\uninstall-3\its-agent-uninstall.exe`); len(got) != 1 {
		t.Fatalf("a directory outside ...\\ITSuite was scheduled for deletion: %v", got)
	}
}
