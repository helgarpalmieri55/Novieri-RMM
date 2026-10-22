package inventory

import (
	"io"
	"io/fs"
	"path"
	"strings"
)

// macOS installed applications: the bundles in /Applications, including one
// level of folders inside it (/Applications/Utilities, vendor folders such as
// "Microsoft Office"). Bundles nested inside other bundles are helpers, not
// applications, and are not scanned. No build tag: fsys is "/" on a Mac and
// testdata/macos in the tests.
//
// pkgutil receipts are not read: they are package identifiers
// (com.apple.pkg.*, com.vendor.pkg.component) rather than product names, and
// the products they install land in /Applications anyway.

// maxPlistBytes bounds one Info.plist read. Real ones are a few KiB.
const maxPlistBytes = 1 << 20

// macApplications returns at most limit applications found under
// fsys/Applications.
func macApplications(fsys fs.FS, limit int) []Software {
	var out []Software
	var scan func(dir string, depth int)
	scan = func(dir string, depth int) {
		entries, err := fs.ReadDir(fsys, dir)
		if err != nil {
			return
		}
		for _, e := range entries {
			if len(out) >= limit {
				return
			}
			name := e.Name()
			if strings.HasPrefix(name, ".") {
				continue
			}
			p := path.Join(dir, name)
			if strings.HasSuffix(name, ".app") {
				if s, ok := readBundle(fsys, p, name); ok {
					out = append(out, s)
				}
				continue
			}
			if depth == 0 && e.IsDir() {
				scan(p, depth+1)
			}
		}
	}
	scan("Applications", 0)
	return out
}

// readBundle turns one .app's Info.plist into a Software entry.
func readBundle(fsys fs.FS, bundle, folder string) (Software, bool) {
	f, err := fsys.Open(path.Join(bundle, "Contents", "Info.plist"))
	if err != nil {
		return Software{}, false
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, maxPlistBytes+1))
	if err != nil || len(raw) > maxPlistBytes {
		return Software{}, false
	}
	info, err := parsePlist(raw)
	if err != nil {
		return Software{}, false
	}
	name := firstUsable(info["CFBundleDisplayName"], info["CFBundleName"], strings.TrimSuffix(folder, ".app"))
	version := firstUsable(info["CFBundleShortVersionString"], info["CFBundleVersion"])
	return Software{Name: name, Version: version}, name != ""
}

// firstUsable returns the first value that is non-empty and not an unexpanded
// Xcode build-setting placeholder such as "${PRODUCT_NAME}".
func firstUsable(values ...string) string {
	for _, v := range values {
		v = strings.TrimSpace(v)
		if v != "" && !strings.Contains(v, "${") && !strings.Contains(v, "$(") {
			return v
		}
	}
	return ""
}
