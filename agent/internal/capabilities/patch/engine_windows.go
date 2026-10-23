//go:build windows

package patch

import (
	"context"

	"github.com/toeshee/it-suite/agent/internal/capabilities/deploy"
)

// Windows selects the Windows Update Agent, in process (wua_windows.go), plus
// winget for applications when App Installer is present.
func osEngine(ctx context.Context) (engine, error) {
	wua, err := newWUAEngine()
	if err != nil {
		return nil, err
	}
	return newWithApps(wua, wingetSlot()), nil
}

func wingetSlot() appSlot {
	path, ok := deploy.WingetPath()
	if !ok {
		return appSlot{source: sourceWinget,
			unsupported: "winget (App Installer) is not installed on this device"}
	}
	return appSlot{source: sourceWinget, eng: newWingetEngine(path)}
}

// newWingetEngine lives here, not beside wingetEngine, because only the
// Windows build constructs one; tests build the struct with a fake runner.
func newWingetEngine(path string) *wingetEngine {
	return &wingetEngine{path: path, run: execRunner}
}
