//go:build windows

package uninstall

import (
	"context"
	"fmt"
	"os"
	"time"

	"golang.org/x/sys/windows"

	"github.com/toeshee/it-suite/agent/internal/secdir"
)

// MsiMain is `its-agent.exe --msi-uninstall`, run by the MSI as SYSTEM. Its
// exit code is logged by Windows Installer and otherwise ignored (the custom
// action is Return="ignore"): a removal that half-fails must still let the
// MSI go, or the machine could never be cleaned up through Group Policy.
func MsiMain() int {
	// FIRST, before anything can load a library (review I2).
	if err := hardenProcess(); err != nil {
		fmt.Fprintln(os.Stderr, "its-agent --msi-uninstall: cannot restrict the DLL search path:", err)
		return 2
	}
	binary, err := os.Executable()
	if err != nil {
		fmt.Fprintln(os.Stderr, "its-agent --msi-uninstall: cannot locate this binary:", err)
		return 2
	}
	programData := os.Getenv("ProgramData")
	if err := msiUninstall(context.Background(), windows.GetCurrentProcessToken().IsElevated(), platformSystem{},
		msiTargets(programData, binary), msiLogDir(programData), appendMsiLog, time.Now()); err != nil {
		fmt.Fprintln(os.Stderr, "its-agent --msi-uninstall finished with errors:", err)
		return 1
	}
	fmt.Fprintln(os.Stderr, "its-agent --msi-uninstall: service and data removed")
	return 0
}

// appendMsiLog appends to %ProgramData%\ITSuite\msi.log WITHOUT creating the
// directory: after a clean removal it is gone and stays gone. When it is
// there, secdir.AppendLine secures it first, refuses one that anyone but
// Administrators or SYSTEM owns, and never follows a link. A line it cannot
// write is dropped.
func appendMsiLog(dir, line string) { _ = secdir.AppendLine(dir, MsiLogFile, line, false) }
