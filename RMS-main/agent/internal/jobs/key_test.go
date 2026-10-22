package jobs

import "testing"

func TestCapabilityKeyGroupsAJobTypeByItsModule(t *testing.T) {
	for _, tc := range []struct{ in, want string }{
		{"backup.run", "backup"},
		{"backup.restore", "backup"},
		{"patch.apply", "patch"},
		{"hello", "hello"},
		{"a.b.c", "a"},
		{"", ""},
		{".leading", ".leading"},
	} {
		if got := capabilityKey(tc.in); got != tc.want {
			t.Errorf("capabilityKey(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}
