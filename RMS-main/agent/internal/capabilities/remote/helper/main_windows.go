package helper

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Main is the `remote-helper` subcommand: the helper process the service
// launches into the user's session. It returns the process exit code.
func Main() int {
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	if err := checkLaunch(); err != nil {
		log.Error("remote-helper refused to start", "err", err)
		return 2
	}
	setDPIAware()
	if err := Run(context.Background(), os.Stdin, os.Stdout, NewPlatform(log), Options{Logger: log}); err != nil {
		log.Error("remote-helper", "err", err)
		return 1
	}
	return 0
}

// checkLaunch refuses to run unless the helper's stdin and stdout are pipes
// and its parent is this same executable (the agent service). It is a guard
// against being started by hand, not a security boundary: the process runs as
// the user either way.
func checkLaunch() error {
	for _, std := range []uint32{windows.STD_INPUT_HANDLE, windows.STD_OUTPUT_HANDLE} {
		h, err := windows.GetStdHandle(std)
		if err != nil {
			return err
		}
		if t, _ := windows.GetFileType(h); t != windows.FILE_TYPE_PIPE {
			return errors.New("standard input and output must be the service's pipes")
		}
	}
	self, err := os.Executable()
	if err != nil {
		return err
	}
	parent, err := parentImage()
	if err != nil {
		return err
	}
	if !strings.EqualFold(parent, filepath.Base(self)) {
		return fmt.Errorf("parent is %q, not the agent", parent)
	}
	return nil
}

func parentImage() (string, error) {
	snap, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return "", err
	}
	defer windows.CloseHandle(snap)
	names := map[uint32]string{}
	parents := map[uint32]uint32{}
	e := windows.ProcessEntry32{Size: uint32(unsafe.Sizeof(windows.ProcessEntry32{}))}
	for err = windows.Process32First(snap, &e); err == nil; err = windows.Process32Next(snap, &e) {
		names[e.ProcessID] = windows.UTF16ToString(e.ExeFile[:])
		parents[e.ProcessID] = e.ParentProcessID
	}
	ppid, ok := parents[uint32(os.Getpid())]
	if !ok {
		return "", errors.New("own process not in the snapshot")
	}
	name, ok := names[ppid]
	if !ok {
		return "", errors.New("parent has exited")
	}
	return name, nil
}
