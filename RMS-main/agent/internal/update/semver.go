// Package update is the self-update engine shared by the `agent.update`
// capability, the transient helper process and the start-up Resume path
// (docs/modules/agent-distribution/spec-self-update.md §4-§5).
package update

import (
	"errors"
	"strconv"
	"strings"
)

// MaxVersionLen bounds a version string, after the optional leading "v".
const MaxVersionLen = 50

// ErrVersion is returned for anything that is not strict MAJOR.MINOR.PATCH[-pre].
var ErrVersion = errors.New("not a strict semantic version")

// Version is a parsed MAJOR.MINOR.PATCH[-pre]. Build metadata is refused
// rather than ignored: two builds that differ only in "+meta" would otherwise
// be one version with two digests.
type Version struct {
	Major, Minor, Patch uint64
	Pre                 []string
}

// ParseVersion parses a version; one leading "v" is stripped.
func ParseVersion(s string) (Version, error) {
	s = strings.TrimPrefix(s, "v")
	if s == "" || len(s) > MaxVersionLen {
		return Version{}, ErrVersion
	}
	core, pre, hasPre := strings.Cut(s, "-")
	if strings.ContainsRune(s, '+') {
		return Version{}, ErrVersion
	}
	parts := strings.Split(core, ".")
	if len(parts) != 3 {
		return Version{}, ErrVersion
	}
	var nums [3]uint64
	for i, p := range parts {
		n, ok := numericIdent(p)
		if !ok {
			return Version{}, ErrVersion
		}
		nums[i] = n
	}
	v := Version{Major: nums[0], Minor: nums[1], Patch: nums[2]}
	if hasPre {
		ids := strings.Split(pre, ".")
		for _, id := range ids {
			if !validPreIdent(id) {
				return Version{}, ErrVersion
			}
		}
		v.Pre = ids
	}
	return v, nil
}

func numericIdent(p string) (uint64, bool) {
	if p == "" || (len(p) > 1 && p[0] == '0') {
		return 0, false
	}
	for _, c := range p {
		if c < '0' || c > '9' {
			return 0, false
		}
	}
	n, err := strconv.ParseUint(p, 10, 64)
	return n, err == nil
}

func validPreIdent(id string) bool {
	if id == "" {
		return false
	}
	allDigits := true
	for _, c := range id {
		switch {
		case c >= '0' && c <= '9':
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c == '-':
			allDigits = false
		default:
			return false
		}
	}
	return !allDigits || id == "0" || id[0] != '0'
}

// String is the canonical form, without the "v".
func (v Version) String() string {
	s := strconv.FormatUint(v.Major, 10) + "." + strconv.FormatUint(v.Minor, 10) + "." +
		strconv.FormatUint(v.Patch, 10)
	if len(v.Pre) > 0 {
		s += "-" + strings.Join(v.Pre, ".")
	}
	return s
}

// Compare returns -1, 0 or 1 by semver 2.0.0 precedence.
func Compare(a, b Version) int {
	for _, d := range [3][2]uint64{{a.Major, b.Major}, {a.Minor, b.Minor}, {a.Patch, b.Patch}} {
		if d[0] != d[1] {
			return cmpU(d[0], d[1])
		}
	}
	switch {
	case len(a.Pre) == 0 && len(b.Pre) == 0:
		return 0
	case len(a.Pre) == 0:
		return 1
	case len(b.Pre) == 0:
		return -1
	}
	for i := 0; i < len(a.Pre) && i < len(b.Pre); i++ {
		if c := comparePre(a.Pre[i], b.Pre[i]); c != 0 {
			return c
		}
	}
	return cmpU(uint64(len(a.Pre)), uint64(len(b.Pre)))
}

func comparePre(x, y string) int {
	xn, xNum := numericIdent(x)
	yn, yNum := numericIdent(y)
	switch {
	case xNum && yNum:
		return cmpU(xn, yn)
	case xNum:
		return -1
	case yNum:
		return 1
	}
	return strings.Compare(x, y)
}

func cmpU(a, b uint64) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	}
	return 0
}
