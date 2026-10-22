//go:build !linux && !darwin && !windows

package backup

import (
	"errors"
	"io/fs"
)

func modeBits(fi fs.FileInfo) uint32 { return uint32(fi.Mode().Perm()) }

func deviceOf(fs.FileInfo) (uint64, bool) { return 0, false }

func platformInodeOf(string) (uint64, error) { return 0, errors.New("no inode on this platform") }

func sameFile(a, b fs.FileInfo) bool {
	return a.Size() == b.Size() && a.ModTime().Equal(b.ModTime())
}

func platformCapture(*metaCapture, string, fs.FileInfo, *entryV2) {}

func applyMeta(*metaApply, string, *entryV2) {}
