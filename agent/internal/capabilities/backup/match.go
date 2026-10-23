package backup

import (
	"path"
	"runtime"
	"strings"
)

// Exclude patterns (spec-server-backup.md §4.1, D11).
//
// A pattern without a '/' matches an entry's name anywhere in the tree; one
// with a '/' matches the whole manifest path. Within a segment `*`, `?` and
// classes mean what path.Match says. A segment that is exactly `**` matches
// any number of segments, including none: path.Match alone treats it as `*`
// and so silently matched nothing across directories. Case-insensitive on
// Windows, where the filesystem is.

var foldCase = runtime.GOOS == "windows"

func excluded(patterns []string, p string) bool {
	if len(patterns) == 0 {
		return false
	}
	if foldCase {
		p = strings.ToLower(p)
	}
	base := path.Base(p)
	for _, pat := range patterns {
		if pat == "" {
			continue
		}
		if foldCase {
			pat = strings.ToLower(pat)
		}
		if matchGlob(pat, p) {
			return true
		}
		if !strings.Contains(pat, "/") {
			if ok, err := path.Match(pat, base); err == nil && ok {
				return true
			}
		}
	}
	return false
}

func matchGlob(pat, p string) bool {
	if !strings.Contains(pat, "**") {
		ok, err := path.Match(pat, p)
		return err == nil && ok
	}
	return matchSegs(strings.Split(pat, "/"), strings.Split(p, "/"))
}

func matchSegs(ps, ns []string) bool {
	for len(ps) > 0 {
		if ps[0] == "**" {
			for len(ps) > 0 && ps[0] == "**" {
				ps = ps[1:]
			}
			if len(ps) == 0 {
				return true
			}
			for i := 0; i <= len(ns); i++ {
				if matchSegs(ps, ns[i:]) {
					return true
				}
			}
			return false
		}
		if len(ns) == 0 {
			return false
		}
		if ok, err := path.Match(ps[0], ns[0]); err != nil || !ok {
			return false
		}
		ps, ns = ps[1:], ns[1:]
	}
	return len(ns) == 0
}

// alwaysExcluded is §4.1's default exclusions that are never data: the paging,
// hibernation and swap files, the shadow-copy store and the recycle bin.
// Readable at all only because a run holds SeBackupPrivilege. A switch, not a
// map: nothing is allocated for it on an idle agent.
func alwaysExcluded(name string) bool {
	if !foldCase {
		return false
	}
	switch strings.ToLower(name) {
	case "pagefile.sys", "hiberfil.sys", "swapfile.sys", "system volume information", "$recycle.bin":
		return true
	}
	return false
}
