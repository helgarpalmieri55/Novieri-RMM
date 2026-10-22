package backup

import "testing"

// Exclude patterns with `**` (spec-server-backup.md §4.1, D11). path.Match
// alone read `**` as `*`, so `**/*.tmp` silently excluded nothing below the
// top level.

func TestDoubleStarExcludes(t *testing.T) {
	for _, c := range []struct {
		pat, p string
		want   bool
	}{
		{"**/*.tmp", "/srv/a/b/c.tmp", true},
		{"**/*.tmp", "/c.tmp", true},
		{"**/*.tmp", "/srv/a/c.txt", false},
		{"/srv/**/cache", "/srv/cache", true},
		{"/srv/**/cache", "/srv/x/y/cache", true},
		{"/srv/**/cache", "/other/cache", false},
		{"/var/tmp/**", "/var/tmp/x/y", true},
		{"*.bak", "/srv/deep/file.bak", true}, // no '/': matches the name
		{"/srv/*.bak", "/srv/deep/file.bak", false},
		{"/srv/*/file.bak", "/srv/deep/file.bak", true},
		{"node_modules", "/app/node_modules", true},
	} {
		if got := excluded([]string{c.pat}, c.p); got != c.want {
			t.Errorf("excluded(%q, %q) = %v, want %v", c.pat, c.p, got, c.want)
		}
	}
}
