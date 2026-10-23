//go:build windows

package deploy

import (
	"os"
	"runtime"

	"golang.org/x/sys/windows"
)

// locateWinget finds App Installer's winget.exe under %ProgramFiles%. The
// folder comes from the known-folder API rather than the environment, so a
// service started with an unusual environment still looks in the real place.
func locateWinget() (string, bool) {
	programFiles, err := windows.KnownFolderPath(windows.FOLDERID_ProgramFiles, 0)
	if err != nil || programFiles == "" {
		programFiles = os.Getenv("ProgramFiles")
	}
	return locateWingetIn(programFiles, runtime.GOARCH, readDirNames, fileExists)
}

func readDirNames(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		if e.IsDir() {
			names = append(names, e.Name())
		}
	}
	return names, nil
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular()
}
