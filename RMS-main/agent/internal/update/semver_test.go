package update

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

type semverVectors struct {
	Valid []struct {
		Input     string `json:"input"`
		Canonical string `json:"canonical"`
	} `json:"valid"`
	Invalid   []string    `json:"invalid"`
	Ascending []string    `json:"ascending"`
	Equal     [][2]string `json:"equal"`
}

func loadSemverVectors(t *testing.T) semverVectors {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "docs", "kernel", "wire-protocol", "testdata", "semver-order.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v semverVectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	if len(v.Valid) == 0 || len(v.Invalid) == 0 || len(v.Ascending) < 2 {
		t.Fatal("semver vectors are empty; wrong file?")
	}
	return v
}

func TestSemverVectorsParse(t *testing.T) {
	v := loadSemverVectors(t)
	for _, c := range v.Valid {
		got, err := ParseVersion(c.Input)
		if err != nil {
			t.Errorf("%q: %v", c.Input, err)
			continue
		}
		if got.String() != c.Canonical {
			t.Errorf("%q canonicalised to %q, want %q", c.Input, got.String(), c.Canonical)
		}
	}
	for _, in := range v.Invalid {
		if _, err := ParseVersion(in); err == nil {
			t.Errorf("%q parsed; it must not", in)
		}
	}
}

func TestSemverVectorsOrder(t *testing.T) {
	v := loadSemverVectors(t)
	parsed := make([]Version, len(v.Ascending))
	for i, s := range v.Ascending {
		p, err := ParseVersion(s)
		if err != nil {
			t.Fatalf("%q: %v", s, err)
		}
		parsed[i] = p
	}
	for i := range parsed {
		for j := range parsed {
			want := 0
			switch {
			case i < j:
				want = -1
			case i > j:
				want = 1
			}
			if got := Compare(parsed[i], parsed[j]); got != want {
				t.Errorf("Compare(%s, %s) = %d, want %d", v.Ascending[i], v.Ascending[j], got, want)
			}
		}
	}
	for _, pair := range v.Equal {
		a, err1 := ParseVersion(pair[0])
		b, err2 := ParseVersion(pair[1])
		if err1 != nil || err2 != nil {
			t.Fatalf("%v: %v %v", pair, err1, err2)
		}
		if Compare(a, b) != 0 {
			t.Errorf("%v should compare equal", pair)
		}
	}
}
